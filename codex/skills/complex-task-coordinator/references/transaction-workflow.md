# 事务工作单模式（仅 ACTIVE）

只有原生运行时注入的 `mode=ACTIVE`、`schema_version=2` 工作包才适用本文件。STAGED 表示实现尚在验证；继续使用原协议。不要通过改文档、环境变量或自行创建数据库切换模式。

常驻运行角色只有 COORDINATOR、A、B、C、TESTER。规划者由用户手动调用；审查者按原授权入口按需调用。两者都是 Skill，不建立角色池、cron、租约、业务工作单或执行绑定。

运行时维护需求/run、任务及整数业务 attempt、当前 work、原生 execution/代次、事件游标与通知；模型处理当前工作包中的业务。当前工作包包含完整任务契约、冻结 AC、检查点、当前候选、未解除失败及证据定位。材料缺失要报告具体缺口，不从旧目录或历史措辞推断权限。事件页有更多时用 `role_work_input(after=displayed_through,through=event_through)` 补读；只能确认实际处理过的已发行事件前缀。

群查询回合只读，用 `role_live_progress` 当次查询。可信当前用户明确开发指令即经原生入口接纳，无需“全部权限”口令；需求接纳后内部开发流程及本地合入自动执行。特殊服务预算/撤销/切换等具体控制使用 `role_control_request` 登记准确目标；群不取得业务租约，不开发、测试或代后台成为 owner。被接受的具体动作持久有效，直到完成、暂停、撤销或授权到期；普通后续消息仍只读。继续不自动解除暂停。付费服务及生产权限分别核原授权，本地合入按local_development_automation持久策略，不由模型填写 `authorized=true` 代授。

协调者通过专用工具发布有版本的完整任务计划和 DAG，核依赖、范围、AC 与资源安排；按原职责评价验证结果并裁决。服务提交只用原 OCR/强审唯一入口，结果就绪不等于 PASS。批准、合入、必要复验及最终完成分别留证。冲突和缺陷回派开发，不自行代开发或日常测试。

普通文档不能一律阻塞开发。先用 `role_repo_state` 的 `documentation_review.artifact` 读逐文件正文、相对 HEAD 的 diff 与 SHA256，核对事实、范围、敏感信息及不改变 AC；可确认的使用说明/交接由协调者用 `role_coordination_command(action="commit_documentation")` 单独提交。payload 为 `expected_head`、`expected_branch`、`summary`、`files:[{path,sha256,review}]`，逐文件 review 记录核验依据。入口核原生 writer、当前需求仓库与当次精确快照，仅提交所列普通文档，不消费其他暂存代码、不改其他工作区文件、不运行hook或push；文件变化后重读。指令、PRD、AC、设计/长期契约及代码不走该入口。归属未知不等于没有审核职责；发现实质冲突、事实不足或仍在变化时保留材料并说明具体证据，不能建议删除。文档需要修正时明确修正内容并交当前范围内执行者落实后复核提交；不把有错误的文档盲目提交。共享主仓清洁后继续原合入；在途受管worktree与既有合入回执保留，隔离不是文档审核职责的替代品。

ACTIVE 准入以账本 `admission_hint` 为准；旧池 current 不参与该判定。`WRITER_FENCE_PRESENT` 表示同角色已有写执行，先核真实进程；`waiting_external` 只记录仍成立的外部条件，15分钟或仓库分支/HEAD/dirty→clean变化允许复核。轮询、检查点和水位增长不是新增业务成果。MySQL等内部验证安排先按任务合同落实 owner/资源，不能把内部派单责任直接改写为等待用户授权；生产迁移仍单独核原授权。

OCR/强审提交须引用已登记的 `authorization_id`，不能在业务工具里自填授权。当前可信群全权请求登记授权时必须明确服务和候选；未写调用限额时最多一次，未写有效期时最长一小时，更窄限制优先。本地开发接纳默认包含受管分支上的合入；未指定目标时运行时选择workflow分支，不再逐任务索要许可。用户原文明示仅候选/后定目标时尊重；自定义合入后命令必须来自原文同一argv JSON。授权不创建审查者角色或工作单。普通“继续”不能代替授权，也不解除暂停。

开发者使用注入的独立工作区，实现和自测当前任务。写操作通过受限 terminal；宿主路径与其他工作区只读。冻结完整 base/head、AC 版本、实际证据与中文总结，用开发专用工具交付。交付事务释放业务容量，但旧执行或工具子进程仍活着/未知时禁止接新写入。不得手写 ACK、claim、slot-release、continuation 或任务终态；代码变更范围仍由交付校验。

测试者选择有意义的命令，通过测试专用工具在独立冻结 checkout 运行。零测试、缺机器结果、超时或取消不能算 PASS。复验必须明确关闭对应旧 FAIL；开发自测不能代替独立验证。

开发和独立测试子进程均隔离宿主写入并禁网络。依赖应由已接受环境预先提供；需要改变环境或联网时保存检查点并说明具体依赖，不绕过隔离。开发工作区可写范围与最终任务 scope 分别核验，交付前不得包含 scope 外改动。

自然安全点保存语义检查点、准确已处理事件前缀和下一步；需要让出时提交 `yield_execution`。旧 run 排空或取消时使用 `safe_release`，不销毁恢复材料。进程退出不自动交付；写权撤销后不启动新动作，未知外部副作用不盲重放。运行时核实际 PID/start/进程组或 cgroup，不依赖业务 TTL 判断存亡。

ACTIVE 后台技术会话默认在 48 个主请求轮次或约 128000 输入 token 时进入安全交接：停止发起新的业务动作，用本角色 `checkpoint` 保存真实进展、未决门禁和精确下一步，设置 `payload.yield_execution=true`，仅确认已实际处理的事件前缀，然后结束本回合。原生主循环的业务请求预算为 64，另沿用原生最终收尾机会；这不是任务完成条件，不删上下文、不弱化 AC、不取消在途独立服务。新会话仍恢复同一 work/attempt，旧 writer 的完整退出证明继续阻止抢占。阈值可在插件 `settings.session_policy` 配置，报告与群问答不受这项后台预算限制。

执行的 `state=ENDED` 仅表示写进程及其子树已核退，不能解释为任务成功。`native_outcome` 单独保留原生 `COMPLETED/FAILED/UNKNOWN/UNOBSERVED`；UNKNOWN 副作用先核查，不自动重放。ACTIVE 观察器只读账本及原生证据，不再生成旧 inbox 指纹。R2 实际采集及派发回执和进程身份决定 `ADOPTED`，未出现 TICK 业务事件不等于未启用；周期 jobs 继续作恢复入口。

机械告警在单群待发语义意图超过该群一小时额度时整理为可追溯摘要，完整源件、成员和 UNKNOWN/SENT 回执保留；不提高原投递额度。只读维护命令 `monitor_notifications.py status --outbox PATH` 可查询同群额度、积压与 `next_eligible_at`。池额度未满不能证明具体群仍有额度。

业务账本是任务状态权威源；TODO/HANDOFF/session/时间线是导出视图，PRD/AC 仍决定目标。保留原始证据与完整历史。20 分钟六态快报和领取通知零模型；时段总结用冻结事实→只读模型语义分析→程序排版，完成总结复用交付原文。生成与送达分账，夜间静默不算已送达，人工问答照常。auto 需云端时优先 sub2api 的 deepseek-v4.1-flash；手动模型选择沿原方式。


## 持久任务授权与隔离 MySQL 补验（2026-10-27）

用户明确授权某需求/任务合入指定分支、给出有效期而未限制attempt/base时，登记任务策略：同run/task/repo/branch、冻结合同与原到期/撤销边界持续有效。正常基线刷新或返修不重复索要授权；入口对当前候选派生精确permit。明确的候选/attempt/base/命令限制仍严格有效。新attempt必须重走适用独立测试、OCR、批准；授权继承不等于门禁继承。范围/AC/目标变化、原许可到期或撤销才说明具体缺口。

`MN-01 迁移 MySQL 双向补验归属 TESTER 隔离 MySQL 会话` 是专用原生控制指令，不要求“全部权限”口令。`role_control_request(action="request_supplemental_verification",target={task_id:"MN-01",profile:"mysql_migration"})`持久保存原消息与固定已合入候选，忙时排队，不抢writer；原生入口识别此句直接登记，后续询问按supplemental_verifications核验。没有COMMITTED/DUPLICATE回执不得声称“后台会消费”；已入账不得让用户重发。

TESTER领取supplemental_verification后调用`role_test_command(action="verify_supplemental",payload={})`。服务只提供本地临时MySQL8.4、无网络/端口的Unix socket和固定候选测试，自动清理自身容器；不接受生产URL、凭据或任意Docker参数。补验单独发布supplemental_mysql，skip不能写MySQL通过，原任务COMPLETED不被回执冒充重新验收。FAIL保留实际日志，由协调者区分测试夹具/环境与迁移实现，按原AC安排最小修正和真实复验，不把代码问题改写成缺用户授权，也不为消红删记录。

原补验失败后的接续：如果同一需求已合入并完成的后代候选，在声明scope内修改了固定迁移测试文件，运行时沿原请求自动排队复验，不测试未合入或在途工作区。复验单记录原始消息事件、修复候选head和待关闭失败ID；跨任务修正只接受原生复验排队事件证明，真实PASS才关闭原失败，失败不关闭旧记录。不因修复归入另一任务而再问用户MySQL归属。


## 本地自动化与验证收口（2026-10-28，替代逐task许可）

local_development_automation覆盖已接纳run的任务，直到完成/暂停/取消/撤销。派单、验证归属、安全收口、基线刷新及本地合入不需要用户a/b或逐次许可；原task策略仅作历史兼容，新需求默认受管本地workflow分支。候选仍逐次核测试、OCR、批准及实际目标base，拒绝冲突/未知副作用，不以机械授权代替验收。当前MN需求沿用户既定feat/a100-multi-node自动交付。

`refresh_task_base`发现旧验证执行时，持久登记VERIFICATION_RETIREMENT_REQUESTED并返回WAITING_FOR_VERIFICATION_SAFE_EXIT。TESTER的next_actions要求safe_release，原角色结束后，observer仅在execution/tool真实核退时自动VERIFICATION_RETIRED；不伪造PASS/FAIL、不改事件确认、不强杀writer。没有活writer的历史空候选验证自动退役；禁止反复拉起该work。此处不是用户审批或MySQL资源授权。

新下游开发领取自动使用同需求审计集成head，保留冻结AC/范围。未交付任务用`refresh_development_base(task_id,reason)`，开发者保存成果后checkpoint让出，协调者在真实安全点重排且保留旧私有工作区。禁止交付head=base空占位候选，禁止拿另一head自测证据包装空交付。这样不会因刷新必须先交付而产生测试/刷新互等。


## 审查未完成的处理（2026-10-28）

INCOMPLETE 不自动推荐再次 resume。先 service_collect 核原生状态及已有发现；固定 HARD_DEADLINE 原样重审不构成根因处理，retry_reason 文字不等于修复，已补审或额度耗尽在派发前拒绝且不消耗授权。证据缺失按待核实处理。已核实实现缺陷可在原开发授权内 decide REWORK，不要求用户先接受部分覆盖；新候选仍重走独立验证及完整审查。只有放宽原 AC、改变审查基础设施或扩大授权才另行说明所需决定。复用批次不称两轮独立复审一致；预检 DUPLICATE_REVIEW/RETRY_LIMIT 不称前次运行中断原因。

## 用户裁决原文（2026-10-29）

`record_user_decision`对可信当前用户答复持久登记，不要求全权口令。短答复如“按解读b”使用target={}，显式对象可填decision_id；不能由模型给短答复捏造ID。接收回执和实际处理分开：USER_DECISION_RECORDED→协调者读原提问和实现→resolve_user_decision→USER_DECISION_RESOLVED→更新checkpoint。未处理项持续进入next_actions，ACK事件不能吞掉处理义务。登记不授予外部权限、不修改冻结AC、不伪造测试通过；确有歧义只澄清关联，不重索同一裁决原文。


2026-10-30 / 0.16.0 接续与证据合同：先处理当前合法动作及完整相关证据，不以追齐历史游标为业务动作前提，未读不ACK。decision工具四种互斥参数：directory(offset?/limit?/expected_fingerprint?)；prepare(decision_id,expected_fingerprint?)；contract(action)；evidence(requests)。prepare不传action，contract不传decision_id或fingerprint。目录和单项材料指纹分别核对；参数拒绝按回执纠正，不重复试探。

测试pytest模式由运行时管理JUnit输出，junit模式argv必须含字面{junit_xml}。新操作的同候选/命令/真实环境失败或未知记录必须先处置；换ID、重建同版MySQL不算修复。原ID仅恢复既有执行，稳定性两次子运行和原证据保留。开发自测用delivery_evidence.directory中的绝对路径，不在Git工作区堆证据、不以忽略dirty交付。test-only OCR无入选时，原生适配只精确纳入默认排除测试源码，保留原显式过滤与规则；空覆盖仍阻断，不免审、不自动PASS。

ACK、checkpoint、worker退出分别不代表业务完成。核最近真实状态转换、首次有效动作、session_handoff及异常下一责任人；旧INCOMPLETE单列历史，旧/跨需求UNKNOWN写者继续围栏。零派发拒绝不是业务通过；UNKNOWN通知不盲重发，健康总量与有界样本分别说明。完整实施与采用限制见工作区效果停滞修复记录。
