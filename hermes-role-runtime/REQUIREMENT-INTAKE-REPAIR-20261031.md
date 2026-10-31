# 新需求接纳入口修复（2026-10-31，0.17.5）

结论：存在机制缺陷，同时协调者误判了错误原因。原生身份没有丢失；业务提案也确实尚未完成正式合同登记。这两件事必须分别处理，不能让用户换通道重发来替代合同准备。

## 现场证据

- 原消息 `om_x100b616c4083bca8de3602bb2f7d972` 的原生记录为 `authenticated=true`、`native_primary=true`，且有对应 `TURN_BOUND`。正文 SHA-256 为 `a44acfd3ce3d63317e8b53ac75d97acc9912875019870a3eb4008ec718d454f6`，与会话中原文独立计算结果一致。
- 13:52 的两个原始接纳调用及随后两次重试都到达 `workflow_intake.accept`，却被同一个复合错误码挡住。实际句首“请统一推进……两个缺陷补齐专题”不匹配旧正则；协调者把它解释成缺少身份绑定，没有证据支持。增加 `use_native_requirement=true` 也不能修正措辞识别，且与 `contract_files` 同用本来就不合法。
- `role_requirement_catalog` 还因旧映射 `hermes-model-routing`、`vla-unattended-workflow` 已退出当前索引而失败。ACTIVE 只读目录不应因此整体不可用。
- 两个 prepared run 当前仍为 PAUSED、没有计划。登记事件分别为 e1497/e1501，八份正式合同源码哈希保持不变；任务表仅含历史任务，没有 BR/SR 新任务。原调用只提供候选 proposal，缺少登记所需正式合同，独立只读预检均返回 `REGISTERED_CONTRACT_FILES_REQUIRED`。

## 修复

1. 识别句首“统一／统筹推进”明确开发指令；原生身份、用户原文、否定指令和范围校验保留。身份缺失与指令措辞未匹配使用不同错误码。
2. 接纳工具返回 `native_source_verified` 和明确的未接纳状态，要求按真实错误处理合同或措辞，不再凭复合错误要求用户换通道。群入口说明与 LLM 回合说明保持一致。
3. 同一原生消息按 requirement ID 隔离幂等身份，允许分别接纳两个包；同包改参数仍冲突。旧版已经提交的接纳回执保持原命令身份，重试不另建 run。
4. ACTIVE 只读目录显示旧映射诊断，只列当前索引条目并关联原生账本；不改变旧的写入／选择校验。
5. 热加载覆盖新增函数、入口回调和异步函数；不替换业务 owner、锁或已有账本结果。

## 验证与范围

108 项相关隔离回归通过（28.858 秒），含真实临时 Git/SQLite、双包同消息接纳、旧回执重试、合同准备门、来源绑定、只读目录、原生 schema、本地授权及运行中激活。Python AST 与 `git diff --check` 通过。最初两个测试启动方式有环境导入问题，已换用 Hermes 现有解释器及明确模块路径完成验证，未修改运行中环境依赖。

复现命令：

```sh
PYTHONPATH=/home/wang/work/hermes-role-runtime:/home/wang/work/hermes-role-runtime/tests:/home/wang/.hermes/hermes-agent \
  /home/wang/.hermes/hermes-agent/venv/bin/python -m unittest -q \
  tests.test_local_development_automation test_workflow_registration \
  test_workflow_autonomy.AutonomyTests test_manual_gateway test_requirement_runtime \
  test_mechanism_repair.ActivationRepairTests test_native_command_schema
```

现场只读诊断、测试日志及后续激活回执保存在 `/home/wang/work/.local/requirement-intake-repair-20261031/`，不备份原始群会话、业务数据库或凭据。此次没有运行模型/OCR、发送群消息、接纳业务提案、发布业务计划或启动开发。

两专题后续必须按原授权审阅并补齐正式合同、提供方任务和登记冻结，再复用原 run 接纳／规划；不能把入口修复或候选提案当作已开工证据。本次维护没有代替这些业务准备，也没有让未准备合同越过接纳门。

已通过原生控制接口热加载到原 Gateway PID `3612975`，前后进程启动身份相同，70 份激活源码哈希全部匹配；两个 prepared run 的整行记录前后一致。代码修复提交 `cbcab96`。本地备份来源及文件哈希以 `skill-backup/MECHANISM-SNAPSHOT.json` 为准；本轮未执行远端推送。
