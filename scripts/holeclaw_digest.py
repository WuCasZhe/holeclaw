import argparse
import json
from copy import copy
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

try:
    from holeclaw_cache import CacheStore
    from holeclaw_checkpoint import CheckpointState, default_cache_path, default_checkpoint_path, load_checkpoint, read_checkpoint, write_checkpoint
    from holeclaw_domain import CHECKPOINT_SCHEMA_VERSION, CliError, FilterSpec, ReportSpec, SHANGHAI
    from holeclaw_reporting import render_report
    from holeclaw_sink import RunSink, SinkServer
except ModuleNotFoundError:
    from scripts.holeclaw_cache import CacheStore
    from scripts.holeclaw_checkpoint import CheckpointState, default_cache_path, default_checkpoint_path, load_checkpoint, read_checkpoint, write_checkpoint
    from scripts.holeclaw_domain import CHECKPOINT_SCHEMA_VERSION, CliError, FilterSpec, ReportSpec, SHANGHAI
    from scripts.holeclaw_reporting import render_report
    from scripts.holeclaw_sink import RunSink, SinkServer


try:
    from holeclaw_browser import BrowserCli, ensure_standalone_login, load_authenticated_state, ensure_runtime_ignored
    from holeclaw_planning import RunPlan, resolve_thresholds, validate_collection_arguments, window_spec, time_window, should_reuse_checkpoint
    from holeclaw_runner import CollectorServices, run_persistent_collector
except ModuleNotFoundError:
    from scripts.holeclaw_browser import BrowserCli, ensure_standalone_login, load_authenticated_state, ensure_runtime_ignored
    from scripts.holeclaw_planning import RunPlan, resolve_thresholds, validate_collection_arguments, window_spec, time_window, should_reuse_checkpoint
    from scripts.holeclaw_runner import CollectorServices, run_persistent_collector

def report_profile(
    min_comments: int | None, min_favorites: int | None, match_mode: str
) -> tuple[str, str]:
    return FilterSpec(min_comments, min_favorites, match_mode).report_profile()


def filter_description(
    min_comments: int | None, min_favorites: int | None, match_mode: str
) -> str:
    return FilterSpec(min_comments, min_favorites, match_mode).description()


def cache_report_data(
    cache: CacheStore,
    report: ReportSpec,
    collected_at: str,
    pages: int,
    scanned: int,
    cache_reused: bool,
) -> dict:
    filters = report.filters
    candidates = cache.query_posts(
        report.start_ts,
        report.end_ts,
        filters.min_comments,
        filters.min_favorites,
        filters.match_mode,
    )
    return {
        "collected_at": collected_at,
        "start_timestamp": report.start_ts,
        "end_timestamp": report.end_ts,
        "min_comments": filters.min_comments,
        "min_favorites": filters.min_favorites,
        "match_mode": filters.match_mode,
        "pages": pages,
        "scanned": scanned,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "cache_reused": cache_reused,
        "favorite_unavailable": cache.query_favorite_unavailable(
            report.start_ts, report.end_ts
        ),
    }


def emit_cached_report(
    cache: CacheStore,
    report: ReportSpec,
    collected_at: str,
    pages: int,
    scanned: int,
    cache_reused: bool,
    marker: str,
    verify: bool = False,
) -> None:
    data = cache_report_data(
        cache, report, collected_at, pages, scanned, cache_reused
    )
    render_report(data, report.output, report.window_label)
    summary = {
        "report": str(report.output),
        "cache": str(cache.path),
        "pages": data["pages"],
        "scanned": data["scanned"],
        "matched": data["candidate_count"],
        marker: True,
        "cache_integrity": cache.integrity_check() if verify else "not_checked",
    }
    print(json.dumps(summary, ensure_ascii=False))

def run_standalone(args: argparse.Namespace, services: CollectorServices | None = None) -> None:
    run_digest(args, standalone=True, services=services)

def run_digest(args: argparse.Namespace, standalone: bool = False,
               services: CollectorServices | None = None) -> None:
    args = copy(args)
    services = services or CollectorServices(ensure_standalone_login, run_persistent_collector)
    resolve_thresholds(args)
    validate_collection_arguments(args)
    spec = window_spec(args)
    start_ts, end_ts, window_label = time_window(args)
    checkpoint_path = (args.checkpoint or default_checkpoint_path(spec)).resolve()
    cache_path = (args.cache or default_cache_path()).resolve()
    ensure_runtime_ignored(checkpoint_path, cache_path)
    if checkpoint_path.exists() and args.fresh:
        existing_checkpoint = read_checkpoint(checkpoint_path)
        if existing_checkpoint.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
            raise CliError(
                f"Checkpoint is incompatible with schema v{CHECKPOINT_SCHEMA_VERSION}: "
                f"{checkpoint_path}. Use a new checkpoint path."
            )
    cache_existed = cache_path.exists()
    report_slug, _title = report_profile(
        args.min_comments, args.min_favorites, args.match_mode
    )
    output = (
        args.output
        or Path.cwd() / "reports" / f"pku-treehole-{report_slug}-{date.today().isoformat()}.md"
    )
    output = output.resolve()
    cache = CacheStore(cache_path)
    try:
        checkpoint = None
        completed_checkpoint = None
        if checkpoint_path.exists() and not args.fresh:
            candidate = load_checkpoint(checkpoint_path, spec)
            if not should_reuse_checkpoint(args, candidate):
                print(
                    "已完成的检查点尚未覆盖当前请求窗口；"
                    "将按当前时间重新规划窗口并复用 SQLite 覆盖。",
                    flush=True,
                )
            else:
                checkpoint = candidate
                start_ts = checkpoint["start_timestamp"]
                end_ts = checkpoint["end_timestamp"]
                window_label = checkpoint["window_label"]
                recorded_cache = checkpoint.get("cache_path")
                if recorded_cache and Path(recorded_cache).resolve() != cache_path:
                    raise CliError(
                        "Checkpoint belongs to a different SQLite cache. "
                        "Use its original --cache path or start with --fresh."
                    )
                if checkpoint["total_pages"] > 0 and not cache_existed:
                    raise CliError(
                        "The SQLite cache required by this checkpoint is missing. "
                        "Restore it or use --fresh."
                    )
                recorded_instance = checkpoint.get("cache_instance_id")
                if recorded_instance != cache.instance_id:
                    raise CliError(
                        "Checkpoint SQLite cache identity does not match. "
                        "Restore the original cache or use --fresh."
                    )
                checkpoint["cache_path"] = str(cache_path)
                if checkpoint["completed"]:
                    completed_checkpoint = checkpoint
                else:
                    print(
                        f"恢复检查点：从 API 第 {checkpoint['next_page']} 页继续，"
                        f"已累计 {checkpoint['total_pages']} 页 / "
                        f"{checkpoint['total_scanned']:,} 条。",
                        flush=True,
                    )

        covering = None
        cache_marker = None
        if completed_checkpoint:
            covering = cache.find_covering(
                start_ts, end_ts, require_favorites=args.min_favorites is not None
            )
            cache_marker = "reused_completed_checkpoint"
        elif checkpoint is None and not args.fresh:
            covering = cache.find_covering(
                start_ts, end_ts, require_favorites=args.min_favorites is not None
            )
            if covering:
                cache_marker = "cache_hit"

        if cache_marker:
            report = ReportSpec.from_args(args, output, window_label, start_ts, end_ts)
            emit_cached_report(
                cache,
                report,
                (
                    covering["completed_at"]
                    if covering
                    else completed_checkpoint.get(
                        "completed_at", completed_checkpoint["updated_at"]
                    )
                ),
                0 if covering else completed_checkpoint["total_pages"],
                0 if covering else completed_checkpoint["total_scanned"],
                bool(covering),
                cache_marker,
                verify=getattr(args, 'verify_cache', False),
            )
            return

        if checkpoint is None:
            plan = RunPlan.build(cache, start_ts, end_ts, window_label, fresh=args.fresh,
                                 require_favorites=args.min_favorites is not None)
            cache_base = plan.coverage
            scan_start_ts = plan.scan_start
            checkpoint = plan.new_checkpoint(spec)
            checkpoint["cache_path"] = str(cache_path)
            checkpoint["cache_instance_id"] = cache.instance_id
            write_checkpoint(checkpoint_path, checkpoint)
            if cache_base:
                print(
                    "复用 SQLite 历史覆盖：只扫描"
                    f" {datetime.fromtimestamp(scan_start_ts, SHANGHAI):%Y-%m-%d %H:%M:%S}"
                    " 之后的新帖子。",
                    flush=True,
                )

        if standalone:
            browser = services.ensure_standalone_login(args)
        else:
            state = args.state.resolve()
            if not state.is_file():
                raise CliError(f"Login state not found: {state}. Run login-open first.")

            browser = BrowserCli(args.session)
            browser.ensure_session()
            if not load_authenticated_state(browser, state):
                raise CliError("登录已失效或未成功加载，请重新执行 login-open 和 login-save。")

        print(
            f"开始常驻有限并发扫描：{window_label}，"
            f"{filter_description(args.min_comments, args.min_favorites, args.match_mode)}，"
            f"并发度 {args.concurrency}，每个请求 0.6–2 秒抖动，"
            f"默认每 {args.checkpoint_pages} 页保存检查点。",
            flush=True,
        )
        sink = RunSink(
            cache,
            checkpoint,
            checkpoint_path,
            args.min_comments,
            args.min_favorites,
            args.match_mode,
        )
        with closing(SinkServer(sink)) as server:
            try:
                result = services.run_persistent_collector(browser, args, checkpoint, sink, server.url)
            except KeyboardInterrupt as error:
                sink.flush()
                raise CliError(
                    "采集已中断，最新检查点已保存："
                    f"{checkpoint_path}；重新运行相同命令将从 API 第 "
                    f"{checkpoint['next_page']} 页继续。"
                ) from error
            except Exception:
                sink.flush()
                raise

        CheckpointState(checkpoint).finish(result)
        if not checkpoint["completed"]:
            sink.flush()
            if args.max_total_pages is not None:
                raise CliError(
                    f"Reached --max-total-pages={args.max_total_pages} before the start time. "
                    f"Checkpoint saved at {checkpoint_path}."
                )
            raise CliError(
                "Collector stopped before the start time or feed exhaustion. "
                f"Checkpoint saved at {checkpoint_path}."
            )
        write_checkpoint(checkpoint_path, checkpoint)

        cache.add_coverage(
            start_ts,
            end_ts,
            checkpoint["completed_at"],
            checkpoint["total_pages"],
            checkpoint["total_scanned"],
            checkpoint["favorites_complete"],
        )
        report = ReportSpec.from_args(args, output, window_label, start_ts, end_ts)
        data = cache_report_data(
            cache,
            report,
            checkpoint["completed_at"],
            checkpoint["total_pages"],
            checkpoint["total_scanned"],
            bool(checkpoint.get("cache_reused")),
        )
        render_report(data, output, window_label)
        print(
            json.dumps(
                {
                    "report": str(output),
                    "checkpoint": str(checkpoint_path),
                    "cache": str(cache_path),
                    "pages": data["pages"],
                    "scanned": data["scanned"],
                    "matched": data["candidate_count"],
                    "reached_start": checkpoint["reached_start"],
                    "cache_integrity": cache.integrity_check() if getattr(args, 'verify_cache', False) else "not_checked",
                    "telemetry": checkpoint["telemetry"],
                },
                ensure_ascii=False,
            )
        )
    finally:
        cache.close()
