# 五分钟独立文档检查与同角色待处理队列（2026-10-13）

每个角色按绑定拥有自己的canonical sessions文档、派单、ACK、回执、实质事件和相关决定。`role_observer.py`以SHA-256比较相关文档内容及控制/进程身份；不用mtime代替内容变化，不要求换成MD5。协调者观察全局交付、审查结果和各角色事件；执行者/测试者只观察自己及与当前任务相关的材料。完整代码和证据按需读取，摘要不是裁决依据。

## 检查独立于模型工作

`role-document-observer.timer`每5分钟运行一次脚本-only `scan --registry`，即使某角色的原生模型job持续开发，仍检查各角色文档、更新ticks/快照并持久保存待处理通知。部署模板位于assets/role-document-observer.service与.timer，当前本机注册表为`/home/wang/.hermes/role-projects.json`。这条路径不调用模型、不投递消息、不派任务、不触发审查；原生角色poll仍为每5分钟错峰，负责模型执行和原有投递。

有新派单、实质内容/控制/进程身份变化，或上轮输入未确认时，先持久入队，角色空闲才接续；无变化不因RUNNING标签、20分钟或30分钟到了而再次唤醒模型。首次接入/损坏材料明确待核查；异常不能当成空闲或通过。开发/测试模型已运行时继续完成当前派单，在安全检查点检查本角色新通知；不是每5分钟重启开发。需要分回合继续未完成工作时先落实质检查点和下一动作，不能静默结束后指望无变化定时唤醒。现有测试/OCR/强审查及范围门禁保持。

## 排队而不是同角色并行

未消费通知保存于`runtime/observer/<ROLE>.json`的`pending`：首次/最近检查时间、最新fingerprint及累计changed_sources。这是可合并的文档变化通知，不是第二份任务状态或逐次commit开发队列；同一路径多次改动合并，消费时重读最新有效派单，过时/撤销单不重做。

角色原生job在途时，Hermes现有claim/in-flight机制拒绝第二个同job；人工群会话保留；明确业务动作才保存原请求，群与poll争取同一角色租约，取得后复核当前任务及已有处理证据，避免重复执行。问答不自动入队，也不触发后台业务。其他角色群与cron仍使用角色owner/租约互斥，不能借新群消息抢占已有任务。独立scan只积压通知，不确认、不删租约、不重启或kill。已有角色工作结束后，后续poll消费pending；事件提前run仍走同一门禁。正常无在途阻塞时，发现变化按5分钟兜底（再加原生调度/模型队列延迟），不能承诺长任务在途时5分钟完成接单。

## 消费之后才确认

被唤醒模型先读当次`runtime_tools.py progress-facts --project ROOT`共同事实包与pending，按需重读相关权威材料；完整pending和watch在observer文件内，12个定位条目不是完整覆盖限制。安全检查点、当前派单交付/阻塞记录后检查是否新增待处理输入。未读新输入不得清空队列。

处理本轮输入之后、final之前执行：

```text
python3 /home/wang/.codex/skills/complex-task-coordinator/scripts/role_observer.py ack --project ROOT --role A --fingerprint 本轮poll输出值
```

确认只消费当前观察通知，不更新TODO、不批准、不替代业务ACK。ACK时还会重新采集实际输入；即使独立scan尚未扫到新内容，旧ack也拒绝并保留pending；失败/取消未确认继续排队，不用自动ack最新指纹抹掉未读输入。角色可以在安全点读取并处理新输入后确认对应观察；未能处理则留给下一回合。observer.lock是短锁，不持开发锁、不删除稳定inode。

## 汇报与部署

报告沿用[夜间静默与时段总结](reporting-policy.md)：协调者08:00夜间汇总/白天20分钟；个人11/15/18，全部no_agent。21–08只落盘，工作不暂停。脚本刷新runtime/activity观察，时长是状态估算；等待、阻塞、未知不算空闲。报告不重跑测试、不读全部历史。模型与脚本共用`progress_projection.py build_projection(project, at=None)`生成的当前事实，字段保留来源与真实时间；冲突写明不同来源各自结论，刷新时间不当业务进展。

独立timer与原生poll都使用同一短锁/持久游标，检查重复不会重复启动模型或消费事件。保留poll job ID、5分钟错峰、origin/deliver、在途任务与门禁；迁移只更新必要prompt，不重启gateway或正在开发的worker。旧轮自然结束后，新轮加载更新规则。结束专项后由协调者停止专项job；scanner只读注册项目控制，不启动非ACTIVE业务。

验收覆盖：无变化零模型、新文档入队、同角色忙碌不再启动、反复检查去重、旧ack不能吞新输入、重启保留pending、暂停不开展业务、真实timer在模型长回合期间仍执行。文档/脚本校验、自然运行检查和真实业务验收分别记录；不能用配置已部署宣称功能已完成。

## 新增观察源：群请求与工程操作

协调者快照包含coordinator_requests；请求PENDING持续接续，不能被observer ack提前吞掉，处理证据与queue-ack见[runtime helpers](runtime-helpers.md)。各角色观察自身工程operation结果及PID/starttime/boot身份，协调者观察全角色；终态/异常变化才接续，PID消失标UNKNOWN而非成功。报告只读其短摘要，不替代原始证据。已停用cron保持停用，不用trigger_job自动启用。


## 观察与业务输入分离（scripted-role-v2）

正常等待只写runtime/observations，脚本持续保留观测时间，不能再追加长session/事件制造自身回声。显式旧观察排除必须经现有租约注册确切完整行字节范围与SHA，内容改变重新唤醒；不得整份忽略session或按自由文字nochange过滤真实问题。未知格式保留核对。canonical/inbox镜像按角色、event_id与完整业务载荷去重，冲突仍唤醒并在event_conflicts保留来源。

处理后先重新poll核对新增通知，真正处理完再ACK对应指纹；不自动ACK最新输入。固定task采样由scan每项目执行一次，当前结果供各角色共享事实，状态变化notice才参与指纹。模型继续独立解释异常、进行开发/验证及审核。
