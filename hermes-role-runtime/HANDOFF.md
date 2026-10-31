# 机制维护交接（2026-10-31）

本文件记录本轮维护流水，不替代原生只读账本。

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
