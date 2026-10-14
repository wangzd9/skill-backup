---
name: rosbag-catalog-status-check
description: "rosbag-catalog 流水线状态按需巡检：只读查健康、状态分布与失败新旧。"
---

# rosbag-catalog 流水线状态巡检（按需只读）

## When to Use

用户要求“检查 rosbag-catalog 当前流水线状态 / 队列有没有在跑 / 有没有新增失败”时。只读汇报当前整体状态，不做重试决策；定时监督和失败重试走 `rosbag-catalog-supervisor`。

与 `rosbag-catalog-supervisor`（定时监督 + 失败重试决策）分工：本 Skill 只回答“当前整体状态如何”，全程只读，不执行任何重试、取消或写操作。

## 步骤

1. **确认目标环境和健康**：生产 Backend 默认 `http://192.168.2.134:8180`；本地测试栈 Backend 为 `127.0.0.1:18080`（Web `8181`）。`GET /health/live` 和 `GET /health/ready`；ready 响应含 `environment`（production/test），先确认查的是哪一边再下结论。
2. **整体链路服务检查（用户问“服务是否都正常”时必做，只读）**：`ssh -o BatchMode=yes processing-machine 'docker ps --format "{{.Names}}\t{{.Status}}"; systemctl is-active rosbag-a100-proxy.service rosbag-phase0-proxy.service rosbag-catalog-mysql-backup.timer'`。五个应用容器（frontend/backend/scanner/orchestrator/mysql）应全部 Up，三个 systemd unit 应 active。宿主机代理健康在 `http://127.0.0.1:8765/health`（Phase0）和 `http://127.0.0.1:8766/health`（A100）——只在处理机本机可访问，`/health` 响应本身含 `active_runs`，无需另找端点；端口猜错（如 8187/8188）只会得到空响应或 404，别据此下结论。
3. **快速失败摘要（可选）**：`python3 /home/wang/.hermes/scripts/rosbag_catalog_monitor.py`。它只返回 FAILED 列表且默认指向生产 Backend；查本地栈必须先 `export ROSBAG_CATALOG_BACKEND_URL=http://127.0.0.1:18080`，否则拿到的不是目标环境的数据。
4. **全量状态分布**：`GET /api/v1/pipeline-runs?page=N&page_size=100`（可加 `status=FAILED` 等过滤），翻页直到返回空页；按 status 和 queue_name 聚合计数。响应是裸 JSON 数组（无 items 包裹层），主键字段是 `pipeline_run_id`（不是 `id`）；列表项还含 source_id、priority、error_message、pipeline_type，可直接用于 FAILED 错误分类计数。终态集合是 `PUBLISHED / FAILED / CANCELLED`，其余一律视为在途/中间态（含 `PHASE1_DONE`、`NVIDIA_DONE` 这类可长期停留的中间状态）。
5. **区分新增异常与历史失败**：列表项不带时间戳（`updated_at`/`created_at` 均为 null），判断新旧必须拉 `GET /api/v1/pipeline-runs/{id}` 详情取 `events[].created_at` 的最大值。按 error_message 分组后每类抽 1-3 条即可定性（历史失败批次往往同类、同日期、高 attempt 重复），不要把全部 FAILED 逐条拉详情——75 条全查浪费数分钟。事件时间与当前日期无关的批次按历史数据汇报，不得列为新故障；只把近期事件时间的失败作为“新增异常”。
6. **中间态复核**：非终态记录先读详情事件和 artifact 再定性；事件里带测试资源键（如 `P0_GATE_TEST_*`）或 run id 是命名测试记录（如 `*-test`）的，按测试数据汇报，不当作业务在途。

## 汇报格式

- 中文：服务健康（Backend environment + 容器/systemd 代理/备份 timer 表）→ 状态分布表 → 活跃任务数与明细 → FAILED 错误分类计数（标注按事件时间的新旧）→ 结论（有无新增异常、队列是否空闲、唯一待处理事项）。
- 不重放全部 FAILED 明细；错误分类计数 + 代表性 run id 即可。

## 失败根因下钻（用户追问“为什么失败”时）

1. Backend `error_message` 常只是 `returned non-zero exit status`，不透明。真实结论在 A100 侧运行状态文件（只读 cat，不修改），分两条路线：自建 Bag 任务在 `/home/xmyf/wzd/runs/<pipeline_run_id>/phase3_run.json`（Phase3/QUALITY_CHECK）或同目录 `phase1_run.json`、`phase1.log`；NVIDIA chunk 任务在 `/data/nvidia_pipeline_runs/<pipeline_run_id>/`（`run.json` 含 attempt/step/status/error，`steps/*.json` 逐步状态，`logs/<step>.log` 该步真实日志含实际命令行）。JSON 的 `error` 字段是质量门禁最终结论，`steps` 表明卡在哪一步。NVIDIA 步骤链为 PRECHECK→DOWNLOAD→VERIFY_DOWNLOAD→EXTRACT→PHASE1→NAVIGATION→DESCRIPTION→FINAL_VALIDATING，其中 DESCRIPTION 最慢（VLM 并发 1）；`--resume` 重试时已完成步骤幂等复用，DOWNLOAD 断点续传、已下 zip 保留不删，EXTRACT 就地解压。
2. `PHASE3_QUALITY_FAILED`、`INPUT_INVALID` 等输入质量类失败是确定性的：validate 按 clip 顺序只报第一个命中的缺陷，对同一 A100 输入重复重试只会换出下一个 clip 的缺陷并白耗 VLM 算力。看到高 attempt 的重复质量失败（如 `no navigation text`、`no valid CoC rows`），明确报告“停止重试”，建议数据侧修复或换 Bag。
   - **例外：NVIDIA DESCRIPTION 的 `QUALITY_DESCRIPTION_FAILED:<clip>:missing or empty description` 可能是 VLM 单 clip 坏输出而非输入缺陷**。`clip_llm_comprehension.py` 对单 clip 报错曾退出码 0（日志 `ok=99 err=1` 但步骤 SUCCEEDED），resume 永不重算，retry 反复撞同一堵墙（2026-09-28 生产 chunk31 `4fb4e1dc` 实证）。诊断：description.log 有 `err=1` + 具体 clip 的 `JSON: ...` 错误行，且该 clip 目录缺 `clip_discription_llm.txt`（正常 clip 有）。脚本仓库 `95f4ff6` 起该脚本与 `navi_gen_llm.py` 已加 clip 级幂等（已有非空描述/LLM 列即 skip）且 err>0 非零退出，新 run 普通 retry 即可。旧 run 撞墙时（用户授权后）在 A100 单 clip 补产物：`python clip_llm_comprehension.py --chunks N --clips <坏clip> --base-dir /data/nvidia_infer_dataset --parallel 1 --intra-threads 1`，坏输出是间歇性的可多试几次，补完普通 retry 秒级过门禁收尾。
2b. **区分两种质量失败：输入缺陷（确定性）vs VLM 单 clip 输出损坏（间歇性）**。第 2 条的“停止重试”只适用于输入数据本身有缺陷的失败。`QUALITY_DESCRIPTION_FAILED:<clip>:missing or empty description` 且 `description.log` 显示 `ok=N-1 err=1`（报错形如 `JSON: Expecting value: ... | raw=\`\`\`json` 围栏包裹/截断）属于**另一类**：VLM 对该 clip 间歇性返回非法 JSON。识别特征：
   - `clip_llm_comprehension.py` 容忍单 clip 报错仍 exit 0 → DESCRIPTION 步骤被标 `SUCCEEDED` → 后续普通 retry 的 `--resume` 跳过全部步骤，3 分钟左右生成**与上次一字不差**的 `quality_report.json` 再败（秒败特征：各 `steps/*.json` mtime 早于本 attempt、launcher.log 为空、quality_report.json 是本 attempt 新写）——这种 retry 是空转，不要建议用户反复发起。
   - 修复路径（需用户授权，A100 生产 infer 目录写操作）：A100 上单 clip 重跑 `/home/xmyf/miniconda3/envs/alpamayo_env/bin/python /home/xmyf/wzd/repos/nvidia/phase2/clip_llm_comprehension.py --chunks <N> --clips <clip_id> --base-dir /data/nvidia_infer_dataset --parallel 1 --intra-threads 1`（约 1 分钟）；失败可再试，坏输出是间歇的，不要判定为确定性。成功后**全 chunk 扫描**确认 100/100 clip 都有非空 `clip_discription_llm.txt`（quality_validator 按 clip rglob 该文件判缺失/空白），再让用户在界面 retry——resume 跳步后只重跑质量门禁即可收尾。
   - 治本缺口：comprehension 脚本 err>0 应非零退出，属 A100 脚本仓库后续改进，救活 run 时不要顺手改生产脚本。
3. 步骤全绿不等于内容合格：nav 脚本对 0 帧匹配也返回 exit 0（日志 `navigated 0/N`），以 validate 结论和日志 0/N 为准。
4. “最近失败的 run”定位法：列表 API 无时间戳，逐条拉详情取 `events[].created_at` 最大值排序；再核对 attempt 数判断是否重复重试。
4b. `NVIDIA_ORIGIN_VERIFY_FAILED: manifest_hash` 与 `NVIDIA_ORIGIN_PUBLISH_CONFLICT` 是两类不同失败，处置相反：CONFLICT 是目标目录已存在（清旧备份后 retry 即可）；VERIFY_FAILED 是发布内容校验不过——A100 源端与下载时 manifest 快照一致的前提下，它指向**传输/CIFS 落地的瞬态损坏**，不是数据问题也不是校验误杀，诊断三步（源端逐文件复核→本地盘复现传输→CIFS 复现落地）与修复口径（代码已含校验失败自动重传一次）见 `rosbag-catalog-local-verify` 技能的 real-mode-e2e 参考文件。看到该错误不要建议放宽校验。
5. 平台代码变更后重试仍报旧错误，先查版本错位：流水线脚本由宿主机 systemd 代理进程（`rosbag-a100-proxy.service`/`rosbag-phase0-proxy.service`）执行，部署脚本只重建容器、不重启代理；代理进程启动时间早于部署时间即仍在跑旧代码。核对：`ssh processing-machine 'systemctl show -p MainPID --value <unit>'` 后 `ps -o lstart= -p <pid>` 对照部署时间。修复需用户授权：`systemctl restart` 要交互 sudo 时用 `kill <MainPID>`（服务以同用户运行且 `Restart=always` 自动拉起），等 `active` 后核对 `/health` 和 `active_runs=[]`。
   - **代理环境变量残留也会跨 run 污染生产**：`NVIDIA_TEST_CLIPS` 这类测试开关若遗留在处理机 `/etc/default/rosbag-a100-proxy`，会把所有 NVIDIA 生产 run 钉死到单个测试 clip。症状：某 chunk 的 run 在 PHASE1 报 `Egomotion not found: .../<clip>.egomotion.parquet`，而该 clip 其实属于别的 chunk。排查：`ssh processing-machine 'tr "\0" "\n" </proc/$(systemctl show -p MainPID --value rosbag-a100-proxy.service)/environ | grep -iE clips'` 看代理实际带的变量；再 A100 侧 `find /data/nvidia_origin_dataset -name "*<clip>*"` 确认 clip 真实归属。修复：用户 sudo 删 `/etc/default` 残留行 + kill 代理 MainPID 重启 + 普通重试（DOWNLOAD/EXTRACT 幂等跳过，PHASE1 以 `--clips all` 重跑）。平台侧已有 `resolve_nvidia_test_clips` 生产强制归一，需部署处理机才在代理进程生效。
6a. **平台 FAILED 不一定是任务失败——先核对 A100 侧活性**：错误为 `NVIDIA_PIPELINE_FAILED: launch returned without PID or state`、或事件链是 `TASK_LEASE_EXPIRED → STEP_FAILED`（心跳中断）时，先 `ssh a100-wzd` 看 `run.json` 是否仍 RUNNING、launcher 与步骤进程（`pgrep -f` 对应脚本名）是否活着、产物目录是否还在增长。处理机开着 unattended-upgrades，装系统补丁时重载无线网卡驱动会断网 2 分钟以上，超过 2 分钟心跳 TTL，Backend 对账就会把在途任务误判 FAILED。误判的 run 不要立即重试（会起第二个代理线程与 A100 活任务并发）；等 A100 侧到 DONE → kill 重启代理（清掉持旧租约的死线程）→ attempt N+1 普通重试，resume 幂等跳过全部已完成步骤只收尾。判断代理是否真断过心跳：对照事件时间窗查处理机 `journalctl --since --until` 过滤 unattended/内核网卡日志。
   - **部署重启 a100 代理也会误杀在途 run（与 unattended-upgrades 同机制，另一独立触发源）**：生产部署若伴随容器整套重建，a100 代理进程重启即丢失对在途 run 的租约跟踪与心跳，Backend 对账在部署后数分钟内把 run 判 FAILED，且平台 `error_message` 常为**空**——"FAILED 且无错误信息"本身就该触发本条活性核对（对照 deploy 目录文件 mtime / 容器创建时间）。误杀 run 不要立即重试：A100 侧任务仍在原进程树上推进，处置同 6a 主句（等 DONE → kill 代理 → 普通重试 resume 收尾）。
活性判读细节：主进程 `/proc/<pid>/wchan` 为 `do_wait` 是正常等子进程，不是卡死；DESCRIPTION（VLM）阶段 GPU 利用率可能为 0，不能据此判死，用产物目录新增文件计数（`find <infer_root> -newermt <心跳停止时刻> | wc -l`）确认是否还在产出。
   - **A100 状态字段陷阱**：`run.json` 的字段是 `chunk_id`/`pipeline_run_id`（不是 `chunk`），解析错键会全打出 None；`steps/*.json` 才有逐步 attempt。DESCRIPTION 步骤先跑 `navi_gen_llm.py`（LLM 导航）再跑 `clip_llm_comprehension.py`（场景描述），**导航子阶段期间 `clip_discription_llm.txt` 计数为 0 属正常**，不能据此判任务已死——以 run.json 状态 + launcher/子进程存活（`ps --ppid <launcher_pid>`）为准。
   - **自建 Phase3 的 description 产物文件名是 `clip_description.txt`（正确拼写），别拿 NVIDIA 的 `clip_discription_llm.txt`（拼错 discription）去 `find`**——找错名得 0 会误判“描述 0 进度、任务死了”。自建 Phase3 真实进度看 `/home/xmyf/wzd/runs/<id>/phase3_run.json` 的 `steps`（nav/cot/plot/cot_llm/description/validate 各自 DONE/RUNNING/PENDING；steps 值混合 str 与 dict，解析一律 `isinstance(v, dict)` 守卫，直接 `.get` 会 AttributeError）+ `clip_description.txt` 计数 vs clip 总数；描述跑 `description_gen_llm.py --parallel 4`（并发 4，比 NVIDIA 的并发 1 快很多，几分钟内可消化上百 clip）。
   - **A100 的 VLM 端口 8002 是 SSH 隧道到远端 LLM 服务器，不是本地服务**：`ss -tln` 上的 8002 监听属于 `ssh -L 8002:localhost:8002 <llm-server>` 隧道进程；vllm 进程、`/metrics` 和日志（如 `/tmp/vllm_qwen38.log`）都在远端 LLM 服务器上，A100 本机 `nvidia-smi` 全 0 是常态。DESCRIPTION 疑似卡死时先经隧道直测：A100 上 `curl http://localhost:8002/v1/models` 几秒内返回即服务健康；再两跳（A100 Python 内 `subprocess.run(["ssh", "-i", <隧道密钥>, "sysadmin@<llm-server>", ...])` 传 heredoc）看 `vllm:num_requests_running/waiting` 和日志尾部是否还有 `POST /v1/chat/completions 200 OK` 滚动。请求全在排队滚动而单 clip 长时间无产物，往往是该 clip 长上下文生成慢，不是卡死——等 `description.log` 的 `[done] chunks=...` 汇总行（并发 1 时每段跑完才打印一次）。队列来源归因：在 LLM 服务器按 peer IP 统计 `/tmp/vllm_qwen38.log` 的 POST 计数——`127.0.0.1` 是 A100 隧道转发（含本机经 A100 两段隧道的流量，占绝对大头），本机 IPv4 公网出口与 A100 也各自直连；外部用户占比很小，判"被谁占满并发 slot"先做这个拆解。本机默认出口是 IPv6，归因必须 `curl -4 ifconfig.me` 拿 IPv4 公网出口再对照 peer 日志（本机直连显示为公网出口 IP，不是 127.0.0.1 也不是 LAN IP）。该生产 VLM 服务器 `max_num_seqs=4`：Running 恒定 4、Waiting 全部 `reason=capacity`、忙时 KV cache 仍 <20%——排队是纯 slot 问题，不是显存，调优方向是调大 max_num_seqs（属服务器管理员权限，只报事实不擅改）。Engine 行的 prompt 吞吐尖峰（数千~数万 tok/s）= 一批长上下文请求刚灌入，队列随后 1~20 分钟内冲高回落——批量 VLM 提交避开尖峰后 10~20 分钟，或连续采样盯到 waiting ≤2 的安静窗口再提。归因前先确认隧道进程存在（A100 `ps` 里 `ssh -L 8002:localhost:8002` 仍在，否则 127.0.0.1 流量归属整个变掉）。用户反复问"当前大模型负载"时按固定格式汇报：`vllm:num_requests_running/waiting` + 日志尾部 Engine 行（Running/Waiting/KV cache/吞吐）→ 分档判读（waiting 0=空闲、1~2 轻度、3~5 偏忙、8+ 高峰）→ 给出"现在提交单请求大约等多久"+ 近一小时趋势（升/平/降），别只报瞬时数。注意 A100 代理 `active_runs=[]` 只说明平台无在途任务，不等于 vLLM 队列空（隧道流量、本机直连、外部用户都占 slot），两个数字分开陈述。
   - **误杀孤儿进程后的 retry 会永久秒败**：对 `NVIDIA_PIPELINE_FAILED:<step>:EXITED` 先查 `run.json`——若 `status` 仍为 RUNNING/STARTING 而 `pid` 已死（进程被手工 kill、OOM、机器重启），resume 对账会把"死 pid 且 status≠FAILED"判 `EXITED`，而旧状态归档只在 status=FAILED 时发生，每个新 attempt 都撞同一份陈旧状态秒败。修复：kill 前先/后把 `run.json` 的 `status` 改为 `FAILED` 并删 `launcher.pid`/`launch.lock`，再普通 retry，`--resume` 会从记录 step 续跑（已完成步骤产物复用）。本地 real 测试栈的 runs 目录在 A100 test 根 `/home/xmyf/wzd/test/rosbag-catalog/runs/{nvidia,self-built}/<run_id>/`，上文 `/data/nvidia_pipeline_runs` 是生产路径。
6b. **自建 Bag 的 `BAG_LOCKED` 失败 = 前次 attempt 遗留的孤儿会话锁 holder，不解除 retry 必秒败**：`scripts/bag_writer_lock.py` 的会话锁是 fail-safe 设计——写者序列异常退出（盘满、SSH 断、进程被杀）后**保留锁和现场供人工对账，永不自动释放**；retry 时 A100_PIPELINE 会在 1 秒内报 `BAG_LOCKED: <bag_uid> 写锁被其他进程持有（跨 run/删除方互斥）`。诊断路径（全程只读）：
   1. 锁键不是 bag uid 而是 `sha1("bag-baglock-v2|<environment>|<bag_uid>")[:24]`——按 uid `find -name` 永远找不到；锁根是 A100 的 `/home/xmyf/wzd/rosbag-cleanup-operations`（以处理机 `/etc/default/rosbag-a100-proxy` 的 `A100_BAG_LOCK_ROOT` 行为准）。
   2. 看 `locks/bag/<env>/<key>.lock` 及 `sessions/<key>/<session>/`：`result=STALE`（holder TTL 7200s 到期）+ `holding` 标记仍在 = 孤儿；`flock -n <lock> -c "echo UNLOCKED" || echo LOCKED` 返回 LOCKED 实锤。
   3. 定位孤儿 pid：`ps -eo pid,ppid,lstart,cmd | grep holder.sh`（holder 被 init 收养、只剩 sleep 1 子进程，其写者早已死透，对账后可安全释放）。
   4. 修复需用户授权：kill holder pid（退出即释放 flock、写 RELEASED）。若前次失败根因是盘满（自建 Phase3 步骤日志末尾 `OSError: [Errno 28] No space left on device`，非代码缺陷），先 `df -h /data` 确认余量足够再 retry，否则重撞同一堵墙；锁与盘都解除后普通 retry 即可，nav/cot/plot 等已完成步骤幂等跳过。
   5. **每次 retry 失败都会在同一锁键下新增一个孤儿 holder**（新 session 目录、holder.sh PPID=1、result 从 OK→STALE）——连续失败 N 次的 Bag 重试前必须先扫 `ps -eo pid,cmd | grep holder.sh` 全清，否则下一 attempt 必然 BAG_LOCKED 秒败。
   6. **盘满崩溃还会留下 0 字节半写产物，让后续 retry 换个症状秒败**：`cot_llm_validate.py` Stage 2 逐 clip 写 `cot_llm_summary.txt`，盘满崩时留下 0 字节空文件；Stage 2 默认 skip 已存在的该文件，retry 不会重新生成，Stage 3 apply 读到空文件报 `no ranges in summary` → `[cot_llm_apply] 完成: ok=0 skip=0 fail=N` → `rc=1 (0.x s)` 秒败（特征：日志尾部 fail=N 且耗时 <1s，区别于真 VLM 失败）。诊断：`find <infer_root>/<bag>_clip* -name cot_llm_summary.txt -size 0` 对照 apply 日志的 fail clip 清单；修复：只删 0 字节文件（`[ ! -s ]` 守卫），普通 retry 即对这几个 clip 重调 VLM、其余 clip 幂等跳过（2026-10-10 `6a82401e` attempt 3 盘满→attempt 5 no-ranges→attempt 6 删 5 个空文件后 PUBLISHED 实证）。
8. **A100 侧 DONE 后平台仍 FAILED，先看产物登记 422**：收尾阶段（artifacts 上报）是常见卡点——宿主机代理代码上报的 `artifact_type` 若不在 Backend `ArtifactType` 枚举（`app/domain/enums.py`），FastAPI 直接 422，整个收尾抛异常，run 滞留 FAILED 而 A100 数据完整。定位：处理机 `docker logs <backend 容器> --since/--until` 过滤 run id，看哪条 `POST .../artifacts` 返回 422（同批 200/422 交替能定位到第几个产物）。修复是纯枚举追加（artifact 服务按字符串存取，无 DB 迁移），本地验证后提交；但**部署与否是用户决策**（用户可能明确不做生产升级）——不部署时按用户指示让 run 保持 FAILED，并在 HANDOFF 记录 A100 产物路径、manifest hash 和“部署后普通 retry 即可收尾”的入口，供后续会话接手。已登记成功的产物靠幂等键（`{run_id}:{attempt}:artifact:{type}`）不会重复。
   - **帧导入类步骤的 422 表现为"数据全就绪但帧一帧未入"**：帧级入库（如 NVIDIA FRAME_IMPORT）在 A100 远端步骤全部收敛之后执行，且其 manifest 产物登记在真正写帧之前——登记 422 时 run 表现为 8 个 A100 步骤 DONE + NAS_BACKUP DONE + run FAILED + 帧表计数 0，全部数据（infer 产物、质量报告）完好，修好后普通 retry 从收尾重走即可，不需要碰 A100 数据。诊断时别把这种"数据齐全但帧为 0"误判成导入逻辑失败。
   - **单测全绿不能证明代理→Backend 的枚举/契约一致**：流水线单测 mock 掉 `deps.api`（API 客户端），代理上报的 `artifact_type` 是否真的在 Backend `ArtifactType` 枚举内从未被校验，首个生产 run 就 422。新增产物类型时除追加枚举外，必须加一个回归测试：断言代理代码上报的每个 type 字面量 `in [e.value for e in ArtifactType]`（`StringEnum` 没有 `.values()`，Pyright 会报错）；改前用 `git stash` 该枚举文件验证新测试变红，确认它真的能拦这类缺口。
   - **帧导入步骤报"没有 clip 目录"而 A100 数据齐全 = SSH 瞬断被掩盖，不是缺数据**：`FRAME_IMPORT_FAILED: .../infer 下没有 clip 目录` 是旧版 clip 枚举（list_clips）的掩盖式失败——远端 find 命令带 `2>/dev/null || true`，sshd 限流丢握手（kex_exchange 被拒）时返回空列表被上层误报成缺数据；帧导入发数千条 SSH，撞限流概率远高于 30 秒轮询的状态读取，部署后对滞留 run 的首次重试最容易撞上。取证：在处理机写探针**复用生产 `run_ssh` 包装**（sys.path 指 deploy 目录 + `/etc/default/rosbag-a100-proxy` 的连接参数）把同款 find 连跑 10 次、`make_list_clips` 连跑 15 次——出现空结果/CalledProcessError 且 stderr 含 `kex_exchange` 即瞬断实锤，全绿则才是数据/代码问题。修复（clip 枚举失败重试 4 次 + 去吞错 + 报错带真实原因 rc/stderr/异常详情，不再静默返空）与单连接读取修复同批合入 main；部署 + 代理重启完成前该症状还会复现，部署后若报错变成"列出 ... clip 目录失败: SSH 传输失败…"新措辞即修复已生效。
   - **平台代码用 `Path(...).exists()` 判"本地挂载 vs 远端 A100 路径"的分支（如 `_is_local_path`）在生产代理上基本恒为假/真错误分支**：处理机不挂 A100 数据盘，`/data/...` 在生产不存在，分支却可能因实现缺陷（如 `exists()` 返回值被丢弃、函数尾部无条件 `return True`）把远端路径误判成本地——症状是帧/文件类步骤报"…下没有 clip 目录"、DB 帧数恒 0，而用生产 `run_ssh` 包装直连 A100 实测完全正常。此类 locality 分支单测通常全部显式 mock，真实实现从未执行：修分支时必加**对真实实现的回归测试**（不 mock 该函数，断言"本机不存在的路径"返回 False/走远端通道），并在处理机 deploy 目录用生产连接参数实跑一次该函数确认。排查时先问"这个路径判断在生产宿主机上成立吗"，再怀疑 SSH。
   - **幂等哈希算法变更后，历史幂等账本行不兼容**：`ApiIdempotency.request_hash` 由 `canonical_request_hash` 生成，改排除字段（如把每 attempt 轮换的 `lease_token` 移出哈希）后，旧 attempt 已存的行仍是旧算法——同键重放继续 409，光改代码不生效。修复必须配对一次**定向清账本**：只删受影响 operation 前缀的行（如 `operation LIKE 'frames-bulk:<run_id>%'`，先只读 count 确认行数并向用户请示），数据表本身不动（upsert 幂等，全量重导入覆盖旧帧）。
   - **核实"某 run 进了多少帧"用 backend 容器内 DB count，别信两个易骗手段**：`GET /api/v1/frames` **不支持 `pipeline_run_id`/`source_id` 查询参数**——传了会被静默忽略，返回库里默认第一页（属于别的 run），且 `source_id` 对 NVIDIA run 是 Bag 级 ULID 不是 `nvidia-chunk-N`，按 chunk 名查永远是 0。权威口径：`docker exec rosbag-catalog-backend-1 python3 -c "...DatasetFrame...filter(pipeline_run_id=='<run>').count()"`（backend 容器内有 app 环境）。mysql 容器直连时多层 SSH 引号转义极易把 WHERE 打坏静默返 0，别据此下"没入库"结论。
9. **resume 后页面阶段总览可能是脏的**：API 的 `steps` 跨 attempt 累积，页面汇总按 step_name 取最新记录；resume 时 A100 侧被跳过复用的步骤（SUCCEEDED 命中）没有新上报记录，总览仍显示上一 attempt 的状态（如 DOWNLOAD 卡“执行中”、EXTRACT 显示“失败”、PRECHECK“未开始”）。用户反馈“重试后状态显示不对”时，以 A100 侧 `steps/*.json` 为准判断真实进度，页面显示是次生问题，需单独修渲染/上报，不阻塞任务本身。
9b. **run 未结束时 NVIDIA chunk 页面中间步骤全程 RUNNING 是已知展示滞后，不是故障**：轮询上报（`scripts/nvidia_pipeline.py` 的 `on_state`）只上报远端 `run.json` 的当前步骤状态，中间步骤的 DONE 批量对账要等整个 run 收尾（读远端 `steps/*.json`）才发生——run 结束前页面步骤表会一直显示 DOWNLOAD/EXTRACT 等"执行中"，事件只有 `STEP_RUNNING`。用户报"状态问题很大"时先核实活性：A100 `run.json` 当前 step + `steps/*.json` 真实状态 + 产物目录仍新增文件（`find <infer_dir>/... -newermt '-3 minutes' -type f`）+ 处理机侧租约心跳在滚动 = 健康，按展示滞后汇报并给修复方向，等用户拍板再动上报路径。
   - **修复已合入平台 main**（`scripts/nvidia_pipeline.py::remote_step_statuses`：`on_state` 每轮增量读远端 `steps/*.json` 逐步上报成功终态，base64 单行传输、按 attempt 严格过滤旧文件缺 attempt 字段一律跳过；`run.json` 的 (step,status) 签名未变化时跳读）——但只在**处理机部署 + a100 代理重启**后生效。生产仍看到"中间步骤全程 RUNNING"时先查 `docs/deployment-snapshot.md` 的平台 revision 是否含该提交、代理进程启动时间是否晚于部署时间，别重复修。
   - **重试后旧 attempt 的 `step_run` 行会永久停留 RUNNING**（终态批量对账只回填最高 attempt；中间步骤无 attempt 记录则不回填）。页面步骤总览按 step_name 取最新 attempt 显示正确，库内看到跨 attempt RUNNING/DONE 混杂是正常残留，不是故障；清理历史残留行需用户单独确认，不自动改生产库。
6c. **retry API 契约与挂起 POST 的副作用**：`POST /api/v1/pipeline-runs/{id}/retry` 普通重试**不接受 `retry_from` 字段**——带它返回 422 `普通重试从断点继续，无需选择重试起点`，空 body `{}` 即可（200，attempt+1，resume 从断点续）。挂起风险：该 POST 在客户端 curl 超时/网关重启打断时**服务端可能已执行**（attempt 已 +1 并开始跑）——任何 retry 重发前必须先拉详情看 attempt 和最后事件，已生效就不重发，否则双重 retry。
6. 秒级 DONE 的步骤可能只是跳过而非执行：每个 Phase3 步骤有远端 done 标记幂等跳过（如 `cocllm_done.json` status=ok 即跳 Stage 3，即使 `n_ok=0`）。判断：STEP_RUNNING→STEP_DONE 仅隔几秒 + 远端产物 mtime 早于本 attempt。此类“ok 但空”产物在普通重试下会反复跳过，必须强制重试（清理派生目录）才能真实重跑。

## SCENE run 下钻（场景识别 run：`scene-` 前缀 id，状态 `SCENE_DONE`/`SCENE_FAILED`/`SCENE_QUEUED` 等）

- 与自建/NVIDIA 分流：`GET /api/v1/pipeline-runs?page=1` 按 `pipeline_type=="SCENE_SLICE"` 或 id 前缀 `scene-` 过滤；终态集合补充 `SCENE_DONE`（成功）/`SCENE_FAILED`（失败），排队在途是 `SCENE_SLICE_QUEUE`，执行中是 `SCENE_RUNNING`。事件字段是 `event_type` + `payload`（**payload 是 JSON 字符串**，要二次 `json.loads`；不是 message/detail 键）。`SCENE_RUN_SCENE_DONE` 的 payload 里 `outcomes.<scene>` 给最终判定（SUCCESS/NOT_APPLICABLE 及缺 topic 清单）。
- 真实失败原因不在 Backend 事件里（`SCENE_FAILED` 事件通常无 error_detail）：在处理机 `docker logs rosbag-scene-proxy --since <窗口> 2>&1 | grep -vE "EGL|Open3D"`，失败行形如 `scene run <id> attempt N FAILED at <阶段>: <中文原因>`。
- 执行现场文件在处理机 `/home/data-process/scene/work/<run_id>/attempt-<N>/`：`checkpoint.json`（各步状态+证据）、`render_log.json`（预处理/verdicts/prompt_hashes）、`<场景>_model_results.json`、`jpg/`+`pcd/` 中间帧——失败 run 的现场保留，成功收尾后 staging 残留应被补偿清理；`.staging` 下出现非空残留目录就是"失败补偿没跑干净"信号。
- 已知失败签名：`缺车辆绑定（source_metadata.vehicle_code 未随父包传递）fail-closed` = RESOLVING 阶段秒败，父包登记时没绑车——修复实证路径：该 run 若由**系统兜底提交**（selection_json 空 source_metadata）触发，提交已绑定不可 PATCH（409 SUBMISSION_BOUND）——正解是给**父 bag_record 绑车**：Web 官方入口 `POST /bags/{bag_uid}/annotations`（form：vehicle_id + row_version=库内当前值 + 页面原有字段回传，处理机本机 `127.0.0.1:18000`），验证 `vehicle_link_status=MANUAL` 且 row_version+1 后普通 retry 即可；带 source_metadata 的用户提交仅在 PENDING_INPUT 阶段可 PATCH 修订元数据；**不猜车、不直接改库**；场景缺支持雷达 topic（`/iv_packets_front_mid`）= 正确判 NOT_APPLICABLE，属预期结果不是故障，汇报时分开归类。
- 场景 retry 契约同 6c（body `{}`，不带 retry_from，重发前先看 attempt）。切片长度语义：`window_size=15` 是**分析窗口**不是切片输出长度——切片边界=enter/exit 异常态 + `lead_seconds=3` 前置 + `overlap_merge_seconds=7` 合并；源包短于窗口时切片≈源包占比（4s 源出 3~4s 切片属正常），长包全段异常时按 15s 切段。用户问"切片怎么不是 15 秒"先查源包 duration（metadata.yaml）再查 segment 表。场景执行机单并发（`SCENE_EXEC_HOST`）：排队 run 的事件表现为 CLAIM/CLAIM_RELEASED 循环（reason=`SCENE_EXEC_HOST busy`），批量提交时后序 run 串行等前序，分钟级正常，别当异常。
- 注意别名歧义：推理服务器（LLM/VLM 端点所在机）的远端 hostname 也含 `a100`，与数据 A100（`a100-wzd`）不是同一台——场景模型调用走的是推理服务器，诊断模型超时别连错机器。
- **同一生产批次多会话并行时状态随时被推进**：读状态与写操作之间，其他会话可能已提交/上传/绑定同批 bag。每个写动作前重查 ingest_submission 状态、NAS 目录现势与容器活性；优先复用已有 PENDING 提交（同 relative_path 幂等），不自建重复提交——relative_path 的日期层口径（如 `20261012/<bag>` vs `<bag>`）与 `path_claim_key` 强相关，两种口径并存会绑不上或重复建 run。
- **生产 MySQL 多层引号陷阱**：`docker exec <mysql容器> sh -c "mysql ... -e \"SQL 含 $ 的 JSON 提取\""` 经 SSH→sh→mysql 三层后 `$` 被外层展开，SQL 静默打碎（语法错或空结果）。稳妥式：`docker exec <容器> mysql -uroot -p"$MYSQL_ROOT_PASSWORD" rosbag_catalog -e "SQL"`（单 SSH 层），仍复杂就落脚本文件执行；库开了 only_full_group_by，SELECT 非聚合列必须进 GROUP BY。

## 在途不推进下钻（用户问"为什么不再运行 / 卡住 / 排队"时）

FAILED 之外的“不推进”大多是**资源竞争排队，不是故障**。两条 run 都停在中间态（RUNNING、PHASE0_DONE、PHASE3_RUNNING 等）时按此诊断，全程只读：
0. **单 run 快照先判活性再下钻**：用户报"某 run 卡住了"时，先 `GET /api/v1/pipeline-runs/{id}` 读 `heartbeat_at`/`lease_expires_at` 与 `steps`——间隔约 5 秒再拉一次，两次 `heartbeat_at` 前进且租约续期（expires_at 后移）就是**正常推进**，即使 steps 表在两次快照间换了阶段（如 PHASE0_RUNNING→A100_UPLOAD）；按"在运行、无需干预"汇报并指出当前步骤，不继续开 SSH 下钻。`heartbeat_at` 停在旧时间且超过租约 TTL 才算可能失活，再进 2-4 步。

1. **先定性两条 run 各自状态**：非终态都属在途/中间态，不代表死了；`6a82…` 状态 RUNNING 但 Phase0 早已 DONE 的，说明它其实卡在“等资源派发下一阶段”。
2. **orchestrator 日志是排队原因的权威**：`ssh processing-machine 'docker logs rosbag-catalog-orchestrator-1 --since <时段> 2>&1 | grep <run_id>'`，找 `resource <X> is busy; pipeline <id> keeps waiting`（等资源中）或 `pipeline <id> released back to queue: <X> busy`（领了又因资源忙放回队列）。资源键：`A100_COMPUTE_IO`（A100 计算，一次只放一个自建 Bag 的 Phase1/Phase3）、`PHASE0_HOST`（Phase0 宿主机代理）。日志刷大量同句是每轮调度重试，**属正常，不是异常**。
3. **谁在占着资源**：A100 代理 `http://127.0.0.1:8766/health` 的 `active_runs`、Phase0 代理 `8765` 同理（只在处理机本机 curl），直接给出当前持资源的 run id。
   - **租约权威口径是 backend 的 ResourceLease 表**（health 的 active_runs 只反映代理线程，且与库内租约可能不同步）：写只读小脚本 scp 到处理机再 `docker cp` 进 backend 容器，`docker exec -e PYTHONPATH=/app rosbag-catalog-backend-1 python3 /tmp/check_lease.py` 列出 ResourcePool（executor_id/resource_key/capacity）和 `expires_at > now` 的 ResourceLease（resource_key/run/units/expires_at）。模型在 **`app.models`**（不是 `app.domain.models`——domain 目录没有 models，import 直接失败）；时间基准用 `app.clock.now()`（无时区北京时间）；容器内跑脚本必须 `-e PYTHONPATH=/app` 否则 `import app` 失败。只读查询不算违反本技能只读约束。
   - **回传 NAS 阶段（COPYING_TO_NAS / NAS_PUBLISH RUNNING）run 同时持有 `A100_COMPUTE_IO` + `NAS_PUBLISH` 双租约，A100 槽要等整个回传完成才释放**：~200G 级产物走网络回传可达几十分钟到几小时，其他自建 Bag 只能全程等回传完。"排队时间长得反常"时先看持租约者是否处于回传阶段（A100 上 `rsync --server` 进程 + NVMe 高读速率是实证），汇报口径为"在等回传收尾"而非"在等算力"；此时 A100 的 CPU/IO 看起来很闲是正常现象，不是资源浪费可提前放行的信号（提前放行走并发配置，见下节门禁）。
   - **回传进度/剩余时间的实测口径**：数 NAS 侧目标目录里**非空 clip 数 ÷ 总 clip 数**（源端 clip 数 = `find <infer_dir> -maxdepth 1 -type d -name "*_clip*" | wc -l`；目标端逐 clip `ls -A` 判非空，目录骨架会先建全，**骨架数不是进度**）；速度看处理机侧目标目录增长或 rsync 客户端 `ps -o etime`；A100 侧出口速率注意网卡是 **bond0**（eth0 恒 0，awk 按 eth/ens 匹配得全 0 会误判"没在传"）。并发 N 时回传（tx）与新任务下载 origin（rx）争同一链路，回传会被压到个位数 MB/s 甚至更慢（单跑 ~100MB/s → 并发 3 实测 ~1.3MB/s），剩余时间估算必须以实测写入速度为准，不能按单跑速率报"几分钟"。
   - **NAS 发布/回传的落点目录带 `.` 前缀**：自建 Bag 的 NAS_PUBLISH 先 rsync 进 final 下的隐藏 incoming 目录（`.../final/.incoming/<bag_uid>-<run前12位>/`，完成后原子 rename 为正式目录）。`ls final/` 或 `grep -i <run>` 匹配不到 `.` 开头目录，会误判"目标不存在"；处理机真实路径在 `/mnt/nas_backup/vla_dataset_builder_backup/final/...`，backend 容器内对应 `/nas/...`——**容器内 /nas 视图可能与处理机挂载点不一致（曾见容器内父目录为空、真实文件在处理机侧），删/查 NAS 内容一律以处理机 `/mnt/nas_backup` 为准并双向核实**（du 在 CIFS 上慢，大目录用 `ls | wc -l`/逐 clip 判空替代）。
4. **占资源的 run 是否健康**（决定是否要用户干预）：连 A100 看它的 `phase1/phase3_run.json` status 和 steps、产物目录是否仍新增。健康且在推进 → 汇报“排队中，前一个跑完自动接续，无需干预”，并按产物计数给个大致收尾预期；不健康 → 转入“失败根因下钻”。
5. 结论口径：排队等待 ≠ 故障；不要把“keeps waiting / released back to queue”当报错上报。

## 资源并发（锁上限）配置与提升（用户问"上限是不是能改 / 有没有到物理上限"时）

- 上限配置：backend `app/config.py::resource_capacity` 读四个 env，**全部默认 1**，由 deploy 目录 `compose.yaml` 的 backend 服务透传：`RESOURCE_COMPUTE_CAPACITY`（A100_COMPUTE_IO）、`RESOURCE_VLM_CAPACITY`（VLM_PERMITS）、`RESOURCE_NAS_PUBLISH_CAPACITY`（NAS_PUBLISH）、`RESOURCE_PHASE0_HOST_CAPACITY`（PHASE0_HOST）；ResourceKey 另有 SCENE_EXEC_HOST（无 env，恒 1）。先 `docker exec ... env | grep RESOURCE_` 实测容器内当前是否有显式覆盖，别假设走默认。
- **ResourcePool.capacity 在首次创建时固化**：`resource_capacity` env 只在 pool 行不存在时被引用。两种改法都属生产库写操作、需用户当次授权：①**热改（推荐）**——直接 `UPDATE resource_pool SET capacity=N WHERE resource_key=... AND executor_id='platform-local'`，acquire 每次实时读 pool 行，**立即生效、不重建容器、不重启代理，在途 run 的租约与心跳完全无感**，orchestrator 下一轮调度（约 10s 内）自动把排队任务派发到新增槽位；改完同步在 deploy 目录 `.env` 追加对应 `RESOURCE_*_CAPACITY`，防 pool 行未来被删重建时回落默认 1。②删 pool 行等下次 acquire 按 env 重建——生效窗口内 acquire 会 409，优先用 UPDATE。
- 提升自建并发前必过三道门禁（都查完再给建议）：
  1. A100 `df -h /data` 余量：单自建 Bag 的 infer 产物 ~200G 级，余量需 ≥ 新并发 × 单 Bag 产物 + 余量，否则第二个 run 中途 `No space left`；不够就先清已 PUBLISHED Bag 的 `xl_infer_dataset` 目录和 `runs` 归档（清理需用户单独授权，走 `a100-test-data-cleanup` 的对照 run 终态口径）。
  2. 远端 VLM 吞吐：自建 description 单 run 已 parallel=4，N 路并发即 4N 路堆远端 vllm 互相拖慢——要么同步提 VLM_PERMITS，要么在汇报里明说会慢。
  3. 自建 Bag 的硬串行点是 A100_COMPUTE_IO，持有窗口从 Phase1 跨到 NAS 发布结束（含纯 IO 的回传阶段）；提并发主要是"一个在回传、另一个在算"能重叠，磁盘余量是第一约束。
  4. **带宽双向争抢**：提并发后已有任务的 NAS 回传（A100 出口）会被新任务下载 origin（A100 入口）显著拖慢（实测回传速率降 ~50-100 倍）。执行热改前若存在回传中的 run，必须把该代价说清楚并给用户"维持全速并行 vs 等下载完加速收尾"的选项，不默认替用户选。
- NAS_PUBLISH pool 可独立提升（回传本身并行），前提同样是 A100 磁盘余量和 NAS/CIFS 侧吞吐。
- **生效验证**：热改后等一轮调度（≤1 分钟）看 `docker logs rosbag-catalog-orchestrator-1 --since 3m` 出现对排队 run 的 `dispatched A100 pipeline <id>`，且 8766 `/health` 的 `active_runs` 数 = 新容量上限；两个都满足才算落地，别只报"capacity 已改"。
- 汇报口径：A100 物理资源（load/IO/VLM）没到上限 ≠ 平台允许并发——串行是租约 cap=1 造成的，明确告诉用户"是配置锁，不是物理锁"。

## A100 盘容量与数据膨胀（用户问"/data 怎么满了 / 数据放大多少倍"时）

- A100 的 7T 数据盘是**多项目共享**（生产路径）：`/data/jc`（他人训练项目，可达 2.4T 且活跃增长——`ps` 有 torchrun/train 进程在跑即不可动）、`/data/mikelee`、`/data/sxd`、NVIDIA 流水线产物（origin+infer 合计 ~2.4T）、平台自建数据（`xl_origin_dataset`+`xl_infer_dataset` 每 Bag 合计 ~100G 级）。查盘先 `du -xh --max-depth=1 /data | sort -rh` 分解归属，别把别人的占用当平台问题，也别建议清他人目录。
- 自建 Bag 的 infer 产物有已知冗余，报"膨胀倍数"前先分解：①每 clip 的 `ins.json` 是整段 Bag INS 全量数据（md5 跨 clip 完全相同，~240M/clip × 全部 clip，单 Bag 约 38G 冗余）；②改名遗留的 `*_pro` 等同内容旧副本（整个 infer 目录再翻一倍）。单 Bag 产物 ~48G 去掉冗余后真实增量仅 ~10G 量级。治本（clip 级 ins 切片）属 A100 自建脚本仓库改动，涉及已发布数据集格式兼容，不顺手改生产脚本；清冗余副本需用户逐目录授权。

## 非 Bag source（NVIDIA chunk）数据与记录全删（用户给 chunk 目录名要求删数据+记录时）

流程与护栏见 `references/nvidia-chunk-deletion.md`：先对账范围（run/帧/副本/NAS）→ 用户确认范围 → 预演行数核对 → 生产 MySQL 表级备份 → 单事务删除 → API 404 + 全表行数 + 邻居 run 三重验证。全程精确匹配完整名（`chunk4` 与 `chunk45` 的 glob 歧义），NAS 侧以处理机挂载点操作。

## 生产配置/功能开关实查（用户问"生产上某开关开着还是关着"时）

- **一律读处理机 deploy 目录的生效文件，不靠本地仓库 HEAD 或 `docs/deployment-snapshot.md` 推断**：deploy 目录是 rsync 同步、无 `.git`，`git rev-parse` 会报"不是 git 仓库"。命令：`ssh -o BatchMode=yes processing-machine 'grep -E "<字段>" /home/data-process/deploy/rosbag-catalog/config/<配置文件>.json; ls -la --time-style=+%F_%T <同文件>'`，文件 mtime 就是最近一次同步时间。判"某代码是否在生产"用文件特征 grep（如该功能新增脚本是否存在、关键符号计数），deploy 目录没有可 git 对象。
- **snapshot 文档和会话开始时拿到的 revision 结论随时会过期**：多会话并行仓库里其他会话/用户可能在你长任务期间完成生产部署，用户追问生产现状时（尤其带情绪的追问）必须重连实查，查完直接给实测值，不用仓库/文档推断垫底再修正。
- 处理机 Backend 直接端口是 `127.0.0.1:18000`（经前端为 8180/192.168.2.134）；`/api/v1/system/health` 在该端口返回 404，健康探测用 `/health/live`、`/health/ready`（ready 含 environment）。
- **功能开关可能被其他会话/用户中途改动，本会话的推断随时失效**：会话开始时基于代码/文档得出的开关结论（如"生产该功能默认关"）在长任务期间可能被别人的配置 commit + 部署改掉。发现实测值与推断不符时（用户质疑或实查翻车），用 `git log --oneline -S'<字段>: false' -- config/<文件>` 找出改动 commit 的时间/作者/消息，如实报告"改动不是我做的、归属待用户确认"，不要默认为用户行为继续推进，也不要替用户解释意图。容器状态：`docker ps --format "{{.Names}}\t{{.Status}}"`，容器创建时间晚于 deploy 文件 mtime 即代码已随部署重建生效；出现新服务容器（如 `rosbag-catalog-bag-cleanup-worker-1`）也是新版上线的铁证。

## 数据集完整性核验（用户给出 A100/NAS 数据路径，问"是否完整可使用的结果"）

1. 路径名→bag：目录名即 bag 名。`/api/v1/bags` 没有 JSON 搜索接口，用处理机本机 `curl 'http://127.0.0.1:18000/bags?q=<bag名>'`（HTML 页），从返回里提取 `/bags/<bag_uid>` 链接；再取 bag 详情页里的 `/pipelines/<run_id>`。处理机本机查 API 直接用 `127.0.0.1:18000`（无需经 192.168.2.134:8180 前端）。
2. `GET /api/v1/pipeline-runs/{id}` 判定：`status=PUBLISHED` 且无 error_message；steps 跨 attempt 累积，只认**终态 attempt**（最高且 DONE 的一轮）各步 DONE；artifacts 里同 `artifact_type` 会重复出现（历史 attempt 的已软删），**只有 `deleted_at=null` 的条目**是终态有效产物，以其 `file_count/total_size_bytes/manifest_hash` 为核验基准。
3. A100 实地（只读）：从处理机连 A100 时别名 `a100-wzd` **不解析**，主机/端口/密钥从 `/etc/default/rosbag-a100-proxy` 取（生产为 `xmyf@211.141.18.194 -p 60022 -i /home/data-process/.ssh/id_ed25519_a100_wzd`）。核验：`phase3_manifest.json` 的 manifest_hash/file_count/size 与 A100_PHASE3 artifact 一致；clip 命名 `<完整bag名>_clipNNN`、每 clip 有非空 `dataset_index.csv`；`nav_done.json`/`cocrule_done.json` 等标记存在。
4. NAS final：FINAL_DATASET artifact 的路径在处理机 `/mnt/nas_backup/vla_dataset_builder_backup/final/<bag_uid>-<run前8位>-a<N>`，`test -d` + `du -sh` 确认已落地（CIFS 慢，长 timeout）。
5. 结论口径：PUBLISHED + manifest 三字段一致 + 实地齐全 = 完整可用；建议使用 **NAS final 目录**（不可变发布版本），A100 infer 目录是同内容生产副本、不得改写。注意目录可能是多次 attempt 叠加的结果（旧 Phase1 产物强制重试时已归档删除），不代表当前目录经历过失败污染。

## 规则

- `FAILED` 且 `auto_retry_at=null` 的输入/质量/冲突类错误（`INPUT_INVALID`、质量门禁失败、`FINGERPRINT_CONFLICT`）属不可自动重试的历史数据问题：汇报单列，不建议盲目重试；重试授权与决策归 `rosbag-catalog-supervisor`。
- 只读：禁止在本流程中调用 retry/cancel API、直连数据库或触碰 NAS。

## 常见陷阱

- 用 monitor 脚本的 FAILED 数量直接断言“系统出故障”——它不含时间维度，生产库长期沉淀大量历史 FAILED；先做第 5 步再定性。
- 拿本地栈端口查生产或反向：Backend URL 和 `environment` 字段不匹配时结论无效。
- 在单次 execute_code 里写超过 5 分钟的轮询循环盯长任务——kernel 300 秒被杀且状态丢失；拆成多次 <270 秒的短轮询调用。
- 多层 SSH 嵌套里内联复杂命令（awk `$`、引号套引号）会被各层 shell 反复解转义炸掉。A100 侧查状态一律用 `ssh a100-wzd 'python3 -' <<'PYEOF' ... PYEOF` 把完整 Python 脚本经 stdin 送达，脚本内再用 subprocess 跑 `pgrep/ss/find`，避免 shell 引号问题；需要两跳（A100→LLM 服务器）时在 Python 里 `subprocess.run(["ssh", ...])` 同样传 heredoc。
- 部署后本地 mock 收敛报 `DISPATCH_NOT_STARTED`/`A100_COMPUTE_IO is busy`：多为刚 `up` 后代理未就绪派发失败、租约未释放挡住后续任务；等服务端自动 `RESOURCE_LEASE_EXPIRED`（约 2 分钟）后重跑 test，不视为代码回归。
- **A100 SSH 高负载瞬断会让轮询脚本误判任务死亡**：A100 跑完 DESCRIPTION 等重活后 load 可冲到 12+，此时 SSH（60022）会间歇性 `kex_exchange_identification: Connection closed by remote host`。写处理机上的定时监控脚本时，`ssh` 返回非 0 / `NO_RUN_JSON` / `CHECK_ERROR` 属**探活失败，不是任务终态**——必须 sleep 30s 后重试 2-3 次再下结论，只有 `ps` 确认 pid 死 **且** `run.json` status 非 RUNNING 才判终态。把单次连接失败直接当 ABNORMAL 退出，会把正在跑/刚跑完的任务误报死亡（2026-09-30 chunk33 `c8b6fabe` 实证：监视器 check#3 撞瞬断误报 ABNORMAL，手动重试 4 分钟后发现 run 已 DONE/quality=PASS、描述 100/100 齐全）。轮询脚本把"连接错误"与"真终态"分进两个分支，终态分支才 `sys.exit`。瞬断的根因不是"A100 负载高"（load 12/128 核属正常）而是 **A100 sshd（60022）`MaxStartups 10:30:100` 未认证连接限流**：诊断特征 `ps -eo cmd | grep listener` 里 sshd 自身会打印 `8 of 10-100 startups` 计数。诱因是每轮轮询发 3~4 条独立 SSH（读 run.json、读 pid、kill -0 校验各自一条），A100 重活期间 sshd 响应变慢、未认证队列堆过 10 就开始随机丢握手。写新的 A100 侧轮询/监视脚本时优先**把多次远端命令合并进单条 SSH**（一个 `sh -c` 取回全部字段），从源头压低并发未认证连接；平台代理侧的 `remote_nvidia_state` 单连接读取 + NOT_STARTED 连续确认修复（2026-09-30 合入 main）同样源于此，未部署该版本的生产仍会复现 launch 误判；同批还有 clip 枚举的重试 + 去吞错修复（帧导入数千条 SSH 是限流最高频命中点，部署前还会见"没有 clip 目录"式掩盖报错，诊断见第 8 条）
- **pipeline-runs 详情响应经 terminal 命令替换读回会 JSON 解析失败**：详情体 ~20KB+，event payload 内嵌原始日志引用，命令替换通道会破坏字节（`Invalid control character`/`Expecting ',' delimiter`），重试无效。一律 `curl -s -o <文件>` 落盘后再在 Python 里 `json.load`；多 run 详情循环尤其如此
