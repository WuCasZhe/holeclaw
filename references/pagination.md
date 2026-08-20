# PKU Treehole pagination research

Last verified: 2026-08-11, authenticated PC web client.

## Observed public client behavior

The active frontend bundle `index-a9102296.js` builds the `GET /api/v3/hole/list_comments` request with `page` and `limit`; the shared request helper adds `comment_stream=1`. The normal PC feed uses `limit=10`. No request construction for a time cursor, PID cursor, `before`, or `offset` was found around this endpoint.

The authenticated response data object contained only:

- `list`
- `total`

It did not return a next-cursor or time-boundary field.

## Low-frequency parameter probes

Six candidate requests were sent sequentially with a random 0.6–2 second delay. Each used `limit=10`, omitted `page`, and added one candidate boundary based on the last item of the observed first page:

| Candidate | Result |
| --- | --- |
| `end_timestamp` | Ignored; identical first-page PIDs |
| `start_timestamp` | Ignored; identical first-page PIDs |
| `timestamp` | Ignored; identical first-page PIDs |
| `before` | Ignored; identical first-page PIDs |
| `cursor` | Ignored; identical first-page PIDs |
| `last_pid` | Ignored; identical first-page PIDs |

All responses were HTTP 200 with application code `20000`; silent acceptance therefore does not imply that a parameter works.

## Implementation decision

Keep page-number pagination with the validated `limit=500`. Stop when the oldest row crosses the requested start time. Use SQLite PID deduplication, resumable `next_page` checkpoints, and reusable time coverage to avoid repeated historical traversal. Bounded workers may fetch a small page window concurrently, but results must be buffered and committed in page order; pages beyond a detected time boundary are treated as overfetch and never advance the checkpoint.

Re-probe only if the frontend request builder changes, the endpoint starts returning cursor metadata, or page pagination demonstrably stops working. Preserve the 0.6–2 second per-request jitter and the bounded concurrency cap during any future probe.
