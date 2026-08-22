import html
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

try:
    from holeclaw_domain import FilterSpec, SHANGHAI
except ModuleNotFoundError:
    from scripts.holeclaw_domain import FilterSpec, SHANGHAI


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


def render_report(data: dict, output: Path, window_label: str) -> None:
    grouped = defaultdict(list)
    for post in data["candidates"]:
        local_time = datetime.fromtimestamp(int(post["timestamp"]), SHANGHAI)
        grouped[local_time.date().isoformat()].append((local_time, post))

    collected = datetime.fromisoformat(
        data["collected_at"].replace("Z", "+00:00")
    ).astimezone(SHANGHAI)
    start = datetime.fromtimestamp(data["start_timestamp"], SHANGHAI)
    end = datetime.fromtimestamp(data["end_timestamp"], SHANGHAI)
    filters = FilterSpec(
        data["min_comments"],
        data["min_favorites"],
        data.get("match_mode", "all"),
    )
    _slug, title = filters.report_profile()
    lines = [
        f"# {title}（{window_label}）",
        "",
        f"- 生成时间：{collected:%Y-%m-%d %H:%M:%S}（Asia/Shanghai）",
        f"- 时间范围：{start:%Y-%m-%d %H:%M:%S} 至 {end:%Y-%m-%d %H:%M:%S}",
        f"- 筛选条件：{filters.description()}",
        f"- 本次网络扫描：{data['pages']} 页，{data['scanned']:,} 条帖子",
        f"- 命中：{data['candidate_count']} 条",
    ]
    if data.get("cache_reused"):
        lines.append("- 数据来源：SQLite 本地缓存（必要的新时间段已增量扫描）")
    unavailable = data.get("favorite_unavailable") or []
    if filters.min_favorites is not None and unavailable:
        unavailable_pids = "、".join(f"#{item['pid']}" for item in unavailable)
        lines.append(
            f"- 收藏数不可用：{len(unavailable)} 条（{unavailable_pids}）；"
            "不按收藏条件命中，但在 OR 模式下仍可按评论条件命中"
        )
    has_favorite_snapshots = filters.min_favorites is not None or any(
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
