"""Filtered text archive backed by reusable list and comment snapshots."""
import hashlib
import json
import sqlite3
from copy import copy
from contextlib import closing
from datetime import date, datetime

try:
    from holeclaw_media import MediaStore
    from holeclaw_cache import CacheStore
    from holeclaw_domain import CliError, SHANGHAI
    from holeclaw_sink import SinkServer
    from holeclaw_search import search_rows
except ModuleNotFoundError:
    from scripts.holeclaw_media import MediaStore
    from scripts.holeclaw_cache import CacheStore
    from scripts.holeclaw_domain import CliError, SHANGHAI
    from scripts.holeclaw_sink import SinkServer
    from scripts.holeclaw_search import search_rows


try:
    from holeclaw_checkpoint import (CheckpointState, default_cache_path, default_checkpoint_path,
        default_runtime_root, load_checkpoint, write_checkpoint)
    from holeclaw_planning import (RunPlan, resolve_thresholds, validate_collection_arguments,
        window_spec, time_window, should_reuse_checkpoint)
    from holeclaw_domain import FilterSpec
    from holeclaw_runner import CollectorServices
except ModuleNotFoundError:
    from scripts.holeclaw_checkpoint import (CheckpointState, default_cache_path, default_checkpoint_path,
        default_runtime_root, load_checkpoint, write_checkpoint)
    from scripts.holeclaw_planning import (RunPlan, resolve_thresholds, validate_collection_arguments,
        window_spec, time_window, should_reuse_checkpoint)
    from scripts.holeclaw_domain import FilterSpec
    from scripts.holeclaw_runner import CollectorServices


try:
    from holeclaw_archive_store import ArchiveStore
    from holeclaw_archive_sink import ArchiveSink
except ModuleNotFoundError:
    from scripts.holeclaw_archive_store import ArchiveStore
    from scripts.holeclaw_archive_sink import ArchiveSink


def run_archive(args, services: CollectorServices | None = None):
    args = copy(args)
    services = services or CollectorServices.default()
    if not args.account.strip():
        raise CliError('--account must be a nonempty local account label.')
    if args.min_comments is not None or args.min_favorites is not None or args.match_mode != 'all':
        resolve_thresholds(args)
    if args.days is None and args.since is None:
        args.since = date(1970, 1, 2)
    validate_collection_arguments(args, archive=True)
    args.extract_images = args.extract_images or args.download_images
    args.archive = True
    spec = window_spec(args)
    spec.update(archive_version=2, account=args.account)
    if args.extract_images:
        spec.update(images_version=1, download_images=args.download_images)
    legacy_spec = dict(spec)
    spec['comment_page_size'] = 100
    account_key = hashlib.sha256(args.account.encode()).hexdigest()[:16]
    cache_path = (args.cache or default_runtime_root() / 'archives' / account_key / 'archive.sqlite3').resolve()
    source_path = (args.source_cache or default_cache_path()).resolve()
    if source_path == cache_path:
        raise CliError('--source-cache must differ from the archive --cache.')
    checkpoint_path = (args.checkpoint or default_checkpoint_path(spec)).resolve()
    legacy_path = default_checkpoint_path(legacy_spec).resolve()
    if not args.checkpoint and not checkpoint_path.exists() and legacy_path.exists():
        checkpoint_path = legacy_path
    start, end, label = time_window(args)
    cache = ArchiveStore(cache_path, args.account)
    source = None
    try:
        if args.extract_images:
            cache.media = MediaStore(cache, cache_path.parent / 'images', args.download_images)
        source = CacheStore(source_path)
        for store in (cache, source):
            store.connection.execute('PRAGMA busy_timeout=1000')
        checkpoint = None
        if checkpoint_path.exists() and not args.fresh:
            candidate = load_checkpoint(checkpoint_path, spec, legacy_spec=legacy_spec)
            if candidate.get('cache_instance_id') != cache.instance_id:
                raise CliError('Archive checkpoint belongs to another database. Restore it or use --fresh.')
            if should_reuse_checkpoint(args, candidate):
                checkpoint = candidate
                if not candidate['completed'] and candidate.get('source_instance_id') != source.instance_id:
                    raise CliError('Source cache identity changed. Restore it or use --fresh.')
        if checkpoint and checkpoint['completed'] and cache.media:
            media_status = cache.media.summary(checkpoint['created_at'])
            if args.download_images and media_status['pending']:
                checkpoint = None
        if checkpoint is None:
            plan = RunPlan.build(source, start, end, label, fresh=args.fresh,
                                 require_favorites=args.min_favorites is not None)
            base = plan.coverage
            checkpoint = plan.new_checkpoint(spec)
            checkpoint.update(cache_path=str(cache_path), cache_instance_id=cache.instance_id,
                              archive_summary_version=1,
                              source_instance_id=source.instance_id, fresh=args.fresh,
                              archive_cache_only=plan.cache_only, archive_cached_pages=0, cached_posts=0)
            if base:
                count = cache.stage_candidates(source_path, checkpoint, FilterSpec.from_args(args),
                                               end if plan.cache_only else plan.scan_start)
                checkpoint.update(archive_cached_pages=(count + 499) // 500, cached_posts=count)
            write_checkpoint(checkpoint_path, checkpoint)
        if (not checkpoint['completed'] and checkpoint.get('archive_cache_only')
                and not checkpoint.get('fresh') and cache.finish_cached_candidates(checkpoint)):
            CheckpointState(checkpoint).finish({'reached_start': True})
            write_checkpoint(checkpoint_path, checkpoint)
        if not checkpoint['completed']:
            filters = FilterSpec.from_args(args).description() or '全部帖子'
            print(f"档案采集：{checkpoint['window_label']}，{filters}；"
                  f"请求并发上限 {args.concurrency}，"
                  f"复用列表缓存 {checkpoint.get('cached_posts', 0)} 帖，"
                  f"每 {args.cache_chunk_pages} 页批量写入列表缓存"
                  + (f"，每 {args.progress_pages} 个已提交列表页汇报新进展" if args.progress_pages else "")
                  + (f"，每 {args.progress_seconds} 秒汇报新进展" if args.progress_seconds else "")
                  + "，阶段结束时汇报剩余进展"
                  + "。", flush=True)
            browser = services.ensure_standalone_login(args)
            sink = ArchiveSink(cache, checkpoint, checkpoint_path, args.min_comments, args.min_favorites,
                               args.match_mode, source_cache=source, progress_seconds=args.progress_seconds,
                               progress_pages=args.progress_pages)
            with closing(SinkServer(sink)) as server:
                try:
                    result = services.run_persistent_collector(browser, args, checkpoint, sink, server.url)
                    CheckpointState(checkpoint).finish(result)
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
            if not checkpoint['completed']:
                raise CliError(f"Archive page limit reached; checkpoint saved: {checkpoint_path}. Increase --max-total-pages to continue.")
            if not checkpoint.get('archive_cache_only'):
                source.add_coverage(checkpoint['start_timestamp'], checkpoint['end_timestamp'],
                                    checkpoint['completed_at'], checkpoint['total_pages'],
                                    checkpoint['total_scanned'], checkpoint['favorites_complete'])
        with cache.transaction():
            cache.connection.execute('DELETE FROM archive_candidates WHERE run_id=?', (checkpoint['created_at'],))
        archive_totals = cache.summary(verify=args.verify_cache)
        summary = dict(cache.window_summary(checkpoint, FilterSpec.from_args(args)),
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
        try:
            if source is not None:
                source.close()
        finally:
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
            rows = search_rows(db, args.query, args.limit)
            print(json.dumps([dict(row) for row in rows], ensure_ascii=False, indent=2))
    except sqlite3.Error as error:
        raise CliError(f'Cannot search archive: {error}') from error
