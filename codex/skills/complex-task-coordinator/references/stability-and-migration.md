# 运行稳定性与改绑（2026-10-19）

发生授权拒绝、角色改绑、接续冲突或命令反复失败时按需读取。本页不扩大业务、生产或审查授权。

## 群消息与租约

- 当前可信用户主消息的“全部权限：动作”“全部权限，动作”“授权你所有权限完成这件事”等明确授权均可识别，不要求固定冒号格式。引用、附件、条件句、否定和提问不授予权限；裸“全部权限”仅给本回合资格，具体动作仍须从当前明确任务确定。
- 群回合忙时，新消息进入原生FIFO，在新回合重新核验原始用户身份与消息回执；不能把新授权注入旧回合。排队不等于业务已执行，不重复索要用户已经明确给出的授权。队列满明确报未接收，不改为steer。
- 普通全权业务动作仍须本角色共享租约；唯一排队例外为当前可信全权协调者消息的专用需求选择控制请求，只存意图、不执行业务，详见[固定角色池](role-pool.md)。已接受需求的后台scope持久生效，后续cron无需逐轮群全权。普通业务工具调用只续期当前精确session/turn/PID/start的owner，不因时间长抢占其他owner。人工孤儿仅在同进程、旧原生turn明确ended且心跳陈旧时由claim恢复；active/unknown、热加载前旧turn、外部进程都不凭超时接管。
- 绑定异常只隔离受损身份，健康项目继续；无法核实身份保持只读。`roles`只能放COORDINATOR/A/B/C/TESTER，改绑审计放顶层`rebind_audit`，不混入角色表。

## 跨项目协调

执行者迁到另一项目、原协调者仍负责其工作时，在**原协调者**绑定中显式声明只读订阅：

```json
"coordinated_projects": [{
  "project_root": "/absolute/canonical/project-root",
  "roles": ["A", "B", "C", "TESTER"]
}]
```

仅订阅当前注册表里的精确角色；不递归、不创建新协调者cron，也不由订阅授予目标项目写权。`context-input`/observer提供带项目身份的回执、事件、当前门禁和定位；必须实读本次候选相关原件，不能只取checkpoint的status和时间。目标暂停或证据采集失败继续展示原因，不把缺失当通过。改绑前只读校验候选：

```sh
python3 /home/wang/.codex/skills/complex-task-coordinator/scripts/binding_contract.py --project ROOT --input candidate-bindings.json --registry /home/wang/.hermes/role-projects.json
```

## 独立测试与合入前复核

测试者实际完成验证后，用`role-record`的可选`verification`冻结完整head、PASS/FAIL、项目内回执和明确关闭的旧TESTER事件ID；脚本冻结当前回执集合及hash。只能由持租约的TESTER发布business记录，不能由协调者补造PASS或把观察记录当测试。例：

```json
"verification": {
  "head": "完整40位commit",
  "result": "PASS",
  "receipt": "test-receipts/TASK-xx/attempt-2.md",
  "supersedes": ["TESTER:previous-failure-id"]
}
```

协调者合入前针对**目标需求包**运行：

```sh
python3 /home/wang/.codex/skills/complex-task-coordinator/scripts/runtime_tools.py acceptance-check --project TARGET_ROOT --input acceptance-spec.json
```

spec仅含`task/repo/head`（repo为干净的候选worktree，head为完整冻结commit）。`BLOCKED`时不能合入：需当前head独立PASS，旧失败必须明确关闭，新增/修改回执、未解除门禁和快照变化都保留阻塞。新attempt、无关文件修复、OCR零finding不能解除既有测试失败。旧非结构化回执保留为证据，不自动转换成PASS；由测试者基于实际证据补充核验记录。

`TEST_GATE_CLEAR`仅代表该独立测试门禁可核对，不替代原AC覆盖、OCR、强审、授权或合入后验证。工具只读，不能拦截任意shell的Git操作；协调者必须沿原合入流程执行并在写入前复核新输入。

## 接续与错误收敛

新continuation冻结不可变输入，活动session追加不应改变原输入。旧`INPUT_RECOVERY_REQUIRED`用`continuation-recover-input`、精确旧字节和原hash修复定位；不是完成ACK，不重跑已交付工作。不再反复ACK同一冲突，也不手改hash或删pending。

安全扫描或CLI/schema首次失败后，先读原错误、该子命令`--help`及对应JSON字段，再用合法的固定入口纠正一次；同一原因仍失败就保存阻塞、证据与具体解除条件，停止等价命令重试。不要反复使用`cat | python3`、解释器嵌套或内联subprocess；普通收集用直接CLI/只读工具和JSON文件。安全拒绝不能移进脚本、spec或service绕过。模型/网络失败与业务失败分开记录，不据此重做已完成动作或自动重复收费。

运行时源码只维护`/home/wang/work/hermes-role-runtime`一份，Skill脚本通过软链接引用；静态备份物化链接内容。热加载只影响未来调用，旧栈帧、未知旧turn和未完成业务不能凭一条ACTIVATED记录认定已恢复。实际证据与边界见[排查报告](/home/wang/work/doc/analysis/role-workflow-stability-20261019/REPORT.md)。
