---
name: hermes-usage-cost-stats
description: 查 Hermes 累计 token 与成本：state.db 用量表 + 云 API 参考价口径。
---

# Hermes 用量与成本换算

用户问"Hermes 累计用了多少 token / 换算成人民币多少"时的标准查询与汇报流程。数据在 `~/.hermes/state.db`。

## 步骤

1. 查询（用 python3 + sqlite3 模块；机器上没有 sqlite3 CLI，别用）：
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

## 规则

- **默认口径是云 API 参考价，不是自建机器电费**：用户问"换算成多少钱"时，先给云 API 参考价；不要自行转向"A100 功耗×电价"估算口径——曾被用户明确打断纠正。只有用户点名要机器/电费成本才按功耗估算（需另查 vLLM 所在机 GPU 数量与功率，且该机器常多负载共享，只能给分摊区间，不能说成精确值）。
- 输入 token 占绝对大头（大系统提示词每轮重发，平均每次调用输入十万级）：参考价基本由输入单价决定，汇报时点明这一点，避免用户对输出占比产生误解。用户追问"为什么输入不是输出"时，机制是 Agent 多轮工具循环：每次调用把完整对话历史 + 全部历史工具结果（测试输出、文件内容、diff）重发为输入，只输出下一步指令（本环境实测平均输入约 15 万/次、输出约 700~800/次，可按当日行重算佐证）；开发类任务（大量命令/读文件）正是输入膨胀最快的场景。
- 用户问"有没有缓存"：本地 vLLM **有 prefix cache**，命中率查引擎日志（见 llm-service-load-check 的 `36.152.27.114:/tmp/vllm_qwen38.log` 中 `Prefix cache hit rate` 行，经 a100-wzd 二次 SSH 获取），本环境常态 97%+。口径必须分清：prefix cache 救的是 GPU 负载（实际重算的 prefill ≈ 输入×(1−命中率)），**不救账单**——API 返回的 `input_tokens` 按原值记账，本表 `cache_read_tokens` 恒为 0（vLLM 不上报）；若真走云 API，其 prompt cache 命中部分另享折扣，参考价仍会偏保守。嵌套 SSH 远程 grep 时，模式里避免括号等 shell 特殊字符（会被外层 bash 当语法报错）。
- 该表只覆盖 Hermes Agent 本体（本机 `~/.hermes/state.db`）；OCR 审查、devflow worker 等其他进程调同一个 vLLM 的用量不在表内，用户问"总共"时主动声明这个边界，可顺带提及但不混入合计。
- 全中文汇报；token 量用"约 X 亿/X 万"口语化 + 精确值括号。
