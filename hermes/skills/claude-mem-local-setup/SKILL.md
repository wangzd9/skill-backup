---
name: claude-mem-local-setup
description: "Use when 安装/配置 claude-mem 代理记忆：非交互装与本地 Qwen 接法。"
---

# claude-mem 本机安装与本地模型配置

用户要求安装 https://github.com/thedotmack/claude-mem 时。目标形态：纯本机（无云同步、无 Anthropic 订阅），记忆压缩走本机 Qwen vLLM（OpenAI 兼容接口）。数据目录 `~/.claude-mem/`，插件目录 `~/.claude/plugins/marketplaces/thedotmack/`。

## 步骤

1. **环境**：Node ≥20、uv（`~/.hermes/bin/uv` 已有）；Bun 缺失时安装器自动装。先查本机可用 LLM：`curl -s http://192.168.2.45:8002/v1/models` 取 model id（是完整路径字符串，不是短名）。
2. **非交互安装**：`CI=1 npx -y claude-mem install </dev/null 2>&1 | tail -45`。交互式的 agent 多选界面在 Hermes PTY 里会卡住（`process_manage` 的 submit/回车不生效，界面静止不动）——不要走 PTY 交互，直接非交互；它会自动检测本机 agent（如 Codex CLI）并注册插件、装 Bun/uv 依赖。
3. **改provider为LAN直连Qwen**：保留`~/.claude-mem/settings.json`的其他键，当前版本使用`CLAUDE_MEM_PROVIDER=openai-compatible`、`CLAUDE_MEM_OPENAI_COMPAT_BASE_URL=http://192.168.2.45:8002/v1`、`CLAUDE_MEM_OPENAI_COMPAT_MODEL=<实际model id>`及`CLAUDE_MEM_OPENAI_COMPAT_API_KEY`（无鉴权实例用既有非空占位值）。不恢复旧18002隧道；云同步三键留空，配置权限保持600。若版本字段不同，先核对该版本支持的键，不能照搬旧openrouter设置。
4. **启动 worker**：安装器跳过 worker 自启，手动 `npx claude-mem start`。完成判据：`curl -s http://127.0.0.1:$(cat ~/.claude-mem/.worker.port)/api/health` 返回 provider 指向本地模型；Web 查看器在该端口（以 `.worker.port` 实际值为准）。**启动后未过 health 验证前不得报"安装完成"**。
5. **可选修复**：tree-sitter CLI 下载失败只影响 `smart_search`/`smart_outline` 辅助功能（核心记忆不受影响），跑 `npx claude-mem repair` 重试。

## 陷阱

- 记忆从项目的**第二次会话**才开始注入，第一次只积累 observations——用户问"怎么没用上"时先确认是否已有第二次会话。
- 本机命令审批门：`npx ... start`/`npm` 类命令可能进审批队列，用户不在线 5 分钟即 BLOCKED 且未执行——遇到 BLOCKED 不要换皮重试，告知用户在线点批或自行终端跑一次（一次即可，后续会话自动拉起 worker）。
- 不要把 cmem.ai 订阅/试用流程当默认路径推给用户：本机 Qwen 配置下全程不出网，云同步保持 OFF。
- 修改 provider 后必须 `npx claude-mem restart`（worker 持有旧 provider 内存态），不重启不生效。
