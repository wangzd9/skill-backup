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
