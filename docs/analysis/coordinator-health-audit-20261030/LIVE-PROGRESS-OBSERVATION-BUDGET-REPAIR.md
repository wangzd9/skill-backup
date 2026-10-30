# 协调者只读进度入口：嵌套观察摘要超限修复

日期：2026-10-30，Asia/Shanghai（+08:00）。发布版本 **0.16.1**。

协调者的“当次进度核验未完成（只读入口异常）”有真实程序错误：**观察健康分层中的通知明细超出原摘要预算，导致进度查询和结构化报告回退都失败。** 已修复并热加载；当前业务账本核验与历史 checkpoint 的时间语义保持独立。

## 原因与影响

- 工具回执和错误日志确认，19:11:27、19:47:57 的 `role_live_progress` 抛出 `OBSERVATION_METADATA_EXCEEDS_BYTE_BUDGET`，调用链为 `live_progress → saved_health → health_view`。格式回退再次调用同一查询，因此也未能取得当前事实。
- 0.16.0 为通知补齐全量聚合及有界明细后，现场 `health_layers` 为 **11,584 bytes**，其中 `notification_health` 为 **8,783 bytes**。旧 `health_view` 只缩减顶层原因/类别等数组，却将整个 `health_layers` 原样放入小于 8 KiB 的摘要；即使删光可缩减项也无法满足预算。
- 这是观察摘要的表示错误，不证明业务任务失败或角色没有工作。原始观察依然为 UNVERIFIED；读取旧 checkpoint 只能给带时间的参考，不能替代本次核验。

## 实际修复

`observation_view.health_view` 现在递归处理嵌套元数据：小的总数、状态和处置口径内联；大的数组、子对象或长文本替换为精确项数、类型与 JSON Pointer。所有引用定位到同一份完整、不可变、带 SHA-256 的 `full_observation`，不另存一份丢失细节的原件。特殊键的 `/` 与 `~` 按 JSON Pointer 转义。

活动策略及超长来源/诊断同样受预算约束。旧版大投影再次进入时，从校验过的原件重新投影，避免将摘要当成原始观察，错误地把 anomaly/source 总量重计为零。输入对象不被修改；原件损坏、符号链接或哈希不符继续拒绝，不吞掉证据错误来伪装查询恢复。

源码提交：`cc880b738bfdef712f0e9c0e6dc88b451435fe30`，父提交 `4efd7b7859e956d828c242f66ad1e0f34d04be5a`。用户/开发指南更新为 3.29。

## 验证及上线

- **64 项相关回归通过，28.924 秒**，覆盖真实隔离账本、原生工具 handler、报告消费者和格式回退、UTF-8 预算、未知通知计数、完整证据重建、特殊键、旧投影及激活别名。编译和差异检查通过。
- 同一份现场快照离线对比：旧函数复现原错误；新摘要为 **5,848 bytes**，保留 **1,563** 条异常的准确总数以及 UNKNOWN 15、SENT 269、SUPPRESSED 88 的通知状态计数，完整原件内容/哈希复核通过。状态仍为 UNVERIFIED。
- **20:00:24**，Gateway **PID 3612975** 通过原生热加载 ACTIVATED，模式 `future_calls_only`；**68 项源码 SHA 全部一致**，文件后端 ACTIVATED，调度钩子 COLLECTED，飞书 connected。无需重启，已进入的旧帧仍按原身份收口。
- **20:01:07 / event 14249**，维护会话只读调用生产 `role_live_progress` handler：返回 **7,723 bytes**，用时约 **1.15 秒**，包含 **6 个任务和 5 个角色**。附加观察摘要 **5,901 bytes**；报告消费者和七分区格式均通过。查询用了原始业务账本和当时新鲜的保存观察，不用旧 checkpoint 冒充当前事实。

本次 handler 核验是维护调用，结合 Gateway 同 PID 激活证据证明修复已安装并可运行；没有声称已经发送新的群汇报或自然角色回合已经消费该结果。没有调用模型、发送通知、ACK、派单、重试业务测试或改写验收结果。当前观察未核实项和 UNKNOWN 通知继续保留。

脱敏核验记录见 [发布记录](live-progress-observation-budget-rollout.json)。机制源码、指南和本记录同步至既有 [GitHub 仓库](https://github.com/wangzd9/skill-backup)，原始运行数据库、观察明细与会话正文不导出。
