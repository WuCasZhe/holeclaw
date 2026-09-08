import base64
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import run_digest as runtime
from scripts.holeclaw_archive import ArchiveSink, ArchiveStore, run_archive
from scripts.holeclaw_cache import CacheStore
from scripts.holeclaw_domain import FilterSpec
from scripts.holeclaw_media import MediaStore
from scripts.holeclaw_runner import CollectorServices


class CacheReuseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ArchiveStore(Path(self.temp.name) / 'archive.sqlite3', 'test')
        self.addCleanup(self.store.close)
        self.source = CacheStore(Path(self.temp.name) / 'source.sqlite3')
        self.addCleanup(self.source.close)
        self.post = dict(pid='1', timestamp=150, reply=1, favorites=2,
                         type='text', text='cached', observed_at=1000)

    def complete(self, posts):
        with self.store.transaction():
            self.store.upsert_posts(posts)
            self.store.connection.executemany('''INSERT INTO comment_scans
                (pid,last_page,complete,observed_at,run_id,reply_count,page_size)
                VALUES(?,2,1,1000,'old',?,100)''', [(p['pid'], p['reply']) for p in posts])

    def stage(self, posts):
        checkpoint = runtime.new_checkpoint({}, 100, 200, 100, 'test')
        checkpoint['archive_summary_version'] = 1
        self.source.upsert_posts(posts)
        count = self.store.stage_candidates(self.source.path, checkpoint, FilterSpec(None, None), 200)
        checkpoint.update(cached_posts=count, archive_cached_pages=(count + 499) // 500)
        return checkpoint

    def test_500_reused_posts_use_three_queries_and_no_row_writes(self):
        posts = [dict(self.post, pid=str(i)) for i in range(500)]
        self.complete(posts)
        self.store.prepare_posts(posts, 'new')
        statements = []
        before = self.store.connection.total_changes
        self.store.connection.set_trace_callback(statements.append)
        resumes = self.store.prepare_posts(posts, 'new')
        self.store.connection.set_trace_callback(None)
        self.assertTrue(all(r['complete'] for r in resumes.values()))
        self.assertEqual(self.store.connection.total_changes, before)
        self.assertEqual(sum(s.lstrip().startswith('SELECT') for s in statements), 3)
        self.assertFalse(any(s.lstrip().split()[0] in ('INSERT', 'UPDATE', 'DELETE') for s in statements))
        self.assertEqual(self.store.connection.execute('SELECT MIN(observed_at) FROM posts').fetchone()[0], 1000)

    def test_batch_reads_cross_sqlite_parameter_boundary(self):
        posts = [dict(self.post, pid=str(i)) for i in range(1001)]
        self.complete(posts)
        self.assertEqual(len(self.store.prepare_posts(posts, 'new')), 1001)
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM archive_members').fetchone()[0], 1001)

    def test_old_observation_preserves_newer_data_and_fills_missing_fields(self):
        self.store.upsert_posts([dict(self.post, observed_at=2000, reply=9, text='new detail')])
        self.store.upsert_posts([dict(self.post, reply=2)])
        row = self.store.rows_by_pid('posts', ['1'])['1']
        self.assertEqual((row['reply'], row['text'], row['observed_at']), (9, 'new detail', 2000))
        self.store.upsert_posts([dict(self.post, observed_at=3000, reply=10, favorites=None, text='')])
        row = self.store.rows_by_pid('posts', ['1'])['1']
        self.assertEqual((row['reply'], row['text'], row['favorites'], row['observed_at']), (10, 'new detail', 2, 3000))
        self.store.upsert_posts([dict(self.post, pid='2', favorites=None, text='', observed_at=2000)])
        self.store.record_favorite_unavailable([dict(pid='2')])
        self.store.upsert_posts([dict(self.post, pid='2')])
        row = self.store.rows_by_pid('posts', ['2'])['2']
        self.assertEqual((row['text'], row['favorites'], row['observed_at']), ('cached', 2, 2000))
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM favorite_unavailable').fetchone()[0], 0)

    def test_batch_resume_keeps_fresh_reply_and_page_size_rules(self):
        posts = [dict(self.post, pid=str(i)) for i in range(4)]
        self.complete(posts)
        self.store.connection.execute("UPDATE comment_scans SET complete=0,run_id='new',last_page=7 WHERE pid IN ('1','2')")
        self.store.connection.execute("UPDATE comment_scans SET page_size=10 WHERE pid='2'")
        posts[3]['reply'] += 1
        for fresh in (False, True):
            batch = self.store.resume_batch(posts, 'new', fresh)
            for post in posts:
                self.assertEqual(batch[post['pid']], self.store.resume_comments(post['pid'], 'new', post['reply'], fresh))
        self.assertEqual(self.store.resume_batch(posts, 'new')['1']['next_page'], 7)
        self.assertEqual(self.store.resume_batch(posts, 'new')['2']['next_page'], 1)

    def test_partial_local_reuse_preserves_pages_retries_and_members(self):
        complete = [dict(self.post, pid=str(i), timestamp=160) for i in range(500)]
        missing = dict(self.post, pid='missing', timestamp=140)
        self.complete(complete)
        checkpoint = self.stage(complete + [missing])
        path = Path(self.temp.name) / 'checkpoint.json'
        sink = ArchiveSink(self.store, checkpoint, path, None, None, source_cache=self.source)
        message = dict(schema_version=3, kind='archive_source', run_id=checkpoint['created_at'],
                       request_id=1, payload=dict(page=1))
        result = sink.ingest(message)
        self.assertEqual(result, dict(posts=[], source_count=500, oldest=160, reused=500, resumes={}))
        self.assertEqual(sink.ingest(message), result)
        self.assertEqual(sink.reused_posts, 500)
        sink.ingest(dict(schema_version=2, archive_cached=True, start_page=1, end_page=1,
                         pages=1, scanned=0, oldest=160, checkpoint=True))
        resumed = runtime.read_checkpoint(path)
        self.assertEqual((resumed['next_page'], resumed['archive_cached_pages']), (2, 2))
        sink = ArchiveSink(self.store, resumed, path, None, None, source_cache=self.source)
        result = sink.ingest(dict(message, request_id=2, payload=dict(page=2)))
        self.assertEqual([p['pid'] for p in result['posts']], ['missing'])
        self.assertEqual(result['oldest'], 140)
        self.assertEqual(result['posts'][0]['observed_at'], 1000)
        self.store.prepare_posts(result['posts'], resumed['created_at'])
        summary = self.store.window_summary(resumed, FilterSpec(None, None))
        self.assertEqual((summary['posts'], summary['completed_comment_posts']), (501, 500))
        self.assertEqual(len(self.store.candidate_page(resumed['created_at'], 1)), 500)

    def test_image_reuse_checks_shared_file_once_and_repairs_deletion(self):
        media = self.store.media = MediaStore(self.store, Path(self.temp.name) / 'images', True)
        posts = [dict(self.post, pid=str(i), media_ids=['10']) for i in range(2)]
        checkpoint = self.stage(posts)
        for post in posts:
            media.record_post(post)
            self.store.ingest_comments(dict(post=post, comments=[], comment_page=2,
                                            complete=True, archive_run='old'))
        media.save('0', dict(media_key='id:10', data=base64.b64encode(b'\x89PNG\r\n\x1a\nfixture').decode()))
        target = next(media.directory.iterdir())
        original_stat = Path.stat
        calls = []
        def count_stat(path, *args, **kwargs):
            if path == target:
                calls.append(path)
            return original_stat(path, *args, **kwargs)
        with patch.object(Path, 'stat', count_stat):
            self.assertEqual(self.store.cached_work_page(checkpoint, 1)['posts'], [])
        self.assertEqual(len(calls), 1)
        target.unlink()
        self.assertEqual(len(self.store.cached_work_page(checkpoint, 1)['posts']), 2)
        fresh = dict(checkpoint, fresh=True)
        self.assertEqual(len(self.store.cached_work_page(fresh, 1)['posts']), 2)

    def test_local_reuse_rolls_back_if_member_registration_fails(self):
        self.complete([self.post])
        checkpoint = self.stage([dict(self.post, observed_at=2000, text='new snapshot')])
        self.store.connection.execute('''CREATE TEMP TRIGGER fail_member BEFORE INSERT ON archive_members
            BEGIN SELECT RAISE(ABORT, 'test registration failure'); END''')
        with self.assertRaisesRegex(Exception, 'test registration failure'):
            self.store.cached_work_page(checkpoint, 1)
        row = self.store.rows_by_pid('posts', ['1'])['1']
        self.assertEqual((row['text'], row['observed_at']), ('cached', 1000))
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM archive_members').fetchone()[0], 0)

    def test_legacy_candidate_observation_is_unknown_and_snapshot_stays_resumable(self):
        self.complete([self.post])
        checkpoint = self.stage([self.post])
        # Rebuild just the candidate table with the schema used by existing archives.
        self.store.connection.execute('''CREATE TABLE legacy_candidates AS SELECT
            run_id,ordinal,pid,timestamp,reply,favorites,type,text FROM archive_candidates''')
        self.store.connection.execute('DROP TABLE archive_candidates')
        self.store.connection.execute('ALTER TABLE legacy_candidates RENAME TO archive_candidates')
        with contextlib.closing(ArchiveStore(self.store.path, 'test')) as migrated:
            self.assertEqual(migrated.candidate_page(checkpoint['created_at'], 1)[0]['observed_at'], 0)
            self.assertEqual(migrated.cached_work_page(checkpoint, 1)['reused'], 1)
            self.assertEqual(migrated.rows_by_pid('posts', ['1'])['1']['observed_at'], 1000)

    def test_integrity_scan_is_explicit_and_cli_forwards_option(self):
        args = runtime.build_parser().parse_args(['archive', '-a', 'test',
            '--cache', str(self.store.path), '--source-cache', str(self.source.path),
            '--checkpoint', str(Path(self.temp.name) / 'cp.json'),
            '--since', '2020-01-01', '--until', '2020-01-02'])
        start, end, _ = runtime.time_window(args)
        self.source.add_coverage(start, end, 'old', 0, 0, True)
        services = CollectorServices(lambda _: self.fail('browser unnecessary'), None)
        for verify in (False, True):
            args.verify_cache = verify
            output = io.StringIO()
            with patch.object(ArchiveStore, 'integrity_check', return_value='ok') as check, contextlib.redirect_stdout(output):
                run_archive(args, services)
            self.assertEqual(check.call_count, int(verify))
            self.assertEqual(json.loads(output.getvalue())['cache_integrity'], 'ok' if verify else 'not_checked')


if __name__ == '__main__':
    unittest.main()
