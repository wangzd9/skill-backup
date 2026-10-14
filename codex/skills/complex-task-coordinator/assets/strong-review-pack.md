# 强模型审查包模板

协调者为已完成开发和常规测试、存在明确审查问题的功能批次生成一份主文档。冻结已有需求/AC，引用真实代码和证据；不复制整套历史文档，不引导新增需求。

```markdown
# {{review_id}} 强模型审查包

## 1. 身份与门禁

| 字段 | 值 |
|---|---|
| review_id | {{唯一标识}} |
| review_type | PRE_MERGE / STAGE；CONTRACT仅限用户明确要求 |
| scope | {{任务和模块范围}} |
| gate | {{暂停的契约消费/合入/集成/工程闭环/生产门禁}} |
| base / target | {{实际 commit；契约审查不适用项说明}} |
| integration_candidate | {{固定只读集成候选分支/代码树；单分支时说明不适用}} |
| contract_version / semantic_hash | {{实际版本和可复算语义哈希}} |
| requirements_baseline | {{已有需求/AC与环境/阶段；文档版本或内容哈希}} |
| delivery_status | {{功能已交付及常规测试证据；不以占位实现作为完成}} |
| trigger_decision | REVIEW_NOW；{{具体风险事实、待回答问题、不变量、决定的门禁}} |
| invocation | MANUAL / AUTHORIZED_CODEX；{{授权记录与调用时再核对的期限/任务范围}} |
| package_status | READY_FOR_REVIEW；不因准备完成自动取得调用授权 |
| suggested_result_path | {{独立结果文档路径}} |

## 2. 目标、不变量与非范围

- 必须成立：{{行为及 AC}}
- 已交付功能与当前完成条件：{{冻结行为；不可借审查新增要求}}
- 触发本次强审查的风险：{{具体事实}}
- 非范围：{{明确排除}}

## 3. 变更地图与调用链

- 完整文件清单：{{路径及职责}}
- 调用链：{{入口 → 关键状态/数据 → 输出}}
- 重点位置：{{文件:行或符号}}
- 完整 diff：{{可定位命令或只读产物路径}}

## 4. 真实代码材料

{{关键代码片段、必要前后文、关联接口/DDL/状态定义。不得只写摘要。}}

## 5. 验证证据

| evidence commit / target关系 | 环境 | 命令/退出码 | 关键断言或结果 | 证据路径 | 未执行项 |
|---|---|---|---|---|---|

## 6. 弱模型 OCR 与待裁决风险

- 已确认并修复：{{问题 ID、修复 commit、验证}}
- 经证据排除的误报：{{结论和依据}}
- 未覆盖：{{文件/行为和原因}}
- 待强模型裁决：{{聚焦疑点}}

## 7. 强模型输出要求

逐项输出：问题 ID/分类/严重级别、对应既定需求/AC、预期与实际行为、代码位置及证据、触发条件、后果、最小修复边界、必要验证、阻塞门禁及解除条件。
分类为 CORRECTION / VERIFICATION_GAP / OUT_OF_SCOPE；范围外建议单列，默认不派单、不阻塞、不降级PASS。只纠正原功能；问题关闭且原AC满足即收口，不新增功能或泛化改造。
疑点证据不足时标待验证；不得虚构运行结果、直接修改代码、合并或部署。
总体结论：PASS / PASS_WITH_FOLLOWUPS / REWORK_REQUIRED / INSUFFICIENT_EVIDENCE。
明确作用范围：契约 / 模块合入 / 集成 / 工程闭环 / 生产门禁。

## 8. 协调者登记（结果返回后填写）

- result_path / received_at：
- result_snapshot_match：
- target_or_contract_changed_since_review：
- verified_findings / false_positives / unresolved：
- correction_ids / verification_gaps / out_of_scope_not_dispatched：
- original_AC_satisfied / remaining_blockers / close_condition：
- token_and_cost：{{实际值或未知}}
- grant_id / call_id / rolling_60m_count / authorization_scope：{{手动审查写不适用；自动调用取脚本账本}}
- rework_round / incremental_review_scope：
- decision / affected_gate / independent_work_continuing：
```
