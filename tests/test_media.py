import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.holeclaw_archive import ArchiveStore
from scripts.holeclaw_cache import CacheStore
from scripts.holeclaw_domain import CliError
from scripts.holeclaw_media import MediaStore
from scripts.holeclaw_domain import FilterSpec


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ArchiveStore(Path(self.temp.name) / 'archive.sqlite3', 'test')
        self.media = self.store.media = MediaStore(self.store, Path(self.temp.name) / 'images', True)
        self.post = dict(pid='1', timestamp=150, reply=1, favorites=25, type='image', text='image', media_ids=['10'])

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def save(self, key='id:10', data=b'\x89PNG\r\n\x1a\nfixture'):
        self.media.save('1', dict(media_key=key, data=base64.b64encode(data).decode(), mime='image/png'))

    def test_shared_references_and_content_dedup_and_missing_file_repair(self):
        self.media.record_post(self.post)
        self.media.record_post(dict(self.post, pid='2'))
        self.assertEqual(len(self.media.plan('1')), 1)
        self.assertEqual(self.media.plan('2'), [])
        self.save()
        self.assertEqual(self.media.plan('1'), [])
        self.media.record_post(dict(self.post, media_ids=['10', '11']))
        self.save('id:11')
        files = list(self.media.directory.iterdir())
        self.assertEqual(len(files), 1, 'identical bytes under different IDs share one file')
        files[0].unlink()
        self.assertEqual(len(self.media.plan('1')), 2)

    def test_legacy_and_validation_and_error_bodies(self):
        self.media.record_post(dict(self.post, media_ids=[]))
        self.assertEqual(self.media.plan('1')[0]['media_key'], 'pid:1')
        self.save('pid:1', b'{"error":"expired"}')
        self.assertFalse(self.media.directory.exists())
        self.assertEqual(self.media.plan('1'), [])
        with self.assertRaises(CliError):
            self.media.record_post(dict(self.post, media_ids=['../secret']))
        with self.assertRaises(CliError):
            self.save('id:unrequested')
        self.assertEqual(self.store.connection.execute('SELECT media_key FROM media_refs').fetchone()[0], 'pid:1')

    def test_old_text_snapshot_requires_media_backfill_then_reuses(self):
        self.store.media = None
        payload = dict(post=self.post, archive_run='old', comment_page=3, complete=True,
                       comments=[dict(cid='7', text='reply')])
        self.store.ingest_comments(payload)
        self.store.media = self.media
        resume = self.store.prepare_posts([self.post], 'new')['1']
        self.assertFalse(resume['complete'])
        self.assertEqual(resume['next_page'], 1)
        self.media.record_post(self.post)
        payload.update(archive_run='new', comments=[dict(cid='7', text='reply', media_ids=['20'])])
        self.store.ingest_comments(payload)
        resume = self.store.prepare_posts([self.post], 'next')['1']
        self.assertTrue(resume['complete'] and resume['post_known'])
        self.assertFalse(self.store.prepare_posts([dict(self.post, reply=2)], 'later')['1']['complete'])

    def test_interrupt_does_not_publish_partial_file(self):
        self.media.record_post(self.post)
        with patch('scripts.holeclaw_media.os.fsync', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.save()
        self.assertEqual(list(self.media.directory.iterdir()), [])
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM media_files').fetchone()[0], 0)

    def test_offline_completion_requires_media_and_repairs_missing_files(self):
        source = CacheStore(Path(self.temp.name) / 'source.sqlite3')
        checkpoint = dict(created_at='run', start_timestamp=100, end_timestamp=200,
                          cached_posts=1, archive_cached_pages=1)
        try:
            source.upsert_posts([self.post])
            self.store.stage_candidates(source.path, checkpoint, FilterSpec(None, 20), 200)
            self.assertFalse(self.store.finish_cached_candidates(checkpoint))
            self.media.record_post(self.post)
            self.store.ingest_comments(dict(post=self.post, archive_run='run', comment_page=2,
                complete=True, comments=[dict(cid='9', text='comment', media_ids=[])]))
            self.assertFalse(self.store.finish_cached_candidates(checkpoint))
            self.save()
            self.assertTrue(self.store.finish_cached_candidates(checkpoint))
            next(self.media.directory.iterdir()).unlink()
            self.assertFalse(self.store.finish_cached_candidates(checkpoint))
        finally:
            source.close()


class CoverageUnionTests(unittest.TestCase):
    def test_union_gaps_and_favorite_completeness(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = CacheStore(Path(directory) / 'cache.sqlite3')
            try:
                cache.add_coverage(100, 200, 'a', 1, 1, True)
                cache.add_coverage(190, 300, 'b', 1, 1, True)
                cache.add_coverage(300, 400, 'c', 1, 1, False)
                cache.add_coverage(410, 500, 'd', 1, 1, True)
                self.assertEqual(cache.find_covering(150, 299, True)['end_timestamp'], 300)
                self.assertEqual(cache.find_prefix(150, 500, True)['end_timestamp'], 300)
                self.assertEqual(cache.find_prefix(150, 500)['end_timestamp'], 400)
                self.assertIsNone(cache.find_covering(150, 500))
                self.assertIsNone(cache.find_prefix(50, 500))
            finally:
                cache.close()
