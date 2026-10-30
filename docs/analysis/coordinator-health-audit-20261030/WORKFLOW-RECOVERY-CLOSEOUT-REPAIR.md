# OCR 拒绝与异常返修接续修复

日期：2026-10-30，Asia/Shanghai（+08:00）。当前运行时 **0.16.5**，提交 `e7269dc3e1fec2be8905d2fc834519fadba8b26d`；主要接续修复为0.16.2 / `ba0ccbec684e93a5e16f7e7016e3d21ad3c0abfa`。

**机制有缺陷，但不是“REJECTED 永久锁住 REVIEW”或“根本没有快照刷新”。** 本次先修复通用接续缺口，再由原生角色执行实际业务动作；不修改业务数据库状态来制造完成。

## 当次核验与原因

维护开始后的只读 `current_input`/`live_progress` 均成功。20:29 的账本水位仍为 14249：MN-04 a8 为 REVIEW，固定 head `e82af00254ddd4dc67a823b142f0f7c6c4d5d596`，18:38:22 的独立测试 PASS 是最后的实质业务推进。`mn04-a8-ocr-001` 于18:36:44被 `NO_REVIEWABLE_FILES_OR_DUPLICATES` 拒绝；现场回执证明 prepare 阶段零派发。e14173 保持 REPAIR_QUEUED，MN-07 为 SNAPSHOT_STALE。

1. **会话超限仍可无限只读。** `workflow_sessions.tool_decision` 在达到交接阈值后提前放行 READ_TOOLS，只阻止写操作。该协调执行在无新增业务状态转换时持续读历史、旧审查材料和不存在的 spillover；预算记录已为 checkpoint_required=true。轮询频率增加无法替它产生有效动作，Gateway 热加载也不会更新独立 worker 已加载的代码。这是实际空转路径，不以“PID 活着”或 checkpoint 内容证明业务正在推进。
2. **决策目录混淆责任提示与可执行命令。** `follow_verification_repair` 指向旧异常候选，只附泛化描述；目录没有可执行类别，也未关联当前 a8 的 OCR。现场该会话确实查询了它的 contract，收到 COMMAND_NOT_ALLOWED_FOR_ROLE。一次参数拒绝不能单独证明全部停滞原因，但这里存在可复现的接口误导。
3. **OCR 适配修好后缺少新鲜的接续证明。** 当前账本其实已提供 service_submit 和有效预算，所以不能称 REJECTED 一定永久锁死；旧请求发生时测试文件未入选。0.16.0 已补精确测试源码选择，本次生产零模型预览确认 `tests/test_mn01_migration.py` 完整入选。另一方面，`coverage_blocker` 对新式 OCR_REVIEW_COVERAGE_UNAVAILABLE 只看旧拒绝，不重新检查已修好的适配，确实可能永久提示覆盖缺失。
4. **异常不是没有登记 owner，而是跟进材料没有反映真实阶段。** e14173 已由 COORDINATOR 处置并创建 a8；普通 independent_test PASS 不足以关闭原 MySQL UNVERIFIED。另发现 `revalidation_ids` 只接受最初返修 attempt，后续再次返修可能丢失原异常复验关联。
5. **SNAPSHOT_STALE 已有自动刷新。** `workflow_completion.reconcile` 等全部提供方完成、真实合入、有效合同/策略及旧验证和 writer 退出后自动排队。当前等 a8 完成是合法门禁。缺陷是 frozen_target 失败被静默返回，不公开当前失败码和责任人，异常环境中难以区分正常等待与冻结故障。

## 代码修复

- 超限后台会话只允许本角色 `checkpoint(yield_execution=true)` 或 `safe_release`，不再继续翻历史。未读不 ACK，原 work、attempt、证据及门禁保留；人工查询和报告会话不受此后台阈值约束。
- 目录增加 `execution_kind`，guidance 的 contract 明确 `executable=false`；异常材料列出真实返修任务、当前候选、责任人及 `related_decision_ids`，不扩大任何命令权限。
- 对固定身份匹配、已证明零派发的旧/新 OCR 选择拒绝，运行当前零模型 preview 和既有测试文件适配。完整无重复覆盖为 READY，可沿原有效授权新 action_id 提交；空覆盖、损坏证明、改变背景或预览失败保持 BLOCKED/UNVERIFIED。预览超时只形成诊断，不打坏整个当前输入入口；不启动模型、审查服务或自动裁决。
- 补验异常关联沿原生 `attempts.previous_candidate_id` 检查同 run/task 的连续谱系，再次返修也能自动排队同源 MySQL 复验。缺链、跨任务、跨 run 不关联；真实 PASS 后仍需显式 close，保留原 UNVERIFIED。
- `refresh_status` 与 reconcile 共用冻结目标守卫；当前输入及目录公开冻结失败码、未完成提供方和责任人，不能拿旧快照当当前目标。REVIEW 阶段责任明确为协调者。

涉及 `workflow_sessions.py`、`workflow_decision_context.py`、`workflow_service_recovery.py`、`workflow_progression.py`、`workflow_verification_exceptions.py`、`workflow_supplemental.py`、`workflow_completion.py`、`workflow_runtime.py` 及热加载清单。

## 验证与上线

- **124 个相关测试均完成通过验证。** 最后主回归123项通过，1项跨进程游标测试首次因维护命令运行目录错误而未能导入运行时；回到隔离仓根单独重跑通过。测试失败没有被改成业务 PASS。
- 新增真实隔离 Git/SQLite 的“UNVERIFIED → 原角色退休 → 原范围返修 → 再次返修 → 独立测试 → 批准/合入/完成 → 原请求 MySQL 补验 → 显式关闭”链路；实际执行，不只验证文本提示。既有最终集成刷新、限额环境重试、权限/未知副作用围栏及报告查询回归保留。
- 生产只读回放确认 a8 新预览有且仅有迁移测试文件，e14173 的责任提示关联实际 OCR 决策，MN-07 的当前冻结原因是 COMPLETION_DEVELOPMENT_NOT_COMPLETE / MN-04 a8 REVIEW。
- **20:38:16** 原生 Gateway 热加载成功：PID 3612975，68项源码 SHA 一致，文件后端 ACTIVATED，调度钩子 COLLECTED，飞书 connected。
- **20:38:53** 为使旧独立协调进程采用修复，按已有维护重启授权对其专属 systemd scope 发送 SIGTERM。先核 PID/start、当次业务进展、完整工具终态、无在途业务工具及无 SUBMITTED/UNKNOWN 外部审查，并以短事务发布围栏避免派发竞态；未改业务行。原 scope 的4个进程全部退出，无 SIGKILL，无伪造 checkpoint、裁决、测试或 work 完成；等待原生观察器核退并接续同一 work。

实际业务接续结果和最终采样见下方增补及[发布核验记录](workflow-recovery-closeout-rollout.json)。原始会话、数据库及含凭据的提交映射不上传 GitHub。

## 原生接续核验

旧执行已由原生观察器核为 ENDED，writer_cleared=true；native_outcome=UNKNOWN，没有冒充业务成功。原生300秒 fire-claim 保护自然到期后，20:43:54 新执行 `97a455b39f3f4e3db7cd9726b1fc9786` / PID 648166 重新领取同一原 work。新进程文件后端 ACTIVATED，全部记录的源码哈希与0.16.2一致，没有另建业务owner或清除原生保护。

截至20:47的当次只读查询，账本水位14252，唯一新增事件是 EXECUTION_BOUND；模型观察器 VERIFIED/complete=true，首个 main_request 仍未结束，没有工具调用。此时尚未核到新 OCR 或业务状态转换，不能把“领取成功、PID存活”报告为需求推进。后续实际业务结果继续增补于本节。

## 生产回放追加修复：预览路径与证据引用

20:48:18 新协调会话的实际 prepare 回执暴露了额外缺陷：递归证据校验把 `current_preview.files[].path`（Git 相对路径）当作缺哈希的文件证据，错误返回 UNVERIFIED / IMMUTABLE_HASH_REQUIRED。其余10项真实证据哈希全部 FOUND。0.16.3 将该元数据字段改为 repository_path；没有忽略或放宽真实证据引用。新增回归直接把 recovery 输出送入真实 decision prepare，校验合法引用 FOUND、另加缺哈希证据仍为 UNVERIFIED。

追加验证：接续9项、决策28项共37项通过。决策工具发现测试首次未带已安装 Hermes core 的 PYTHONPATH，修正测试环境后28项全过。20:52:50 网关热加载成功，PID3612975、68项源码哈希全部匹配；20:53:00 生产只读材料重放 source_status=FOUND、10项真实证据均匹配，唯一选择元数据为 tests/test_mn01_migration.py。正在运行的协调worker不被旁路替换，已有请求按原门禁继续；新安全会话采用0.16.3。

0.16.2 机制快照已提交 `700edaf` 并推送既有 GitHub 仓库 wangzd9/skill-backup 的 main；0.16.3 与最终接续证据随后同步。

## 实际业务已恢复推进，当前等待真实审查结果

20:57:18，原生协调者执行 `97a455b39f3f4e3db7cd9726b1fc9786` 使用原有效授权，以新 action_id **mn04-a8-ocr-002** 正常 service_submit，账本 **e14253 / EXTERNAL_ACTION_REGISTERED**；工具回执 COMMITTED / SUBMITTED。没有由维护进程冒用其identity或直接修改业务状态。

随后只读 `ocr_runtime.py status` 与 systemd 当次核验一致：受管服务 `coordinator-ocr-fe57ead766604892b993393597f54ed3.service` active/running，主PID700578、审查子进程700668；spec哈希与提交回执相符，唯一批次b01实际入选 **tests/test_mn01_migration.py**，状态RUNNING。原先拒绝后的无人推进已解除，不能把启动审查报告为审查通过。最终当次账本、原生服务和精确时间保存在发布记录 business_resumption；该节不引用历史checkpoint作当前状态。

当前真实等待点是OCR结果尚未返回。MN-04 a8仍为REVIEW；e14173仍是REPAIR_QUEUED；MN-07仍为SNAPSHOT_STALE。后续由原协调者读取完整审查结果并裁决，满足门禁后批准/合入/完成a8；再由原TESTER完成同源MySQL补验，真实PASS后显式close异常；MN-07在提供方及writer条件齐备后原生刷新并最终验证。当前未伪造这些后续动作或宣称需求完成。

机制0.16.3与指南已提交并推送GitHub **wangzd9/skill-backup/main**：`f95c6ce`，远端HEAD已核对一致；本节业务接续证据另随显式快照提交同步。保留该仓原有未跟踪文件，不上传数据库、原始会话、凭据及含token的invocation。

## OCR结果返回与审查生命周期关联修复

20:59后的原生账本水位升至 **e14254**，新OCR被自动收集为 RESULT_READY。21:01:25只读 results 核实覆盖完整、READY_FOR_ADJUDICATION、findings=[]，没有错误；仍为 NOT_EVALUATED，不自动登记PASS。当前目录明确给出 service_decide。

追加发现：异常提示关联只按目标candidate_id匹配，结果返回后的service_decide只带action_id，导致related_decision_ids为空。0.16.4改为沿原生external_action归属解析候选，补齐收集/裁决阶段，排除其他候选；不改变命令身份、授权或裁决。接续10项、决策28项共38项回归通过；21:01:10网关热加载68项源码哈希一致，21:01:25生产材料FOUND，e14173提示已正确关联新OCR的service_decide。当前原协调者仍负责阅读原件并实际裁决，后续门禁未被伪造完成。

## 历史覆盖拒绝路径的同类回归

继续核代码发现新式 OCR_REVIEW_COVERAGE_UNAVAILABLE 的旧回执还包含 excluded_files[].path。为覆盖当前旧式拒绝之外的通用路径，0.16.5 将已知path/exclude_reason结构在恢复视图中同样表示为repository_path；原拒绝回执及哈希保留。回归夹具加入原生生成的excluded_files结构，并实际经过完整decision prepare：修复覆盖FOUND、真实缺哈希证据仍UNVERIFIED，10项通过。21:06:06网关热加载68项源码哈希一致，生产当前审查裁决材料仍FOUND。

原协调者于e14256/e14257正常核退，21:05:01原生自动绑定下一执行 `afa9a10695b94912811dfdb87a9bbe31` / PID731080（e14258），仍是原work；没有新增接管owner，也没有重启或中断在途OCR。当前其职责是读取已完成审查的真实结果并裁决，不能以提交回合的历史checkpoint覆盖已经RESULT_READY的当前状态。

## 实际推进：OCR裁决、批准和本地合入

随后原协调者已实际完成三项业务转换：**e14259 SERVICE_ADJUDICATED / OCR PASS**、**e14260 CANDIDATE_DECIDED / APPROVE**、**e14268 CANDIDATE_MERGED / head e82af002**。这些来自当次账本，不是checkpoint叙述。修复后的路径已从拒绝恢复推进到真实合入，未跳过独立测试或审查。

21:47后的当次入口仍显示a8 REVIEW，但 independent_test/ocr均PASS且合入记录存在；下一合法动作是 **complete**，其准备材料FOUND，合同要求真实交付summary。该回执落账后才会将任务COMPLETED并触发原生MySQL补验和MN-07刷新。此时不能把本地合入冒充整个需求完成；e14173仍REPAIR_QUEUED，MN-07仍SNAPSHOT_STALE，新MySQL补验尚未排队。新协调执行 `cb0c82a4435348b8a533d1ff373e9fcb` / PID869700已自动绑定原work，模型请求在途，尚未取得complete回执。

全部机制源码、指南和截至0.16.5的核验已推送 **wangzd9/skill-backup/main**，提交 `0573a1c90c3e04f9dfd7e6ae19e6678b44b156a0`，已核远端HEAD一致。后续业务状态另随证据更新同步。

## 实际闭环接续：开发完成、补验派单和快照刷新

**e14274**：原协调者通过complete提交真实交付收据，MN-04 a8变为COMPLETED，当前开发任务 **6/6完成**。随后原生巡检自动落账 **e14275 SUPPLEMENTAL_REVALIDATION_QUEUED**，明确关联异常 **14173**、原请求 **8937**、原work及原FAIL supersedes；新work `work-467824927822491bbf9094e4b24d1825`固定a8 head e82af002。**e14277**：原TESTER已领取该work，下一动作verify_supplemental，无需新授权。

**e14276 INTEGRATION_VERIFICATION_QUEUED**：原生重新冻结当前集成版本并排MN-07 attempt 3，work `work-05b4512fec3d4ebfad4afbac89029232`。当次状态已从SNAPSHOT_STALE变为 **VERIFICATION_QUEUED**，没有手工改快照、复用旧PASS或伪造测试。具体目标、原生派单与查询时刻见发布记录refreshed_business_status。

当前剩余的是实际独立验证门禁：TESTER先完成同源MySQL补验，再执行排队的MN-07；MySQL真实PASS后协调者显式close e14173，最终集成真实PASS并完成逐项证据审核后才complete_requirement。当前尚无这些PASS/异常关闭/需求完成回执，不能宣称需求已经验收。派单、快照刷新及原角色自动领取均已实际发生，原来的无人推进链路已解除。
