# 机制维护交接（2026-10-31）

本文件记录本轮维护流水，不替代原生只读账本。

## 交接检查点的格式错误（0.17.11）

Foundation正式计划已发布e15442，B/C已分别领取REC-01/BR-00（e15443/e15445）。协调者随后把checkpoint payload传为字符串，预算层在格式校验前对字符串调用.get抛异常，原生只显示权限核验失败。修复为无副作用格式拒绝和准确对象示例，不解析字符串、不放宽权限/预算/ACK。首次实际文件变更仍待单独核验，详见专项记录。

## 计划版本与已接纳编号（0.17.10）

原生v6稿已修正为真实文件范围，随后因把尝试次数6填作首计划版本、把TASK-FND-REC/SRC-01缩写为TASK-REC/SRC-01继续被拒（state.db 380762、380764）。prepare原先不提供这些确定性字段，拒绝也没有期望值。现准备材料预填准确version并列已接纳ID，两个拒绝回执补当前/期望/提交版本及全部未知ID；schema和next_actions说明版本语义、任务级required_checks。无自动重命名或写业务计划，未知任务与版本门不变。

## 发布计划合同及预检（0.17.9）

Foundation新scope已原生绑定e15437，连续四版计划因缺验收字段、依赖并集、晚期提供方引用和阶段单调性逐项被拒；期间改参复用冻结command_id还触发冲突。工具schema原先没有完整声明这些既有要求。现补条件字段、依赖关系和scope文件路径说明，汇总无状态合同错误，并明确未启动校验可沿用ID、事务门拒绝改参必须换ID。实际业务依赖和提供方接口仍由原生协调者冻结，不代填、不降低账本校验。详见[专项记录](REQUIREMENT-PREPARATION-REPAIR-20261031.md)。

## 激活后的原生scope交接（0.17.8）

两专题已真实接纳（Foundation e15416、Spray e15423），Foundation选中e15432、旧work释放e15434。激活回执和events_only页补明确的safe_release→结束回合→原生重绑指引，避免旧scope继续筹划新run；不强清writer，不越权派单。业务正式计划及代码开工仍另看真实回执，见[专项记录](REQUIREMENT-PREPARATION-REPAIR-20261031.md)。

## 准备提交的引用兼容（0.17.7）

16:00原生owner真正提交两包prepare_requirement，但因给原指令任务附了冗余来源映射被整单拒绝（state.db行380531/380532）。修复接受并验证本次任务集合内的附加证据、需求根内精确冻结source的绝对路径，工具schema补明确说明；不接受未知任务、跨根/..、伪造引用、无源提供方或历史重启。测试及现场后续见[专项记录](REQUIREMENT-PREPARATION-REPAIR-20261031.md)。尚无真实接纳/计划/领单前不得称业务开工。

## 新需求接纳入口（0.17.5）

原“统一推进”消息的 authenticated/native_primary/TURN_BOUND 记录完整，正文哈希一致；拒绝来自句首正则，协调者将复合错误误判成身份缺失。修复措辞识别、来源诊断、双需求幂等（兼容旧回执）、ACTIVE只读目录旧映射和异步热加载，108项相关回归通过。两 prepared run 的 BR/SR 提案仍缺正式合同登记；保留此门，不改业务状态、不冒充已接纳/开工。证据、部署和范围见 [专项记录](REQUIREMENT-INTAKE-REPAIR-20261031.md)。

## 最终交付阻塞恢复

- 事实：原生请求 `final-delivery-80243bcfdb116ca44b0ba1bcf3bf3b97` 保存旧 main `ba9e367` 的 quality_gate 失败。业务 main/GitHub 已推进到 `aba3ac8`，既有成功验证没有自动导入原生账本。
- 根因：原执行器对 BLOCKED/UNVERIFIED 直接返回，重复请求又被拒；next_actions 忽略阻塞交付。报告只枚举任务和旧问题，漏读 e14612 的 issue_dispositions。
- 实现：同一 request_final_delivery 支持 `{request_id, expected_head, reason}` 的可信精确恢复授权；原 owner 的 finalize_delivery 仅对账 Git 和重跑本地 mock up/test/quality_gate。保留原阶段/失败日志，每个恢复版本独立事件、操作ID和回执。明确失败和无副作用预检拒绝可在满足新前提时显式恢复，UNKNOWN/中断不可盲重试，同 head 不原样重复。
- 报告：风险与下一步保留最终交付责任；展示正式验收处置、真实阶段未完成义务；缺任务编号不打印 None。
- 已执行：148 项隔离回归通过；最后报告修订后 48 项定向回归通过、随后报告版本区分用例 1 项通过；真实只读入口 8080 字节及中文七区格式通过；编译和差异检查通过，协调者 Skill 校验通过。
- 验证与上线记录：见仓库外 `doc/analysis/coordinator-health-audit-20261030/FINAL-DELIVERY-RECOVERY-20261031.md` 和 `.local/final-delivery-recovery-20261031/`。代码提交后必须完成固定 OCR、热加载和原生回执核验，未完成前不声明交付成功。
- 范围：只维护本机机制与本地测试栈，不部署处理机。原特性分支验收快照仍是 e82af002，真实 MySQL 分配并发、NAS 实物和远端代理义务未冒充通过。
- 可复用性检查：更新现有协调者 Skill 的 runtime-helpers 和两份流程指南，不新增重复 Skill。


## 原生执行的 Python 环境隔离（0.17.4）

首次真实恢复已完成 Git 对账、mock up/test，但质量门禁在 pytest 收集阶段报 15 个 ModuleNotFoundError。实际继承的 PYTHONPATH 使 tests 来自 Hermes 自身的 tests/__init__.py；同一业务仓/解释器清除该覆盖后可定位本地 scene_fixture。修复最终交付及恢复子进程清理 PYTHON*/PYTEST_* 覆盖，不改业务代码/门禁。恢复身份新增执行器源码 SHA，真实机制修复可在同一业务 head 上显式重新授权，仍拒绝相同 head+执行器的失败重放；执行时核对源码不漂移，历史恢复阶段也完整进入 artifact。

34 项真实隔离 Git/账本回归通过，包含继承 PYTHONPATH/PYTHONHOME/PYTEST_ADDOPTS 时原交付与恢复均隔离、同版本拒绝重复、执行器变更重绑与漂移拒绝、前轮失败历史保留。编译和差异检查通过。生产原生重验、补丁 OCR 与最终结果仍以专项报告为准；未取得新真实回执前不宣布交付完成。

## ACTIVE维护CLI进度分流

真实入口核查发现，无池的`runtime_tools.py live-progress`仍使用LEGACY派单投影，可能把当前ACTIVE角色列为暂停并漏掉最终交付。新增明确`--pool`及原生`HERMES_ROLE_POOL_PATH`分流；先验证当前run需求根匹配，再调用与原生工具相同的workflow_query/live报告渲染。非ACTIVE池、错项目和读取中run改变直接报错，不回退旧事实。未指定池保留LEGACY兼容，指南明确不把它用于ACTIVE。

新增CLI隔离真实Git/账本回归，加既有live_query、coordinated_reporting及delivery_recovery共56 passed /40.66s；测试确认报告含当前交付且业务SQLite字节未变。编译、差异检查后发布。此改动仅影响新CLI进程，不改恢复执行器SHA，不重新授权或接管原owner；原生完成及OCR仍以专项报告最终核验为准。可复用性检查更新既有协调者runtime-helpers与用户/流程指南3.37，Skill校验通过，不新增重复Skill。

## 原生恢复终态核验（13:11）

原协调者同一work已实际完成恢复：reconcile e15180、local_up e15183、local_test e15186、quality_gate e15189，四步退出均为0，最终交付COMPLETED。完整门禁2166 passed /20 skipped /25 warnings /311.96s，Ruff规则、320文件格式、编译、浏览器交互和本地Compose解析全部通过。业务main/GitHub同aba3ac89efedfd93a693fcdd943e802271c44d1f，工作区干净；本轮只对账Git，不重新merge/push。12份原失败阶段文件哈希不变、首次环境污染失败仍保留，原验收快照e82af002未改。

completion.json核同Gateway PID3612975、70个激活源码哈希一致，无重启/接管；当前只读核心8052B、七区格式通过，20项real_nodes/production义务仍未授权未完成。处理机Compose只在开发机解析，未连接或部署处理机。机制OCR与备份推送尚在收尾，不把在途审查的未知问题总数写成0；后续两个零调用prepare合为31cd7a5..6c7df78的7文件范围审查，保留旧材料且不分别启动。

13:23机制源码、指南和原生终态证据已同步既有GitHub skill-backup仓main：7009b9cc7db115a7fec4f4a9b29293c836ea8cca，远端回读一致。备份包括canonical源码快照734c3a8（代码6c7df78）、core c3901f5累计可还原补丁、六个Skill及选定文档；1204项manifest按拟提交内容核SHA。未跟踪旧文件及运行账本/凭据未纳入。OCR仍在运行，云端记录未冒充审查通过，后续按审查真实终态补收尾记录。

## OCR证据阻塞（13:34）

主run05febce8d9d843959789d6993eae1235的b01四文件触发3900秒HARD_DEADLINE，service已退出；b02六文件覆盖完整、可读comments0。整体results仍INSUFFICIENT_EVIDENCE，问题总数未知，不接受整个机制审查，也不将未知critical/high写成不存在。固定流程失败停链，followup-v2（run e3c91b81ea3a44b481d8710912a90fcd，31cd7a5..6c7df78的7文件）仍PREPARED、未start；2b9f3fd与6c7df78的独立OCR未执行，原因是主审硬截止后停止新准入。两个单独prepare同样零调用、由合并材料取代，不另启动。

证据：.local/final-delivery-recovery-20261031/ocr-results-final.json和review-decisions.json；原始会话不进Git。下一步先定位主四文件批次未完成原因，再有实质处理后按固定入口补齐审查；同代码/参数/截止仅补reason不能重跑，不改冻结材料、不延长预算。业务最终交付已经COMPLETED，不因OCR证据缺口重放merge/push/本地栈或改旧回执。外部TODO保持[~]，不得宣称全项完成。

## 双专题准备链闭环（0.17.6）

前次0.17.5未提供后台合同准备能力，故两个包仍未开工。新增持久准备请求、带CAS和文件意图日志的prepare_requirement、重新登记后复用原生来源接纳及activate_requirement安全交接；B只记待关联答复，不新增权限。原始问题、回归和真实采用分别见[准备链修复记录](REQUIREMENT-PREPARATION-REPAIR-20261031.md)，不以机制测试代替业务开工证据。

现场准备请求已真实入账Foundation e15395、Spray e15396，复用原用户message ID；B恢复e15394、原问题关联e15400、后台裁决关联完成e15405。原生e15404已接续并读取冻结合同/提案，但尚无新需求接纳/计划/首派单回执，不能报告业务已开工。运行中Gateway PID3612975未变、71份激活源码SHA一致；140项隔离回归通过。源码a564c7c，本地备份f682e59；未远端推送、未重启或接管owner。
