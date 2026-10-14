# 自定义自动审查授权与调用限制

只有用户明确授权后，协调者才能通过Codex调用 `complex-task-reviewer`。没有有效授权时保留人工触发流程。本规则不取消“已完成功能、具体风险、冻结AC、最小纠错”的审查边界；授权和空余额都不是触发审查的理由。

## 接收用户授权

- 支持任意明确时长（如90分钟、3小时30分钟、2天）、开始/截止时间或“整个当前任务”。一天/半天只是示例，不是选项限制。默认从收到授权起算；指定未来区间时未到开始时间不能起跑。
- 时间统一记录Asia/Shanghai的绝对时间；“今天18点前”按当日解释。范围或时间无法唯一确定时只澄清缺失项，不替用户设一个期限。
- 绑定当前项目、代码根及允许的任务范围，记录原话、消息/会话来源ID和落盘证据。不能把“增加授权能力”或文档示例当成一次实际授权，不能从旧聊天自动续期。
- “整个任务”只覆盖当次明确的任务/专项，完成后撤销，不继承到下一项目/阶段。项目COMPLETE时脚本拒绝新调用；更细的任务范围完成后协调者立即revoke。
- 用户可随时撤销或重新指定范围/时间；新消息替换本项目旧授权，但不清零调用计数。同一消息重复读取是幂等操作，不重新延长期限。
- 授权只允许审查调用，不扩大开发、数据删除、推送、部署、正式日志批准或发布权限。

## 唯一调用入口与限额

使用 [strong_review.py](../scripts/strong_review.py)。生产入口没有修改状态目录或提高限额的参数，所有项目共用本账号的调用账本：`~/.local/state/complex-task-reviews/ledger.sqlite3`。

- **任意连续60分钟最多5次自动审查起跑**，不是自然整点重置；跨项目、群、cron和重启共用计数。失败、超时、复审及已占位后起跑失败也计入，替换授权/换review_id不重置额度。
- 在途审查全局串行。相同review_id/target/审查包已成功返回则复用有效结果，不再次收费；失败不会自动重试，核对原因后显式引用原call_id才可重试，并重新占一次额度。
- 触发前要有REVIEW_NOW的具体风险、问题、原AC与门禁，固定base/target和完整审查包。脚本检查授权、任务与仓库范围、项目运行状态、幂等与额度，再启动只读Codex。
- 不得用裸 `codex exec`、其他API、换账本目录或把Codex审查改称“第二意见”绕过计数。项目已有 `codex-independent-review` 也遵守该入口。常规弱模型OCR保持原独立流程。
- 额度已满时记录下一可用时间，五分钟巡检到期后再评估必要性，不循环重试。授权过期/撤销则停止新调用；已经起跑的审查可在原超时预算内完成，仍按实际证据裁决。
- 脚本以固定detached worktree、`codex exec --sandbox read-only`与审查者Skill起跑，完整结果由脚本落盘；CLI退出0只表示结果收到，不表示PASS或批准合入。
- 授权和占位持久化；占位后崩溃保持未决，不直接补跑。`recover`需要原进程确已停止的证据，拒绝存活/无法核验的进程，也不返还小时额度。不要杀其他角色或既有业务进程。

调用次数按审查会话起跑计数，单个会话内部API请求不另算；token记录实际CLI证据，费用不可获取则写未知，不估算成已知费用。

## 命令入口

先把用户真实消息保存到证据文件；以下ROOT/REPO/消息ID/原话都是占位说明，不能照抄成实际授权。Hermes定时回合使用脚本命令，不写内联可执行heredoc。

```text
python3 /home/wang/.codex/skills/complex-task-coordinator/scripts/strong_review.py grant --project ROOT --repo REPO --source-id 用户消息ID --source-file 用户消息证据绝对路径 --duration-minutes 210
```

指定截止时间时用 `--until 2026-10-13T18:00:00+08:00` 替换duration；整个任务用 `--until-project-complete`。未来起点用 `--starts-at`；限制任务用 `--task-ids TASK-01 TASK-02`。模型仅在用户指定时通过grant的 `--model` 冻结，未指定沿用本机Codex配置，执行者不得临时升级或换模型。

```text
python3 /home/wang/.codex/skills/complex-task-coordinator/scripts/strong_review.py run --project ROOT --repo REPO --review-id R-ID --task-id TASK-ID --base 实际base --target 实际target --package 项目内审查包绝对路径 --decision REVIEW_NOW --risk 具体风险 --question 待回答问题 --ac 既定AC --gate 受影响门禁
python3 /home/wang/.codex/skills/complex-task-coordinator/scripts/strong_review.py status --project ROOT
python3 /home/wang/.codex/skills/complex-task-coordinator/scripts/strong_review.py revoke --project ROOT --reason 用户撤销或授权任务完成
```

长调用作为可恢复后台进程，记录call_id、实际PID和日志；释放短时协调写租约，不占住二十分钟只读汇报。重启后先status检查在途，再恢复，不自动开启第二轮。只读status在没有账本时也不创建文件。

## 报告呈现与完整账本

协调者整体进度保留`**强审查情况**`分区，简短说明当前是否需要、在途/最近结果、后续裁决或等待条件。无有效授权且当前确需调用时，明确“尚未授权，待用户触发或授权”；当前不需要审查时如实说明，不新增授权请求。授权期限或额度确实阻塞接续时给绝对期限/下一可用时间及受影响范围。

完整授权起止/完成条件、任务范围、近60分钟次数/5、本授权累计、call_id、实际token及费用仍从status与原账本核对，保留到报告详情或按需给链接；正文默认不展示grant/token字典和全部调用字段。费用不可取得写未知，并说明现有CLI证据未提供费用字段。计数不能猜，未知不可当0；呈现精简不改变任何调用前授权、限额与结果门禁。

授权建立/撤销/到期、起跑/结果/限流时更新对应回执、事件及开发日志；不把授权账本复制成第二份业务任务状态。
