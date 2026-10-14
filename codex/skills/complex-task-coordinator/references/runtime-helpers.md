# 单协调入口与确定性工程工具

按需读本页：处理群请求、查询负载、环境预检或等待长工程操作时使用。源码维护在 `/home/wang/work/hermes-role-runtime`；Skill 入口 `scripts/runtime_tools.py` 为软链接。工具不批准任务、不替代测试/OCR/强审查、不新增生产操作权限。

## 人工交互与业务执行

Hermes原生群会话始终保留，用户自然语言问进展、解释或讨论方向时直接由模型读最新证据后答复，不自动入业务队列，不等定时poll。插件只为实际绑定协调群定位当前角色，新会话通过原生auto_skill加载协调者与显示Skill，旧会话提示读取最新版Skill，不修改system/tools前缀；普通私聊、无关群、执行者/测试者群不改变。

群与cron是同一逻辑角色的不同技术会话，共用角色租约与任务状态。问答不持业务租约；明确业务动作才保存请求，再争取同一执行权。取得租约后必须复读TODO/派单/ACK/decision、实际任务/attempt及已有处理证据；已执行不重做。空闲时群可直接执行既有授权内的协调动作；忙碌时保存原文并说明接续条件，后台poll随后消费，不能让用户失去对话入口。

用结构化工具写入临时JSON（避免拼shell原文），包含原生message_id、chat_id、user_id、text及context（引用与附件身份）。不能把模型总结或第三方引用冒充用户授权。仅明确需要执行的指令使用：

```text
python3 /home/wang/.codex/skills/complex-task-coordinator/scripts/runtime_tools.py request --project ROOT --input 原生指令.json
python3 /home/wang/.codex/skills/complex-task-coordinator/scripts/runtime_tools.py queue --project ROOT
python3 /home/wang/.codex/skills/complex-task-coordinator/scripts/runtime_tools.py queue-ack --project ROOT --request-id 完整ID --evidence ROOT内处理记录 --token-file ROOT/inbox/control/runtime/leases/COORDINATOR.json
```

request仅保存一次，按chat/message ID去重、不拿业务租约、不开启暂停job；queue-ack必须有效协调租约与项目内非空处理证据。原群消息身份冲突拒绝覆盖。observer ack只确认变化通知，不能消费业务指令；HANDLED只代表处理/转派/阻塞已有证据，不证明业务完成或回复送达。崩溃后先对账已有动作，外部操作继续用原幂等身份，不能靠租约宣称exactly-once。旧队列记录保留，不批量作废。

只有用户显式发送/progress才走可选脚本简报，当次读当前文档；自然语言进展仍由模型回应。脚本读取失败不显示旧数据为当前。两种答复先读同一`progress-facts`当前事实包，再按显示Skill呈现；协调者整体进度固定七个加粗分区与简短条目，证据详情按需展开。主动问答走群路径，不受夜间cron主动通知抑制；被暂停的业务不由聊天自动恢复。原生slash控制/审批不拦截。

## 当前进度的共同事实入口

```text
python3 /home/wang/.codex/skills/complex-task-coordinator/scripts/runtime_tools.py progress-facts --project ROOT --json
```

模型答复和脚本报告均读取本次生成的事实包，入口实现为`progress_projection.py build_projection(project, at=None)`。当前事实落盘在`inbox/control/runtime/progress/facts.json`，仍须当次生成或核对来源时效，不把保存的旧投影冒充当前。每字段保留来源与有依据的发生/观测/记录时间；来源缺失、过旧或矛盾不静默覆盖，指出具体差异及核对动作。事实包是文档投影，不替代原测试/OCR/强审查证据，也不获取业务写租约或生成新业务事件。标题/读取时间不能充当进展发生时间。

## 机械负载直接读指标

```text
python3 /home/wang/.codex/skills/complex-task-coordinator/scripts/runtime_tools.py load --json
```

直接 GET LAN `/metrics`，不调用模型。仅在当前ROLE-BINDINGS实际绑定的角色群中，`/llm-load`、`当前大模型负载情况`、`查看千问负载`等精确机械查询由插件直接回复；要解释原因/修改参数的请求仍交模型。缺失指标或采集错误标未知；首样本累计值不冒充速率，不从一个瞬时值判断来源占比。

## 环境预检、恢复与长操作

```text
python3 /home/wang/.codex/skills/complex-task-coordinator/scripts/runtime_tools.py doctor --cwd 实际工作目录 --storage-path 实际数据目录 --need-bytes 所需字节 --program gzip --docker
```

检查目标真实文件系统与 Docker 实际根，不按配置名猜空间。预检失败即停，先修具体环境。恢复须先核备份、目标、授权和已停止owner；不会因检查通过自动恢复生产数据库。

长工程步骤先把 JSON spec 落盘：

```json
{
  "operation_id": "TASK-xx-attempt-2-environment",
  "task": "TASK-xx", "attempt": "attempt-2",
  "cwd": "/absolute/authorized/workdir",
  "need_bytes": 0, "storage_paths": ["/absolute/authorized/workdir"],
  "steps": [{"argv": ["python3", "/absolute/existing-check.py"]}]
}
```

每步使用 argv，不拼装 shell。需要已授权的 gzip 恢复时设置该步 `stdin_gzip` 为备份绝对路径；helper先解压再送下游，输入损坏或下游非零均失败，后续步骤不执行。调用的脚本本身也必须传播真实退出码。凭据使用现有受控凭据文件，不放进 spec/argv/日志。

```text
python3 /home/wang/.codex/skills/complex-task-coordinator/scripts/runtime_tools.py start --project ROOT --role A --spec SPEC.json --token-file ROOT/inbox/control/runtime/leases/A.json
python3 /home/wang/.codex/skills/complex-task-coordinator/scripts/runtime_tools.py status --state 返回的result.json路径
```

start复用有效角色租约和ACTIVE控制；冻结输入，独立systemd服务跨回合执行。相同ID不会重复跑，输入改变拒绝；本角色有在途/UNKNOWN操作不启动第二项。提交成功不等于完成；进程消失/PID复用/服务状态不明标UNKNOWN，保留日志待对账，不自动重试。状态与日志在 `inbox/control/runtime/operations/<ROLE>/<ID>/`。五分钟observer只看状态和进程身份变化；启动后记录检查点并结束等待回合，无变化不反复问模型或循环status。失败/UNKNOWN由原owner按证据纠正，显式新attempt才能重新执行。

本入口用于已授权的环境检查/恢复/常规长工程命令。**OCR仍只走 `ocr_runtime.py`，强审仍只走 `strong_review.py`。** 被安全扫描拒绝的命令不能移入spec、脚本或systemd绕过审批；应保持阻塞并按原流程处置。`SUCCEEDED`仅代表命令退出0，原独立验证、覆盖、真实样本及合入门禁全部保留。

## 前缀审计

插件只记录 system/可取得的tool schema指纹、变化次数和长度，不修改提示/工具/推理参数，不新增模型tool或动态prompt section。Hermes同一会话已经冻结系统提示、工具已有稳定排序，合法压缩和契约更新仍生效。`~/.hermes/runtime/prefix-audit/`可查变化；hook工具材料经过脱敏/限长，`UNAVAILABLE`不能视为无工具或完整schema证明。不能用保持缓存为由屏蔽真实新规则。先按会话定位变化原因，再判断是否无必要，不凭变化次数自动改系统提示。

普通私聊、未绑定群和其他项目会话保持Hermes原生模型处理路径。不能因为曾用某会话查负载就把它加入脚本拦截名单；旧load_query_chat_ids配置已废弃，代码不再读取。

## 用户使用说明与投递边界

见[指南§0](/home/wang/work/doc/SKILL-DEVELOPMENT-WORKFLOW.md#0-先看这里我该怎么使用飞书协调者)。直接自然语言交流即可；/progress与/llm-load是可选机械命令。21–08静默只限制主动cron通知，原生人工会话可以答复。群与后台不共享全部聊天记忆，任务和证据以共享文档为准；HANDLED不代表送达。人工会话仍受服务排队及原生busy处理影响，不承诺零延迟。当前互斥依赖原租约和角色按协议使用受控写入口，不能称为对任意terminal命令的系统级强制沙箱。


## 固定采样、安全记账与测试证据（scripted-role-v2）

长期运行任务由项目`runtime/task-observation-spec.json`配置固定只读采样，五分钟scan采一次Backend/远端进程/存储证据。当前观测保存在`runtime/observations/tasks/`；只在状态、身份、错误、阈值或未知/恢复变化时更新`runtime/task-notices/`供对应角色接续。正常心跳/CPU/mtime变化不触发模型，不反复手写ssh/数据库探针；观察未知不等于运行健康，终态仍需独立验证。对象、阈值与路径由维护者核对，配置不新增生产写权限。

已有有效角色租约时，结构化记账使用：

```text
python3 /home/wang/.codex/skills/complex-task-coordinator/scripts/runtime_tools.py role-record --project ROOT --role C --input record.json --token-file ROOT/inbox/control/runtime/leases/C.json
```

`record.json`包含稳定`event_id`（以`C:`等实际角色前缀开头）、`kind`、`task`、`attempt`、`status`、`summary`、`evidence`、`next`。`kind=business`原子追加canonical session并写唯一事件，journal支持中断补齐；`kind=observation`仅保存独立观察，不写session/event。相同ID内容变化拒绝；不会领取、续租、抢占或批准任务，不再手工拼shell/解析token/覆盖文件。每轮无变化不必生成新的记账记录，脚本已有巡检时间。

当前刚发生的行为可指定`occurrence:{"kind":"now"}`，脚本使用系统时钟生成日期、时间与时区，内部自动记录的秒可保留；群报展示到分钟即可。历史行为仅通过`occurrence:{"source":"项目内证据.json","field":"ended_at"}`读取有来源的明确时间；完整日期、明确分钟与时区即可接受，如`2026-10-14T10:22+08:00`，保留原始分钟精度，不补造秒数。缺证据省略occurrence，保存`occurred_at:null`并明确发生时间未知。`recorded_at`为脚本写入时间。禁止将观测、消息发送或文件mtime当发生时刻，禁止手填模糊钟点、缺日期或缺时区的发生时间。

固定测试命令复用原operation runner，在spec增加`evidence:{"inputs":[],"config":[],"environment":[]}`（显式列出影响结果的文件/环境变量名）。使用`evidence-record`，其参数与`start`相同；结果旁生成`.evidence.json`：

```text
python3 /home/wang/.codex/skills/complex-task-coordinator/scripts/runtime_tools.py evidence-record --project ROOT --role TESTER --spec test-spec.json --token-file ROOT/inbox/control/runtime/leases/TESTER.json
python3 /home/wang/.codex/skills/complex-task-coordinator/scripts/runtime_tools.py evidence-check --record result.evidence.json
```

记录前后commit、index/worktree/未跟踪文件、声明输入/配置/环境指纹、命令身份、实际退出码及日志hash。完整argv保存在原冻结spec，凭据继续用受控文件。状态和日志放代码Git仓库外。`mechanical_match=true`仅表示声明范围与快照匹配，不是业务PASS；未声明的ignored文件、远端服务和测试覆盖不在机械证明中，必要独立测试/合入回归/OCR/强审仍进行。代码变化或失败不借旧证据放行。
