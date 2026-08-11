# HoleClaw

HoleClaw 是一个树洞爬虫 Skill，它按时间范围和评论数阈值顺序读取帖子，并生成高评论帖子报告。

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
git clone https://github.com/WuCasZhe/holeclaw.git
cd holeclaw
HOLECLAW_SKILLS_DIR="${CODEX_HOME:-$HOME/.codex}/skills"
mkdir -p "$HOLECLAW_SKILLS_DIR"
cp -R holeclaw-repo/holeclaw "$HOLECLAW_SKILLS_DIR/holeclaw"
```


## 使用

```text
$holeclaw 汇总 2026-07-01 到 2026-07-31 评论数大于 100 的帖子，给出树洞号和简介
```
