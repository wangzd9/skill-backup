# 协调者进度：旧审查误列当前待办的修复

日期：2026-10-30，Asia/Shanghai。源码基线 `e9cff36492437f1ae50a3189d21a448ad31e286c` / 0.15.0；已上线版本 0.15.1，提交 `9d8c3a67adfb00d6aadf408346c1a9e435c94d31`。

用户提供的 17:41 汇报列出三项“待处置审查”，同时说“未列出打开门禁”。本轮只读核对确认：这不是当前又启动了三次 OCR，而是进度投影把旧候选的 INCOMPLETE 当成当前待办。

## 已核实原因

| 汇报动作 | 原候选 attempt | 17:45 当前 attempt | 原候选状态 |
| --- | --- | --- | --- |
| mn03-c1-ocr-001 | MN-03 a1 | a9 | SUPERSEDED |
| mn03-a4-ocr-suppl-001 | MN-03 a4 | a9 | SUPERSEDED |
| mn04-a2-ocr-001 | MN-04 a2 | a7 | SUPERSEDED |

同次核查还发现 MN-04 a2 补审及 a3 的另外两条历史 INCOMPLETE，共 5 条。当前 MN-03 a9、MN-04 a7 已有各自的 OCR PASS 登记；这不代替最终验收和未关闭问题处置。

`workflow_query.live_progress` 原先收集本 run 全部历史审查，核心仅按 SUBMITTED/UNKNOWN/INCOMPLETE 筛选；没有附 attempt、候选状态或当前版本绑定。`render_live_progress_fallback` 又按动作名顺序取前三条，把 INCOMPLETE 统一写成“结果落账后由协调者裁决”。因此遗留项持续占据当前审查区，真正 RESULT_READY 反而没有进入原核心筛选。

当前查询在 17:45 已保留 event:14173 的补验异常门禁；用户贴出的 17:41 汇报没有反映它，不能据那份报告认为没有风险。另有 TESTER 已切换工作单但仍展示前一补验工作的长 checkpoint；日期虽标历史，正文仍会混淆当前工作。

## 实现边界

- 新 `review_action_projection` 按 run/task/attempt/candidate 绑定分类，不从 action_id 命名猜版本。原始动作、验证和 FAIL 关闭关系不修改。
- 当前 RESULT_READY 是“审查待裁决”；INCOMPLETE 是“审查未完成，需核原因并决定恢复或处置”，不凭该状态生成业务 PASS/FAIL。
- 旧候选 INCOMPLETE 统计为历史遗留，完整行和原状态保留在查询 facts 哈希附件，不显示成当前候选待裁决。不意味着自动关闭历史故障或免除当前合同。
- 旧候选及跨需求的 SUBMITTED/UNKNOWN 继续显示、继续受全局 OCR 串行围栏约束；当前结果准备就绪和失败也保留责任及下一步。
- 进度列当前候选 OCR PASS 登记数量，不把它描述为最终验收通过。历史强审动作与当前强审待办分开，不把“没有当前待办”误写成“从未登记”。
- 风险、下一步同时显示当前补验异常、协调者责任、原生派单/退出/复验前提；不能只写“MN-07 集成验证中”。
- 前一工作单 checkpoint 仅留原时间和事件引用，完整原文仍在历史附件，不把旧单事件分页叙述粘贴成当前进展。
- 七区、通知时点、业务权限、模型配置、原测试/OCR/强审及合入门禁均按既有机制。维护会话不发送替代群报、不自动重审或代写业务裁决。

用户指南与开发流程指南更新为 3.27，图示生成源和受影响 SVG 同步。显示规则 Skill 同步约束原候选/历史边界。

## 验证与上线

- 隔离回归共 233 项通过，耗时 203.272 秒。覆盖真实 Git/SQLite 的候选返修与替代关系、当前 RESULT_READY/INCOMPLETE、历史 UNKNOWN 串行围栏、补验异常风险与下一步、角色换单后的旧检查点，以及历史强审记录。首次扩大回归发现第四条当前审查会被旧三条上限静默丢弃，修正为最多四条且显式列余项数及状态分布后，完整重跑通过。
- compileall、diff --check、指南本地链接和 SVG XML 核验通过。没有为本次汇报修复重跑生产业务测试。
- 运行时提交已发布至规范仓库 `feature/role-workflow-v3`。2026-10-30 17:56:25 +08:00 经原生插件重载验证：Gateway PID `3612975`，running、飞书 connected；同 PID 的激活回执为 ACTIVATED/future_calls_only，61 个源文件 SHA 全部吻合，新增 `review_action_projection` 已纳入只读查询绑定。受限文件后端 ACTIVATED，scheduler hook 同 PID。未重启或中断在途执行。
- 17:56:38 的只读现场查询水位为 event `14187`，核心响应 7,840 UTF-8 字节，仍在 8 KiB 契约内。当前审查待办为空；5 条历史 INCOMPLETE 分别归于 MN-03（2 条）、MN-04（3 条），不再渲染为“待处置审查”。原状态和证据保留在完整事实附件。
- 同次查询的当前候选 OCR PASS 登记为 5/5；MN-04 已进入 a8 返修，尚未提交新候选，因此不能沿用旧报告的 6/6 作为当前统计。报告明确列出 `VERIFICATION_EXCEPTION_REPAIR_PENDING` 风险、COORDINATOR 责任和后续固定候选复验/显式关闭条件。

17:56:58 的后续只读核验还确认，实际协调者已登记 repair 处置 event `14184`，原生程序据此创建 MN-04 attempt 8，返修派单事件为 `14185`；任务已为 DEVELOPING，owner 仍为 A。这次是真实新 attempt，不再只是检查点中的 a8 文字。旧补验已按原生安全退休路径退出，异常 event `14173` 仍保留 UNVERIFIED/REPAIR_QUEUED。返修交付、独立验证、按合同审查与合入、补验和显式关闭仍须完成；本轮维护没有代写业务裁决、修改验收门禁或发送替代群报。

脱敏版本与现场采用摘要见[上线核验](review-progress-rollout.json)。源码、显示 Skill、指南和流程图纳入既有 GitHub 机制快照；原始生产快照、checkpoint、数据库和凭据不导出。
