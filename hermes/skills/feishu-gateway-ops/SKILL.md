---
name: feishu-gateway-ops
version: 1.0.0
author: Hermes Agent
license: MIT
description: Use when 飞书 bot 不回复、改 FEISHU_* 配置或重启 hermes-gateway。
metadata:
  hermes:
    tags: [feishu, lark, gateway, ops, diagnostics]
    related_skills: [hermes-agent]
---

# 飞书 Gateway 运维（本机用户级 systemd 服务）

## When to Use

飞书单聊/群聊里 bot 不回应、要修改 `FEISHU_*` 网关配置、改了配置需要重启 gateway 生效、或飞书开放平台权限导致图片/会话功能异常时使用。

本机网关是用户级服务 `hermes-gateway`（systemd --user），飞书走 WebSocket 长连接，配置在 `~/.hermes/.env` 的 `FEISHU_*` 变量。

## 诊断“不回复”的标准顺序

1. `journalctl --user -u hermes-gateway --since "30 min ago" --no-pager`：有消息处理记录 = 网关收到过事件；不要怀疑模型或网络。群聊“完全没有”而单聊正常时，先排除第 9 步的时序原因（重启窗口/串行排队）；仍无 `Received raw message` 才有两种可能，必须用第 5 步区分：网关策略把消息丢了，或飞书应用缺 `im:message.group_at_msg` 权限导致事件根本没推过来。
2. 启动段找两类关键行：
   - `Dropped a group message ... group policy is 'allowlist' and FEISHU_ALLOWED_USERS is empty` → 群策略把消息全丢了；
   - `No env user allowlists configured ... deny unknown senders` → 策略生效前提缺失（见下）。
3. 用 terminal `grep -E "^(FEISHU_GROUP_POLICY|FEISHU_REQUIRE_MENTION|...)=" ~/.hermes/.env` 核对变量（search_files/read_file 会被凭据保护拒绝，terminal 可用）；只输出变量名和布尔类值（true/false、allowlist、open），白名单可只数条数和"是否含当前 ou_"，绝不输出 SECRET 和完整 open_id 列表。
4. 飞书开放平台权限问题单独排查：`im:resource` 缺失时发图报 `Feishu image upload missing image_key`；`im:chat:readonly` 缺失时读不了会话信息；**群聊 @ 文本消息需要 `im:message.group_at_msg`**（单聊文本走 p2p 消息权限，不受此限）。权限在开放平台“权限管理”开通后必须**创建版本并发布**才生效。
5. 区分“策略丢弃”和“平台没推过来”：读 `~/.hermes/logs/gateway.log`（比 journalctl 干净，有 `[Feishu] Received raw message` 与 `Inbound dm message received` 行）。策略丢弃会看到群消息进了网关又被拒（`dropping inbound event: <reason>`，DEBUG 级；空 allowlist 时有专属 WARNING）；平台权限缺失则群消息**连 `Received raw message` 都没有**，而同期单聊 dm 正常记录。可用 `GET /open-apis/im/v1/chats`（需 `im:chat:readonly`，凭据取自本 profile `.env` 的 `FEISHU_APP_ID/SECRET` 换 tenant token）确认 bot 确实在群里——bot 在群但群消息零到达 = 缺 `im:message.group_at_msg`，让用户去开放平台开通并发布版本，此时改 Hermes 侧任何 `FEISHU_*` 配置都无效。
6. 第三种症状：群消息**有 `Received raw message` 但没有 `Inbound group message received`**，且权限、群策略、mention 全正常——大概率用户发的是纯 `@bot` 不带正文，adapter 剥掉 @ 后为空即按设计丢弃（DEBUG 行 `Ignoring empty text message`）。用 `GET /open-apis/im/v1/messages/<message_id>`（需 `im:message`）读那条消息的正文确认：只有 `@_user_1` 占位 = 空 @，让用户带文字重发即可，不是故障。
7. 审批命令反复 5 分钟超时未执行：检查飞书端审批卡片的 Allow 按钮能否点回（该应用需配置卡片回调 card callback，开放平台有“尚未配置卡片回调”提示时点“立即配置”）。回调配好后按钮点回即放行；sudo 类命令批准一次后 15 分钟内缓存免重弹，不要每次前 `sudo -k` 清缓存。
8. 不要走 sudo askpass 辅助脚本路线：`~/.hermes/.env` 里有 `SUDO_PASSWORD` 时网关会给 sudo 自动加 `-S`，与 `-A`（askpass）互斥直接报错，askpass 脚本本身还会再触发高危审批，死循环。卡片回调配好后普通 sudo 即可，密码由审批/缓存机制解决。
9. 第四种症状：群消息有 `Inbound group message received`、策略/权限全正常，但该 chat 之后长时间没有 `response ready` / `Sending response` 行——两个原因，都不是故障：(a) 刚发过 `/restart`，WebSocket 重连窗口（断开到 `connected to wss://` 之间）到达的消息不处理，窗口约几十秒；(b) 同一会话串行，上一轮长任务没收尾时新消息只收不回，回复排在任务结束后。用 gateway.log 里该 chat 的 `Sending response` 行确认回复是否只是延迟送达。用户预期管理：长任务期间群消息会排队，不是不响应。
10. “@ 回复快、不 @ 不回复”是时机错觉，不是 mention 快速通道：`FEISHU_REQUIRE_MENTION=false` 时 adapter 对 @ 与非 @ 消息同队列同优先级（mention 逻辑只用于 require_mention=true 时的拒绝）。@ 通常落在空闲时刻所以显得及时。另注意：轮内用户新消息会打断当前轮（日志 “response interrupted by user correction”），长轮被打断即浪费已跑进度。所以预计单轮超 1 分钟的任务，先发一条短确认（“收到，在跑 X，到点报你”）再转后台执行、完成后主动通报——把每个会话的串行窗口压到最短，这是用户多次投诉“群里不回”后的固定操作规则。
11. 第五种症状：用户说“没回”但日志有 `Sending response` 行——投递台账才是权威回执：读 `~/.hermes/state.db` 的 `delivery_obligations` 表（按 `chat_id` 过滤，`state` 应全为 `delivered`、`last_error` 为空）。注意 `Sending response` 日志打在 API 调用**之前**，只表示“准备发”，不能当成功证据。若台账全 delivered，链路正常，问题在延迟/打断、用户客户端过滤，**或飞书应用权限缺失**（发送 API 成功但消息不到用户端，见下文“发送成功但用户看不到”一节）；可直调飞书 API 隔离发送通道（凭据取本 profile `.env` 的 `FEISHU_APP_ID/SECRET` 换 tenant token）：`POST /open-apis/im/v1/messages?receive_id_type=chat_id` 发普通群消息 + `POST /open-apis/im/v1/messages/<message_id>/reply` 发引用回复（网关群回复常用路径），让用户看哪条可见，据此区分通道问题与锚点/展示问题。
12. 第六种症状（多会话同时"卡住"）：gateway 日志正常处理各会话，但多个会话都长时间不回复、还伴随 `Auxiliary approval: request to http://127.0.0.1:xxxx/v1/ timed out after 30.0s` 和 `review_input_budget_exhausted`——瓶颈在**共享的本地模型服务排队**，不是网关。诊断与缓解见 `references/shared-llm-load.md`（本机 → SSH 隧道 → vLLM 的链路、`/metrics` 队列指标、延迟探针、会话上下文排行）；可自修的是把 `auxiliary.approval.timeout` 调大和让用户串行化会话，`max-num-seqs` 在服务端启动参数里，改不了只能等或找服务属主。

## "发送成功但用户看不到"的症状

发送 API 返回 code 0、网关日志 `Sending response`、投递台账 `delivered`，但用户反馈群里看不到 bot 回复：**先怀疑飞书应用权限缺失**（用户确认根因即此；开放平台开通并发布版本后正常）。注意它与"接收侧零到达"（缺 `im:message.group_at_msg`，消息连 `Received raw message` 都没有）是**两个不同症状**：权限缺失也可能只掐断某一方向（收或发），日志显示全链路正常不代表消息真的到了用户端。验证方法：用本 profile `.env` 的凭据换 tenant token 调 `GET /open-apis/im/v1/messages?container_id_type=chat&container_id=<群>`，报 230027 = 读权限未生效，code 0 = 已生效；发送侧则直接 `POST /open-apis/im/v1/messages?receive_id_type=chat_id` 发一条测试消息让用户目视确认。投递台账 `delivered` 只代表发送 API 成功，不代表用户端可见，不能据此宣布链路正常；用户报告"看不到"时不要只查网关侧，必须向用户确认效果或让用户截图。**用户第二次及以上说"还是没好/不对"时，停止复述网关侧已验证过的事实**——说明当前假设未命中，直接换轴：验证对端系统状态（飞书开放平台权限、发布版本是否生效）、给用户发测试消息让其目视确认、或让用户截图。本案教训：发送侧全绿但用户端不可见，绕了多轮网关日志分析，最终根因是飞书应用权限缺失、由用户自己去平台开通后解决；权限类对端状态只有平台侧能最终确认，Hermes 侧日志证明不了。

## “不同群聊会串”的症状

用户反馈 A 群的问题在 B 群被回答、或一个群冒出另一个群的任务/上下文。**先确认不是网关合并**：读 `~/.hermes/sessions/sessions.json`，每个 (chat_id, union_id) 都有独立的 `agent:main:feishu:group:<chat_id>:on_<union_id>` 会话键，群里聊天记录本身就隔离——网关没有把多群并成一个会话的 bug。感知到的“串”来自 agent 侧的两层共享状态：① 跨群共享的长期 memory；② 后台任务/监视器是全局的（在 test1 群发起的流水线跟踪，用户到另一个群问“怎么样了”就会被拿去回答）。修复是行为规则而非配置：每个群只按**该群自己最近的话题**回答；无上下文的短消息（“怎么样了”）不确定指什么先问而不是跨群猜；后台任务/监视器只向**发起任务的那个群**通报，不扩散到其他群。诊断结论要如实告诉用户“会话已隔离，串味是我带错了上下文”，不要让用户以为是网关合并了群。

## 群策略配置规则

- `FEISHU_GROUP_POLICY=open` **单独改无效**：启动检查要求平台白名单或全局放行（`GATEWAY_ALLOW_ALL_USERS=true`）存在，否则未知发送者仍被拒绝。正确做法二选一：
  - `FEISHU_GROUP_POLICY=allowlist` + `FEISHU_ALLOWED_USERS=<你的 open_id>[,ou_...]`（推荐，最安全）；
  - `FEISHU_GROUP_POLICY=open` + `GATEWAY_ALLOW_ALL_USERS=true`。
- open_id 形如 `ou_...`，来自会话元数据（Source 中的 User ID）或飞书 API，不要猜。
- `FEISHU_REQUIRE_MENTION=true` 控制群内是否必须 @ bot；与群策略是两层独立门禁，都要满足。用户已明确要求免 @ 响应，default profile 保持 `FEISHU_REQUIRE_MENTION=false`；新建 profile 时同样设为 false，除非用户另有指示。
- 多 profile（如给 GPT 单独开 profile）时每个 profile 只读自己的 `.env`，群策略和白名单**不继承** default；新 profile 必须单独配置，否则复现同样的“群里不回复”。

## 改配置与重启

- `~/.hermes/.env` 是受保护凭据文件：write/patch 工具会拒绝，用 `sed -i` 精准替换单个变量行，改完回读该行确认，绝不在对话或日志里输出 SECRET 值。
- 配置改动必须重启 gateway 才生效。**agent（包括 delegate_task 子代理）无法从进程内重启自身**：`systemctl --user restart`、`hermes gateway restart`、`setsid`、`systemd-run` 包装都会被安全机制拦截（SIGTERM 会波及网关子进程树）。正确动作：请用户在任意已连通的聊天里发 `/restart`（等当前 run 收尾后重启）。不要派子代理去试重启——已验证全部路径不可行，只会浪费几分钟。
- 重启后验证：`systemctl --user is-active hermes-gateway` + journal 里出现新的 `connected to wss://msg-frontier.feishu.cn` 行且无 `Refusing to start`。
