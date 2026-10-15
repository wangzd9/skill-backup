---
name: hermes-model-routing-ops
version: 1.3.0
author: Hermes Agent
license: MIT
description: Use when 诊断或切换本机模型路由：角色会话为何走云端、切了模型不生效、OCR审查用哪个模型。
metadata:
  version: "1.3.0"
  hermes:
    tags: [hermes, model-routing, ocr, config, diagnostics]
    related_skills: [hermes-usage-cost-stats, llm-service-load-check]
---

# 本机模型路由（本地 vLLM ↔ 云端）运维

## When to Use

- 用户问"为什么还在跑 pro / 为什么走云端"，或"我已经切成 flash 了为什么没变"。
- 要把角色会话切到本地或换云端供应商/模型。
- 要确认 OCR 代码审查当前用的是本地还是云端模型。

## 机制（先记住这条链，再动手）

网关在每次发请求前调用插件 `role-workflow-runtime` 的 `resolve_model_route` hook，对**已绑定角色的身份**返回覆盖后的模型。因此：

- **会话级 `model` 字段对角色会话不是生效项**，会被 hook 覆盖。切模型只改会话字段 = 无效。
- 生效项是路由配置里的 `cloud_model`（判为云端时）或 `local_model`（判为本地时）；判据只有本地等待队列，没有时间段。
- 判据链：`api_identity` 按 ROLE-BINDINGS 的 `chat_id` 或 cron 的 `poll_job_id` 匹配 → 命中才选路；未绑定的会话（用户 DM、临时群）保持各自记录的 `model`。
- 选路规则 `choose_route(mode, metrics)` **只看本地等待队列**：①`mode=='cloud'` 直接云端（不探测）；②`mode` 非法值保护性走云端；③`running is None or waiting != 0` → 云端；否则本地。**时间窗（原 `8 <= hour < 21` 白天短路）与流水线忙闲（原 `pipeline_busy` 闸门）都已按用户要求移除**——本机准入的既有取向是"只认本地队列空闲"，不要再引入或恢复额外闸门（用户对再次加闸门的倾向是明确否定的）。`pipeline_busy()` 方法保留但已不参与选路，改动时不要误以为它还在拦截。
- `auto` 与 `local` 现在走**同一判定**，差别只剩持久化的模式名；`cloud` 仍短路、一次 GET 都不发。未指定的会话/后台任务按各自 `model` 字段走，不参与这条策略。
- 每次判定发一次 metrics GET（超时 2 秒），失败或字段缺失即"未知"→云端。用户问"为什么还在走云端"时，**先实时采一次 `running`/`waiting` 再回答**，不要用时间或流水线状态解释（这两个已不是原因）。
- 运行态：`~/.hermes/runtime/model-routing-state.json`（`mode` = auto/cloud/local，另有 `cloud_profile`、`updated_at`）。
- 群内控制（不调用大模型）：`/llm status`、`/llm local`、`/llm auto`、`/llm cloud [供应商]`。改动对**后续**请求生效，已发出的单次推理允许跑完。

## 切换云端模型（"切了不生效"的正确解法）

1. **`~/.hermes/config.yaml` 对 `write_file`/`patch` 是拒写的**（安全机制："Agent cannot modify security-sensitive configuration"）。不要走 sed/脚本绕，走正规入口：
   ```bash
   hermes config set "plugins.entries.role-workflow-runtime.settings.model_routing.cloud_model" "<模型>"
   hermes config set "plugins.entries.role-workflow-runtime.settings.model_routing.cloud_profiles.<profile>.cloud_model" "<模型>"
   ```
   用 `hermes config get <同名点分键>` 或 `hermes config path` 复核。
2. **两处都要改**：`cloud_model` 与 `cloud_profiles.<profile>.cloud_model`。选了 profile（`default_cloud_profile` 或 `cloud_profile`）时 profile 的值**覆盖**顶层 `cloud_model`，只改顶层等于没改。
3. 各 profile 的 `cloud_provider`/`cloud_model`/`cloud_key_env` 在 `model_routing.cloud_profiles` 下；供应商定义（base_url/api_mode/key_env）在 `config.yaml` 顶层 `providers`。密钥只写在 `~/.hermes/.env`，报错信息里只提变量名，不回显值。
4. 不需要重启网关：hook 每次请求重读配置，`state.db` 里马上能看到新模型名。

## 改选路策略本身（闸门/阈值）

选路逻辑住在插件源码里，**不在 `config.yaml`**。本机 `~/.hermes/plugins/role-workflow-runtime` 是指向源码仓库 `/home/wang/work/hermes-role-runtime` 的**软链**，策略是 `model_routing.py::choose_route`，测试在 `tests/test_model_routing.py`。用户提"这个校验太严了，改成 X 就行"时按下面四条判断，不要直接动手。

- **本地判据不是配置项**：准入条件写在代码里，`hermes config set` 改不动。改代码 ≠ 改配置。
- **代码改动不会自动生效**：`config.yaml` 类改动 hook 每次请求重读、立即生效；**插件 `.py` 改动必须热加载**（原生控制入口 `gateway.control_socket.reload_gateway_plugins(HOME)`，与历史部署脚本同一路径），否则用户以为改了、实际还在跑旧逻辑。改前先 `git status --short` 确认仓库干净，只提交本任务文件。
- **改 `choose_route` 会同时改 OCR 选路**：`ocr_model_route.py` 调的是同一个 `router.decision()`，所以放宽/收紧闸门会一并改 OCR 的 provider。方向必须说对：OCR 代码审查**本就该用弱模型、优先本地**——"审查用强的"指的是审查者（`strong_review.py` 那条强模型审查链），不是 OCR。OCR 落到本地 Qwen 是期望行为而非降级，不要把它写成"代价"去劝阻改动；需要交代的是相反情况：改动若把 OCR 推到强云端（或错用 `ocr_profile`），那才是副作用。
- **放宽准入闸门先枚举"哪些输入会翻转"**：把闸门从 `running==0 && waiting==0` 放宽到只看 `waiting==0`，翻转的输入是 `running>0 && waiting==0`——该实例 `max_num_seqs=4`，`running=4 且 waiting=0` 是并发槽刚占满那一刻。`running=1..3 且 waiting=0` 则是真有空槽、适合让步的时刻。先算出翻转区间和当前真实指标再报告，不要把"饱和"当否决理由（用户取向是能本地就本地）。**用户说"流水线状态不管"时不要再拿 `pipeline_busy` 当拦截理由**——它已不参与选路。用户对本机资源使用的取向是"能本地就本地"（甚至宁可本地排队、OCR 降级到本地也要用本地），所以翻转落在饱和区**不是否决理由**，是知情项：报清矩阵后按用户拍板执行，不要用它替用户否决。

完整改动步骤（同步测试断言、跑单测基线、热加载、按用量表验证、回退）见 `references/routing-policy-change.md`。

## OCR 审查的模型是耦合的（改一处必须改三处）

OCR 每批启动前经 `ocr_model_route.py select <project>` 探路由，并**逐字段校验** `~/.opencodereview/config.json` 里对应 `hermes-<profile>` 条目的 `url`/`model`/`protocol` 是否与路由结果一致；不一致直接抛 "OCR provider configuration differs from the selected Hermes route"。所以改云端模型时：

1. `config.yaml` 的 `cloud_model`（顶层 + profile，见上）；
2. `~/.opencodereview/config.json` 的 `custom_providers.hermes-<profile>.model`（`write_file`/`patch` 可写，非受保护文件）；
3. 复核 `ocr_model_route.py select <project>` 返回的 `model` 与两条配置一致。

`custom_providers` 里同时存在 `vllm-local` 与多个 `hermes-*` 云端条目，**未在用的条目也在文件里**，不要当成证据。

### OCR 与群会话可以走不同供应商（别拿会话状态推断 OCR）

OCR 有自己的 profile：`model_routing.ocr_profile`（无此键时退回会话的 `cloud_profile`）。标志是 `ocr_model_route.py select` 里的 `config = router.config(ocr_profile)`。因此 `~/.hermes/runtime/model-routing-state.json` 的 `cloud_profile` 只代表**群会话/后台任务**，可以是 sub2api，而 OCR 实际走 xcoding——**判断 OCR 走哪条路由只认 select 的返回和 run 的 `batches[].execution_route`，不要读会话状态文件**。改 OCR 供应商时改 `ocr_profile` 指向的 profile（或新增 profile），不必动会话选路。

### 换端点/供应商后重提一轮 OCR 的正确姿势

换端点即换配置身份，旧的 run 不能续跑，但入口对重提有两条硬约束，容易白跑一次：

1. **输出目录必须尚不存在**：`prepare --output <RUN_DIR>` 遇到已存在的目录直接 `RUN_DIRECTORY_EXISTS` 返回 BLOCKED（不覆盖历史）。所以**背景文件不能先写在 RUN_DIR 里**——先写到 run 目录之外（如 scratch），再 `prepare --background-file <该路径> --output <新目录>`。
2. **不要加 `--reuse-run`**：换供应商后 `review_identity` 已变（identity 含 `model_routing`），带 reuse-run 会 `REUSE_IDENTITY_MISMATCH`。新 run 天然 `retry_depth=0`，是独立额度，不受旧链"初审+一次补审"限制。

提交后按 `status` 的 `batches[].execution_route`（provider/model/effort）确认本批真的走到了目标端点，并把这条实际路由写进裁决/事件证据——这是"路由确实生效"的硬证据，`spec.json` 的 model/provider 只是 prepare 时的意图。

## 云端 OCR 的端点协议门槛（选端点前必验）

把 OCR 指向某个云端端点前，先确认它对**非流式**请求返回 `application/json`：

- OCR 的 Go 客户端按 `application/json` 解析非流式响应。端点若对 `stream:false` 仍回 `content-type: text/event-stream`（部分中转站的统一行为，`/v1/responses` 与 `/v1/chat/completions` 都一样），该批会立刻以 `expected destination type of 'string' or '[]byte' for responses with content-type 'text/event-stream' that is not 'application/json'` 失败：`returncode=1`、`coverage_complete=false`、gate `NOT_EVALUATED`，几十秒即终。
- 与负载/超时/模型能力分开归因：撞 3900s 单批硬截止是排队问题（端点 `running`/`waiting` 不为 0），协议失败是秒级失败且 diagnosis 里带上述 `item_error_messages`。
- 选前用 `scripts/probe_ocr_endpoint_protocol.py <provider>` 实测（只读、不打印密钥）。VERDICT `UNUSABLE` 的端点不要用于 OCR，即使它在飞书会话里工作正常——会话走网关 provider 通道，OCR 走自己的客户端，两者兼容性不同。
- 已知差异：`hermes-xcoding`/`gpt-6.1-sol` 非流式返回 `application/json`（OCR 可通）；`hermes-sub2api` 的 `deepseek-v4-pro` 与 `deepseek-v4.1-flash` 非流式仍回 SSE（OCR 不可用）。
- 已有可通组合的取证：`~/.opencodereview/sessions/*/*.jsonl` 的 `session_end` 记录带 `execution.provider`/`model` 与 `coverage`，可直接确认某 provider 是否已跑出完整覆盖（`completed` 非空、`failed` 为空），不必先盲试一轮。
- 换端点即换配置身份：OCR spec 冻结 `config_sha256`，改端点后不能 `--reuse-run` 续跑，须新建 run；同一身份最多初审加一次补审。
- **判一轮 OCR 实际走的是哪条路由，读 run 的 `state.json` 里 `batches[].execution_route`（`provider`/`model`/`endpoint`/`reason`），不要拿 `spec.json` 的 `model`/`provider` 当实际路由**：spec 是 prepare 时冻结的意图；`reason` 会写明判定原因（当前策略下形如"本地无等待请求"或"本地有等待请求或负载未知"），不会出现时间窗或流水线的措辞。
- **事后追溯一轮 OCR 走的是本地还是云端、跑完还是被打断，读 session jsonl 即可，不必重跑**：`~/.opencodereview/sessions/<项目目录>/<uuid>.jsonl`（项目目录名由 cwd 编码而来，如 `home-wang-.local-state-rosbag-scene-wt-04a-t12sf`），每个文件一轮：首行 `session_start` 带 `cwd`/`model`/`llmSource`/`reviewMode`/`diffFrom`/`diffTo`/`gitBranch`，末行 `session_end`，中间是 `llm_request`/`llm_response`/`tool_call`/`review_item_done`。`llmSource` 形如 `provider:vllm-local`（模型为 qwen 路径）或 `provider:hermes-xcoding`（云端）——**这是判定"某天 OCR 是否已被切到云端"的直接证据**，同一项目目录下不同 uuid 可以一个是本地一个是云端，说明路由在期间被改过。中断轮次带 `llm_error.error`：`connection reset by peer` = 云端点链路被重置（与模型负载无关），`context canceled` = 撞批次硬截止。查"现在有没有 OCR 在跑"看目录内最新 jsonl 的 mtime 是否仍在前进（`stat` 隔 20 秒比对一次），停在 `session_end` 就是已收尾，别把"最后一批刚结束"报成"正在跑"。

## 验证改动真的生效

- 读回配置：`grep -nE "cloud_model|cloud_profile" ~/.hermes/config.yaml`。
- 路由本身：`ocr_model_route.py select <project>` 打印当前 provider/model/endpoint/reason；或跑 `scripts/verify_routing_decision.py`（只读）打印 `state.mode` + 实时 `running`/`waiting` + 当前判定与原因，用于"现在这个模式会走哪条路由"。
- 真实生效以用量表为准：查 `~/.hermes/state.db` 的 `session_model_usage`（按 `session_id`/`last_seen` 看 `model` 列）——出现新模型名且旧模型 `last_seen` 不再前进 = 已切；只有顶层字段变了、用量表仍出旧模型 = 还有一处没改。
- 该表的 `model` 是**每次调用实际用的模型**，是判定"还在不在跑 pro"的唯一硬证据，不要用配置或会话字段代替。

## 坑

- **不要在 OCR 审查在途时改 `~/.opencodereview/config.json`**：run 在中途会重新校验配置指纹，改动即让该轮以 `CONFIG_CHANGED_DURING_REVIEW`、`returncode=1`、`gate_decision=NOT_EVALUATED` 中止（保护机制本身正确，但白跑一轮）。改前先确认无在途 run：`pgrep -af "ocr_runtime|coordinator-ocr"`、看 run 目录 `state.json` 的 `state` 是否为运行中、以及全局锁 `~/.local/state/ocr-runtime/ocr.lock`；被打断的那轮需以新身份重提，不能续跑。
- **抢占运行槽不阻止准入，但"无法判定"必须拦**：这一对是共同的不变量——放宽应该只放宽已知就绪的情形，任何指标取不到（接口失败、字段缺失）都必须落回云端。删闸门时先逐格确认 `None` 输入的去向，再删代码。
- **`/llm auto` 保留的是 state 里的 `cloud_profile`，不是 `config.yaml` 的 `default_cloud_profile`**：取值顺序是 `state.cloud_profile` → 配置 `default_cloud_profile`。所以"恢复自动切换"之后实际落在哪家云端要看 `~/.hermes/runtime/model-routing-state.json`，可能与 PRD/文档写的默认供应商不一致；回答"切到 auto 会怎样"前先读 state。
- **放宽/收紧准入后，`auto` 白天的结果取决于队列本身**：时间窗移除后 `auto` 与 `local` 同判，白天本地队列为空即走本地。回答"切 auto 会怎样"前先读 `~/.hermes/runtime/model-routing-state.json` 确认 `mode`，再实时采一次 `waiting`；`waiting != 0` 时 auto 仍走云端，别答成"切了就用本地"。
- **`state.mode` 若是 `cloud`（人工指定云端），任何负载判断都不发生**：`auto`/`local` 才有看队列的意义。改完策略、或用户切完模式后，实际生效与否读 `session_model_usage` 的 `model` 列，不要只看 state 文件。
- **放宽负载闸门时未知指标必须仍然判忙**：把原来只看双零的闸门改成只看 waiting 时，正解是 `metrics.get('running') is None or metrics.get('waiting') != 0`，**不能**只留 `waiting != 0`——`running` 缺字段/解析失败时是 `None`，只判 waiting 会让"负载未知"落到本地，破坏"未知不放松保护"这条既有不变量（`running != 0` 对 `None` 恒为真，所以原式一直是拦住的，放宽时要显式保留这一支）。`waiting is None` 已被 `!= 0` 覆盖，无需另判；改完用翻转矩阵逐格确认 `(None, 0)` 仍是云端，并把这条不变量写进测试用例（含 `(None, 0)` 与 `(4, None)` 两格）。
- **删旧闸门时同步删掉它们的下游代码和文案**：移除流水线闸门后 `decision()` 不应再调 `pipeline_busy`、`choose_route` 不应再收 `at`/`pipeline_busy` 参数，否则 pyright 报 `Expected 2 positional arguments`；随之清掉只服务于旧闸门的 import（`datetime`/`ZoneInfo`）与测试里模拟时间窗的 `datetime(...)`/`hour` 循环，否则留下永不执行的死断言，下一个会话会照它以为时间窗还在。
- **策略变更后要主动把 `state.mode` 从 `cloud` 切回 `auto`**：用户在群里用 `切换到云端模型` 钉死过 `cloud` 时，改完闸门仍是云端（`cloud` 短路、根本不看队列），看起来像"改动没生效"。切回 `auto` 走插件自身的 `ModelRouter.set_mode('auto')`（保留 `cloud_profile`），不要重写 state 文件。此操作会改变所有已绑定角色会话的选路，执行前告知用户。
- 用户说"已经切成 X 了"时，先确认他改的是会话模型还是路由配置。只改会话字段对角色会话无效，这不是 bug 而是 hook 覆盖；解释机制并给正确改法，不要重复原结论。
- **"卡住的 worker"先查 `executions` 再动手**：`cron/executions.db` 的 `status`/`started_at`/`finished_at` 是硬证据——一条看起来跑了 40+ 分钟的 poll 可能早已 `completed`（用户贴的分析往往截在中途）。未确认前不要 kill；被跳过的后续 fire 是同一 job 的串行保护，不是堆积故障。
- "角色会话是否在烧云端"要按会话归因 token（`session_model_usage` join `sessions` 拿 `chat_id`/`title`），不要凭"哪个工具在跑"猜；具体归因流程见 `hermes-usage-cost-stats` 的云端消耗归因一节。

## 判定一个长回合是在工作还是卡死

角色会话/`cron` 回合异常变长时，按以下顺序取证，不要凭时长猜：

1. **先看它是否已结束**：`~/.hermes/cron/executions.db` 里该 job 的 `status`、`started_at`、`finished_at`；`status=completed` 说明只是长，不是卡。
2. **对比该 job 的历史时长分布**：同一 job 的历次回合时长差异很大（几分钟到 4 小时都正常），先算出正常量级再判断本次是否异常。
3. **重建它到底做了什么**：`~/.hermes/state.db` 的 `messages` 表按 `session_id` 取行，解析 `tool_calls` JSON 拿工具名与参数，按 `timestamp` 排序即得"什么时间在读哪个文件 / 跑了哪条命令"的时间线。工具名+参数足够定位耗时段落（如反复 `claim/recover` 试探、读源码、写裁决），不必读正文。
4. **归因耗时**：长回合的两个常见结构性原因是串行安全试探（租约 claim/recover 往返）和云端模型每次调用叠加的推理延迟；两者都不是死循环。
5. **同类发现按上面的取证顺序给出**，同时说明该回合的实际产出（裁决/核销/登记），不要只答"跑了 44 分钟"。
