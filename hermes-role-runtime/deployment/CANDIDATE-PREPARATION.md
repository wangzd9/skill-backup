# 隔离候选与授权切换

候选源码可以在独立 Git worktree 中修订、回归和打包。生产 Skill 链接、
gateway、timer、pool 和业务库均保持原样。规划者手动；规划者和审查者继续
是按需 Skill，不进入五角色、cron、绑定、租约或业务账本。

## 候选构建

在待发布 checkout 内执行下列命令，`--output` 必须是尚不存在的私有目录：

```sh
/home/wang/.hermes/hermes-agent/venv/bin/python -B scripts/candidate_profile.py \
  --output /absolute/new/candidate-profile
```

该目录只包含 Skill 文档/模板、指向待测 checkout 的兼容链接、受管 wrapper
副本，以及指向既有 Hermes core 的依赖链接。不复制生产配置、凭据、jobs、
租约、outbox 或业务库；不安装 systemd unit，不修改用户 HOME。

完成候选回归后，以显式 profile 生成源码包：

```sh
/home/wang/.hermes/hermes-agent/venv/bin/python -B scripts/build_release.py \
  --profile-home /absolute/new/candidate-profile \
  --validation /absolute/source-validation.json \
  --output /absolute/new/candidate.tar.gz
```

验证 JSON 必须有当前 `code_version(profile_home=...)` 的 `version_digest`、
同一 `profile_home` 和 `passed=true`。打包继续拒绝未追踪/脏源码和错误入口。
Git bundle 另行保留候选提交和测试源码。

候选指纹包含绝对来源路径与 profile 的配置语义，因此不会等于安装后的指纹。
包内 `deployment_acceptance=false`；验证默认生产门禁时仍使用安装布局，不能
用候选 profile 回执替代安装后的 P0/T1/T2/M/R。

## 窗口稳定判据

授权后先关闭新增业务准入，保留在途结果的合法收口路径，并核清 owner、
完整进程子树和独立服务。没有原生行、未知进程或未知副作用都不能按已退出处理。

稳定不等于“所有文件和 PENDING 数量完全不变”：

- 两次库存比较通过既有 `_sources_unchanged()`；业务事实、身份/终态、job
  策略、原生执行和强审查高水位的变化必须重新库存并对账。
- 派生观察数据可继续变化；service 只豁免已有白名单的采样字段。身份、
  runner、状态/终态和未知字段仍受比较约束，不新增宽泛豁免。
- outbox 可新增，待发摘要和普通事件必须在同一事务快照里分类并记采样时间。
  保留成员索引、源 hash、SENT/UNKNOWN 与持久配额；不恢复旧 outbox 覆盖新回执。
- 责任映射与 R 覆盖最终库存。代码、策略或事实增量变化则对应门重新验收，
  不重封历史 manifest 冒充无变化。

## 授权后的执行顺序

1. 复核候选包、Git bundle、目标基线和源码工作区；归档当前配置/服务/最终
   业务库存及高水位。候选包只提供源码，配置语义只能核验，不能覆盖凭据。
2. 安全暂停观察 timer 和新准入，完成在途责任交接及完整子树核退。存在未知
   writer 时保持关闭，不强杀、不因租约到期接管。
3. 将目标运行时快进到候选提交，核 Skill 链接和受管 wrapper；按目标布局
   重算完整指纹，冻结实际路由/投递策略。实际安装语义与候选范围分别记账。
4. 刷新最终 `inspect`、proposal 和责任映射，执行既有 `prepare`，保持 STAGED。
   在明确隔离验收环境和当前安装版本上完成 P0/T1/T2/M/R；M 的真实受信入站
   必须沿原生入口验证，不能用合成消息或旧版本回执替代。
5. 既有 `validate` 全门通过后才调用 `activate`；拒绝则停止。gateway 在旧
   writer 安全退出后采用代码；核实际 hook、采集、派发、消费及消息回执。
   `scan --registry` 继续采集，主 sender 仍为 scheduler hook，避免第二出口。
6. 核首轮与跨 tick 配额、UNKNOWN 不重投、历史摘要不逐条回灌和同根身份。
   R1 保持既有关闭策略，恢复轮询保留，不把准备完成声称为成本优化已兑现。

回退先关闭新准入并收口副作用，使用既有 export/rollback-preview 保留最新
业务责任；源码回退与业务回退分别验收。不得用旧数据库、租约或通知回执覆盖
切换后增量。

## 在途责任接纳

`prepare` 保留所有原始导入，不把暂存当作已接纳。`reconcile --manifest ...
--review ... --import-id ... --run ...` 仅在关闭准入的 STAGED 库、原 writer
已退出且最终库存未变时接纳人工核实的任务状态。须先发布完整计划，再逐条
提供原 task/attempt、整数映射、精确来源身份、固定回执 hash 和协调者待办。
REVIEW 保留原候选；DEVELOPING 创建无执行权的待续接 work。该入口不创建
PASS、完成状态、服务调用、旧输入 ACK 或通知回执。激活门仍独立验证。
