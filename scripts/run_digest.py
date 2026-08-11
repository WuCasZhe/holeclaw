#!/usr/bin/env python3
import argparse
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
from collections import defaultdict
from datetime import date, datetime, time as dt_time, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from zoneinfo import ZoneInfo


SITE_URL = "https://treehole.pku.edu.cn/ch/web/pc/index"
SITE_ORIGIN = "https://treehole.pku.edu.cn"
CONFIG_KEY = "codex_pku_digest_config"
SHANGHAI = ZoneInfo("Asia/Shanghai")
CACHE_SCHEMA_VERSION = 1


class CliError(RuntimeError):
    pass


def codex_base() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))


def find_pwcli() -> Path:
    override = os.environ.get("PWCLI")
    path = Path(override) if override else codex_base() / "skills/playwright/scripts/playwright_cli.sh"
    if not path.is_file():
        raise CliError(f"Playwright wrapper not found: {path}")
    if subprocess.run(["bash", "-lc", "command -v npx >/dev/null 2>&1"]).returncode != 0:
        raise CliError("npx is required. Install Node.js/npm first.")
    return path


def native_path(path: Path) -> str:
    if Path("/proc/sys/fs/binfmt_misc/WSLInterop").exists():
        completed = subprocess.run(
            ["wslpath", "-w", str(path.resolve())], text=True, capture_output=True, check=True
        )
        return completed.stdout.strip()
    return str(path.resolve())


class BrowserCli:
    def __init__(self, session: str):
        self.pwcli = find_pwcli()
        self.session = session

    def run(self, *args: str, check: bool = True, raw: bool = False) -> subprocess.CompletedProcess:
        command = [str(self.pwcli), f"-s={self.session}"]
        if raw:
            command.append("--raw")
        command.extend(args)
        completed = subprocess.run(command, text=True, capture_output=True, errors="replace")
        if check and completed.returncode != 0:
            message = (completed.stdout + "\n" + completed.stderr).strip()
            raise CliError(message[-3000:])
        return completed

    def ensure_session(self) -> None:
        listing = subprocess.run(
            [str(self.pwcli), "list"], text=True, capture_output=True, errors="replace"
        )
        if self.session not in listing.stdout:
            self.run("open", "about:blank", "--headed")


def ensure_auth_ignored(state: Path) -> None:
    default_auth = Path.cwd() / ".auth"
    try:
        state.resolve().relative_to(default_auth.resolve())
    except ValueError:
        return
    ignore = Path.cwd() / ".gitignore"
    current = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
    lines = current.splitlines()
    if ".auth/" not in lines:
        suffix = "" if not current or current.endswith("\n") else "\n"
        ignore.write_text(current + suffix + ".auth/\n", encoding="utf-8")


def ensure_runtime_ignored(*paths: Path) -> None:
    runtime_root = (Path.cwd() / "output/playwright").resolve()
    if not any(
        path.resolve() == runtime_root or runtime_root in path.resolve().parents for path in paths
    ):
        return
    ignore = Path.cwd() / ".gitignore"
    current = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
    if "output/playwright/" not in current.splitlines():
        suffix = "" if not current or current.endswith("\n") else "\n"
        ignore.write_text(current + suffix + "output/playwright/\n", encoding="utf-8")


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
        end_day = args.until or now.date()
        end = datetime.combine(end_day + timedelta(days=1), dt_time.min, SHANGHAI) if args.until else now
        label = f"{start:%Y-%m-%d}至{(end - timedelta(seconds=1)):%Y-%m-%d}"
    else:
        if args.days <= 0:
            raise CliError("--days must be positive.")
        if args.until:
            end = datetime.combine(args.until + timedelta(days=1), dt_time.min, SHANGHAI)
        else:
            end = now
        start = end - timedelta(days=args.days)
        label = f"近{args.days}天"
    if start >= end:
        raise CliError("The start time must be before the end time.")
    if start >= now:
        raise CliError("The requested range is entirely in the future.")
    return int(start.timestamp()), int(min(end, now).timestamp()), label


def window_spec(args: argparse.Namespace) -> dict:
    return {
        "days": None if args.since else args.days,
        "since": args.since.isoformat() if args.since else None,
        "until": args.until.isoformat() if args.until else None,
        "min_comments": args.min_comments,
    }


def default_checkpoint_path(spec: dict) -> Path:
    fingerprint = hashlib.sha256(
        json.dumps(spec, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return Path.cwd() / "output/playwright/holeclaw-checkpoints" / f"{fingerprint}.json"


def default_cache_path() -> Path:
    return Path.cwd() / "output/playwright/holeclaw-cache.sqlite3"


def write_checkpoint(path: Path, checkpoint: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(checkpoint, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.chmod(0o600)
    os.replace(temporary, path)
    path.chmod(0o600)


def new_checkpoint(
    spec: dict,
    start_ts: int,
    end_ts: int,
    scan_start_ts: int,
    window_label: str,
    checkpoint_pages: int,
    cache_base: dict | None,
) -> dict:
    now = datetime.now(SHANGHAI).isoformat()
    return {
        "schema_version": 2,
        "request": spec,
        "start_timestamp": start_ts,
        "end_timestamp": end_ts,
        "scan_start_timestamp": scan_start_ts,
        "window_label": window_label,
        "checkpoint_pages": checkpoint_pages,
        "cache_base": cache_base,
        "next_page": 1,
        "total_pages": 0,
        "total_scanned": 0,
        "details_requested": 0,
        "matched_by_pid": {},
        "reached_start": False,
        "feed_exhausted": False,
        "completed": False,
        "created_at": now,
        "updated_at": now,
    }


def load_checkpoint(path: Path, spec: dict) -> dict:
    try:
        checkpoint = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CliError(f"Cannot read checkpoint {path}: {error}") from error
    if checkpoint.get("schema_version") not in (1, 2) or checkpoint.get("request") != spec:
        raise CliError(
            f"Checkpoint parameters do not match: {path}. Use --fresh to start over."
        )
    checkpoint.setdefault("scan_start_timestamp", checkpoint["start_timestamp"])
    checkpoint.setdefault("cache_base", None)
    return checkpoint


class CacheStore:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.connection.row_factory = sqlite3.Row
        with self.lock:
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=NORMAL")
            self.connection.execute("PRAGMA temp_store=MEMORY")
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS posts (
                    pid TEXT PRIMARY KEY,
                    timestamp INTEGER NOT NULL,
                    reply INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    text TEXT NOT NULL,
                    observed_at INTEGER NOT NULL,
                    source_page INTEGER NOT NULL
                )
                """
            )
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS posts_timestamp_idx ON posts(timestamp)"
            )
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS posts_reply_idx ON posts(reply)"
            )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS coverage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    start_timestamp INTEGER NOT NULL,
                    end_timestamp INTEGER NOT NULL,
                    completed_at TEXT NOT NULL,
                    source_pages INTEGER NOT NULL,
                    source_scanned INTEGER NOT NULL
                )
                """
            )
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS coverage_window_idx "
                "ON coverage(start_timestamp, end_timestamp)"
            )
            self.connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
                (str(CACHE_SCHEMA_VERSION),),
            )
            instance_row = self.connection.execute(
                "SELECT value FROM metadata WHERE key='instance_id'"
            ).fetchone()
            if instance_row:
                self.instance_id = str(instance_row[0])
            else:
                self.instance_id = secrets.token_hex(16)
                self.connection.execute(
                    "INSERT INTO metadata(key, value) VALUES('instance_id', ?)",
                    (self.instance_id,),
                )
            self.connection.commit()
        self.path.chmod(0o600)

    def close(self) -> None:
        with self.lock:
            self.connection.commit()
            self.connection.close()
        self.path.chmod(0o600)

    def upsert_posts(self, rows: list[dict]) -> None:
        if not rows:
            return
        observed_at = int(datetime.now(SHANGHAI).timestamp())
        values = []
        for row in rows:
            pid = str(row.get("pid", ""))
            timestamp = int(row.get("timestamp", 0))
            reply = int(row.get("reply", 0))
            source_page = int(row.get("source_page", 0))
            if not pid or timestamp <= 0 or reply < 0 or source_page <= 0:
                raise CliError("Collector returned an invalid cache row.")
            values.append(
                (
                    pid,
                    timestamp,
                    reply,
                    str(row.get("type") or "text"),
                    str(row.get("text") or ""),
                    observed_at,
                    source_page,
                )
            )
        with self.lock:
            self.connection.executemany(
                """
                INSERT INTO posts(pid, timestamp, reply, type, text, observed_at, source_page)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(pid) DO UPDATE SET
                    timestamp=excluded.timestamp,
                    reply=excluded.reply,
                    type=excluded.type,
                    text=CASE WHEN excluded.text <> '' THEN excluded.text ELSE posts.text END,
                    observed_at=excluded.observed_at,
                    source_page=excluded.source_page
                """,
                values,
            )
            self.connection.commit()

    def query_posts(self, start_ts: int, end_ts: int, min_comments: int) -> list[dict]:
        with self.lock:
            rows = self.connection.execute(
                """
                SELECT pid, timestamp, reply, type, text
                FROM posts
                WHERE timestamp >= ? AND timestamp < ? AND reply > ?
                ORDER BY timestamp DESC, pid DESC
                """,
                (start_ts, end_ts, min_comments),
            ).fetchall()
        return [dict(row) for row in rows]

    def find_covering(self, start_ts: int, end_ts: int) -> dict | None:
        with self.lock:
            row = self.connection.execute(
                """
                SELECT * FROM coverage
                WHERE start_timestamp <= ? AND end_timestamp >= ?
                ORDER BY end_timestamp DESC, completed_at DESC
                LIMIT 1
                """,
                (start_ts, end_ts),
            ).fetchone()
        return dict(row) if row else None

    def find_prefix(self, start_ts: int, end_ts: int) -> dict | None:
        with self.lock:
            row = self.connection.execute(
                """
                SELECT * FROM coverage
                WHERE start_timestamp <= ? AND end_timestamp > ? AND end_timestamp < ?
                ORDER BY end_timestamp DESC, completed_at DESC
                LIMIT 1
                """,
                (start_ts, start_ts, end_ts),
            ).fetchone()
        return dict(row) if row else None

    def add_coverage(
        self, start_ts: int, end_ts: int, completed_at: str, pages: int, scanned: int
    ) -> None:
        with self.lock:
            self.connection.execute(
                """
                INSERT INTO coverage(
                    start_timestamp, end_timestamp, completed_at, source_pages, source_scanned
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (start_ts, end_ts, completed_at, pages, scanned),
            )
            self.connection.commit()

    def integrity_check(self) -> str:
        with self.lock:
            return str(self.connection.execute("PRAGMA integrity_check").fetchone()[0])

    def post_count(self) -> int:
        with self.lock:
            return int(self.connection.execute("SELECT COUNT(*) FROM posts").fetchone()[0])


class RunSink:
    def __init__(
        self,
        cache: CacheStore,
        checkpoint: dict,
        checkpoint_path: Path,
        min_comments: int,
    ):
        self.cache = cache
        self.checkpoint = checkpoint
        self.checkpoint_path = checkpoint_path
        self.min_comments = min_comments
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.last_progress: dict | None = None
        self.progress_sequence = 0
        self.terminal_result: dict | None = None

    def ingest(self, payload: dict) -> None:
        if payload.get("schema_version") != 1:
            raise CliError("Collector sink schema mismatch.")
        with self.lock:
            start_page = int(payload.get("start_page", 0))
            end_page = int(payload.get("end_page", 0))
            pages = int(payload.get("pages", 0))
            scanned = int(payload.get("scanned", 0))
            if (
                start_page != self.checkpoint["next_page"]
                or end_page < start_page
                or pages != end_page - start_page + 1
                or scanned < 0
            ):
                raise CliError("Collector returned a non-sequential cache chunk.")

            rows = payload.get("rows") or []
            matches = payload.get("matches") or []
            if len(rows) != scanned:
                raise CliError("Collector cache row count does not match scanned count.")
            self.cache.upsert_posts(rows)

            report_start = self.checkpoint["start_timestamp"]
            report_end = self.checkpoint["end_timestamp"]
            for post in matches:
                timestamp = int(post.get("timestamp", 0))
                reply = int(post.get("reply", 0))
                pid = str(post.get("pid", ""))
                if (
                    not pid
                    or not (report_start <= timestamp < report_end)
                    or reply <= self.min_comments
                ):
                    raise CliError("Collector returned a post outside the requested filter.")
                self.checkpoint["matched_by_pid"][pid] = {
                    "pid": pid,
                    "timestamp": timestamp,
                    "reply": reply,
                    "type": str(post.get("type") or "text"),
                    "text": str(post.get("text") or ""),
                }

            self.checkpoint["next_page"] = end_page + 1
            self.checkpoint["total_pages"] += pages
            self.checkpoint["total_scanned"] += scanned
            self.checkpoint["details_requested"] += int(payload.get("details_requested", 0))
            self.checkpoint["reached_start"] = bool(payload.get("reached_start"))
            self.checkpoint["feed_exhausted"] = bool(payload.get("feed_exhausted"))
            self.checkpoint["updated_at"] = datetime.now(SHANGHAI).isoformat()
            self.last_progress = {
                "page": end_page,
                "pages": self.checkpoint["total_pages"],
                "scanned": self.checkpoint["total_scanned"],
                "matched": len(self.checkpoint["matched_by_pid"]),
                "oldest": int(payload.get("oldest", 0)),
            }
            terminal_result = payload.get("result")
            if payload.get("terminal"):
                if not isinstance(terminal_result, dict):
                    raise CliError("Collector terminal chunk is missing its result.")
                if (
                    int(terminal_result.get("batch_end_page", 0)) != end_page
                    or int(terminal_result.get("next_page", 0)) != end_page + 1
                    or int(terminal_result.get("pages", 0)) <= 0
                    or int(terminal_result.get("scanned", -1)) < scanned
                    or bool(terminal_result.get("reached_start"))
                    != bool(payload.get("reached_start"))
                    or bool(terminal_result.get("feed_exhausted"))
                    != bool(payload.get("feed_exhausted"))
                ):
                    raise CliError("Collector terminal result does not match its cache chunk.")
                self.terminal_result = dict(terminal_result)
            elif terminal_result is not None:
                raise CliError("Collector returned a result before the terminal chunk.")
            if payload.get("checkpoint") or payload.get("terminal"):
                write_checkpoint(self.checkpoint_path, self.checkpoint)
            self.progress_sequence += 1
            self.condition.notify_all()

    def flush(self) -> None:
        with self.lock:
            write_checkpoint(self.checkpoint_path, self.checkpoint)

    def wait_for_progress(
        self, after_sequence: int, process_done: threading.Event
    ) -> tuple[int, dict | None]:
        with self.condition:
            self.condition.wait_for(
                lambda: self.progress_sequence > after_sequence or process_done.is_set()
            )
            progress = dict(self.last_progress) if self.last_progress else None
            return self.progress_sequence, progress

    def wake_waiters(self) -> None:
        with self.condition:
            self.condition.notify_all()

    def result(self) -> dict | None:
        with self.lock:
            return dict(self.terminal_result) if self.terminal_result else None


class SinkServer:
    def __init__(self, sink: RunSink):
        token = secrets.token_urlsafe(24)
        sink_ref = sink

        class Handler(BaseHTTPRequestHandler):
            def _cors(self) -> None:
                self.send_header("Access-Control-Allow-Origin", SITE_ORIGIN)
                self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "content-type")
                self.send_header("Access-Control-Allow-Private-Network", "true")
                self.send_header("Access-Control-Max-Age", "3600")

            def do_OPTIONS(self) -> None:
                self.send_response(204)
                self._cors()
                self.end_headers()

            def do_POST(self) -> None:
                parsed = urlparse(self.path)
                supplied = parse_qs(parsed.query).get("token", [""])[0]
                origin = self.headers.get("Origin", "")
                if (
                    parsed.path != "/ingest"
                    or not hmac.compare_digest(supplied, token)
                    or origin != SITE_ORIGIN
                ):
                    self.send_response(403)
                    self._cors()
                    self.end_headers()
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > 64 * 1024 * 1024:
                        raise CliError("Invalid local cache payload size.")
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    sink_ref.ingest(payload)
                    body = b'{"ok":true}'
                    self.send_response(200)
                except Exception as error:
                    body = json.dumps(
                        {"ok": False, "error": str(error)[:500]}, ensure_ascii=False
                    ).encode("utf-8")
                    self.send_response(500)
                self._cors()
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{port}/ingest?{urlencode({'token': token})}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def one_line_summary(text: str, post_type: str) -> str:
    original = re.sub(r"\s+", " ", html.unescape(text or "")).strip()
    value = re.sub(r"https?://\S+", "[链接]", original).replace("|", "｜")
    if not value:
        value = "帖子没有文字说明"
    if len(value) > 120:
        clauses = re.split(r"(?<=[。！？!?；;])", value)
        picked = ""
        for clause in clauses:
            if picked and len(picked) + len(clause) > 112:
                break
            picked += clause
            if len(picked) >= 45:
                break
        value = (picked or value[:112]).strip().rstrip("。！？!?；;") + "…"
    if post_type == "image":
        value = f"[图片帖] {value}"
    return value


def checkpoint_report_data(checkpoint: dict) -> dict:
    candidates = sorted(
        checkpoint["matched_by_pid"].values(), key=lambda post: post["timestamp"], reverse=True
    )
    return {
        "collected_at": checkpoint.get("completed_at") or checkpoint["updated_at"],
        "start_timestamp": checkpoint["start_timestamp"],
        "end_timestamp": checkpoint["end_timestamp"],
        "min_comments": checkpoint["request"]["min_comments"],
        "pages": checkpoint["total_pages"],
        "scanned": checkpoint["total_scanned"],
        "details_requested": checkpoint["details_requested"],
        "candidate_count": len(candidates),
        "candidates": candidates,
        "cache_reused": False,
    }


def cache_report_data(
    cache: CacheStore,
    start_ts: int,
    end_ts: int,
    min_comments: int,
    collected_at: str,
    pages: int,
    scanned: int,
    details_requested: int,
    cache_reused: bool,
) -> dict:
    candidates = cache.query_posts(start_ts, end_ts, min_comments)
    return {
        "collected_at": collected_at,
        "start_timestamp": start_ts,
        "end_timestamp": end_ts,
        "min_comments": min_comments,
        "pages": pages,
        "scanned": scanned,
        "details_requested": details_requested,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "cache_reused": cache_reused,
    }


def render_report(data: dict, output: Path, window_label: str) -> None:
    grouped = defaultdict(list)
    for post in data["candidates"]:
        local_time = datetime.fromtimestamp(int(post["timestamp"]), SHANGHAI)
        grouped[local_time.date().isoformat()].append((local_time, post))

    collected = datetime.fromisoformat(data["collected_at"].replace("Z", "+00:00")).astimezone(SHANGHAI)
    start = datetime.fromtimestamp(data["start_timestamp"], SHANGHAI)
    end = datetime.fromtimestamp(data["end_timestamp"], SHANGHAI)
    threshold = data["min_comments"]
    lines = [
        f"# 北大树洞高评论帖报告（{window_label}）",
        "",
        f"- 生成时间：{collected:%Y-%m-%d %H:%M:%S}（Asia/Shanghai）",
        f"- 时间范围：{start:%Y-%m-%d %H:%M:%S} 至 {end:%Y-%m-%d %H:%M:%S}",
        f"- 筛选条件：评论数 > {threshold}",
        f"- 本次网络扫描：{data['pages']} 页，{data['scanned']:,} 条帖子",
        f"- 命中：{data['candidate_count']} 条",
    ]
    if data.get("cache_reused"):
        lines.append("- 数据来源：SQLite 本地缓存（必要的新时间段已增量扫描）")
    lines.extend(
        [
            "",
            "> 评论数为最近一次采集快照。图片帖默认仅摘要文字说明，不对图片做 OCR。",
            "",
        ]
    )
    for day in sorted(grouped, reverse=True):
        lines.extend([f"## {day}", ""])
        for local_time, post in grouped[day]:
            summary = one_line_summary(post.get("text", ""), post.get("type", "text"))
            lines.append(
                f"- **#{post['pid']}** · {post['reply']} 条评论 · {local_time:%H:%M} — {summary}"
            )
        lines.append("")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def login_open(args: argparse.Namespace) -> None:
    browser = BrowserCli(args.session)
    browser.ensure_session()
    browser.run("goto", SITE_URL)
    print("可视浏览器已打开。请亲自完成北大统一身份认证，进入树洞首页后再保存登录状态。")


def login_save(args: argparse.Namespace) -> None:
    state = args.state.resolve()
    browser = BrowserCli(args.session)
    browser.ensure_session()
    snapshot = browser.run("snapshot").stdout
    if not authenticated(snapshot):
        raise CliError("当前页面仍未进入北大树洞首页，请先完成登录。")
    state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    browser.run("state-save", native_path(state))
    state.chmod(0o600)
    ensure_auth_ignored(state)
    print(f"登录状态已保存：{state}")


def run_persistent_collector(
    browser: BrowserCli,
    args: argparse.Namespace,
    checkpoint: dict,
    sink: RunSink,
    sink_url: str,
) -> dict:
    remaining = args.max_total_pages - checkpoint["total_pages"]
    if remaining <= 0:
        raise CliError(
            f"Reached --max-total-pages={args.max_total_pages} before the start time."
        )
    config = {
        "report_start_timestamp": checkpoint["start_timestamp"],
        "scan_start_timestamp": checkpoint["scan_start_timestamp"],
        "end_timestamp": checkpoint["end_timestamp"],
        "min_comments": args.min_comments,
        "start_page": checkpoint["next_page"],
        "page_size": 500,
        "max_pages": remaining,
        "pages_before": checkpoint["total_pages"],
        "checkpoint_pages": args.checkpoint_pages,
        "cache_chunk_pages": args.cache_chunk_pages,
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
    process = subprocess.Popen(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        errors="replace",
    )
    process_done = threading.Event()
    process_output: dict[str, str] = {}

    def collect_process_output() -> None:
        stdout, stderr = process.communicate()
        process_output["stdout"] = stdout
        process_output["stderr"] = stderr
        process_done.set()
        sink.wake_waiters()

    output_thread = threading.Thread(target=collect_process_output)
    output_thread.start()
    progress_sequence = 0
    last_reported_pages = checkpoint["total_pages"]
    progress_step = max(25, args.cache_chunk_pages)
    while True:
        progress_sequence, progress = sink.wait_for_progress(
            progress_sequence, process_done
        )
        if progress and progress["pages"] - last_reported_pages >= progress_step:
            oldest = progress.get("oldest", 0)
            oldest_label = ""
            if oldest:
                oldest_label = f" / 最旧 {datetime.fromtimestamp(oldest, SHANGHAI):%Y-%m-%d %H:%M}"
            print(
                f"进度：API 第 {progress['page']} 页"
                f" / 累计 {progress['pages']} 页"
                f" / {progress['scanned']:,} 条"
                f" / 本次命中 {progress['matched']} 条"
                f"{oldest_label}",
                flush=True,
            )
            last_reported_pages = progress["pages"]
        if process_done.is_set():
            break

    output_thread.join()
    stdout = process_output.get("stdout", "")
    stderr = process_output.get("stderr", "")
    if process.returncode != 0:
        sink.flush()
        raise CliError((stdout + "\n" + stderr).strip()[-3000:])
    data = sink.result()
    if data is None:
        sink.flush()
        message = (stdout + "\n" + stderr).strip()[-2000:]
        raise CliError(message or "Collector exited without a terminal result.")
    return data


def run_digest(args: argparse.Namespace) -> None:
    if args.min_comments < 0:
        raise CliError("--min-comments cannot be negative.")
    if args.checkpoint_pages <= 0 or args.checkpoint_pages > 500:
        raise CliError("--checkpoint-pages must be between 1 and 500.")
    if args.cache_chunk_pages <= 0 or args.cache_chunk_pages > 20:
        raise CliError("--cache-chunk-pages must be between 1 and 20.")
    if args.max_total_pages <= 0 or args.max_total_pages > 5000:
        raise CliError("--max-total-pages must be between 1 and 5000.")
    if args.max_pages is not None:
        print("提示：--max-pages 已由常驻采集器取代；请使用 --max-total-pages。", flush=True)

    spec = window_spec(args)
    start_ts, end_ts, window_label = time_window(args)
    checkpoint_path = (args.checkpoint or default_checkpoint_path(spec)).resolve()
    cache_path = (args.cache or default_cache_path()).resolve()
    ensure_runtime_ignored(checkpoint_path, cache_path)
    cache_existed = cache_path.exists()
    output = args.output or Path.cwd() / "reports" / f"pku-treehole-high-comments-{date.today().isoformat()}.md"
    output = output.resolve()
    cache = CacheStore(cache_path)
    try:
        checkpoint = None
        if checkpoint_path.exists() and not args.fresh:
            checkpoint = load_checkpoint(checkpoint_path, spec)
            start_ts = checkpoint["start_timestamp"]
            end_ts = checkpoint["end_timestamp"]
            window_label = checkpoint["window_label"]
            checkpoint["checkpoint_pages"] = args.checkpoint_pages
            if checkpoint["schema_version"] == 1 and not checkpoint["completed"]:
                print(
                    "检测到旧版未完成检查点；为建立完整 SQLite 缓存，"
                    "将保留冻结时间窗口并从 API 第 1 页重建。",
                    flush=True,
                )
                checkpoint = new_checkpoint(
                    spec,
                    start_ts,
                    end_ts,
                    start_ts,
                    window_label,
                    args.checkpoint_pages,
                    None,
                )
                checkpoint["cache_path"] = str(cache_path)
                checkpoint["cache_instance_id"] = cache.instance_id
                write_checkpoint(checkpoint_path, checkpoint)
            elif checkpoint["schema_version"] == 2:
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
                if recorded_instance and recorded_instance != cache.instance_id:
                    raise CliError(
                        "Checkpoint SQLite cache identity does not match. "
                        "Restore the original cache or use --fresh."
                    )
                if (
                    not recorded_instance
                    and checkpoint["total_scanned"] > 0
                    and cache.post_count() == 0
                ):
                    raise CliError(
                        "Checkpoint has progress but the SQLite cache is empty. "
                        "Restore the original cache or use --fresh."
                    )
                checkpoint["cache_path"] = str(cache_path)
                checkpoint["cache_instance_id"] = cache.instance_id
            if checkpoint["completed"]:
                coverage = cache.find_covering(start_ts, end_ts)
                if coverage:
                    data = cache_report_data(
                        cache,
                        start_ts,
                        end_ts,
                        args.min_comments,
                        coverage["completed_at"],
                        0,
                        0,
                        0,
                        True,
                    )
                else:
                    data = checkpoint_report_data(checkpoint)
                render_report(data, output, window_label)
                print(
                    json.dumps(
                        {
                            "report": str(output),
                            "cache": str(cache_path),
                            "pages": data["pages"],
                            "scanned": data["scanned"],
                            "matched": data["candidate_count"],
                            "reused_completed_checkpoint": True,
                        },
                        ensure_ascii=False,
                    )
                )
                return
            print(
                f"恢复检查点：从 API 第 {checkpoint['next_page']} 页继续，"
                f"已累计 {checkpoint['total_pages']} 页 / {checkpoint['total_scanned']:,} 条。",
                flush=True,
            )

        if checkpoint is None:
            covering = None if args.fresh else cache.find_covering(start_ts, end_ts)
            if covering:
                data = cache_report_data(
                    cache,
                    start_ts,
                    end_ts,
                    args.min_comments,
                    covering["completed_at"],
                    0,
                    0,
                    0,
                    True,
                )
                render_report(data, output, window_label)
                print(
                    json.dumps(
                        {
                            "report": str(output),
                            "cache": str(cache_path),
                            "pages": 0,
                            "scanned": 0,
                            "matched": data["candidate_count"],
                            "cache_hit": True,
                        },
                        ensure_ascii=False,
                    )
                )
                return

            cache_base = None if args.fresh else cache.find_prefix(start_ts, end_ts)
            scan_start_ts = cache_base["end_timestamp"] if cache_base else start_ts
            checkpoint = new_checkpoint(
                spec,
                start_ts,
                end_ts,
                scan_start_ts,
                window_label,
                args.checkpoint_pages,
                cache_base,
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

        state = args.state.resolve()
        if not state.is_file():
            raise CliError(f"Login state not found: {state}. Run login-open first.")

        browser = BrowserCli(args.session)
        browser.ensure_session()
        browser.run("goto", "about:blank")
        browser.run("state-load", native_path(state))
        browser.run("goto", SITE_URL)
        snapshot = browser.run("snapshot").stdout
        if not authenticated(snapshot):
            raise CliError("登录已失效或未成功加载，请重新执行 login-open 和 login-save。")

        print(
            f"开始常驻限速扫描：{window_label}，评论数 > {args.min_comments}，"
            f"单线程 0.6–2 秒间隔，默认每 {args.checkpoint_pages} 页保存检查点。",
            flush=True,
        )
        sink = RunSink(cache, checkpoint, checkpoint_path, args.min_comments)
        server = SinkServer(sink)
        try:
            result = run_persistent_collector(browser, args, checkpoint, sink, server.url)
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
            raise CliError(
                f"Reached --max-total-pages={args.max_total_pages} before the start time. "
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
        )
        data = cache_report_data(
            cache,
            start_ts,
            end_ts,
            args.min_comments,
            checkpoint["completed_at"],
            checkpoint["total_pages"],
            checkpoint["total_scanned"],
            checkpoint["details_requested"],
            bool(checkpoint.get("cache_base")),
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
                },
                ensure_ascii=False,
            )
        )
    finally:
        cache.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Low-frequency PKU Treehole digest collector")
    parser.add_argument("--session", default="pku-hole-digest")
    parser.add_argument("--state", type=Path, default=Path(".auth/pku-treehole.json"))
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("login-open")
    subparsers.add_parser("login-save")
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--days", type=int, default=30)
    run_parser.add_argument("--since", type=parse_date)
    run_parser.add_argument("--until", type=parse_date)
    run_parser.add_argument("--min-comments", type=int, default=50)
    run_parser.add_argument("--max-pages", type=int, help=argparse.SUPPRESS)
    run_parser.add_argument("--checkpoint-pages", type=int, default=500)
    run_parser.add_argument("--cache-chunk-pages", type=int, default=5)
    run_parser.add_argument("--max-total-pages", type=int, default=2000)
    run_parser.add_argument("--checkpoint", type=Path)
    run_parser.add_argument("--cache", type=Path)
    run_parser.add_argument("--fresh", action="store_true")
    run_parser.add_argument("--output", type=Path)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        if args.command == "login-open":
            login_open(args)
        elif args.command == "login-save":
            login_save(args)
        else:
            run_digest(args)
    except CliError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
