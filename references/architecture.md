# 代码结构与重构边界

命令行仍从 `scripts/run_digest.py` 进入。命令和 SQLite / JSON 检查点版本保持兼容；入口保留原有函数的导出，便于已有脚本继续导入。热门报告默认路径保持不变，归档列表缓存改为 `archives/<账号标签哈希>/list-cache-v5.sqlite3`，与报告缓存分离。归档检查点记录 `source_cache_path` 和数据库身份；旧共享库的未完成检查点经只读身份核对后重建列表扫描计划，已有档案继续保留。新增集成应从对应模块导入，并通过 `CollectorServices` 注入浏览器登录与采集执行函数，不再把整个入口模块作为运行依赖传入。

| 模块 | 职责 |
| --- | --- |
| `holeclaw_browser.py` | Windows / WSL 环境选择、Playwright CLI、登录与认证文件 |
| `holeclaw_planning.py` | 参数校验、时间窗口、滚动窗口续传判断、`RunPlan` 缓存覆盖规划 |
| `holeclaw_runner.py` | 常驻采集进程、进度等待、取消与进程树清理、`CollectorServices` |
| `holeclaw_digest.py` | 报告采集工作流与报告输出 |
| `holeclaw_archive.py` | 归档工作流、缓存候选规划与离线搜索 |
| `holeclaw_archive_store.py` | 账号档案、候选快照、评论去重与续传 |
| `holeclaw_archive_sink.py` | 各类归档回调的业务处理与进度 |
| `holeclaw_protocol.py` | 消息解码、请求身份校验、有界回执 |
| `holeclaw_sink.py` | HTTP 回调服务、统一分派入口、列表提交 |
| `holeclaw_database.py` | SQLite 连接、锁、事务与嵌套保存点 |
| `holeclaw_cache.py` / `holeclaw_media.py` | 列表缓存 / 图片引用与文件保存 |
| `holeclaw_search.py` | 可选 trigram 索引迁移、事务内同步和精确子串查询 |
| `holeclaw_checkpoint.py` | 检查点读写校验、迁移、进度状态转换 |

## 状态和事务

`RunPlan` 固定本次窗口与可复用覆盖。`CollectionPosition` 将缓存候选批次与网络列表页额度分开计算，序列化仍使用原 v4 检查点字段。

页数、最旧帖子日期和完成状态通过 `CheckpointState` 更新。旧评论分页签名在检查点加载层转换，转换仅发生在内存中；工作流验证数据库身份后再按正常检查点保存时机落盘。检查点 JSON 必须是对象，时间窗口、游标与计数类型会在加载时检查。

所有数据库写方法自行声明事务，组合操作可以在外层再开启事务。`DatabaseSession` 用 SQLite 保存点支持嵌套：内层成功不会提前提交外层；内层失败可以单独回滚；外层失败回滚整批。调用方不再传 `commit=False`。关闭连接不会隐式提交未完成工作。

SQLite 数据与 JSON 检查点仍是分开持久化。崩溃后可能重采最后尚未记录在 JSON 的页面；帖子和评论使用数据库唯一键去重，评论游标只引用已提交数据。这次重构没有引入跨文件事务。

缓存复用按最多 500 个 PID 批量读取评论、图片和已存帖子状态。帖子写入先合并已有正文和收藏数，再比较有效字段；相同观察不重复写入，真正的新观察仍更新 `observed_at`。归档成员仅在新增或预期评论数变化时写入。

候选快照以可空列 `observed_at` 保存列表观察时间，启动时对旧库自动增列。旧快照未知的观察时间用 0 表示，不伪造当前观察时间。快照的 `(run_id, ordinal)` 与检查点页数不变；`archive_source` 在事务内登记本地已完成候选，返回待处理 `posts`、原始 `source_count`、原始页的 `oldest` 和 `reused`。采集器按原始数量判断是否耗尽，避免一页全部本地复用时提前结束。v3 回执重放不会重复增加本地复用计数。

图片状态按批读取，文件存在性和大小的检查结果只在一次批处理内复用，不跨批缓存，以便后续发现文件被删除。归档摘要默认将 `cache_integrity` 标为 `not_checked`；`archive --verify-cache` 显式执行全库校验，避免每次复用都扫描整个数据库。

## 回调协议

新采集器发送 v3 消息：

```json
{
  "schema_version": 3,
  "kind": "archive_comments",
  "run_id": "检查点 created_at",
  "request_id": 17,
  "payload": {"post": {}, "comments": [], "comment_page": 2, "comment_page_size": 100, "complete": true}
}
```

`kind` 唯一决定处理器。每个采集进程中的逻辑请求使用递增编号；HTTP 重试复用同一编号和冻结的请求体。sink 在同一把锁下查询回执、执行处理器并记住结果。交错到达的重复请求返回原结果，评论批数、图片回执和复用统计不会因响应丢失重试而重复增加。同一编号携带不同内容会被拒绝。

回执保留在本次 sink 内存中，最多 2048 条且序列化响应合计不超过 16 MiB。超出保留范围的旧编号会被拒绝，不能再次执行写入；用户重新运行命令后，从持久化检查点和评论游标恢复。回执不提供跨进程的恰好一次语义。

v2 平面消息通过兼容层解码，冲突的布尔消息类型会被拒绝。旧评论批次支持相同内容的重试去重；旧的状态查询仍读取当前游标。新代码统一使用 v3。

## 浏览器采集器

`collect.js` 保持单文件，以符合 Playwright `page.evaluate` 的序列化边界，内部按状态归属拆成工厂：

- `createTelemetry`：累计计数、快照差值与已确认快照。
- `createRequestScheduler`：全局请求额度、共享冷却、429 降速与重试。
- `createSinkClient`：消息编号、序列化与固定请求体重试。
- `createListPipeline`：有界预取、边界判断与按页号取页。
- `createPostQueue`：全局帖子工作队列和同 PID 串行化。
- `createPostArchiver`：详情补齐、评论与图片归档。

主循环负责按连续完成页提交。请求总上限、随机等待、评论分页单位、评论数量上限、缓存与网络批次边界保持原有语义。取消时唤醒等待者并等待已启动工作收尾。

## 验证

```bash
python3 -m unittest discover -s tests -q
node --test tests/test_collect*.js
```

Windows 使用 `python` 代替 `python3`。测试只用合成数据与临时数据库，不依赖树洞登录。平台进程树测试在对应系统运行。

`tests/test_refactoring.py` 覆盖并发重试、回执过期、嵌套回滚、旧检查点转换和清理失败，并实际执行 JS 采集器，将其生成的 v3 消息交给 Python sink。JS harness 为每次采集创建独立 VM，不再覆盖全局 `fetch` 和计时器。既有行为测试通过测试侧适配器继续检查原有列表、评论与图片结果；新增协议测试直接检查原始消息。

`tests/collector_simulation.js` 为优化回归测试提供确定性离线模拟，使用合成响应和虚拟时钟检查请求统计、分页边界与并发调度。

## 可靠性与大档案优化

每次远端请求和 sink 回调有 60 秒期限，图片流每次读取另有 30 秒空闲期限。超时只取消当前尝试，全局取消仍会终止全部工作。共享冷却完整保留服务器指定等待时间，按最多 60 秒的小段等待以避免计时器溢出。

`archive_post_unavailable` 独立于图片功能。在 `comment_scans.unavailable` 保存不可访问原因，`complete` 保持 0；完成判断和摘要区分不可访问、已完成和待采集。新评论观察或 `--fresh` 可使评论扫描重新检查，成功采集会清除不可访问状态。旧图片客户端的 `archive_media_unavailable` 仍兼容。

新图片客户端向 `/media` 上传 Blob，`X-Holeclaw-Message` 携带 URI 编码的 v3 JSON 元数据，沿用原随机令牌和 Origin 校验。服务端最多接收两个并行上传，流式写入独立临时文件并计算 SHA-256、同步文件；这些工作在 sink 锁外完成。发布前将实际内容哈希加入回执摘要，在锁内检查重复请求、原子重命名并提交元数据。上传失败或回执重放都会清理临时文件，旧 Base64 回调继续兼容。

搜索索引使用 external-content FTS5 trigram 表和插入、删除、正文变更触发器；更新观察时间不会重新分词。旧库首次打开时在事务内回填；不支持 FTS5/trigram 的构建保持原搜索路径。查询用索引缩小候选后仍用 `instr` 验证，短关键词和含 NUL 的查询回退。索引增加磁盘和正文写入成本，换取长关键词查询免于全表扫描。

`tests/test_improvements.py` 覆盖索引回填与回滚、不可访问状态、显式校验、流式上传去重与失败清理、锁外 fsync，并将真实 JS 二进制请求发给 Python HTTP sink。`tests/test_collect_resilience.js` 覆盖请求超时、长 Retry-After、删除帖子和慢上传时的并发限制。

## 工作队列与状态复用

准备批次在同一事务内完成零评论扫描、图片评论元数据标记和成员登记。`empty_completed` 区分本地新完成与历史复用；不删除历史评论。缓存候选返回 `resumes`，避免浏览器对未改变的候选重复准备。

帖子工作队列先完成文字和图片引用，再返回独立图片阶段的完成 Promise。工作名额立即释放，同 PID 串行约束和页提交仍等待该 Promise。图片准备队列最多保留配置并发数的执行任务，文件传输队列最多两个。按 `min(reply,1000)` 降序安排同页帖子，不改变页序。

浏览器用递增完成版本校验准备快照。后续同 PID 工作执行前若版本更新，重新准备单帖；最多保留 8192 个版本，超过保留边界的快照保守地重新准备。详情补齐导致字段变化也重新准备。

图片引用按评论批次读取并比较有效字段，仅变化时写入并使该帖计划缓存失效。带身份的下载计划使用键分页，每次最多读 128 个引用，最多缓存 64 个队列；每次预约前重新批查文件状态，防止重复下载其他帖子刚保存的共享图片。完整走完后清除队列，后续新计划仍会发现文件缺失。最近计划回执最多 256 个，v3 重试另由统一回执账本保护。

`favorite_deferred_pids` 只接受 AND 模式下按评论数已不命中的未知收藏行。sink 验证延期条件，保存帖子但不登记接口不可用，并使窗口收藏覆盖不完整；后续收藏查询不能将这段覆盖当成已完整补齐。

`tests/test_efficiency.py` 和 `tests/test_collect_efficiency.js` 覆盖批量提交、阶段解耦、重复 PID、准备结果失效、引用写入跳过、计划分页、筛选短路及缓存覆盖。
