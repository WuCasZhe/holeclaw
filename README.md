# HoleClaw

HoleClaw 是一个树洞Claw，它按时间范围、评论数和收藏数阈值顺序读取帖子，并生成 Markdown 报告。它既可以作为 Codex Skill 使用，也可以在命令行中手动执行。

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

## 手动执行

直接在命令行加上 `standalone`：

```bash
cd /path/to/holeclaw

# 近 7 天收藏数大于 50
python3 scripts/run_digest.py standalone --days 7 --min-favorites 50

# 指定日期范围，同时满足评论和收藏阈值
python3 scripts/run_digest.py standalone \
  --since 2026-08-01 --until 2026-08-07 \
  --min-comments 100 --min-favorites 50

# 使用 1–4 的有限并发；默认值为 2
python3 scripts/run_digest.py standalone \
  --days 7 --min-comments 100 --concurrency 4
```

首次运行时，脚本会打开可视浏览器。用户亲自完成统一身份认证，进入树洞首页后回到终端按 Enter。脚本会保存本地登录状态并自动继续

登录状态、SQLite 缓存和检查点默认相对于当前工作目录保存，因此请始终从同一目录运行，或用全局 `--state` 和运行参数 `--cache` / `--checkpoint` / `--output` 指定固定路径。全局参数需放在 `standalone` 之前，例如：

```bash
python3 scripts/run_digest.py \
  --state /var/lib/holeclaw/pku-treehole.json \
  standalone --days 1 --min-comments 100 \
  --cache /var/lib/holeclaw/cache.sqlite3
```
