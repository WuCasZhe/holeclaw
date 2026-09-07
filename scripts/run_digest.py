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


SITE_URL = "https://treehole.pku.edu.cn/ch/web/pc/index"
CONFIG_KEY = "codex_pku_digest_config"
PATH_ARGUMENTS = {"--state", "--cache", "--source-cache", "--checkpoint", "--output",
                  "-s", "-C", "-S", "-k", "-o"}


def codex_base() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))


def running_on_windows() -> bool:
    return os.name == "nt"


def is_wsl() -> bool:
    return not running_on_windows() and bool(os.environ.get("WSL_DISTRO_NAME"))


def playwright_npx_path() -> str | None:
    override = os.environ.get("PLAYWRIGHT_CLI_NPX")
    if override:
        return override
    windows_npx = Path("/mnt/c/Program Files/nodejs/npx")
    if is_wsl() and not shutil.which("google-chrome") and windows_npx.is_file():
        return str(windows_npx)
    return shutil.which("npx")


def is_windows_mounted_path(path: str | None) -> bool:
    if not path:
        return False
    normalized = str(Path(path).expanduser()).replace("\\", "/")
    return normalized == "/mnt" or normalized.startswith("/mnt/")


def windows_native_path(value: str) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        return value
    completed = subprocess.run(
        ["wslpath", "-w", str(path.resolve())],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    )
    return completed.stdout.strip()


def windows_cli_arguments(arguments: list[str]) -> list[str]:
    converted = []
    expecting_path = False
    for argument in arguments:
        if expecting_path:
            converted.append(windows_native_path(argument))
            expecting_path = False
            continue
        matched_option = next(
            (option for option in PATH_ARGUMENTS if argument.startswith(option + "=")),
            None,
        )
        if matched_option:
            _option, value = argument.split("=", 1)
            converted.append(f"{matched_option}={windows_native_path(value)}")
            continue
        # argparse also accepts attached short values, e.g. -o/tmp/report.json.
        short_option = next((option for option in PATH_ARGUMENTS
                             if len(option) == 2 and argument.startswith(option)
                             and len(argument) > 2), None)
        if short_option:
            converted.extend([short_option, windows_native_path(argument[2:])])
            continue
        converted.append(argument)
        expecting_path = argument in PATH_ARGUMENTS
    return converted


def windows_python_command() -> list[str] | None:
    python = shutil.which("python.exe")
    if python:
        return [python]
    launcher = shutil.which("py.exe")
    return [launcher, "-3"] if launcher else None


def maybe_reexec_windows_runtime(arguments: list[str] | None = None) -> int | None:
    if not is_wsl() or not is_windows_mounted_path(playwright_npx_path()):
        return None
    python_command = windows_python_command()
    if not python_command:
        raise CliError(
            "Playwright 将使用 Windows Node，但未找到 Windows Python。"
            "请安装 Windows Python，或在 WSL 内安装原生 Node.js 和浏览器。"
        )
    child_environment = os.environ.copy()
    child_environment["PYTHONUTF8"] = "1"
    child_environment.pop("PWCLI", None)
    child_environment.pop("PLAYWRIGHT_CLI_NPX", None)
    codex_home = child_environment.get("CODEX_HOME")
    if codex_home and not is_windows_mounted_path(codex_home):
        child_environment.pop("CODEX_HOME", None)
    runtime_root = child_environment.get("HOLECLAW_RUNTIME_DIR")
    if runtime_root:
        native_runtime = windows_native_path(runtime_root)
        if native_runtime.startswith("\\\\wsl"):
            raise CliError(
                "Windows 采集运行时不能把 SQLite 放在 WSL 文件系统。"
                "请取消 HOLECLAW_RUNTIME_DIR，或将其设置到 /mnt/c 下。"
            )
        child_environment["HOLECLAW_RUNTIME_DIR"] = native_runtime
    child_arguments = windows_cli_arguments(
        list(sys.argv[1:] if arguments is None else arguments)
    )
    command = [
        *python_command,
        windows_native_path(str(Path(__file__).resolve())),
        *child_arguments,
    ]
    print(
        "检测到 Windows Playwright 依赖；切换到 Windows Python，"
        "确保浏览器、本地回调和 SQLite 位于同一运行环境。",
        flush=True,
    )
    return subprocess.run(command, env=child_environment).returncode


def find_pwcli() -> Path:
    override = os.environ.get("PWCLI")
    wrapper_name = (
        "playwright_cli.cmd" if running_on_windows() else "playwright_cli.sh"
    )
    local_wrapper = Path(__file__).with_name(wrapper_name)
    codex_wrapper = codex_base() / "skills/playwright/scripts" / wrapper_name
    candidates = [Path(override)] if override else [local_wrapper, codex_wrapper]
    path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if path is None:
        checked = ", ".join(str(candidate) for candidate in candidates)
        raise CliError(f"Playwright wrapper not found. Checked: {checked}")
    npx = (
        shutil.which("npx.cmd") or shutil.which("npx")
        if running_on_windows()
        else playwright_npx_path()
    )
    if not npx:
        raise CliError("npx is required. Install Node.js/npm first.")
    return path


def native_path(path: Path) -> str:
    # Windows-backed WSL launches are re-executed before reaching BrowserCli.
    return str(path.resolve())


class BrowserCli:
    def __init__(self, session: str, headed: bool = True):
        self.pwcli = find_pwcli()
        self.session = session
        self.headed = headed

    def run(self, *args: str, check: bool = True, raw: bool = False) -> subprocess.CompletedProcess:
        command = [str(self.pwcli), f"-s={self.session}"]
        if raw:
            command.append("--raw")
        command.extend(args)
        completed = subprocess.run(
            command,
            text=True,
            encoding="utf-8",
            capture_output=True,
            errors="replace",
        )
        if check and completed.returncode != 0:
            message = (completed.stdout + "\n" + completed.stderr).strip()
            raise CliError(message[-3000:])
        return completed

    def ensure_session(self) -> None:
        listing = subprocess.run(
            [str(self.pwcli), "list"],
            text=True,
            encoding="utf-8",
            capture_output=True,
            errors="replace",
        )
        if self.session not in listing.stdout:
            arguments = ["open", "about:blank"]
            if self.headed:
                arguments.append("--headed")
            self.run(*arguments)


def ensure_gitignore_entry(entry: str) -> None:
    ignore = Path.cwd() / ".gitignore"
    current = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
    if entry not in current.splitlines():
        suffix = "" if not current or current.endswith("\n") else "\n"
        ignore.write_text(current + suffix + entry + "\n", encoding="utf-8")


def ensure_path_ignored(path: Path, root: Path, entry: str) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    ensure_gitignore_entry(entry)
    return True


def ensure_auth_ignored(state: Path) -> None:
    ensure_path_ignored(state, Path.cwd() / ".auth", ".auth/")


def ensure_runtime_ignored(*paths: Path) -> None:
    runtime_root = (Path.cwd() / "output/playwright").resolve()
    for path in paths:
        if ensure_path_ignored(path, runtime_root, "output/playwright/"):
            return


def authenticated(snapshot: str) -> bool:
    return "Page Title: 北大树洞" in snapshot and "treehole.pku.edu.cn/ch/web/pc/index" in snapshot


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Use YYYY-MM-DD.") from error


def time_window(args: argparse.Namespace) -> tuple[int, int, str]:
    now = datetime.now(SHANGHAI)
    if args.since:
        start = datetime.combine(args.since, dt_time.min, SHANGHAI)
        requested_end = (
            datetime.combine(args.until + timedelta(days=1), dt_time.min, SHANGHAI)
            if args.until
            else now
        )
        end = min(requested_end, now)
        label = f"{start:%Y-%m-%d}至{(end - timedelta(seconds=1)):%Y-%m-%d}"
    else:
        if args.days <= 0:
            raise CliError("--days must be positive.")
        requested_end = (
            datetime.combine(args.until + timedelta(days=1), dt_time.min, SHANGHAI)
            if args.until
            else now
        )
        end = min(requested_end, now)
        start = end - timedelta(days=args.days)
        label = f"近{args.days}天"
    if start >= end:
        raise CliError("The start time must be before the end time.")
    if start >= now:
        raise CliError("The requested range is entirely in the future.")
    return int(start.timestamp()), int(end.timestamp()), label


def resolve_thresholds(args: argparse.Namespace) -> None:
    if args.min_comments is None and args.min_favorites is None:
        args.min_comments = 50
    if args.min_comments is not None and args.min_comments < 0:
        raise CliError("--min-comments cannot be negative.")
    if args.min_favorites is not None and args.min_favorites < 0:
        raise CliError("--min-favorites cannot be negative.")
    if args.match_mode == "any" and (
        args.min_comments is None or args.min_favorites is None
    ):
        raise CliError("--match-mode any requires both --min-comments and --min-favorites.")


def window_spec(args: argparse.Namespace) -> dict:
    spec = {
        "days": None if args.since else args.days,
        "since": args.since.isoformat() if args.since else None,
        "until": args.until.isoformat() if args.until else None,
        "min_comments": args.min_comments,
    }
    # Preserve compatibility with checkpoints created before favorite filtering existed.
    if args.min_favorites is not None:
        spec["min_favorites"] = args.min_favorites
    if args.match_mode != "all":
        spec["match_mode"] = args.match_mode
    return spec


def is_rolling_window(args: argparse.Namespace) -> bool:
    """Return true only when the window end moves with the current clock."""
    if args.until is None:
        return True
    requested_end = datetime.combine(
        args.until + timedelta(days=1), dt_time.min, SHANGHAI
    )
    return requested_end > datetime.now(SHANGHAI)


def should_reuse_checkpoint(args: argparse.Namespace, checkpoint: dict) -> bool:
    """Resume unfinished work, but never let a completed run freeze a rolling window."""
    if not checkpoint.get("completed", False):
        return True
    if is_rolling_window(args):
        return False
    # A future --until may have been clipped to the clock on the previous run.
    requested_end = int(datetime.combine(
        args.until + timedelta(days=1), dt_time.min, SHANGHAI
    ).timestamp())
    return checkpoint.get("end_timestamp", requested_end) >= requested_end

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
    }
    print(json.dumps(summary, ensure_ascii=False))


def login_open(args: argparse.Namespace) -> None:
    browser = BrowserCli(args.session)
    browser.ensure_session()
    browser.run("goto", SITE_URL)
    print("可视浏览器已打开。请亲自完成北大统一身份认证，进入树洞首页后再保存登录状态。")


def login_save(args: argparse.Namespace) -> None:
    state = args.state.resolve()
    browser = BrowserCli(args.session)
    browser.ensure_session()
    save_login_state(browser, state)


def save_login_state(browser: BrowserCli, state: Path) -> None:
    snapshot = browser.run("snapshot").stdout
    if not authenticated(snapshot):
        raise CliError("当前页面仍未进入北大树洞首页，请先完成登录。")
    state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    browser.run("state-save", native_path(state))
    state.chmod(0o600)
    ensure_auth_ignored(state)
    print(f"登录状态已保存：{state}")


def load_authenticated_state(browser: BrowserCli, state: Path) -> bool:
    browser.run("goto", "about:blank")
    browser.run("state-load", native_path(state))
    browser.run("goto", SITE_URL)
    return authenticated(browser.run("snapshot").stdout)


def ensure_standalone_login(args: argparse.Namespace) -> BrowserCli:
    state = args.state.resolve()
    if not state.is_file() and args.non_interactive:
        raise CliError(
            "未找到本地登录状态；--non-interactive 模式不会等待人工登录。"
            "请先不带该参数运行一次 standalone。"
        )

    browser = BrowserCli(args.session, headed=not args.non_interactive)
    browser.ensure_session()
    if state.is_file():
        try:
            if load_authenticated_state(browser, state):
                print("已加载有效登录状态，继续自动采集。", flush=True)
                return browser
            reason = "已保存的登录状态已失效"
        except CliError:
            reason = "已保存的登录状态无法加载"
            if not args.non_interactive:
                browser.run("goto", SITE_URL)
    else:
        browser.run("goto", SITE_URL)
        reason = "未找到本地登录状态"

    if args.non_interactive:
        raise CliError(
            f"{reason}；--non-interactive 模式不会等待人工登录。"
            "请先不带该参数运行一次 standalone。"
        )

    print(
        f"{reason}。可视浏览器已打开，请亲自完成北大统一身份认证。\n"
        "进入北大树洞首页后，回到此终端按 Enter 继续。",
        flush=True,
    )
    try:
        input()
    except EOFError as error:
        raise CliError("需要交互式终端完成首次登录。") from error
    save_login_state(browser, state)
    return browser


def run_standalone(args: argparse.Namespace) -> None:
    run_digest(args, standalone=True)


def stop_collector_process(process: subprocess.Popen, timeout: float = 1) -> None:
    if os.name == "nt":
        if process.poll() is None:
            # The browser already received cooperative cancellation. Kill the
            # wrapper tree before its root can exit and orphan npx/Node children.
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, timeout=3)
        process.wait(timeout=timeout)
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass
    finally:
        # The group can outlive its leader, including a wrapper that exited early.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=timeout)


def remaining_page_limit(
    max_total_pages: int | None, completed_pages: int
) -> int | None:
    if max_total_pages is None:
        return None
    remaining = max_total_pages - completed_pages
    if remaining <= 0:
        raise CliError(
            f"Reached --max-total-pages={max_total_pages} before the start time."
        )
    return remaining


def run_persistent_collector(
    browser: BrowserCli,
    args: argparse.Namespace,
    checkpoint: dict,
    sink: RunSink,
    sink_url: str,
) -> dict:
    cached_pages = checkpoint.get("archive_cached_pages", 0)
    cached_remaining = max(0, cached_pages - checkpoint["next_page"] + 1)
    remaining = (cached_remaining if checkpoint.get("archive_cache_only") else
                 remaining_page_limit(args.max_total_pages, max(0, checkpoint["total_pages"] - cached_pages)))
    if remaining is not None and not checkpoint.get("archive_cache_only"):
        remaining += cached_remaining
    config = {
        "archive": getattr(args, "archive", False),
        "archive_run": checkpoint["created_at"],
        "archive_cached_pages": checkpoint.get("archive_cached_pages", 0),
        "archive_cache_only": checkpoint.get("archive_cache_only", False),
        "comment_batch_pages": getattr(args, "comment_batch_pages", 10),
        "comment_page_size": 100,
        "extract_images": getattr(args, "extract_images", False),
        "download_images": getattr(args, "download_images", False),
        "control_url": sink_url.replace("/ingest?", "/control?"),
        "report_start_timestamp": checkpoint["start_timestamp"],
        "scan_start_timestamp": checkpoint["scan_start_timestamp"],
        "end_timestamp": checkpoint["end_timestamp"],
        "min_comments": args.min_comments,
        "min_favorites": args.min_favorites,
        "match_mode": args.match_mode,
        "start_page": checkpoint["next_page"],
        "page_size": 500,
        "max_pages": remaining,
        "pages_before": checkpoint["total_pages"],
        "checkpoint_pages": args.checkpoint_pages,
        "cache_chunk_pages": args.cache_chunk_pages,
        "request_concurrency": args.concurrency,
        "delay_min_ms": 600,
        "delay_max_ms": 2000,
        "sink_url": sink_url,
    }
    browser.run("sessionstorage-set", CONFIG_KEY, json.dumps(config, separators=(",", ":")))

    collector = Path(__file__).with_name("collect.js")
    command = [
        str(browser.pwcli),
        f"-s={args.session}",
        "run-code",
        "--filename",
        native_path(collector),
    ]
    process_options = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    process = subprocess.Popen(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        errors="replace",
        **process_options,
    )
    process_done = threading.Event()
    process_output: dict[str, str] = {}

    def collect_process_output() -> None:
        stdout, stderr = process.communicate()
        process_output["stdout"] = stdout
        process_output["stderr"] = stderr
        process_done.set()
        sink.wake_waiters()

    output_thread = threading.Thread(target=collect_process_output, daemon=True)
    output_thread.start()
    progress_sequence = 0
    last_reported_pages = checkpoint["total_pages"]
    progress_step = args.progress_pages
    last_reported_at = time.monotonic()
    try:
        while True:
            progress_sequence, progress = sink.wait_for_progress(
                progress_sequence, process_done
            )
            if (not getattr(args, "archive", False) and progress
                    and progress["pages"] > last_reported_pages
                    and (progress["pages"] - last_reported_pages >= progress_step
                         or (args.progress_seconds and time.monotonic() - last_reported_at >= args.progress_seconds)
                         or sink.result() is not None)):
                oldest = progress.get("oldest", 0)
                oldest_label = " / 帖子最后日期：暂无"
                if oldest:
                    oldest_label = f" / 帖子最后日期（最旧）：{datetime.fromtimestamp(oldest, SHANGHAI):%Y-%m-%d %H:%M}"
                print(
                    f"进度：API 第 {progress['page']} 页"
                    f" / 累计 {progress['pages']} 页"
                    f" / {progress['scanned']:,} 条"
                    f" / 本次命中 {progress['matched']} 条"
                    f"{oldest_label}",
                    flush=True,
                )
                last_reported_pages = progress["pages"]
                last_reported_at = time.monotonic()
            if process_done.is_set():
                break
    except BaseException:
        sink.cancel()
        # Let the browser abort fetches and timers before terminating the CLI tree.
        process_done.wait(timeout=0.5)
        stop_collector_process(process)
        output_thread.join(timeout=1)
        raise

    output_thread.join()
    stdout = process_output.get("stdout", "")
    stderr = process_output.get("stderr", "")
    if process.returncode != 0:
        raise CliError((stdout + "\n" + stderr).strip()[-3000:])
    data = sink.result()
    if data is None:
        message = (stdout + "\n" + stderr).strip()[-2000:]
        raise CliError(message or "Collector exited without a terminal result.")
    return data


def validate_progress_arguments(args: argparse.Namespace) -> None:
    if args.progress_pages < 1:
        raise CliError('--progress-pages must be positive.')
    if args.progress_seconds != 0 and args.progress_seconds < 10:
        raise CliError('--progress-seconds must be 0 (disabled) or at least 10.')


def run_digest(args: argparse.Namespace, standalone: bool = False) -> None:
    resolve_thresholds(args)
    validate_progress_arguments(args)
    if args.checkpoint_pages <= 0 or args.checkpoint_pages > 500:
        raise CliError("--checkpoint-pages must be between 1 and 500.")
    if args.cache_chunk_pages <= 0 or args.cache_chunk_pages > 20:
        raise CliError("--cache-chunk-pages must be between 1 and 20.")
    if args.max_total_pages is not None and args.max_total_pages <= 0:
        raise CliError("--max-total-pages must be positive when specified.")
    if args.concurrency <= 0 or args.concurrency > 8:
        raise CliError("--concurrency must be between 1 and 8.")
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
            )
            return

        if checkpoint is None:
            cache_base = (
                None
                if args.fresh
                else cache.find_prefix(
                    start_ts, end_ts, require_favorites=args.min_favorites is not None
                )
            )
            scan_start_ts = cache_base["end_timestamp"] if cache_base else start_ts
            checkpoint = new_checkpoint(
                spec,
                start_ts,
                end_ts,
                scan_start_ts,
                window_label,
                cache_reused=bool(cache_base),
                favorites_complete=(
                    bool(cache_base["favorites_complete"]) if cache_base else True
                ),
            )
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
            browser = ensure_standalone_login(args)
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
        server = SinkServer(sink)
        try:
            result = run_persistent_collector(browser, args, checkpoint, sink, server.url)
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
        finally:
            server.close()

        checkpoint["reached_start"] = bool(result["reached_start"])
        checkpoint["feed_exhausted"] = bool(result.get("feed_exhausted", False))
        checkpoint["completed"] = checkpoint["reached_start"] or checkpoint["feed_exhausted"]
        checkpoint["updated_at"] = datetime.now(SHANGHAI).isoformat()
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
        checkpoint["completed_at"] = checkpoint["updated_at"]
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
                    "cache_integrity": cache.integrity_check(),
                    "telemetry": checkpoint["telemetry"],
                },
                ensure_ascii=False,
            )
        )
    finally:
        cache.close()


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
                run_archive(args, sys.modules[__name__])
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
