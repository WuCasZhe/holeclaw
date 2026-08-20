# HoleClaw

HoleClaw 是一个北大树洞只读采集器，它按时间范围、评论数和收藏数阈值顺序读取帖子，并生成 Markdown 报告。它既可以作为 Codex Skill 使用，也可以在终端中由 Playwright 独立自动运行，不调用 AI 或大模型。

## 工作方式

```text
用户登录的浏览器
        │
        ▼
北大树洞列表/详情接口
        │
        ▼
带随机令牌的 127.0.0.1 回调
        ├── SQLite 本地缓存
        ├── JSON 检查点
        └── Markdown 报告
```

认证请求头只在浏览器进程内存中短暂使用。登录状态保存在当前工作目录的 `.auth/`。

## 环境要求

| 依赖 | 说明 |
|------|------|
| Python 3.10+ | 运行采集脚本 |
| Node.js/npm | 通过 `npx` 启动 Playwright CLI |
| Google Chrome / Edge | 浏览器自动化 |

## 安装

作为 Codex Skill 安装：

```bash
HOLECLAW_SKILLS_DIR="${CODEX_HOME:-$HOME/.codex}/skills"
mkdir -p "$HOLECLAW_SKILLS_DIR"
git clone https://github.com/WuCasZhe/holeclaw.git "$HOLECLAW_SKILLS_DIR/holeclaw"
```

只使用独立自动化时，可克隆到任意固定目录：

```bash
git clone https://github.com/WuCasZhe/holeclaw.git
cd holeclaw
python3 scripts/run_digest.py standalone --help
```


## 使用

```text
$holeclaw 汇总 2026-07-01 到 2026-07-31 评论数大于 100 或者收藏数大于 50 的帖子，给出树洞号和简介

```

对应命令行参数：

```bash
# 只按评论数筛选
python3 scripts/run_digest.py run --days 7 --min-comments 100

# 只按收藏数筛选
python3 scripts/run_digest.py run --days 7 --min-favorites 50

# 两个条件同时满足
python3 scripts/run_digest.py run --days 30 --min-comments 100 --min-favorites 50

# 两个条件满足任意一个（OR）
python3 scripts/run_digest.py run --days 60 \
  --min-comments 100 --min-favorites 45 --match-mode any
```

阈值均为严格大于；两种阈值都不提供时，默认筛选近 30 天评论数大于 50 的帖子。两个阈值默认使用 AND；`--match-mode any` 改为 OR。

采集结果会写入共享 SQLite 缓存。同一时间范围后续更换阈值或 AND/OR 模式时优先本地筛选，只扫描缓存末端之后的新帖子。列表收藏数缺失时只补取该帖详情；详情仍不可用则记录洞号并继续，不会让整页或整段覆盖失效。

## 不使用 AI，由 Playwright 独立运行

直接在终端执行 `standalone`，不需要打开 Codex：

```bash
cd /path/to/holeclaw

# 近 7 天收藏数大于 50
python3 scripts/run_digest.py standalone --days 7 --min-favorites 50

# 指定日期范围，同时满足评论和收藏阈值
python3 scripts/run_digest.py standalone \
  --since 2026-08-01 --until 2026-08-07 \
  --min-comments 100 --min-favorites 50
```

首次运行时，脚本会打开可视浏览器。用户亲自完成北大统一身份认证，进入树洞首页后回到终端按 Enter。脚本会保存本地登录状态并自动继续；以后有效期内的运行不再需要人工操作。脚本不读取或填写账号密码。

登录状态、SQLite 缓存和检查点默认相对于当前工作目录保存，因此请始终从同一目录运行，或用全局 `--state` 和运行参数 `--cache` / `--checkpoint` / `--output` 指定固定路径。全局参数需放在 `standalone` 之前，例如：

```bash
python3 scripts/run_digest.py \
  --state /var/lib/holeclaw/pku-treehole.json \
  standalone --days 1 --min-comments 100 \
  --cache /var/lib/holeclaw/cache.sqlite3
```

### 计划任务

先交互运行一次完成登录，然后计划任务使用 `--non-interactive`：

```bash
cd /path/to/holeclaw
python3 scripts/run_digest.py standalone \
  --days 1 --min-favorites 100 --non-interactive
```

`--non-interactive` 会使用无头浏览器，并在登录状态缺失或过期时直接返回错误，不会让 cron/systemd 永久等待输入。定时任务不会自动绕过或刷新北大认证。

独立模式中的报告也是完全确定性生成的：清洗帖子原文并截取一行摘要，不调用 LLM。

## 增量、恢复与性能数据

- 未完成任务会冻结当次时间窗口，重跑后继续扫描。
- 已完成的 `--days N` 任务不会冻结下一次滚动窗口；下次运行会复用 SQLite 历史覆盖，只扫新增头部。
- checkpoint 只保留命中 PID、进度和计数，完整帖子内容以 SQLite 为准。
- 网络运行结束后的 JSON 输出包含 `telemetry`，可区分 API 请求、限速等待、重试退避、响应规模和 SQLite 写入耗时。
