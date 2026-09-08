import contextlib
import io
import json
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from scripts.holeclaw_archive_sink import ArchiveSink
from scripts.holeclaw_archive_store import ArchiveStore
from scripts.holeclaw_cache import CacheStore
from scripts.holeclaw_checkpoint import new_checkpoint
from scripts.holeclaw_digest import run_digest
from scripts.holeclaw_domain import CliError, FilterSpec
from scripts.holeclaw_media import MAX_IMAGE_BYTES, MediaStore
from scripts.holeclaw_reporting import one_line_summary
from scripts.holeclaw_runner import CollectorServices
from scripts.holeclaw_search import install_search_indexes, search_rows
from scripts.holeclaw_sink import SITE_ORIGIN, SinkServer
from scripts.run_digest import build_parser


class ImprovementsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = ArchiveStore(self.root / 'archive.sqlite3', 'test')
        self.addCleanup(self.archive.close)
        self.source = CacheStore(self.root / 'source.sqlite3')
        self.addCleanup(self.source.close)
        self.checkpoint = new_checkpoint({}, 100, 200, 100, 'test')
        self.checkpoint['archive_summary_version'] = 1
        self.sink = ArchiveSink(self.archive, self.checkpoint, self.root / 'checkpoint.json',
                                None, None, source_cache=self.source)
        self.post = dict(pid='1', timestamp=150, reply=1, favorites=0, type='image',
                         text='测试正文', media_ids=['10'])
        self.image = b'\x89PNG\r\n\x1a\nfixture'

    def message(self, kind, request_id=1, **payload):
        return dict(schema_version=3, kind=kind, request_id=request_id,
                    run_id=self.checkpoint['created_at'], payload=payload)

    def enable_media(self):
        self.archive.media = MediaStore(self.archive, self.root / 'images', True)
        self.archive.media.record_post(self.post)

    def binary_message(self, request_id=1):
        return self.message('archive_media_file', request_id, post=self.post,
                            media_key='id:10', mime='image/png')

    def test_unavailable_posts_resume_without_claiming_comments_complete(self):
        message = self.message('archive_post_unavailable', post=self.post)
        self.sink.ingest(message)
        self.sink.ingest(message)
        resume = self.archive.prepare_posts([self.post], 'later')['1']
        self.assertEqual(resume['unavailable'], 'post_not_found')
        self.assertFalse(resume['complete'])
        self.assertNotIn('unavailable', self.archive.prepare_posts([self.post], 'fresh', fresh=True)['1'])
        summary = self.archive.window_summary(self.checkpoint, FilterSpec(None, None))
        self.assertEqual((summary['unavailable_posts'], summary['completed_comment_posts'],
                          summary['incomplete_comment_posts']), (1, 0, 0))
        self.assertEqual(self.checkpoint['archive_metrics']['unavailable_posts'], 1)
        self.archive.ingest_comments(dict(post=self.post, archive_run='fresh', comment_page=1,
                                         complete=True, comments=[]))
        self.assertTrue(self.archive.resume_comments('1', 'again', 1)['complete'])

    def test_unavailable_cached_candidates_can_finish_offline(self):
        self.source.upsert_posts([self.post])
        cp = dict(self.checkpoint, cached_posts=1, archive_cached_pages=1)
        self.archive.stage_candidates(self.source.path, cp, FilterSpec(None, None), 200)
        self.archive.record_unavailable(self.post, cp['created_at'])
        self.assertTrue(self.archive.finish_cached_candidates(cp))
        self.assertEqual(self.archive.cached_work_page(cp, 1)['posts'], [])

    def test_new_reply_observation_rechecks_unavailable_images(self):
        self.enable_media()
        self.archive.record_unavailable(self.post, 'old')
        self.assertTrue(self.archive.prepare_posts([self.post], 'same')['1']['unavailable'])
        resume = self.archive.prepare_posts([dict(self.post, reply=2)], 'new')['1']
        self.assertFalse(resume.get('unavailable'))
        self.assertFalse(resume['post_known'])
        self.assertFalse(resume['complete'])

    def test_indexed_search_preserves_substrings_and_transaction_updates(self):
        db = self.archive.connection
        samples = ['北大树洞中文测试', 'AbC abc a"b %_词 😀中文', '另一条北大树洞', 'x\x00y']
        self.archive.upsert_posts([dict(self.post, pid=str(i), text=text) for i, text in enumerate(samples)])
        self.archive.ingest_comments(dict(post=self.post, archive_run='test', comment_page=1,
            complete=True, comments=[dict(cid='1', text=samples[1])]))
        for query in ('北大树洞', '树洞', 'AbC', 'abc', 'a"b', '%_词', '😀中文', '\x00', '', 'no match'):
            expected = db.execute("""SELECT 'post' AS kind,pid,NULL AS cid,timestamp,text FROM posts
                WHERE instr(text,?)>0 UNION ALL SELECT 'comment',pid,cid,timestamp,text FROM comments
                WHERE instr(text,?)>0 ORDER BY timestamp DESC LIMIT 100""", (query, query)).fetchall()
            actual = search_rows(db, query, 100)
            self.assertEqual(sorted(map(tuple, actual)), sorted(map(tuple, expected)), query)
        with self.assertRaises(RuntimeError), self.archive.transaction():
            self.archive.upsert_posts([dict(self.post, text='rollback keyword')])
            raise RuntimeError('rollback')
        self.assertEqual(search_rows(db, 'rollback', 100), [])
        self.archive.upsert_posts([dict(self.post, text='replacement keyword')])
        self.assertEqual(len(search_rows(db, 'replacement', 100)), 1)
        with self.archive.transaction():
            db.execute('DELETE FROM posts WHERE pid=?', ('1',))
        self.assertEqual(search_rows(db, 'replacement', 100), [])

    def test_legacy_search_and_index_rebuild(self):
        self.archive.upsert_posts([dict(self.post, text='旧档案中文搜索')])
        db = self.archive.connection
        for table in ('posts', 'comments'):
            for action in ('insert', 'delete', 'update'):
                db.execute(f'DROP TRIGGER IF EXISTS {table}_search_{action}')
            db.execute(f'DROP TABLE IF EXISTS {table}_search')
        self.assertEqual(len(search_rows(db, '中文搜索', 100)), 1)
        install_search_indexes(self.archive)
        self.assertEqual(len(search_rows(db, '中文搜索', 100)), 1)
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='posts_search'").fetchone():
            self.skipTest('SQLite trigram tokenizer unavailable')
        plan = db.execute("EXPLAIN QUERY PLAN SELECT rowid FROM posts_search WHERE posts_search MATCH ?",
                          ('"中文搜索"',)).fetchall()
        self.assertTrue(any('VIRTUAL TABLE INDEX' in row[3] for row in plan))

    def test_sqlite_without_trigram_retains_read_only_search(self):
        db = self.archive.connection
        self.archive.upsert_posts([dict(self.post, text='中文搜索回退')])
        for table in ('posts', 'comments'):
            for action in ('insert', 'delete', 'update'):
                db.execute(f'DROP TRIGGER IF EXISTS {table}_search_{action}')
            db.execute(f'DROP TABLE IF EXISTS {table}_search')
        class WithoutTrigram:
            def execute(self, sql, *args):
                if 'CREATE VIRTUAL TABLE' in sql:
                    raise sqlite3.OperationalError('no such tokenizer: trigram')
                return db.execute(sql, *args)
        with patch.object(self.archive, 'connection', WithoutTrigram()):
            install_search_indexes(self.archive)
        self.assertEqual(len(search_rows(db, '中文搜索', 100)), 1)
        self.assertFalse(db.in_transaction)

    def test_binary_retries_validate_content_and_leave_no_partial_files(self):
        self.enable_media()
        message = self.binary_message()
        for _ in range(2):
            self.sink.ingest_media_stream(message, io.BytesIO(self.image), len(self.image))
        self.assertEqual(self.checkpoint['archive_metrics']['image_receipts'], 1)
        with self.assertRaisesRegex(CliError, 'changed'):
            changed = self.image + b'changed'
            self.sink.ingest_media_stream(message, io.BytesIO(changed), len(changed))
        with self.assertRaisesRegex(CliError, 'Incomplete'):
            self.sink.ingest_media_stream(self.binary_message(2), io.BytesIO(b'partial'), 100)
        with self.assertRaisesRegex(CliError, '20 MiB'):
            self.sink.ingest_media_stream(self.binary_message(3), io.BytesIO(), MAX_IMAGE_BYTES + 1)
        files = list((self.root / 'images').iterdir())
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_bytes(), self.image)

    def test_binary_fsync_does_not_block_comment_commits(self):
        self.enable_media()
        entered, release = threading.Event(), threading.Event()
        def slow_fsync(_fd):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('test timed out')
        with ThreadPoolExecutor(max_workers=2) as workers, patch('scripts.holeclaw_media.os.fsync', slow_fsync):
            upload = workers.submit(self.sink.ingest_media_stream, self.binary_message(), io.BytesIO(self.image), len(self.image))
            try:
                self.assertTrue(entered.wait(1))
                comment = self.message('archive_comments', 2, post=self.post, comment_page=1,
                    complete=True, comments=[dict(cid='1', text='saved during fsync', media_ids=[])])
                workers.submit(self.sink.ingest, comment).result(timeout=1)
                self.assertEqual(self.archive.summary()['comments'], 1)
            finally:
                release.set()
            upload.result(timeout=1)

    def test_cancel_during_binary_receive_never_publishes_file(self):
        self.enable_media()
        sink = self.sink
        class CancellingStream(io.BytesIO):
            def read(self, size):
                data = super().read(size)
                sink.cancel()
                return data
        with self.assertRaisesRegex(CliError, 'cancelled'):
            self.sink.ingest_media_stream(self.binary_message(), CancellingStream(self.image), len(self.image))
        self.assertEqual(list((self.root / 'images').iterdir()), [])
        self.assertEqual(self.archive.connection.execute('SELECT COUNT(*) FROM media_files').fetchone()[0], 0)

    def test_binary_http_auth_cors_and_atomic_upload(self):
        self.enable_media()
        with SinkServer(self.sink) as server:
            url = server.url.replace('/ingest?', '/media?')
            headers = {'Origin': SITE_ORIGIN, 'Content-Type': 'application/octet-stream',
                       'X-Holeclaw-Message': quote(json.dumps(self.binary_message()))}
            for _ in range(2):
                with urlopen(Request(url, data=self.image, headers=headers), timeout=3) as reply:
                    self.assertTrue(json.load(reply)['ok'])
            with urlopen(Request(url, method='OPTIONS', headers={'Origin': SITE_ORIGIN}), timeout=3) as reply:
                self.assertIn('x-holeclaw-message', reply.headers['Access-Control-Allow-Headers'])
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(url, data=self.image, headers=dict(headers, Origin='https://invalid.test')), timeout=3)
            self.assertEqual(error.exception.code, 403)
        self.assertEqual(self.checkpoint['archive_metrics']['image_receipts'], 1)

    @unittest.skipUnless(shutil.which('node'), 'Node required')
    def test_javascript_binary_upload_reaches_real_python_sink(self):
        self.enable_media()
        program = r'''
const {runCollector,response} = require('./tests/collector_harness');
runCollector({config: {archive:true,archive_run:process.argv[2],extract_images:true,download_images:true,
  report_start_timestamp:100,scan_start_timestamp:100,end_timestamp:200,
  min_comments:null,min_favorites:null,max_pages:1,request_concurrency:1,sink_url:process.argv[1]},
  wireSinkFetch:(url,options)=>fetch(url,{...options,headers:{...options.headers,Origin:'https://treehole.pku.edu.cn'}}),
  remoteFetch:async url => {
    if(url.includes('getMediaBinary'))return new Response(new Uint8Array([137,80,78,71,13,10,26,10]),{headers:{'content-type':'image/png'}});
    if(url.includes('/hole/one'))return response({code:20000,data:{hole:{media_ids:['10']}}});
    return response({code:20000,data:{list:url.includes('list_comments')?
      [{pid:'1',timestamp:150,reply:0,likenum:0,text:'fixture'}]:[]}});
  }
}).catch(error=>{console.error(error);process.exitCode=1;});
'''
        with SinkServer(self.sink) as server:
            subprocess.run(['node', '-e', program, server.url, self.checkpoint['created_at']],
                           cwd=Path(__file__).parents[1], check=True, capture_output=True, timeout=10)
        self.assertEqual(self.archive.media.pending('1'), [])
        self.assertEqual(self.checkpoint['archive_metrics']['image_receipts'], 1)


class ReportImprovementsTests(unittest.TestCase):
    def test_summary_caps_long_first_sentence_and_unpunctuated_text(self):
        for text in ('x' * 10000, '很长的第一句话' * 1000 + '。下一句。'):
            self.assertLessEqual(len(one_line_summary(text, 'text')), 113)
        self.assertEqual(one_line_summary('正常短句。', 'text'), '正常短句。')

    def test_digest_verification_is_explicit_for_network_and_cached_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = build_parser().parse_args(['standalone', '--since', '2020-01-01', '--until', '2020-01-02',
                '--cache', str(root / 'cache.sqlite3'), '--checkpoint', str(root / 'cp.json'),
                '--output', str(root / 'report.md')])
            services = CollectorServices(MagicMock(), MagicMock(return_value={'reached_start': True}))
            for fresh, verify in ((True, False), (True, True), (False, False), (False, True)):
                args.fresh, args.verify_cache = fresh, verify
                with patch.object(CacheStore, 'integrity_check', return_value='ok') as check, \
                        contextlib.redirect_stdout(io.StringIO()) as output:
                    run_digest(args, standalone=True, services=services)
                self.assertEqual(check.call_count, int(verify))
                self.assertEqual(json.loads(output.getvalue().splitlines()[-1])['cache_integrity'],
                                 'ok' if verify else 'not_checked')
