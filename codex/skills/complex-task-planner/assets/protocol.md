# 五角色文档协作协议 v3

规划者把本模板落入本需求根的 inbox/PROTOCOL.md，填入需求ID、唯一绝对根、角色绑定、主分支、授权和现有工具调用方式。已有任务/回执保持兼容，不重置业务状态。

## 需求归属

本协议只管启动入口指定的需求包；代码仓、产品目录与本需求根分别记录。独立新目标由规划者判定归属，默认新建同级包；原REQ/AC返修留本包，不因同仓或复用角色把关联需求加入本期范围。跨包依赖引用需求/任务ID、固定产物和接受证据；各包TODO保持唯一状态源，产品索引不抄各具体任务状态。

业务工具读取实际project_root下的TODO/inbox/回执，该根仍须是完整需求包。固定五角色池持有稳定群/job，以需求总索引导航选择，运行时解析当前requirement_id/generation及各角色业务根；总索引不存业务TODO/ACK。文档建包不自动创建群/cron或选择需求。角色选择、安全切换与持久scope按complex-task-coordinator/references/role-pool.md执行，不复制同一群/job制造多个可写绑定，不在普通巡检自动迁移历史目录。

## 单一来源与角色

- 本需求TODO：唯一业务任务状态与依赖源。DESIGN：契约。HANDOFF：最新恢复检查点。TIMELINE：事件历史，不替代状态。
- ROLE-BINDINGS.json：项目根、角色/群/Skill、cron、控制模式和迁移信息，不复制任务状态。
- 新包规划由规划者初始化；进入运行后协调者单写共享主文档、派单、decision、OCR批次、强审查包和全局时间线。活跃包规划修订先交协调者受控写入，或在安全检查点按既有租约交接写权；群聊发起回合默认只读；当前用户明确全部权限且取得业务租约才可动作，cron按原授权继续。
- 协调者维护绑定的development_log：在重要交付、验证、审查、合入、阻塞变化及里程碑时按证据更新，不为无变化巡检刷日志。正式功能日志按仓库既有流程：功能完成并验证后及时生成/合并pending候选；用户明确批准后approved，实际发布成功后released并更新公开日志，不自行批准/发布。
- A/B/C 只写自己的代码范围、sessions、ACK、receipts、events；测试者只写指定测试材料、sessions/TESTER、test-receipts、验证报告与自身 events。
- 每个执行者自己新建任务专属分支/worktree；测试者独立取证；协调者审核门禁后合入，不代业务开发和日常测试。

## 领取、交付和接续

派单必须包含 task/attempt、固定 base、角色、范围、AC、停止条件和路径。新领取先只读核对派单及环境，在有效角色租约内准备固定base/被测commit的实际branch/worktree/checkout，再直接通过task-claim可恢复事务创建独立ACK、稳定领取事件/checkpoint和outbox，由脚本一句说明领取了什么并开始工作；不能先手写ACK或修改派单status。历史已有独立ACK或旧status ACK仅兼容恢复，already_acknowledged不产生新领取事件/通知，不删除旧ACK强制重报，派单正文不改。完成由原生模型总结成果、证据、缺口与下一步。扫描全部 inbox 中有效派单，不依赖自由文件名是否符合某个 glob。

任务状态沿用已有 TODO；常用交付状态 REVIEW_PENDING / TEST_REPORT_PENDING 只是等待审核，非通过。最新有效 attempt 由协调者明确，不能用新的无效文件覆盖已接受身份。接受后记录真实 merge 和合入后必要验证，满足门禁才 DONE。

依赖接受、阻塞解除、冻结交付通过slot-release明确释放槽后重新计算候选；等待审核的后台责任保留，已释放槽可显示空闲。本期已授权、依赖齐全、输入完整、文件/资源无冲突且 owner 确认可接才派。最多三路开发，不等整波；无可派任务不扩需求。强审查等待只冻结受审门禁。

协调者每轮巡检检查空闲开发槽；若任务过粗，在原REQ/AC、已接受契约和已授权阶段内主动拆分可独立验收的剩余工作。按实际可接人数分配，不按人数机械切分；共享文件、迁移链、资源和硬依赖仍串行。先更新TODO父子关系、子任务卡、依赖/接续、HANDOFF和拆分事件，再派单；总任务汇总各功能并完成集成验收，不重复派完整实现，子任务接受且必要集成验证完成才收口父任务。已ACK/在途工作先确认执行者检查点与交接边界，不能直接换owner重派；范围/关键契约变化升级。具体操作读协调者Skill的references/task-decomposition.md。拆分不增加逐任务强审；无可并行工作时说明具体串行原因和解除条件。

## 单写与恢复

绑定中的 runtime_script 提供 worker-identity、claim/renew/release、短控制 run、preflight、event 和 timeline。cron先用只读worker-identity取得当前调用链自己的独立worker PID/start，再以真实Hermes会话身份作owner；不临场编写祖先脚本，不使用即将结束的shell PID、共享gateway或其他worker。未知owner存活时不能自动抢占。群全权仍使用原生role_manual_lease，不能套用cron身份。

每个角色在开发、测试写入或关键控制动作前claim；拿不到租约只核查和报告。用带token的run包裹短文档/派单/ACK/合入操作；run内执行前重读相关版本，已死owner的token不可复用。长开发/测试保留当前worker的角色owner，并在安全检查点renew；释放短文档flock不代表释放业务执行槽。独立OCR/强审可在冻结提交和登记独立服务身份后释放协调者控制租约，后续回合只核查同一在途操作，不重复提交。

租约到期不证明owner已退出；绑定PID/start明确退出或被复用时，可由claim记录原恢复证据并立即取得新租约，不等待TTL。存活或身份不明不抢占；未知/未绑定旧owner按下段显式恢复。禁止删除稳定锁inode。协调者沿用inbox/coordinator.lock，OCR独立沿用inbox/ocr.lock；文件存在不等于锁被持有。

同一存活owner的匹配token在未被接管时可以renew或release过期租约，之后才继续控制动作。未知/未绑定旧owner中断后，仍须先核实原会话与进程已停止并保存证据；租约到期后用recover --owner 原owner --confirmed-stopped --evidence 证据绝对路径显式恢复，再claim。绑定owner明确死亡的恢复不等TTL；recover仍拒绝存活或无法核实PID，禁止用“超时”代替停止证明，不沿用死亡owner的token。

claim成功只代表取得本角色租约，不是重新启动业务或测试许可；同task/attempt/被测commit仍有存活或未核验进程时，只检查并续写状态，不再次ACK、测试或启动OCR。恢复时先context-input、当前派单/ACK及实际回执；已完成不重做。

运行示例（按绑定中的真实绝对脚本路径替换 RUNTIME 与 ROOT，TOKEN 为 claim 返回值，不在群公开）：

```text
python3 RUNTIME worker-identity
python3 RUNTIME claim --project ROOT --role A --owner 本轮会话ID --pid 上一步worker_pid --ttl 600
python3 RUNTIME run --project ROOT --role A --token TOKEN -- 允许的短文档写入命令
python3 RUNTIME event --project ROOT --role A --token TOKEN --event-id 稳定事件ID --task TASK-xx --attempt 1 --status REVIEW_PENDING --summary 交付行为 --evidence 回执路径或commit --next 等协调者审核
python3 RUNTIME release --project ROOT --role A --token TOKEN
```

preflight 在每个巡检回合更新真实检查时间并报告 ACTIVE/PAUSED、旧 owner/租约情况。不要把重启/压缩理解成重新派已 ACK 的任务。异常中断保留日志、PID和资源身份，逾期告警，不自动 kill、reset 或重派。

## 5分钟巡检、分时段总结和事件

五个运行角色各有 poll/report 两个 Hermes 原生 cron，均显式绑定对应 Skill，origin 与 deliver 指向同一角色群。巡检不用Hermes monitor跳过检查本身：每5分钟由脚本真正核查，无变化不调用模型；独立检查、变化入队与原生角色接续按下方最新规则。

当前报告策略role-workflow-v3（Asia/Shanghai）：21:00–08:00停止主动定时/事件通知，夜间工作、必要接续、派单、测试、OCR和落盘继续，人工提问正常答复。协调者08:00由脚本收集21–08全角色与18–21补充，再由短只读模型总结；白天08:20–20:40每20分钟只由脚本显示A/B/C和测试者（空闲/工作中/等待/阻塞/暂停/待核实），不要求七区。个人11/15/18先采集08–11/11–15/15–18本角色事实，再由短只读模型整理。只有20分钟状态短报no_agent；个人时段/08:00不是全部no_agent。旧个人20分钟及2小时空闲报告停用。

状态统一为空闲、工作中、等待、阻塞、暂停、待核实；空闲表示可领新任务，不按TODO关键词或mtime推定百分比。已冻结交付且明确释放槽的等待审查者可空闲，证据不明待核实。历史确认空闲时长另按可靠区间统计。时段总结列本角色阶段、客户端LLM响应、业务排队、确认空闲/可接新单区间与本地/云端请求、实际token、可得费用及比例；范围包含本人全部绑定原生群/业务cron/总结/辅助会话，不含独立OCR/Codex。协调者08:00可汇总跨角色业务事实，用量严格只计协调者本人，不汇总执行者/测试者用量。请求按实际路由和稳定身份去重，使用[start,end)并按完成时间归属调用/token，自身总结一般计下一时段。费用/usage/路由未知标缺口与覆盖率，不估成0，不倒填历史。

只读模型总结与业务owner/租约/pending隔离，不claim/renew/release业务执行权，不ACK业务输入，不写业务event。报告产物与投递状态独立存储；失败或送达不明不伪装完成，不自动付费重试。脚本事实是第一输入，模型仍须核对冲突；摘要不代替验收。完整规则：/home/wang/.codex/skills/complex-task-coordinator/references/reporting-policy.md。未绑定专项只同步模板，不新建cron。
有意义的派单、领取、返修、实质进展、阻塞/解除、交付、测试/OCR结论、批准、合入及暂停/恢复先落有权写的文档与唯一事件；白天可在本角色群一次通报。正常续租、重复等待和自身巡检记账不作为新进展。cron final 自动投递，禁止再 send_message 双发；角色事件唯一 event_id，协调者去重汇总 TIMELINE。

事件落盘后由同一五分钟检查/poll接续，已运行目标不重启。只有原生提前唤醒入口不会启用已暂停job时才使用；当前trigger_job会重新启用job，不用它绕过暂停。定时回合不创建或更改定时任务；巡检回合只有用户有效授权且明确需要时才通过统一限额脚本调用Codex，二十分钟报告仍只读不起跑。

用户可见进度按本角色报告契约及 `feishu-progress-style` 呈现；人工当前进度当次运行`live-progress`核验，事件基稿和时段历史材料不能替代它。协调者人工/有意义事件/08:00汇总保留七区，20分钟例外只角色状态；所有角色名字后标（空闲/工作中/等待/阻塞/暂停/待核实）；执行者/测试者仅本人工作、成果、阻塞依赖及下一步。事实范围、时点及静默遵循complex-task-coordinator/references/reporting-policy.md，工程证据按需核对；报告不决定门禁或修改任务事实。

无变化、正常心跳、纯等待、自写回声与重复进展不主动群报，poll final仅[SILENT]；有意义的新完成、交付、失败、阻塞变化、返修、实质发现或待决策事项先落唯一事件，白天允许一条即时通报。人工询问正常答复。周期报告不是重复事件，不伪造时间线业务进展。实际排队延迟如实记录，5 分钟是调度周期而非模型运行时限。

## 审查和合入

弱模型 OCR 流程保留，由协调者串行触发；覆盖清单、大文件完整职责拆批、上下游契约、固定快照及在途运行全部记录。超时、空输出、缺工具不是通过；先确认旧批停止才重启。开发者与测试者都不跑 OCR。

强审查额外，默认人工触发；用户可授权协调者在自行指定的时长、起止时间或当前任务范围内自动调用Codex审查者。范围、原话/消息来源和绝对时间落盘到统一授权账本，无授权/过期/撤销仍等待人工；更换授权不重置额度。所有项目共用strong_review.py，滚动60分钟最多5次且在途串行，失败/超时/复审计入，禁止裸Codex/API绕过。强审run短提交独立systemd service，冻结spec/input/package/worker及完整请求身份哈希；同review_id改变task/base/repo/model/timeout或风险/问题/AC/门禁明确冲突，旧无完整身份记录不猜复用。提交前解析并冻结实际模型，无法确定失败，worker显式--model并在实际起跑前再核授权；不从terminal后台长跑，不把nohup/setsid当持久。status/recover核unit/PID/start/cgroup，UNKNOWN不自动重试。协调者人工/事件/08:00报告含强审查分区，20分钟状态例外；写当前需要与否、执行/返回状态、结论和后续；授权或额度影响接续时说明具体条件。完整授权、次数、在途与真实用量保留在原账本/详情，不在正文倾倒字段。默认在功能批次完成开发和常规测试后审，不增加实现前审查或按任务/commit机械触发；冻结原AC，具体风险、问题与门禁明确才REVIEW_NOW，旧PASS不覆盖新行为。

强审查只纠正原功能：CORRECTION须证明违反既定需求/AC，回派原任务最小修复；VERIFICATION_GAP只补证明原AC所需证据；OUT_OF_SCOPE（新功能、未来规模、偏好重构或额外质量标准）默认不派单、不进当前TODO、不阻塞。跨模块阅读须与当前错误有因果关系；真实样本和生产等独立阶段的原门禁不提前变成无关模块条件。复审只看问题ID、修复diff和必要上下文；问题关闭且原AC满足即收口，不以“还可完善”继续加单。同根因两轮未解决按原规则升级，不无限返修。历史已确认问题不因本次范围收紧自动撤销，现有待办按上述分类继续核对。

合入前核对代码、开发自测、必要独立验证、OCR及适用强门禁。禁止自动“先合后审”例外。同根因两轮返修失败升级；不得删测试、弱化断言或成功标准。已批准工程闭环与生产启用分别记录。

## 授权与结束

当前授权写在项目 HANDOFF；Skill 不新增推送、生产部署、NAS/处理机写入或付费调用授权。已有用户明确授权继续执行，不逐项重复询问。

PAUSED/CANCELLED/COMPLETE时停止新业务领取，保存检查点与恢复入口。固定池在需求完成后保存收口证据并保留当前选择待命，五角色群/job不停止，无新业务输入不唤业务模型；新需求仍须明确选择；暂停/撤销保留恢复材料且不强杀owner。非池历史专项按原协议暂停专项周期任务。最后报告遵循静默规则，逻辑角色通过文档和持久调度恢复。

## 分时段汇报（2026-10-13，替代旧空闲降频）

报告频率与静默边界统一遵循上文role-workflow-v3；旧20分钟个人报告/2小时空闲报告已停用，不从历史恢复。

Hermes现有report_job_id沿用，旧idle_report_job_id停用；协调者独立morning_report_job_id，仅20分钟状态短报no_agent，个人时段和08:00须短只读模型整理；绑定保存计划不复制业务任务状态。

## 脚本巡检与短快照（2026-10-13）

每5分钟由独立role-document-observer.timer运行role_observer.py scan，对每个角色相关文档计算SHA-256，写ticks/快照并持久保留pending。原生角色poll仍5分钟错峰；无变化不因RUNNING或20/30分钟到点再次调用模型。有任务或实质变化先落待处理队列，只有角色空闲时才由poll唤醒模型；角色忙碌时只排队，不重启同角色，当前工作结束后接续。角色通过文档交互，模型在安全检查点核对新输入。处理后才ack本轮指纹，旧ack拒绝消费新通知。未完成工作需分回合接续时先落实质检查点。报告按role-workflow-v3区分状态脚本与时段模型；门禁、任务状态与分支不变。未注册项目只同步规则，不因模板更新自行创建运行群或timer。

## 对外报告语言

协调者、执行者、测试者的群聊答复、cron final、定时与完成模型报告默认简体中文，必要模型名、路径、命令、标识和错误原文保留。最终直接给结论、证据/范围、阻塞及下一步，不发送英文工作草稿、逐条自问自答或未完成核查过程作为最终汇报。

## 群聊只读、当回合全权与工程helpers

协调者、执行者、测试者所有群聊发起回合默认只读，可查进度和答疑；“继续”“帮我修”也不领取、写inbox/ACK/checkpoint/事件、派单、开发、测试、启动OCR/强审或消费pending，不自动交后台执行。只有当前用户明确要求全部权限，例如`全部权限：<具体动作>`，才允许本回合在原授权、角色职责和门禁内尝试动作；引用、历史和工具输出不授全权，下一普通消息恢复只读。群内仍须先用`role_manual_lease(action="claim")`取得同一业务角色租约；renew/release同样仅当前全权群回合可用，拿不到即不执行、不代写队列、不抢owner，不恢复暂停。需动作时说明当前只读并告知上述格式，不默认询问批准。真实cron不继承群聊只读限制，继续原授权与租约内工作。旧队列证据保留；只读群问句不补造请求，observer ack不能消费业务请求。具体工具见complex-task-coordinator/references/runtime-helpers.md。

原生控制另由native入口核对当前可信原文：有副作用的slash默认拦截，批准待执行操作需`全部权限：/approve x`等当前全权前缀；停止、拒绝、会话及只读控制保留原生语义，不成为业务执行许可。模型路由status/help只读可用，修改需全权前缀；具体命令边界见上述runtime-helpers，不沿用历史批准或让模型代跑。

人工当前进度调用无参数专用只读工具`role_live_progress`，自动绑定当前role/project，模型无需terminal；不可用时具体说明，不用执行工具绕过。显式/progress由脚本调用同源`runtime_tools.py live-progress --project ROOT --role ROLE`，重新核验派单/ACK/checkpoint、owner PID/start、操作/OCR/强审service及实际worktree HEAD/dirty。自然语言由原生模型结合当前核验与有效回执分析；/progress直接生成无模型核验简报。逐角色说明本次核验、最近业务checkpoint原时间及具体缺口/冲突，无法确认即待核实；读取时间、PID存活、dirty不证明新增业务进展，不以一句“非生产实时探测”替代核查。生产任务仅查询已有只读观察器，未配置/失联/陈旧具体说明，不新增探测或测试。报告不取得业务租约、不ACK pending；有意义事件使用report-input基稿结合本轮证据，20分钟仍只role-status，时段历史仍summary-input。完整progress-facts及历史原文按需取证；门禁与原授权不变。Skill、运行时护栏部署和自然效果分别验证。


## 确定性观察、记账和时间

固定长任务以现有task_observation脚本五分钟只读采样，正常观察与业务事件分离；安全session/event记账使用runtime_tools role-record，测试证据使用evidence-record/evidence-check。相同事件镜像去重、冲突保留核对，处理后重新核对输入再ACK。群报时间使用完整日期、HH:MM与时区，从系统或可信原始证据读取；内部自动记录的秒可保留，历史分钟记录保留原始精度，不补造秒。发生、观测、补录分开。未知发生时刻明确未记录，不用模糊钟点或文件mtime冒充发生时间。原测试/OCR/强审/授权/合入门禁保持。


## 当前上下文、归档与安全继续（v3）

业务恢复先context-input读取本角色最新checkpoint、全部未完成任务索引和变更定位；同会话参考SHA未变且正文仍在上下文时不重复全文读，压缩/重启/hash变化后按需重读。不默认读archives或长session全文，必要代码和证据可精确展开。

sessions/TIMELINE等追加文档64KiB或400行触发完整记录归档，活跃正文保留<=48KiB、最近最多30条；大记录转完整证据引用，不按字节硬切。document-archive与所有writer共用稳定文档锁，可恢复journal+持久event索引保证旧记录不因marker归档而回填。role-record同步最新checkpoint；首次迁移不覆盖活跃旧writer，安全检查点再整理。TODO/HANDOFF按语义保留所有未完成、依赖、授权、门禁和最新恢复信息，完成历史归档不删除证据、不复制第二份任务状态。

正常长开发/验证不按请求数或分钟截断。在自然安全点需要交回合时用continuation保存task/attempt、具体下一动作和操作幂等身份，后续业务回合对账后接续并continuation-ack；同pending/自产ACK回声不重复唤醒，其他新输入继续保留。完整约定见complex-task-coordinator/references/context-and-archives.md。


## 改绑、接续与测试输入核验（v3.3）

角色表只存已知角色，审计放顶层rebind_audit；写候选绑定前用binding_contract只读校验。原协调者继续协调迁出角色时，在其coordinated_projects中显式指定注册项目的canonical绝对root与角色列表；只读订阅不授予跨项目写权，不创建第二个协调者cron。状态、回执、门禁和pending必须带项目身份；不能只读旧根的status就批准新根候选。

测试者business记录可带verification（完整head、PASS/FAIL、项目内实际receipt、明确关闭的旧TESTER事件ID），脚本冻结回执集合及hash。合入前对目标包执行acceptance-check，BLOCKED保持门禁；当前head独立PASS和旧FAIL闭环仍需核对，OCR零finding不能解除测试失败。工具结果仅为独立测试核验，不替代原AC、审查、授权和合入后验证，也不拦截任意shell Git。

群忙时用户消息经原生FIFO另起精确turn，当前授权不注入旧回合；当前明确授权只限本回合及共享租约，active/unknown owner不能凭心跳超时抢占。新continuation冻结不可变输入；旧输入恢复用精确字节/hash，不当完成ACK、不重做已交付任务。固定CLI首次安全/schema错误先查--help和字段，再纠正一次；同因仍失败即保存阻塞，不等价重试或绕过扫描。细则按需见complex-task-coordinator/references/stability-and-migration.md。

## 固定五角色池与需求选择（v3.4）

固定池的群/job不随需求更换。只有协调者群当前可信全权消息明确选择需求，才由role_requirement_select专用控制入口保存选择；可在owner忙时独立排队，不claim业务租约、不派单/ACK、不抢占。它是普通全权业务动作先取租约规则的唯一排队例外。请求已接收时给出安全点等待条件，事务完成才说明切换生效。

选择接受形成该需求的持久后台scope，cron在需求范围、角色职责、租约及原门禁内持续接续，直到完成、暂停或撤销；不要求每次cron再授群全权。人工全权只限当前人工turn，下一普通消息仍只读。scope不自动开启后续阶段、付费强审或生产权限。

每个业务回合从池解析并冻结requirement_id、generation、role和project_root；身份改变停止新动作，旧owner保留原根到安全收口。旧task/attempt、ACK、continuation、回执及在途操作留原包，不写入新包或批量确认。活/未知owner、cron或独立操作阻止切换；不以TTL替代停止证明。
