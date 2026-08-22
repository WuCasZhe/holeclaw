import argparse
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


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


@dataclass(frozen=True)
class FilterSpec:
    min_comments: int | None
    min_favorites: int | None
    match_mode: str = "all"

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "FilterSpec":
        return cls(args.min_comments, args.min_favorites, args.match_mode)

    def matches(self, reply: int, favorites: int | None) -> bool:
        conditions = []
        if self.min_comments is not None:
            conditions.append(reply > self.min_comments)
        if self.min_favorites is not None:
            conditions.append(
                favorites is not None and favorites > self.min_favorites
            )
        return any(conditions) if self.match_mode == "any" else all(conditions)

    def sql_clause(self) -> tuple[str, list[int]]:
        conditions = []
        parameters = []
        if self.min_comments is not None:
            conditions.append("reply > ?")
            parameters.append(self.min_comments)
        if self.min_favorites is not None:
            conditions.append("favorites > ?")
            parameters.append(self.min_favorites)
        joiner = " OR " if self.match_mode == "any" else " AND "
        return (f"({joiner.join(conditions)})" if conditions else ""), parameters

    def description(self) -> str:
        conditions = []
        if self.min_comments is not None:
            conditions.append(f"评论数 > {self.min_comments}")
        if self.min_favorites is not None:
            conditions.append(f"收藏数 > {self.min_favorites}")
        return (" 或 " if self.match_mode == "any" else " 且 ").join(conditions)

    def report_profile(self) -> tuple[str, str]:
        if (
            self.min_comments is not None
            and self.min_favorites is not None
            and self.match_mode == "any"
        ):
            return "high-comments-or-favorites", "北大树洞高评论或高收藏帖报告"
        if self.min_comments is not None and self.min_favorites is not None:
            return "high-comments-and-favorites", "北大树洞高评论与高收藏帖报告"
        if self.min_favorites is not None:
            return "high-favorites", "北大树洞高收藏帖报告"
        return "high-comments", "北大树洞高评论帖报告"


@dataclass(frozen=True)
class ReportSpec:
    output: Path
    window_label: str
    start_ts: int
    end_ts: int
    filters: FilterSpec

    @classmethod
    def from_args(
        cls,
        args: argparse.Namespace,
        output: Path,
        window_label: str,
        start_ts: int,
        end_ts: int,
    ) -> "ReportSpec":
        return cls(output, window_label, start_ts, end_ts, FilterSpec.from_args(args))
