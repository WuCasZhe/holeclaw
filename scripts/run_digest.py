#!/usr/bin/env python3
import argparse
import json
import os
import signal
import shutil
import subprocess
import sys
import threading
import time
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path

try:
    from holeclaw_cache import CacheStore
    from holeclaw_checkpoint import (
        default_cache_path,
        default_checkpoint_path,
        default_runtime_root,
        empty_telemetry,
        load_checkpoint,
        merge_telemetry,
        new_checkpoint,
        read_checkpoint,
        write_checkpoint,
    )
    from holeclaw_domain import (
        CACHE_SCHEMA_VERSION,
        CHECKPOINT_SCHEMA_VERSION,
        SINK_SCHEMA_VERSION,
        TELEMETRY_FIELDS,
        TELEMETRY_MAX_FIELDS,
        CliError,
        FilterSpec,
        ReportSpec,
        SHANGHAI,
    )
    from holeclaw_reporting import one_line_summary, render_report
    from holeclaw_sink import SITE_ORIGIN, RunSink, SinkServer
except ModuleNotFoundError:
    from scripts.holeclaw_cache import CacheStore
    from scripts.holeclaw_checkpoint import (
        default_cache_path,
        default_checkpoint_path,
        default_runtime_root,
        empty_telemetry,
        load_checkpoint,
        merge_telemetry,
        new_checkpoint,
        read_checkpoint,
        write_checkpoint,
    )
    from scripts.holeclaw_domain import (
        CACHE_SCHEMA_VERSION,
        CHECKPOINT_SCHEMA_VERSION,
        SINK_SCHEMA_VERSION,
        TELEMETRY_FIELDS,
        TELEMETRY_MAX_FIELDS,
        CliError,
        FilterSpec,
        ReportSpec,
        SHANGHAI,
    )
    from scripts.holeclaw_reporting import one_line_summary, render_report
    from scripts.holeclaw_sink import SITE_ORIGIN, RunSink, SinkServer


try:
    from holeclaw_browser import (
        SITE_URL, CONFIG_KEY, PATH_ARGUMENTS, codex_base, running_on_windows, is_wsl, playwright_npx_path, is_windows_mounted_path, windows_native_path, windows_cli_arguments, windows_python_command, maybe_reexec_windows_runtime, find_pwcli, native_path, BrowserCli, ensure_gitignore_entry, ensure_path_ignored, ensure_auth_ignored, ensure_runtime_ignored, authenticated, login_open, login_save, save_login_state, load_authenticated_state, ensure_standalone_login
    )
    from holeclaw_planning import (
        parse_date, time_window, resolve_thresholds, window_spec, is_rolling_window, should_reuse_checkpoint, validate_progress_arguments
    )
    from holeclaw_runner import (
        stop_collector_process, remaining_page_limit, run_persistent_collector
    )
    from holeclaw_digest import (
        report_profile, filter_description, cache_report_data, emit_cached_report, run_standalone, run_digest
    )
except ModuleNotFoundError:
    from scripts.holeclaw_browser import (
        SITE_URL, CONFIG_KEY, PATH_ARGUMENTS, codex_base, running_on_windows, is_wsl, playwright_npx_path, is_windows_mounted_path, windows_native_path, windows_cli_arguments, windows_python_command, maybe_reexec_windows_runtime, find_pwcli, native_path, BrowserCli, ensure_gitignore_entry, ensure_path_ignored, ensure_auth_ignored, ensure_runtime_ignored, authenticated, login_open, login_save, save_login_state, load_authenticated_state, ensure_standalone_login
    )
    from scripts.holeclaw_planning import (
        parse_date, time_window, resolve_thresholds, window_spec, is_rolling_window, should_reuse_checkpoint, validate_progress_arguments
    )
    from scripts.holeclaw_runner import (
        stop_collector_process, remaining_page_limit, run_persistent_collector
    )
    from scripts.holeclaw_digest import (
        report_profile, filter_description, cache_report_data, emit_cached_report, run_standalone, run_digest
    )


def add_digest_arguments(parser: argparse.ArgumentParser, *, archive: bool = False) -> None:
    parser.add_argument("-d", "--days", type=int, default=None if archive else 30,
                        help="最近 N 天（正整数）；归档默认全部可访问历史，报告默认 30 天；--since 优先")
    parser.add_argument("-b", "--since", type=parse_date, help="起始日期 YYYY-MM-DD，包含当天，上海时区")
    parser.add_argument("-e", "--until", type=parse_date, help="结束日期 YYYY-MM-DD，包含当天；默认当前时间")
    parser.add_argument(
        "-c", "--min-comments", type=int, help="评论数严格大于 N（非负）；报告未设任何阈值时默认 50，归档不设默认阈值"
    )
    parser.add_argument(
        "-f", "--min-favorites", type=int, help="收藏数严格大于 N（非负）；仅设收藏阈值时不附加评论筛选"
    )
    parser.add_argument(
        "-m", "--match-mode",
        choices=("all", "any"),
        default="all",
        help="多个阈值的关系：all 全部满足（默认），any 满足任一",
    )
    parser.add_argument("-K", "--checkpoint-pages", type=int, default=100,
                        help="每 N 个列表页保存续传检查点（1–500，默认 100）；结束或中断时保存已提交进度")
    parser.add_argument("-B", "--cache-chunk-pages", type=int, default=1,
                        help="每 N 个列表页批量写入 SQLite（1–20，默认 1）；未提交页在中断后重扫")
    parser.add_argument("-p", "--progress-pages", type=int, default=1,
                        help="每新增 N 个已提交列表页输出进度（正整数，默认 1）；末尾不足 N 页也输出")
    parser.add_argument("-t", "--progress-seconds", type=int, default=0,
                        help="有新进展时额外按秒输出（0 关闭，默认 0；启用须 >=10）；评论采集耗时长时可设 120")
    parser.add_argument(
        "-j", "--concurrency",
        type=int,
        default=8,
        help="树洞请求并发数（1–8，默认 8）；列表、详情、评论和图片共用上限，429 时自动降低",
    )
    parser.add_argument(
        "-n", "--max-total-pages",
        type=int,
        default=None,
        help="本轮累计网络列表页上限（正整数，默认不限）；归档的缓存批次不占额度",
    )
    parser.add_argument("-k", "--checkpoint", type=Path, help="续传检查点路径；默认按时间窗口和筛选条件存入运行时目录")
    parser.add_argument("-C", "--cache", type=Path, help="SQLite 路径；报告默认共享列表库，归档默认按账号隔离的档案库")
    parser.add_argument("-F", "--fresh", action="store_true", help="忽略可复用覆盖和旧检查点，重新联网采集；不删除历史档案")
    parser.add_argument("-o", "--output", type=Path, help="输出文件；报告默认 reports/ 下的 Markdown，归档指定后另存 JSON 摘要")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="北大树洞低频采集、报告与评论归档",
                                     epilog="子命令参数说明：python3 scripts/run_digest.py <子命令> -h；全局参数放在子命令之前。",
                                     allow_abbrev=False)
    parser.add_argument("-l", "--session", default="pku-hole-digest", help="浏览器会话名（默认 pku-hole-digest）；放在子命令之前")
    parser.add_argument("-s", "--state", type=Path, default=Path(".auth/pku-treehole.json"), help="登录状态文件（默认 .auth/pku-treehole.json）；放在子命令之前")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("login-open", help="打开可见浏览器，供用户自行登录")
    subparsers.add_parser("login-save", help="完成登录后保存本地登录状态")
    add_digest_arguments(subparsers.add_parser("run", help="利用已有登录状态采集并生成报告", allow_abbrev=False))
    standalone_parser = subparsers.add_parser(
        "standalone",
        help="独立运行，必要时等待交互登录", allow_abbrev=False,
    )
    add_digest_arguments(standalone_parser)
    standalone_parser.add_argument(
        "-y", "--non-interactive",
        action="store_true",
        help="无头运行，登录失效时直接报错；适合首次登录后的定时任务",
    )
    archive_parser = subparsers.add_parser("archive", help="归档可访问帖子、评论及可选图片", allow_abbrev=False)
    add_digest_arguments(archive_parser, archive=True)
    archive_parser.set_defaults(days=None)
    archive_parser.add_argument("-i", "--extract-images", action="store_true", help="保存帖子和评论的图片引用，不下载文件")
    archive_parser.add_argument("-I", "--download-images", action="store_true", help="提取图片引用并下载原图到账号档案 images/ 目录")
    archive_parser.add_argument("-a", "--account", required=True, help="必填：本地账号标签；不同登录账号使用不同标签")
    archive_parser.add_argument("-y", "--non-interactive", action="store_true", help="无头运行，登录失效时直接报错，不等待用户登录")
    archive_parser.add_argument("-S", "--source-cache", type=Path, help="可复用列表缓存路径；默认现有报告列表库，与档案库分开")
    archive_parser.add_argument("-P", "--comment-batch-pages", type=int, default=10, help="每 N 个评论页提交一批（1–20，默认 10）；帖子结束时提交余量")
    search_parser = subparsers.add_parser("archive-search", help="离线搜索本地档案，无需登录", allow_abbrev=False)
    search_parser.add_argument("-C", "--cache", type=Path, required=True, help="必填：要搜索的 SQLite 档案库路径")
    search_parser.add_argument("-q", "--query", required=True, help="必填：在帖子和评论正文中查找的关键词")
    search_parser.add_argument("-n", "--limit", type=int, default=50, help="最多返回的结果条数（1–1000，默认 50）")
    return parser


def main() -> None:
    try:
        args = build_parser().parse_args()
        reexec_code = None if args.command == "archive-search" else maybe_reexec_windows_runtime()
        if reexec_code is not None:
            raise SystemExit(reexec_code)
        if args.command == "login-open":
            login_open(args)
        elif args.command == "login-save":
            login_save(args)
        elif args.command == "standalone":
            run_standalone(args)
        elif args.command in ("archive", "archive-search"):
            try:
                from holeclaw_archive import run_archive, search_archive
            except ModuleNotFoundError:
                from scripts.holeclaw_archive import run_archive, search_archive
            if args.command == "archive":
                run_archive(args)
            else:
                search_archive(args)
        else:
            run_digest(args)
    except CliError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)
    except KeyboardInterrupt:
        print("ERROR: 已中断。", file=sys.stderr)
        raise SystemExit(130)


if __name__ == "__main__":
    main()
