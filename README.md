# HoleClaw

HoleClaw 是一个北大树洞只读采集器，按时间范围、评论数和收藏数阈值顺序读取帖子，并生成 Markdown 报告。它既可以作为 Codex Skill 快速使用，也可以在终端中由 Playwright 独立自动运行。

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

运行环境为WSL2

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


## Codex 使用

```text
使用$holeclaw 汇总 2026-07-01 到 2026-07-31 评论数大于 100 或者收藏数大于 50 的帖子，给出树洞号和简介
```

对应命令行参数：

```bash
# 只按评论数筛选
python3 scripts/run_digest.py run --days 7 --min-comments 100

# 只按收藏数筛选
python3 scripts/run_digest.py run --days 7 --min-favorites 50

python3 scripts/run_digest.py run --days 30 --min-comments 100 --min-favorites 50
```

阈值均为严格大于；两种阈值都不提供时，默认筛选近 30 天评论数大于 50 的帖子。

## Playwright 独立运行

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

`--non-interactive` 使用无头浏览器
