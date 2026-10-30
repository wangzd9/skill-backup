**让协调者集中做语义裁决：基于当前代码的具体调整清单**

本次按 `/home/wang/work/hermes-role-runtime` 的 `f332dc489089d95a3a6054ecf5a81e97aeddbd2a` 分析；初稿基于deafa31，续查已纳入新增的token优化提交。目标是减少查找、拼接、抄写、分页和纠错，让协调者把精力用于需求含义、证据覆盖、问题处置与交付判定。保留现有账本、独立角色和事务门，不增加一个常驻协调模型。

以下正文保留方案分析时的基线；经用户授权，后续已进入0.15.0实现与验证，另合并补验异常闭环修复。逐项审查、保守落地范围、回归及实际采用以[本轮实施记录](SEMANTIC-DECISION-IMPLEMENTATION.md)为准；不能把下面的基线说明当成实时上线状态。原分析阶段未修改运行机制、未启动外部模型/审查服务或业务测试。[已有实施记录](/home/wang/work/doc/analysis/coordinator-health-audit-20261030/IMPLEMENTATION.md)记载0.13.1已热加载；此处0.14.0未核验的描述属于方案分析基线；其后0.14.0的重启采用见Token优化记录，本轮0.15.0的采用另见上链实施记录。此前[token专项统计](/home/wang/work/doc/analysis/coordinator-health-audit-20261030/TOKEN-CONSUMPTION-REVIEW.md)采于14:47，是修复前基线，不能当作这些修复上线后的运行成绩。

当前并非没有程序辅助。已有 `next_actions`、失败关闭预览、证据哈希引用、逐AC覆盖矩阵、结构化问题处置、显式ACK、无进展退避及测试预检。f332dc4又增加了包内精确去重、终态动作授权归档、按角色的原生工具目录和脱敏hook字节观测，这些不再列为从零开发事项。接下来应把这些能力连接成“准备事实 → 展示待判断问题 → 模型核证裁决 → 原入口复核执行”的完整路径。

**建议的职责边界**

| 工作 | 程序准备/核验 | 协调者保留的判断 |
|---|---|---|
| 候选与版本 | 固定run/task/attempt/head/AC版本，列差异和证据来源 | 差异是否影响原义务及其他模块 |
| 测试与审查 | 校验来源、哈希、固定对象、真实case和原始结果 | 场景与断言是否充分，发现是否成立 |
| 历史失败 | 列未关闭FAIL、可关联的新证据、原裁决 | 修复是否覆盖原失败，是否提交关闭关系 |
| 用户答复 | 登记原文、提问关联、选项和当前对象 | 答复究竟解决哪个业务分歧 |
| 操作执行 | schema、角色、互斥、幂等、授权、版本和事务门 | 选择动作并给出可审查依据 |
| 汇报 | 当前状态、时间、版本、数量及事件引用 | 解释影响、风险及优先顺序 |

下列接口在原分析时属于建议；本轮已实现的入口和未完成项见实施记录逐项表。

1. **先统一裁决材料的采样上下文，减少模型自行对账。**

   代码现状：[snapshot](/home/wang/work/hermes-role-runtime/business_ledger.py:426)已在读事务内取得一致快照，并支持按run及event_after查询。但[current_input](/home/wang/work/hermes-role-runtime/workflow_runtime.py:447)先取一次快照，[next_actions](/home/wang/work/hermes-role-runtime/workflow_progression.py:82)又取快照、查询验收及仓库状态，[最终验收动作建议](/home/wang/work/hermes-role-runtime/workflow_completion.py:448)再次取快照。材料来自多次采样，不能仅因处于同一个工具回包就视为同一时刻；本轮未复现具体跨时刻冲突。

   具体调整：组装开始时生成带run、generation、事件水位及相关对象版本的只读上下文，显式传给各投影函数。账本事实尽量共用同一快照；Git、原始文件等外部事实另记采样时间和哈希，必要时前后核对。允许入口继续兼容旧签名，逐步消除视图内部重复取快照。不要将event_after直接套到依赖完整历史的函数上；应先建立可重建的当前状态投影，再增量获取事件。

   协调者得到：哪些事实来自同一快照，哪些是稍后采样，哪些已有变化，无需自行比较多个工具里的零散状态。

   验收：每项判断能追到对象版本和来源；材料不一致时显式标为待刷新。执行仍按最新权威事实重新校验，准备快照不成为授权或放行依据。

2. **把默认启动输入从run级资料集合改成面向本次判断的材料。**

   代码现状：[current_input的数据选择](/home/wang/work/hermes-role-runtime/workflow_runtime.py:466)对协调者收集本run任务及其历次候选、验证和动作，再加入completion、历史失败等内容；[work_packet](/home/wang/work/hermes-role-runtime/workflow_views.py:47)压缩长字段和执行历史，并已调用[project](/home/wang/work/hermes-role-runtime/workflow_model_input.py:55)归档终态授权、对相同原文使用包内指针。[deduplicate](/home/wang/work/hermes-role-runtime/workflow_model_input.py:9)保留全部阶段义务和门禁结构，不依赖跨会话记忆。这解决了部分重复，却尚未按本次判断选择材料。[seal_input](/home/wang/work/hermes-role-runtime/workflow_runtime.py:543)超过32KiB仍仅设置requires_paging。输入事件页已有限长，不等于整份启动包已经有界。

   具体调整：在已有保真去重基础上，默认包保留身份、当前目标、所有待决事项的完整目录、未闭合门禁和紧急阻断摘要；按当前判断展开相关候选、义务、证据及其依赖。终态历史保留稳定ID、必要结果、关闭关系和完整原文引用；涉及当前FAIL、授权或争议的历史必须重新进入判断材料。同一对象继续复用现有包内指针，不再新增第二套去重协议。

   面向判断至少区分：审查结果裁决、候选批准/返修、最终逐AC收口、用户答复核对、文档审核。不同判断有不同必读内容，不能把它们都塞进一个固定大包。总目录不能只显示“最推荐的一项”，否则可能遮住其他紧急问题。

   协调者得到：本轮有哪些判断要做、每项还缺什么、相关证据在哪。每个新会话仍有足够状态快照，不能只给增量并假设模型记得上一会话。

   验收：全部未关闭FAIL、UNKNOWN、控制边界和待决项可枚举；不静默删除关键事实。材料超预算时明确分组、页数和未展开项；不能靠裁掉义务满足硬上限。字节与实际token分开记录。已有重建测试能检查去重可逆，但不能据此证明模型理解指针后的裁决质量等价，仍需真实判断场景验证。

3. **增加按对象批量取证的只读入口，减少找文件和手工拼JSON。**

   代码现状：[rows_view](/home/wang/work/hermes-role-runtime/workflow_views.py:22)以detail_source/index/field引用长字段；[大查询分页](/home/wang/work/hermes-role-runtime/workflow_query.py:106)将序列化JSON按约2,000字节分块，并要求拼接后重新解析。它保证保真和传输边界，却未必适合一次裁决的阅读边界。当前[role_work_input](/home/wang/work/hermes-role-runtime/__init__.py:1137)提供事件范围查询，没有按候选、发现或义务选择证据的专用接口。

   具体调整：增加建议接口 `role_decision_context` / `role_evidence`。运行时绑定身份，参数只允许本work合法关联的候选ID、action_id、issue_id、obligation_id等，以及有限条批量请求。返回按发现、case、文件/函数组织的完整片段、来源哈希、版本、位置及后续读取引用。稳定ID应优先于跨快照容易变化的数组index；原始完整文件仍能读取。

   批量请求逐项报告FOUND、MISSING、CHANGED或UNVERIFIED，限制条数和总量；不能因部分成功就声称整组证据齐备。小对象不必再经manifest、index、page多层跳转，大对象仍保持有界读取。[work_input_tool](/home/wang/work/hermes-role-runtime/__init__.py:459)第495行每次非历史翻页仍调用current_input，连同非事件材料一起组装。应提供兼容的events-only读取方式或独立事件工具，只返回该页、冻结边界、next_after及ACK信息，避免翻一页就重发一次全部工作状态；首次启动和显式刷新仍返回所需状态快照。

   协调者得到：一次拿到同一问题的合同、报告发现、相关代码及测试定位，直接核对语义。程序只能验证引用或返回确定片段，不能凭关键词自动宣布该问题已覆盖。

   验收：原始证据可还原且版本一致；读取不ACK、不启动服务、不自选其他run；完成同一判断的查询轮数下降，完整证据展开能力不退化。

4. **将自由文本checkpoint升级为带版本的工作记忆，保留兼容性。**

   代码现状：[checkpoint合同](/home/wang/work/hermes-role-runtime/workflow_contracts.py:37)的正文仍是任意object。[账本checkpoint](/home/wang/work/hermes-role-runtime/business_ledger.py:1112)已拒绝误置控制键并返回实际水位；[checkpoint_history](/home/wang/work/hermes-role-runtime/workflow_query.py:41)也明确标注历史叙述。这些修复应保留，下一步需要减少下轮重新查找和验证同一批材料。

   具体调整：新增可选schema版本，规范记录判断所针对的candidate/head/AC、已读来源及哈希、已确认接口、已提交裁决ID、未决问题及下一处文件/函数。采用双层结构：程序提供当前权威事实，模型记录有证据的结论和具体下一步；不要重复抄写整份状态。沿用现有waiting_external格式，若引入结构化等待，另设显式迁移规则，不能直接使旧检查点失效。

   接续时程序按各条记忆的依赖核验版本：未变项可标为“此前已核实且依赖未变”，改变项列出失效原因。不能因文件哈希未变就认定结论永远有效，AC、调用方、环境和授权变化也可能使判断失效。仅复制一个哈希不构成已读证明；应有本次/此前读取来源与裁决关联。新引入但此前未读的证据仍需展开。

   验收：新会话无需从零定位；过期结论不能冒充当前事实；检查点保存、事件确认和业务完成仍是不同操作。只保存可审查的结论、依据和下一步，不要求记录模型内部推理过程。

5. **把next_actions完善为有依据、可填写的动作建议，减少临场猜参数。**

   代码现状：[next_actions](/home/wang/work/hermes-role-runtime/workflow_progression.py:140)已有审查裁决、失败关闭预览、授权选择和合入建议，但不少动作主要由action、reason及少量ID组成。[执行入口](/home/wang/work/hermes-role-runtime/workflow_runtime.py:624)已经在副作用前校验schema，最终仍由账本和专用执行入口检查业务门。

   具体调整：建议统一包含action、固定目标、采样版本、已验证前置、未满足前置、必读证据、确定字段的payload草稿、需要协调者填写的语义字段，以及失败恢复方式。ID、事件引用、候选版本由程序填；PASS/FAIL、关闭哪些历史失败及理由由协调者核证填写。只读准备逻辑复用现有门禁查询，不应通过执行真实命令再回滚模拟预检。

   可提供基于相关对象版本的材料摘要指纹，提交时检测是否需要刷新；不要把全部事件水位当唯一失效条件，否则无关心跳也会使材料作废。指纹不是新授权，不能替代现有候选、证据和权限校验。动作提交继续走稳定command_id；未启动的参数纠错与新逻辑动作应有清楚的ID规则。

   验收：建议与执行使用同一套合同；缺字段时给具体字段和修正方法。过期材料明确报变化原因，不能自动改绑新候选或将旧裁决套给新版本。

6. **为语义裁决准备可直接审核的表格，自动填引用，不自动填结论。**

   代码现状：[validate_matrix](/home/wang/work/hermes-role-runtime/workflow_acceptance_obligations.py:52)已核验合同、场景、环境及JUnit case；[closure_review](/home/wang/work/hermes-role-runtime/workflow_acceptance_obligations.py:98)要求逐义务语义审查。[问题处置](/home/wang/work/hermes-role-runtime/workflow_issue_policy.py:40)要求非阻断事项有依据。[service_decide](/home/wang/work/hermes-role-runtime/workflow_contracts.py:63)则仍主要提交result、summary和supersedes，[服务裁决入口](/home/wang/work/hermes-role-runtime/business_ledger.py:1642)复核完整源证据。

   具体调整：按适用合同自动列出“原义务/场景—实际断言—环境—case/报告—待核对差异”，以及“历史FAIL—原证据—新证据—建议关联”。预填obligation_id、validation_event_id、issue_id和证据定位，结论及理由留空。必要时对审查发现增加逐项处置记录，保留原始发现和来源，不把摘要改写成消除发现。现有逐AC要求适用范围继续按冻结合同判断，不能悄悄改非MN合同的验收方式。

   对用户答复也采用同一模式：[record](/home/wang/work/hermes-role-runtime/workflow_user_decisions.py:36)目前会附上此前协调检查点作为上下文，[resolve](/home/wang/work/hermes-role-runtime/workflow_user_decisions.py:77)负责明确关联。后续应持久保存提问ID、选项原文、相关对象版本与收到的答复，程序准备对照表，协调者判断其含义；歧义仍需澄清，不能程序猜选项，也不能要求用户重复已经明确的答复。

   验收：协调者主要填写“证据为什么满足/不满足义务”和“问题如何处置”，不再抄编号；相同原始证据仍可复核，表格不能预填PASS或替代独立测试、OCR与强审。确认用户答复不等于授予新权限。

7. **程序组装审查背景和结果材料，协调者补充业务风险。**

   代码现状：[_request_inputs](/home/wang/work/hermes-role-runtime/workflow_services.py:71)要求OCR明确背景输入；[_freeze_background](/home/wang/work/hermes-role-runtime/workflow_services.py:56)负责冻结模型提供的background_text。[服务提交](/home/wang/work/hermes-role-runtime/workflow_services.py:261)已能选择已有授权，[event tick](/home/wang/work/hermes-role-runtime/workflow_runtime.py:940)也已自动收集在途服务结果，不能再将它描述成只能靠协调者手动轮询。

   具体调整：按冻结合同、任务规格、候选差异、测试结果、历史失败和接口依赖，生成带来源的背景草稿及审查清单。协调者补充风险问题、确认范围与必要背景后，沿原提交入口冻结整份材料。服务结果就绪时，程序整理覆盖缺口、原始发现、代码位置、证据哈希及历史失败关联，供协调者判定。不要另建收集循环；复用现有tick、服务目录和副作用幂等身份。

   验收：同一来源内容不需反复复制编写；关键不利发现和未覆盖项完整保留。不能因程序材料组装成功、服务终止或coverage_complete，就自动写业务PASS；SUBMITTED/UNKNOWN仍不得盲重发。

8. **核验已精简工具目录的实际采用，并完善按动作查合同的能力。**

   代码现状：f332dc4已将[prepare](/home/wang/work/hermes-role-runtime/workflow_runtime.py:183)改为调用[configure](/home/wang/work/hermes-role-runtime/workflow_toolsets.py:4)，协调者/测试者使用明确的只读工具及本角色命令，A/B/C另有编辑工具；报告会话只读。[_tool_decision](/home/wang/work/hermes-role-runtime/workflow_runtime.py:576)继续保留底层守卫。因此，“移除不该出现的普通terminal/write_file/patch”已属于源码已有能力，不能继续写成尚未实现。

   具体调整：沿用现有configure，在新会话实际发往提供方的工具目录中核验其采用状态；旧会话在安全交接后切换目录，不为更新工具强行终止在途任务。新只读取证工具应在目录和底层READ_TOOLS/守卫中同步登记并保持身份边界。补充按当前动作定位完整合同、最小有效例子及字段说明的能力；不要为省schema而让模型靠报错猜参数，也不必每轮重新浏览全部角色命令。

   [command_schema](/home/wang/work/hermes-role-runtime/workflow_contracts.py:142)明确兼容主机剥离顶层组合schema的行为。若后续拆分大命令工具或按需披露合同，需要验证实际提供方看到的action与payload关系，不能把已经修好的完整参数说明又压没。旧会话在安全交接后更新工具目录，底层权限检查始终保留。

   验收：协调者能直接发现合法的查证、日志、文档和合入入口；确定不允许的普通写工具不再被推荐。[新增工具选择测试](/home/wang/work/hermes-role-runtime/tests/test_token_optimization.py:111)已覆盖原生选择及denylist路径，本轮未重新运行。还需观察真实会话工具目录和无效选择是否改善，真实取证能力及底层拒绝能力不减少。

9. **统一短错误回执与恢复指引，避免每次失败重发整套角色说明。**

   代码现状：[命令预检](/home/wang/work/hermes-role-runtime/workflow_runtime.py:635)已返回INVALID_NOT_STARTED、retryable和new_command_id_required；[测试预检](/home/wang/work/hermes-role-runtime/delivery_evidence.py:505)已有字段级信息，应直接复用。能力拒绝仍在[这里](/home/wang/work/hermes-role-runtime/workflow_runtime.py:598)拼接整份usage；[usage](/home/wang/work/hermes-role-runtime/workflow_tool_bridge.py:17)包含大量与当前错误无关的角色细则。[诊断回执](/home/wang/work/hermes-role-runtime/workflow_diagnostics.py:203)已经有分类、能力、身份及副作用状态。

   具体调整：把规则与工具用法作为稳定、带版本的合同；单次错误只返回code、发生阶段、是否启动/是否提交、副作用状态、无效字段、是否可重试、是否换ID、可用入口与诊断引用。完整合同和capture按需展开。外层参数类型错误、schema错误、业务门拒绝和服务未知状态也应遵循一致格式，不只覆盖其中一条分支。

   同码、同对象、同依据版本重复拒绝时，提供已有诊断和合法下一步；机制故障登记到维护责任与恢复条件，不能长期写成“等用户开放权限”。分类UNKNOWN必须保持未知，不能为给出恢复指引而猜测无副作用、盲重发或开放原本禁止的能力。

   验收：模型无需读几千字通用说明才能修一个字段；执行是否启动与ID能否重用始终明确。完整诊断仍可审计，恢复提示不扩大权限。

10. **用待决事实和实际进展控制唤醒，统一ACK的进展口径。**

   代码现状：[admission_hint](/home/wang/work/hermes-role-runtime/business_ledger.py:481)已有等待及角色准入；[dispatch](/home/wang/work/hermes-role-runtime/workflow_dispatch.py:320)已有唤醒合并；[resumption](/home/wang/work/hermes-role-runtime/workflow_resumption.py:5)已对无进展接续退避，不能再建议从零实现这些功能。

   仍需补齐的具体路径：resumption第11～15行只从CHECKPOINT_SAVED的acknowledged/processed_through变化识别推进；但[ack_events](/home/wang/work/hermes-role-runtime/business_ledger.py:1105)直接更新水位并返回，没有产生同样的checkpoint事件。静态看，两条合法ACK路径的进展基线不一致，独立ACK可能未被这套历史计数识别。本轮未执行复现或确认现场误退避。

   具体调整：用持久化的执行前后水位/推进回执统一计量独立ACK和checkpoint ACK；再按待决事项及其相关对象版本判断是否有新工作。定时tick继续检查状态、超时和故障，模型主要在新材料、材料变化、必要裁决、明确接续或到期复核时启动。安全核退、控制动作和新业务结果不得被一般退避挡住。用事件合并降低重复启动，不能合并掉业务含义。

   “无命令提交”不能单独当作无进展：阅读长报告、核对跨模块语义也可能是必要工作，需要有界接续与可核验交接。ACK只表示已处理连续事件前缀，不代表语义问题全部关闭；只有ACK也不能当作已完成业务判断。

   验收：两种ACK对退避统计一致；新结果及时唤醒；没有相关变化时避免重复启动。历史查询不ACK，自动轮询不能代模型确认未读事件；判断未完成时仍能继续工作。

11. **把重复抄写状态的报告和日志改成事实自动填充、判断增量补充。**

   代码现状：[报告构建](/home/wang/work/hermes-role-runtime/workflow_reports.py:100)已程序汇集事实，[进度查询](/home/wang/work/hermes-role-runtime/workflow_query.py:263)也提供有界现状与材料索引；[开发日志](/home/wang/work/hermes-role-runtime/workflow_coordination_log.py:10)仍主要接收协调者自由文本。已有报告渲染、通知和幂等写入应继续复用。

   具体调整：从同一已提交裁决及其来源派生事件ID、候选版本、检查状态、责任人和时间等确定字段，协调者补充影响、理由、风险和下一步。将可复用的语义结论关联裁决版本，避免为了群回复、checkpoint、开发日志、交付摘要各重新拼接一遍旧材料。报告中的实时事实重新查询，旧结论明确标注适用版本；不能把已过期的“等待”直接照抄成当前阻断。

   验收：同一事件在各视图中的版本和结果一致；日志重试不重复追加；报告仍保留适用的七区/个人格式和原通知时点。不能让自动状态文本替代应有的业务解释。本项收益应排在前面的大上下文和工具往返之后，旧统计中summary本身只占0.077%输入。

12. **新增“完成一次有效判断”的成本与质量关联，而不只计会话数。**

   代码现状：[用量记录](/home/wang/work/hermes-role-runtime/usage_accounting.py:180)已有物理请求归属，[调度测量](/home/wang/work/hermes-role-runtime/workflow_measurements.py:191)已有提交至派发/结果可见等阶段；新增的prefix_audit记录脱敏hook可见字节，[workflow_model_input](/home/wang/work/hermes-role-runtime/workflow_model_input.py:70)记录投影前后字节，二者都明确不冒充实测token。尚需把这些指标与具体待决事项、材料版本和裁决结果关联起来，才能判断搬运是否减少。

   具体调整：用稳定的待决事项ID关联多轮材料准备、查询、物理模型请求、裁决及后续返修。仅记录组装各部分的大小/token覆盖、来源哈希、证据展开次数、拒绝恢复轮数和耗时，不新增提示正文或模型内部推理日志。一个请求可能处理多项判断，应保留共享成本或明确分摊，不能把同一请求token完整计入每一项后相加。

   验收：对同类事项比较总输入、工具往返、从材料就绪到裁决的延迟、最终交付耗时、漏义务、返工和用户介入；同时保留未完成事项及其年龄，不能靠只统计已完成事项美化数据。先保存基线，再逐批上线。10%～20%输入下降仍是验证目标，不能据源码承诺质量或效率无退化。

**建议实施顺序与验证范围**

| 批次 | 调整项 | 重点验收 |
|---|---|---|
| 基线同步准备 | 第12项的最小关联与指标 | 保留未完成事项、真实请求分母及质量/耗时基线 |
| 第一批：先减少确定的机械纠错 | 第8项的采用核验、第9、10项 | 已有工具精简实际生效、短错误可恢复、两条ACK进展一致、新反馈及时处理 |
| 第二批：减少材料查找与重复传输 | 第1、2、3、4、5项 | 同源快照、完整待决目录、按对象查证、带版本接续、提交前重新核门 |
| 第三批：提升每次判断的材料质量 | 第6、7、11项 | 逐义务/逐发现审核、服务背景保真、语义结论与报告复用 |

上述批次可再拆为独立交付，不宜一次大改current_input、事件确认、工具目录及裁决接口。每批保留旧接口兼容或明确迁移，并能回退新增视图而不回退已落账裁决。

已有[健康修复测试](/home/wang/work/hermes-role-runtime/tests/test_health_audit_repair.py:19)覆盖新页、嵌套控制键、退避、覆盖矩阵和收口等路径；本轮只阅读测试，没有重新运行，也不把测试源码存在当作本轮通过证明。后续应增加与本次改动直接相关的行为验证：按对象视图不漏阻断、原文可还原、跨会话版本变化使结论失效、独立ACK与checkpoint ACK一致、准备后对象变化被提交入口拒绝、实际工具目录符合角色、同一裁决生成的各视图一致。真实任务还需观察返工和交付延迟，不能只靠JSON变短宣布优化成功。

具体代码位置、文件哈希和版本记录见[源码证据清单](/home/wang/work/doc/analysis/coordinator-health-audit-20261030/semantic-decision-code-evidence.json)。
