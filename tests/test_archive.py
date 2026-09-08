import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import run_digest as runtime
from scripts.holeclaw_runner import CollectorServices
from scripts.holeclaw_archive import ArchiveStore, ArchiveSink, run_archive, search_archive
from scripts.holeclaw_cache import CacheStore
from scripts.holeclaw_domain import CliError


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'archive.sqlite3'
        self.store = ArchiveStore(self.path, 'test-account')
        self.post = dict(pid='1', timestamp=150, reply=0, favorites=0, text='正文', type='text')
        self.payload = dict(post=self.post, comment_page=1, complete=False, archive_run='run-1',
                            comments=[dict(cid='2', timestamp=151, text='评论关键词', name_tag='Alice', quote_cid='3')])

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_comment_commit_replay_resume_and_new_run(self):
        self.store.ingest_comments(self.payload)
        self.store.ingest_comments(self.payload)
        self.assertEqual(self.store.summary()['comments'], 1)
        self.assertEqual(self.store.resume_comments('1', 'run-1'), dict(next_page=1, complete=False))
        self.store.ingest_comments(dict(self.payload, comment_page=2, comments=[], complete=True))
        self.assertTrue(self.store.resume_comments('1', 'run-1')['complete'])
        self.assertFalse(self.store.resume_comments('1', 'run-2')['complete'])
        self.assertEqual(self.store.summary()['posts_with_completed_comment_scan'], 1)

    def test_invalid_comments_do_not_commit_post(self):
        with self.assertRaises(CliError):
            self.store.ingest_comments(dict(self.payload, comments=[{'text': 'missing id'}]))
        self.assertEqual(self.store.post_count(), 0)

    def test_capped_resume_preserves_existing_comments(self):
        post = dict(self.post, reply=1200)
        payload = dict(self.payload, post=post)
        comments = [dict(cid=str(i), text='cached') for i in range(1200)]
        self.store.ingest_comments(dict(payload, comments=comments, comment_page=120))
        resume = self.store.prepare_posts([post], 'run-1')['1']
        self.assertEqual(len(resume['saved_comment_ids']), 1000)
        self.assertEqual(self.store.summary()['comments'], 1200)
        self.store.ingest_comments(dict(payload, comments=[], comment_page=120, complete=True))
        self.assertTrue(self.store.resume_comments('1', 'run-2', reply=1200)['complete'])
        self.assertEqual(self.store.summary()['comments'], 1200)

    def test_account_and_digest_isolation(self):
        with self.assertRaisesRegex(CliError, 'archive database'):
            CacheStore(self.path)
        with self.assertRaises(CliError):
            ArchiveStore(self.path, 'another-account')
        digest_path = Path(self.temp.name) / 'digest.sqlite3'
        CacheStore(digest_path).close()
        with self.assertRaises(CliError):
            ArchiveStore(digest_path, 'test-account')

    def test_search_reads_posts_and_comments(self):
        self.store.ingest_comments(self.payload)
        args = runtime.build_parser().parse_args(['archive-search', '--cache', str(self.path), '--query', '关键词'])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            search_archive(args)
        self.assertEqual(json.loads(output.getvalue())[0]['cid'], '2')

    def test_sink_checks_run_and_window(self):
        checkpoint = runtime.new_checkpoint({}, 100, 200, 100, 'test')
        sink = ArchiveSink(self.store, checkpoint, Path(self.temp.name) / 'checkpoint.json', None, None)
        with self.assertRaisesRegex(CliError, 'identity'):
            sink.ingest(dict(self.payload, schema_version=2, archive_comments=True))
        payload = dict(self.payload, archive_run=checkpoint['created_at'], schema_version=2, archive_comments=True)
        sink.ingest(payload)
        self.assertEqual(checkpoint['next_page'], 1, 'comment receipts must not advance feed checkpoint')
        with self.assertRaisesRegex(CliError, 'window'):
            sink.ingest(dict(payload, post=dict(self.post, timestamp=300)))

    def test_archive_runner_resumes_unfinished_checkpoint(self):
        checkpoint_path = Path(self.temp.name) / 'resume.json'
        args = runtime.build_parser().parse_args(['archive', '--account', 'test-account',
            '--cache', str(self.path), '--checkpoint', str(checkpoint_path),
            '--source-cache', str(Path(self.temp.name) / 'source.sqlite3'),
            '--since', '2020-01-01', '--until', '2020-01-02', '--non-interactive'])
        def interrupted(browser, args, checkpoint, sink, url):
            checkpoint['next_page'] = 2
            checkpoint['total_pages'] = 1
            raise CliError('network interruption')
        with patch.object(runtime, 'ensure_standalone_login'), patch.object(runtime, 'run_persistent_collector', side_effect=interrupted):
            with self.assertRaisesRegex(CliError, 'network interruption'):
                run_archive(args, CollectorServices(runtime.ensure_standalone_login, runtime.run_persistent_collector))
        def resumed(browser, args, checkpoint, sink, url):
            self.assertEqual(checkpoint['next_page'], 2)
            return dict(reached_start=True, feed_exhausted=False)
        with patch.object(runtime, 'ensure_standalone_login'), patch.object(runtime, 'run_persistent_collector', side_effect=resumed):
            run_archive(args, CollectorServices(runtime.ensure_standalone_login, runtime.run_persistent_collector))
        with patch.object(runtime, 'ensure_standalone_login', side_effect=AssertionError('should use completed archive')):
            run_archive(args, CollectorServices(runtime.ensure_standalone_login, runtime.run_persistent_collector))

    def test_filtered_archive_keeps_unmatched_rows_only_in_source_cache(self):
        source = CacheStore(Path(self.temp.name) / 'source.sqlite3')
        try:
            checkpoint = runtime.new_checkpoint({}, 100, 200, 100, 'test')
            sink = ArchiveSink(self.store, checkpoint, Path(self.temp.name) / 'cp.json', 100, 50,
                               source_cache=source)
            included = dict(self.post, reply=101, favorites=51)
            excluded = dict(self.post, pid='9', reply=100, favorites=51)
            receipt = sink.ingest(dict(schema_version=2, archive_prepare=True,
                                      archive_run=checkpoint['created_at'], posts=[included]))
            self.assertFalse(receipt['resumes']['1']['complete'])
            with self.assertRaisesRegex(CliError, 'filter'):
                sink.ingest(dict(schema_version=2, archive_prepare=True,
                                 archive_run=checkpoint['created_at'], posts=[excluded]))
            sink.ingest(dict(schema_version=2, start_page=1, end_page=1, pages=1,
                scanned=2, rows=[included, excluded], matched_pids=[], favorite_unavailable=[],
                telemetry={}, reached_start=True, terminal=True))
            self.assertEqual(source.post_count(), 2)
            self.assertEqual(self.store.post_count(), 1)
        finally:
            source.close()

    def test_comment_snapshot_reuse_and_refresh(self):
        self.store.ingest_comments(dict(self.payload, complete=True))
        self.assertTrue(self.store.resume_comments('1', 'new-run', reply=0)['complete'])
        self.assertFalse(self.store.resume_comments('1', 'new-run', reply=1)['complete'])
        self.assertFalse(self.store.resume_comments('1', 'new-run', reply=0, fresh=True)['complete'])
        self.assertFalse(self.store.resume_comments('1', 'run-1', reply=1)['complete'])

    def test_interrupted_transaction_rolls_back(self):
        with self.assertRaises(KeyboardInterrupt):
            with self.store.transaction():
                self.store.upsert_posts([self.post])
                raise KeyboardInterrupt()
        self.assertEqual(self.store.post_count(), 0)

    def test_cached_candidates_are_filtered_and_frozen(self):
        source = CacheStore(Path(self.temp.name) / 'source.sqlite3')
        try:
            source.upsert_posts([dict(self.post, reply=101, favorites=0),
                                 dict(self.post, pid='2', reply=0, favorites=51),
                                 dict(self.post, pid='3', reply=100, favorites=50)])
            checkpoint = runtime.new_checkpoint({}, 100, 200, 100, 'test')
            count = self.store.stage_candidates(source.path, checkpoint, runtime.FilterSpec(100, 50, 'any'), 200)
            self.assertEqual(count, 2)
            source.upsert_posts([dict(self.post, reply=999, text='changed')])
            rows = self.store.candidate_page(checkpoint['created_at'], 1)
            self.assertEqual({row['pid'] for row in rows}, {'1', '2'})
            self.assertEqual(next(row for row in rows if row['pid'] == '1')['reply'], 101)
        finally:
            source.close()

    def test_complete_source_and_comment_caches_need_no_browser(self):
        source_path = Path(self.temp.name) / 'source.sqlite3'
        args = runtime.build_parser().parse_args(['archive', '--account', 'test-account',
            '--cache', str(self.path), '--source-cache', str(source_path),
            '--checkpoint', str(Path(self.temp.name) / 'offline.json'),
            '--since', '2020-01-01', '--until', '2020-01-02', '--min-comments', '100'])
        start, end, _ = runtime.time_window(args)
        post = dict(self.post, timestamp=start + 1, reply=101)
        source = CacheStore(source_path)
        source.upsert_posts([post, dict(post, pid='2', reply=100)])
        source.add_coverage(start, end, '2020-01-03', 1, 2, True)
        source.close()
        self.store.ingest_comments(dict(self.payload, post=post, complete=True))
        with patch.object(runtime, 'ensure_standalone_login', side_effect=AssertionError('browser unnecessary')):
            run_archive(args, CollectorServices(runtime.ensure_standalone_login, runtime.run_persistent_collector))
        checkpoint = runtime.read_checkpoint(args.checkpoint)
        self.assertTrue(checkpoint['completed'])
        self.assertEqual(checkpoint['cached_posts'], 1)
        self.assertEqual(self.store.post_count(), 1)

    def test_progress_is_time_limited_and_cancel_rejects_writes(self):
        checkpoint = runtime.new_checkpoint({}, 100, 200, 100, 'test')
        source = CacheStore(Path(self.temp.name) / 'source.sqlite3')
        with patch('scripts.holeclaw_archive_sink.time.monotonic', return_value=0):
            sink = ArchiveSink(self.store, checkpoint, Path(self.temp.name) / 'cp.json', None, None,
                               source_cache=source)
        payload = dict(self.payload, schema_version=2, archive_comments=True, archive_run=checkpoint['created_at'])
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch('scripts.holeclaw_archive_sink.time.monotonic', return_value=299):
            for page in range(1, 31):
                sink.ingest(dict(schema_version=2, start_page=page, end_page=page, pages=1,
                                 scanned=0, terminal=False))
                sink.ingest(dict(payload, comment_page=page))
        self.assertEqual(output.getvalue(), '')
        with contextlib.redirect_stdout(output), patch('scripts.holeclaw_archive_sink.time.monotonic', return_value=300):
            sink.ingest(dict(payload, comment_page=31))
            sink.ingest(dict(payload, comment_page=32))
        self.assertEqual(output.getvalue().count('档案阶段进度'), 1)
        self.assertIn('帖子最后日期（最旧）：1970-01-01 08:02', output.getvalue())
        with contextlib.redirect_stdout(output), patch('scripts.holeclaw_archive_sink.time.monotonic', return_value=599):
            sink.ingest(dict(payload, comment_page=33))
        self.assertEqual(output.getvalue().count('档案阶段进度'), 1)
        with contextlib.redirect_stdout(output), patch('scripts.holeclaw_archive_sink.time.monotonic', return_value=600):
            sink.ingest(dict(payload, comment_page=34))
            sink.flush()
        self.assertEqual(output.getvalue().count('档案阶段进度'), 2)
        sink.cancel()
        with self.assertRaisesRegex(CliError, 'cancelled'):
            sink.ingest(payload)
        source.close()

    def test_progress_every_five_list_pages_and_final_partial_chunk(self):
        source = CacheStore(Path(self.temp.name) / 'source.sqlite3')
        checkpoint = runtime.new_checkpoint({}, 100, 200, 100, 'test')
        sink = ArchiveSink(self.store, checkpoint, Path(self.temp.name) / 'cp.json', None, None,
                           source_cache=source, progress_pages=5)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            for page in range(1, 8):
                sink.ingest(dict(schema_version=2, start_page=page, end_page=page, pages=1,
                                 scanned=0, oldest=160-page, terminal=page == 7))
                self.assertEqual(output.getvalue().count('档案阶段进度'), int(page >= 5) + int(page == 7))
            sink.flush()
        self.assertEqual(output.getvalue().count('档案阶段进度'), 2)
        self.assertIn('列表累计 5 页', output.getvalue())
        self.assertIn('列表累计 7 页', output.getvalue())
        self.assertEqual(runtime.read_checkpoint(sink.checkpoint_path)['oldest_post_timestamp'], 153)
        source.close()

    def test_comment_progress_is_quiet_by_default_and_flush_preserves_oldest_date(self):
        checkpoint = runtime.new_checkpoint({}, 100, 200, 100, 'test')
        sink = ArchiveSink(self.store, checkpoint, Path(self.temp.name) / 'cp.json', None, None)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            for timestamp in [160, 140, 150]:
                sink.ingest(dict(self.payload, post=dict(self.post, timestamp=timestamp),
                                 schema_version=2, archive_comments=True, archive_run=checkpoint['created_at']))
            self.assertEqual(output.getvalue(), '')
            sink.flush()
            sink.flush()
        self.assertEqual(output.getvalue().count('档案阶段进度'), 1)
        self.assertEqual(runtime.read_checkpoint(sink.checkpoint_path)['oldest_post_timestamp'], 140)

    def test_run_summary_excludes_unselected_historical_archive_rows(self):
        self.store.upsert_posts([dict(self.post, pid='old', favorites=999)])
        checkpoint = runtime.new_checkpoint({}, 100, 200, 100, 'test')
        checkpoint['archive_summary_version'] = 1
        post = dict(self.post, favorites=21)
        self.store.prepare_posts([post], checkpoint['created_at'])
        self.store.ingest_comments(dict(self.payload, post=post, comments=[], complete=True,
                                         archive_run=checkpoint['created_at']))
        summary = self.store.window_summary(checkpoint, runtime.FilterSpec(None, 20))
        self.assertEqual(summary, dict(posts=1, comments=0, completed_comment_posts=1,
                                       incomplete_comment_posts=0, unavailable_posts=0, oldest_archived_post_timestamp=150))
        self.assertEqual(self.store.post_count(), 2)


if __name__ == '__main__':
    unittest.main()
