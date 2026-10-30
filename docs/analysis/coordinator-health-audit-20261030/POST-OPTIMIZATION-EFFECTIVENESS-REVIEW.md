# 优化后全局效果复核

核对日期：2026-10-30，Asia/Shanghai。主统计截止 **17:46:36**，窄范围补充核对截止 **17:52:05**；两次窗口分别保留，不把后续调用混入先前用量。源码及采用基线为 **0.15.0 / e9cff36492437f1ae50a3189d21a448ad31e286c**。

**18:12 追审更新：** 以下第 1～7 节保留原统计窗口；当前运行时已为 **0.15.1 / 9d8c3a6**，MN-04 a8 已真实返修交付进入 REVIEW，原文“尚未派单／0.15.1 进行中”仅适用于原截止时点。新增问题、当前证据及优先级见[第 8 节](#8-1812-追审当前仍需处理什么)。本轮仅补充审查记录和只读取证脚本，未修改或重启运行机制。

**整体判断：优化方向正确，基础可靠性、输入去重和异常恢复已有实证改善；协调者集中做语义裁决的路径已开始真实采用，但端到端交付闭环、持续运行健康和“质量效率不变而 token 下降”尚未证明。** 当前适合继续在可观测、可维护的范围内运行；证据还不足以据此扩大并发或认定已经能长期无人值守。

这次核对只读取源码、既有测试日志、SQLite 和只读 Gateway status；新增本报告与证据脚本。没有修改运行机制、配置、任务或账本，没有替角色 ACK、重载插件、派单、发送群报或启动业务测试/模型/审查。

证据：[主快照](post-optimization-audit/effects-summary.json)、[补充快照与源码/测试日志哈希](post-optimization-audit/followup-summary.json)。两份快照只导出状态和计数，不复制会话叙述或模型内部推理。历史报告保持其原采样口径。

## 1. 从整条流程看，哪些地方已经改善

| 流程位置 | 当前证据 | 评价 |
| --- | --- | --- |
| 合同→开发→最终验证→协调收口 | 分阶段 AC 义务、覆盖矩阵、问题阻断性及收口校验已进入源码与既有回归；现场已冻结结构化义务 | 原先“一项 AC 混合多个角色职责”的问题已有机制修复；当前需求仍未完成最终闭环 |
| 事件→接续 | 显式 ACK、正确续读边界、误置字段拒绝、无进展退避均已实现；新执行保存起始水位 | 不能再把 checkpoint 正文里的水位当真进展；旧历史欠账仍需处理 |
| 取证→判断 | 两个自然启动的新角色均包含 `role_decision_context`；补充核对时 COORDINATOR 和 TESTER 都成功取得 prepare 材料 | 从“仅存在工具”推进到“实际调用成功”；尚未证明材料已转化为正确业务裁决 |
| 异常→恢复 | 旧 UNVERIFIED 补验已登记异常、安全退出，TESTER 已领取 MN-07 | 原占槽循环的一段确已解除；返修、复验和显式关闭仍未走完 |
| 输入→模型 | 同一冻结输入完整展开相等；COORDINATOR / TESTER 字节减少 9.10% / 10.22% | 可恢复的去重收益成立；不是实测 token 降幅 |
| 工具目录→执行 | 新 COORDINATOR / TESTER 目录不再直接提供必拒的 terminal/write_file/patch；实际新版会话未见 terminal 调用 | 已减少一类明确的误导入口；参数错误及其他机械查询仍存在 |
| 查询→存储 | 新 `work_inputs` 命令的持久化回执约 4KB，正文走不可变页引用 | 新增长方式改善；历史约 3.13GB 库及附件留存问题还在 |
| 观测→汇报 | 当前写者、采集故障、历史债和容量已分层 | 观测含义更清楚；历史审查混入当前待办、UNKNOWN 通知未进入健康摘要等缺口仍在 |

全局瓶颈已经更清楚：MN-01～06 的开发任务均为 COMPLETED，当前主要工作位于**异常处置、最终验证和协调收口**。现阶段增加开发并发，对解除眼前阻断帮助有限。应优先减少这几个后段节点的等待与重复劳动。

源码依据：[统一输入与快照](/home/wang/work/hermes-role-runtime/workflow_runtime.py:449)、[ACK 接续判定](/home/wang/work/hermes-role-runtime/workflow_resumption.py:5)、[输入去重](/home/wang/work/hermes-role-runtime/workflow_model_input.py:55)、[角色工具目录](/home/wang/work/hermes-role-runtime/workflow_toolsets.py:4)。历史实施与回归边界见 [0.13.1](IMPLEMENTATION.md)、[0.14.0](TOKEN-OPTIMIZATION-IMPLEMENTATION.md)、[0.15.0](SEMANTIC-DECISION-IMPLEMENTATION.md)。

## 2. 已核实上线，不能只看源码文件

- 0.14.0 的 Gateway 自身采用回执为 16:31:45，54 项源码哈希匹配；0.15.0 为 17:29:59，61 项源码哈希匹配。
- 本轮重新核对 0.15.0 记录中的 61 个哈希均与源码一致。只读 control socket 返回 `pid=answering_pid=3612975`、running、飞书 connected。
- 共享 `mechanism-activation.json` 的最新 PID 为 worker 的 `4017308`，不能把它当成另一次 Gateway 上线证明。
- 当前 COORDINATOR execution `d555cbe123814b84861f4db3ac9e0b94` 与 TESTER execution `7bedf4d4198b46ac8f08b940b7c3c9c7` 均持久记录 0.15.0 runtime digest。旧 worker 不会被一次重载回溯改写，因此统计按 execution digest 分组，而非只按发布时间切分。
- 核对到 0.14.0 最终日志 **152 项 / OK**，0.15.0 `release-tests.log` **212 项 / OK**。早期失败日志与重叠测试组没有叠加计数；本轮没有重新跑测试。这证明已有隔离回归通过，不等同于当前 MN 需求验收通过。

同期已出现 [0.15.1 进度投影修复记录](REVIEW-PROGRESS-PROJECTION-REPAIR.md)，但截至本轮源码/采用核查，正式 HEAD 仍为 e9cff36，记录的最终验证和上线回执也未补齐。因此该修复列为进行中的后续工作，不能计入本报告的已生效收益。

## 3. 当前真实业务链：已恢复占槽，尚未闭环

| 时间 | 现场事实 | 能证明的范围 |
| --- | --- | --- |
| 17:30:00 | e14173 登记 MN-04 a7 的 `VERIFICATION_EXCEPTION_RECORDED`，保留 UNVERIFIED | 异常被持久识别，责任转给协调者 |
| 17:38:13 | e14175：原 TESTER 自行提交 safe_release | 角色使用了合法退出入口 |
| 17:38:57 | e14177：原补验工作 `VERIFICATION_RETIRED` | 原 work 真实 ENDED，不能仅凭 worker 结束推断 |
| 17:39:02 | TESTER 新执行领取 MN-07 集成验证 a2，使用 0.15.0 | 原占槽已解除，新工作能够被接续 |
| 17:41:59 | COORDINATOR 新执行采用 0.15.0；启动建议已列 `resolve_verification_exception` | 新异常在当前输入中可见，不必等历史事件逐页读到才首次发现 |
| 17:51:42 | COORDINATOR 的 prepare 返回 FOUND，取得该待决项材料 | 判断入口实际可用，取到材料不代表已裁决 |
| 17:52:05 截止 | MN-04 仍 a7 / COMPLETED；异常只有登记，尚无 disposition、repair queued 或 close 事件 | 尚未创建真实新返修 attempt，不能说“A 已经在修 a8” |

这次恢复相对 [16:53 的阻塞诊断](LIVE-BLOCKER-DIAGNOSIS-20261030.md) 有明确进展，原“补验不退→TESTER 被占→MN-07 不启动”的链已断开。但完整路径仍须由真实角色走完：

> 核 UNVERIFIED 证据 → 显式处置 → 若需返修则产生真实派单 → 新候选独立验证/适用审查/合入 → 同谱系真实补验 PASS → 显式关闭异常 → 最终验证及协调收口。

调查、返修或关闭由协调者根据证据选择，本报告不代写该判断。新协调者到补充采样只运行约十分钟，也不能据此直接判定它已经陷入新的长期停滞。

源码中已把异常建议前置：[异常 next_actions](/home/wang/work/hermes-role-runtime/workflow_progression.py:94)；实际返修创建新 attempt、恢复任务为 PENDING：[返修排队](/home/wang/work/hermes-role-runtime/workflow_verification_exceptions.py:244)；最终收口仍拒绝未关闭异常：[complete](/home/wang/work/hermes-role-runtime/workflow_completion.py:365)。这是恢复能力的进步，业务效果仍需完整运行证明。

## 4. 语义裁决入口已采用，但新接口还在制造纠错回合

主快照时 TESTER 共调用新工具 4 次：directory 成功；两次 contract 和一次 prepare 被 `INVALID_DECISION_QUERY` 拒绝。补充核对确认其后已纠正参数，prepare 返回 FOUND；COORDINATOR 也成功 prepare。

实际错误形态是：

- `contract` 同时带入 `decision_id` 和 `expected_fingerprint`，但运行时只接受 operation、action。
- 改成 `prepare` 后仍带 action，运行时只接受 operation、decision_id、expected_fingerprint。
- 最后移除 action，prepare 成功。TESTER 的完整材料超过内联预算，返回哈希附件引用；COORDINATOR 的材料为内联。

根因不只是“模型没照说明做”。[原生工具 schema](/home/wang/work/hermes-role-runtime/__init__.py:1177) 把所有操作的字段放在一个 properties 中，仅要求 operation；[实际校验](/home/wang/work/hermes-role-runtime/workflow_decision_context.py:256) 又按操作严格限制字段组合。于是存在“外层 schema 可接受、运行时必拒绝”的组合。当前 [错误回执](/home/wang/work/hermes-role-runtime/workflow_decision_context.py:310) 只给统一错误和泛化纠正指引，没有列出本次多余字段及合法最小参数。

**建议优先统一两层合同。** 可采用原生工具系统支持的分操作 schema；若不支持条件 schema，就在字段说明、目录返回及错误回执中明确最小调用形式、allowed/unexpected fields，并区分目录 fingerprint 与单项 material_fingerprint。保持严格拒绝未知字段；不能通过静默忽略错误参数来“消除错误率”。以现场三种失败组合补有意义的合同回归，并验证一次错误即可获得可执行的纠正方式。

这类调整不减少协调者的判断材料，也不改变模型能力，是本轮最直接、风险较小的 token 和回合节省空间。不能把 3 次错误除以 4 次调用，推广成全系统长期错误率。

另一个正向结果是成本关联开始落地：17:46 只有 2 份 work 目录观测；17:52 已有 **2 execution、4 request** 关联，4 个物理请求均能在 role-usage.db 找到且 execution 匹配，其中 2 个已有用量、2 个仍在途。因此此前“只有 works 文件”不能认定指标失灵。但现在只有最小关联，尚无完整交付成本、质量或等待时延聚合。[关联实现](/home/wang/work/hermes-role-runtime/workflow_decision_metrics.py:20) 保留共享上下文口径，不按多个 decision 重复拆费。

## 5. token：有减负证据，还没有无损节省结论

最可信的定量结果是[同一冻结输入的还原对比](token-optimization-comparison.json)：

| 角色 | 原始 bytes | 优化后 bytes | 字节减少 | 完整展开 |
| --- | ---: | ---: | ---: | --- |
| COORDINATOR | 199,464 | 181,313 | 9.10% | 逐字段相等 |
| TESTER | 95,404 | 85,649 | 10.22% | 逐字段相等 |

它证明重复传输减少且原件可恢复。模型是否需要额外展开引用、展开引入多少新请求，以及实际分词后的差值，还需用真实请求衡量。

本轮 12:00～17:46:36 的 business_cron 物理请求，按运行版本及角色分组：

| 运行版本/角色 | 物理请求 | 有用量请求 | 已知输入 token | 每请求输入 P50 |
| --- | ---: | ---: | ---: | ---: |
| 0.14.0 / COORDINATOR | 64 | 64 | 7,625,959 | 116,422 |
| 0.14.0 / TESTER | 55 | 55 | 5,047,864 | 81,018 |
| 0.15.0 / COORDINATOR | 2 | 1 | 103,549 | 103,549，单样本 |
| 0.15.0 / TESTER | 5 | 4 | 322,023 | 82,295.5 |

这是物理输入量，不是计费金额，也不是“每完成一项业务”的成本。缺失用量仍为未知，未计作零。各组均走本地路由，缓存读取覆盖未知；已记录请求未见额外物理重试，不代表所有逻辑工作没有重复。

同日各组**首请求**输入的中位数也说明不能简单宣称全面下降：

| 角色 | 旧版/未映射 | 0.14.0 | 0.15.0 |
| --- | ---: | ---: | ---: |
| COORDINATOR | 123,247.5，32 会话 | 105,251，5 会话 | 103,549，1 会话 |
| TESTER | 66,526.5，10 会话 | 76,051.5，6 会话 | 66,913，1 会话 |

协调者首包变轻是积极信号；TESTER 不同阶段没有单调下降。样本、任务内容、维护 drain 和角色阶段都不同，旧版/未映射组也不是严格的同任务对照，不能直接计算因果节省率。0.15.0 唯一完成的协调者请求耗时 249 秒，同样不能据一次请求断言变慢或判断质量下降。

从源码看，0.15.0 仍保留大启动上下文，[seal_input](/home/wang/work/hermes-role-runtime/workflow_runtime.py:559) 的 32KiB 只是 requires_paging 标志，不是首包硬限；补充目录并未替换整包。主快照中新版 COORDINATOR 启动包仍为 153,083 bytes。模型配置与验收门未被裁减，但这只说明没有主动降低能力配置，不能当成语义质量等价的实证。

目前可确认“减少了一部分机械传输与误导工具”，尚不能确认原先 10%～20% token 目标已达成，也不能确认接纳到交付耗时已缩短。

## 6. 剩余全局问题，按交付影响排序

### P1：把异常处置走成真实业务闭环

现有修复已提供合法出口，当前缺的是现场走通并留下端到端证据。第一优先级应是追踪 e14173 的处置、真实派单、独立验收、复验与关闭，而不是再加开发槽位。需分别记录“等待协调者判断”“等待旧 writer”“等待 owner 开发”“等待独立验证”等时间，才能区分资源瓶颈、语义判断瓶颈和机械负担。

若最终验证与返修预期修改相同供应快照，应让协调者明确优先顺序，减少很快失效的验证。已有 [target_current 检查](/home/wang/work/hermes-role-runtime/workflow_completion.py:456) 会在版本变化后要求安全退出，但尚不能据此认定已经具备全交付链的排队与在制品控制。

### P1：修正工具参数合同，随后减少历史续读占用

参数问题见第 4 节。另有旧游标积压：主快照 COORDINATOR 有 2,940 条游标之后的相关原始事件，排除六类机械事件后为 122；TESTER 为 12,294 / 411。它们不是同数量的业务待办，也不一定都需模型逐条裁决。

补充快照中 COORDINATOR processed/issued 为 11243/11430，TESTER 为 1889/2089；发行已前进，不能据 issued 就说处理完成。新版 TESTER 仍有一次跳到未发行水位的拒绝；0.14.0 COORDINATOR 19 次输入查询中有 4 次同类拒绝。原来的大量失败已有改善信号，但问题尚未消失。

[机械事件已有合并显示](/home/wang/work/hermes-role-runtime/workflow_progression.py:63)，[输入工具已支持 events_only](/home/wang/work/hermes-role-runtime/__init__.py:497)，这两项不应重复列为从零开发。剩余机会是：在保留完整事件映射、冻结边界、独立 ACK 和业务义务的前提下，提高连续机械段的有界发行效率，并让角色优先处理当前阻断。不能直接把游标跳到最新，也不能把取到决策目录当成确认所有历史事件。

### P1：当前待办与历史审查仍有混淆，影响调度判断

本轮直接关联账本，得到 **5 条 INCOMPLETE OCR 均属于 SUPERSEDED 候选**：MN-03 a1/a4，MN-04 a2 的两条及 a3；当前 attempt 分别为 a9/a7。没有因此新启动五次审查。

当前 [live_progress 筛选](/home/wang/work/hermes-role-runtime/workflow_query.py:366) 按状态选整个 run 的历史动作，未在核心列表中区分当前 attempt，同时漏列 RESULT_READY；[fallback 渲染](/home/wang/work/hermes-role-runtime/workflow_reports.py:949) 取前三条，并把 INCOMPLETE 一律描述成待处置审查。这解释了旧审查为什么会抢占“当前待办”的显示位置，也使协调者重复核对无关历史。

应按 run/task/attempt/candidate 关联当前性，单列历史证据债，同时继续保留旧候选或跨需求 SUBMITTED/UNKNOWN 的实际写者与全局服务围栏。不能为了清爽直接隐藏真正未退出的服务，也不能把历史失败自动关闭。0.15.1 的专项修复记录已经覆盖该方向，下一步应核其真实回归、Gateway 采用和自然报告，不宜另起重复实现。

### P2：成本关联、工作记忆与按需查询仍是初步实现

- [工作记忆](/home/wang/work/hermes-role-runtime/workflow_work_memory.py:98) 的 current_verified 始终为 false，保护了历史结论边界；尚不能指望它自动免除重复核证。应先观察真实会话是否写入并复用有效条目，再优化失效范围。
- [材料 fingerprint](/home/wang/work/hermes-role-runtime/workflow_decision_context.py:17) 仍含部分 run 级 completion、controls、全局异常，依赖失效集较宽；可在证据充分后按具体判断收窄，不能先缩小而漏掉相关变更。
- [prepare/evidence 查询](/home/wang/work/hermes-role-runtime/workflow_decision_context.py:267) 内部仍组装完整 current_input；[账本快照](/home/wang/work/hermes-role-runtime/business_ledger.py:428) 已按 run 过滤，并非仍全局无差别扫描，但同 run 的事件与执行历史仍可能很大。传输减少不等于后端计算已增量化，应测查询时延后再处理热点。
- 用户原问题/选项的持久关联、跨报告/checkpoint/log 的语义结论复用、成本与质量/交付时延聚合，实施记录均仍留有边界。不能把新增五个模块认定为十二个优化方向全部完成。

### P2：健康摘要已分层，但仍不能称全绿

补充读取的健康快照时间为 **17:51:53**，采样时距其约 12 秒，排除了“只是在读很久以前的缓存”这一解释：

| 维度 | 观察 |
| --- | --- |
| 权威与当前活动 | ACTIVE，admission_open=1；当前 writer 2，unverified writer scope 0 |
| 服务观察 | 当前在途服务 0；终态服务已核验 40；不完整终态结果 7 |
| 总体健康 | unverified；7 项 collector fault 均为终态服务证据未核实 |
| 历史债 | 分层摘要记录 1,387 项；完整 anomalies 含 1,382 条 frozen execution 未核实等旧记录，不能全部当成当前 writer 故障 |
| 容量 | SQLite 3,134,615,552 bytes，CAPACITY_WARNING；历史未压缩 |
| 通知 | outbox 尚有 12 条 PERIODIC_SUMMARY/UNKNOWN，最早为 10 月 23 日；健康通知摘要为空 |

[通知 diagnostics](/home/wang/work/hermes-role-runtime/workflow_notification_registry.py:17) 只统计 PENDING，因此空摘要不能证明 UNKNOWN 已清零。UNKNOWN 不重发是正确边界，不能因缺确认就自动补发；仍应在健康视图中单列未决数量、年龄、既有出口及责任。当前证据既不能证明这些旧总结已送达，也不能证明全部丢失。

历史债归档应保留原证据、哈希和恢复路径；容量治理需同时考虑 SQLite 与不可变 input-pages 等持久附件。新格式约 4KB 的回执只证明新写放大改善，旧库不会随之自动缩小。此次没有测锁等待、压测或执行归档，不把慢请求归因为数据库。

## 7. 下一轮怎样判断“优化真正有效”

下一轮宜先完成当前链路，再用若干同类任务观察，避免继续堆功能而没有效果反馈。至少同时记录：

| 要验证的目标 | 需要的指标 |
| --- | --- |
| token 更少 | 每次成功业务状态转换及每个完整交付的全部物理输入/输出；保留未知用量、重试与返工 |
| 机械回合更少 | 参数拒绝、无效工具发现、重复材料读取、事件续读与 ACK 占比；错误后恢复所需回合 |
| 交付更快 | 各节点排队、判断、执行、writer 核退耗时；接纳到本地收口总时长 |
| 判断质量保持 | 首次验收、漏判/返工、新增缺陷、AC 覆盖及未关闭 FAIL/UNKNOWN；用户重复介入 |
| 机制可持续 | 异常是否有 owner 和实际出口；数据库/附件增长率、查询时延和观察新鲜度 |

同一物理请求只计一次；多个待决项共享上下文时不重复拆费。维护 drain、版本切换、阶段变化及未完成窗口需单列。当前最有价值的顺序是：**走通异常闭环 → 验证进行中的进度投影修复 → 修正工具合同与机械续读负担 → 补交付成本/等待指标 → 再考虑更深的启动包裁剪和并发调节。**

本轮确认的是机制更可恢复、机械负担已有减少；是否最终让协调者用更少 token 完成同等质量、更快的交付，仍须以上述真实交付结果证明。

## 8. 18:12 追审：当前仍需处理什么

本次账本、消息与指标采样截止 **18:12:43**，账本水位 `14224`；终态拒绝证据另采于 **18:12:44**。各 SQLite 连接使用 `mode=ro`、`query_only` 与独立读事务，数据库之间及文件/观察器不共用一个原子快照。新证据见[当前核对摘要](post-optimization-audit/current-review-summary.json)与[拒绝证明核对](post-optimization-audit/terminal-rejection-proof-summary.json)，生成脚本分别为 [review_current.py](post-optimization-audit/review_current.py) 和 [review_terminal_rejections.py](post-optimization-audit/review_terminal_rejections.py)。不导出消息正文、checkpoint 结论、模型推理、完整 argv、数据库或凭据。

### 8.1 已完成事项从待办中移出

- 0.15.1 已于 17:56 原生热加载，Gateway 同 PID 采用回执、61 项源码哈希和 233 项通过回归见[专项上线记录](REVIEW-PROGRESS-PROJECTION-REPAIR.md)。本次复核规范 HEAD 及记录中的 61 项哈希全部匹配；没有为追审再次重载或重跑该套回归。
- e14184 为真实协调者 repair 裁决，e14185 创建 MN-04 a8，至 18:12 已为 REVIEW。此阶段仍缺独立验证、适用审查、合入、同谱系补验和显式关闭；不能再把缺口写成“未创建 a8”，也不能宣称异常闭环已经完成。
- 新 COORDINATOR 已采用 0.15.1 摘要对应的 runtime digest；TESTER 原 0.15.0 执行已安全交接，新执行恢复原 MN-07 a2。版本效果仍按持久 execution digest 分组，不能把发布时间之后所有请求自动算作新版请求。

### 8.2 P1：工具合同需要一次性覆盖调用格式和执行前提

第 4 节指出的 decision-query 问题仍未修复：[工具 schema](/home/wang/work/hermes-role-runtime/__init__.py:1177) 与[分操作允许字段](/home/wang/work/hermes-role-runtime/workflow_decision_context.py:256) 不一致，通用错误不返回本次 unexpected/allowed fields 及最小纠正形式。

追审又发现集成测试合同缺少可见的组合约束：[verify_integration schema](/home/wang/work/hermes-role-runtime/workflow_completion_contracts.py:7) 只列 argv/report_format 类型，未说明 `junit` 必须有 `{junit_xml}`；[执行前校验](/home/wang/work/hermes-role-runtime/delivery_evidence.py:526) 则严格要求它。现场 18:05:08 首次 verify_integration 被 `JUNIT_PLACEHOLDER_REQUIRED` 拒绝，确认没有启动测试；错误虽指向 argv，但 contract 再查询仍不能直接给出这一约束。

建议把操作字段规则、pytest/junit 两种运行时管理方式、必要的交叉条件集中到同一合同来源，并在预检错误中返回具体修正方法。若外部工具系统不能可靠传输条件 schema，使用字段说明、合法最小调用示例和结构化错误补齐。不得以静默忽略字段、放宽 JUnit 取证或新建重复测试来降低拒绝率。验收应覆盖现场三种 decision 失败组合，以及 JUnit 缺占位后的单次纠正路径；无副作用的非法输入仍须明确拒绝。

### 8.3 P1：游标推进与业务有效进展需要分开

原 TESTER 执行 17:39:02 开始，期间 3 次 ACK 均成功；但两次 verify_integration 均未启动：第一次为上述参数拒绝，18:06:30 修正参数后被原安全交接阈值拦住。预算记录随后为 **22 个请求、约 133,089 输入 token**，已通过 checkpoint 让出；原工作单由新 execution 接续。后一个值是会话内输入估算的高水位，不是累计消耗 token。不能把这段观察写成“测试业务失败”或“TESTER 已永久卡死”。

至追审时 COORDINATOR 仍有 **2,520** 条未确认原始事件，排除既有六类机械事件后为 **113**；TESTER 为 **12,135 / 413**。这些计数包含历史，不等于当前待办数。[现有退避](/home/wang/work/hermes-role-runtime/workflow_resumption.py:20) 将 ACK 推进视为接续有进展，这对事件消费是合理口径，但不足以衡量完成验证、裁决或交付的业务进展。

应保留 ACK 原含义，另记录“最近真实业务状态转换、第一次有效动作、交接前未执行原因和 owner”，优先处理当前阻断。机械连续段可以提高有界发行效率，但必须保留完整 ID 映射、冻结边界、原始证据展开和显式确认。验收需要大历史积压下的实际关键动作和交接续做证据，不能只验证 issued/processed 增长；不能直接跳游标、自动确认未读事件或单纯提高会话阈值。现有证据表明机械往返和材料准备占用了窗口，尚不能把全部耗时因果归于历史续读。

### 8.4 P2：终态服务健康把“已证明未派发”误归为缺少运行回执

原报告列出的 **7 项 terminal collector fault 全部对应 REJECTED 动作，且均无服务终态 evidence**。维护会话对现存 sidecar 调用既有只读 `rejection_proof`，6 项通过、1 项不通过；sidecar action 身份及读取前后字节也已核对。通过的 6 项覆盖当前需求的旧拒绝动作和 MN-06 当前 a2 的一次拒绝；另 1 项属于历史需求，不能因年代久而自动清除。

原因是[观察器](/home/wang/work/hermes-role-runtime/role_observer.py:581) 把所有非 SUBMITTED/UNKNOWN 动作都送入[终态核验](/home/wang/work/hermes-role-runtime/workflow_terminal_observation.py:7)，后者一律要求服务运行回执；但[原生拒绝入口](/home/wang/work/hermes-role-runtime/business_ledger.py:1696) 的含义正是核实未产生外部副作用。

应增加“未派发且拒绝证据已核实”的独立观察结果，复用既有零派发证明，保留拒绝原因和业务未通过标记；有真实派发或证明失效时仍维持 UNKNOWN/UNVERIFIED。不能只按 REJECTED 枚举白名单忽略所有证据，也不能把当前 7 项全部归为可清理噪声。本轮只识别分类缺口，没有更改健康状态或关闭观察。

### 8.5 P2：通知未决状态仍不可见

账本仍有 **12 条 PERIODIC_SUMMARY/UNKNOWN**，健康快照的 `notification_health` 为空；[diagnostics](/home/wang/work/hermes-role-runtime/workflow_notification_registry.py:17) 只读 PENDING，且最多取前 256 条，既没有 UNKNOWN 聚合，也没有显示被截断的数量。

应按状态与种类聚合未决数量、最老年龄、送达确认覆盖和处置责任，再列有界样本；不能用有界明细替代总量。UNKNOWN 继续禁止自动重发，若没有平台回执，只能保留送达未知；此问题是可观测性缺口，不是已证实 12 条都丢失。

### 8.6 P2：待决材料的历史分类还没有跟上报告层

0.15.1 修正了用户报告投影，但[decision directory 的 blocker_index](/home/wang/work/hermes-role-runtime/workflow_decision_context.py:69) 仍按非 ENDED/REJECTED 汇总全部 action，行内只有 action_id/candidate_id/state，没有 attempt、候选状态或是否仍为当前义务。已确认的 5 条 SUPERSEDED/INCOMPLETE 因而仍进入名为 nonterminal_actions 的目录。

应复用候选绑定分类，分开当前待决、历史证据债和全局在途/未知围栏，保留完整行及历史失败关闭关系。[真实 next_actions](/home/wang/work/hermes-role-runtime/workflow_progression.py:142) 已筛 CURRENT 候选，不能据目录的缺标记断言系统正在自动重审这 5 条。这项优化主要减少协调者反复辨认历史的负担，不再重复实现用户报告的专项修复。

### 8.7 P2：衡量完整交付之前，补齐成本与复用口径

- 请求关联现已达到 **22 条，22 条均可匹配同 execution 的 usage**，不应继续沿用“只有 works 文件／4 个 request”的旧统计。但[request 关联](/home/wang/work/hermes-role-runtime/workflow_decision_metrics.py:43) 表示 execution 先前取得过的准备材料共享上下文，不证明后续每次请求都在处理该 decision。目录移除只更新 works 的展示状态，没有结束 execution 中的关联区间；聚合前应保留取得、失效、处置和业务转换时点，明确关联覆盖与非因果边界，物理请求仍只计一次。
- 当前 run **没有工作单写入 work_memory**。这是可选能力尚未产生实际复用收益，不能称模块失效，也不能用它宣称重复核证已减少。可先在具体重复读取场景接入历史定位和精确下一步，继续保留 `current_verified=false`，不免除必要的当前证据复核。
- `_basis` 的宽失效范围、prepare/evidence 的完整包组装和启动包裁剪仍值得测量后优化。先补每个查询的分阶段时延、读取字节、展开回合、cache 命中依据和单个完成交付总成本，再按明确依赖收窄。没有实际热点证据前，不为追求 token 比例删合同、减少验证或更换低能力模型。

### 8.8 P2：容量治理必须覆盖可恢复引用链

账本仍为 **3,134,615,552 bytes**。同轮窄范围目录统计还见 input-pages 约 **7.75 MB**、decision-views 约 **0.10 MB**、query-views 约 **55.00 MB**；这是一次容量读数，不是增长率，也未证明查询慢由这些数据造成。此前“新回执约 4KB”不能代表全部持久化成本。

应先建立 SQLite/WAL 与引用附件的统一容量、增长率和查询时延观测，再设计离线一致归档、哈希清单、引用完整性检查和实际还原验证；不在线删除、VACUUM 或仅归档数据库而遗漏附件。

下一轮优先完成 **工具合同与错误恢复 → 当前阻断优先及有效业务进展观测 → 两类健康误差（拒绝、通知）→ 待决材料历史分层 → 成本/复用与容量治理**。MN-04 a8 后续独立验证和异常关闭由实际角色沿原门禁继续；本轮没有代为执行。源码、测试通过、Gateway 采用、自然角色采用与端到端业务闭环仍分别记证，不把其中一项替代其他项。
