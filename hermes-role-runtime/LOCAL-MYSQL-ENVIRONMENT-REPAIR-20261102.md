# 本地 MySQL 测试环境修复（2026-11-02）

## 根因与处理

宿主机 Docker 服务及 mysql:8.4 镜像本就可用，wang 及业务进程已具备 Docker 组权限。开发者 bwrap 沙箱隔离网络并用临时 /run 隐藏 Docker socket，这属于既有边界；旧检查点误将其表述为整机没有 Docker。受管 MySQL 会话原本只接入 MN-01 迁移补验，普通候选验证没有数据库 profile，最终集成也只注入旧变量，导致现有环境无法被统一发现与使用。

0.17.18 复用现有 MySQL 会话与测试执行器，打通普通候选、最终集成、旧补验三条入口。普通 `verify` 和 `verify_integration` 可选 `profile="mysql"`；普通候选仍发布 `independent_test`，旧补验仍发布 `supplemental_mysql`，profile 留在真实证据中。宿主为每次验证准备独占 MySQL 8.4、固定镜像 ID、无网络/无开放端口、独立临时数据；专用 Unix socket 可在原隔离沙箱内连接，不开放 Docker 接口。普通/集成测试三个固定变量指向同一专用库，旧补验保留单变量合同，在途旧集成变量兼容。

三条通道共用数据库生命周期锁及既有命令冻结规则；同命令并发或重放不会重新建库覆盖已冻结的 ready 证据。已准备但结果不完整时保留原状态先对账，不重建数据库。结束仅清理自己创建的容器，未知副作用保留待核。数据库默认 +08:00；socket 路径过长在创建容器前给出明确错误。

角色工作包、测试工具参数、每回合入口提示和三角色 Skill 均公开本地环境用法。已接受的本地验证直接使用，不再要求用户每次选择容器/处理机；开发者交付原 scope 中的必要用例，由原生 TESTER 固定候选实跑。未解除沙箱、不改变原 AC、不伪造角色身份或修改活跃需求验收状态。仅维护运行时/技能及业务环境文档，未重启本地业务栈、未更改业务数据库或执行生产部署。

## 验证证据

- `evidence/local-mysql-20261102/regression.log`：94 项相关回归通过；随后统一三通道生命周期，`shared-lifecycle-regression.log` 48 项针对性回归通过；最终集成重放与长路径另外 2 项通过，见 `integration-profile-regression.log`。
- `migration-smoke.json`：业务主仓 c3987b150f04130a69eeccb5f376b2c7fe8c6e4b，在相同 bwrap 沙箱执行既有 MN-01 MySQL upgrade/downgrade 往返，1 passed / 0 skipped；MySQL 8.4.11，容器清理状态 CLOSED。原始 JUnit、测试日志及环境清单保留在 migration-artifacts。
- `managed-route-smoke.json` / `managed-route-artifacts`：隔离合成候选通过新增普通验证入口连接真实 MySQL，验证事务回滚、两个独立连接并发唯一键，2 passed / 0 skipped；结果发布为合成账本的 independent_test，candidate_mysql 证据保留，容器结束 CLOSED。没有向真实需求发布这些环境测试结果。
- `nas-preflight.json`：现有 `/mnt/nas_backup/vla_dataset_builder_test` CIFS 测试根只读预检通过；不代表 NAS 业务身份复核、摘要全量读取开销或性能验收完成。
- `discovery.json`：工作包和原生验证参数均公开受管入口；三角色 Skill 结构校验通过，统一指南 3.44 及流程图同步。

环境可用与业务验收分开。Foundation 的 receipt 迁移/回滚、Backend 并发抢占及 NAS AC 仍须对应固定候选的专用测试；旧迁移测试不能替代新验收。原生 TESTER 自然接续时使用新入口；不能因维护者做了环境冒烟就把旧 NOT_RUN 改成 PASS。完整演练中曾发现取证脚本临时目录过长导致 socket 无法连接，改用短路径后实际通过；运行时增加前置诊断，保留该问题说明。

## 部署

源码验证完成后使用原生插件热加载，只替换已列明的未来调用入口，保留在途角色、工作单、任务、审查和单写锁。实际加载证据见同目录 activation.json；未取得该回执前不宣称部署完成。

2026-11-02 11:49 +08:00 热加载已核实：`ACTIVATED / future_calls_only`，75 个源码哈希全部匹配，激活 helper 哈希匹配；本次回执 PID 1203596 / 118 个已加载调用绑定。未加载模块在下次正常导入时使用新源码，在途栈帧不替换。三份实测会话均 CLOSED，逐一按本次容器名核实已删除，详见 `cleanup.json`。没有实测证明当前 Foundation 的专用 MySQL AC 已完成，也没有向真实需求发布环境冒烟 PASS。
