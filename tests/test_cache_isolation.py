import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import run_digest as runtime
from scripts.holeclaw_archive import ArchiveStore, run_archive
from scripts.holeclaw_cache import CacheStore
from scripts.holeclaw_domain import CliError
from scripts.holeclaw_runner import CollectorServices


class CacheIsolationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, HOLECLAW_RUNTIME_DIR=str(self.root))
        environment.start()
        self.addCleanup(environment.stop)

    def args(self, account='test', *extra):
        return runtime.build_parser().parse_args([
            'archive', '--account', account, '--since', '2020-01-01',
            '--until', '2020-01-02', '--non-interactive', *extra])

    def run_archive(self, args, collector):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            run_archive(args, CollectorServices(lambda args: None, collector))
        return json.loads(output.getvalue().splitlines()[-1])

    def collect(self, browser, args, checkpoint, sink, url):
        post = dict(pid='1', timestamp=checkpoint['start_timestamp'] + 1,
                    reply=0, favorites=0, text='archive only', type='text')
        sink.ingest(dict(schema_version=2, archive_prepare=True,
                         archive_run=checkpoint['created_at'], posts=[post]))
        sink.ingest(dict(schema_version=2, start_page=1, end_page=1, pages=1,
                         scanned=1, rows=[post], matched_pids=[], favorite_unavailable=[],
                         telemetry={}, reached_start=True, terminal=True))
        return dict(reached_start=True, feed_exhausted=False)

    def test_default_archive_sources_are_separate_from_reports_and_other_accounts(self):
        report_path = runtime.default_cache_path()
        with CacheStore(report_path) as report:
            report.upsert_posts([dict(pid='9', timestamp=1577808001, reply=50,
                                     favorites=10, text='report only', type='text')])
            report.add_coverage(0, 2000000000, '2020-01-03', 1, 1, True)
        original_report = report_path.read_bytes()
        sources = []
        for account in ('first', 'second'):
            summary = self.run_archive(self.args(account), self.collect)
            source_path = Path(summary['source_cache'])
            sources.append(source_path)
            key = hashlib.sha256(account.encode()).hexdigest()[:16]
            self.assertEqual(source_path, self.root / 'archives' / key / 'list-cache-v5.sqlite3')
            self.assertEqual(source_path.parent, Path(summary['archive']).parent)
            self.assertEqual(summary['cached_posts'], 0)
            with CacheStore(source_path) as source:
                self.assertEqual(source.post_count(), 1)
                self.assertFalse(source.rows_by_pid('posts', ['9']))
                self.assertIsNotNone(source.find_covering(1577808000, 1577980800))
        self.assertNotEqual(*sources)
        self.assertEqual(report_path.read_bytes(), original_report)

    def test_archive_does_not_create_report_cache_and_completed_run_still_reuses(self):
        args = self.args()
        summary = self.run_archive(args, self.collect)
        def unexpected(*args):
            self.fail('completed archive must not collect again')
        repeated = self.run_archive(args, unexpected)
        self.assertEqual(summary['archive'], repeated['archive'])
        self.assertFalse(runtime.default_cache_path().exists())

    def interrupt(self, browser, args, checkpoint, sink, url):
        checkpoint['next_page'] = 7
        checkpoint['total_pages'] = 6
        raise CliError('test interruption')

    def test_legacy_shared_source_restarts_list_scan_and_preserves_archive(self):
        report_path = runtime.default_cache_path()
        checkpoint_path = self.root / 'legacy.json'
        args = self.args('test', '--checkpoint', str(checkpoint_path),
                         '--source-cache', str(report_path))
        with self.assertRaisesRegex(CliError, 'test interruption'):
            self.run_archive(args, self.interrupt)
        checkpoint = runtime.read_checkpoint(checkpoint_path)
        checkpoint.pop('source_cache_path')  # Format written before cache separation.
        runtime.write_checkpoint(checkpoint_path, checkpoint)
        with ArchiveStore(Path(checkpoint['cache_path']), 'test') as archive:
            archive.upsert_posts([dict(pid='8', timestamp=1577808001, reply=0,
                                      favorites=0, text='saved archive', type='text')])
        original_report = report_path.read_bytes()
        args.source_cache = None
        def restarted(browser, args, current, sink, url):
            self.assertEqual(current['next_page'], 1)
            self.assertFalse(current['cache_reused'])
            self.assertEqual(current['cache_instance_id'], checkpoint['cache_instance_id'])
            return self.collect(browser, args, current, sink, url)
        summary = self.run_archive(args, restarted)
        current = runtime.read_checkpoint(checkpoint_path)
        self.assertEqual(current['source_cache_path'], summary['source_cache'])
        self.assertNotEqual(current['source_instance_id'], checkpoint['source_instance_id'])
        self.assertEqual(summary['archive_totals']['posts'], 2)
        self.assertEqual(report_path.read_bytes(), original_report)

    def test_replaced_source_still_rejects_checkpoint(self):
        checkpoint_path = self.root / 'resume.json'
        args = self.args('test', '--checkpoint', str(checkpoint_path))
        with self.assertRaisesRegex(CliError, 'test interruption'):
            self.run_archive(args, self.interrupt)
        checkpoint = runtime.read_checkpoint(checkpoint_path)
        Path(checkpoint['source_cache_path']).unlink()
        with self.assertRaisesRegex(CliError, 'Source cache identity changed'):
            self.run_archive(args, self.collect)

    def test_explicit_source_override_resumes_existing_progress(self):
        args = self.args('test', '--source-cache', str(self.root / 'custom.sqlite3'))
        with self.assertRaisesRegex(CliError, 'test interruption'):
            self.run_archive(args, self.interrupt)
        def resumed(browser, args, checkpoint, sink, url):
            self.assertEqual(checkpoint['next_page'], 7)
            return dict(reached_start=True, feed_exhausted=False)
        summary = self.run_archive(args, resumed)
        self.assertEqual(Path(summary['source_cache']), self.root / 'custom.sqlite3')


if __name__ == '__main__':
    unittest.main()
