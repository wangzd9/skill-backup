# Hermes 角色运行优化

独立插件及确定性工具，维护源在本仓库，安装软链接为 `~/.hermes/plugins/role-workflow-runtime`。不修改 Hermes 核心，不改变模型、生成参数和测试/OCR/强审/合入门禁。

- 人工自然语言保留原生模型入口，通过原生auto_skill加载协调者与显示Skill。问进展/讨论不自动入队；明确业务动作才用request保存原生身份，群或cron共用角色租约，取得后复读任务与已有证据。原生slash控制/审批保留，不自动恢复暂停。
- 仅实际绑定角色群的`/llm-load`和精确机械查询直接GET LAN指标；解释/分析仍交模型。缺指标标未知，只有有效双样本才算差分速率。
- 系统/可取得的工具前缀只做指纹审计；不改提示、不新增tool或prompt section。工具hook经过限长，未知不冒充完整schema证据。
- doctor检查实际磁盘和依赖；长工程操作使用冻结JSON、独立systemd、逐步退出码、日志和进程身份，五分钟observer接续，UNKNOWN不重跑。

角色使用方法与边界见 [runtime helpers](/home/wang/.codex/skills/complex-task-coordinator/references/runtime-helpers.md)。核心命令：

```text
python3 runtime_tools.py load --json
python3 runtime_tools.py progress-facts --project ROOT
python3 runtime_tools.py queue --project ROOT
python3 runtime_tools.py doctor --cwd WORKDIR --storage-path DATA --docker
python3 runtime_tools.py start --project ROOT --role A --spec SPEC.json --token-file LEASE.json
python3 runtime_tools.py status --state RESULT.json
python3 runtime_tools.py role-record --project ROOT --role A --input RECORD.json --token-file LEASE.json
python3 runtime_tools.py evidence-record --project ROOT --role TESTER --spec SPEC.json --token-file LEASE.json
python3 runtime_tools.py evidence-check --record RESULT.evidence.json
```

工程命令成功不代表业务PASS；生产恢复仍需原授权，不能通过spec/systemd绕过安全拒绝。OCR与强审分别继续使用原专用入口。凭据不写进spec或日志。

`role-record`复用现有租约与绑定，业务事件和session采用可恢复journal；正常观察独立保存，不制造业务唤醒。`evidence-record`复用原operation runner，保存真实代码、声明输入/配置/环境与日志的身份；机械匹配不替代独立验收或审核。

`task_observation.py`由既有五分钟observer按显式项目spec执行固定只读采样；无spec不访问网络。正常心跳/CPU/mtime不改变业务通知，状态变化、异常、未知及恢复保留通知。每轮两次有硬超时的GET及一次只读SSH，预算最多45秒；不调用推理或改生产。群报显示完整日期、分钟和时区，后台时间保留原精度，发生/观测/补录分开。实施及回退见[2026-10-14记录](/home/wang/work/doc/analysis/quality-first-script-optimization-20261014/IMPLEMENTATION.md)。

部署脚本、隔离systemd验证、实际热加载结果与回退说明在 [实施记录](/home/wang/work/doc/analysis/hermes-runtime-optimization-20261013/REPORT.md)。单测：

```text
/home/wang/.hermes/hermes-agent/venv/bin/python -m unittest discover -s tests -v
```

单测使用临时项目和fake投递adapter；真实plugin discovery使用隔离HERMES_HOME。不会调用模型或向真实群发消息。部署不启动已暂停job、不取消已有回合；旧在途协调者需自然结束后再评估单入口效果。

范围纠正：普通私聊、未绑定群和其他项目会话保持原生模型处理；旧 `load_query_chat_ids` 不再读取，不能据历史诊断用途扩展拦截。参见 [2026-10-13范围修复](/home/wang/work/doc/analysis/hermes-runtime-optimization-20261013/SCOPE-FIX.md)。

可选`/progress`当次读当前文档生成简报；自然语言“当前状态”“汇报进展”等仍由模型答复。两者通过`progress_projection.py`使用同源事实，按整体目标、执行者、测试者、OCR、强审、风险、下一步七区显示；新派单不能用旧IDLE解释，交付、验证、审核和合入分开。事实保留来源及业务时间，不输出原始大段日志或HTML折叠区。当前事实是可重建报告材料，不替代TODO任务状态。夜间人工对话正常回应。

原生pre/post/error API hook仅为已绑定角色记录请求身份与起止元数据，索引为`~/.hermes/runtime/role-time.db`；不存prompt、响应正文或凭据，不改变请求。cron会话按job绑定，群会话按原生chat_id及父session恢复。重试保持一个逻辑请求，计时故障不会阻断模型。客户端响应耗时含网络、重试、服务端排队和推理，不冒充纯排队；业务阶段占用与API耗时是重叠维度，不相加。历史日志仅能近似恢复已完成的成功请求。

五分钟文档检查在同角色owner存活时仍积压完整pending，不启动第二个执行模型；owner正常释放后再接续。stdout仅给定位预览，完整来源保留observer文件；必须实际处理后才ACK，不吞未读输入。

原消息明确需要执行时：

```text
python3 runtime_tools.py request --project ROOT --input 原生指令.json
```

request只持久保存，字段与引用/附件来源见runtime helpers。业务执行须同一角色租约、任务/attempt和操作幂等身份；旧PENDING保留处理。共享写入口的互斥不等于对任意terminal的系统级沙箱。

[当前用户说明](/home/wang/work/doc/SKILL-DEVELOPMENT-WORKFLOW.md#0-先看这里我该怎么使用飞书协调者) · [2.0调整与验证记录](/home/wang/work/doc/analysis/coordinator-interaction-reset-20261014/REPORT.md)
