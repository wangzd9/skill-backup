# 需求准备链闭环修复（2026-10-31，0.17.6）

前一次0.17.5修复只覆盖措辞、错误归因和双包幂等，没有提供后台合同准备能力，因此没有把完整执行链打通。本次修复这一遗漏。

## 已核实的原因

- 群指令原生来源可验证；实际接纳拒因是 `TASK_OUTSIDE_REGISTERED_REQUIREMENT`，不是身份缺失。原消息DB行379887，正文SHA-256为 `a44acfd3ce3d63317e8b53ac75d97acc9912875019870a3eb4008ec718d454f6`。
- 群回合只能提交控制，后台协调者也没有普通文件写入和终端权限；原系统没有受控合同编辑/重新登记命令，导致“先准备再接纳”和“未接纳不能工作”循环等待。
- 用户单字母B的裁决登记被拒绝。后续“现在呢”回合只有只读查询，无成功登记回执；协调者声称“已重新登记、后台执行中”不符合账本事实。

## 修复边界与流程

1. 合法原生开发指令遇到上述准备门时，持久登记全局 `REQUIREMENT_PREPARATION_REQUESTED`，冻结原始授权、提案和已有登记身份。只复用未开工的PAUSED prepared run；拒绝活动run、已有work、来源不符及并发准备请求。QUEUED不表示已接纳。
2. 后台原生owner使用 `prepare_requirement`，提交限定正式文件的旧SHA和完整正文或有界追加、任务范围、额外提供方任务的冻结提案引用及审阅说明。没有增加shell、任意写文件或群直接开发能力。
3. 全集预检后，先持久冻结审阅及文件意图，再做原子替换。中断后只允许相同审阅重试并核对BEFORE/AFTER；出现第三种文件哈希就停止，保留外来修改。SQLite回滚不冒充文件回滚。
4. 同一链内更新REGISTRATION.json任务/文件元数据、审计重新登记事件、冻结新合同，再用原始原生指令接纳；接受任务范围只包含本次任务，保留历史任务记录。数值区间如TASK-BR-00～03按精确前缀/位数展开。额外提供方任务必须有冻结提案引用，不复活历史任务。
5. 接纳与选择分开。`activate_requirement`沿用原有安全切换协议，不强清writer、不生成重复run；存在未交出的开发/测试work时阻止切换。跨包按真实依赖接续，不虚报跨run并行开工。
6. 准备工作单持续出现在原生next_actions、待决目录、按对象取证及报告artifact中；全局事件允许既有终态run的owner收到新工作。事件ACK不会删除尚未处理的准备单。
7. 单字母B只记录为待关联答复，不新增授权、测试PASS或门禁裁决。维护恢复入口校验原生存储信封、角色群、原平台message ID和正文哈希；“现在呢”不能伪装成新开发来源，也不要求用户重发。

## 验证与现场采用

回归命令：

```sh
PYTHONPATH=$PWD:$PWD/tests:/home/wang/.hermes/hermes-agent \
/home/wang/.hermes/hermes-agent/venv/bin/python -m unittest -q \
tests.test_local_development_automation test_workflow_preparation \
test_semantic_decision.PreparationTests test_workflow_registration \
test_user_decisions test_workflow_autonomy.AutonomyTests test_manual_gateway \
test_requirement_runtime test_mechanism_repair.ActivationRepairTests test_native_command_schema
```

140项隔离回归通过（最终日志见外部证据目录）。回归包含真实临时Git/SQLite/进程身份的原指令入队→合同准备→重新登记→接纳→安全切换→发布计划→开发者领取首任务；双包幂等、旧进展保留、文件冲突、中断恢复、提供方映射、原生历史恢复、工具schema、裁决材料及热激活一并检查。详细回执保存在 `/home/wang/work/.local/requirement-preparation-repair-20261031/`，不把原会话或业务数据库加入源码/备份。

真实业务状态单独以现场回执为准；机制测试通过不表示两专题已经接纳、开发或验收完成。现场采用记录在完成核验后追加。

现场已核验热加载前后Gateway PID3612975、进程启动身份不变；71份激活源码SHA全部匹配。原消息已恢复为两张真实准备工作单：Foundation e15395（prepare-d7542139ec5b900c59d346c63b082c1f）、Spray e15396（prepare-1ea43dd0ccff73591ffb162951797ccd）；单字母B仅补记待关联答复e15394。15:24后台原生新执行e15399已发行包含这三条业务事件的输入；尚不能据此宣称已接纳或已派单。真实后续结果以账本为准。

实际后台处理暴露另一处材料缺口：短答复恢复只附旧checkpoint，协调者会反复搜索日志寻找选项。恢复入口现同时链接同一原生会话中紧邻该用户消息的已发布assistant回答（本例行380090），保存哈希artifact；这是历史问答证据，不是新授权，语义仍由owner核对。完整开发准备指令明确不依赖短答复的历史选项关联。

现场后续：原问答artifact通过e15400关联，原生owner在e15401保存检查点并安全交接，e15404新执行接续；e15405已真实完成用户答复语义关联。原生会话已调用prepare_requirement动作合同并批量读取Foundation冻结合同/提案，尚未出现prepare_requirement成功提交、REQUIREMENT_ACCEPTED或新计划，两个业务run仍为PAUSED。此时机制源码已部署并通过140项隔离回归；不能将上述材料准备或未返回模型请求报告为代码开工。

## 16:00真实提交暴露的引用兼容缺陷（0.17.7）

15:35会话的合同准备被会话预算交接门拦截；该次checkpoint又因payload被序列化为字符串而失败，不能把原生COMPLETED当业务完成。15:48原生执行e15408接续，16:00两包真正调用prepare_requirement，均被PREPARATION_NEW_TASK_SOURCE_MAPPING_REQUIRED拒绝（state.db工具结果行380531/380532），没有正式文件或账本业务进展。

提交为每个已请求任务附上了冻结提案原文，并用了需求根内绝对路径。原实现要求映射key必须严格等于“额外拆分任务集合”，因此额外提供正确证据反而被整单拒绝；工具schema没有解释这一约束，也没有声明只收相对路径。

0.17.7允许本次任务集合内的冗余引用，绝对路径仅在其明确属于需求根、且精确匹配已冻结source时接受；相对路径规则相同。每份引用仍验证非正式可编辑文件、冻结原文和长度，不读用户引用指向的实时文件；未知任务key、跨根路径、..、伪造引用、缺少额外提供方来源、历史任务复活均拒绝。工具schema明确这些语义。隔离回归新增模型实际提交形式的成功用例，以及上述失败不得产生文件/事件副作用的检查。

143项隔离回归通过（38.981s，regression-citation-compat.log）；现场八条任务引用只读比对均确实匹配已冻结proposal原文。此验证不调用真实业务prepare、不代替原生owner提交，也不代表已接纳或开工。

## 实际接纳与切换接续（0.17.8）

16:24原生owner真实完成两包合同增补、重新登记和接纳：Foundation注册e15414、接纳e15416，任务为TASK-FND-SRC-01、TASK-FND-REC-01及TASK-BR-00～03；Spray注册e15421、接纳e15423，任务为TASK-SR-00～03。提供方拆分有冻结来源，历史任务未重启。随后e15427保存检查点安全交接，16:31原生owner选中Foundation（e15431/e15432）。

激活回执原先只说COMMITTED，没有明确说明调用者仍绑定旧run；只读events_only页也未显示选中run已改变，模型在旧身份下继续查新需求材料，容易多走一轮并碰发布门。0.17.8在激活回执与事件页追加明确的scope_transition/安全释放指引；旧owner先checkpoint、safe_release并结束，旧writer自然退出且核验清零后，下一原生tick才绑定新run。保留全部身份、ACK、串行和安全退出校验，不自动接管或强清进程。16:35旧work已真实释放（e15434）。此处仍没有正式任务计划或开发者开工证据，不能把自动生成的COMPLETION_PLAN_PUBLISHED当作PLAN_PUBLISHED。

151项隔离回归通过（44.208s，regression-handoff-full.log），包含真实存活旧writer仍不能向新run发布计划、事件页指向安全释放、旧进程退出后新身份可正常发布并领取任务。定向21项回归亦通过。

## 计划发布的合同可发现性与预检（0.17.9）

16:37 Foundation原生执行绑定e15437。该owner连续提交四版完整计划，依次触发STAGED_TASK_CONTRACT_INCOMPLETE、STAGED_DEPENDENCIES_MUST_COVER_DEPENDENCIES、PRESTART_DEPENDENCY_CONTRACT_REQUIRED及STAGED_DEPENDENCY_GATES_NOT_MONOTONIC（state.db工具结果380700、380711、380713、380715）。第一次改参重试还复用了已冻结command_id，产生COMMAND_ID_CONFLICT。没有PLAN_PUBLISHED或开发者领单事件。

这些依赖拒绝符合账本要求；缺陷在于工具合同未完整声明既有条件字段及集合关系，且每次只在事务门报告首项错误。修复在schema声明分阶段任务的八项必填合同字段，预检一次汇总依赖并集、阶段单调性和晚期提供方精确引用错误；接口和simulation仍由协调者按正式合同提供，不自动推断业务依赖。scope说明明确其是仓库相对文件路径/glob，任务叙述应填入ac/inputs。账本权限、依赖、验收和共享文件约束不变。

恢复指引区分两种情形：INVALID_NOT_STARTED未启动、无副作用，可修正后用同ID；TRANSACTION_GATE已拒绝的命令ID绑定原输入和回执，改参或前提变化需新ID，保留原拒绝记录。UNKNOWN仍不得盲重放。热激活同步更新schema常量和无状态预检函数；当前在途执行不接管、不重启，预算交接规则保持不变。

定向34项回归通过（11.428s，regression-plan-preflight.log），涵盖原生schema裁剪后的条件仍有效、多项错误同时报告、提供方精确匹配、拒绝/未启动ID恢复语义。完整179项隔离回归通过（53.867s，regression-plan-preflight-full.log），包括既有账本、准备接纳、安全交接、派单、验收和恢复。旧模块缺少新预检函数时的热激活亦有专门检查。现场实际采用证据待核验后追加；工具修复不等于业务开工。
