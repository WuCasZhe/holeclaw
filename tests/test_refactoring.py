"""Behavioral checks for the new state, transaction and callback boundaries."""
import base64
import contextlib
import io
import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts import holeclaw_archive as archive_workflow
from scripts import run_digest
from scripts.holeclaw_archive_sink import ArchiveSink
from scripts.holeclaw_archive_store import ArchiveStore
from scripts.holeclaw_cache import CacheStore
from scripts.holeclaw_checkpoint import (
    CollectionPosition, load_checkpoint, new_checkpoint, read_checkpoint, write_checkpoint,
)
from scripts.holeclaw_domain import CliError
from scripts.holeclaw_media import MediaStore
from scripts.holeclaw_protocol import ReceiptBook, SinkMessage
from scripts.holeclaw_runner import CollectorServices


class RefactoringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = ArchiveStore(self.root / 'archive.sqlite3', 'test')
        self.addCleanup(self.archive.close)
        self.source = CacheStore(self.root / 'source.sqlite3')
        self.addCleanup(self.source.close)
        self.checkpoint = new_checkpoint({}, 100, 200, 100, 'test')
        self.sink = ArchiveSink(self.archive, self.checkpoint, self.root / 'checkpoint.json',
                                None, None, source_cache=self.source)
        self.post = dict(pid='1', timestamp=150, reply=1, favorites=0, type='text', text='fixture')

    def message(self, kind, request_id, **payload):
        return dict(schema_version=3, kind=kind, request_id=request_id,
                    run_id=self.checkpoint['created_at'], payload=payload)

    def comments(self, request_id=1, post=None):
        return self.message('archive_comments', request_id, post=post or self.post,
                            comment_page=2, comment_page_size=100, complete=True,
                            comments=[dict(cid='1', text='fixture', timestamp=151, name_tag='A')])

    def test_interleaved_concurrent_retries_count_commits_once(self):
        messages = [self.comments(i, dict(self.post, pid=str(i))) for i in range(1, 17)]
        # Retries arrive after other requests, not just immediately after themselves.
        with ThreadPoolExecutor(max_workers=8) as workers:
            list(workers.map(self.sink.ingest, messages + list(reversed(messages))))
        self.assertEqual(self.archive.summary()['comments'], 16)
        self.assertEqual(self.sink.comment_chunks, 16)
        self.assertEqual(self.checkpoint['archive_metrics']['comment_batches'], 16)

    def test_changed_retry_and_wrong_run_are_rejected_before_mutation(self):
        message = self.comments()
        self.sink.ingest(message)
        altered = self.comments()
        altered['payload']['comments'][0]['text'] = 'changed'
        with self.assertRaisesRegex(CliError, 'changed'):
            self.sink.ingest(altered)
        with self.assertRaisesRegex(CliError, 'identity'):
            self.sink.ingest(dict(message, request_id=2, run_id='another run'))
        self.assertEqual(self.archive.connection.execute('SELECT text FROM comments').fetchone()[0], 'fixture')

    def test_prepare_retry_returns_original_response_after_cursor_changes(self):
        prepare = self.message('archive_prepare', 1, posts=[self.post])
        original = self.sink.ingest(prepare)
        self.assertFalse(original['resumes']['1']['complete'])
        self.sink.ingest(self.comments(2))
        self.assertEqual(self.sink.ingest(prepare), original)
        self.assertEqual(self.checkpoint['archive_metrics']['prepare_batches'], 1)
        current = self.sink.ingest(self.message('archive_prepare', 3, posts=[self.post]))
        self.assertTrue(current['resumes']['1']['complete'])

    def test_image_retry_does_not_write_file_or_count_twice(self):
        self.archive.media = MediaStore(self.archive, self.root / 'images', True)
        post = dict(self.post, media_ids=['10'])
        self.sink.ingest(self.message('archive_post_media', 1, post=post))
        plan = self.message('archive_media_plan', 2, post=post, plan_id='1')
        original = self.sink.ingest(plan)
        file = self.message('archive_media_file', 3, post=post, media_key='id:10',
                            mime='image/png', data=base64.b64encode(b'\x89PNG\r\n\x1a\nfixture').decode())
        with patch('scripts.holeclaw_media.os.fsync') as fsync:
            self.sink.ingest(file)
            self.sink.ingest(file)
            fsync.assert_called_once()
        self.assertEqual(self.sink.ingest(plan), original)
        self.assertEqual(self.checkpoint['archive_metrics']['image_receipts'], 1)

    def test_legacy_comment_retry_is_deduplicated(self):
        payload = dict(self.comments()['payload'], schema_version=2, archive_comments=True,
                       archive_run=self.checkpoint['created_at'])
        self.sink.ingest(payload)
        self.sink.ingest(payload)
        self.assertEqual(self.sink.comment_chunks, 1)

    def test_conflicting_message_kinds_are_rejected(self):
        with self.assertRaisesRegex(CliError, 'conflicting'):
            self.sink.ingest(dict(schema_version=2, archive_prepare=True, archive_comments=True))
        message = self.message('archive_prepare', 1, posts=[self.post], archive_comments=True)
        with self.assertRaisesRegex(CliError, 'body'):
            self.sink.ingest(message)
        self.assertEqual(self.archive.post_count(), 0)

    def test_receipts_are_bounded_and_expired_retries_cannot_reapply(self):
        book = ReceiptBook(limit=2)
        messages = [SinkMessage.decode(self.comments(i), self.checkpoint['created_at']) for i in range(1, 4)]
        for message in messages:
            book.remember(message, {'ok': True})
        self.assertEqual(len(book.entries), 2)
        with self.assertRaisesRegex(CliError, 'expired'):
            book.lookup(messages[0])
        self.assertEqual(book.lookup(messages[1]), (True, {'ok': True}))

    def test_nested_writes_stay_invisible_and_rollback_with_outer_transaction(self):
        with contextlib.closing(sqlite3.connect(self.source.path)) as observer:
            with self.assertRaisesRegex(RuntimeError, 'outer'):
                with self.source.transaction():
                    self.source.upsert_posts([self.post])
                    with self.source.transaction():
                        self.source.upsert_posts([dict(self.post, pid='2')])
                    self.assertEqual(observer.execute('SELECT COUNT(*) FROM posts').fetchone()[0], 0)
                    raise RuntimeError('outer')
        self.assertEqual(self.source.post_count(), 0)

    def test_inner_rollback_does_not_discard_successful_outer_work(self):
        with self.source.transaction():
            self.source.upsert_posts([self.post])
            with self.assertRaisesRegex(RuntimeError, 'inner'):
                with self.source.transaction():
                    self.source.upsert_posts([dict(self.post, pid='2')])
                    raise RuntimeError('inner')
            self.source.upsert_posts([dict(self.post, pid='3')])
        self.assertEqual({row['pid'] for row in self.source.query_posts(100, 200, None, None)}, {'1', '3'})

    def test_legacy_checkpoint_migration_is_in_memory_and_validated(self):
        path = self.root / 'legacy.json'
        write_checkpoint(path, self.checkpoint)
        original = path.read_bytes()
        migrated = load_checkpoint(path, {'comment_page_size': 100}, legacy_spec={})
        self.assertEqual(migrated['request'], {'comment_page_size': 100})
        self.assertEqual(path.read_bytes(), original)
        write_checkpoint(path, dict(self.checkpoint, next_page=True))
        with self.assertRaisesRegex(CliError, 'next_page'):
            load_checkpoint(path, {})
        path.write_text('[]')
        with self.assertRaisesRegex(CliError, 'object'):
            read_checkpoint(path)

    def test_position_separates_cached_batches_and_remote_page_budget(self):
        position = CollectionPosition.from_checkpoint(dict(next_page=5, total_pages=4, archive_cached_pages=10))
        self.assertEqual(position.remaining_cached_batches, 6)
        self.assertEqual(position.committed_remote_pages, 0)
        position = CollectionPosition.from_checkpoint(dict(next_page=14, total_pages=13, archive_cached_pages=10))
        self.assertEqual(position.remaining_cached_batches, 0)
        self.assertEqual(position.committed_remote_pages, 3)

    def test_checkpoint_flush_failure_still_closes_archive_server(self):
        args = run_digest.build_parser().parse_args([
            'archive', '-a', 'cleanup', '-b', '2020-01-01', '-e', '2020-01-02',
            '-C', str(self.root / 'cleanup.sqlite3'), '-S', str(self.source.path),
            '-k', str(self.root / 'cleanup.json'),
        ])
        server = MagicMock()
        services = CollectorServices(MagicMock(), MagicMock(return_value={'reached_start': True}))
        with patch.object(archive_workflow, 'SinkServer', return_value=server), \
             patch.object(ArchiveSink, 'flush', side_effect=OSError('disk full')), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(OSError, 'disk full'):
                archive_workflow.run_archive(args, services)
        server.close.assert_called_once()

    @unittest.skipUnless(shutil.which('node'), 'Node is required for the wire-contract integration test')
    def test_actual_javascript_envelopes_are_accepted_by_python_sink(self):
        program = r'''
const {runCollector, response} = require('./tests/collector_harness');
const runId = process.argv[1];
runCollector({config: {
  archive: true, archive_run: runId,
  report_start_timestamp: 100, scan_start_timestamp: 100, end_timestamp: 200,
  min_comments: null, min_favorites: null, max_pages: 1, request_concurrency: 1,
  sink_url: 'http://127.0.0.1:12345/ingest?token=fixture',
}, remoteFetch: async url => response({code: 20000, data: {list:
  url.includes('list_comments') ? [{pid: '1', timestamp: 150, reply: 1, likenum: 0, text: 'fixture'}]
  : url.includes('page=1') ? [{cid: '1', text: 'fixture', timestamp: 151}] : []}}),
  sinkFetch: async (_url, options) => response(JSON.parse(options.body).archive_prepare
    ? {ok: true, resumes: {'1': {next_page: 1, complete: false}}} : {ok: true})
}).then(result => process.stdout.write(JSON.stringify(result.wirePayloads)))
  .catch(error => {console.error(error); process.exitCode = 1;});
'''
        result = subprocess.run(['node', '-e', program, self.checkpoint['created_at']],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True,
                                text=True, check=True, timeout=15)
        envelopes = json.loads(result.stdout)
        self.assertEqual({item['kind'] for item in envelopes},
                         {'archive_prepare', 'archive_comments', 'list_chunk', 'telemetry_final'})
        with contextlib.redirect_stdout(io.StringIO()):
            for envelope in envelopes:
                self.sink.ingest(envelope)
                self.sink.ingest(envelope)
        self.assertEqual(self.archive.summary()['comments'], 1)
        self.assertEqual(self.source.post_count(), 1)
        self.assertEqual(self.checkpoint['next_page'], 2)
        self.assertEqual(self.checkpoint['archive_metrics']['comment_batches'], 1)
        self.assertEqual(self.checkpoint['telemetry']['list_requests'], 1)


if __name__ == '__main__':
    unittest.main()
