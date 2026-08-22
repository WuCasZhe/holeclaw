import hmac
import json
import secrets
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

try:
    from holeclaw_cache import CacheStore
    from holeclaw_checkpoint import empty_telemetry, merge_telemetry, write_checkpoint
    from holeclaw_domain import CliError, FilterSpec, SHANGHAI, SINK_SCHEMA_VERSION
except ModuleNotFoundError:
    from scripts.holeclaw_cache import CacheStore
    from scripts.holeclaw_checkpoint import (
        empty_telemetry,
        merge_telemetry,
        write_checkpoint,
    )
    from scripts.holeclaw_domain import (
        CliError,
        FilterSpec,
        SHANGHAI,
        SINK_SCHEMA_VERSION,
    )


SITE_ORIGIN = "https://treehole.pku.edu.cn"


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
        self.filter_spec = FilterSpec(min_comments, min_favorites, match_mode)
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
            if (
                self.filter_spec.min_favorites is not None
                and not missing_favorite_pids.issubset(unavailable_pids)
            ):
                raise CliError("Collector omitted favorite counts or availability metadata.")

            report_start = self.checkpoint["start_timestamp"]
            report_end = self.checkpoint["end_timestamp"]
            validated_match_pids = []
            rows_by_pid = {str(row.get("pid", "")): row for row in rows}
            for pid in matched_pids:
                post = rows_by_pid[pid]
                timestamp = int(post.get("timestamp", 0))
                reply = int(post.get("reply", 0))
                raw_favorites = post.get("favorites")
                favorites = None if raw_favorites is None else int(raw_favorites)
                if not (
                    report_start <= timestamp < report_end
                    and self.filter_spec.matches(reply, favorites)
                ):
                    raise CliError("Collector returned a post outside the requested filter.")
                validated_match_pids.append(pid)

            chunk_telemetry = empty_telemetry()
            merge_telemetry(chunk_telemetry, dict(payload.get("telemetry") or {}))
            cache_started = time.perf_counter()
            with self.cache.transaction():
                self.cache.record_favorite_unavailable(unavailable, commit=False)
                self.cache.upsert_posts(rows, commit=False)
            chunk_telemetry["cache_write_ms"] = round(
                (time.perf_counter() - cache_started) * 1000
            )
            merge_telemetry(self.checkpoint["telemetry"], chunk_telemetry)
            if missing_favorite_pids and self.filter_spec.min_favorites is None:
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
            if payload.get("terminal"):
                self.terminal_result = {
                    "reached_start": bool(payload.get("reached_start")),
                    "feed_exhausted": bool(payload.get("feed_exhausted")),
                }
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
