# 代码结构与重构边界

命令行仍从 `scripts/run_digest.py` 进入。命令、默认路径和 SQLite / JSON 检查点版本保持兼容；入口保留原有函数的导出，便于已有脚本继续导入。新增集成应从对应模块导入，并通过 `CollectorServices` 注入浏览器登录与采集执行函数，不再把整个入口模块作为运行依赖传入。

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
| `holeclaw_checkpoint.py` | 检查点读写校验、迁移、进度状态转换 |

## 状态和事务

`RunPlan` 固定本次窗口与可复用覆盖。`CollectionPosition` 将缓存候选批次与网络列表页额度分开计算，序列化仍使用原 v4 检查点字段。

页数、最旧帖子日期和完成状态通过 `CheckpointState` 更新。旧评论分页签名在检查点加载层转换，转换仅发生在内存中；工作流验证数据库身份后再按正常检查点保存时机落盘。检查点 JSON 必须是对象，时间窗口、游标与计数类型会在加载时检查。

所有数据库写方法自行声明事务，组合操作可以在外层再开启事务。`DatabaseSession` 用 SQLite 保存点支持嵌套：内层成功不会提前提交外层；内层失败可以单独回滚；外层失败回滚整批。调用方不再传 `commit=False`。关闭连接不会隐式提交未完成工作。

SQLite 数据与 JSON 检查点仍是分开持久化。崩溃后可能重采最后尚未记录在 JSON 的页面；帖子和评论使用数据库唯一键去重，评论游标只引用已提交数据。这次重构没有引入跨文件事务。

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
