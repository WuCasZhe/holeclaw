"""Filtered text archive backed by reusable list and comment snapshots."""
import hashlib
import json
import sqlite3
import time
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

try:
    from holeclaw_media import MediaStore
    from holeclaw_cache import CacheStore
    from holeclaw_domain import CliError, SHANGHAI
    from holeclaw_sink import RunSink, SinkServer
except ModuleNotFoundError:
    from scripts.holeclaw_media import MediaStore
    from scripts.holeclaw_cache import CacheStore
    from scripts.holeclaw_domain import CliError, SHANGHAI
    from scripts.holeclaw_sink import RunSink, SinkServer


class ArchiveStore(CacheStore):
    allow_archive = True

    def __init__(self, path, account):
        if path.exists():
            try:
                with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
                    marker = db.execute("SELECT value FROM metadata WHERE key='archive_account'").fetchone()
                    version = db.execute("SELECT value FROM metadata WHERE key='archive_schema_version'").fetchone()
                if marker != (account,):
                    raise CliError("Archive account does not match, or this is a digest cache. Use a separate --cache path.")
                if version not in (('1',), ('2',), ('3',)):
                    raise CliError('Incompatible archive schema. Use a new --cache path.')
            except sqlite3.Error as error:
                raise CliError("Invalid archive database. Use a new --cache path.") from error
        super().__init__(path)
        self.media = None
        with self.transaction():
            self.connection.execute("INSERT OR REPLACE INTO metadata VALUES('archive_account', ?)", (account,))
            self.connection.execute("""CREATE TABLE IF NOT EXISTS comments (
                pid TEXT NOT NULL, cid TEXT NOT NULL, text TEXT NOT NULL,
                timestamp INTEGER NOT NULL, name_tag TEXT NOT NULL, quote_cid TEXT,
                observed_at INTEGER NOT NULL, PRIMARY KEY(pid, cid))""")
            self.connection.execute("""CREATE TABLE IF NOT EXISTS comment_scans (
                pid TEXT PRIMARY KEY, last_page INTEGER NOT NULL,
                complete INTEGER NOT NULL, observed_at INTEGER NOT NULL,
                run_id TEXT NOT NULL)""")
            columns = {row[1] for row in self.connection.execute('PRAGMA table_info(comment_scans)')}
            if 'reply_count' not in columns:
                self.connection.execute('ALTER TABLE comment_scans ADD COLUMN reply_count INTEGER')
                self.connection.execute('''UPDATE comment_scans SET reply_count=(
                    SELECT reply FROM posts WHERE posts.pid=comment_scans.pid) WHERE complete=1''')
            if 'page_size' not in columns:
                # NULL means legacy/unknown pagination; never reuse its offsets.
                self.connection.execute('ALTER TABLE comment_scans ADD COLUMN page_size INTEGER')
            self.connection.execute("INSERT OR REPLACE INTO metadata VALUES('archive_schema_version', '3')")
            self.connection.execute('''CREATE TABLE IF NOT EXISTS archive_candidates (
                run_id TEXT NOT NULL, ordinal INTEGER NOT NULL, pid TEXT NOT NULL,
                timestamp INTEGER NOT NULL, reply INTEGER NOT NULL, favorites INTEGER,
                type TEXT NOT NULL, text TEXT NOT NULL, PRIMARY KEY(run_id, ordinal))''')
            self.connection.execute('''CREATE TABLE IF NOT EXISTS archive_members (
                run_id TEXT NOT NULL, pid TEXT NOT NULL, reply INTEGER NOT NULL,
                PRIMARY KEY(run_id, pid))''')

    def stage_candidates(self, source_path, checkpoint, filters, end):
        clause, parameters = filters.sql_clause()
        self.connection.execute('ATTACH DATABASE ? AS source', (source_path.as_uri() + '?mode=ro',))
        try:
            with self.transaction():
                self.connection.execute('DELETE FROM archive_candidates WHERE run_id=?', (checkpoint['created_at'],))
                self.connection.execute(f'''INSERT INTO archive_candidates
                    SELECT ?, ROW_NUMBER() OVER (ORDER BY timestamp DESC, pid DESC),
                           pid,timestamp,reply,favorites,type,text FROM source.posts
                    WHERE timestamp>=? AND timestamp<? {('AND ' + clause) if clause else ''}''',
                    [checkpoint['created_at'], checkpoint['start_timestamp'], end, *parameters])
                count = self.connection.execute('SELECT COUNT(*) FROM archive_candidates WHERE run_id=?',
                                                (checkpoint['created_at'],)).fetchone()[0]
            return count
        finally:
            self.connection.execute('DETACH DATABASE source')

    def candidate_page(self, run_id, page):
        with self.lock:
            return [dict(row) for row in self.connection.execute('''
                SELECT pid,timestamp,reply,favorites AS likenum,type,text FROM archive_candidates
                WHERE run_id=? AND ordinal>? AND ordinal<=? ORDER BY ordinal''',
                (run_id, (page - 1) * 500, page * 500))]

    def finish_cached_candidates(self, checkpoint):
        """Finish without launching a browser if every selected comment snapshot exists."""
        run_id = checkpoint['created_at']
        count = self.connection.execute('SELECT COUNT(*) FROM archive_candidates WHERE run_id=?', (run_id,)).fetchone()[0]
        if count != checkpoint.get('cached_posts', 0):
            raise CliError('Cached candidate snapshot is missing. Restore it or use --fresh.')
        pending = self.connection.execute('''SELECT COUNT(*) FROM archive_candidates c
            LEFT JOIN comment_scans s ON s.pid=c.pid
            WHERE c.run_id=? AND (s.pid IS NULL OR s.complete!=1 OR s.reply_count IS NULL
                OR s.reply_count!=c.reply)''', (run_id,)).fetchone()[0]
        if pending or (self.media and not self.media.candidates_complete(run_id)):
            return False
        with self.transaction():
            for page in range(1, checkpoint.get('archive_cached_pages', 0) + 1):
                posts = self.candidate_page(run_id, page)
                for post in posts:
                    post['favorites'] = post.pop('likenum')
                self.upsert_posts(posts, commit=False)
            self.connection.execute('''INSERT INTO archive_members
                SELECT run_id,pid,reply FROM archive_candidates WHERE run_id=?
                ON CONFLICT(run_id,pid) DO UPDATE SET reply=excluded.reply''', (run_id,))
        return True

    def ingest_comments(self, payload):
        post = payload.get('post')
        comments = payload.get('comments')
        page = payload.get('comment_page')
        complete = payload.get('complete')
        page_size = payload.get('comment_page_size', 100)
        if (not isinstance(post, dict) or not isinstance(comments, list)
                or not isinstance(page, int) or page < 1
                or not isinstance(complete, bool)
                or type(page_size) is not int or page_size not in (10, 100)):
            raise CliError('Invalid archive comment chunk.')
        now = int(datetime.now(SHANGHAI).timestamp())
        values = []
        for comment in comments:
            if not isinstance(comment, dict) or not isinstance(comment.get('cid'), str) or not comment['cid']:
                raise CliError('Invalid archive comment ID.')
            values.append((str(post['pid']), comment['cid'], str(comment.get('text') or ''),
                           int(comment.get('timestamp', 0)), str(comment.get('name_tag') or ''),
                           comment.get('quote_cid'), now))
        with self.transaction():
            if self.media:
                self.media.record_comments(payload)
            self.upsert_posts([post], commit=False)
            self.connection.executemany("""INSERT INTO comments VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(pid,cid) DO UPDATE SET text=excluded.text,
                timestamp=excluded.timestamp, name_tag=excluded.name_tag,
                quote_cid=excluded.quote_cid, observed_at=excluded.observed_at""", values)
            self.connection.execute("""INSERT INTO comment_scans
                (pid,last_page,complete,observed_at,run_id,reply_count,page_size) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(pid) DO UPDATE SET last_page=excluded.last_page,
                complete=excluded.complete, observed_at=excluded.observed_at,
                run_id=excluded.run_id, reply_count=excluded.reply_count,page_size=excluded.page_size""",
                (str(post['pid']), page, int(complete), now, payload['archive_run'], int(post['reply']), page_size))

    def resume_comments(self, pid, run_id, reply=None, fresh=False, page_size=100):
        with self.lock:
            row = self.connection.execute(
                'SELECT last_page, complete, run_id, reply_count, page_size FROM comment_scans WHERE pid=?',
                (str(pid),)).fetchone()
            if row and row[1] and reply is not None and row[3] == reply and (not fresh or row[2] == run_id):
                return {'next_page': 1, 'complete': True}
            if not row or row[2] != run_id:
                return {'next_page': 1, 'complete': False}
            if reply is not None and row[3] != reply:
                return {'next_page': 1, 'complete': False}
            if not row[1] and row[4] != page_size:
                return {'next_page': 1, 'complete': False}
            # Replay the last durable page: new replies/deletions can shift offsets.
            return {'next_page': max(1, row[0]), 'complete': bool(row[1])} if row else {'next_page': 1, 'complete': False}

    def prepare_posts(self, posts, run_id, fresh=False):
        resumes = {}
        with self.transaction():
            for post in posts:
                resume = self.resume_comments(post['pid'], run_id, post['reply'], fresh)
                if self.media:
                    state = self.media.state(post['pid'], post['reply'])
                    if fresh:
                        state.update(post_known=False, unavailable=None)
                    if not state['comments_known'] and resume['complete']:
                        resume = {'next_page': 1, 'complete': False}
                    resume.update(state)
                if not resume['complete'] and resume['next_page'] > 1:
                    resume['saved_comment_ids'] = [row[0] for row in self.connection.execute(
                        'SELECT cid FROM comments WHERE pid=? LIMIT 1000', (str(post['pid']),))]
                resumes[post['pid']] = resume
            self.upsert_posts(posts, commit=False)
            self.connection.executemany('''INSERT INTO archive_members VALUES(?,?,?)
                ON CONFLICT(run_id,pid) DO UPDATE SET reply=excluded.reply''',
                [(run_id, post['pid'], post['reply']) for post in posts])
        return resumes

    def summary(self):
        return {
            'posts': self.post_count(),
            'comments': self.connection.execute('SELECT COUNT(*) FROM comments').fetchone()[0],
            'posts_with_completed_comment_scan': self.connection.execute(
                'SELECT COUNT(*) FROM comment_scans WHERE complete=1').fetchone()[0],
            'cache_integrity': self.integrity_check(),
        }

    def window_summary(self, checkpoint, filters):
        clause, parameters = filters.sql_clause()
        # Count only selected posts in this run's frozen window; historical
        # archive totals must not be presented as today's collection result.
        if checkpoint.get('archive_summary_version') == 1:
            selected = 'SELECT pid,reply FROM archive_members WHERE run_id=?'
            parameters = [checkpoint['created_at']]
        else:
            selected = f'''SELECT pid, reply FROM posts WHERE timestamp>=? AND timestamp<?
                {('AND ' + clause) if clause else ''}'''
            parameters = [checkpoint['start_timestamp'], checkpoint['end_timestamp'], *parameters]
        with self.lock:
            row = self.connection.execute(f'''WITH selected AS ({selected}) SELECT
                (SELECT COUNT(*) FROM selected) AS posts,
                (SELECT MIN(timestamp) FROM posts JOIN selected USING(pid)) AS oldest_archived_post_timestamp,
                (SELECT COUNT(*) FROM comments JOIN selected USING(pid)) AS comments,
                (SELECT COUNT(*) FROM selected p JOIN comment_scans s USING(pid)
                 WHERE s.complete=1 AND s.reply_count=p.reply) AS completed_comment_posts''',
                parameters).fetchone()
        result = dict(row)
        result['incomplete_comment_posts'] = result['posts'] - result['completed_comment_posts']
        return result


class ArchiveSink(RunSink):
    def __init__(self, archive, checkpoint, checkpoint_path, min_comments, min_favorites,
                 match_mode='all', *, source_cache=None, progress_seconds=0, progress_pages=1):
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
                or self.checkpoint['total_pages'] - self.last_reported_pages >= self.progress_pages
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

    def ingest(self, payload):
        if self.cancel_event.is_set():
            raise CliError('Collector cancelled.')
        if any(payload.get(key) for key in ('archive_comments', 'archive_resume', 'archive_prepare', 'archive_source', 'archive_post_media', 'archive_media_plan', 'archive_media_file', 'archive_media_unavailable')):
            if payload.get('schema_version') != 2:
                raise CliError('Archive sink schema mismatch.')
            with self.lock:
                if self.cancel_event.is_set():
                    raise CliError('Collector cancelled.')
                if payload.get('archive_run') != self.checkpoint['created_at']:
                    raise CliError('Archive run identity mismatch.')
                if payload.get('archive_source'):
                    page = payload.get('page')
                    if not isinstance(page, int) or not 1 <= page <= self.checkpoint.get('archive_cached_pages', 0):
                        raise CliError('Invalid cached page.')
                    posts = self.archive.candidate_page(self.checkpoint['created_at'], page)
                    expected = min(500, self.checkpoint['cached_posts'] - (page - 1) * 500)
                    if len(posts) != expected:
                        raise CliError('Cached candidate snapshot is missing. Restore it or use --fresh.')
                    return {'posts': posts}
                if payload.get('archive_prepare'):
                    posts = payload.get('posts')
                    if not isinstance(posts, list) or len(posts) > 500:
                        raise CliError('Invalid archive batch.')
                    for post in posts:
                        self.validate_post(post)
                    started = time.perf_counter()
                    resumes = self.archive.prepare_posts(posts, payload['archive_run'], self.checkpoint.get('fresh', False))
                    self.add_metric('prepare_ms', round((time.perf_counter() - started) * 1000))
                    self.add_metric('prepare_batches')
                    reused = sum(bool(row['complete']) for row in resumes.values())
                    self.reused_posts += reused
                    self.add_metric('reused_posts', reused)
                    for post in posts:
                        if resumes[post['pid']]['complete']:
                            self.record_post_date(int(post['timestamp']))
                    self.report_progress()
                    return {'resumes': resumes}
                post = payload.get('post') or {}
                self.validate_post(post)
                if payload.get('archive_post_media') or payload.get('archive_media_plan') or payload.get('archive_media_file') or payload.get('archive_media_unavailable'):
                    if not self.archive.media:
                        raise CliError('Image extraction is not enabled.')
                    if payload.get('archive_media_unavailable'):
                        self.archive.media.unavailable(str(post['pid']), 'post_not_found')
                    elif payload.get('archive_post_media'):
                        self.archive.media.record_post(post)
                    elif payload.get('archive_media_plan'):
                        request_id = payload.get('plan_id')
                        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
                            raise CliError('Invalid image plan identity.')
                        return {'images': self.archive.media.plan(str(post['pid']), request_id)}
                    else:
                        self.archive.media.save(str(post['pid']), payload)
                        self.add_metric('image_receipts')
                    self.record_post_date(int(post['timestamp']))
                    self.report_progress()
                    return
                if payload.get('archive_resume'):
                    return self.archive.resume_comments(post['pid'], payload['archive_run'], post['reply'], self.checkpoint.get('fresh', False))
                started = time.perf_counter()
                self.archive.ingest_comments(payload)
                self.record_post_date(int(post['timestamp']))
                self.add_metric('comment_write_ms', round((time.perf_counter() - started) * 1000))
                self.add_metric('comment_batches')
                self.add_metric('comment_rows_received', len(payload['comments']))
                self.comment_chunks += 1
                self.report_progress()
            return
        if self.source_cache is None:
            raise CliError('Archive list collection requires a separate source cache.')
        if payload.get('archive_cached'):
            payload = dict(payload, rows=[], scanned=0, favorite_unavailable=[], matched_pids=[])
        super().ingest(payload)
        with self.lock:
            self.report_progress(force=bool(payload.get('terminal')))


def run_archive(args, runtime):
    if not args.account.strip():
        raise CliError('--account must be a nonempty local account label.')
    if args.min_comments is not None or args.min_favorites is not None or args.match_mode != 'all':
        runtime.resolve_thresholds(args)
    if args.days is None and args.since is None:
        args.since = date(1970, 1, 2)
    if not 1 <= args.concurrency <= 8 or (args.max_total_pages is not None and args.max_total_pages < 1):
        raise CliError('Invalid archive concurrency or page limit.')
    if not 1 <= args.checkpoint_pages <= 500 or not 1 <= args.comment_batch_pages <= 20:
        raise CliError('Invalid checkpoint or comment batch size.')
    runtime.validate_progress_arguments(args)
    if not 1 <= args.cache_chunk_pages <= 20:
        raise CliError('--cache-chunk-pages must be between 1 and 20.')
    args.extract_images = args.extract_images or args.download_images
    args.archive = True
    spec = runtime.window_spec(args)
    spec.update(archive_version=2, account=args.account)
    if args.extract_images:
        spec.update(images_version=1, download_images=args.download_images)
    legacy_spec = dict(spec)
    spec['comment_page_size'] = 100
    account_key = hashlib.sha256(args.account.encode()).hexdigest()[:16]
    cache_path = (args.cache or runtime.default_runtime_root() / 'archives' / account_key / 'archive.sqlite3').resolve()
    source_path = (args.source_cache or runtime.default_cache_path()).resolve()
    if source_path == cache_path:
        raise CliError('--source-cache must differ from the archive --cache.')
    checkpoint_path = (args.checkpoint or runtime.default_checkpoint_path(spec)).resolve()
    legacy_path = runtime.default_checkpoint_path(legacy_spec).resolve()
    if not args.checkpoint and not checkpoint_path.exists() and legacy_path.exists():
        checkpoint_path = legacy_path
    start, end, label = runtime.time_window(args)
    cache = ArchiveStore(cache_path, args.account)
    if args.extract_images:
        cache.media = MediaStore(cache, cache_path.parent / 'images', args.download_images)
    source = None
    try:
        source = CacheStore(source_path)
        for store in (cache, source):
            store.connection.execute('PRAGMA busy_timeout=1000')
        checkpoint = None
        if checkpoint_path.exists() and not args.fresh:
            previous = runtime.read_checkpoint(checkpoint_path)
            if (previous.get('schema_version') == runtime.CHECKPOINT_SCHEMA_VERSION
                    and previous.get('request') == legacy_spec):
                previous['request'] = spec
                runtime.write_checkpoint(checkpoint_path, previous)
            candidate = runtime.load_checkpoint(checkpoint_path, spec)
            if candidate.get('cache_instance_id') != cache.instance_id:
                raise CliError('Archive checkpoint belongs to another database. Restore it or use --fresh.')
            if runtime.should_reuse_checkpoint(args, candidate):
                checkpoint = candidate
                if not candidate['completed'] and candidate.get('source_instance_id') != source.instance_id:
                    raise CliError('Source cache identity changed. Restore it or use --fresh.')
        if checkpoint and checkpoint['completed'] and cache.media:
            media_status = cache.media.summary(checkpoint['created_at'])
            if args.download_images and media_status['pending']:
                checkpoint = None
        if checkpoint is None:
            covering = None if args.fresh else source.find_covering(start, end, args.min_favorites is not None)
            prefix = None if args.fresh or covering else source.find_prefix(start, end, args.min_favorites is not None)
            base = covering or prefix
            scan_start = prefix['end_timestamp'] if prefix else start
            checkpoint = runtime.new_checkpoint(spec, start, end, scan_start, label,
                cache_reused=bool(base), favorites_complete=bool(base['favorites_complete']) if base else True)
            checkpoint.update(cache_path=str(cache_path), cache_instance_id=cache.instance_id,
                              archive_summary_version=1,
                              source_instance_id=source.instance_id, fresh=args.fresh,
                              archive_cache_only=bool(covering), archive_cached_pages=0, cached_posts=0)
            if base:
                count = cache.stage_candidates(source_path, checkpoint, runtime.FilterSpec.from_args(args),
                                               end if covering else prefix['end_timestamp'])
                checkpoint.update(archive_cached_pages=(count + 499) // 500, cached_posts=count)
            runtime.write_checkpoint(checkpoint_path, checkpoint)
        if (not checkpoint['completed'] and checkpoint.get('archive_cache_only')
                and not checkpoint.get('fresh') and cache.finish_cached_candidates(checkpoint)):
            checkpoint.update(completed=True, completed_at=datetime.now(SHANGHAI).isoformat())
            runtime.write_checkpoint(checkpoint_path, checkpoint)
        if not checkpoint['completed']:
            filters = runtime.FilterSpec.from_args(args).description() or '全部帖子'
            print(f"档案采集：{checkpoint['window_label']}，{filters}；"
                  f"请求并发上限 {args.concurrency}，"
                  f"复用列表缓存 {checkpoint.get('cached_posts', 0)} 帖，"
                  f"每 {args.progress_pages} 个已提交列表页汇报一次，"
                  f"每 {args.cache_chunk_pages} 页批量写入列表缓存"
                  + (f"，额外每 {args.progress_seconds} 秒汇报新进展" if args.progress_seconds else "")
                  + "。", flush=True)
            browser = runtime.ensure_standalone_login(args)
            sink = ArchiveSink(cache, checkpoint, checkpoint_path, args.min_comments, args.min_favorites,
                               args.match_mode, source_cache=source, progress_seconds=args.progress_seconds,
                               progress_pages=args.progress_pages)
            server = SinkServer(sink)
            try:
                result = runtime.run_persistent_collector(browser, args, checkpoint, sink, server.url)
                checkpoint['completed'] = bool(result['reached_start'] or result.get('feed_exhausted'))
                checkpoint['completed_at'] = datetime.now(SHANGHAI).isoformat() if checkpoint['completed'] else None
            except KeyboardInterrupt:
                sink.cancel()
                print(f"已中断，已提交的数据保留；重新运行相同命令即可续传：{checkpoint_path}", flush=True)
                raise
            except CliError as error:
                raise CliError(f"Archive interrupted ({error or 'keyboard interrupt'}). "
                               f"Saved comments remain in {cache_path}; rerun the same command "
                               f"to resume with {checkpoint_path}.") from error
            finally:
                sink.flush()
                server.close()
            if not checkpoint['completed']:
                raise CliError(f"Archive page limit reached; checkpoint saved: {checkpoint_path}. Increase --max-total-pages to continue.")
            if not checkpoint.get('archive_cache_only'):
                source.add_coverage(checkpoint['start_timestamp'], checkpoint['end_timestamp'],
                                    checkpoint['completed_at'], checkpoint['total_pages'],
                                    checkpoint['total_scanned'], checkpoint['favorites_complete'])
        with cache.transaction():
            cache.connection.execute('DELETE FROM archive_candidates WHERE run_id=?', (checkpoint['created_at'],))
        archive_totals = cache.summary()
        summary = dict(cache.window_summary(checkpoint, runtime.FilterSpec.from_args(args)),
                       archive_totals=archive_totals, cache_integrity=archive_totals['cache_integrity'],
                       start_timestamp=checkpoint['start_timestamp'], end_timestamp=checkpoint['end_timestamp'],
                       filters={'min_comments': args.min_comments, 'min_favorites': args.min_favorites, 'match_mode': args.match_mode},
                       telemetry=checkpoint['telemetry'], archive_metrics=checkpoint.get('archive_metrics', {}),
                       archive=str(cache_path), checkpoint=str(checkpoint_path),
                       source_cache=str(source_path), cached_posts=checkpoint.get('cached_posts', 0),
                       completed=checkpoint['completed'], pages=checkpoint['total_pages'],
                       oldest_post_timestamp=checkpoint.get('oldest_post_timestamp'),
                       note='Accessible feed and comment snapshots; engagement counts reflect cached observations.')
        if cache.media:
            summary['media'] = cache.media.summary(checkpoint['created_at'])
        last_timestamp = summary['oldest_post_timestamp'] or summary['oldest_archived_post_timestamp']
        summary['last_post_date'] = datetime.fromtimestamp(last_timestamp, SHANGHAI).isoformat() if last_timestamp else None
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            args.output.chmod(0o600)
        print(json.dumps(summary, ensure_ascii=False))
    finally:
        if source is not None:
            source.close()
        cache.close()


def search_archive(args):
    if args.limit < 1 or args.limit > 1000:
        raise CliError('--limit must be between 1 and 1000.')
    if not args.cache.is_file():
        raise CliError('Archive database does not exist.')
    try:
        with closing(sqlite3.connect(f"{args.cache.resolve().as_uri()}?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            if not db.execute("SELECT 1 FROM metadata WHERE key='archive_account'").fetchone():
                raise CliError('This is not an archive database.')
            rows = db.execute("""SELECT 'post' AS kind, pid, NULL AS cid, timestamp, text FROM posts
                WHERE instr(text, ?) > 0
                UNION ALL SELECT 'comment', pid, cid, timestamp, text FROM comments
                WHERE instr(text, ?) > 0 ORDER BY timestamp DESC LIMIT ?""",
                (args.query, args.query, args.limit)).fetchall()
            print(json.dumps([dict(row) for row in rows], ensure_ascii=False, indent=2))
    except sqlite3.Error as error:
        raise CliError(f'Cannot search archive: {error}') from error
