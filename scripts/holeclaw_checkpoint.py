import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TypedDict

try:
    from holeclaw_domain import (
        CACHE_SCHEMA_VERSION,
        CHECKPOINT_SCHEMA_VERSION,
        TELEMETRY_FIELDS,
        TELEMETRY_MAX_FIELDS,
        CliError,
        SHANGHAI,
    )
except ModuleNotFoundError:
    from scripts.holeclaw_domain import (
        CACHE_SCHEMA_VERSION,
        CHECKPOINT_SCHEMA_VERSION,
        TELEMETRY_FIELDS,
        TELEMETRY_MAX_FIELDS,
        CliError,
        SHANGHAI,
    )


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


def default_runtime_root() -> Path:
    override = os.environ.get("HOLECLAW_RUNTIME_DIR")
    if override:
        return Path(override).expanduser()
    codex_home = Path(
        os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
    ).expanduser()
    return codex_home / "holeclaw-runtime"


def default_checkpoint_path(spec: dict) -> Path:
    fingerprint = hashlib.sha256(
        json.dumps(spec, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return (
        default_runtime_root()
        / f"holeclaw-checkpoints-v{CHECKPOINT_SCHEMA_VERSION}"
        / f"{fingerprint}.json"
    )


def default_cache_path() -> Path:
    return default_runtime_root() / f"holeclaw-cache-v{CACHE_SCHEMA_VERSION}.sqlite3"


def write_checkpoint(path: Path, checkpoint: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(checkpoint, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.chmod(0o600)
    os.replace(temporary, path)
    path.chmod(0o600)


class Checkpoint(TypedDict, total=False):
    schema_version: int
    request: dict
    start_timestamp: int
    end_timestamp: int
    scan_start_timestamp: int
    window_label: str
    next_page: int
    total_pages: int
    total_scanned: int
    matched_by_pid: dict[str, bool]
    telemetry: dict[str, int]
    cache_reused: bool
    favorites_complete: bool
    cache_path: str
    cache_instance_id: str
    completed: bool
    reached_start: bool
    feed_exhausted: bool
    created_at: str
    updated_at: str
    completed_at: str | None
    archive_cached_pages: int
    archive_cache_only: bool
    cached_posts: int
    source_instance_id: str
    source_cache_path: str
    archive_summary_version: int
    archive_metrics: dict[str, int]
    oldest_post_timestamp: int
    fresh: bool


@dataclass(frozen=True)
class CollectionPosition:
    """Separate cache batches from remote pages without changing checkpoint v4."""

    next_batch: int
    cached_batches: int
    committed_batches: int

    @classmethod
    def from_checkpoint(cls, checkpoint: dict) -> "CollectionPosition":
        return cls(checkpoint['next_page'], checkpoint.get('archive_cached_pages', 0),
                   checkpoint['total_pages'])

    @property
    def remaining_cached_batches(self) -> int:
        return max(0, self.cached_batches - self.next_batch + 1)

    @property
    def committed_remote_pages(self) -> int:
        return max(0, self.committed_batches - self.cached_batches)


class CheckpointState:
    """Own progress transitions; serialization remains the existing JSON mapping."""

    def __init__(self, data: dict):
        self.data = data

    def record_post_date(self, timestamp: int) -> None:
        if timestamp > 0:
            previous = self.data.get('oldest_post_timestamp', 0)
            self.data['oldest_post_timestamp'] = min(previous, timestamp) if previous else timestamp

    def commit_pages(self, payload: dict, matched_pids: list[str]) -> None:
        data = self.data
        data['matched_by_pid'].update(dict.fromkeys(matched_pids, True))
        data['next_page'] = payload['end_page'] + 1
        data['total_pages'] += payload['pages']
        data['total_scanned'] += payload['scanned']
        data['reached_start'] = bool(payload.get('reached_start'))
        data['feed_exhausted'] = bool(payload.get('feed_exhausted'))
        data['updated_at'] = datetime.now(SHANGHAI).isoformat()
        self.record_post_date(int(payload.get('oldest', 0)))

    def finish(self, result: dict) -> None:
        data = self.data
        data['reached_start'] = bool(result.get('reached_start'))
        data['feed_exhausted'] = bool(result.get('feed_exhausted'))
        data['completed'] = data['reached_start'] or data['feed_exhausted']
        data['updated_at'] = datetime.now(SHANGHAI).isoformat()
        data['completed_at'] = data['updated_at'] if data['completed'] else None


def new_checkpoint(
    spec: dict,
    start_ts: int,
    end_ts: int,
    scan_start_ts: int,
    window_label: str,
    cache_reused: bool = False,
    favorites_complete: bool = True,
) -> Checkpoint:
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
        checkpoint = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(checkpoint, dict):
            raise CliError(f"Checkpoint must be a JSON object: {path}")
        return checkpoint
    except (OSError, json.JSONDecodeError) as error:
        raise CliError(f"Cannot read checkpoint {path}: {error}") from error


def validate_checkpoint(checkpoint: dict) -> None:
    for field in ('start_timestamp', 'end_timestamp', 'scan_start_timestamp',
                  'next_page', 'total_pages', 'total_scanned'):
        value = checkpoint.get(field)
        minimum = 1 if field == 'next_page' else 0 if field in ('total_pages', 'total_scanned') else None
        if type(value) is not int or (minimum is not None and value < minimum):
            raise CliError(f"Invalid checkpoint field: {field}")
    if not checkpoint['start_timestamp'] <= checkpoint['scan_start_timestamp'] < checkpoint['end_timestamp']:
        raise CliError('Invalid checkpoint time window.')
    for field in ('completed', 'reached_start', 'feed_exhausted', 'favorites_complete'):
        if type(checkpoint.get(field)) is not bool:
            raise CliError(f"Invalid checkpoint field: {field}")
    for field in ('created_at', 'updated_at', 'window_label'):
        if not isinstance(checkpoint.get(field), str) or not checkpoint[field]:
            raise CliError(f"Invalid checkpoint field: {field}")
    if not isinstance(checkpoint.get('matched_by_pid'), dict) or not isinstance(checkpoint.get('telemetry'), dict):
        raise CliError('Invalid checkpoint counters.')
    merge_telemetry(empty_telemetry(), checkpoint['telemetry'])
    cached_pages = checkpoint.get('archive_cached_pages', 0)
    if type(cached_pages) is not int or cached_pages < 0:
        raise CliError('Invalid checkpoint cached page count.')


def load_checkpoint(path: Path, spec: dict, *, legacy_spec: dict | None = None) -> dict:
    checkpoint = read_checkpoint(path)
    # Migrate in memory; callers validate database identity before writing it back.
    if (checkpoint.get('schema_version') == CHECKPOINT_SCHEMA_VERSION
            and legacy_spec is not None and checkpoint.get('request') == legacy_spec):
        checkpoint['request'] = spec
    if (
        checkpoint.get("schema_version") != CHECKPOINT_SCHEMA_VERSION
        or checkpoint.get("request") != spec
    ):
        raise CliError(
            f"Checkpoint is incompatible or parameters do not match: {path}. "
            "Use a new checkpoint path."
        )
    validate_checkpoint(checkpoint)
    return checkpoint
