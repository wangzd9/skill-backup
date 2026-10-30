# 五分钟独立文档检查与同角色待处理队列（2026-10-13）

每个角色按绑定拥有自己的canonical sessions文档、派单、ACK、回执、实质事件和相关决定。`role_observer.py`以SHA-256比较相关文档内容及控制/进程身份；不用mtime代替内容变化，不要求换成MD5。协调者观察全局交付、审查结果和各角色事件；执行者/测试者只观察自己及与当前任务相关的材料。完整代码和证据按需读取，摘要不是裁决依据。

## 检查独立于模型工作

`role-document-observer.timer`每5分钟运行一次脚本-only `scan --registry`，即使某角色的原生模型job持续开发，仍检查各角色文档、更新ticks/快照并持久保存待处理通知。部署模板位于assets/role-document-observer.service与.timer，当前本机注册表为`/home/wang/.hermes/role-projects.json`。这条路径不调用模型、不投递消息、不派任务、不触发审查；原生角色poll仍为每5分钟错峰，负责模型执行和原有投递。

有新派单、实质内容/控制/进程身份变化，或上轮输入未确认时，先持久入队，角色空闲才接续；无变化不因RUNNING标签、20分钟或30分钟到了而再次唤醒模型。首次接入/损坏材料明确待核查；异常不能当成空闲或通过。开发/测试模型已运行时继续完成当前派单，在安全检查点检查本角色新通知；不是每5分钟重启开发。需要分回合继续未完成工作时先落实质检查点和下一动作，不能静默结束后指望无变化定时唤醒。现有测试/OCR/强审查及范围门禁保持。

## 排队而不是同角色并行

未消费通知保存于`runtime/observer/<ROLE>.json`的`pending`：首次/最近检查时间、最新fingerprint及累计changed_sources。这是可合并的文档变化通知，不是第二份任务状态或逐次commit开发队列；同一路径多次改动合并，消费时重读最新有效派单，过时/撤销单不重做。

角色原生job在途时，Hermes现有claim/in-flight机制拒绝第二个同job；人工群会话保留并默认只读，含“继续”“帮我修”也不写请求/inbox/ACK、不领取或触发后台业务。只有当前用户明确全部权限才允许本回合尝试，在原授权内取得同一角色租约后复核任务及已有证据；拿不到不执行、不代写队列，下一普通回合恢复只读。专用可信需求选择是独立控制排队例外，不取业务租约、不派单/ACK/开发。cron按已接受需求的持久scope及原门禁接续，不要求逐轮群全权，不继承群只读限制，不能借新群消息抢owner。独立scan只积压通知，不确认、不删租约、不重启或kill。已有角色工作结束后，后续poll消费pending；事件提前run仍走同一门禁。正常无在途阻塞时，发现变化按5分钟兜底（再加原生调度/模型队列延迟），不能承诺长任务在途时5分钟完成接单。

## 消费之后才确认

被唤醒业务模型先读当次`runtime_tools.py context-input --project ROOT --role ROLE`本角色最新checkpoint、未完成索引与pending，按需重读相关权威材料；完整pending和watch在observer文件内，12个定位条目不是完整覆盖限制。安全检查点、当前派单交付/阻塞记录后检查是否新增待处理输入。未读新输入不得清空队列。

处理本轮输入之后、final之前执行：

```text
python3 /home/wang/.codex/skills/complex-task-coordinator/scripts/role_observer.py ack --project ROOT --role A --fingerprint 本轮poll输出值
```

确认只消费当前观察通知，不更新TODO、不批准、不替代业务ACK。ACK时还会重新采集实际输入；即使独立scan尚未扫到新内容，旧ack也拒绝并保留pending；失败/取消未确认继续排队，不用自动ack最新指纹抹掉未读输入。角色可以在安全点读取并处理新输入后确认对应观察；未能处理则留给下一回合。observer.lock是短锁，不持开发锁、不删除稳定inode。

## 汇报与部署

报告沿用[状态短报与模型时段总结](reporting-policy.md)：协调者白天20分钟仅脚本报告A/B/C/测试者是否可领任务；08:00与个人11/15/18由脚本采集、短只读模型整理各自时段及本地/云端用量。总结身份与业务owner/租约/pending隔离，不能抢占或误计业务忙碌。21–08只落盘，工作不暂停。脚本刷新runtime/activity观察，时长是状态估算；等待、阻塞、未知不自动计作历史真实空闲，明确释放槽可显示可接新单。报告不重跑测试、不读全部历史。模型与脚本从同一权威投影取得本角色报告契约，完整投影仅按需取证，字段保留来源与真实时间；冲突写明不同来源各自结论，刷新时间不当业务进展。

独立timer与原生poll都使用同一短锁/持久游标，检查重复不会重复启动模型或消费事件。保留poll job ID、5分钟错峰、origin/deliver、在途任务与门禁；迁移只更新必要prompt，不重启gateway或正在开发的worker。旧轮自然结束后，新轮加载更新规则。固定池完成需求后保存收口证据并保留当前选择待命，五角色固定job不停止，无新业务输入不唤业务模型；非池历史专项按原协议停止重复job。scanner只读当前控制，非ACTIVE不开展业务。池选择/解析见[固定角色池](role-pool.md)，pool-poll按当前generation冻结实际根；旧在途owner及独立操作未安全收口前不切换。

验收覆盖：无变化零模型、新文档入队、同角色忙碌不再启动、反复检查去重、旧ack不能吞新输入、重启保留pending、暂停不开展业务、真实timer在模型长回合期间仍执行。文档/脚本校验、自然运行检查和真实业务验收分别记录；不能用配置已部署宣称功能已完成。

## OCR 终态接续（2026-10-15）

协调者额外观察 `inbox/control/runtime/ocr/*.json` 注册运行，以及 `reviews` 内带受支持 `coordinator.ocr-run/v1` spec 的历史运行。只将冻结身份、生命周期、终态/失败纳入语义指纹；`updated_at`、session mtime、正常批次推进与日志追加不触发重复模型回合。运行状态保存在同一快照的 `ocr_runs`，真实 `state.json` 路径进入原pending，不另建任务队列。

若原状态仍RUNNING而绑定PID/start已退出或专属cgroup不匹配，快照投影为INTERRUPTED；身份无法核实为UNKNOWN。保留原JSON，不kill、不重启、不推断通过。完成、失败、身份不匹配、状态缺失或读取损坏必须可见；只观察受项目路径约束的材料，不跟随越界索引。已有owner忙时保留pending，后续空闲poll接续；旧ACK不能吞掉新终态。首次部署会合并历史运行通知，先核是否已有对应快照的裁决，不重复审查或返修，不能提前批量ACK。

`AWAITING_DECISION`不是通过。协调者用 `ocr_runtime.py results --run RUN_DIR` 读取批次结果及原始证据，材料不足保持门禁。夜间同样处理，只抑制主动群通知。脚本发现到原生可唤醒通常0—5分钟，实际开始仍受角色占用、cron错峰与模型排队影响。

## 新增观察源：群请求与工程操作

协调者快照包含coordinator_requests；请求PENDING持续接续，不能被observer ack提前吞掉，处理证据与queue-ack见[runtime helpers](runtime-helpers.md)。各角色观察自身工程operation结果及PID/starttime/boot身份，协调者观察全角色；终态/异常变化才接续，PID消失标UNKNOWN而非成功。报告只读其短摘要，不替代原始证据。已停用cron保持停用，不用trigger_job自动启用。


## 观察与业务输入分离（scripted-role-v2）

正常等待只写runtime/observations，脚本持续保留观测时间，不能再追加长session/事件制造自身回声。显式旧观察排除必须经现有租约注册确切完整行字节范围与SHA，内容改变重新唤醒；不得整份忽略session或按自由文字nochange过滤真实问题。未知格式保留核对。canonical/inbox镜像按角色、event_id与完整业务载荷去重，冲突仍唤醒并在event_conflicts保留来源。

处理后先重新poll核对新增通知，真正处理完再ACK对应指纹；不自动ACK最新输入。固定task采样由scan每项目执行一次，当前结果供各角色共享事实，状态变化notice才参与指纹。模型继续独立解释异常、进行开发/验证及审核。

## 强审终态、自己的ACK回声与继续（v3）

strong_review.py短提交独立systemd service，协调者观察其原账本call_id/冻结身份、unit/PID/start/cgroup及终态；只在生命周期、结果、失败或未知变化时接入原pending，日志增长不唤醒。RESERVED/SUBMITTED不是模型已开始，SUCCEEDED不是PASS，UNKNOWN/INTERRUPTED保留门禁；不另开审查、不自动retry/recover。

同一pending指纹在owner仍忙时合并积压，不每轮重发模型；精确排除本角色已消费ACK产生的回声，未读新输入不被顺带确认。自然安全检查点有未完成工作时持久continuation，后续业务回合对账后接续并确认对应ID；不能静默结束后期待不变文档自动唤醒。归档/checkpoint索引维护不作为新业务事件，细节见[上下文与归档](context-and-archives.md)。
