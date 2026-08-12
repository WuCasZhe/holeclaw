# HoleClaw

HoleClaw 是一个树洞爬虫 Skill，它按时间范围、评论数和收藏数阈值顺序读取帖子，并生成筛选报告。

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
| Codex | VS Code 扩展或 CLI |
| Python 3.10+ | 运行采集脚本 |
| Google Chrome / Edge | 浏览器自动化 |

## 安装
```bash
HOLECLAW_SKILLS_DIR="${CODEX_HOME:-$HOME/.codex}/skills"
mkdir -p "$HOLECLAW_SKILLS_DIR"
git clone https://github.com/WuCasZhe/holeclaw.git "$HOLECLAW_SKILLS_DIR/holeclaw"
```


## 使用

```text
$holeclaw 汇总 2026-07-01 到 2026-07-31 评论数大于 100 的帖子，给出树洞号和简介
```

对应命令行参数：

```bash
# 只按评论数筛选
python3 scripts/run_digest.py run --days 7 --min-comments 100

# 只按收藏数筛选（不会额外套用默认评论阈值）
python3 scripts/run_digest.py run --days 7 --min-favorites 50

# 两个条件同时满足
python3 scripts/run_digest.py run --days 30 --min-comments 100 --min-favorites 50
```

阈值均为严格大于；两种阈值都不提供时，默认筛选近 30 天评论数大于 50 的帖子。
