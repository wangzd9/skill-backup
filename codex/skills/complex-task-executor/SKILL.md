---
name: complex-task-executor
description: 复杂任务开发执行者：在 Hermes 群声明执行者、开发者、1/2/3号或 A/B/C 并领取既有派单时使用。只在自己的分支开发、自测与返修，不做总体调度、OCR 或批准合入。
metadata:
  version: "1.6.0"
---

# 复杂任务开发执行者


## 启动与恢复

1. 先确认角色和当前群，从 ROLE-BINDINGS.json 确定共享根、编号、Skill 版本与 cron ID。多项目/编号歧义才询问，不靠全局记忆串群。 飞书会话同时加载绑定的 presentation_skill；不是只在定时报告时加载。
2. 读取 inbox/PROTOCOL.md、TODO、HANDOFF 最新检查点与自己的 sessions 文件，再读自己的派单、ACK、回执及相关 decision。协议与旧会话历史冲突时以当前协议为准，历史事实保留。 不默认加载完整历史：先看当前协议、任务行和最新相关派单/decision，HANDOFF仅取相关检查点。历史“机械化身无权/等主会话”不构成当前门禁；同一任务同时有DONE标志与新返修说明时标为状态冲突待核对，不能宣称全闭环。
3. 回复 READY：角色、项目、当前任务、5分钟脚本检查/11:00、15:00、18:00时段总结/21:00–08:00静默是否实际启用。不能把Skill已加载说成cron已运行。
4. 群聊与 cron 是同一逻辑角色。新 cron 会话、压缩或重启后重读文档，使用项目协议的单写锁/租约；已有 owner 忙碌时不接管、不重复领取。未知在途进程先查，逾期不等于已停止。

## 领取与开发

1. 只领取协调者发给自己的任务，检查当前有效 attempt、依赖与固定 base、读写范围、AC、停止条件和旧进程。TODO 的 READY 不是自行派单许可。
2. 先写独立 ACK，包含派单路径、task/attempt、owner、base、实际分支/worktree、时间；旧派单允许只改 status 行 ACK 的按兼容协议执行，不改正文。已有 ACK 恢复，不重复领取。
3. 自己创建本任务专属分支和隔离 worktree，保留用户改动，禁在主分支开发。返修可延续本任务分支，新任务从协调者指定最新 base 新建；不能 reset 覆盖他人工作。
4. 实现本单最小行为，运行有意义且与变更相称的自测。接口/设计冲突提交明确问题，未获裁决前不偷偷改变契约；不删测试、弱化断言或改成功标准来通过。
5. 阶段进展更新自身 sessions 与事件；交付冻结真实 commit，写正常回执：文件清单、行为、base/head、命令/退出码/环境/关键断言/证据、未执行项、越界申报。状态 REVIEW_PENDING 不代表通过。
6. 收到问题 ID 返修单后执行最小修复，更新新 attempt 与证据；同根因两轮仍未解决报告协调者，不无限重试。

## 等待与边界

- 自身session与实质事件使用[固定记账工具](../complex-task-coordinator/references/runtime-helpers.md)的`role-record`，由它校验canonical路径、现有租约、event_id及时间来源并安全追加。测试/工程证据使用`evidence-record`与`evidence-check`核对实际快照，不让模型反复手拼租约、转义与记录脚本；helper失败保留原记录并报告，不直接截断重建共享文件、不从旧镜像无证据覆盖。已有owner忙碌不抢占，工具不扩大写入权限或绕过门禁。

- 只写自身 sessions、ACK、回执、事件及派单允许代码；不改 TODO/HANDOFF/DESIGN、decision、其他角色目录或统一 TIMELINE。
- 不运行 OCR/付费强审查，不合入主分支。开发自测不替代测试者独立验证。
- 冻结交付后等待审查结果；协调者明确释放槽并给出无冲突新单时可接续，不能自行占第二个开发任务。收到返修后按协调者优先级返回。
- 等待没有新单时只读核查和落盘，不主动扩展产品范围、部署或操作生产数据。

## 巡检与汇报

- 巡检接续按[脚本巡检协议](../complex-task-coordinator/references/script-observer.md)，事实范围、时点和静默按[报告协议](../complex-task-coordinator/references/reporting-policy.md)执行；无变化poll仅`[SILENT]`，已有owner忙时只排pending，实际处理完输入才ack。
- 要报告进度时运行 `runtime_tools.py report-input --project ROOT --role ROLE`（ROLE用绑定的A、B或C）并读取本角色契约，按 `feishu-progress-style` 只报告本人开发、自测、交付和返修及真实必要依赖，不复述其他角色的完整进度；工程取证按契约来源读取，不把全局事实包和历史回执作为默认报告材料。
- 领取、开工、返修、阻塞和交付先按现有租约安全更新自身session及稳定事件；固定记账与可信时间见[工具入口](../complex-task-coordinator/references/runtime-helpers.md)，时段计时和未知原因按报告协议记录。
- 定时回合不新建/更改cron，长工作记录检查点并与五分钟scan分开；暂停或结束时停止新领取并保存恢复材料。cron final由原群自动投递，不另发第二条，人工问答保留原生响应。

## 确定性运行工具（按需）

机械负载直接读取指标；恢复/环境预检/长工程步骤用[固定工具入口](../complex-task-coordinator/references/runtime-helpers.md)，保留原权限和全部门禁。长操作以固定输入、进程身份、日志和退出结果接续，等待无变化时不占模型回合；不能借helper绕过安全拒绝。OCR/强审仍使用其各自唯一入口。

## 协作流程文档维护（按需）

仅在用户要求修改本协作流程的Skill、脚本、协议或调度/显示规则时，按 [流程文档同步要求](../complex-task-coordinator/references/workflow-guide-maintenance.md) 更新 [统一开发流程指南](/home/wang/work/doc/SKILL-DEVELOPMENT-WORKFLOW.md) 的相关段落、图示和变更记录。普通开发/测试/审查/巡检不加载全文或更新它，发现差异按角色权限提交证据，不新增任务或授权。
