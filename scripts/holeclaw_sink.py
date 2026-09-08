import hashlib
import hmac
import json
import secrets
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urlparse

try:
    from holeclaw_protocol import ReceiptBook, SinkMessage
    from holeclaw_cache import CacheStore
    from holeclaw_checkpoint import CheckpointState, empty_telemetry, merge_telemetry, write_checkpoint
    from holeclaw_domain import CliError, FilterSpec, SHANGHAI, SINK_SCHEMA_VERSION
except ModuleNotFoundError:
    from scripts.holeclaw_protocol import ReceiptBook, SinkMessage
    from scripts.holeclaw_cache import CacheStore
    from scripts.holeclaw_checkpoint import (
        CheckpointState,
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
        self.state = CheckpointState(checkpoint)
        self.checkpoint_path = checkpoint_path
        self.filter_spec = FilterSpec(min_comments, min_favorites, match_mode)
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.last_progress: dict | None = None
        self.progress_sequence = 0
        self.terminal_result: dict | None = None
        self.last_chunk_digest: bytes | None = None
        self.final_telemetry_digest: bytes | None = None
        self.cancel_event = threading.Event()
        self.receipts = ReceiptBook()

    def cancel(self) -> None:
        self.cancel_event.set()

    def record_post_date(self, timestamp: int) -> None:
        self.state.record_post_date(timestamp)

    def post_date_label(self) -> str:
        timestamp = self.checkpoint.get("oldest_post_timestamp", 0)
        return (f"帖子最后日期（最旧）：{datetime.fromtimestamp(timestamp, SHANGHAI):%Y-%m-%d %H:%M}"
                if timestamp else "帖子最后日期：暂无")

    def ingest(self, payload: dict, *, prepared_media=None) -> dict | None:
        message = SinkMessage.decode(payload, self.checkpoint['created_at'])
        with self.lock:
            if self.cancel_event.is_set():
                raise CliError('Collector cancelled.')
            found, receipt = self.receipts.lookup(message)
            if found:
                return receipt
            receipt = (self.dispatch(message.kind, message.payload) if prepared_media is None
                       else self.publish_media(message, prepared_media))
            self.receipts.remember(message, receipt)
            return receipt

    def ingest_media_stream(self, payload, stream, length):
        raise CliError('Image upload requires an archive sink.')

    def publish_media(self, message, prepared):
        raise CliError('Image upload requires an archive sink.')

    def dispatch(self, kind: str, payload: dict):
        if kind not in ('list_chunk', 'telemetry_final'):
            raise CliError('Archive message requires an archive sink.')
        return self.ingest_list(payload)

    def ingest_list(self, payload: dict) -> None:
        if self.cancel_event.is_set():
            raise CliError("Collector cancelled.")
        if payload.get("schema_version") != SINK_SCHEMA_VERSION:
            raise CliError("Collector sink schema mismatch.")
        with self.lock:
            if self.cancel_event.is_set():
                raise CliError("Collector cancelled.")
            start_page = int(payload.get("start_page", 0))
            end_page = int(payload.get("end_page", 0))
            pages = int(payload.get("pages", 0))
            scanned = int(payload.get("scanned", 0))
            digest = hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).digest()
            if payload.get('telemetry_final'):
                if self.terminal_result is None:
                    raise CliError('Final telemetry requires a committed terminal page.')
                if self.final_telemetry_digest is not None:
                    if digest != self.final_telemetry_digest:
                        raise CliError('Final telemetry receipt changed.')
                else:
                    delta = empty_telemetry()
                    merge_telemetry(delta, dict(payload.get('telemetry') or {}))
                    merge_telemetry(self.checkpoint['telemetry'], delta)
                    self.final_telemetry_digest = digest
                write_checkpoint(self.checkpoint_path, self.checkpoint)
                return
            # The browser sends one chunk at a time. Retain only the last receipt;
            # a lost HTTP response must not replay writes or progress counters.
            if digest == self.last_chunk_digest:
                if payload.get("checkpoint") or payload.get("terminal"):
                    write_checkpoint(self.checkpoint_path, self.checkpoint)
                return
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
            deferred = payload.get('favorite_deferred_pids') or []
            if not isinstance(deferred, list) or not all(isinstance(pid, str) and pid for pid in deferred):
                raise CliError('Invalid deferred favorite IDs.')
            deferred_pids = set(deferred)
            if len(deferred_pids) != len(deferred):
                raise CliError('Duplicate deferred favorite IDs.')
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
            rows_by_pid = {str(row.get('pid', '')): row for row in rows}
            missing_favorite_pids = {
                str(row.get("pid", "")) for row in rows_by_pid.values() if row.get("favorites") is None
            }
            report_start = self.checkpoint["start_timestamp"]
            report_end = self.checkpoint["end_timestamp"]
            missing_report_favorites = {
                str(row.get("pid", "")) for row in rows_by_pid.values()
                if row.get("favorites") is None
                and report_start <= int(row.get("timestamp", 0)) < report_end
            }
            if not unavailable_pids.issubset(missing_favorite_pids):
                raise CliError("Collector marked a known favorite count as unavailable.")
            if deferred_pids and (not deferred_pids.issubset(missing_report_favorites)
                    or deferred_pids & unavailable_pids
                    or self.filter_spec.match_mode != 'all'
                    or self.filter_spec.min_comments is None or self.filter_spec.min_favorites is None
                    or any(int(rows_by_pid[pid]['reply']) > self.filter_spec.min_comments for pid in deferred_pids)):
                raise CliError('Deferred favorite lookup could change the requested matches.')
            if (
                self.filter_spec.min_favorites is not None
                and not missing_report_favorites.issubset(unavailable_pids | deferred_pids)
            ):
                raise CliError("Collector omitted favorite counts or availability metadata.")

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
                self.cache.record_favorite_unavailable(unavailable)
                self.cache.upsert_posts(rows)
            chunk_telemetry["cache_write_ms"] = round(
                (time.perf_counter() - cache_started) * 1000
            )
            merge_telemetry(self.checkpoint["telemetry"], chunk_telemetry)
            if deferred_pids or (missing_report_favorites and self.filter_spec.min_favorites is None):
                self.checkpoint["favorites_complete"] = False
            self.state.commit_pages(dict(payload, end_page=end_page, pages=pages, scanned=scanned),
                                    validated_match_pids)
            self.last_progress = {
                "page": end_page,
                "pages": self.checkpoint["total_pages"],
                "scanned": self.checkpoint["total_scanned"],
                "matched": len(self.checkpoint["matched_by_pid"]),
                "oldest": self.checkpoint.get("oldest_post_timestamp", 0),
            }
            if payload.get("terminal"):
                self.terminal_result = {
                    "reached_start": bool(payload.get("reached_start")),
                    "feed_exhausted": bool(payload.get("feed_exhausted")),
                }
            self.last_chunk_digest = digest
            self.progress_sequence += 1
            self.condition.notify_all()
            if payload.get("checkpoint") or payload.get("terminal"):
                write_checkpoint(self.checkpoint_path, self.checkpoint)

    def flush(self) -> None:
        with self.lock:
            write_checkpoint(self.checkpoint_path, self.checkpoint)

    def wait_for_progress(
        self, after_sequence: int, process_done: threading.Event
    ) -> tuple[int, dict | None]:
        with self.condition:
            self.condition.wait_for(
                lambda: self.progress_sequence > after_sequence or process_done.is_set(),
                timeout=0.25,
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
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def __init__(self, sink: RunSink):
        self.server_cancel = sink.cancel
        token = secrets.token_urlsafe(24)
        sink_ref = sink
        upload_slots = threading.BoundedSemaphore(2)

        class Handler(BaseHTTPRequestHandler):
            def handle(self) -> None:
                try:
                    super().handle()
                except (ConnectionError, TimeoutError):
                    # Closing/aborting a browser callback is normal on Windows,
                    # including WinError 10053 during successful teardown.
                    pass

            def _cors(self) -> None:
                self.send_header("Access-Control-Allow-Origin", SITE_ORIGIN)
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "content-type, x-holeclaw-message")
                self.send_header("Access-Control-Allow-Private-Network", "true")
                self.send_header("Access-Control-Max-Age", "3600")

            def do_OPTIONS(self) -> None:
                self.send_response(204)
                self._cors()
                self.end_headers()

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                supplied = parse_qs(parsed.query).get("token", [""])[0]
                if (parsed.path != "/control" or not hmac.compare_digest(supplied, token)
                        or self.headers.get("Origin", "") != SITE_ORIGIN):
                    self.send_response(403)
                    self.end_headers()
                    return
                # One event-driven cancellation connection, no browser polling.
                sink_ref.cancel_event.wait()
                body = b'{"cancelled":true}'
                self.send_response(200)
                self._cors()
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_POST(self) -> None:
                parsed = urlparse(self.path)
                supplied = parse_qs(parsed.query).get("token", [""])[0]
                origin = self.headers.get("Origin", "")
                if (
                    parsed.path not in ("/ingest", "/media")
                    or not hmac.compare_digest(supplied, token)
                    or origin != SITE_ORIGIN
                ):
                    self.send_response(403)
                    self._cors()
                    self.end_headers()
                    return
                try:
                    self.connection.settimeout(10 if parsed.path == '/media' else 2)
                    length = int(self.headers.get("Content-Length", "0"))
                    if length < 0 or (length == 0 and parsed.path != '/media') or length > 64 * 1024 * 1024:
                        raise CliError("Invalid local cache payload size.")
                    if parsed.path == '/media':
                        if not upload_slots.acquire(blocking=False):
                            self.send_response(503)
                            self._cors()
                            self.end_headers()
                            return
                        try:
                            raw = self.headers.get('X-Holeclaw-Message', '')
                            if not raw or len(raw) > 16 * 1024:
                                raise CliError('Invalid image metadata.')
                            receipt = sink_ref.ingest_media_stream(json.loads(unquote(raw)), self.rfile, length)
                        finally:
                            upload_slots.release()
                    else:
                        payload = json.loads(self.rfile.read(length).decode("utf-8"))
                        receipt = sink_ref.ingest(payload)
                    body = json.dumps({"ok": True, **(receipt or {})}).encode("utf-8")
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
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, _format: str, *_args) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{port}/ingest?{urlencode({'token': token})}"

    def close(self) -> None:
        # Also releases a control request after normal completion.
        self.server_cancel()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
