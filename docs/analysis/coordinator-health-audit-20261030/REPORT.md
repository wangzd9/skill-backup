**协调者及角色运行机制健康审计，2026-10-30**

修复上线记录见 [IMPLEMENTATION.md](IMPLEMENTATION.md)；下文保留原审计时点的事实与判断。

结论：主干流程能够运行，互斥、固定版本、独立验证和真实证据门禁有实际作用；但当前还不足以称为健壮的无人值守机制。主要短板是收口语义、事件接续、账本增长和监控生命周期。继续增加角色提示词或增加轮询频率，不能解决这些结构性问题。

本次检查采用源码静态分析、运行日志、SQLite 只读事务快照及部署采用回执。未修改运行代码、角色配置或业务账本，未启动测试、模型、OCR、远端任务或群通知。本文时间均沿用运行日志的 UTC+08:00。业务快照截至 14:30:39，监控补充截至 14:32:07；系统仍在运行，不将这些时间点的状态当作永久状态。

代码仓为 `/home/wang/work/hermes-role-runtime`，HEAD 为 `252232d`。插件软链接指向该仓；机制采用回执为 ACTIVATED，capability 为 ADOPTED，hook 为 COLLECTED；采用清单中的 45 项源码 SHA256 与现场文件全部一致。因此，以下问题不能简单解释为“新修复尚未部署”。这些回执也不能替代端到端业务验收。核对结果见 [deployment-observation.json](/home/wang/work/doc/analysis/coordinator-health-audit-20261030/deployment-observation.json)。

当前业务事实如下。

| 观察项 | 实际证据 | 含义 |
|---|---|---|
| 当前账本 | ACTIVE，generation=1，admission_open=1 | 运行机制处于启用状态 |
| 当前 run | prepared-rosbag-catalog-a100-multi-node | 其他已登记 run 不代表已开工 |
| 开发节点 | MN-01/02/03/05/06 COMPLETED；MN-04 attempt 7 为 REVIEW | 最终验收返修已进入正常候选审查链 |
| 最终验收 | e13993，13:28:59，整体 FAIL | AC01～09 报 PASS，AC10 UNVERIFIED，同时存在 code/environment issues |
| 当前协调者游标 | processed=9549，issued=10549；事件高水位 14060 | 阅读叙述与实际处理水位存在明显差距 |
| 最近24小时 execution 绑定 | 协调者228，TESTER44，C21，A2，B2 | 是会话绑定数，不是独立任务数，也不能全部判为空转 |
| 通知 | PENDING 3，SENT 229，SUPPRESSED 85，UNKNOWN 15 | 三条新增验收事件未进入投递消费；UNKNOWN 不等于已证实丢失 |
| 账本文件 | 3,092,041,728 bytes，约3.09GB | 与当前业务规模明显不相称 |

来源：[focused-evidence.json](/home/wang/work/doc/analysis/coordinator-health-audit-20261030/focused-evidence.json)、[较早完整结构快照](/home/wang/work/doc/analysis/coordinator-health-audit-20261030/ledger-snapshot.json)。两次24小时滚动计数略有变化属于窗口移动，不是数据冲突。

已有机制中，应保留的保护包括：

- 单角色 ACTIVE/DRAINING execution 有数据库唯一约束；核退检查真实 PID/start identity、进程组/cgroup 及登记工具写者。TESTER 已交付而 execution 暂为 DRAINING，是正常核退阶段，不能直接判为双写。见 [唯一约束](/home/wang/work/hermes-role-runtime/business_ledger.py:124) 与 [核退实现](/home/wang/work/hermes-role-runtime/business_ledger.py:1107)。
- 最终集成版本核对已接受合同、供应候选、合入记录及固定 head；交付和收口均复查证据。未执行、失败或 skipped 不能转成 PASS，真实隔离 MySQL 另有门禁。见 [冻结版本](/home/wang/work/hermes-role-runtime/workflow_completion.py:94)、[证据门禁](/home/wang/work/hermes-role-runtime/workflow_completion.py:259)。
- e13993 已有实际独立测试记录，并非只有文档承诺；e13998 把 CODE-01 回派给原 MN-04 owner、新 attempt，保留原范围和旧证据。见 [返修实现](/home/wang/work/hermes-role-runtime/workflow_completion_repair.py:57)。
- 本地收口明确记录 production_complete=false、push_performed=false；等待复核不授予额外业务权限。见 [收口事件](/home/wang/work/hermes-role-runtime/workflow_completion.py:354)、[等待复核](/home/wang/work/hermes-role-runtime/workflow_wait.py:23)。

下面按问题给出判断。P1 表示优先修复的收口、正确性或持续运行风险；P2 表示可靠性及可观测性缺口，不使用这些分级暗示已经发生数据破坏。

1. **F1 / P1：最终验收与协调收口的前置条件混合，存在循环等待路径。**

   已观察到：e13816 将 MN-AC-01～10 全部放进 TESTER 的最终检查清单；e13993 把 MN-AC-10 留为 UNVERIFIED，明确说明收口回执属于 MN-08 协调者职责，不由 TESTER 代判。

   源码原因：[ensure_plan](/home/wang/work/hermes-role-runtime/workflow_completion.py:60) 从整份 ACCEPTANCE.md 用正则提取全部 AC 编号，未表达 owner、阶段或证据类型；[deliver](/home/wang/work/hermes-role-runtime/workflow_completion.py:255) 要求完整覆盖这些编号，所有检查 PASS 且无 issue 才能整体 PASS；[complete](/home/wang/work/hermes-role-runtime/workflow_completion.py:317) 又只允许在最终 PASS 后开始协调收口。

   原合同并不是完全互斥的两组 AC：[TODO](/home/wang/work/doc/prd/rosbag-catalog-a100-multi-node/TODO.md:32) 将 MN-07 定为 AC01～10 的“本地部分”，MN-08 定为 AC10 的聚合收口；[AC10](/home/wang/work/doc/prd/rosbag-catalog-a100-multi-node/ACCEPTANCE.md:31) 同时包含测试报告、审查和收口证据。因此问题是缺少“同一 AC 的分阶段子义务”，不能简单从测试清单删掉 AC10。

   影响：即使 CODE-01 修复，仍可能出现“TESTER 等协调收口证据，协调者等 TESTER 全 PASS”。当前已出现职责分歧和 UNVERIFIED，尚不能声称已经证明永久死锁。

   建议：冻结结构化义务，包括 ac_id、阶段、owner、证据类型、适用条件、前置节点。测试者完成 AC10 的测试报告部分，协调者随后完成审查与收口部分；生产阶段继续单独保留。文档索引、审查判定应使用相应证据类型，不能要求它们以 pytest 成绩代替。

   验收标准：同一 AC 可跨两个角色分阶段完成；本地测试通过后能进入协调收口；缺失真实测试证据、审查证据或生产授权时，相应阶段仍保持未完成。

2. **F2 / P1：逐 AC 覆盖仍主要依赖自由文本，已经出现编号与证明内容错位。**

   e13993 中，AC06 的说明主要引用清理契约/路径服务，AC07 的说明主要引用分配 API/分配事务；但原 [AC06](/home/wang/work/doc/prd/rosbag-catalog-a100-multi-node/ACCEPTANCE.md:27) 要求超时、未知启动、子写者与占用恢复，原 [AC07](/home/wang/work/doc/prd/rosbag-catalog-a100-multi-node/ACCEPTANCE.md:28) 要求页面、节点访问及清理围栏。AC05 的说明也不足以逐项解释两路线闭环与发布适用性。

   这里不能据此断言相关代码完全没测：同一条 e13991 引用的是整组专项测试，可能包含相关场景。可以确认的是，当前逐 AC 的证据索引无法清楚证明其声称的覆盖；本地、替身和待真实验证部分也不能仅靠一个 PASS 清楚区分。

   [交付校验](/home/wang/work/hermes-role-runtime/workflow_completion.py:260) 主要验证事件归属、证据完整性、机械成绩及 skipped 状态，未检查 AC 与具体 case/断言/环境之间的语义对应。[交付 schema](/home/wang/work/hermes-role-runtime/workflow_completion_contracts.py:12) 也只保存 test_event_ids 和 notes。

   建议：建立 AC → 场景/断言 → 测试 case 或非测试证据 → 环境/阶段的覆盖矩阵；整组测试事件可复用，但每项应有具体定位。把替身覆盖和实物验证显式拆开，由独立审查确认语义覆盖，而不是仅统计“几个测试通过”。

   验收标准：每个 AC 的本地义务都有可定位的证明；编号说明错位能被审查发现；后续真实环境义务不会因本地全绿而消失。

3. **F3 / P1：checkpoint 控制字段误嵌套被静默保存，实际游标长期不前进。**

   这是正在发生的问题。[checkpoint-contract-misuse.json](/home/wang/work/doc/analysis/coordinator-health-audit-20261030/checkpoint-contract-misuse.json) 保存了24小时内15条同类事件，涉及多个角色。e14049 的 checkpoint 正文含 processed_through=14047，命令返回 COMMITTED；14:30:39 该 work 的有效 processed_through 仍为9549。

   [工具映射](/home/wang/work/hermes-role-runtime/workflow_runtime.py:665) 只读取外层 processed_through/yield_execution；[内层 schema](/home/wang/work/hermes-role-runtime/workflow_contracts.py:37) 是任意 object；[账本 checkpoint](/home/wang/work/hermes-role-runtime/business_ledger.py:1084) 不拒绝正文中的同名保留控制键，返回值也没有有效处理水位。于是“成功保存了一段文字”很容易被理解为“成功推进业务状态”。

   实际后果可以跨会话看到：14:22 和14:27 的原生输入都重新从9549发出同一百条前缀；14:23 已发行至10549，14:29 请求跳至14047仍被拒绝，又回读9549～9649。跳跃拒绝本身正确，问题在误用没有及时纠正。

   [准入逻辑](/home/wang/work/hermes-role-runtime/business_ledger.py:489) 又以 processed_through 后的事件判断是否有业务反馈；无显式 waiting_external 时，即使无新反馈仍可 CHECKPOINT_RESUMPTION。旧游标、重读和频繁接续因此可能互相放大。协调者24小时228次绑定是成本信号，不能直接把228次全部算作无效运行。

   建议：对误置控制键给出明确结构化拒绝；回执始终返回有效 processed/issued、work/execution 状态及实际是否 yield；向模型提供可续读页游标和明确 ACK 能力。重复恢复且水位/业务状态无变化时，进入有界退避及可定位的诊断。

   验收标准：保存正文不能伪装成 ACK；误嵌套得到明确反馈；正确续读能推进已发行前缀；历史查询不能自动确认未处理事件；退出后仍须保留真实写者核退门禁。

4. **F4 / P1：输入查询造成巨大写放大，账本缺少与持续运行匹配的容量边界。**

   [存储采样](/home/wang/work/doc/analysis/coordinator-health-audit-20261030/storage-stats.json) 显示，commands 表占3,045,519,360 bytes，而 events 表约16.4MB。4,079条 work_inputs 命令保存了2,965,291,603 bytes 的结果正文，最大单条6,175,353 bytes。大头是重复输入页，不是业务事件本身。最初业务结构快照只有约1.94万条 commands、1.41万条 events。

   [work_inputs](/home/wang/work/hermes-role-runtime/business_ledger.py:1044) 经由 [_command](/home/wang/work/hermes-role-runtime/business_ledger.py:325) 执行 BEGIN IMMEDIATE，并把整页 JSON 作为幂等结果写入 commands；同一结果还会在补充回执行号时 UPDATE。[snapshot](/home/wang/work/hermes-role-runtime/business_ledger.py:439) 即便指定 run，仍全量读取 events、executions、execution_tools 等多张表，多条调度路径反复调用它。

   现场 journal_mode 为 delete，业务查询索引也较少。这些事实说明增长和锁竞争风险，但本次没有测出锁等待分位数，不能把所有工具错误归因为 SQLite 性能。

   建议：保留幂等身份和审计可重放性，将大结果改为固定水位、内容寻址的不可变页引用，避免重复存正文；输入页增加字节上限；热点快照按 run/当前生命周期/增量查询；按实际查询补索引，并建立容量、留存和归档规则。WAL 是否适用需另评估，不应直接在线改库或删 commands。

   验收标准：同一事件页重复读取不会按正文大小线性增加数据库；恢复页大小有界；近期调度成本不随全部历史无限增长；归档后仍能核对历史命令与证据。

5. **F5 / P1：问题分类缺少阻断性和适用范围，自动返修/环境等待容易误路由。**

   e13993 的 CODE-01 同时写了“一次死锁、重跑未复现、建议 owner 评估、未阻塞 AC03 PASS”，但 kind=code。[deliver](/home/wang/work/hermes-role-runtime/workflow_completion.py:288) 只要有 code issue 就整体 FAIL；[queue_repairs](/home/wang/work/hermes-role-runtime/workflow_completion_repair.py:11) 随即回派，实际产生e13998。

   这不证明该死锁不值得修复；它证明自由文本判断和机器状态的含义不一致。当前 schema 只有 code/environment，不能表达待调查、已确认、阻断缺陷、非阻断建议或范围外既有失败。

   ENV-01 明确声称是既有平台失败、不影响本批，但 [结果计算](/home/wang/work/hermes-role-runtime/workflow_completion.py:292) 对任何非空 issues 都不允许 PASS。即使CODE-01和AC10解决，这些范围外环境项仍可能让报告变成UNVERIFIED。环境等待另有[同目标最多3轮、间隔300秒](/home/wang/work/hermes-role-runtime/workflow_completion.py:193)的保护；源码搜索未发现同目标耗尽后由环境恢复事件重新启用预算的专用入口。这是静态恢复缺口，不是已经发生的三轮耗尽事故。

   建议：增加 blocking、confidence、accepted_scope、ac_ids、disposition、owner 等语义；只对已确认且阻断本地合同的缺陷自动返修，待调查项走有界核实；范围外已确认问题保留原始 FAIL 记录并明确列为不计入本阶段，不改写原始结果。环境恢复应有可审计的重新排队条件，既限制频率，也允许恢复后继续。

   验收标准：文字“非阻断建议”和机器 FAIL 不再冲突；范围外环境项有明确处置；真实阻断缺陷不能靠改分类抹掉；等待预算耗尽后的恢复路径明确。

6. **F6 / P2：新增最终验收通知只有生产与渲染，漏接实际消费。**

   14:30:39 仍有三条 PENDING：e13817 的集成验证排队（10:29）、e13993 的验证报告（13:28）、e13998 的返修回派（13:31）。[生产端](/home/wang/work/hermes-role-runtime/workflow_completion.py:221) 设置 notify=True，[渲染端](/home/wang/work/hermes-role-runtime/workflow_reports.py:640) 也支持这些类型，但 [drain 白名单](/home/wang/work/hermes-role-runtime/workflow_notifications.py:15) 及其扩展列表没有它们，连 REQUIREMENT_COMPLETED 也缺少。

   已检查仓内其他 outbox 消费点：周期报告走自己的固定出口，候选完成通知只复用 CANDIDATE_*；未发现另一条自动消费上述最终验收事件的路径。因此这是通知接线遗漏。账本排队与执行已经发生，不能说“没有通知所以任务没有执行”。

   建议：统一事件通知注册表，声明 producer、renderer、consumer、投递对象及静默策略；为 PENDING 年龄和无消费者类型提供诊断。补齐接线后，对历史通知依据时效性显式处置，避免集中补发过时消息。

   验收标准：新增业务事件必须有投递契约或明确不通知声明；相应日间事件能进入消费；UNKNOWN 保持不盲重发，静默语义仍一致。

7. **F7 / P2：工具预检过晚，纠正一次无效请求可能遇到新的幂等冲突。**

   10/29～30 的错误日志出现 PAYLOAD_MUST_BE_OBJECT、PYTEST_COMMAND_REQUIRED、JUnit占位要求、evidence字段不合法、TEST_COMMAND_ID_CONFLICT 和交付后继续写入的拒绝。同一异常可能产生 traceback、GuardRejected 和工具 warning 三条记录，不能把日志出现次数相加当独立事故数。见 [recent-log-errors.json](/home/wang/work/doc/analysis/coordinator-health-audit-20261030/recent-log-errors.json)。

   其中一个明确代码原因是：[execute_candidate_tests](/home/wang/work/hermes-role-runtime/delivery_evidence.py:375) 先保存与command_id绑定的 request.json，并建立checkout，随后才检查pytest/JUnit形式和evidence结构；修正这些尚未启动测试的错误时，同一command_id因请求哈希变化而冲突。日志中10:43:33的evidence错误后有10:43:46的command冲突，10:56:29的JUnit错误后有10:56:37的冲突，时间链与此路径一致；本次未逐一证明它们属于同一command_id。

   建议：无副作用的完整参数预检应先于请求冻结和checkout创建；区分INVALID_NOT_STARTED、PREPARED、STARTED、RESULT_READY等恢复状态；把可重试性、是否需要新command_id和缺失字段结构化返回。避免模型凭错误字符串反复猜参数。

   验收标准：测试未启动的参数错误能得到一致纠正指引；已启动的请求仍严格幂等；交付后只允许合法收尾，不能继续尝试业务写入。

8. **F8 / P2：监控混合历史证据债与当前服务状态，且无活动阈值未配置。**

   14:32:07的监控health为unverified，保留1,599条anomaly，其中1,387条last_seen已超过24小时，1,382条属于旧frozen execution终止证据未核实。样例的native_status=completed、owner_process_state=exited_or_reused、scope_writer_present=false，但仍缺终止证明。见 [监控摘要](/home/wang/work/doc/analysis/coordinator-health-audit-20261030/observer-latest-summary.json)。它们是未解决的证据债，不能全说成误报，也不能等同于1,382个当前活写者。

   源码在[缺少可核实恢复事实时保留旧异常](/home/wang/work/hermes-role-runtime/workflow_observation.py:264)，这个保守方向正确；但缺少按当前run、实际写者风险、历史证据完整性分别展示和处理的生命周期。已有[小于8KiB的健康视图](/home/wang/work/hermes-role-runtime/observation_view.py:8)解决输出长度，不等于完成历史状态治理。

   更直接的问题是，当前39条fault中38条属于独立服务观察不可核实；与账本对照，31条action已为ENDED，7条为INCOMPLETE。见 [终态服务交叉核对](/home/wang/work/doc/analysis/coordinator-health-audit-20261030/terminal-service-observation.json)。[observer](/home/wang/work/hermes-role-runtime/role_observer.py:555) 遍历全部历史external_actions，并继续查systemd unit；[unit查询失败](/home/wang/work/hermes-role-runtime/role_observer.py:602) 不区分这些状态就记fault。即使终态临时unit正常回收，也会落入同一路径。本次没有采集每条systemctl失败原因，不能断言38条全部都是unit回收造成。

   同一快照还明确记录activity_policy=POLICY_UNCONFIGURED、no_activity_seconds=null。[activity_step](/home/wang/work/hermes-role-runtime/activity_signals.py:180) 因而不生成按阈值判断的NO_ACTIVITY；已登记操作超时和来源不可核实等其他规则仍存在，不能说整个监控被关闭。

   建议：当前业务门禁、当前写者/在途服务、采集器健康、历史证据债分别展示；ENDED服务的存活检查转为持久终态回执核验，INCOMPLETE保留结果完整性问题；历史scope通过可核实的终止证明归档，不按年龄直接清除。配置并展示无活动策略及其采用状态，同时把“模型/工具在动”和“任务状态/游标实际前进”作为两类指标。

   验收标准：正常结束且回执完整的临时unit消失不会持续制造当前存活故障；UNKNOWN写者仍不能释放；历史证据债仍可审计；无活动阈值缺失可见，合法长测试不会因静默而误判卡死。

这些问题不是八个互不相关的小缺陷。当前最明确的放大链是：checkpoint误用未被识别 → 有效水位不前进 → 老事件被重复读和当作待反馈 → 会话恢复及结果留存增加 → 账本与上下文负担增加。另一条链是：AC缺少阶段语义 → 逐项覆盖靠文字解释 → 测试与收口职责分歧 → issue分类过粗触发返修或等待 → 自动接续难以到达稳定终态。监控混合历史问题，又使这两条链更难被及时看清。

下一步建议按下面顺序推进，保持每批可单独验收。

| 批次 | 工作重点 | 完成标志 |
|---|---|---|
| 第一批 | F3 checkpoint有效回执与误置字段诊断；F6通知接线；F8终态服务观察 | 有效水位、投递和当前故障能被可信观察，先减少持续误读和噪声 |
| 第二批 | F1阶段义务、F2覆盖矩阵、F5问题处置与环境恢复 | 使用真实MN合同能够从独立验证走到本地收口，生产义务仍独立保留 |
| 第三批 | F4输入页去重、增量快照、索引/容量治理；F7完整预检 | 历史增加不再导致每次恢复重复存大正文，错误恢复路径清楚 |
| 持续治理 | 配置无活动策略，观察无进展接续、事件积压、等待年龄和告警历史债 | 能分别回答“有无运行”“有无进展”“为何阻断”“恢复条件是什么” |

已有最终验收回归日志显示12项通过，见 [历史回归日志](/home/wang/work/doc/analysis/native-final-acceptance-20261030/evidence/runtime-tests/tests_test_workflow_completion.py.recheck.log)。但 [测试夹具](/home/wang/work/hermes-role-runtime/tests/test_workflow_completion.py:24) 使用单项TEST-AC-01合同，[交付构造](/home/wang/work/hermes-role-runtime/tests/test_workflow_completion.py:71)也使用相同简化语义；这不足以证明真实MN合同的多阶段收口、范围外issue处置及新通知消费都正确。后续验证应补真实合同形态、跨会话正常接续、无进展接续的退避、终态服务回执与环境恢复等生命周期场景，而不只重复已有单元测试。

本次未对所有历史授权、每条服务结果、全部AC实际测试断言及生产环境逐一复核，也未做运行期故障注入或性能压测。因此不能据此宣称机制“已经没有漏洞”，也不能宣称已经发生越权合入、双写或数据损坏。当前证据支持的结论是：保护主干基本有效，活性、成本边界、证据语义和监控收敛仍需系统补强。

后续已补充[角色工作流token消耗专项分析](/home/wang/work/doc/analysis/coordinator-health-audit-20261030/TOKEN-CONSUMPTION-REVIEW.md)，结合近期角色用量、工具回包元数据及启动上下文结构，量化重复接续和输入负担，并提出保留验收质量的优化顺序与验证口径。
