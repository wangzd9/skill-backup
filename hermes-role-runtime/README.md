# Hermes 角色运行优化

2026-10-30 **0.15.0**：同源账本裁决材料、完整待决目录、绑定工作单的对象取证/动作合同、纯事件续读、保守版本化记忆、短恢复、统一 ACK 进展及日志事件引用。MySQL补验UNVERIFIED现形成异常移交与安全退休，协调者显式处置后程序落实原范围已完成任务的新attempt；原结果与全部验收门禁保留。逐项审查和实际采用见[实施记录](/home/wang/work/doc/analysis/coordinator-health-audit-20261030/SEMANTIC-DECISION-IMPLEMENTATION.md)。

2026-10-30 **0.14.0**：ACTIVE工作输入保真去重，终态动作授权哈希归档，后台工具发现匹配角色能力，补充脱敏hook可见字节观测。保留模型、推理预算、全部验收/授权门禁和显式ACK；不依据字节降幅宣称Token或交付效率已达标。实施与安全重启采用见[本轮记录](/home/wang/work/doc/analysis/coordinator-health-audit-20261030/TOKEN-OPTIMIZATION-IMPLEMENTATION.md)。

2026-10-20 缺陷修复、验证、安装和恢复边界见 [FIXES-20261020.md](FIXES-20261020.md)。源码交付与生产完整采用分别记录；隔离首次激活测试不替代真实 M 门。

2026-10-20 增量已接入写权诊断、活动生命周期、共用时序回放、观察历史适配和有界事件派发，范围/回退/验证见 [Ruflo 增量实施记录](/home/wang/work/doc/analysis/role-workflow-flow-audit-20261019/RUFLO-IMPLEMENTATION.md)。本地检查工具为 `scripts/observation_tools.py`；生产迁移及存储切换未自动启用。

2026-10-19 事务工作流源码已实施，生产仍为 **LEGACY**，未激活新业务账本、未重启 gateway。新路径与切换门见 [MIGRATION.md](MIGRATION.md) 和[实施记录](/home/wang/work/doc/analysis/role-workflow-flow-audit-20261019/IMPLEMENTATION.md)。2026-10-21 上线阻断缺陷的修复、发布候选及未通过的真实门见 [FIXES-20261021.md](FIXES-20261021.md)。下文历史租约/ACK/continuation 说明仅适用于 LEGACY；ACTIVE 使用原生绑定的 work/execution、事务命令和同源 outbox，不能同时运行两套业务写权。

常驻角色只有 COORDINATOR/A/B/C/TESTER。规划者保持手动，规划者及审查者均按需加载 Skill，不进入角色池、cron、租约或业务工作单。auto 需云端时优先 `sub2api/deepseek-v4.1-flash`，手动选路单独保留。

独立插件及确定性工具，维护源在本仓库，安装软链接为 `~/.hermes/plugins/role-workflow-runtime`。模型选路复用 Hermes 原生供应商和传输；核心提供两个通用 hook。保持会话 system 前缀与工具结果，保留测试/OCR/强审/合入门禁。

- 五角色群人工回合默认只读，原生模型使用对应角色与显示Skill回答。运行时阻断写入、终端、代码执行、委派及未知工具；只读查询不入队、不claim或ACK。只有当前可信主消息明确“全部权限：具体动作”才授权精确本回合，业务操作仍须受控领取共享角色租约；外来owner存在/未核实时不接管、不代写队列。下个普通回合恢复只读。停止、拒绝、会话及只读控制保留，effectful slash/批准操作不能绕过权限。
- 仅实际绑定角色群的`/llm-load`和精确机械查询直接GET LAN指标；解释/分析仍交模型。缺指标标未知，只有有效双样本才算差分速率。
- 系统/可取得的工具前缀只做指纹审计；新增两个固定受控工具，权限与中文汇报约定通过原生当前回合上下文注入，不逐轮重建system前缀。工具hook经过限长，未知不冒充完整schema证据。
- doctor检查实际磁盘和依赖；长工程操作使用冻结JSON、独立systemd、逐步退出码、日志和进程身份，五分钟observer接续，UNKNOWN不重跑。

角色使用方法与边界见 [runtime helpers](/home/wang/.codex/skills/complex-task-coordinator/references/runtime-helpers.md)。核心命令：

```text
python3 runtime_tools.py load --json
python3 runtime_tools.py progress-facts --project ROOT
python3 runtime_tools.py report-input --project ROOT --role COORDINATOR
python3 runtime_tools.py queue --project ROOT
python3 runtime_tools.py doctor --cwd WORKDIR --storage-path DATA --docker
python3 runtime_tools.py start --project ROOT --role A --spec SPEC.json --token-file LEASE.json
python3 runtime_tools.py status --state RESULT.json
python3 runtime_tools.py role-record --project ROOT --role A --input RECORD.json --token-file LEASE.json
python3 runtime_tools.py evidence-record --project ROOT --role TESTER --spec SPEC.json --token-file LEASE.json
python3 runtime_tools.py evidence-check --record RESULT.evidence.json
```

工程命令成功不代表业务PASS；生产恢复仍需原授权，不能通过spec/systemd绕过安全拒绝。OCR与强审分别继续使用原专用入口。凭据不写进spec或日志。

`role-record`复用现有租约与绑定，业务事件和session采用可恢复journal；正常观察独立保存，不制造业务唤醒。`evidence-record`复用原operation runner，保存真实代码、声明输入/配置/环境与日志的身份；机械匹配不替代独立验收或审核。

`task_observation.py`由既有五分钟observer按显式项目spec执行固定只读采样；无spec不访问网络。正常心跳/CPU/mtime不改变业务通知，状态变化、异常、未知及恢复保留通知。每轮两次有硬超时的GET及一次只读SSH，预算最多45秒；不调用推理或改生产。群报显示完整日期、分钟和时区，后台时间保留原精度，发生/观测/补录分开。实施及回退见[2026-10-14记录](/home/wang/work/doc/analysis/quality-first-script-optimization-20261014/IMPLEMENTATION.md)。

部署脚本、隔离systemd验证、实际热加载结果与回退说明在 [实施记录](/home/wang/work/doc/analysis/hermes-runtime-optimization-20261013/REPORT.md)。单测：

```text
/home/wang/.hermes/hermes-agent/venv/bin/python -m unittest discover -s tests -t . -v
```

完整 discovery 需要解释器同时具备 Hermes 依赖和 pytest，并将本仓 `tests/` 加入 PYTHONPATH（部分既有测试使用平级导入）。Hermes venv 未装 pytest 时，用独立临时测试环境，不向运行中的 venv 安装包。本轮可复现命令及环境见修复记录；只用业务仓 venv 会缺 jsonschema/snowballstemmer 等 Hermes 依赖，不能将导入故障当业务回归。

单测使用临时项目和fake投递adapter；真实plugin discovery使用隔离HERMES_HOME。不会调用模型或向真实群发消息。部署不启动已暂停job、不取消已有回合；旧在途协调者需自然结束后再评估单入口效果。

范围纠正：普通私聊、未绑定群和其他项目会话保持原生模型处理；旧 `load_query_chat_ids` 不再读取，不能据历史诊断用途扩展拦截。参见 [2026-10-13范围修复](/home/wang/work/doc/analysis/hermes-runtime-optimization-20261013/SCOPE-FIX.md)。

人工模型问进度使用无参数 `role_live_progress`，由原生会话绑定角色/项目；不需开放terminal。脚本入口为 `runtime_tools.py live-progress --project ROOT --role ROLE`。当次核验 owner 启动身份、工程操作、审查服务和 worktree HEAD/dirty；已配置任务探针则只读即时采样，不发布通知或 ACK。模型结合实际核验与有效派单、检查点和回执解释当前阶段；缺失、失联或冲突具体说明。工具只返回当前事实和完整观察产物的哈希引用，避免重放原始长文档。`/progress`使用相同当次核验事实直接呈现，普通自然语言继续由原生模型答复。`report-input`保留有意义事件的业务记录基稿，不能代替人工当次核验。协调者保持七区，执行者/测试者仅自身四区。业务验收和历史成果仍按对应回执确认，PID存活、dirty或新读取时间不等于取得新进展；视图不替代TODO，不改变门禁或暂停状态。对用户默认用简体中文报告结论、依据、阻塞与下一步，不发送英文工作草稿。

原生pre/post/error API hook仅为已绑定角色记录请求身份与起止元数据，索引为`~/.hermes/runtime/role-time.db`；不存prompt、响应正文或凭据，不改变请求。cron会话按job绑定，群会话按原生chat_id及父session恢复。重试保持一个逻辑请求，计时故障不会阻断模型。客户端响应耗时含网络、重试、服务端排队和推理，不冒充纯排队；业务阶段占用与API耗时是重叠维度，不相加。历史日志仅能近似恢复已完成的成功请求。

五分钟文档检查在同角色owner存活时仍积压完整pending，不启动第二个执行模型；owner正常释放后再接续。stdout仅给定位预览，完整来源保留observer文件；必须实际处理后才ACK，不吞未读输入。

仅业务cron或当前全权且已持租约的回合，确有需要保存原消息时：

```text
python3 runtime_tools.py request --project ROOT --input 原生指令.json
```

request只持久保存，字段与引用/附件来源见runtime helpers。业务执行须同一角色租约、任务/attempt和操作幂等身份；旧PENDING保留处理。共享写入口的互斥不等于对任意terminal的系统级沙箱。

[当前用户说明](/home/wang/work/doc/SKILL-DEVELOPMENT-WORKFLOW.md#0-先看这里我该怎么使用飞书协调者) · [2.0调整与验证记录](/home/wang/work/doc/analysis/coordinator-interaction-reset-20261014/REPORT.md)

## 0.6 模型切换

在绑定角色群发送 `查看模型状态` 或 `/llm status` 可只读查看。修改须当前可信主消息明确 `全部权限：切换到云端模型`，也支持全权前缀后的 `/llm cloud sub2api`、`/llm cloud xcoding`、`/llm auto` 或 `/llm local`。`local`与`auto`同样只看本地等待队列，忙时云端；恢复auto保留已选云端。控制不调用模型，后续主推理请求生效。

自动模式只看本地 vLLM 等待队列：`waiting=0` 且 `running` 可读时用本地，不再看时间窗口，也不再看流水线状态。占用运行槽不阻止准入，但`running`或`waiting`未知仍走云端。未知和云端故障不放松保护。metrics 探测超时 5 秒（原 2 秒对 192.168.2.45 偏紧，单次超时即整轮判云端，造成本地/云端抖动）；超时值只为压抖动，不放宽"未知即云端"。范围是已绑定角色的群、cron及可追溯子会话；独立辅助调用与外部强审不在本次范围。OCR代码审查默认固定本地（`ocr_local_only: true`，独立于群会话模式，不探测队列、不回退云端）；显式设为 false 才允许 OCR 回云端。x-coding使用gpt-6.1-sol High。

配置、操作和供应商现场结果见[使用说明](/home/wang/work/doc/prd/hermes-model-routing/README.md)。需要核心 `resolve_model_route`、`pre_gateway_busy_dispatch` hook；升级Hermes后保留此通用补丁。协议接续验证：

```text
/home/wang/.hermes/hermes-agent/venv/bin/python scripts/validate_model_routing.py
```

## 2026-10-28 协调者报告与审查失败接续

协调者实质 business final 与审查故障现进入同源 outbox；20分钟短报保持原 no_agent 通道并记录正文 SHA/终态回执。晨报超预算先完整归档历史明细，全部验证及当前门禁保留。固定截止原样补审在派发前拒绝，已核实问题按原授权返修，新候选仍要求完整验证与审查。实现、热加载、实际采用与限制见[修复记录](/home/wang/work/doc/analysis/coordinator-reporting-repair-20261028/REPORT.md)。

## 0.7 状态、模型总结与活动上下文

协调者20分钟快报走 `role-status`，仅列四角色六态。个人11/15/18点及协调者08点走 `report_summary.py phase|morning`，脚本采集本人用量及相关事实，独立只读模型按原结构整理，原cron投递。总结任务不使用业务租约或确认pending；无模型输出不算生成成功。`report_summary.py status --input INPUT.json` 按精确原生execution查询生成/失败/投递状态。

`task-claim`在实际派单与固定工作区核验后事务记录ACK、事件和领取通知；新领取不要预先手写ACK。领取按项目/角色/task/attempt去重，恢复不重发。`slot-release`须协调者租约及有效交付证据，明确释放后待审可空闲，证据失效显示待核实。完成、失败和阻塞由角色模型总结，静默时段保持。

`context-input`提供当前检查点、相关任务定位、派单/pending及参考版本。`role-record`的可选 `pending_actions/open_gates/references` 数组维护恢复材料；同身份省略时保留、显式空数组清除。日志达到64KiB或400行轮转，完整归档/哈希/持久事件索引与journal保留。TODO/HANDOFF走 `document-archive` 语义整理，不能机械截尾。`continuation`只保存下一可执行动作，`continuation-ack`要求原输入指纹及独立完成证据。

`~/.hermes/runtime/role-usage.db`记录应用层请求尝试，按实际端点与路由配置分类本地/云端/未知；辅助调用归本人，独立OCR/Codex强审排除。用量按完成时间归入半开窗口，缺usage与旧进程采集缺口不填零。人工权限通过固定白名单函数重绑定对后续调用激活，记录源哈希；不重载整个模块、不重启owner。旧在途回合保留原调用栈与用量采集边界，缺可信原消息的旧群回合不能获得新执行授权。

现有cron迁移用 `scripts/configure_reporting.py --project ROOT` 预览，`--apply`沿用原job/群/时点/暂停状态。当前完整实现、验证与回退见[3.0实施记录](/home/wang/work/doc/analysis/role-workflow-v3-20261016/REPORT.md)。

2026-10-19稳定性修复：绑定隔离、忙消息原生FIFO、原生turn租约恢复、跨项目只读订阅、不可变接续、独立测试候选核验、用量冻结及长cron owner保护。详情与实际部署边界见 `../doc/analysis/role-workflow-stability-20261019/REPORT.md`。

## 0.10 固定角色池与需求选择

角色池固定关联 `/home/wang/work/doc/prd/README.md`，五角色群与 job 身份保持；具体业务根由 `~/.hermes/role-pool.json` 的当前选择解析。索引只导航，TODO、任务、ACK、回执和门禁仍在具体需求根。兼容包到旧运行根的映射必须明确配置。

所有角色群回合默认只读，当前可信原文明确“全部权限”后才可执行业务动作，并须取得本角色共享租约。协调者可发送 `全部权限：切换需求 rosbag-catalog-raw-collection`，或用只读 `role_requirement_catalog` 查询后，在同一个明确全权回合用 `role_requirement_select` 登记用户原文指定的需求。选择属于控制请求，不抢业务租约；后台在旧 owner 和独立操作的安全点切换。后台在已接受需求范围内持续执行，不需每轮群聊全权。

`runtime_tools.py requirement-list|requirement-status|requirement-resolve --pool PATH` 只读查询；resolve 另带 `--role ROLE`。`scripts/configure_role_pool.py` 默认只预览，`--apply` 安装通用 wrapper 与 job 范围配置，保留原时点、群和暂停状态，初始化时保留当前混合业务根。后续每个新 cron worker 在 pre-script 前冻结需求根、代次与会话归属，旧代次写操作拒绝；journal 恢复不覆盖并发变化。

切换等待期间新旧任务不并行起跑；协调者仅在已知独立操作尚待收口时获 `drain_only` 回合，核既有结果和裁决，不新派单或提交长操作。固定提交入口拒绝 drain 回合新领取、长操作、OCR 与强审；这不是对任意终端命令的系统沙箱。报告只在脚本采集事实时持短 admission 锁，模型总结不占业务槽；若采集前范围已改变则明确失败，避免跨需求拼接。完成后保留当前选择并待命，不自动挑选下一个需求或停止固定 jobs。


## 原生最终验收（2026-10-30）

ACTIVE 模式的本地开发自动化包含需求级最终验收：全部开发任务真实合入后自动冻结集成版本、排队 TESTER；失败按问题 ID 回原 owner，返修走原门禁；最终 PASS 后由协调者复核证据并本地收口。规划者仍手动触发；开发叶任务完成与需求验收完成分别记账，无需再向用户索要内部派单口令。详见 [机制修复记录](FIXES-20261030.md)。
