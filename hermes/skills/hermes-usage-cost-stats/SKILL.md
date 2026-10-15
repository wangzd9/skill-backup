---
name: hermes-usage-cost-stats
description: 查 Hermes 累计 token 与成本：state.db 用量表 + 云 API 参考价口径。
---

# Hermes 用量与成本换算

用户问"Hermes 累计用了多少 token / 换算成人民币多少"时的标准查询与汇报流程。数据在 `~/.hermes/state.db`。

## 步骤

1. 查询（用 **execute_code 里的 sqlite3**；在 terminal 里跑引用 `state.db` 的命令或脚本会被 gateway 的 lifecycle guard 以 "is a SQLite database open in this gateway process" failed-closed 拒绝）：
   ```python
   import sqlite3
   c = sqlite3.connect('/home/wang/.hermes/state.db')
   r = c.execute('''SELECT billing_provider, model,
        SUM(api_call_count), SUM(input_tokens), SUM(output_tokens),
        SUM(cache_read_tokens)+SUM(cache_write_tokens)+SUM(reasoning_tokens),
        SUM(estimated_cost_usd), SUM(actual_cost_usd),
        MIN(first_seen), MAX(last_seen) FROM session_model_usage GROUP BY billing_provider, model''').fetchall()
   ```
   `billing_provider` 可能为空串（历史行），空串行与 `custom` 行是同一模型，汇报时合并；`first_seen/last_seen` 是 epoch 秒，换算出统计区间。
   单日查询：`WHERE last_seen >= 当日 00:00(UTC+8) 的 epoch 秒`。注意每行是**会话级累计**而非逐日值：跨天长会话（如常驻 cron/巡检）的整段历史用量会因 last_seen 落在今天而被全额计入，单日数字偏大是近似值；汇报前可对比相关行的 first_seen 判断偏差量级，必要时点一句口径。
2. 判断计费来源：`billing_base_url` 指向 `127.0.0.1` 本地 vLLM 隧道（本环境 `qwen-a100` provider，模型路径 `/home/sysadmin/.../qwen3.8-27b-fp8`）→ 实际 API 花费为 0，`estimated_cost_usd/actual_cost_usd` 也应为 0；若出现非 0 的云端 provider 行，单独列出其真实费用。
3. 云 API 参考价换算（本环境是 Qwen 模型，默认给这两档 + GPT-4o 对照，标注"参考价非真实账单"）：
   - qwen-plus 档：输入 ¥0.8/百万、输出 ¥2/百万
   - qwen-max 档：输入 ¥2.4/百万、输出 ¥9.6/百万
   - GPT-4o 档：$2.5/百万输入、$10/百万输出（USD 原值 + 按 7.2 汇率折人民币）
4. 汇报：总 token（输入/输出拆分）→ 统计区间与调用次数 → 实际花费（本地推理为 0）→ 参考价表 → 一句口径说明。

## 云端（pro 等付费模型）消耗归因：谁在用、为什么用、是不是某工具导致

用户问"为什么我的会话在持续消耗、而且走的是云端 pro"或"是不是 XX 导致的"时，按下面顺序取证，不要凭印象归因。

1. **先排除被点名的工具（如 OCR 审查）**，三条都查，缺一条不能下结论：
   - `pgrep -af "opencodereview|ocr_runtime|ocr review"`（**别只写 `pgrep -af ocr`**：OCR 审查的真实进程名是 `opencodereview` 二进制与 `ocr_runtime.py`，只匹配 `ocr` 会漏掉正在跑的那一个，得出"无 OCR 进程、不是它"的错结论——本环境已实际踩过）。反向也要滤假阳性：VSCode 的 `code --enable-features=...ScreenAIOCREnabled` 与微信常驻 `wxocr` 命令行里都含 `OCR`，宽匹配会刷出一屏无关进程并误判"OCR 在跑"，再叠 `grep -viE "vscode|ScreenAIOCR|wxocr|code --"` 才准；
   - `cat ~/.opencodereview/config.json`：该文件同时列出 `vllm-local` 与 `hermes-sub2api`/`hermes-xcoding`/`hermes-numenume` 等多个供应商条目，**条目存在不等于在用**。当前 `provider` 字段本身已不是生效依据——OCR 每批启动前经 `ocr_model_route.py select <project>` 动态选路（跟随角色路由），所以判断"这一轮 OCR 实际用什么"要看**该 run 的 `state.json` 里按 batch 记录的 `execution_route.model`**，或跑一次 select（机制与故障处置见 `hermes-model-routing-ops`）；
   - `ls -lt ~/.opencodereview/sessions | head` 看最后活动时间，与可疑时间窗对照。
   三条都为否才能明确答复"不是它"，并给出证据而不是推测。
2. **抓持有云端连接的真实进程**（决定性一步）：`getent hosts <云端域名>` 拿 IP，再 `ss -tnp | grep <ip>:443`；PID 直接指认发起方（`feishu`/`chrome`/`codex` 都是无关连接，别混）。`ps -o pid,ppid,etime,cmd -p <pid>`：若显示 `python -m cron.scheduler --external-worker-file /home/wang/.hermes/cron/external-workers/<jobid>.json`，说明是某个 cron 任务回合在调用云端，`<jobid>` 去 `hermes cron list` 对任务名。
3. **按会话归因云端 token**：`session_model_usage` 按 `session_id, billing_provider` 分组（`WHERE model LIKE '%pro%'`），再 join `sessions` 拿 `chat_id`/`source`/`title` 才能说清"这笔钱是谁花的"；`billing_provider` 为 `custom:<cloud-provider>` 的行就是云端真实计费，`custom`+`billing_base_url` 是本地 vLLM。
4. **解释"为什么白天用 pro 不用本地"**：角色路由插件 `role-workflow-runtime`（`config.yaml` → `plugins.entries.role-workflow-runtime.settings.model_routing`）决定，规则是 `choose_route(mode, metrics)`：**只看本地 vLLM 等待队列**——`running` 可读且 `waiting == 0` → 本地，否则云端；`mode=='cloud'` 直接云端不探测，`mode=='local'` 与非法的模式值各有分支。**时间窗口（原 `8 <= hour < 21`）与流水线忙闲两道闸门已按用户要求移除**，不要再拿"白天窗口"或"流水线在跑"解释云端选路。运行态在 `~/.hermes/runtime/model-routing-state.json`（`mode`: auto/cloud/local + `updated_at`）。只有**绑定了角色的会话与后台任务**会被选路（`api_identity` 按 ROLE-BINDINGS.json 的 `chat_id` 或 cron 的 `poll_job_id` 匹配）；未绑定的会话（用户 DM、临时群会话）保持各自记录的 `model`。
   - **用户说"已经把模型切成 X（如 flash）了，为什么还在跑 pro"时**：角色会话的会话级 `model` 字段会被 hook 覆盖，不生效；生效项是 `model_routing.cloud_model`（顶层 + `cloud_profiles.<profile>`，profile 值覆盖顶层）。切法、耦合点与验证见 `hermes-model-routing-ops`——不要只答 `/llm local` 就收尾。
   - 切走云端用 `/llm local`（本地优先、**忙时仍走云端**）；`/llm auto` 与非绑定会话一样按本地队列判定，队列空时白天也会用本地；若 `model-routing-state.json` 的 `mode` 被钉在 `cloud`，任何负载判断都不发生（此时"为什么还在烧云端"的答案就是它，切回 `auto` 或 `local`）。
5. **长 cron 回合 = 持续烧云端 token 的最常见原因，但"长"不等于"卡死"**：`sessions` 里 `ended_at IS NULL` + `last_activity_description='receiving stream response'` + `hermes cron list` 该任务 `Execution: running <file-id>`。**`Execution: running` 加上下一轮已调度（`Dispatch: on time`）只说明本回合还在跑，不构成卡死证据**——先隔 2~3 分钟做两次采样，比 `message_count`/`tool_call_count`/`api_call_count`/`input_tokens`/`last_activity_at` 是否仍在增长：仍在增长 = 正常长回合（汇报"已跑 N 分钟、调用 M 次、token X，仍在推进"，可提示这是正常回合耗量的数倍）；计数与 `last_activity_at` 同时冻结数分钟 = 真卡死。别把一个 40+ 分钟的正常长回合报成卡死。
6. **用户批准 kill 后，先做目标确认再动手**：`ps -o pid,ppid,etime,stat,cmd -p <pid>`。长回合经常在"批准"与"执行"之间自己收尾（`sessions.end_reason='cron_complete'`、cron 标 `ok/completed`、进程已不存在）。此时**必须显式说明"未执行 kill，回合已自行结束"**，并给出该回合实际用料（按模型/调用数/输入输出 token）与终态（正常收尾 vs 崩溃），不要让用户默认是被停掉的；同时确认"下一轮已按计划重新调度"和"无残留云端连接"（`ss -tnp | grep <云端 ip>:443`，只剩 gateway 主进程属正常）。
7. **判断某轮 cron 是否正常收尾，读 `~/.hermes/cron/executions.db` 的 `executions` 表**（按 `job_id` 查，`status`/`started_at`/`finished_at`/`error`）：这比 `hermes cron list` 的当前快照更硬——已完成回合在里面有明确的 finished_at；**缺失某分钟的记录 = 该次 fire 被串行保护跳过**（上一轮未结束时下一轮不并发），不是丢任务。判断"现在是否有回合在跑"用 `ps -eo pid,etime,cmd | grep hermes_cli`。
8. **用户问"那个长了 N 分钟的回合到底在干什么"时，从 `state.db` 的 `messages` 表重建逐时工具时间线**（不要只丢一句"在跑正常"）：
   ```python
   import sqlite3, json, datetime, re
   c = sqlite3.connect('/home/wang/.hermes/state.db')
   cols = [r[1] for r in c.execute('PRAGMA table_info(messages)')]
   rows = c.execute(f"SELECT {','.join(cols)} FROM messages WHERE session_id=? ORDER BY timestamp", (sid,))
   # 每行 tool_calls 是 JSON 数组；用 re.search(r'"path"\\s*:\\s*"([^"]*)"', args) 抽文件路径、
   # '"command"' 抽 shell 命令，按时间聚合即可看出每段在干什么；
   # 后半段若出现 assistant 消息以“[CONTEXT COMPACTION”开头，说明中间发生了上下文压缩（耗时也来自这里）。
   ```
   归因时段时同时对照 `session_model_usage` 的 `first_seen`/`last_seen`：耗时大头常是"多段串行工具往返 + 某一段反复试错（如租约 claim/recover 被拒后读源码再重试）"，要按段给结论。
9. **用户批准要停 OCR 审查时：先看进程树，再决定动不动配置**。`ps -eo pid,ppid,etime,cmd | grep -E "opencodereview|ocr_runtime|ocr review"` 通常给三层：`ocr_runtime.py _worker --run <dir>` → `node .../bin/ocr review ...` → `.../opencodereview review ...`；`kill -TERM` 这三个 PID，杀完复验"进程已消失"+"该云端连接已消失"（只剩 gateway 主进程），并从 `<run dir>` 看产物是否为空（`b01.log`/`service.log` 为 0 字节 = 没出结果，这次审查作废、需重跑），把这个代价如实报给用户。
   **关键：OCR 用哪个模型由命令行 `--model` 决定，它会覆盖 `~/.opencodereview/config.json` 里的 profile 模型。** 所以一次已启动的 run 即使配置文件被改成便宜模型，仍按命令行里写死的模型继续烧云端——"改配置"拦不住在跑的 run，只有 kill 有效；反过来，看到 config 已是目标模型也**不能**推断在跑的 run 就是目标模型，必须读进程命令行确认。归因/处置前先读命令行，别只读配置文件。

## 规则

- **默认口径是云 API 参考价，不是自建机器电费**：用户问"换算成多少钱"时，先给云 API 参考价；不要自行转向"A100 功耗×电价"估算口径——曾被用户明确打断纠正。只有用户点名要机器/电费成本才按功耗估算（需另查 vLLM 所在机 GPU 数量与功率，且该机器常多负载共享，只能给分摊区间，不能说成精确值）。
- 输入 token 占绝对大头（大系统提示词每轮重发，平均每次调用输入十万级）：参考价基本由输入单价决定，汇报时点明这一点，避免用户对输出占比产生误解。用户追问"为什么输入不是输出"时，机制是 Agent 多轮工具循环：每次调用把完整对话历史 + 全部历史工具结果（测试输出、文件内容、diff）重发为输入，只输出下一步指令（本环境实测平均输入约 15 万/次、输出约 700~800/次，可按当日行重算佐证）；开发类任务（大量命令/读文件）正是输入膨胀最快的场景。
- 用户问"有没有缓存"：本地 vLLM **有 prefix cache**，命中率查引擎日志（见 llm-service-load-check 的 `36.152.27.114:/tmp/vllm_qwen38.log` 中 `Prefix cache hit rate` 行，经 a100-wzd 二次 SSH 获取），本环境常态 97%+。口径必须分清：prefix cache 救的是 GPU 负载（实际重算的 prefill ≈ 输入×(1−命中率)），**不救账单**——API 返回的 `input_tokens` 按原值记账，本表 `cache_read_tokens` 恒为 0（vLLM 不上报）；若真走云 API，其 prompt cache 命中部分另享折扣，参考价仍会偏保守。嵌套 SSH 远程 grep 时，模式里避免括号等 shell 特殊字符（会被外层 bash 当语法报错）。
- 该表只覆盖 Hermes Agent 本体（本机 `~/.hermes/state.db`）；OCR 审查、devflow worker 等其他进程调同一个 vLLM 的用量不在表内，用户问"总共"时主动声明这个边界，可顺带提及但不混入合计。
- 全中文汇报；token 量用"约 X 亿/X 万"口语化 + 精确值括号。
- **同一环境可能有并行会话在改同一批配置**：若发现 `~/.opencodereview/config.json`、`~/.hermes/config.yaml` 等已经是"你想要的"值，先 `stat -c '%y' <file>` 看 mtime 与自己的操作时刻对照，再决定口径。汇报处置结果时把"我 kill 的进程"和"别人改的配置"分开写清，不要把另一个 agent/工作目录（本环境 `/home/wang/work/.local/*model-routing*/` 这类）的改动说成自己的处置——用户会据此判断谁在动他的系统。
