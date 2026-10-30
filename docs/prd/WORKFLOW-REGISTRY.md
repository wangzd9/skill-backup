# 需求登记与新机制接续

**2026-10-30 阅读校正：** 下文分类、数量、控制和事件号是 2026-10-22 登记记录，不是当前“未开发清单”。本轮归属与入口见 [README](README.md) / [规划规则](PLANNING.md)，有采样时间的范围核对见 [整理报告](../analysis/prd-reorganization-20261030/REPORT.md)。现行自动接纳、本地合入与最终验收以 [用户指南](../ROLE-WORKFLOW-USER-GUIDE.md)和有效运行合同为准。

2026-10-22。本目录已按固定五角色业务账本适配。文档清单在 [README](README.md)，每包的 `WORKFLOW-ADAPTER.md` 给出范围、映射及启动消息，`REGISTRATION.json` 给出登记合同与批次身份。最终登记事件、哈希和运行状态读取账本/实际回执；本文不维护实时进度。

## 登记不等于启动

本次是用户在 IDE 授权的**整理和登记**，不是飞书开发消息。登记来源如实保存为 IDE 维护来源，不伪造 chat_id、用户消息 ID 或“全部权限”。不选择新 run、不恢复旧需求、不发布计划、不调用模型或审查，也不产生合入/生产授权。

| 登记类别 | 内容 | 后续入口 |
| --- | --- | --- |
| 待启动开发需求 | 自建、NVIDIA、场景基础、场景 UI、水雾、A100 双节点、NVIDIA 清理、自建 clip 清理 | 已有 `prepared-<需求目录名>` run，初始 PAUSED、无计划。按本包适配页发真实开发消息，复用此 run 接入，不另建同名批次 |
| 已有运行批次 | 原始采集 | 复用 `raw-collection-cutover-20261021`；原任务/attempt/候选/检查点和暂停保留，不根据 TODO 重新建任务 |
| 参考/运维需求 | 无人化总方案、Hermes 模型切换 | 登记来源及范围，无虚构开发 task/run；实施新目标先补本期任务与 AC，再受管登记 |
| 历史与兼容入口 | archive、旧集成根、顶层兼容链接、水雾 runs 子批次 | 继续追溯，不重复登记为独立开发需求、不拆成第二套可写状态 |

场景基础、UI、水雾的新 run 使用各自独立需求根；原 `rosbag-catalog-integration` 仅作存量证据来源。原文没有被复制成新业务状态，也没有把旧已完成任务导入成 PENDING。旧 pool 的 LEGACY 兼容路径不用于 ACTIVE 新 run 的执行身份。

## 用户怎样开始

1. 在协调者群只读查询对应包的登记 run、当前选择、历史接受证据、实际代码仓及资源冲突。
2. 使用该包 `WORKFLOW-ADAPTER.md` 中的开发消息。明确“开发……自动推进”，带需求 ID/根、已有 run、精确任务与限制。协调者使用 `accept_requirement`，冻结 `REGISTRATION.json` 的全部 `contract_files`。
3. 核对真实回执：已登记 run 被复用，需求已接受、选择生效、OCR 有限策略已登记，然后后台出现计划/work/execution。登记前的“收到”不是开工证据。
4. 运行后可用 `暂停当前需求`、`恢复当前需求`、`继续当前需求`。切换已有 run 用精确 ID；切换不会自动解除 PAUSED，也不会补发 OCR/合入授权。

**不要先“恢复当前需求”再开始新需求。** 接入前“当前”可能仍是暂停的原始采集。

自建/NVIDIA/场景三包有多年轮次或历史 DONE/DEFERRED，开发消息必须列本批精确任务 ID；接入时可从登记目录缩小范围，不得扩大。整包“继续做完”不能被解释为重做所有旧任务或执行生产阶段。其他已有规划的包也按本期合法开发叶任务推进，TESTER/协调里程碑不得发布为 A/B/C 开发任务。

有界 OCR 策略在真实自动开发接入时起算：72 小时入场、最多 `min(20,2×本批任务数+2)` 个 action，每个合法 action 独立执行期限最多两小时。登记不会提前占用窗口。当前本地合入策略按用户指南：明确自动推进且未指定分支、未限制合入时，可使用受管 workflow 分支；main/master 或用户保留的候选/分支限制按现有授权核对。旧登记说明不要求逐 task/attempt 重复许可。生产、远端作业/删除、NAS 写入、额外付费强审和跨仓开发仍按原边界。

## 合同和历史状态

业务运行权威是 `business.sqlite3`，需求/设计/验收是业务合同，Markdown TODO 是任务目录与历史阅读视图。旧文档中的“唯一 TODO”“尚未绑定”“本回合仅文档授权”及 claim/run/release 等流程是原时点说明。新入口明确覆盖运行接入，不抹除旧事实，不自动续权或把图标转成机器状态。

登记将合同原字节按 SHA256 冻结到 runtime 的 requirements 目录，并记录 `REQUIREMENT_PACKAGE_REGISTERED` 事件。合同内容变更需审阅后重新登记，再启动；不能悄悄让旧登记接受新 AC。运行中已冻结计划的调整走原有受控 scope/返修/基线更新入口。

开发者在程序分配的私有 Git 工作区开发、自测、提交；协调者发布计划并通过服务落审查背景/交付摘要；TESTER 对固定候选独立验证。环境不可用、收集失败或零测试不判代码 FAIL。未知副作用保持围栏，先对账再接续。你无需逐角色启动、手写 background、ACK 或修改 SQLite。

## 维护与恢复

本次备份、登记计划、隔离验证及实际回执位于 [/home/wang/work/.local/prd-workflow-registration-20261022](/home/wang/work/.local/prd-workflow-registration-20261022)。恢复文档/源码可按该目录记录操作；备份业务库只作比对，不能覆盖后续已提交事件、任务或通知。取消登记也不能删除既有真实执行与历史证据。

完整日常操作见 [用户指南](../ROLE-WORKFLOW-USER-GUIDE.md)。A100 的分步人工验流程见 [专项启动清单](rosbag-catalog-a100-multi-node/START-WITH-NEW-WORKFLOW.md)。

## 本次实际登记回执

以下为 2026-10-22 登记快照，之后运行状态应再查询账本。事件号为各包最后一条登记记录；自建 TODO 的并发更新及模型选路原始 README 来源已补登记，旧记录保留。

| 需求 | 登记 run | 快照控制 | 登记事件 |
| --- | --- | --- | --- |
| [vla-self-dataset-pipeline](vla-self-dataset-pipeline/WORKFLOW-ADAPTER.md) | `prepared-vla-self-dataset-pipeline` | PAUSED | 1511 |
| [vla-nvidia-dataset-pipeline](vla-nvidia-dataset-pipeline/WORKFLOW-ADAPTER.md) | `prepared-vla-nvidia-dataset-pipeline` | PAUSED | 1495 |
| [rosbag-catalog-scene-foundation](rosbag-catalog-scene-foundation/WORKFLOW-ADAPTER.md) | `prepared-rosbag-catalog-scene-foundation` | PAUSED | 1497 |
| [rosbag-catalog-scene-ui](rosbag-catalog-scene-ui/WORKFLOW-ADAPTER.md) | `prepared-rosbag-catalog-scene-ui` | PAUSED | 1499 |
| [rosbag-catalog-spray-pipeline](rosbag-catalog-spray-pipeline/WORKFLOW-ADAPTER.md) | `prepared-rosbag-catalog-spray-pipeline` | PAUSED | 1501 |
| [rosbag-catalog-a100-multi-node](rosbag-catalog-a100-multi-node/WORKFLOW-ADAPTER.md) | `prepared-rosbag-catalog-a100-multi-node` | PAUSED | 1503 |
| [rosbag-catalog-nvidia-cleanup](rosbag-catalog-nvidia-cleanup/WORKFLOW-ADAPTER.md) | `prepared-rosbag-catalog-nvidia-cleanup` | PAUSED | 1505 |
| [rosbag-catalog-self-built-clip-cleanup](rosbag-catalog-self-built-clip-cleanup/WORKFLOW-ADAPTER.md) | `prepared-rosbag-catalog-self-built-clip-cleanup` | PAUSED | 1507 |
| [rosbag-catalog-raw-collection](rosbag-catalog-raw-collection/WORKFLOW-ADAPTER.md) | `raw-collection-cutover-20261021` | PAUSED | 1508 |
| [vla-unattended-workflow](vla-unattended-workflow/WORKFLOW-ADAPTER.md) | `参考项，无开发 run` | REFERENCE | 1509 |
| [hermes-model-routing](hermes-model-routing/WORKFLOW-ADAPTER.md) | `参考项，无开发 run` | REFERENCE | 1512 |

[整理与登记报告](../analysis/prd-workflow-registration-20261022/REPORT.md)。
