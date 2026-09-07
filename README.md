# HoleClaw

HoleClaw 是一个树洞Claw，它可以按时间范围、评论数和收藏数阈值读取帖子，并生成报告，既可以作为 Codex Skill 使用，也可以在命令行中手动执行。

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
登录状态保存在当前工作目录的 `.auth/`。

## 环境要求

| 依赖 | 说明 |
|------|------|
| Python 3.10+ | 运行采集脚本 |
| Node.js/npm | 通过 `npx` 启动 Playwright CLI |
| Google Chrome / Edge | 浏览器自动化 |

## 安装

作为 Codex Skill 安装：

`推荐直接在 Codex 中发送消息，让内置技能安装器完成安装`

只使用独立自动化时，可克隆到任意固定目录：

```bash
git clone https://github.com/WuCasZhe/holeclaw.git
cd holeclaw
python3 scripts/run_digest.py standalone --help
```
