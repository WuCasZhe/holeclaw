# Comment archive endpoint evidence

Inspected on 2026-09-06: the public PC entry page at
https://treehole.pku.edu.cn/ch/web/pc/index referenced
`/ch/web/assets/index-da7e78a1.js`.

The frontend maps `getReplyList` to `/api/v3/comment/list` and calls it with
`pid`, `page`, `limit: 10`, and `sort: 0` (default). The shared GET helper
adds `comment_stream: 1`. The component reads `list`, `cid`, `text`,
`name_tag`, and `quote` from comment objects. The collector uses the existing
`/chapi` proxy prefix and response envelope validation.

The archive deliberately requests an additional page after short pages and
stops on an empty list or after collecting 1000 unique comments per post. Existing cached comments are retained without truncation. Missing lists and pages with no new IDs fail
closed. Authenticated probes and regression tests cover the response envelope, termination, and resume behavior.

## Authenticated verification on 2026-09-06

For a post with 169 replies, `limit=100` returned 100, 69, and 0 comments on pages 1–3, with 169 unique CIDs. Image mode therefore uses 100 comments per page; text-only mode retains 10 so existing partial checkpoints keep their page units. Image mode has a separate checkpoint signature. Short pages still require an empty-page confirmation unless the 1000-comment cap has been reached.

The frontend maps `getMedia` to `/api/v3/media/getMediaBinary`. Original image fetches use `id=<media_id>` through `/chapi`, with the same observed authorization headers. Both post and comment `media_ids` are comma-separated IDs. Legacy image posts with empty IDs use `pid=<post_id>` instead. Thumbnails are a separate endpoint and are not used for archive downloads.

A live detail request for PID 8498065 returned HTTP 200, code 41001, message “树洞不存在”. Image metadata collection treats this verified missing-post code as unavailable, retains cached text and continues; authentication failures and unknown API errors still stop collection.

The live five-day run downloaded 32 image files, with 18 post references and 14 comment references. All 32 files passed Pillow verification and SHA-256 checks. Local regression tests additionally cover legacy IDs, shared image/content deduplication, missing files, interruption during atomic writes, non-image payloads, unavailable posts, and expired authentication.
