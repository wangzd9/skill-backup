---
name: executor-session-triage
description: 多角色开发的会话判活、租约接管与残留处置。Use when 执行者长时间无产出、需接管过期租约或处理完成未冻结残留。
metadata:
  version: "1.0.0"
---

# 执行者会话判活与残留接管

适用：多角色/多 cron 执行者并行开发时，协调者判断某个执行者会话是"在干活"还是"挂死"，以及 kill/催办前的保全动作。宿主 skill 若自带巡检规则以其为准；本 skill 管判活/处置/残留三个环节，不替代业务门禁。

## 判活：单轮快照不是证据

1. 先查三处再下结论：usage 审计里该会话**最近一次模型调用的 duration**（长会话单轮可达数分钟到 10+ 分钟）、会话日志最近成功响应时间、worktree/scratch 产物 mtime。
2. "零子进程+文件零写入"只是大模型调用间隙的正常画像；**必须连续两轮巡检（≥10 分钟）**零写入+零子进程+零 CPU，且（若有）VLM 连接长期挂起，才可定性挂起。
3. 归属证据先核再算账：scratch 产物、日志按文件名/内容归属会话，不把别的角色会话的产出算到被疑者头上。

## kill 前保全协议（强制）

1. kill 前先 `git status --short` + `git diff --stat` 核 worktree 未提交进度——kill 不伤磁盘，伤的是会话上下文里"做到哪了"的记忆。
2. 有进度时**先落续接声明再 kill**：哪些项已完成/在途/未动、磁盘证据位置（文件+diff）、"续用不重写、禁止回滚已完成面"。落 inbox 后重触发巡检并附紧急上下文。
3. 无进度 kill 后重触发；重触发 prompt 写清上一轮死因与禁止重做的事。
4. 误判 kill 要公开纠正并落账（根因=采样撞上模型间隙），修正巡检判活口径，不静默带过。

## "完成未冻结"残留

执行者代码已写完但会话被长验证（真实环境实验/大测试）消耗后自然结束，冻结步骤落在轮次外；之后巡检因"无新派单"空转，无人推它。识别信号：树上 diff 完整且自洽、无新 commit、会话已 terminated。

处置：
1. 催办单点名**只做三件事**：复跑定向回归（树未动可直接引用上轮结果，不重跑）、一次冻结 commit、更新回执；显式禁止重做已完成工作、长验证最多再跑一次带 timeout。
2. 可手动触发该角色巡检省等待（脚本 wake 条件成立时）。
3. 若同一修复面被另一执行者/强模型直接接管完成，旧派单被事实性取代——按磁盘证据（commit+独立验证日志）收口，不等被取代者 ACK。

## 外部二进制升级 vs 版本门禁

固定入口脚本（如 ocr_runtime.py）常写死外部工具版本门禁。工具二进制被升级（npm 等）后门禁不匹配会**静默拒掉所有 prepare/启动**，表面像"审查没排上"。

升级处置顺序：核对 CLI 参数面（`--help` + 关键子命令如 `--preview` 实测，新增参数不影响既有用法才放行）→ 同步门禁与测试 fixture 的版本字符串 → 跑该入口自带的全套隔离回归（零真实模型调用）→ 在版本基线引用文档记录核对结果 → 放行下一次真实 run 并核其 spec 记录的新版本。历史 run spec 里的旧版本及其裁决不受影响，不重审。

## 过期/死 owner 租约的接管（协调者机械动作）

判定与接管分两步，**租约到期不等于 owner 已停**：

1. 先核实旧 owner 真已停：lease 文件里的 `expires_at`/`pid`/`process_start`、`role_runtime.py status` 的 `owner_process_state`、按 owner 名搜进程、以及该角色**最近一次 canonical 落账时间**。把这几项写成证据文件（放 scratch，别放仓库文档目录）。
2. 接管顺序固定为 `recover` → `claim`，两者参数都有硬要求，写错白跑：
   ```bash
   role_runtime.py recover --project ROOT --role ROLE --owner <旧owner全名> --confirmed-stopped --evidence <证据文件>
   role_runtime.py claim  --project ROOT --role ROLE --owner <新owner名> --ttl 600   # ttl 只接受 30–600
   ```
   `recover` **必须传 `--owner` 且与当前 lease 里的 owner 完全一致**，否则返回 `Recovery owner does not match; recheck status`——所以先 `status` 读 owner 再 recover，不要凭记忆拼。`claim` 不传 `ttl` 或超出 30–600 直接报错不执行。接管后用同一 token 收尾，完成后 `release`。

业务指令的记账链路固定为三步，顺序不能颠倒：`request`（保存原生 message_id/chat_id/user_id/原文）→ 执行动作 → `role-record`（canonical session + 唯一事件）→ `queue-ack --request-id <完整ID> --evidence <ROOT 内处理记录>`。`queue-ack` 要求有效租约**且**证据文件在项目根内，否则拒绝；`HANDLED` 只代表已处理并留证，不代表回复已送达。

## 判活/处置通用坑

- 判断"在途任务进度"用磁盘产物+会话日志的**内容时间**，不用巡检 tick 时间或 sessions 文件 mtime 冒充进展。
- 长会话中途不刷 sessions 文件；in-flight 真相在 cron 执行记录（`executions.db` status/pid）+ 进程树 + 子进程活动。
- 手动重触发被 "already running" 挡住 ≠ 故障，先查该轮是否正在跑长模型/长命令。
- `role-record` 的发生时间只有两种合法写法：刚发生用 `"occurrence":{"kind":"now"}`（脚本取系统时间），有来源的历史行为用 `"occurrence":{"source":"<项目内证据文件>","field":"<时间字段>"}`；缺证据就整个省略。**别把"17:0x"这类模糊钟点写进 summary 当时间**——脚本会以"禁止模糊钟点"直接拒绝整条记录。
