# 独立审查结果

- review_id / 审查类型：
- 触发来源 MANUAL / AUTHORIZED_CODEX；grant_id / call_id（如适用）：
- base / target / contract_version / semantic_hash：
- 冻结需求/AC、环境与阶段基线 / 已交付功能范围：
- 证据 commit / 环境 / 实际执行与未执行项：
- 实际阅读范围 / 未覆盖范围：
- 结论：PASS / PASS_WITH_FOLLOWUPS / REWORK_REQUIRED / INSUFFICIENT_EVIDENCE
- 作用门禁：契约 / 模块合入 / 集成 / 工程闭环 / 生产

## 问题（每个问题单独记录）

- 问题 ID / CORRECTION或VERIFICATION_GAP / 严重级别 / 确认或待验证：
- 对应既定需求/AC或必要不变量 / 预期与实际行为：
- 代码位置及证据：
- 触发条件 / 错误后果：
- 最小修复边界 / 必要验证与受影响回归：
- 是否阻塞 / 哪个门禁 / 验证关闭条件：

## 范围外建议（没有则省略）

- OUT_OF_SCOPE / 为什么超出本批既定需求：
- 默认不派单、不进当前TODO、不阻塞、不因建议降级PASS；用户明确采用后才进入规划。

## 快照与后续

- 可复用的已接受结论及依据：
- 本轮问题关闭情况 / 原AC满足情况 / 剩余范围内阻塞项：
- 非阻塞项 owner / 阶段 / 解除条件：
- token / 费用（不可获取写未知）：
- 确认缺陷 / 核实误报 / 后续遗漏 / 返修轮数 / 重复阅读：

没有实际验证不得声称测试已执行；必要证据不足不得 PASS。结果只对以上快照和门禁有效。
