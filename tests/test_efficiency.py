import tempfile
import unittest
import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

from scripts.holeclaw_archive_store import ArchiveStore
from scripts.holeclaw_media import MediaStore
from scripts.holeclaw_cache import CacheStore
from scripts.holeclaw_checkpoint import new_checkpoint
from scripts.holeclaw_sink import RunSink
from scripts.holeclaw_sink import SinkServer
from scripts.holeclaw_archive_sink import ArchiveSink
from scripts.holeclaw_domain import CliError, FilterSpec


class EfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ArchiveStore(self.root / 'archive.db', 'test')
        self.addCleanup(self.store.close)
        self.post = dict(pid='1', timestamp=150, reply=0, favorites=1, text='fixture', type='text')

    def test_zero_comments_batch_completes_in_single_transaction(self):
        posts = [dict(self.post, pid=str(i)) for i in range(500)]
        statements = []
        self.store.connection.set_trace_callback(statements.append)
        resumes = self.store.prepare_posts(posts, 'run')
        self.store.connection.set_trace_callback(None)
        self.assertTrue(all(row['complete'] and row['empty_completed'] for row in resumes.values()))
        self.assertEqual(sum(s == 'COMMIT' for s in statements), 1)
        self.assertEqual(self.store.summary()['posts_with_completed_comment_scan'], 500)
        again = self.store.prepare_posts(posts, 'run')
        self.assertTrue(all(row['complete'] and not row.get('empty_completed') for row in again.values()))

    @unittest.skipUnless(shutil.which('node'), 'Node required')
    def test_500_zero_comment_posts_need_only_three_real_http_callbacks(self):
        program = r'''
const {runCollector,response}=require('./tests/collector_harness');
runCollector({config:{archive:true,archive_run:process.argv[2],report_start_timestamp:100,
  scan_start_timestamp:100,end_timestamp:200,min_comments:null,min_favorites:null,max_pages:1,
  sink_url:process.argv[1]},wireSinkFetch:(url,options)=>fetch(url,{...options,
    headers:{...options.headers,Origin:'https://treehole.pku.edu.cn'}}),
  remoteFetch:async()=>response({code:20000,data:{list:Array.from({length:500},(_,i)=>({
    pid:String(i),timestamp:150,reply:0,likenum:0,text:'fixture'}))}})
}).then(result=>process.stdout.write(JSON.stringify(result.wirePayloads.map(p=>p.kind))))
  .catch(error=>{console.error(error);process.exitCode=1;});
'''
        with CacheStore(self.root / 'source.db') as source:
            cp = new_checkpoint({}, 100, 200, 100, 'test')
            sink = ArchiveSink(self.store, cp, self.root / 'cp.json', None, None, source_cache=source)
            with SinkServer(sink) as server:
                result = subprocess.run(['node', '-e', program, server.url, cp['created_at']],
                    cwd=Path(__file__).parents[1], capture_output=True, text=True, check=True, timeout=15)
            self.assertEqual(json.loads(result.stdout), ['archive_prepare', 'list_chunk', 'telemetry_final'])
        self.assertEqual(self.store.summary()['posts_with_completed_comment_scan'], 500)

    def test_empty_comments_preserve_history_and_require_post_image_metadata(self):
        self.store.ingest_comments(dict(post=dict(self.post, reply=1), archive_run='old',
            comment_page=1, complete=True, comments=[dict(cid='1', text='historical')]))
        self.store.media = MediaStore(self.store, self.root / 'images', True)
        resume = self.store.prepare_posts([self.post], 'run')['1']
        self.assertTrue(resume['complete'])
        self.assertFalse(resume['post_known'])
        self.assertTrue(self.store.media.state('1', 0)['comments_known'])
        self.assertEqual(self.store.summary()['comments'], 1)

    def test_empty_batch_rolls_back_when_registration_fails(self):
        with patch.object(self.store, 'record_posts', side_effect=RuntimeError('failure')):
            with self.assertRaises(RuntimeError):
                self.store.prepare_posts([self.post], 'run')
        self.assertEqual(self.store.summary()['posts_with_completed_comment_scan'], 0)

    def test_cached_candidates_return_prepared_state_without_second_lookup(self):
        with CacheStore(self.root / 'source.db') as source:
            post = dict(self.post, reply=2)
            source.upsert_posts([post])
            cp = dict(created_at='run', start_timestamp=100, cached_posts=1, archive_cached_pages=1)
            self.store.stage_candidates(source.path, cp, FilterSpec(None, None), 200)
            with patch.object(self.store, 'resume_batch', wraps=self.store.resume_batch) as resume:
                result = self.store.cached_work_page(cp, 1)
            self.assertEqual(resume.call_count, 1)
            self.assertFalse(result['resumes']['1']['complete'])
            self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM archive_members').fetchone()[0], 1)

    def test_identical_media_references_do_not_write_rows(self):
        media = self.store.media = MediaStore(self.store, self.root / 'images', True)
        payload = dict(post=self.post, complete=True,
                       comments=[dict(cid=str(i), media_ids=['a', 'b']) for i in range(500)])
        with self.store.transaction():
            media.record_comments(payload)
        before = self.store.connection.total_changes
        statements = []
        self.store.connection.set_trace_callback(statements.append)
        with self.store.transaction():
            media.record_comments(payload)
        self.store.connection.set_trace_callback(None)
        self.assertEqual(self.store.connection.total_changes - before, 1, 'only scan state should be updated')
        self.assertEqual(sum(s.lstrip().startswith('SELECT') for s in statements), 1)
        payload['comments'] = [dict(cid='1', media_ids=['b','a']), dict(cid='1', media_ids=['c'])]
        with self.store.transaction():
            media.record_comments(payload)
        self.assertEqual([row[0] for row in self.store.connection.execute(
            "SELECT media_key FROM media_refs WHERE pid='1' AND cid='1'")], ['id:c'])

    def test_media_keyset_queue_is_bounded_and_scans_refs_once(self):
        media = self.store.media = MediaStore(self.store, self.root / 'images', True)
        comments = [dict(cid=str(i), media_ids=[f'{i}-{j}' for j in range(100)]) for i in range(4)]
        with self.store.transaction():
            media.record_comments(dict(post=self.post, complete=True, comments=comments))
        statements, seen = [], set()
        self.store.connection.set_trace_callback(statements.append)
        for serial in range(1, 30):
            planned = media.plan('1', str(serial))
            self.assertEqual(media.plan('1', str(serial)), planned)
            if not planned:
                break
            self.assertLessEqual(len(media.pending_queues['1']['rows']), 128)
            for item in planned:
                self.assertNotIn(item['media_key'], seen)
                seen.add(item['media_key'])
                media.save('1', dict(media_key=item['media_key'], status='unavailable'))
        self.store.connection.set_trace_callback(None)
        self.assertEqual(len(seen), 400)
        self.assertEqual(sum('SELECT DISTINCT media_key,source_url FROM media_refs' in s for s in statements), 4)
        self.assertEqual(len(media.pending_queues), 0)
        media.record_post(dict(self.post, media_ids=['new']))
        self.assertEqual(media.plan('1', 'new')[0]['media_key'], 'id:new')
        media.cancel_plans()
        self.assertFalse(media.pending_queues)

    def test_deferred_favorites_do_not_create_complete_favorite_coverage(self):
        with CacheStore(self.root / 'source.db') as cache:
            cp = new_checkpoint({}, 100, 200, 100, 'test')
            sink = RunSink(cache, cp, self.root / 'cp.json', 10, 20)
            post = dict(self.post, favorites=None)
            chunk = dict(schema_version=2, start_page=1, end_page=1, pages=1, scanned=1,
                rows=[post], matched_pids=[], favorite_deferred_pids=['1'], terminal=True, reached_start=True)
            sink.ingest(chunk)
            self.assertFalse(cp['favorites_complete'])
            self.assertEqual(cache.query_favorite_unavailable(100, 200), [])
            cache.add_coverage(100, 200, 'now', 1, 1, cp['favorites_complete'])
            self.assertIsNotNone(cache.find_covering(100, 200))
            self.assertIsNone(cache.find_covering(100, 200, require_favorites=True))

    def test_deferred_favorites_reject_potential_matches_and_or_filters(self):
        with CacheStore(self.root / 'source.db') as cache:
            for mode, reply in [('all', 11), ('any', 0)]:
                sink = RunSink(cache, new_checkpoint({}, 100, 200, 100, 'test'), self.root / 'cp.json', 10, 20, mode)
                with self.assertRaises(CliError):
                    sink.ingest(dict(schema_version=2, start_page=1, end_page=1, pages=1, scanned=1,
                        rows=[dict(self.post, reply=reply, favorites=None)], favorite_deferred_pids=['1']))
            self.assertEqual(cache.post_count(), 0)
