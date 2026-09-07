import argparse
import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable
from datetime import datetime
from pathlib import Path

try:
    from holeclaw_domain import CliError, SHANGHAI
    from holeclaw_sink import RunSink
except ModuleNotFoundError:
    from scripts.holeclaw_domain import CliError, SHANGHAI
    from scripts.holeclaw_sink import RunSink


try:
    from holeclaw_browser import CONFIG_KEY, BrowserCli, native_path, ensure_standalone_login
except ModuleNotFoundError:
    from scripts.holeclaw_browser import CONFIG_KEY, BrowserCli, native_path, ensure_standalone_login

try:
    from holeclaw_checkpoint import CollectionPosition
except ModuleNotFoundError:
    from scripts.holeclaw_checkpoint import CollectionPosition


@dataclass(frozen=True)
class CollectorServices:
    ensure_standalone_login: Callable[[argparse.Namespace], BrowserCli]
    run_persistent_collector: Callable[[BrowserCli, argparse.Namespace, dict, RunSink, str], dict]

    @classmethod
    def default(cls) -> "CollectorServices":
        return cls(ensure_standalone_login, run_persistent_collector)

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
    position = CollectionPosition.from_checkpoint(checkpoint)
    cached_remaining = position.remaining_cached_batches
    remaining = (cached_remaining if checkpoint.get("archive_cache_only") else
                 remaining_page_limit(args.max_total_pages, position.committed_remote_pages))
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
