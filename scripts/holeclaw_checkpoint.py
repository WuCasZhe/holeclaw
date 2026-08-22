import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

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


def default_checkpoint_path(spec: dict) -> Path:
    fingerprint = hashlib.sha256(
        json.dumps(spec, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return Path.cwd() / "output/playwright/holeclaw-checkpoints-v4" / f"{fingerprint}.json"


def default_cache_path() -> Path:
    return Path.cwd() / f"output/playwright/holeclaw-cache-v{CACHE_SCHEMA_VERSION}.sqlite3"


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
