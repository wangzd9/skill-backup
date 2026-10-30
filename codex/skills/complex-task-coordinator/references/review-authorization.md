# 自定义自动审查授权与调用限制

只有用户明确授权后，协调者才能通过Codex调用 `complex-task-reviewer`。没有有效授权时保留人工触发流程。本规则不取消“已完成功能、具体风险、冻结AC、最小纠错”的审查边界；授权和空余额都不是触发审查的理由。

群聊回合另受默认只读限制：既有强审授权不授予本回合动作权限。群内登记授权、起跑/重试、撤销等写操作须当前用户明确全部权限，并取得业务角色租约；取不到不执行、不代写队列。cron按有效审查授权与原门禁继续，不继承群只读限制。

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
- 在途审查全局串行。同project/review_id仅在完整冻结输入哈希一致时复用在途调用或有效成功结果，不再次收费；task、repo、base/target、审查包、实际模型、timeout、risk/question/AC/gate任一变化明确冲突，使用新review_id。旧账本缺完整身份哈希时明确无法核验，只读查看历史，不猜复用或自动补跑。失败不会自动重试，核对原因后显式引用原call_id才可重试，并重新占一次额度。
- 触发前要有REVIEW_NOW的具体风险、问题、原AC与门禁，固定base/target和完整审查包。脚本检查授权、任务与仓库范围、项目运行状态、幂等与额度，短提交独立systemd用户service，worker启动只读Codex前再次在全局账本事务内复核授权与项目状态。
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

指定截止时间时用 `--until 2026-10-13T18:00:00+08:00` 替换duration；整个任务用 `--until-project-complete`。未来起点用 `--starts-at`；限制任务用 `--task-ids TASK-01 TASK-02`。用户指定模型时通过grant的`--model`冻结；未指定则提交前只读解析本机Codex当前有效model/profile，冻结实际模型到spec及身份哈希，worker始终显式`--model`，执行者不得临时升级或换模型。解析失败、缺模型或项目配置优先级无法确定时明确失败，不留到服务启动后猜默认；配置解析只返回model/profile白名单，不输出凭据。

```text
python3 /home/wang/.codex/skills/complex-task-coordinator/scripts/strong_review.py run --project ROOT --repo REPO --review-id R-ID --task-id TASK-ID --base 实际base --target 实际target --package 项目内审查包绝对路径 --decision REVIEW_NOW --risk 具体风险 --question 待回答问题 --ac 既定AC --gate 受影响门禁
python3 /home/wang/.codex/skills/complex-task-coordinator/scripts/strong_review.py status --project ROOT
python3 /home/wang/.codex/skills/complex-task-coordinator/scripts/strong_review.py revoke --project ROOT --reason 用户撤销或授权任务完成
```

`run`参数保持兼容，现在短提交`complex-task-strong-review-<call_id>.service`后立即返回id/call_id、unit、spec_path、账本status及service_state。审查包、input.md、spec.json、worker.py冻结并绑定SHA，worker在专属service cgroup执行；Restart=no、KillMode=control-group，原timeout保留。不得用terminal后台、nohup/setsid或将群会话视为必然持久来起跑长强审；用户systemd不可用保持阻塞，不降级临时runner。

同身份在途返回原call而不再次提交，成功校验result hash后复用；失败必须显式retry-of，失败/超时/不明占位不退额度。status联合service ActiveState/MainPID/ControlGroup、PID/start和真实cgroup；账本仍RESERVED/RUNNING但service已退且结果未收口时按中断/未知核查，不能默认通过。recover除原证据还核unit已inactive/failed、cgroup无存活成员、worker/child确已停止；service未知或存活拒绝恢复。用户撤销/到期在实际Codex启动前复核，已起跑调用按原预算完成。

绑定和提交前以只读`systemctl --user show --property=Version`检查用户manager；命令不可执行、超时或返回非零时明确`USER_SYSTEMD_UNAVAILABLE_BEFORE_SUBMISSION`并记FAILED，不绑定unit、不启动审查，仍计原额度。systemd-run不存在或无法exec同属确定未提交：未被worker采用的占位原子记FAILED。预检通过后仍可能失联；实际提交超时、调用方中断、服务状态不可读等不确定情形保留RESERVED/待核实，不据此启动第二项；确定失败处理也不能覆盖已采用或运行中的worker。

释放短时协调写租约，五分钟observer将终态接回原pending，不占住二十分钟只读状态报告，不新建cron。机器重启或服务停止后不自动续审。只读status在没有账本时也不创建文件。

## 报告呈现与完整账本

协调者人工/事件/08:00整体进度保留`**强审查情况**`分区；20分钟例外只报角色可接状态。该分区简短说明当前是否需要、在途/最近结果、后续裁决或等待条件。无有效授权且当前确需调用时，明确“尚未授权，待用户触发或授权”；当前不需要审查时如实说明，不新增授权请求。授权期限或额度确实阻塞接续时给绝对期限/下一可用时间及受影响范围。

完整授权起止/完成条件、任务范围、近60分钟次数/5、本授权累计、call_id、实际token及费用仍从status与原账本核对，保留到报告详情或按需给链接；正文默认不展示grant/token字典和全部调用字段。费用不可取得写未知，并说明现有CLI证据未提供费用字段。计数不能猜，未知不可当0；呈现精简不改变任何调用前授权、限额与结果门禁。

授权建立/撤销/到期、起跑/结果/限流时更新对应回执、事件及开发日志；不把授权账本复制成第二份业务任务状态。
