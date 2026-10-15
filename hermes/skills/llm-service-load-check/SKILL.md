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
   - 处置权归用户：列出可停项（通常是 ocr 审查）+ 代价（不可续、要重跑），用户拍板后再 kill；不要替用户杀连接，也不要建议杀 `hermes` 网关自身的连接（含当前会话）。列 OCR 时用 `pgrep -af "opencodereview|ocr_runtime"` 而不是 `pgrep -af ocr`（真实进程名是 `opencodereview`/`ocr_runtime.py`，只匹配 `ocr` 会漏，得"无 OCR 进程"的错结论）；具体 kill 流程与 `--model` 覆盖配置的坑见 hermes-usage-cost-stats。

## 队列归因与批量高峰判读（用户反复问"当前负载"或要"总结负载让别的模型分析"时）

- **Engine 行是唯一同时带 Running/Waiting/KV/吞吐/prefix 命中的采样源**：远端日志（本部署为 `/tmp/vllm_qwen38.log`）里 `Engine 000:` 行每 10s 一条；`tail -3` 给当前水位，`tail -N` 给近几分钟趋势。metrics 的 running/waiting 是单点，趋势必须看 Engine 行序列。远端日志走 `ssh llm-server`（`~/.ssh/config` Host `llm-server`，key `~/.ssh/id_ed25519_llm_server`，User sysadmin，HostName 36.152.27.114）：`ssh -i ~/.ssh/id_ed25519_llm_server -o IdentitiesOnly=yes -o BatchMode=yes llm-server 'tail -3 /tmp/vllm_qwen38.log | grep -a "Engine 000"'`。
- **按 peer IP 归因流量**：对日志 `POST /v1/chat/completions` 行抽 ip 计数用 `grep -aoE "[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+"`（access 行形如 `(APIServer pid=N) INFO: <ip>:<port> - "POST ..."`）；不要用 awk 去匹配 `INFO: <ip>` 前缀——`INFO:` 前有 `(APIServer pid=...)` 且后跟多个空格，awk 抽不到任何 ip——`127.0.0.1` 桶 = A100 隧道转发 + **LLM 服务器本机自己的进程**两类，不能一概归给隧道：sysadmin 常在这台服务器上跑脚本直连 `localhost:8002`（如 `edit_road_boundary_batch_v3.py --recursive` 这类批处理，连跑数小时~数天，几十条 ESTAB 把 4 槽占满自排队）。两类在 vLLM access 日志和 `ss -tn dst :8002` 里 peer 都是 `127.0.0.1`，靠 peer IP 分不开。区分用**交叉核销**：先在 A100 侧 `ss -tn state established | grep -c ":8002"`——为 0 说明隧道此刻没在带 VLM 流量（A100 上也无 VLM worker），则 LLM 服务器上仍在滚动的 127.0.0.1 请求必是服务器本机进程；再在 LLM 服务器 `ps -eo pid,user,etime,cmd | grep -aiE "python|curl|vllm"` 找本机灌请求的脚本进程（按 etime 找长跑者、`ss -tn dst :8002 | grep -c ESTAB` 数其连接）。`sudo -n ss -tnp dst :8002` 需密码会直接失败，改用 ps 枚举进程兜底。本机公网 IPv4 出口与 A100 直连各自独立成桶，外部用户占比通常很小。判"被谁占满并发 slot"先做这个拆解再下结论。本机默认出口是 IPv6，对照 peer 日志必须 `curl -4 ifconfig.me` 拿 IPv4 公网出口（直连时本机显示为公网出口 IP，不是 127.0.0.1 也不是 LAN IP）。
- **批量涌入的指纹与窗口**：Engine 行 prompt 吞吐瞬时尖峰（数千~数万 tok/s）= 一批长上下文请求刚灌入，随后 1~20 分钟 Waiting 冲高（5~8）再回落；同一服务器一天可循环多次（间隔约 20~40 分钟）。批量 VLM 提交避开尖峰后 10~20 分钟，或连续采样盯到 Waiting ≤2 的安静窗口再提。归因前先确认隧道进程仍在（A100 `ps` 里有 `ssh -L 8002:localhost:8002`，否则 127.0.0.1 流量归属整个变掉）。
- **长批量任务起跑前的空闲门禁 + 硬截止失败的判定语义**：提交 OCR 逐批审查、VLM 批处理这类长任务前，先 `curl -s http://192.168.2.45:8002/metrics | grep -E 'num_requests_(running|waiting)'`。`running` 已达 `max_num_seqs`（本实例 4）且 `waiting>0` 时**不要提交**：单批有外层硬截止（OCR 为 3900s/批），排队占满后单批在窗内跑不完，终态是 `rc=1` + `coverage_complete=false` + 0 完成文件 + 0 findings（`context canceled` 只是截止时客户端取消的症状，不是模型或代码问题）。把批次拆小（如逐文件一批）只缩短单批时长，端点饱和时仍会撞截止，不能靠拆批绕过排队。这类进程层失败**不得**登记为"零问题/已通过"，也不要在同参数下立即重试（根因未消除必再撞、白烧额度）；等 `running==0 && waiting==0` 的空窗再经原定入口重提。
- **"15k+ prompt 尖峰 + MM cache 极低（<10%）"常是自己 pipeline 的 cot_llm，不是外部灌新数据**：自建 Phase3 的 `cot_llm_validate.py` 每个 range 最多带 48 张相机图（4 相机 × 12 帧），prompt 15k+ tokens；同 Bag 内 clip 图像重复时 MM cache 可达 80%+，换 Bag/换数据集则回落到 ~5%。看到这种尖峰先查 A100 上是否有 VLM 脚本 worker 在跑再归因"外部在灌新数据集"。MM cache 从 80%+ 骤降到 5~10% 同时 Waiting 台阶式爬升，才更像负载真正换成外部新数据集。
- **队列按来源分类（用户问"我的协调者/流水线各占多少"时的口径）**：vLLM 等待队列 metrics 不暴露按客户端归属，只能两侧 TCP 连接反查估算：
  1. A100 侧（两类来源，指纹相反）：
     - 数据流水线 worker：`ps -eo pid,ppid,cmd | grep -aE "cot_llm_validate|description_gen_llm|navi_gen_llm|clip_llm_comprehension" | grep -v grep`——有 worker 且数 >4（max_num_seqs）= 流水线自己占满 4 slot 并自排队，Waiting 里至少 4 个是它自己（worker 数 − 4）；`ss -tn state established | grep -c ":8002"` 估在途连接总数。`ps -eo pid,ppid,cmd` 看进程树：`coc_gen_llm.py` 主进程 → `cot_llm_validate.py` 父 → N 个 Pool worker。
     - 训练 CoC probe：`ps -eo pid,ppid,etime,cmd | grep -aE "coc_probe_watch|train_.*_ddp.*--coc-monitor" | grep -v grep`——`train_ae_joint_online_ddp.py --coc-monitor-only --coc-probe-steps`、`train_v8_coc_attn_fullkl.py`、`coc_probe_watch.py` 这类训练任务也会周期经隧道发探针打满 slot，但无 `cot_llm_validate` 进程。
  2. 本机侧：本机已永久停用 18002，改为 LAN 直连 `192.168.2.45:8002`，所以 `ss -tnp dst 192.168.2.45:8002` **无 ESTAB 属正常**（网关按需建连、空闲即断），不能用它证明“本机零占用”；要查本机是否正在灌请求看 `ss -tnp dst 192.168.2.45:8002` 同一时刻是否有连接 + 本机自身会话/进程（`ps`、`hermes cron list`）交叉。到 LLM 服务器的其它链路（:22 SSH）不算 VLM 流量。
  3. 剩余 Waiting 扣掉上面两类后归"外部用户"，汇报注明是估算（keep-alive 连接数 ≠ 在途请求数）。
- **训练 CoC probe 是又一类经隧道打满 slot 的来源，指纹与 VLM 数据流水线相反**：A100 训练任务（`coc_probe_watch.py`、`train_ae_joint_online_ddp.py --coc-monitor-only --coc-probe-steps`、`train_v8_coc_attn_fullkl.py`）会周期性经 A100 8002 隧道向 LLM 发 CoC 探针请求。它不打图片、不做 cot_llm 数据流水线，所以指纹是 **prefix cache hit rate 高（>85%）+ MM cache 中高（实测 60%→80%+，随 probe 复用同一批输入缓慢爬升，不是固定档位，别拿"~60%"当判据）+ 生成吞吐被压到个位数~百**——同一 prompt 模板反复复用（prefix 命中高），不是换新数据集（那会 MM/prefix 同步骤降）。看到高 prefix + waiting=capacity + running=4 时，先 `ps` 查 A100 训练进程的 `--coc-monitor`/`--coc-probe-steps` 再归因；不要把高 prefix 命中误读成"轻负载"，也不要因为 MM cache 高就判成"数据流水线"——两者在此实例都表现为高 MM，区分靠**有没有 `cot_llm_validate` 类 worker 进程**，而不是靠 MM 数值。
- **MM cache 命中率是负载构成的指纹，要连同 prefix cache 一起读**：Engine 行的 `MM cache hit rate` 高（80%+）= 当前批次在**同一组图像**上反复送请求（数据流水线型，如 description/cot_llm 按 clip 送相机图，缓存复用、单请求快）；MM 骤降（80%→28%→5% 这类台阶式下跌）+ prefix 同步走低 = **换成了全新数据集在灌**，每请求图像编码/attention 全量重算、单请求占 slot 时间变长，排队更难消化。汇报趋势时点明"当前批次是同数据重复还是新数据集"，这是判断高峰还会持续多久的最强信号。
- **持续高峰形态（别套用 20 分钟回落预期）**：新数据集持续分批灌入时，prompt 尖峰每 20~40 分钟再现一次（15k+ 级），Waiting 台阶式爬升（7→8→11）且可维持 2 小时以上，生成吞吐被压到个位数~百。此时安静窗口在小时级而非分钟级；只有 Waiting 连续两次采样回落（且 MM/prefix 停止下台阶）才判"快收尾"，否则按"继续恶化/维持"汇报并建议避开晚间高峰，不要报"再等 10~20 分钟"。同理**不要拿相邻两次采样的差值线性外推给"约 N 小时后归零"的时间窗**——到达率 ≥ 消化率时队列停在平台并来回震荡，单点差值是噪声；只有连续两次以上采样同向回落才给时间窗，否则只报方向（升/平/降）。
- **"平台"是有力的诊断结论，也靠计数而非感觉**：同一水位区间（本环境 waiting 10~16）连续 3 次以上采样都落在同一带内来回抖动、running 恒等于 `max_num_seqs`，就是**稳态平台**，不是排队在缓解。此时汇报口径：明确写"已连续 N 分钟钉在该区间、running 恒满 4"，给方向（平）不给归零时间窗，并直说"只要上游不停，平台就维持"。不要因为某一次采样掉到带内低点（如 16→10）就改口说"在缓降、约 N 小时归零"——下一条采样回到 13 就自我推翻，用户会跟着你的错判做决策。
- **该生产 vLLM 实例 `max_num_seqs=4` 已实锤**：Running 恒定 4、Waiting 全部 `reason=capacity`、忙时 KV cache 仍 <20%——排队是纯 slot 问题不是显存，调优方向是调大 max_num_seqs（属服务器管理员权限，只报事实不擅改）。
- **负载总结给外部分析时的固定产物**：采样时间线表（Running/Waiting/生成吞吐/KV/prefix 命中率）+ 客户端归属表（含各桶归属说明与"127.0.0.1 桶无法细分"的局限声明）+ 尖峰事件清单（时间 + prompt 吞吐值）+ 数据局限（10s 采样粒度、瞬时值、请求级 latency 分位缺失）。用户要转给强模型时给全局限，不只给当前水位。

## 坑

- 延迟探针必须用小 `max_tokens`（≤40）：生成 token 数直接拉长占槽时间，大探针会恶化正在诊断的拥塞。
- `kv_cache_usage_perc` 很低但 `waiting` 数 >0（reason=capacity）时，瓶颈是 vLLM `max_num_seqs` 并发槽，不是显存；建议方向是调大 `max_num_seqs`（8 卡 80G 跑 27B fp8 有余量），而不是加卡/加内存。
- HTTP推理已切LAN直连；远端SSH操作另外核验凭据。A100本机GPU状态不证明当前HTTP端点负载，负载以直连metrics与实际服务日志为准。
- 区分LAN不可达、客户端超时、推理错误和模型排队；低KV与waiting=0不能单独证明长请求正常。
- 验证视觉能力别造纯色块当样本：自造 1x1/64x64 色块模型也能答对（答颜色），证明不了"理解真实场景"；抽真实业务帧（如 rosbag2 db3 的 CompressedImage）再送，判定才有意义。
- **查"当前队列负载"先走 LAN 直连（192.168.2.45:8002，步骤 1-2）**；A100 两跳隧道（A100 8002 → LLM 服务器）是同一实例的备用路（经 `/v1/models` 返回的模型路径/实例确认同源）。两条都通时优先 LAN（少一层 SSH 瞬断）；A100 隧道偶发 `kex_exchange_identification: Connection closed by remote host`（sshd MaxStartups 限流）时 sleep 10~15s 重试一次即可，不要因此换路径重查。
- **用户连问多次"当前负载"时的汇报口径**：每次实时采样，除瞬时水位外给趋势方向（较上次升/平/降）+ "现在提交单请求大约等多久"（沿用最近一次实测值并注明）；只报单点数字是漏答。时间窗只在连续两次以上采样同向回落时才给，平台态一律不给归零预测。
- **确诊拥塞后不要每次追问都重发探针**：waiting 两位数时探针自己排队，既拖长测量又给已被诊断的服务加压；直接读 metrics / Engine 行给水位+趋势，并沿用上一次实测的"单请求大约等 N 秒"（汇报时注明是沿用前次实测、非本次新测）。只在负载明显回落、或用户明确要看当前真实延迟时才重测一次。
- 长时客户端（如 ocr review 跑 20 分钟以上）疑似卡死时，三件套区分"服务慢"和"客户端挂死"：`ps -o time -p <pid>` 看 CPU 累计时间是否还在涨、`/metrics` 的 running/waiting、小探针墙钟。三者显示服务空闲而客户端 CPU 时间冻结 = 客户端连接挂死（非拥塞），kill 客户端进程重跑同一任务即可，不要归因成"模型慢"继续等；重跑前先确认没有其他排队者会插队。
- 长请求cancelled时结合客户端超时、真实请求生命周期和服务端日志取证，不能仅凭低KV或无error排除所有服务原因。旧18002路径已由用户要求永久停用；不得再用临时切换＋finally恢复旧配置的方式复审。更换链路不等于审查完成，仍核验session及完整覆盖。
- **metric 名要按实测全名 grep，不要凭印象缩写**：该实例实际导出的是 `vllm:num_requests_running` / `vllm:num_requests_waiting`（含 `_by_reason{reason="capacity"}`）/ `vllm:kv_cache_usage_perc` / `vllm:num_preemptions_total`，以及 `vllm:time_to_first_token_seconds_{sum,count}`、`vllm:e2e_request_latency_seconds_{sum,count}`、`vllm:request_queue_time_seconds_{sum,count}`。写查询时直接 `grep -E "num_requests|kv_cache|preemptions"` 列全名，拍错一个词会静默返空，被误读成"指标不存在/服务异常"。终身均值一律用 `_sum/_count` 现算（本次实测 TTFT 7.7s / E2E 19.8s / 队列 6.1s，样本 20 万+），别把单次探针值当基线。
- **具名归因（A100 训练 probe / 数据流水线 / 外部用户）必须拿到两侧正向证据才能出口**：指纹吻合（高 prefix 命中、MM cache 数值、Waiting 台阶）只能提示"往哪查"，不能充当证据。下结论前在具名机器上确认"进程真的在跑"或"连接真的指向 :8002"——本环境实测过这样的自我推翻：据"高 prefix + capacity 排队"判定是 A100 训练 CoC probe，交叉核销时发现 A100 上 0 个 VLM worker、0 条到 :8002 的连接，归因站不住。**已报出口的归因被推翻时，下一轮必须明确撤回**（写明"上一条归因未确认，撤回"），再给带证据的版本，不要让它以默认事实的方式留在对话里被用户拿去汇报。
- **不要把"不知道是谁"包装成"大概是 X"**：无法拆分的部分（如 127.0.0.1 桶里隧道转发与本机服务器进程混在一起、且两侧都查不到进程）就明确报"无法归属"并给已排除项，比一个听起来很具体的错杀更安全。
- **跨多次采样对比前先确认网关没被重启**：`hermes-gateway` 会因 systemd 触发 SIGTERM 重启（`systemctl --user show hermes-gateway -p ActiveEnterTimestamp` 看当前实例启动时刻，`journalctl --user -u hermes-gateway | grep -i 'signal=SIGTERM'` 看原因）。重启后网关主 PID 与所有子进程 PID 全部换新，拿旧采样的 PID 去 `ps` 会得到空结果，极易被误读成"进程已退出/任务已收尾"，实际只是换了 PID；汇报前先核当前实例的启动时间，再解释 PID 变化。

## 结论模板

回复给出四件套：当前 running/waiting 数、探针延迟 vs 终身均值、排队原因（capacity/链路断）、下游影响（审批超时/流中断）。用户授权时再给修复项（`auxiliary.approval.timeout` 调大、`max_num_seqs` 调大、后台任务限速）。

- **报负载时同时给准入判定**：用户反复问"当前负载"的真实意图往往是"我的会话现在会走本地还是云端"。除水位与趋势外一并给当前选路结论（route + reason，见 `hermes-model-routing-ops` 的 `scripts/verify_routing_decision.py`），否则用户拿到一个孤立数字还得再问一轮。
- **归零要报起始时刻与持续时长，不只报"现在为 0"**：`grep -a "Engine 000" <远端日志> | tail -N` 里找最后一次非零到零的交界，回答"空多久了"。队列从平台直接跳到双零是常见收尾形态（不是渐进下降），所以"不给归零时间窗"的规则与"归零后确认已持续多久"并不矛盾。
- **队列转空后仍要说明谁在拦准入**：vLLM 空闲不等于会话会走本地（策略还看 `mode` 是否被钉在 cloud）。回答"不忙了为什么还走云端"前先读 `model-routing-state.json` 与当前判定，不要把"队列空"答成"应该用本地了"。
