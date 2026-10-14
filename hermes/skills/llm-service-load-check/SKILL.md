---
name: llm-service-load-check
description: 诊断千问vLLM服务负载：通过已配置的LAN直连端点查队列、排队原因与延迟。
---

# 本机千问 vLLM 负载诊断

## When to Use

- 用户问“为什么很多会话没回复/变慢”，需判断是否模型服务排队。
- 要给千问推理服务（LAN直连8002）负载、队列、延迟出一份诊断结论。
- 网关日志出现 auxiliary approval 超时，需定位上游原因。

当前开发机直连 `http://192.168.2.45:8002/v1`，模型为qwen3.8-27b-fp8，不再使用18002 SSH隧道。HTTP与metrics均走LAN；SSH运维仍遵守既有授权和凭据边界。迁移证据见 `/home/wang/work/doc/analysis/hermes-qwen-load-20261012/DIRECT-CONNECTION-20261013.md`。

## 步骤

1. **确认直连可达**：`curl --noproxy '*' -sS -m 8 http://192.168.2.45:8002/v1/models`。200且返回model id只证明可达；失败查LAN路由、端口与服务，不能自动恢复旧隧道。
2. **拉取指标**：`curl -s http://192.168.2.45:8002/metrics`，只看这几个关键 gauge：
   - `vllm:num_requests_running` / `vllm:num_requests_waiting`（含 `_by_reason`，reason=capacity 即并发槽满）
   - `vllm:kv_cache_usage_perc`（显存缓存占用）
   - `vllm:num_preemptions_total`（>0 说明发生过抢占，服务在饱和）
3. **延迟探针**：发一个极小请求测墙钟时间：
   ```bash
   curl -s -m 60 http://192.168.2.45:8002/v1/chat/completions -H 'Content-Type: application/json' \
     -d '{"model":"<model id>","messages":[{"role":"user","content":"用一句话回答：1+1=?"}],"max_tokens":40,"temperature":0}'
   ```
   探针本身排队时耗时会包含前面请求的时间，结论要结合 running/waiting 数解读。
   视觉/图像请求验证（判断"模型到底能不能看图"，不是测负载）时同端点另走一条口径：图像进 prompt 后 prompt_tokens 明显上涨（数百→上千）但 `content=null` + `finish_reason=length` = 思考 token 吃光预算（Qwen3 系思考模型，`max_tokens` ≤256 常见），加大到 ~2048 或 payload 加 `"chat_template_kwargs":{"enable_thinking":false}`（关思考后 512 足够）再判。**纯文本探针绿不能证明视觉可用**，必须发真实图像（data:image/jpeg;base64，OpenAI image_url 形态）；模型对 `<image>` 文本 token 会自述"看不到图"，不代表端点无视觉能力，只是输入形态不对。`/v1/models` 返回的模型 ID 常是完整文件系统路径，短别名（如 `qwen-vl`）会 404，调用必须用返回的原文 ID。
4. **基线对比**：从 metrics 算终身均值（`_sum/_count`）：`time_to_first_token_seconds`、`e2e_request_latency_seconds`、`request_queue_time_seconds`；分位数读 `e2e_request_latency_seconds_bucket`（看 >60s 的请求占比）。探针值远超均值 = 当前在排队，均值异常 = 服务长期变慢。
5. **服务空闲但会话仍慢 → 查 prefill/上下文体积**（步骤 1-4 全绿时）：会话慢最常见的非排队原因是 prompt 过长——每轮全量 prefill，耗时线性。发一条中等大小探针（约 6-10 万 token 的重复文本，`max_tokens` ≤8）测出 prefill 速率（本机基线约 4,400 tok/s），再按会话实际 context token 数（Hermes gateway status 输出的 Context 行即有）外推首 token 延迟；外推值与用户体感吻合即坐实。汇报时点明：`max_model_len`（如 1M，本机 2026-10-13 实测 `/v1/models` 返回 1010000）是容量上限不是性能指标，context 占 19% 也可能每轮等 40s+。对策按效果排序：`/new` 开新会话（立竿见影）、清理超长工具结果所在的历史会话。
6. **网关侧关联**（用户报"会话不回复"时）：`journalctl --user -u hermes-gateway --since '2h ago' | grep -iE 'auxiliary|timeout'`。若出现 `Auxiliary approval ... timed out after 30.0s`，说明智能审批的辅助模型调用在排队中超 30s 超时，审批升级挂起，会话表现为卡住——这是模型服务排队最常见的下游症状。
7. **按客户端归属在途请求**（用户问"每个请求是谁的、想停掉一些"时）：vLLM metrics 是请求级匿名的，无法直接枚举"第 N 个 running 请求 = 哪个会话"；用两侧 TCP 连接反查客户端进程做归因（近似，keep-alive 连接数与 running 数可能有偏差，汇报时注明）：
   - dev机：`ss -tnp dst 192.168.2.45:8002`查看实际客户端PID，结合命令/cwd及会话记录归属。连接数不等于请求数；OCR能否续审以实际session与工具版本为准。
   - 服务端：仅通过已授权SSH入口读取实际监听与模型日志；本机HTTP可达不代表SSH凭据可用，不能凭旧拓扑推断流量来源。
   - 处置权归用户：列出可停项（通常是 ocr 审查）+ 代价（不可续、要重跑），用户拍板后再 kill；不要替用户杀连接，也不要建议杀 `hermes` 网关自身的连接（含当前会话）。

## 队列归因与批量高峰判读（用户反复问"当前负载"或要"总结负载让别的模型分析"时）

- **Engine 行是唯一同时带 Running/Waiting/KV/吞吐/prefix 命中的采样源**：远端日志（本部署为 `/tmp/vllm_qwen38.log`）里 `Engine 000:` 行每 10s 一条；`tail -3` 给当前水位，`tail -N` 给近几分钟趋势。metrics 的 running/waiting 是单点，趋势必须看 Engine 行序列。
- **按 peer IP 归因流量**：对日志 `POST /v1/chat/completions` 行用 awk 抽 `INFO: <ip>:<port>` 的 ip 计数——`127.0.0.1` 是 A100 隧道转发（含本机经 A100 两段隧道的流量，通常占绝对大头且**无法在 LLM 侧再细分**），本机公网 IPv4 出口与 A100 直连各自独立成桶，外部用户占比通常很小。判"被谁占满并发 slot"先做这个拆解再下结论。本机默认出口是 IPv6，对照 peer 日志必须 `curl -4 ifconfig.me` 拿 IPv4 公网出口（直连时本机显示为公网出口 IP，不是 127.0.0.1 也不是 LAN IP）。
- **批量涌入的指纹与窗口**：Engine 行 prompt 吞吐瞬时尖峰（数千~数万 tok/s）= 一批长上下文请求刚灌入，随后 1~20 分钟 Waiting 冲高（5~8）再回落；同一服务器一天可循环多次（间隔约 20~40 分钟）。批量 VLM 提交避开尖峰后 10~20 分钟，或连续采样盯到 Waiting ≤2 的安静窗口再提。归因前先确认隧道进程仍在（A100 `ps` 里有 `ssh -L 8002:localhost:8002`，否则 127.0.0.1 流量归属整个变掉）。
- **该生产 vLLM 实例 `max_num_seqs=4` 已实锤**：Running 恒定 4、Waiting 全部 `reason=capacity`、忙时 KV cache 仍 <20%——排队是纯 slot 问题不是显存，调优方向是调大 max_num_seqs（属服务器管理员权限，只报事实不擅改）。
- **负载总结给外部分析时的固定产物**：采样时间线表（Running/Waiting/生成吞吐/KV/prefix 命中率）+ 客户端归属表（含各桶归属说明与"127.0.0.1 桶无法细分"的局限声明）+ 尖峰事件清单（时间 + prompt 吞吐值）+ 数据局限（10s 采样粒度、瞬时值、请求级 latency 分位缺失）。用户要转给强模型时给全局限，不只给当前水位。

## 坑

- 延迟探针必须用小 `max_tokens`（≤40）：生成 token 数直接拉长占槽时间，大探针会恶化正在诊断的拥塞。
- `kv_cache_usage_perc` 很低但 `waiting` 数 >0（reason=capacity）时，瓶颈是 vLLM `max_num_seqs` 并发槽，不是显存；建议方向是调大 `max_num_seqs`（8 卡 80G 跑 27B fp8 有余量），而不是加卡/加内存。
- HTTP推理已切LAN直连；远端SSH操作另外核验凭据。A100本机GPU状态不证明当前HTTP端点负载，负载以直连metrics与实际服务日志为准。
- 区分LAN不可达、客户端超时、推理错误和模型排队；低KV与waiting=0不能单独证明长请求正常。
- 验证视觉能力别造纯色块当样本：自造 1x1/64x64 色块模型也能答对（答颜色），证明不了"理解真实场景"；抽真实业务帧（如 rosbag2 db3 的 CompressedImage）再送，判定才有意义。
- **查"当前队列负载"先走 LAN 直连（192.168.2.45:8002，步骤 1-2）**；A100 两跳隧道（A100 8002 → LLM 服务器）是同一实例的备用路（经 `/v1/models` 返回的模型路径/实例确认同源）。两条都通时优先 LAN（少一层 SSH 瞬断）；A100 隧道偶发 `kex_exchange_identification: Connection closed by remote host`（sshd MaxStartups 限流）时 sleep 10~15s 重试一次即可，不要因此换路径重查。
- **用户连问多次"当前负载"时的汇报口径**：每次实时采样，除瞬时水位外给趋势方向（较上次升/平/降）+ "现在提交单请求大约等多久" + 预计回到安静档的时间窗；只报单点数字是漏答。
- 长时客户端（如 ocr review 跑 20 分钟以上）疑似卡死时，三件套区分"服务慢"和"客户端挂死"：`ps -o time -p <pid>` 看 CPU 累计时间是否还在涨、`/metrics` 的 running/waiting、小探针墙钟。三者显示服务空闲而客户端 CPU 时间冻结 = 客户端连接挂死（非拥塞），kill 客户端进程重跑同一任务即可，不要归因成"模型慢"继续等；重跑前先确认没有其他排队者会插队。
- 长请求cancelled时结合客户端超时、真实请求生命周期和服务端日志取证，不能仅凭低KV或无error排除所有服务原因。旧18002路径已由用户要求永久停用；不得再用临时切换＋finally恢复旧配置的方式复审。更换链路不等于审查完成，仍核验session及完整覆盖。

## 结论模板

回复给出四件套：当前 running/waiting 数、探针延迟 vs 终身均值、排队原因（capacity/链路断）、下游影响（审批超时/流中断）。用户授权时再给修复项（`auxiliary.approval.timeout` 调大、`max_num_seqs` 调大、后台任务限速）。
