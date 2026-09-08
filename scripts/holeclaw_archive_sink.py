"""Archive callback handlers; transport validation and receipts live in RunSink."""
import sqlite3
import time

try:
    from holeclaw_domain import CliError
    from holeclaw_sink import RunSink
    from holeclaw_protocol import SinkMessage
except ModuleNotFoundError:
    from scripts.holeclaw_domain import CliError
    from scripts.holeclaw_sink import RunSink
    from scripts.holeclaw_protocol import SinkMessage


class ArchiveSink(RunSink):
    def __init__(self, archive, checkpoint, checkpoint_path, min_comments, min_favorites,
                 match_mode='all', *, source_cache=None, progress_seconds=300, progress_pages=0):
        super().__init__(source_cache or archive, checkpoint, checkpoint_path,
                         min_comments, min_favorites, match_mode)
        self.archive = archive
        self.source_cache = source_cache
        self.progress_seconds = progress_seconds
        self.progress_pages = progress_pages
        self.last_reported_pages = checkpoint['total_pages']
        self.last_reported_at = time.monotonic()
        self.comment_chunks = self.reused_posts = 0
        self.last_reported_state = (checkpoint['total_pages'], 0, 0, 0)
        self.metrics = checkpoint.setdefault('archive_metrics', {})

    def add_metric(self, key, value=1):
        self.metrics[key] = self.metrics.get(key, 0) + value

    def cancel(self):
        super().cancel()
        for store in (self.archive, self.cache):
            try:
                store.connection.interrupt()
            except sqlite3.ProgrammingError:
                pass
        with self.lock:
            if self.archive.media:
                self.archive.media.cancel_plans()

    def report_progress(self, *, force=False):
        state = (self.checkpoint['total_pages'], self.comment_chunks, self.reused_posts,
                 self.metrics.get('image_receipts', 0))
        now = time.monotonic()
        if state != self.last_reported_state and (force
                or (self.progress_pages and
                    self.checkpoint['total_pages'] - self.last_reported_pages >= self.progress_pages)
                or (self.progress_seconds and now - self.last_reported_at >= self.progress_seconds)):
            print(f"档案阶段进度：列表累计 {self.checkpoint['total_pages']} 页，"
                  f"评论已保存 {self.comment_chunks} 批，复用评论 {self.reused_posts} 帖，"
                  f"{self.post_date_label()}。", flush=True)
            self.last_reported_at = now
            self.last_reported_pages = self.checkpoint['total_pages']
            self.last_reported_state = state

    def flush(self):
        with self.lock:
            super().flush()
            self.report_progress(force=True)

    def validate_post(self, post):
        if not self.checkpoint['start_timestamp'] <= int(post.get('timestamp', 0)) < self.checkpoint['end_timestamp']:
            raise CliError('Archive post outside requested window.')
        if not self.filter_spec.matches(int(post['reply']), post.get('favorites')):
            raise CliError('Archive post outside requested filter.')

    def dispatch(self, kind, payload):
        handlers = {
            'archive_source': self.ingest_source,
            'archive_prepare': self.ingest_prepare,
            'archive_resume': self.ingest_resume,
            'archive_comments': self.ingest_comment_chunk,
            'archive_post_media': self.ingest_media,
            'archive_media_plan': self.ingest_media,
            'archive_media_file': self.ingest_media,
            'archive_media_unavailable': self.ingest_media,
            'archive_post_unavailable': self.ingest_unavailable,
        }
        if kind in handlers:
            if payload.get('archive_run') != self.checkpoint['created_at']:
                raise CliError('Archive run identity mismatch.')
            if kind not in ('archive_source', 'archive_prepare'):
                self.validate_post(payload.get('post') or {})
            return handlers[kind](payload)
        if self.source_cache is None:
            raise CliError('Archive list collection requires a separate source cache.')
        if payload.get('archive_cached'):
            payload = dict(payload, rows=[], scanned=0, favorite_unavailable=[], favorite_deferred_pids=[], matched_pids=[])
        result = super().dispatch(kind, payload)
        self.report_progress(force=bool(payload.get('terminal')))
        return result

    def ingest_source(self, payload):
        page = payload.get('page')
        if type(page) is not int or not 1 <= page <= self.checkpoint.get('archive_cached_pages', 0):
            raise CliError('Invalid cached page.')
        result = self.archive.cached_work_page(self.checkpoint, page)
        self.reused_posts += result['reused']
        self.add_metric('reused_posts', result['reused'])
        self.add_metric('locally_reused_posts', result['reused'])
        self.report_progress()
        return result

    def ingest_prepare(self, payload):
        posts = payload.get('posts')
        if not isinstance(posts, list) or len(posts) > 500:
            raise CliError('Invalid archive batch.')
        for post in posts:
            self.validate_post(post)
        started = time.perf_counter()
        resumes = self.archive.prepare_posts(posts, payload['archive_run'], self.checkpoint.get('fresh', False))
        self.add_metric('prepare_ms', round((time.perf_counter() - started) * 1000))
        self.add_metric('prepare_batches')
        reused = sum(bool(row['complete']) and not row.get('empty_completed') for row in resumes.values())
        self.add_metric('empty_posts_completed', sum(bool(row.get('empty_completed')) for row in resumes.values()))
        self.reused_posts += reused
        self.add_metric('reused_posts', reused)
        for post in posts:
            if resumes[post['pid']]['complete']:
                self.record_post_date(int(post['timestamp']))
        self.report_progress()
        return {'resumes': resumes}

    def ingest_resume(self, payload):
        post = payload['post']
        return self.archive.resume_comments(post['pid'], payload['archive_run'], post['reply'],
                                            self.checkpoint.get('fresh', False))

    def ingest_unavailable(self, payload):
        self.archive.record_unavailable(payload['post'], payload['archive_run'])
        self.record_post_date(int(payload['post']['timestamp']))
        self.add_metric('unavailable_posts')
        self.report_progress()

    def ingest_comment_chunk(self, payload):
        started = time.perf_counter()
        self.archive.ingest_comments(payload)
        self.record_post_date(int(payload['post']['timestamp']))
        self.add_metric('comment_write_ms', round((time.perf_counter() - started) * 1000))
        self.add_metric('comment_batches')
        self.add_metric('comment_rows_received', len(payload['comments']))
        self.comment_chunks += 1
        self.report_progress()

    def ingest_media(self, payload):
        media = self.archive.media
        if not media:
            raise CliError('Image extraction is not enabled.')
        post = payload['post']
        if payload.get('archive_media_unavailable'):
            media.unavailable(str(post['pid']), 'post_not_found', post['reply'])
        elif payload.get('archive_post_media'):
            media.record_post(post)
        elif payload.get('archive_media_plan'):
            request_id = payload.get('plan_id')
            if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
                raise CliError('Invalid image plan identity.')
            return {'images': media.plan(str(post['pid']), request_id)}
        else:
            media.save(str(post['pid']), payload)
            self.add_metric('image_receipts')
        self.record_post_date(int(post['timestamp']))
        self.report_progress()

    def ingest_media_stream(self, raw, stream, length):
        message = SinkMessage.decode(raw, self.checkpoint['created_at'])
        if message.kind != 'archive_media_file' or raw.get('schema_version') != 3:
            raise CliError('Invalid binary image message.')
        payload = message.payload
        if any(key in payload for key in ('data', 'binary_sha256', 'status')):
            raise CliError('Invalid binary image payload.')
        media = self.archive.media
        with self.lock:
            if self.cancel_event.is_set():
                raise CliError('Collector cancelled.')
            self.validate_post(payload.get('post') or {})
            if not media:
                raise CliError('Image extraction is not enabled.')
            media.validate_download(str(payload['post']['pid']), payload.get('media_key'))
        with media.receive(stream, length, payload.get('mime', '')) as prepared:
            # Include the bytes in request identity without serializing them.
            envelope = dict(raw, payload=dict(raw['payload'], binary_sha256=prepared['digest']))
            return self.ingest(envelope, prepared_media=prepared)

    def publish_media(self, message, prepared):
        payload = message.payload
        self.archive.media.save_received(str(payload['post']['pid']), payload['media_key'], prepared)
        self.add_metric('image_receipts')
        self.record_post_date(int(payload['post']['timestamp']))
        self.report_progress()
