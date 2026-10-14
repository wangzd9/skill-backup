---
name: claude-mem-memory
description: "查或记跨会话项目记忆：claude-mem worker 37700 + MCP 检索工具。"
version: 1.0.0
author: wang
license: Apache-2.0
metadata:
  hermes:
    tags: [claude-mem, memory, hermes, project-history]
    related_skills: [rosbag-catalog-status-check]
---

# claude-mem 记忆（Hermes 接入）

claude-mem 的 worker 常驻本机 127.0.0.1:37700，SQLite 库 `~/.claude-mem/claude-mem.db`，
压缩模型已配置为LAN直连Qwen vLLM（openai-compatible → 192.168.2.45:8002，模型
`/home/sysadmin/data/disk02/jc/models/qwen3.8-27b-fp8`），不花云额度、不出网。
检索工具经 Hermes 原生 MCP 注册（`hermes mcp add claude-mem`，17 工具，工具名前缀
`mcp_claude-mem_`）。

## 适用条件
- 会话涉及某项目，且需要：
  (a) 查"之前在这个项目做过什么/踩过什么坑"；
  (b) 把本会话的关键决策/bug 根因/验证结论沉淀供后续会话检索。
- 与 Hermes 自带 memory/skills 的分工：memory 只装每条会话都需要的紧凑事实；
  claude-mem 装按项目可检索的工作历史（量大、按需查）。

## 读：查历史（3 层工作流，省 token）
1. `mcp_claude-mem_search`(query=关键词, projectId=绝对路径如 /home/wang/work/rosbag-catalog) → 拿 ID 索引
2. `mcp_claude-mem_timeline`(anchor=某个ID) → 看该观察前后的上下文
3. `mcp_claude-mem_get_observations`(ids=[...]) → 只取筛中 ID 的全文
不要跳过第 1 步直接 get_observations 拉全量。

## 写：记录观察（worker HTTP API，无需额外工具）
POST `http://127.0.0.1:37700/api/sessions/init`：
`{"contentSessionId":"hermes-<会话id或描述>","sessionId":同上,"agentId":"hermes","cwd":"<项目绝对路径>","platformSource":"hermes","initialUserMessage":"<一句话任务>"}`

每完成一个值得沉淀的节点（根因定位、部署收口、验证结论），POST `/api/sessions/observations`（**单对象，非数组**）：
`{"contentSessionId":"...","cwd":"<项目绝对路径>","type":"discovery|tool_call|decision|bugfix","tool_name":"terminal","title":"≤60字标题","narrative":"发生了什么、为什么重要","facts":["要点1","要点2"],"concepts":["tags"],"files":["绝对路径"]}`

坑：observation 缺顶层 `cwd` 会报 Missing cwd 且 obsCount=0（init 的 cwd 不会自动带过来）；
worker 没起时先 `npx claude-mem start`。

## 验证
- worker 健康：`curl -s http://127.0.0.1:37700/api/health`（provider 应为 openai-compatible）
- 入库确认：`python3 -c "import sqlite3;print(list(sqlite3.connect('/home/wang/.claude-mem/claude-mem.db').execute('SELECT id,type,substr(title,1,40) FROM observations ORDER BY id DESC LIMIT 3')))"`

## 停止条件
查询无结果就直说"claude-mem 里还没有该项目的观察"，不要反复换词空转；
记录观察每会话 3-8 条为宜（关键节点），不要逐条 tool call 都记。
