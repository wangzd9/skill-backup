# 最新上下文、稳定归档与安全接续（role-workflow-v3）

## 默认只读当前材料

业务回合先运行`runtime_tools.py context-input --project ROOT --role ROLE`，读取本角色最新checkpoint、全部未完成任务的定位索引、相关pending与有效派单/ACK/decision。报告另走`report-input`或`summary-input`，不靠长会话全文恢复。首次接入、冲突或证据不足时精确展开相关原文与代码；摘要/字节预算不能减少必要审查覆盖。

同一技术会话对PROTOCOL、ROLE-BINDINGS、runtime-helpers与已加载Skill按canonical路径+SHA复用；hash未变且正文仍在当前上下文时不重复全文读。新会话、压缩丢失正文、配置/规则hash变化后按需重读。绑定是动态事实，不能把它永久塞进系统提示或以prompt cache为由屏蔽新授权/暂停。工具返回短定位和变化字段，不默认回显几十KB不变参考。

## 追加式日志轮转

sessions、TIMELINE等追加文档到**64KiB或400行**触发归档，按完整记录保留最近最多30条、活跃正文不超过48KiB，历史完整保存。单条过大保留完整正文为证据，当前文件写短记录与定位，不能在UTF-8、Markdown或事务记录中间硬切。checkpoint和未完成索引不受“只留30条”影响，不遗漏旧但仍活跃的任务。

`document-archive`与所有append/原子替换共用canonical稳定文档锁，锁inode不删除。归档清单、文件hash、活跃文件切换和索引通过可恢复journal更新；未完成`role-record`先补齐，不把半条事件搬走。持久event索引跨归档保留，重放旧event不得因为当前session的marker已移走而回填。归档/checkpoint刷新本身不制造业务唤醒。

首次迁移保留旧→新定位、原始记录与哈希；活跃旧writer尚未遵循新锁时延后相关文件至安全检查点，不能与它并发读改写、停止进程或重建租约。执行者/测试者只归档自己有权写的材料；TODO/HANDOFF/TIMELINE仍由协调者维护。

## TODO/HANDOFF按语义整理

TODO保留全部未完成、阻塞、返修、依赖、门禁、当前授权与验收状态；完成任务的详细过程可归档，当前索引保留ID、最终状态、接受/merge/证据定位。HANDOFF保留最新恢复点、未完成任务、在途PID/unit身份、授权期限、风险、下一动作和全部未解除条件，旧检查点完整归档。不能对它们做简单tail、按行数删除前缀或只留最近几项。

TODO的`document-archive`语义替换输入可带`obligations`：按未完成task ID列已核实的当前动作、未解除门禁、恢复/证据定位，每任务最多100条非空字符串、单条≤4096字节；每条都必须留在替换后的该任务行或章节。没有该清单时完整保留未完成任务的原叙述；有清单只允许缩短已关闭过程，仍保留task/state/title/owner、deps、REQ/AC及状态variants等非叙述字段。协调者先语义核查，再以`expected_sha256`防并发覆盖；原文完整归档，清单随manifest保存。该参数仅用于TODO，不把HANDOFF当任务表截断。

原TODO仍是唯一业务状态源，checkpoint/归档索引只是可重建定位材料。历史证据路径继续可追溯；归档不是删除、不是接受，也不替代测试/OCR/强审。正常模型只读当前材料；确有未解决问题指向历史时允许精确回查，不自动读全部archives。

## 安全检查点继续

`role-record`同步最新checkpoint。输入可用`pending_actions`、`open_gates`、`references`维护未完成动作、未解除门禁和证据定位；各字段最多100个非空短字符串，同task/attempt缺省继承、显式`[]`清空，新身份不继承旧项。checkpoint从有效租约自动记录不含token的`process_identity`，不手工伪造。正在开发/验证的模型不按请求数、token或分钟硬截断；在自然安全检查点保存当前task/attempt、固定快照/分支、完成证据、未完成项、下一具体动作与外部操作幂等身份。

需要结束本回合但业务未完成时用`continuation`持久请求下一业务回合；同角色已有owner在途时只排队，不重复启动。下一回合先核实际进程、有效任务和已完成动作，再接续，处理后才`continuation-ack`。等待独立服务结果则观察真实身份/终态，不用无限模型poll或睡眠保持会话。

`continuation`发布时由脚本把输入checkpoint原文保存为按SHA寻址的不可变`checkpoint_snapshot`，并保留原活动文档路径、task/attempt和输入指纹。恢复优先读取该快照；正常的`role-record`追加、checkpoint刷新和归档不改变本次已冻结输入，也不破坏ACK。相同continuation身份只恢复同一动作，想改变动作须另建明确身份，不能覆盖旧输入。完成证据须是独立回执并包含精确task/attempt；不能把原输入或更新后的同一活动文档当完成证明。

旧记录没有输入快照且原活动文档已变化时，`context-input`保留原`PENDING`与输入指纹，同时显示`runnable_now=false / resume_state=INPUT_RECOVERY_REQUIRED`。这表示恢复门禁，不表示工作完成或作废：不重跑已交付动作、不手改哈希、不反复claim和发重复事件。报告这一已知门禁后可精确ACK本次observer观察，新派单与真实结果仍照常入队。原文能从归档或完整前缀精确找回时，在本角色有效租约内使用`runtime_tools.py continuation-recover-input --project ROOT --role ROLE --id ID --evidence 原文文件 --token-file TOKEN --fingerprint 原input_sha256`；脚本仅接受SHA与原`checkpoint_sha256`完全相等的字节，补快照后重新唤醒，不替代`continuation-ack`或裁决。原文不可恢复则保留门禁交协调者处理，不自动降级、取消或确认完成。

短命cron worker正常退出后，只有检查点与实际稳定业务交付/终态事件完整匹配（或已核验释放槽），并且没有可执行continuation或在途操作，观察器才不将退出再次当业务变化；实际进程身份仍在状态中展示。仅有“待审/已完成”标签、事件缺失或冲突、工作中进程死亡仍必须触发核查。

observer只合并相同pending身份/输入；ACK仅确认实际处理的指纹。精确识别自己的ACK/记录回声，不能连带忽略同批次新派单、不同attempt、测试/OCR/强审终态或授权变化。失败、身份UNKNOWN和新输入仍保留；不得自动ACK最新指纹消队列。


固定池业务回合先解析并冻结当前需求ID、generation、role及实际project_root，再从该根取context-input；总索引不成为业务根。旧continuation、ACK和回执留原需求包，切换不批量ACK、不把旧输入写进新包；generation冲突停止新动作，先核安全点和池事务。详见[固定角色池](role-pool.md)。

显式共用存量runtime_root的兼容需求，`context-input`只读返回所选包TODO“本包任务编号定位”精确列出的任务、派单与continuation，跨包旧checkpoint显示暂存定位与排除数量；有校验通过的本包停放快照时读其完整门禁和恢复动作。合法业务替换活动checkpoint前按SHA完整停放，切回与恢复既有ACK时核精确task/attempt，不用原领取事件覆盖更新后的交付和门禁。不要据共享根的完整旧索引恢复其他包任务；新增任务先登记本包精确ID，缺定位先准备材料。固定写入入口仍复核范围，任意terminal不是隔离沙箱。
