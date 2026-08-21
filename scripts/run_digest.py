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
import time
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, time as dt_time, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from zoneinfo import ZoneInfo


SITE_URL = "https://treehole.pku.edu.cn/ch/web/pc/index"
SITE_ORIGIN = "https://treehole.pku.edu.cn"
CONFIG_KEY = "codex_pku_digest_config"
SHANGHAI = ZoneInfo("Asia/Shanghai")
CACHE_SCHEMA_VERSION = 5
CHECKPOINT_SCHEMA_VERSION = 4
SINK_SCHEMA_VERSION = 2
TELEMETRY_FIELDS = (
    "list_requests",
    "detail_requests",
    "request_ms",
    "pacing_ms",
    "retry_backoff_ms",
    "response_chars",
    "cache_write_ms",
    "wall_ms",
    "throttle_responses",
    "concurrency_reductions",
    "max_in_flight",
    "overfetch_pages",
)
TELEMETRY_MAX_FIELDS = {"max_in_flight"}


class CliError(RuntimeError):
    pass


def codex_base() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))


def find_pwcli() -> Path:
    override = os.environ.get("PWCLI")
    local_wrapper = Path(__file__).with_name("playwright_cli.sh")
    codex_wrapper = codex_base() / "skills/playwright/scripts/playwright_cli.sh"
    candidates = [Path(override)] if override else [local_wrapper, codex_wrapper]
    path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if path is None:
        checked = ", ".join(str(candidate) for candidate in candidates)
        raise CliError(f"Playwright wrapper not found. Checked: {checked}")
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
    def __init__(self, session: str, headed: bool = True):
        self.pwcli = find_pwcli()
        self.session = session
        self.headed = headed

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
    return args.since is None and args.until is None


def should_reuse_checkpoint(args: argparse.Namespace, checkpoint: dict) -> bool:
    """Resume unfinished work, but never let a completed run freeze a rolling window."""
    return not checkpoint.get("completed", False) or not is_rolling_window(args)


def empty_telemetry() -> dict[str, int]:
    return {field: 0 for field in TELEMETRY_FIELDS}


def merge_telemetry(target: dict, update: dict) -> None:
    for field in TELEMETRY_FIELDS:
        raw_value = update.get(field, 0)
        if isinstance(raw_value, bool):
            raise CliError("Collector returned invalid telemetry.")
        try:
            value = int(raw_value)
        except (TypeError, ValueError) as error:
            raise CliError("Collector returned invalid telemetry.") from error
        if value < 0:
            raise CliError("Collector returned invalid telemetry.")
        if field in TELEMETRY_MAX_FIELDS:
            target[field] = max(int(target.get(field, 0)), value)
        else:
            target[field] = int(target.get(field, 0)) + value


def default_checkpoint_path(spec: dict) -> Path:
    fingerprint = hashlib.sha256(
        json.dumps(spec, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return Path.cwd() / "output/playwright/holeclaw-checkpoints-v4" / f"{fingerprint}.json"


def default_cache_path() -> Path:
    return Path.cwd() / "output/playwright/holeclaw-cache-v5.sqlite3"


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
    cache_reused: bool = False,
    favorites_complete: bool = True,
) -> dict:
    now = datetime.now(SHANGHAI).isoformat()
    return {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "request": spec,
        "start_timestamp": start_ts,
        "end_timestamp": end_ts,
        "scan_start_timestamp": scan_start_ts,
        "window_label": window_label,
        "cache_reused": cache_reused,
        "next_page": 1,
        "total_pages": 0,
        "total_scanned": 0,
        "matched_by_pid": {},
        "telemetry": empty_telemetry(),
        "favorites_complete": favorites_complete,
        "reached_start": False,
        "feed_exhausted": False,
        "completed": False,
        "created_at": now,
        "updated_at": now,
    }


def read_checkpoint(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CliError(f"Cannot read checkpoint {path}: {error}") from error


def load_checkpoint(path: Path, spec: dict) -> dict:
    checkpoint = read_checkpoint(path)
    if (
        checkpoint.get("schema_version") != CHECKPOINT_SCHEMA_VERSION
        or checkpoint.get("request") != spec
    ):
        raise CliError(
            f"Checkpoint is incompatible or parameters do not match: {path}. "
            "Use a new checkpoint path."
        )
    return checkpoint


class CacheStore:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                inspection = sqlite3.connect(
                    f"file:{self.path}?mode=ro", uri=True, timeout=5
                )
                try:
                    schema_row = inspection.execute(
                        "SELECT value FROM metadata WHERE key='schema_version'"
                    ).fetchone()
                finally:
                    inspection.close()
                schema_version = int(schema_row[0]) if schema_row else 0
            except (sqlite3.Error, TypeError, ValueError) as error:
                raise CliError(
                    f"Cache is incompatible with schema v{CACHE_SCHEMA_VERSION}: {self.path}. "
                    "Use a new cache path."
                ) from error
            if schema_version != CACHE_SCHEMA_VERSION:
                raise CliError(
                    f"Cache schema v{schema_version} is incompatible with schema "
                    f"v{CACHE_SCHEMA_VERSION}: {self.path}. Use a new cache path."
                )
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
                    favorites INTEGER,
                    type TEXT NOT NULL,
                    text TEXT NOT NULL,
                    observed_at INTEGER NOT NULL
                )
                """
            )
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS posts_window_idx "
                "ON posts(timestamp DESC, pid DESC)"
            )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS coverage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    start_timestamp INTEGER NOT NULL,
                    end_timestamp INTEGER NOT NULL,
                    completed_at TEXT NOT NULL,
                    source_pages INTEGER NOT NULL,
                    source_scanned INTEGER NOT NULL,
                    favorites_complete INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS favorite_unavailable (
                    pid TEXT PRIMARY KEY,
                    observed_at INTEGER NOT NULL,
                    reason TEXT NOT NULL
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

    @contextmanager
    def transaction(self):
        """Serialize a cache chunk and commit it as one SQLite transaction."""
        with self.lock:
            try:
                yield
            except Exception:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    def upsert_posts(self, rows: list[dict], *, commit: bool = True) -> None:
        if not rows:
            return
        observed_at = int(datetime.now(SHANGHAI).timestamp())
        values = []
        for row in rows:
            pid = str(row.get("pid", ""))
            timestamp = int(row.get("timestamp", 0))
            reply = int(row.get("reply", 0))
            raw_favorites = row.get("favorites")
            favorites = None if raw_favorites is None else int(raw_favorites)
            if (
                not pid
                or timestamp <= 0
                or reply < 0
                or (favorites is not None and favorites < 0)
            ):
                raise CliError("Collector returned an invalid cache row.")
            values.append(
                (
                    pid,
                    timestamp,
                    reply,
                    favorites,
                    str(row.get("type") or "text"),
                    str(row.get("text") or ""),
                    observed_at,
                )
            )
        with self.lock:
            self.connection.executemany(
                """
                INSERT INTO posts(
                    pid, timestamp, reply, favorites, type, text, observed_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(pid) DO UPDATE SET
                    timestamp=excluded.timestamp,
                    reply=excluded.reply,
                    favorites=CASE
                        WHEN excluded.favorites IS NOT NULL THEN excluded.favorites
                        ELSE posts.favorites
                    END,
                    type=excluded.type,
                    text=CASE WHEN excluded.text <> '' THEN excluded.text ELSE posts.text END,
                    observed_at=excluded.observed_at
                """,
                values,
            )
            input_pids = [value[0] for value in values]
            if input_pids:
                self.connection.executemany(
                    """
                    DELETE FROM favorite_unavailable
                    WHERE pid = ?
                      AND EXISTS (
                          SELECT 1 FROM posts
                          WHERE posts.pid = ? AND posts.favorites IS NOT NULL
                      )
                    """,
                    ((pid, pid) for pid in input_pids),
                )
            if commit:
                self.connection.commit()

    def record_favorite_unavailable(
        self, rows: list[dict], *, commit: bool = True
    ) -> None:
        if not rows:
            return
        observed_at = int(datetime.now(SHANGHAI).timestamp())
        values = []
        for row in rows:
            pid = str(row.get("pid", ""))
            reason = str(row.get("reason") or "detail_missing")
            if not pid:
                raise CliError("Collector returned an invalid unavailable favorite PID.")
            values.append((pid, observed_at, reason[:120]))
        with self.lock:
            self.connection.executemany(
                """
                INSERT INTO favorite_unavailable(pid, observed_at, reason)
                VALUES(?, ?, ?)
                ON CONFLICT(pid) DO UPDATE SET
                    observed_at=excluded.observed_at,
                    reason=excluded.reason
                """,
                values,
            )
            if commit:
                self.connection.commit()

    def query_posts(
        self,
        start_ts: int,
        end_ts: int,
        min_comments: int | None,
        min_favorites: int | None,
        match_mode: str = "all",
    ) -> list[dict]:
        filters = ["timestamp >= ?", "timestamp < ?"]
        parameters: list[int] = [start_ts, end_ts]
        engagement_filters = []
        if min_comments is not None:
            engagement_filters.append("reply > ?")
            parameters.append(min_comments)
        if min_favorites is not None:
            engagement_filters.append("favorites > ?")
            parameters.append(min_favorites)
        if engagement_filters:
            joiner = " OR " if match_mode == "any" else " AND "
            filters.append(f"({joiner.join(engagement_filters)})")
        with self.lock:
            rows = self.connection.execute(
                f"""
                SELECT pid, timestamp, reply, favorites, type, text
                FROM posts
                WHERE {' AND '.join(filters)}
                ORDER BY timestamp DESC, pid DESC
                """,
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def query_favorite_unavailable(self, start_ts: int, end_ts: int) -> list[dict]:
        with self.lock:
            rows = self.connection.execute(
                """
                SELECT p.pid, p.timestamp, p.reply, p.type, p.text,
                       u.observed_at, u.reason
                FROM favorite_unavailable AS u
                JOIN posts AS p ON p.pid = u.pid
                WHERE p.timestamp >= ? AND p.timestamp < ?
                ORDER BY p.timestamp DESC, p.pid DESC
                """,
                (start_ts, end_ts),
            ).fetchall()
        return [dict(row) for row in rows]

    def find_covering(
        self, start_ts: int, end_ts: int, require_favorites: bool = False
    ) -> dict | None:
        favorites_clause = "AND favorites_complete = 1" if require_favorites else ""
        with self.lock:
            row = self.connection.execute(
                f"""
                SELECT * FROM coverage
                WHERE start_timestamp <= ? AND end_timestamp >= ?
                {favorites_clause}
                ORDER BY end_timestamp DESC, completed_at DESC
                LIMIT 1
                """,
                (start_ts, end_ts),
            ).fetchone()
        return dict(row) if row else None

    def find_prefix(
        self, start_ts: int, end_ts: int, require_favorites: bool = False
    ) -> dict | None:
        favorites_clause = "AND favorites_complete = 1" if require_favorites else ""
        with self.lock:
            row = self.connection.execute(
                f"""
                SELECT * FROM coverage
                WHERE start_timestamp <= ? AND end_timestamp > ? AND end_timestamp < ?
                {favorites_clause}
                ORDER BY end_timestamp DESC, completed_at DESC
                LIMIT 1
                """,
                (start_ts, start_ts, end_ts),
            ).fetchone()
        return dict(row) if row else None

    def add_coverage(
        self,
        start_ts: int,
        end_ts: int,
        completed_at: str,
        pages: int,
        scanned: int,
        favorites_complete: bool,
    ) -> None:
        with self.lock:
            self.connection.execute(
                """
                INSERT INTO coverage(
                    start_timestamp, end_timestamp, completed_at, source_pages, source_scanned,
                    favorites_complete
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (start_ts, end_ts, completed_at, pages, scanned, int(favorites_complete)),
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
        min_comments: int | None,
        min_favorites: int | None,
        match_mode: str = "all",
    ):
        self.cache = cache
        self.checkpoint = checkpoint
        self.checkpoint_path = checkpoint_path
        self.min_comments = min_comments
        self.min_favorites = min_favorites
        self.match_mode = match_mode
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.last_progress: dict | None = None
        self.progress_sequence = 0
        self.terminal_result: dict | None = None

    def ingest(self, payload: dict) -> None:
        if payload.get("schema_version") != SINK_SCHEMA_VERSION:
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
            matched_pids = payload.get("matched_pids") or []
            unavailable = payload.get("favorite_unavailable") or []
            if not isinstance(matched_pids, list) or not all(
                isinstance(pid, str) and pid for pid in matched_pids
            ):
                raise CliError("Collector returned invalid matched PIDs.")
            if len(rows) != scanned:
                raise CliError("Collector cache row count does not match scanned count.")
            row_pids = {str(row.get("pid", "")) for row in rows}
            match_pids = {str(pid) for pid in matched_pids}
            unavailable_pids = {str(row.get("pid", "")) for row in unavailable}
            if len(match_pids) != len(matched_pids):
                raise CliError("Collector returned duplicate matched PIDs.")
            if "" in unavailable_pids or not unavailable_pids.issubset(row_pids):
                raise CliError("Collector returned invalid unavailable favorite metadata.")
            if "" in match_pids or not match_pids.issubset(row_pids):
                raise CliError("Collector returned matches outside its cache rows.")
            missing_favorite_pids = {
                str(row.get("pid", "")) for row in rows if row.get("favorites") is None
            }
            if not unavailable_pids.issubset(missing_favorite_pids):
                raise CliError("Collector marked a known favorite count as unavailable.")
            if self.min_favorites is not None and not missing_favorite_pids.issubset(
                unavailable_pids
            ):
                raise CliError("Collector omitted favorite counts or availability metadata.")

            report_start = self.checkpoint["start_timestamp"]
            report_end = self.checkpoint["end_timestamp"]
            validated_match_pids = []
            rows_by_pid = {str(row.get("pid", "")): row for row in rows}
            for pid in matched_pids:
                post = rows_by_pid[str(pid)]
                timestamp = int(post.get("timestamp", 0))
                reply = int(post.get("reply", 0))
                raw_favorites = post.get("favorites")
                favorites = None if raw_favorites is None else int(raw_favorites)
                normalized_pid = str(pid)
                conditions = []
                if self.min_comments is not None:
                    conditions.append(reply > self.min_comments)
                if self.min_favorites is not None:
                    conditions.append(
                        favorites is not None and favorites > self.min_favorites
                    )
                engagement_match = (
                    any(conditions) if self.match_mode == "any" else all(conditions)
                )
                if (
                    not normalized_pid
                    or not (report_start <= timestamp < report_end)
                    or not engagement_match
                ):
                    raise CliError("Collector returned a post outside the requested filter.")
                validated_match_pids.append(normalized_pid)

            chunk_telemetry = empty_telemetry()
            merge_telemetry(chunk_telemetry, dict(payload.get("telemetry") or {}))
            cache_started = time.perf_counter()
            with self.cache.transaction():
                self.cache.record_favorite_unavailable(unavailable, commit=False)
                self.cache.upsert_posts(rows, commit=False)
            cache_write_ms = round((time.perf_counter() - cache_started) * 1000)
            chunk_telemetry["cache_write_ms"] = cache_write_ms
            merge_telemetry(self.checkpoint["telemetry"], chunk_telemetry)
            if missing_favorite_pids and self.min_favorites is None:
                self.checkpoint["favorites_complete"] = False
            for pid in validated_match_pids:
                self.checkpoint["matched_by_pid"][pid] = True

            self.checkpoint["next_page"] = end_page + 1
            self.checkpoint["total_pages"] += pages
            self.checkpoint["total_scanned"] += scanned
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


def report_profile(
    min_comments: int | None, min_favorites: int | None, match_mode: str
) -> tuple[str, str]:
    if min_comments is not None and min_favorites is not None and match_mode == "any":
        return "high-comments-or-favorites", "北大树洞高评论或高收藏帖报告"
    if min_comments is not None and min_favorites is not None:
        return "high-comments-and-favorites", "北大树洞高评论与高收藏帖报告"
    if min_favorites is not None:
        return "high-favorites", "北大树洞高收藏帖报告"
    return "high-comments", "北大树洞高评论帖报告"


def filter_description(
    min_comments: int | None, min_favorites: int | None, match_mode: str
) -> str:
    conditions = []
    if min_comments is not None:
        conditions.append(f"评论数 > {min_comments}")
    if min_favorites is not None:
        conditions.append(f"收藏数 > {min_favorites}")
    return (" 或 " if match_mode == "any" else " 且 ").join(conditions)


def cache_report_data(
    cache: CacheStore,
    start_ts: int,
    end_ts: int,
    min_comments: int | None,
    min_favorites: int | None,
    match_mode: str,
    collected_at: str,
    pages: int,
    scanned: int,
    cache_reused: bool,
) -> dict:
    candidates = cache.query_posts(
        start_ts, end_ts, min_comments, min_favorites, match_mode
    )
    return {
        "collected_at": collected_at,
        "start_timestamp": start_ts,
        "end_timestamp": end_ts,
        "min_comments": min_comments,
        "min_favorites": min_favorites,
        "match_mode": match_mode,
        "pages": pages,
        "scanned": scanned,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "cache_reused": cache_reused,
        "favorite_unavailable": cache.query_favorite_unavailable(start_ts, end_ts),
    }


def render_report(data: dict, output: Path, window_label: str) -> None:
    grouped = defaultdict(list)
    for post in data["candidates"]:
        local_time = datetime.fromtimestamp(int(post["timestamp"]), SHANGHAI)
        grouped[local_time.date().isoformat()].append((local_time, post))

    collected = datetime.fromisoformat(data["collected_at"].replace("Z", "+00:00")).astimezone(SHANGHAI)
    start = datetime.fromtimestamp(data["start_timestamp"], SHANGHAI)
    end = datetime.fromtimestamp(data["end_timestamp"], SHANGHAI)
    min_comments = data["min_comments"]
    min_favorites = data["min_favorites"]
    match_mode = data.get("match_mode", "all")
    _slug, title = report_profile(min_comments, min_favorites, match_mode)
    lines = [
        f"# {title}（{window_label}）",
        "",
        f"- 生成时间：{collected:%Y-%m-%d %H:%M:%S}（Asia/Shanghai）",
        f"- 时间范围：{start:%Y-%m-%d %H:%M:%S} 至 {end:%Y-%m-%d %H:%M:%S}",
    ]
    lines.extend(
        [
            f"- 筛选条件：{filter_description(min_comments, min_favorites, match_mode)}",
            f"- 本次网络扫描：{data['pages']} 页，{data['scanned']:,} 条帖子",
            f"- 命中：{data['candidate_count']} 条",
        ]
    )
    if data.get("cache_reused"):
        lines.append("- 数据来源：SQLite 本地缓存（必要的新时间段已增量扫描）")
    unavailable = data.get("favorite_unavailable") or []
    if min_favorites is not None and unavailable:
        unavailable_pids = "、".join(f"#{item['pid']}" for item in unavailable)
        lines.append(
            f"- 收藏数不可用：{len(unavailable)} 条（{unavailable_pids}）；"
            "不按收藏条件命中，但在 OR 模式下仍可按评论条件命中"
        )
    has_favorite_snapshots = min_favorites is not None or any(
        post.get("favorites") is not None for post in data["candidates"]
    )
    snapshot_note = (
        "评论数和收藏数为最近一次采集快照。"
        if has_favorite_snapshots
        else "评论数为最近一次采集快照。"
    )
    lines.extend(
        [
            "",
            f"> {snapshot_note}图片帖默认仅摘要文字说明，不对图片做 OCR。",
            "",
        ]
    )
    for day in sorted(grouped, reverse=True):
        lines.extend([f"## {day}", ""])
        for local_time, post in grouped[day]:
            summary = one_line_summary(post.get("text", ""), post.get("type", "text"))
            metrics = [f"{post['reply']} 条评论"]
            if post.get("favorites") is not None:
                metrics.append(f"{post['favorites']} 次收藏")
            lines.append(
                f"- **#{post['pid']}** · {' · '.join(metrics)} · "
                f"{local_time:%H:%M} — {summary}"
            )
        lines.append("")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def emit_cached_report(
    cache: CacheStore,
    output: Path,
    window_label: str,
    start_ts: int,
    end_ts: int,
    min_comments: int | None,
    min_favorites: int | None,
    match_mode: str,
    collected_at: str,
    pages: int,
    scanned: int,
    cache_reused: bool,
    marker: str,
) -> None:
    data = cache_report_data(
        cache,
        start_ts,
        end_ts,
        min_comments,
        min_favorites,
        match_mode,
        collected_at,
        pages,
        scanned,
        cache_reused,
    )
    render_report(data, output, window_label)
    summary = {
        "report": str(output),
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


def run_digest(args: argparse.Namespace, standalone: bool = False) -> None:
    resolve_thresholds(args)
    if args.checkpoint_pages <= 0 or args.checkpoint_pages > 500:
        raise CliError("--checkpoint-pages must be between 1 and 500.")
    if args.cache_chunk_pages <= 0 or args.cache_chunk_pages > 20:
        raise CliError("--cache-chunk-pages must be between 1 and 20.")
    if args.max_total_pages <= 0 or args.max_total_pages > 5000:
        raise CliError("--max-total-pages must be between 1 and 5000.")
    if args.concurrency <= 0 or args.concurrency > 4:
        raise CliError("--concurrency must be between 1 and 4.")
    if args.max_pages is not None:
        print("提示：--max-pages 已由常驻采集器取代；请使用 --max-total-pages。", flush=True)

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
        if checkpoint_path.exists() and not args.fresh:
            candidate = load_checkpoint(checkpoint_path, spec)
            if not should_reuse_checkpoint(args, candidate):
                print(
                    "已完成的滚动窗口检查点仅作为历史记录；"
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
                    coverage = cache.find_covering(
                        start_ts, end_ts, require_favorites=args.min_favorites is not None
                    )
                    emit_cached_report(
                        cache,
                        output,
                        window_label,
                        start_ts,
                        end_ts,
                        args.min_comments,
                        args.min_favorites,
                        args.match_mode,
                        coverage["completed_at"]
                        if coverage
                        else checkpoint.get("completed_at", checkpoint["updated_at"]),
                        0 if coverage else checkpoint["total_pages"],
                        0 if coverage else checkpoint["total_scanned"],
                        bool(coverage),
                        "reused_completed_checkpoint",
                    )
                    return
                print(
                    f"恢复检查点：从 API 第 {checkpoint['next_page']} 页继续，"
                    f"已累计 {checkpoint['total_pages']} 页 / "
                    f"{checkpoint['total_scanned']:,} 条。",
                    flush=True,
                )

        if checkpoint is None:
            covering = (
                None
                if args.fresh
                else cache.find_covering(
                    start_ts, end_ts, require_favorites=args.min_favorites is not None
                )
            )
            if covering:
                emit_cached_report(
                    cache,
                    output,
                    window_label,
                    start_ts,
                    end_ts,
                    args.min_comments,
                    args.min_favorites,
                    args.match_mode,
                    covering["completed_at"],
                    0,
                    0,
                    True,
                    "cache_hit",
                )
                return

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
            checkpoint["favorites_complete"],
        )
        data = cache_report_data(
            cache,
            start_ts,
            end_ts,
            args.min_comments,
            args.min_favorites,
            args.match_mode,
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


def add_digest_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--since", type=parse_date)
    parser.add_argument("--until", type=parse_date)
    parser.add_argument(
        "--min-comments", type=int, help="Require reply count to be strictly greater than N"
    )
    parser.add_argument(
        "--min-favorites", type=int, help="Require favorite count to be strictly greater than N"
    )
    parser.add_argument(
        "--match-mode",
        choices=("all", "any"),
        default="all",
        help="Require all thresholds (AND) or any threshold (OR)",
    )
    parser.add_argument("--max-pages", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--checkpoint-pages", type=int, default=500)
    parser.add_argument("--cache-chunk-pages", type=int, default=1)
    parser.add_argument(
        "--concurrency",
        type=int,
        default=2,
        help="Bounded Treehole request concurrency (1-4; default: 2)",
    )
    parser.add_argument("--max-total-pages", type=int, default=2000)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--output", type=Path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Low-frequency PKU Treehole digest collector")
    parser.add_argument("--session", default="pku-hole-digest")
    parser.add_argument("--state", type=Path, default=Path(".auth/pku-treehole.json"))
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("login-open")
    subparsers.add_parser("login-save")
    add_digest_arguments(subparsers.add_parser("run"))
    standalone_parser = subparsers.add_parser(
        "standalone",
        help="Run without AI assistance and handle interactive login when needed",
    )
    add_digest_arguments(standalone_parser)
    standalone_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Fail instead of waiting for login; suitable for cron after initial setup",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        if args.command == "login-open":
            login_open(args)
        elif args.command == "login-save":
            login_save(args)
        elif args.command == "standalone":
            run_standalone(args)
        else:
            run_digest(args)
    except CliError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
