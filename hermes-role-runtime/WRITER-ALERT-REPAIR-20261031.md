# 执行收尾告警循环修复（2026-10-31，0.17.14）

`observer_business_writer_exited` 检查的是业务账本仍为 ACTIVE/DRAINING、原进程与子写入进程已退出。Gateway 的 scheduler tick 先运行 `role_observer.observation_tick`，随后运行 `workflow.collect_event_tick`，后者才用已验证的进程组退出事实登记 EXECUTION_ENDED。原实现对这一正常收尾间隙立即生成首次告警，下一次观察又将告警消失称为 recovered。

用户提供的两条通知均属这个间隙。协调者执行 `2ca6566b2e1142559b04c42581a8d5c6` 原生 completed、error为空，20:51:53.356650 被观察为退出，20:51:54.012683 已登记 EXECUTION_ENDED；C执行 `bf8e3b0b9abf410399f68e7139505f85` 原生 completed、error为空，20:15:53.356506 被观察为退出，20:15:56.888278 已登记结束。完成的是原生回合，不等于业务任务通过。

只读核对当日 outbox 中229条同类 first，全部随后完成执行收尾；收尾延迟中位数0.675秒、最大57.381秒。128条原生当前表记录为completed；其余101条已不在当前表，不以缺行推断成功。详细统计和样例证据保存在仓库外 `/home/wang/work/.local/writer-alert-repair-20261031/`。

修复在共用 `workflow_observation.step` 中实施，原生 hook 与 systemd observer 采用相同规则：

- 退出事实仍立即记录到 faults/anomalies，连续观察120秒后才生成一次 first；采样缺失/不明会中断未通知事件的连续窗口。
- 同一执行已列入核验后的 closed_execution_ids 时，未通知事件静默关闭；已通知事件生成 closed_with_execution，避免把正常结束描述为运行恢复。
- 可读账本、工作结束或替换执行都不足以证明旧writer恢复。恢复须有同一执行的写入进程存在、scope和写入范围已验证的证据。
- 旧cursor按已生成首次通知接续，防止升级后补发first。写权、回收事务、owner和业务门禁不变。

隔离回归覆盖正常退出间隙、持续退出、只通知一次、盲采样、精确执行关闭、旧cursor与热激活保留原owner。完整 `test_lease_monitor` 另有两项既有失败：静默时段投递夹具、compaction活动覆盖夹具；用HEAD原始 `workflow_observation.py` 在独立解释器复现同样失败，不记为本修复通过。最终测试和现场激活结果见外部证据记录。
