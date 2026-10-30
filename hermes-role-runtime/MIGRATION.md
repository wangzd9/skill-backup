# 五角色运行机制迁移

生产切换只允许同一份完整源码/配置语义版本、旧业务高水位和 P0/T1/T2/M/R 门禁全部通过后执行。规划者和审查者仍为手动、按需加载的 Skill；下面没有为其创建角色、工作、cron 或执行绑定的命令。

已完成的当前状态备份位于 `/home/wang/work/.local/backups/role-workflow-redesign-20261019-162507`，恢复入口为其中的 `RESTORE.md`。一旦新机制接收业务增量，应先导出并对账，再采用已验证的旧版适配器；不能直接覆盖旧数据库或恢复旧 token。

## 检查与暂存

2026-10-20 缺陷修复前的补充备份、恢复演练及交付边界见 [FIXES-20261020.md](FIXES-20261020.md)。该备份与上面的初始设计备份分别保留，均不得覆盖修复后的业务增量。

所有命令在 `/home/wang/work/hermes-role-runtime` 内运行。以下 `HOME_DIR`、`POOL` 和 `OUT` 应指向本轮明确选定的 home、pool 与材料目录；不得把隔离演练的回执称为生产验收。

```sh
python3 workflow_migration.py inspect --home HOME_DIR --pool POOL --acceptance-environments OUT/acceptance-environments.json --output OUT/inventory.json
python3 workflow_migration.py prepare --manifest OUT/inventory.json --proposal OUT/proposal.json --output OUT/staged.json
```

`inspect` 只读原始来源和真实进程、scope、service；保留 missing/null/空字符串/整数等原始身份区别。历史会话只哈希，不将历史正文装入当前工作上下文。需求根目录的非业务导航 Markdown 与 `REFERENCES.json` 导航链接单独保留别名、真实目标和字节哈希，仅作非权威来源；业务文件链接或越界目标仍报错。它不会创建业务数据库、修改旧任务或停止进程。

`proposal.json` 必须显式提供 `run_id`、`requirement_id`、`requirement_root`、`repo` 和准确的 `task_ids`。`prepare` 只创建 STAGED 新账本和原始导入提案，并关闭新账本 admission。它不会把 DONE、字符串 attempt、旧 checkpoint 自动转换为任务、PASS、ACK 或完成状态。接受的新计划仍须通过正常的 `publish_plan` 校验，R 逐条对账。STAGED 期间旧机制继续拥有权威。

`acceptance-environments.json` 须在 inspect 前明确允许的隔离验收环境，示意如下（路径、run 和服务身份均需换成本轮实际值）：

```json
{
  "P0": {"purpose":"isolated_acceptance","home":"/absolute/p0-home","pool_path":"/absolute/p0-home/role-pool.json","database":"/absolute/p0-home/role-pool.runtime/business.sqlite3","run_id":"p0-run"},
  "T1": {"purpose":"isolated_acceptance","database":"/absolute/t1/business.sqlite3","run_id":"t1-run"},
  "T2": {"purpose":"isolated_acceptance","spec_path":"/absolute/t2/spec.json","unit":"acceptance-t2.service","service_id":"t2-service"},
  "M": {"purpose":"isolated_acceptance","home":"/absolute/m-home","controlled_project":"/absolute/m-project","run_id":"m-run"}
}
```

inspect 自动补充各门的源码版本、入口合同和相关配置 hash（P0 pool、T2 spec、M config.yaml）；缺失配置也明确记录。proof 必须匹配其声明环境，不能临时换另一个池或数据库；P0 数据库还须能从 pool 推导，P0/M 的 run 由实际权威核验。生产 `target_database` 单独密封，不允许 prepare 或 activate 替换。目标保持 STAGED；P0 在预先声明的独立 ACTIVE 池取真实 schema-2 writer proof，避免要求同池同时处于 STAGED 与 ACTIVE。隔离环境不能冒充生产安装或真实 M。

来源稳定性按语义核验：仅白名单观察缓存可变化或增删；jobs 只忽略有限运行采样字段，暂停、启用、模型、来源及未知字段仍保护；独立服务仅忽略有限采样字段，身份、状态、终态和 spec 仍保护。业务事件、原生执行和强审高水位变化继续要求新库存。validate 重采实际服务并保留观测记录。这不免除最终 writer 收口、增量保存和对账。

默认版本清单覆盖插件、相关核心 scheduler/gateway/main+aux usage hooks、全部相关 Skill、scanner，以及 routing 配置的白名单语义哈希。路由配置不输出凭据，不读取 `.env` 或解析环境变量取值。`--version-source` 只用于检查演练；缺少完整默认来源的清单无法通过生产切换门禁。

## 门禁材料

每门回执均须为 JSON，包含 `schema_version: 1`、`gate`、`result: "PASS"`、完全匹配 inventory 的 `version_digest`、非空 `artifacts: [{"path": "绝对路径", "sha256": "文件字节SHA256"}]` 及 `proof`。布尔字段只是声明，实际放行还会回读下列源。

| 门 | 声明 | proof 与实际复核 |
| --- | --- | --- |
| P0 | `observation_schema: 2`、`real_execution_observed: true`、`observed_at` 距验证不超过180秒 | `home`、`pool_path`、`generation`、`execution_id`；重新只读采样 schema2 账本及真实 PID/start、专用 scope/进程组，必须仍可观测且无 observer fault；冻结 execution scope 的 `runtime_version_digest` 必须匹配本轮版本。schema1 不能代替。 |
| T1 | `real_business_lifecycle: true` | `database`、`run_id`、`candidate_id`；回读已完成候选、同 attempt 的必要验证、APPROVE/COMPLETE，重验独立测试 runner/log/JUnit/checkout，并核 Git 对象；runner 前后版本必须等于本轮版本且执行期间未变。完整业务生命周期不代替等待静默、服务返回等异常路径的独立验收。 |
| T2 | `real_systemd: true` | `spec_path`、`spec_sha256`、`unit`、`service_id`；冻结 spec 的 run_id/call_id/unit 及 `runtime_version_digest` 必须一致，实际独立 systemd service 已完成，InvocationID、ExecMainPID、退出结果可核。保留 RemainAfterExit 的 `active/exited` 也可核。离线模型不能证明 M。 |
| M | `real_model: true`、`real_channel: true`、`controlled_scope: true` | `home`、`session_id`、`physical_attempt_id`、`logical_request_id`、`execution_id`、`controlled_project`、`business_event_id` 及 `channel`；分别回读真实业务模型与同一需求、角色、pool、版本的真实原生周期报告/送达，二者执行身份必须明确关联，详见下文。没有真实模型/渠道回执不能放行。 |
| R | `imports_reconciled: true`、`inventory_manifest_sha256` | `reviewed_by`、`resolutions`；每个原始 record 均需唯一 `record_sha256=sha([source,source_sha256,identity])`，明确映射 `run_id/task_id/整数attempt`，或 `excluded:true` 加非空 `reason`。不能仅凭 stage_import 成功放行。 |

此处 SHA 对 JSON 使用 UTF-8、键排序、无多余空白，与 `workflow_migration.sha` 完全相同。本地门禁验证材料与权威源的完整性；有权改写验证器及全部权威库的操作员不属于该完整性边界。

M 的业务 execution 须有真实已分发且完成的 `business_cron` 物理请求、schema2 冻结身份、实际已结束账本 execution/work，以及该 work 在实际执行区间内、模型请求结束后写入的工作结果事件。原业务 cron final 按设计为 suppressed，不得伪造 delivered。`channel` 包含 `session_id`、`physical_attempt_id`、`logical_request_id`、`execution_id`、`notification_id`、`input_artifact`、`input_sha256`，指向另一份同需求和角色的真实 `summary` 物理请求及原生 report scope。冻结报告事实必须完整包含前述业务事件，模型生成状态须为 GENERATED，账本同一 `PERIODIC_SUMMARY` 必须 SENT 且有同 native home 的 `native_cron` 确认回执，回执内实际清理后发送正文的 `content_sha256` 必须等于冻结 renderer/outbox 的 `rendered_sha256`。原队列终态会按隐私策略清空正文及 job_json，因此只回读其同 execution 的 delivered/finished_at；不依赖已清空的正文。原生执行的 delivery_outcome 必须 delivered；若其保留 queued，另须原队列实际终态发送回调 `native_queue_terminal`，排队或 UNKNOWN 自身均不算送达。独立 `hermes send` 不写 cron delivery 表，不能借用或编造这条关联。

```sh
python3 workflow_migration.py validate --manifest OUT/inventory.json --evidence OUT/P0.json --evidence OUT/T1.json --evidence OUT/T2.json --evidence OUT/M.json --evidence OUT/R.json --output OUT/readiness.json
python3 workflow_migration.py activate --manifest OUT/inventory.json --evidence OUT/P0.json --evidence OUT/T1.json --evidence OUT/T2.json --evidence OUT/M.json --evidence OUT/R.json --output OUT/activation.json
```

`validate` 只读；未满足条件返回退出码2。旧 owner 存活或不明、scope 子写者未退出、原始高水位变化、未知独立服务、缺失计划、未对账任务，均阻止切换。`activate` 在同一 pool admission 锁内重做全部验证，先持久化激活日志，关闭 admission，单调递增 generation 后切 ACTIVE，最后开 admission。中途崩溃保持 closed；重跑仍须重新通过全部门禁。不会 resume 旧 cron，也不会修改原来的 pause 配置。

M 还要核验业务与报告物理请求的实际 usage：缺 output_tokens 或实际输出超过 1024 tokens 均拒绝，不能只凭请求填写的上限放行。原授权最多 4 次请求、每次完整请求不超过 131072 字节、最多 1 条群总结；历史已用 2 次请求和唯一群总结，真实 M 仍 UNVERIFIED。本轮离线测试不消耗或扩展授权额度。

## 观察与导出回滚

原生 `on_scheduler_tick` 在 tick 锁内、ESTOP/dispatch暂停检查前触发。插件按60秒合并采样，独立保存监测 cache/outbox，并复用原角色群的 Hermes 发送器投递机械异常、升级和恢复通知；直接调用观察库而未传 sender 时只保留待发送记录。只有确认送达及原生 message_id 才记 SENT，发送结果不明保持 UNKNOWN，不盲目重发；夜间静默仍生效。观察器不调用模型、续租、回收 owner、确认业务结果或新建业务工作。schema2 观察当前及遗留 execution 的实际写者；独立 service 状态只读记录。monitor故障不得被解释为无任务或 owner 已退出。

```sh
python3 workflow_migration.py export --pool POOL --freeze --reason '本轮恢复前冻结新入口' --output OUT/final-export.json
python3 workflow_migration.py export --pool POOL --after EVENT_HIGH_WATER --output OUT/increment.json
python3 workflow_migration.py rollback-preview --export OUT/final-export.json --target-contract OUT/verified-old-adapter.json --output OUT/rollback-preview.json
```

`export --freeze` 关闭新入口，保持现有 owner 的安全收口；仍有 writer 时导出标为 provisional。最终导出包含完整业务事实、整数 attempt、candidate、失败闭环、控制、事件高水位、通知回执和独立服务身份；增量范围为 `(after, through]`。晚到事件/通知回执需要另做增量和最终导出。

`rollback-preview` 完全只读，要求最终导出及经测试、具备完整身份侧车/整数attempt/candidate/失败记录/控制/pause/通知/service/generation隔离能力的旧 adapter 契约和真实哈希材料。冲突、未知外部副作用、晚到事件、变化的回执和不明确身份会阻止预览通过。它不会执行回滚、覆盖库、恢复旧 token 或重发已送达消息；回滚应在已核验适配器下使用下一代 generation 执行。

当前离线回归只证明迁移算法和隔离资源边界。未取得真实 M 和与最终源码版本一致的全部门禁前，生产保持原权威。
