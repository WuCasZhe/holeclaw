import argparse
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta

try:
    from holeclaw_cache import CacheStore
    from holeclaw_checkpoint import new_checkpoint
    from holeclaw_domain import CliError, SHANGHAI
except ModuleNotFoundError:
    from scripts.holeclaw_cache import CacheStore
    from scripts.holeclaw_checkpoint import new_checkpoint
    from scripts.holeclaw_domain import CliError, SHANGHAI


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

def validate_progress_arguments(args: argparse.Namespace) -> None:
    if args.progress_pages < 1:
        raise CliError('--progress-pages must be positive.')
    if args.progress_seconds != 0 and args.progress_seconds < 10:
        raise CliError('--progress-seconds must be 0 (disabled) or at least 10.')


def validate_collection_arguments(args: argparse.Namespace, *, archive: bool = False) -> None:
    validate_progress_arguments(args)
    for option, maximum in (('checkpoint_pages', 500), ('cache_chunk_pages', 20), ('concurrency', 8)):
        if not 1 <= getattr(args, option) <= maximum:
            raise CliError(f"--{option.replace('_', '-')} must be between 1 and {maximum}.")
    if args.max_total_pages is not None and args.max_total_pages <= 0:
        raise CliError('--max-total-pages must be positive when specified.')
    if archive and not 1 <= args.comment_batch_pages <= 20:
        raise CliError('--comment-batch-pages must be between 1 and 20.')


@dataclass(frozen=True)
class RunPlan:
    """A frozen time window and its usable cache coverage, before network work."""

    start: int
    end: int
    label: str
    scan_start: int
    coverage: dict | None
    cache_only: bool

    @classmethod
    def build(cls, cache: CacheStore, start: int, end: int, label: str,
              *, fresh: bool, require_favorites: bool) -> "RunPlan":
        covering = None if fresh else cache.find_covering(start, end, require_favorites)
        prefix = None if fresh or covering else cache.find_prefix(start, end, require_favorites)
        return cls(start, end, label, prefix['end_timestamp'] if prefix else start,
                   covering or prefix, bool(covering))

    def new_checkpoint(self, spec: dict) -> dict:
        return new_checkpoint(spec, self.start, self.end, self.scan_start, self.label,
                              cache_reused=bool(self.coverage),
                              favorites_complete=bool(self.coverage['favorites_complete'])
                              if self.coverage else True)
