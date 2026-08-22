import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

try:
    from holeclaw_domain import CACHE_SCHEMA_VERSION, CliError, FilterSpec, SHANGHAI
except ModuleNotFoundError:
    from scripts.holeclaw_domain import (
        CACHE_SCHEMA_VERSION,
        CliError,
        FilterSpec,
        SHANGHAI,
    )


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
        filter_spec = FilterSpec(min_comments, min_favorites, match_mode)
        engagement_filter, engagement_parameters = filter_spec.sql_clause()
        if engagement_filter:
            filters.append(engagement_filter)
            parameters.extend(engagement_parameters)
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
