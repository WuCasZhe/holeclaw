---
name: holeclaw
description: HoleClaw performs rate-limited, sequential, read-only collection and Markdown summarization of authenticated PKU Treehole posts, with a persistent collector, resumable checkpoints, and a reusable local SQLite cache. Use when the user invokes HoleClaw or asks to crawl, browse, count, filter, summarize, or generate a daily/report digest of high-comment or high-favorite posts from treehole.pku.edu.cn, including requests such as "北大树洞近 7 天评论数大于 100 的帖子", "收藏数大于 50 的树洞", or "树洞高评论日报".
---

# HoleClaw

Use the bundled browser collector and renderer. Keep the workflow read-only and never request the user's password, QR token, authorization header, cookie, or other login secret in chat.

## Interpret parameters

- Accept `近 N 天` as `--days N`.
- Accept an explicit inclusive local date range as `--since YYYY-MM-DD --until YYYY-MM-DD`.
- Accept `评论数大于 N` as `--min-comments N`. The comparison is strictly `reply > N`.
- Accept `收藏数大于 N` or `关注数大于 N` as `--min-favorites N`. The comparison is strictly `likenum > N`.
- When both thresholds are present, default to both conditions (logical AND). If the user
  explicitly says "or" / "或者" / "任一", pass `--match-mode any` for logical OR.
- Default to `--days 30 --min-comments 50` only when the user omits both engagement thresholds. A favorite-only request must not silently add the default comment filter.
- Reject non-positive days, negative thresholds, an end before a start, and future-only ranges.
- Warn before scanning a range older than 90 days or a past range far behind the current feed; the API is newest-first and must traverse intervening posts.

## Run the workflow

Set the skill path from the current skill location:

```bash
SKILL_DIR="${CODEX_HOME:-$HOME/.codex}/skills/holeclaw"
```

### 1. Establish login when needed

Open a visible browser:

```bash
python3 "$SKILL_DIR/scripts/run_digest.py" login-open
```

Ask the user to complete PKU authentication in the visible browser. Pause until the user confirms that the Treehole home page is visible. Then save the local state:

```bash
python3 "$SKILL_DIR/scripts/run_digest.py" login-save
```

The default state path is `.auth/pku-treehole.json`. The script sets mode `0600` and adds `.auth/` to the current workspace `.gitignore`. Do not print or inspect the state contents.
Normal collection runs load this state but do not save it again. Persist authentication only through the explicit `login-save` command, after login or when the session must be refreshed.

### 2. Collect and render

Examples:

```bash
# Rolling time window
python3 "$SKILL_DIR/scripts/run_digest.py" run --days 7 --min-comments 100

# Favorite-only filter
python3 "$SKILL_DIR/scripts/run_digest.py" run --days 7 --min-favorites 50

# Require both thresholds
python3 "$SKILL_DIR/scripts/run_digest.py" run \
  --days 7 --min-comments 100 --min-favorites 50

# Require either threshold (logical OR)
python3 "$SKILL_DIR/scripts/run_digest.py" run \
  --days 60 --min-comments 100 --min-favorites 45 --match-mode any

# Inclusive local calendar range
python3 "$SKILL_DIR/scripts/run_digest.py" run \
  --since 2026-07-01 --until 2026-07-31 --min-comments 80

# Long range with resumable checkpoints
python3 "$SKILL_DIR/scripts/run_digest.py" run \
  --days 365 --min-comments 100 \
  --checkpoint-pages 500 --max-total-pages 2000
```

Use `--output PATH` only when the user specifies an output location. Otherwise write under `reports/` in the current workspace.

### Standalone Playwright automation without AI

When the user wants HoleClaw to run without Codex or AI assistance, use the bundled standalone entry point:

```bash
python3 "$SKILL_DIR/scripts/run_digest.py" standalone \
  --days 7 --min-favorites 50
```

The repository bundles `scripts/playwright_cli.sh`, so standalone mode does not depend on the separate Codex Playwright skill. It still requires Python 3.10+, Node.js/npm (`npx`), and a supported local browser. It makes no LLM or AI API calls; one-line report summaries are deterministic text cleanup and truncation.

If the saved state is missing or expired, standalone mode opens a headed browser and pauses once. The user must personally complete PKU authentication, navigate to the Treehole home page, and press Enter in the terminal. The script then saves the local state and continues automatically. Never fill credentials or bypass authentication.

For cron/systemd after the initial interactive login, add `--non-interactive`. This uses a headless browser and makes missing or expired login state fail immediately instead of waiting on stdin. Always use a stable working directory or explicit `--state`, `--cache`, `--checkpoint`, and `--output` paths so scheduled runs reuse the intended state and cache. Global `--state` and `--session` options must appear before the `standalone` subcommand.

Use one persistent browser collector for the whole run. It performs list parsing and time/comment/favorite filtering inside the request process, then streams compact cache chunks to the same Python process over a tokenized localhost callback. Progress and completion are event-driven from that callback; do not add Playwright/session-storage polling or one CLI process per page/checkpoint.

The authenticated `list_comments` endpoint currently supports page-number pagination only. The 2026-08-11 frontend inspection and low-frequency probes found no working time/PID cursor; read [references/pagination.md](references/pagination.md) before changing pagination or probing the endpoint again. Keep `page + limit=500` and rely on checkpoints plus the SQLite coverage cache for historical scans.

Write the visible checkpoint every `--checkpoint-pages` pages; the default and maximum are 500 pages. SQLite/WAL cache chunks default to one page so a failure replays at most the current page. An error flushes the latest durable progress before exit. Re-run the exact same command to resume from `next_page`; an unfinished rolling run keeps its first start and end timestamps frozen. After it completes, the next invocation recalculates the rolling window and reuses SQLite coverage instead of returning the old frozen report. Checkpoints retain matched PIDs and counters, while complete post bodies remain in SQLite only.

Store checkpoints under `output/playwright/holeclaw-checkpoints/` and the shared cache at `output/playwright/holeclaw-cache.sqlite3`, both with mode `0600`. The cache is independent of the report threshold: reuse complete coverage for different thresholds, AND/OR modes, or shorter ranges, and scan only the new head when an older coverage interval contains the requested start. Cache coverage created before favorite counts were stored remains reusable for comment-only reports, but favorite-filtered reports must rescan that coverage once to populate `likenum`. If a list favorite count is missing or invalid, fetch only that post's detail once. If the detail also lacks a usable count, record the PID as explicitly unavailable, keep the rest of the favorite coverage reusable, and mention it in the report. Use `--fresh` only when the user explicitly requests a network refresh that ignores reusable coverage.

After a network run, use the terminal `telemetry` object to distinguish request time, pacing delay, retry backoff, response size, and SQLite write time before proposing rate changes. Do not infer that the configured jitter is the bottleneck from total runtime alone.

### 3. Handle authentication expiry

If `run` reports that the page returned to `iaaa.pku.edu.cn`, repeat `login-open`, pause for the user's login, then run `login-save` and retry. Never attempt to fill the account or password fields.

## Request-safety rules

- Keep the bundled fixed page size and 0.6–2 second jitter; do not lower the delay.
- Keep the checkpoint interval at or below 500 pages and the default total safety ceiling at 2,000 pages.
- Keep one persistent collector process and send no concurrent Treehole requests.
- Keep progress/completion event-driven through the localhost sink; never poll with extra Playwright CLI calls.
- Filter requested comment/favorite matches inside the browser request process; send cache chunks only to the tokenized `127.0.0.1` sink.
- Save accumulated PIDs, counts, and `next_page` atomically at checkpoint boundaries and on errors.
- Store all list rows in SQLite/WAL so later thresholds and covered time ranges can be rendered without rescanning.
- Send requests sequentially with no concurrency.
- Stop as soon as the collector crosses the requested start time.
- Honor `Retry-After`; back off on transient fetch/network errors and 429/5xx, then stop after three failed attempts.
- Do not mass-fetch detail endpoints when list responses already contain the full post text, reply count, and favorite count. Fetch details only when required data is missing.
- A post with an explicitly unavailable favorite count does not match a favorite threshold. In
  `--match-mode any`, it may still match the comment threshold.
- Do not like, follow, comment, publish, or modify account state.
- Do not auto-save browser authentication after scans; use `login-save` only after an intentional login refresh.

## Verify the result

After the script completes, check:

- `reached_start` is true.
- SQLite `PRAGMA integrity_check` returns `ok` after a network run.
- Every entry satisfies the requested logic: every threshold in `all` mode, or at least one threshold in `any` mode.
- PIDs are unique.
- Entries fall inside the requested time window.
- The report contains a treehole number and a one-line content summary for every entry.

Return a clickable path to the Markdown report plus scanned-page, scanned-post, and matched-post counts. Mention that comment and favorite counts are scan-time snapshots and that image posts summarize captions only unless the user explicitly requests image analysis.
