import base64
import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.holeclaw_archive import run_archive
from scripts.holeclaw_archive_sink import ArchiveSink
from scripts.holeclaw_archive_store import ArchiveStore
from scripts.holeclaw_cache import CacheStore
from scripts.holeclaw_checkpoint import new_checkpoint, write_checkpoint
from scripts.holeclaw_domain import CliError, FilterSpec
from scripts.holeclaw_media import MediaStore
from scripts.holeclaw_protocol import SinkMessage
from scripts.holeclaw_runner import CollectorServices
from scripts.run_digest import build_parser


class IOTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.archive = ArchiveStore(self.root / 'archive.db', 'test')
        self.addCleanup(self.archive.close)
        self.source = CacheStore(self.root / 'source.db')
        self.addCleanup(self.source.close)
        self.post = dict(pid='1', timestamp=150, reply=1000, favorites=5,
                         type='text', text='post', observed_at=100, media_ids=['10'])
        self.payload = dict(post=self.post, comments=[dict(cid=str(i), timestamp=150, text='comment')
                            for i in range(1000)], comment_page=10, complete=True, archive_run='old')
        self.cp = new_checkpoint({}, 100, 200, 100, 'test')

    def test_replayed_comments_only_write_scan_state_and_preserve_observation(self):
        with patch('scripts.holeclaw_archive_store.datetime') as clock:
            clock.now.return_value.timestamp.return_value = 1000
            self.archive.ingest_comments(self.payload)
            before = self.archive.connection.total_changes
            clock.now.return_value.timestamp.return_value = 2000
            self.archive.ingest_comments(self.payload)
        self.assertEqual(self.archive.connection.total_changes - before, 1)
        self.assertEqual(self.archive.connection.execute('SELECT MAX(observed_at) FROM comments').fetchone()[0], 1000)
        self.assertEqual(self.archive.connection.execute('SELECT observed_at FROM comment_scans').fetchone()[0], 2000)
        changed = dict(cid='0', timestamp=151, text='updated searchable text', name_tag='Bob', quote_cid='2')
        self.archive.ingest_comments(dict(self.payload, comments=[changed]))
        row = self.archive.connection.execute("SELECT * FROM comments WHERE cid='0'").fetchone()
        self.assertEqual((row['text'], row['timestamp'], row['name_tag'], row['quote_cid']),
                         ('updated searchable text', 151, 'Bob', '2'))
        self.archive.ingest_comments(dict(self.payload, comments=[dict(changed, quote_cid=None)]))
        self.assertIsNone(self.archive.connection.execute("SELECT quote_cid FROM comments WHERE cid='0'").fetchone()[0])
        self.assertEqual(self.archive.summary()['comments'], 1000)

    def test_totals_follow_transactions_deletions_and_scan_transitions(self):
        self.archive.ingest_comments(self.payload)
        original = self.archive.summary()
        with self.assertRaisesRegex(RuntimeError, 'rollback'):
            with self.archive.transaction():
                self.archive.connection.execute('DELETE FROM comments')
                self.archive.connection.execute('UPDATE comment_scans SET complete=0')
                self.archive.upsert_posts([dict(self.post, pid='2')])
                raise RuntimeError('rollback')
        self.assertEqual(self.archive.summary(), original)
        with self.archive.transaction():
            self.archive.connection.execute("DELETE FROM comments WHERE cid='0'")
            self.archive.connection.execute('UPDATE comment_scans SET complete=0')
        result = self.archive.summary()
        self.assertEqual((result['posts'], result['comments'], result['posts_with_completed_comment_scan']), (1, 999, 0))
        self.archive.connection.execute('UPDATE comment_scans SET complete=1')
        self.archive.connection.execute('DELETE FROM comment_scans')
        self.archive.connection.execute('DELETE FROM posts')
        self.assertEqual(self.archive.summary()['posts_with_completed_comment_scan'], 0)
        self.assertEqual(self.archive.summary()['posts'], 0)

    def test_old_archives_backfill_totals_once(self):
        self.archive.ingest_comments(self.payload)
        expected = self.archive.summary()
        for row in self.archive.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE '%_total_%'").fetchall():
            self.archive.connection.execute(f'DROP TRIGGER {row[0]}')
        self.archive.connection.execute('DROP TABLE archive_totals')
        with ArchiveStore(self.archive.path, 'test') as reopened:
            statements = []
            reopened.connection.set_trace_callback(statements.append)
            self.assertEqual(reopened.summary(), expected)
            self.assertFalse(any('COUNT(' in sql for sql in statements))
        self.assertEqual(self.archive.summary(), expected)

    def test_full_cache_hit_avoids_candidate_body_copy(self):
        args = build_parser().parse_args(['archive', '--account', 'test',
            '--cache', str(self.archive.path), '--source-cache', str(self.source.path),
            '--checkpoint', str(self.root / 'cp.json'), '--since', '2020-01-01', '--until', '2020-01-02'])
        post = dict(self.post, timestamp=1577808001, reply=0)
        self.archive.prepare_posts([post], 'old')
        self.source.upsert_posts([post])
        self.source.add_coverage(1577808000, 1577980800, 'old', 1, 1, True)
        def unexpected(*args, **kwargs):
            self.fail('fully cached run must not stage bodies or start browser')
        with patch.object(ArchiveStore, 'stage_candidates', unexpected), contextlib.redirect_stdout(io.StringIO()) as output:
            run_archive(args, CollectorServices(unexpected, unexpected))
        summary = json.loads(output.getvalue())
        self.assertTrue(summary['completed'])
        self.assertEqual((summary['posts'], summary['cached_posts'], summary['completed_comment_posts']), (1, 1, 1))
        self.assertEqual(self.archive.connection.execute('SELECT COUNT(*) FROM archive_candidates').fetchone()[0], 0)

    def test_partial_source_retains_durable_candidate_snapshot(self):
        self.source.upsert_posts([self.post])
        self.assertIsNone(self.archive.finish_cached_source(self.source.path, self.cp, FilterSpec(None, None), 200))
        self.assertEqual(self.archive.connection.execute('SELECT COUNT(*) FROM archive_members').fetchone()[0], 0)
        self.archive.stage_candidates(self.source.path, self.cp, FilterSpec(None, None), 200)
        self.source.upsert_posts([dict(self.post, text='later change', observed_at=200)])
        self.assertEqual(self.archive.candidate_page(self.cp['created_at'], 1)[0]['text'], 'post')

    def test_source_fast_path_rolls_back_on_registration_failure(self):
        post = dict(self.post, reply=0)
        self.archive.prepare_posts([post], 'old')
        self.source.upsert_posts([dict(post, text='new text', observed_at=200)])
        self.archive.connection.execute('''CREATE TEMP TRIGGER fail_member BEFORE INSERT ON archive_members
            BEGIN SELECT RAISE(ABORT, 'registration failed'); END''')
        with self.assertRaisesRegex(sqlite3.Error, 'registration failed'):
            self.archive.finish_cached_source(self.source.path, self.cp, FilterSpec(None, None), 200)
        self.assertEqual(self.archive.rows_by_pid('posts', ['1'])['1']['text'], 'post')

    def test_binary_retry_checks_hash_without_disk_writes(self):
        media = self.archive.media = MediaStore(self.archive, self.root / 'images', True)
        media.record_post(self.post)
        sink = ArchiveSink(self.archive, self.cp, self.root / 'cp.json', None, None, source_cache=self.source)
        raw = dict(schema_version=3, request_id=1, run_id=self.cp['created_at'], kind='archive_media_file',
                   payload=dict(post=self.post, media_key='id:10', mime='image/png'))
        data = b'\x89PNG\r\n\x1a\nfixture'
        sink.ingest_media_stream(raw, io.BytesIO(data), len(data))
        with patch('scripts.holeclaw_media.tempfile.NamedTemporaryFile', side_effect=AssertionError('retry wrote file')), \
                patch('scripts.holeclaw_media.os.fsync', side_effect=AssertionError('retry synced file')):
            sink.ingest_media_stream(raw, io.BytesIO(data), len(data))
            with self.assertRaisesRegex(CliError, 'changed'):
                sink.ingest_media_stream(raw, io.BytesIO(data + b'x'), len(data) + 1)
        self.assertEqual(self.cp['archive_metrics']['image_receipts'], 1)
        self.assertEqual(next(media.directory.iterdir()).read_bytes(), data)
        # A concurrent receipt eviction must fail closed after the body was read.
        sink.receipts.limit = 1
        class EvictingStream(io.BytesIO):
            def read(self, size):
                other = dict(raw, request_id=2)
                sink.receipts.remember(SinkMessage.decode(other, raw['run_id']), None)
                return super().read(size)
        with self.assertRaisesRegex(CliError, 'expired'):
            sink.ingest_media_stream(raw, EvictingStream(data), len(data))

    def test_file_checks_reuse_across_batches_and_detect_next_operation_deletion(self):
        media = self.archive.media = MediaStore(self.archive, self.root / 'images', True)
        posts = [dict(self.post, pid=str(i), reply=0) for i in range(501)]
        self.archive.prepare_posts(posts, 'old')
        for post in posts:
            media.record_post(post)
        media.save('0', dict(media_key='id:10', data=base64.b64encode(b'\x89PNG\r\n\x1a\nfixture').decode()))
        target = next(media.directory.iterdir())
        original_stat, calls = Path.stat, []
        def counting_stat(path, *args, **kwargs):
            if path == target:
                calls.append(path)
            return original_stat(path, *args, **kwargs)
        with patch.object(Path, 'stat', counting_stat):
            self.assertTrue(media.posts_complete(self.archive.connection.execute('SELECT pid,reply FROM posts')))
        self.assertEqual(len(calls), 1)
        target.unlink()
        self.assertFalse(media.posts_complete(self.archive.connection.execute('SELECT pid,reply FROM posts')))

    def test_checkpoint_skips_unchanged_writes_and_repairs_external_changes(self):
        path = self.root / 'cp.json'
        with patch('scripts.holeclaw_checkpoint.os.replace', wraps=os.replace) as replace:
            write_checkpoint(path, self.cp)
            write_checkpoint(path, self.cp)
            self.assertEqual(replace.call_count, 1)
            self.assertEqual(json.loads(path.read_text()), self.cp)
            self.assertNotIn('\n ', path.read_text())
            self.cp['completed'] = True
            write_checkpoint(path, self.cp)
            self.assertEqual(replace.call_count, 2)
            path.write_text('external change')
            write_checkpoint(path, self.cp)
            path.unlink()
            write_checkpoint(path, self.cp)
            self.assertEqual(replace.call_count, 4)
        self.assertEqual(json.loads(path.read_text()), self.cp)

    def test_checkpoint_failed_replace_is_retried(self):
        path = self.root / 'cp.json'
        write_checkpoint(path, self.cp)
        self.cp['next_page'] = 2
        with patch('scripts.holeclaw_checkpoint.os.replace', side_effect=OSError('disk failure')):
            with self.assertRaisesRegex(OSError, 'disk failure'):
                write_checkpoint(path, self.cp)
        self.assertEqual(json.loads(path.read_text())['next_page'], 1)
        write_checkpoint(path, self.cp)
        self.assertEqual(json.loads(path.read_text())['next_page'], 2)


if __name__ == '__main__':
    unittest.main()
