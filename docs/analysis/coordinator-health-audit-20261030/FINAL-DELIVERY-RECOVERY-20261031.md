# 最终交付阻塞恢复及协调者报告修复（2026-10-31）

本文以当次只读入口、Git、隔离回归和真实原生回执为证据；不是历史 checkpoint 的当前化。当前交付结果以下方“最终核验”记录为准。

## 已确认事实与根因

2026-10-31 12:14，当次 `workflow_query.live_progress` 显示原请求 `final-delivery-80243bcfdb116ca44b0ba1bcf3bf3b97` 在旧版本 `ba9e367bee4b` 的 quality_gate 上 BLOCKED，五个此前步骤成功。业务 main 已为 `aba3ac89efedfd93a693fcdd943e802271c44d1f`，工作区干净。原生账本没有自动吸收维护者在业务仓完成的 GitHub 同步、2166 条测试及本地 PUBLISHED 证据；报告因此确实保存了旧失败，不能直接改为 PASS。

机制确有缺口：

1. `workflow_final_delivery.execute` 对 BLOCKED/UNVERIFIED 直接返回，`request` 又拒绝已有请求。原本正确的“不重放未知副作用”被实现成了没有显式恢复出口；即使本地验证已修复，仍无法推进原请求。
2. `next_actions` 只选择 PENDING/READY/RUNNING，报告“下一步”只枚举任务与需求验收，漏掉阻塞最终交付，形成“有交付阻塞却无事可做”。
3. `progress_context` 只投影 TESTER 的旧 issues，遗漏 `REQUIREMENT_COMPLETED e14612` 已登记的四项 `issue_dispositions` 和 20 项 deferred obligations。报告中的 `None` 实际是缺失的 task_id，不是一个未知验收结论。
4. 生产入口实测发现，最终 Git 交付行只有英文状态/错误码，不能通过自身中文七区格式校验；缺少中文业务说明。原生交付回执版本也应与当次仓库版本分开说明。

## 最小修复及不变量

代码提交 `31cd7a566febd28e5411d22335bac7a1625b8383`，运行时 0.17.3。

- 原 `request_final_delivery` 支持 `{request_id, expected_head, reason}` 的可信精确恢复授权。群来源仍验证实际用户原文/身份；本次维护通过已有 direct_user_maintenance 入口引用本会话用户原授权并保存证据哈希，不伪造飞书消息或角色 writer。
- 原 owner 沿 `finalize_delivery(request_id)` 执行。冻结修复后的 main SHA 与已提交脚本，确认需求验收/特性分支未变、main 包含原验收及前次交付、工作区干净、remote 身份未变、GitHub main 同 SHA。只读对账 Git，随后重新执行本地 mock up/test/quality_gate，不重新 merge/push，不导入旧 PASS 冒充本轮结果。
- 每个恢复版本有独立 recovery_id、事件、START、退出回执与证据目录；旧阶段及失败文件不覆盖。未知中断不重放，同 head 不重复创建失败恢复；无副作用预检拒绝可在新版本精确授权后重绑，避免再次形成永久死路。
- BLOCKED/UNVERIFIED 纳入协调者 next_actions、决策材料和指纹、风险与下一步。报告显示正式验收处置和真实阶段未完成义务；交付回执版本与当次仓库 head 分开，补上中文状态。
- 主回归与既有门禁不削减，8KiB 只读预算不放大，完整历史留哈希 artifact。特性分支本地验收快照仍是 e82af002，不重写成维护后的 main；real_nodes/production 仍未授权未完成。

## 回归、审查与上线证据

证据目录 `/home/wang/work/.local/final-delivery-recovery-20261031/`：

- `regression-final.log`：148 passed / 108.41s，真实隔离 Git/bare remote、合成账本与本地脚本替身，覆盖原交付、失败恢复、授权/版本漂移、远端不一致、历史保留、幂等、中断不重放、原生控制、报告及最终验收。
- 最后报告修订后 48 项定向回归通过（35.39s），版本分离用例再验证 1 项通过；相关 Python 编译、`git diff --check` 通过。
- 真实只读入口：恢复前 8080B、登记后 8032B，均小于 8192B；完整七区通过自身中文格式校验。验收处置来自 e14612，不凭描述猜结论；20 项真实阶段义务明确显示。
- 协调者 Skill 校验 `Skill is valid!`。可复用性检查采用更新既有 `references/runtime-helpers.md`，同步用户指南与统一流程 3.36，不另建重复 Skill。
- `activation.json` / `source-publication.json`：Gateway PID 3612975，ACTIVATED，70 个源码哈希一致、319 处 future-call 绑定生效；未重启、不清 writer、不改原业务结果。
- 固定 OCR run `05febce8d9d843959789d6993eae1235`，两批共 10 个代码/测试/配置文件；覆盖和裁决见下方最终记录。第一次准备缺独立维护项目绑定，第二次批次遗漏 plugin.yaml/默认排除 tests，均为零模型预检拒绝；随后在隔离 worktree 用冻结项目规则精确纳入全部范围，无共享 provider 修改、无业务代码修改，没有将未完成覆盖当通过。

## 原生交付恢复

- 恢复登记 e15159，`recovery-b5accfaff4d44b83b346c03e`，绑定 main `aba3ac89efedfd93a693fcdd943e802271c44d1f`。
- 通过已有原生唤醒入口排队；遇原 writer 在场返回 DEFERRED/WRITER_FENCE_PRESENT，保留原 owner，不冒用身份接管。
- 原五个成功步骤与旧 quality_gate 失败事件、日志保留。`original-stage-hashes.json` 保存原文件哈希，最终需逐一比对。
- 本次只管理本机机制、仓库和本地 mock 栈；没有处理机部署、生产数据库/NAS 写入或真实 A100 任务。

## 原生环境污染的追加修复

首次真实恢复：Git对账、本地up和test均SUCCEEDED；quality_gate于12:35:38启动、12:35:39以2退出，15项ModuleNotFoundError发生在pytest收集阶段。原生worker实际继承PYTHONPATH。对同一业务.venv解释器只读复查：继承环境的tests_origin=`/home/wang/.hermes/hermes-agent/tests/__init__.py`，business_fixture_found=false；清理Python覆盖后tests为本地namespace、business_fixture_found=true。这是受控执行器环境污染，不能称业务回归失败或直接接受旧门禁。

补丁 `2b9f3fd666c90e5b4914c35859faa90fbd42cbe9`（0.17.4）清理最终交付与恢复命令继承的PYTHON*/PYTEST_*，再设置既有固定mock环境；新增执行器源码SHA绑定，真实机制修复可显式重验同一业务head，仍拒绝相同head+执行器的无变化重复。执行器在授权后漂移则阻断，recovery_history保留所有旧恢复与失败。未改业务main/断言或放宽门禁。

`environment-regression.log` 34 passed / 31.76s，覆盖污染环境下原交付及恢复、版本身份与旧失败历史，编译/差异检查通过。`environment-activation.json`证明同一Gateway热加载70源码哈希一致，无重启/接管。补丁OCR单独冻结5个实际入选文件，未修改已在途主修复审查快照。

## ACTIVE维护CLI追加修复

内容级入口核验发现，无池的维护CLI走LEGACY投影，将当前ACTIVE角色误列暂停并漏交付；原生群工具本身已走正确账本。补丁`6c7df787ca7932a81810e5916fc0a9170c8bcd81`增加明确`--pool`与原生池环境分流，先核当前run需求根，错配/非ACTIVE/读取中切换run即报错；无池保留旧项目兼容，不能作为ACTIVE事实来源。

`cli-regression-final.log`：56 passed /40.66s，覆盖真实隔离Git/账本、当前交付、业务SQLite字节不变、错项目/非ACTIVE、环境继承及旧模式。编译/diff检查通过。13:01实际由已部署Skill脚本加`--pool`读取同一原生PENDING交付及接续动作。此CLI新进程直接采用源码，不改恢复执行器哈希或原owner；运行中Gateway的70个激活源码不变。用户/流程指南3.37和既有runtime-helpers同步，Skill校验通过。

## 最终核验

2026-10-31 12:52 再核真实报告入口：核心8032B、中文七区格式合格，包含最终交付接续、正式验收处置和20项真实阶段义务，没有`None：`标签；12份原交付阶段文件SHA全部保持不变。12:54从协调者Skill软链接执行无池`runtime_tools.py live-progress`虽退出0，进一步内容核查却发现它读取LEGACY视图、误列暂停且漏交付，不能当作ACTIVE核验通过。已追加显式池/原生池环境分流及项目范围核对，真实结果见最终核验。

2026-10-31 13:11当次只读终态核验：**COMPLETED**。原协调者同一work完成reconcile e15180、local_up e15183、local_test e15186和quality_gate e15189，四步均有真实退出0及SHA核对的回执；恢复对账明确git_write_performed=false，没有重放merge/push。完整质量门禁 **2166 passed、20 skipped、25 warnings /311.96s**，Ruff规则、320文件格式、编译、浏览器交互、三份Compose解析、差异检查均通过。处理机Compose仅本地解析，没有连接或部署处理机。

业务main与GitHub main均为`aba3ac89efedfd93a693fcdd943e802271c44d1f`，工作区干净。12份原阶段文件不变，第一次环境污染失败与第二次成功分别保留；原验收快照e82af002未改。Gateway同PID3612975，70激活源码哈希一致，无重启/接管。当前核心8052B，报告七区及正式处置/真实阶段义务核验通过。20项real_nodes/production义务仍NOT_AUTHORIZED_NOT_COMPLETED。

本轮机制OCR尚在收口，不能把运行中问题总数写成0。后续两个仅PREPARED、零调用的分开材料（python-env/cli-progress）不分别启动，按`31cd7a5..6c7df78`合为7个代码/测试/配置文件的一批完整范围审查（followup-v2）；主修复在途快照不动。最终审查裁决和机制GitHub同步记录待下方补齐。

### 机制GitHub同步

2026-10-31 13:23，既有仓`git@github.com:wangzd9/skill-backup.git`已执行`git push origin HEAD:main`，提交`7009b9cc7db115a7fec4f4a9b29293c836ea8cca`，远端回读同SHA。包含canonical快照734c3a8（代码6c7df78）、core c3901f5累计补丁、六个Skill、指南和本报告；1204项清单按拟提交核SHA，累计补丁隔离还原通过，18个变化Python文件编译及差异/秘密模式检查通过。保护预存未跟踪文件，不导出业务DB、凭据或原始会话。该次同步如实保留OCR进行中，不能把源码备份当审查接受；最终裁决继续补录。

### OCR真实终态与剩余阻塞（13:34）

主run `05febce8d9d843959789d6993eae1235` 已于13:34:55达到外层3900秒硬截止，状态HARD_DEADLINE，service已inactive/dead、MainPID=0。b02的六个文件覆盖完整、可读comments为0，已结合当前源码/跨模块合同、回归和原生报告验证核对；b01的四个文件没有完整manifest/结果，不能把它或整个run判为PASS。整体results为INSUFFICIENT_EVIDENCE，问题总数未知；没有可读critical/high条目可逐项判定，不代表它们不存在。

按固定OCR“失败停链、不原样重跑”条件，**没有启动后续补丁run** `e3c91b81ea3a44b481d8710912a90fcd`（`reviews/followup-v2`，31cd7a5..6c7df78，7个实际代码/测试/配置文件）。它及此前两个被合并的prepare均零模型调用；因此2b9f3fd和6c7df78的独立OCR尚未执行，原因是主审硬截止后停止新准入，不得将prepare写成已审。

剩余工作是恢复独立审查的有效覆盖与裁决，**不是重跑业务交付**。后续先读`ocr-results-final.json`、`review-decisions.json`和固定run状态，定位四文件批次为何在3900秒内未形成完整结果；同代码/参数/截止只补retry_reason不能重跑，不延长预算、不改冻结材料。范围或执行策略需有实质、可验证的处理后再按固定入口审查；不得借新task名或新目录重复调用。

业务终态和上线证据不回退：原生最终交付COMPLETED、e15189完整门禁2166 passed /20 skipped、main/GitHub同aba3ac8、旧失败文件不变。整体机制审查**尚未接受**，TODO保持[~]，明确保留该证据债。原始会话只留本地，不导出到GitHub。
