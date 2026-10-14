---
name: nvidia-pipeline-ops
description: "提交/诊断处理机 NVIDIA chunk 流水线：提交与 code_revision 门禁。"
---

# NVIDIA chunk 流水线操作（处理机生产）

在处理机生产环境提交 NVIDIA 数据集 chunk、排查 PRECHECK 秒失败、核对 run 真实进度时用。单 chunk 走 `chunks` 接口；批量 `submissions` 接口受配置 `batch_enabled` 门禁，未开放时只能单 chunk（报“尚未开放批量提交”）。

## 提交（用户已授权的生产写操作，在处理机 127.0.0.1:18000 上 curl）
1. preview：`curl -s -X POST http://127.0.0.1:18000/api/v1/nvidia/chunks/preview -H 'Content-Type: application/json' -d '{"chunk":N}'`。确认 `can_create:true` 且 `existing_pipeline_run_id` 为空。历史 run 已到终态不阻塞新提交；同 source_identity+config_digest 有**活动** run 会 409。
2. 提交：`... /api/v1/nvidia/chunks -d '{"chunk":N,"publish_origin_to_nas":true,"publish_final_to_nas":false,"note":"...用户授权..."}'`。publish 开关默认与既有生产 run 一致（origin 发布、final 不发布）。返回 `pipeline_run_id` + `NVIDIA_QUEUE`。
3. orchestrator 自动派发到 A100 代理；轮询 `GET /api/v1/pipeline-runs/{id}`，看 `events` 的 `STEP_RUNNING`/`STEP_FAILED`/`STATUS_CHANGED`。提交成功≠跑通，必须核对到终态再报。

## PRECHECK 的 code_revision 门禁（最易踩）
- 批准配置（如 `nvidia-prod-v1`）锁定 `code_revision`；A100 生产代码工作区 HEAD 必须与之一致，否则 PRECHECK 秒失败：`NVIDIA_PIPELINE_FAILED:PRECHECK:CODE_REVISION_CONFLICT:<实际head>`。
- **PRECHECK 只校验 code_revision，不探测 chunk 数据是否存在**：物理缺失的 chunk（A100 origin 目录没有）PRECHECK 照样秒过、正常进 DOWNLOAD——数据缺口的真实暴露点在 DOWNLOAD（从 hf-mirror 拉取失败），或当 HF 上其实有该 chunk 时整条流程完整跑通（等效一个新数据集的正向 run）。不要把"A100 本地路径不存在"当负向用例的预期失败点去预判，提交后按实际步骤（PRECHECK→DOWNLOAD→…）核对到终态再定性。
- 诊断：SSH A100 看 `/home/xmyf/wzd/code/nvidia`（`repos/nvidia` 常同 HEAD）`git log -1 --stat`。若它比配置锁的 revision 领先，就是冲突来源。
- **不要自动 bump 配置 code_revision、不要强推/回退 A100 工作区**——把 A100 新代码纳入生产基线属生产配置变更，需用户明确授权。向用户报告领先提交的作者+改动内容请其拍板。
- 用户授权后的 bump 路径（改仓库配置再部署，不是生成新 config version）：`config/nvidia_pipeline_configs.json` 里 **test 与 production 两个配置的 `code_revision` 要一起改**——测试强制两者 code_revision 一致（不在允许的切换字段差异集），只改 prod 会让本地全量测试红。同步点共 4 处：① A100 **test** 工作区 fast-forward（`./scripts/update_a100_script_repositories.sh --environment test --check` 先看不动远端，再 `--apply`，内部 git pull --ff-only；A100 production 工作区已在目标 SHA 不用动）；② `config/local-a100-real.env` 的 `A100_NVIDIA_REVISION`（不同步则部署快照 revision 对账报 stale）；③ `tests/test_nvidia_control_plane.py` 硬编码的 code_revision 断言；④ 切换文档里的基线描述。全量测试绿后部署处理机（config 随 deploy rsync 上线），再重新提交 chunk，PRECHECK 通过。
- bump 后首次提 chunk 若秒失败，先确认失败 run 是旧配置提交（config_digest 不一致）——重新 POST 提交一个新 run 即可，旧失败 run 留作记录不阻塞。

## 生产部署后收口（修复上线 → 跑通一次 chunk 验证）
- 处理机部署脚本（rsync + 容器重建）**不会重启宿主机代理 service**（`rosbag-a100-proxy`/`rosbag-phase0-proxy`）：部署后必须 `kill <MainPID>`（systemd Restart=always 自动拉起新进程）再验证生效——grep deploy 目录里本次修复的标记字符串（如新函数名）确认代码已同步、`ps -o lstart` 确认进程新于部署时刻，`curl 127.0.0.1:8766/health` 且 `active_runs` 为空。跳过此步时代理继续跑旧代码，修复"已部署"但完全没生效，排查极易走偏（按已部署版本推断行为全部错误）。
- 部署后若 retry 秒败/失败形态与修复前**完全相同**，第一嫌疑就是代理未重启（旧代码在跑），先验代理版本再改代码。
- **重启 a100 代理会自动从 recovery state 续跑未完成 run**：重启后未手动 retry，代理可能先自行发起一次 attempt，随后的手动 retry 再 +1——retry 返回的 `attempt` 字段才是事实，监视脚本与幂等键命名按返回值而非预期值；发现 attempt 数对不上时先查代理 journal 时间线再怀疑有他人操作。
- 失败签名与新代码预期行为**矛盾**（报错来自不该走的分支/旧错误文案）时，先实证生产实际走哪个分支：**"数据缺失类"文案（如 infer 下没有 clip 目录）反复同签名出现、而探针/直调入口函数每次都真实返回数据时，指向分支走错（locality 谓词、路径拼接、配置取值），不是 SSH 瞬断**——此时按分支判断→入口函数实测递进取证：在处理机写脚本 `sys.path.insert(0, deploy 目录)` import 被测模块，对**真实路径**直调分支谓词与入口函数验证——本地单测全绿不算证据，谓词与远端回调通常全被 mock，谓词本身恒错的 bug 只有生产首跑暴露（修谓词后必须红绿验证：撤掉修复跑新用例应红）。诊断脚本按"分支判断→入口函数实测→N 次连发统计"递进取证，别先改代码再猜。
- A100 跑 chunk 计算期间 load 高（几十核机器也可能 load 10+），sshd MaxStartups 默认 10:30:100 会随机断未认证连接（诊断：A100 `/var/log/auth.log` 的 `sshd -D [listener] N of 10-100 startups` 行 + 处理机 `ss -tn state syn-recv`）。平台侧缓解 = 减少连接数 + 只读瞬断重试；不要指望改 A100 sshd 配置（需 root、非本项目资产）。客户端两层加固（2026-10 已落地，改 ssh 调用链时别退回去）：① `ssh_base` 与 rsync `-e` 传输串带同一组 ControlMaster 连接复用选项（`ControlMaster=auto` + 按 用户@主机:端口 隔离的 ControlPath + `ControlPersist=300`），数百条批量只读共享一条已认证 socket，不再经 MaxStartups 未认证队列；② 自建流水线的读通道已接入（`SelfBuiltPipelineDeps.ssh_read` 注入 `run_ssh_read`，帧导入/Phase1 轮询/Phase3 状态读走读通道，写通道保持无重试防双执行）——自建链路不再"撞瞬断即整 run FAILED"，剩余无重试风险只在写通道与未走读通道的调用点。
- 核验帧入库数量**直查生产 DB**：`/frames` API 不支持 `pipeline_run_id` 过滤（未知参数被忽略返回默认页），帧 `source_id` 是 bag 的 26 字符 ULID 而非 run_id/chunk 名——用 `ssh processing-machine "docker exec rosbag-catalog-backend-1 python ..."` 走 ORM 查 `DatasetFrame` 按 `pipeline_run_id` 计数与抽样。
- retry 的 `Idempotency-Key` 全局唯一且跨操作类型判重：create/cancel/retry 每个操作用独立 key（如 `manual-<run>-retry:<attempt>`），复用前缀会 409 重放冲突。

## 读真实状态（页面会滞后）
- 页面 `steps` 中间步骤的 DONE 最多延迟一个轮询周期刷新（增量上报，非实时）。判断某步是否真完成，以 A100 侧文件为准。
- 分步终态：A100 `<run_root>/<run_id>/steps/*.json` 的 `status`；**只信带当前 `attempt` 匹配字段的文件**（重跑不清理旧 steps/，遗留文件不可信）。
- 当前活跃步骤+进程存活：`<run_root>/<run_id>/run.json`（`step`/`pid`/`pid_start_ticks`）+ `kill -0 <pid>`。
- 生产路径：run_root=`/data/nvidia_pipeline_runs`；origin=`/data/nvidia_origin_dataset`；infer=`/data/nvidia_infer_dataset`。
- A100 代理健康/活动 run：在处理机 `curl 127.0.0.1:8766/health`（`active_runs` 为空才可安全重启代理）。

## 终态产物核验（NAS_BACKUP DONE 不能只信状态）
- NAS 发布路径带 **dataset_revision 子目录**：`/mnt/nas_backup/vla_dataset_builder_backup/nvidia_origin/<dataset_revision>/data_sample_chunkNN`。按猜的短路径查不到≠没发布，先 `find` 到前一个成功 chunk 的实际位置再对照。
- NAS 副本是**下载原件白名单收敛**（payload + `download_payload_manifest.json`），不是 A100 全量 origin——NAS 量级明显小于 A100 origin 目录属正常，用与前一成功 chunk 的量级对比做合理性判断。量级参考（单 chunk 真实 run）：A100 origin 全量 ~27G、A100 final 推理产物 ~23G（数十万文件）、NAS payload 收敛 ~14G（16 个下载对象 + manifest）——三者差距是白名单收敛的设计结果，不是丢数据。
- 完整性：`find <NAS目录> -type f | wc -l` 应等于 manifest `file_count` + 1（manifest 自身），并核 `manifest_hash` 存在。
- 长 run（小时级）：提交后设周期只读巡检（cron 每 30 分钟只查 Backend API 的 run 状态），到终态自动删任务、核验产物、回填 HANDOFF 后汇报；不要长驻轮询。**巡检脚本放 `~/.hermes/scripts/`**（scratch 24h 被清理，跨数小时的监控会丢脚本；terminal `background=true` 会被超时截断且 nohup 被拒，小时级监控一律走 cron）；cron 用 `monitor` 门控 + 确定性只读脚本（只打印 `nvidia=<status> self=<status>` 这类无时间戳状态行），状态不变则跳过 agent 不烧 LLM，跳变/终态才唤醒：prompt 里写全两条 run 的 id、终态集合、到终态后的动作（抓 events 尾部 + error_message → 删 cron job → 中文汇报），deliver 保持 origin。

## 数据库落库边界（回答"结果存哪"类问题）
- **NVIDIA 链路已做帧级入库**（FRAME_IMPORT）：`nvidia_pipeline.py` 在 `NAS_FINAL` 后、`PUBLISHED`/`NVIDIA_DONE` 前执行 `FRAME_IMPORT` 代理本地步骤，解析 A100 infer 产物（`inference_index_strict.csv` + `navigation/navi_text.csv` + `navigation/clip_discription_llm.txt`）调 `POST /api/v1/pipeline-runs/{id}/frames/bulk`，与自建链路**同表**（`DatasetFrame`）、同 `sample_id` 规则；网页帧级查询/筛选对两条链路统一可用。产物级记录（`pipeline_artifact`、source copy）照旧。
- 开关：`frames_import` 从 run 的 `pipeline_config_snapshot` 读（**必须 JSON 布尔**，非 bool 提交即拒），test 默认 true、production 默认 false，历史 snapshot 缺字段保持关闭。配置生效需同步 `config/nvidia_pipeline_configs.json` + proxy 侧 snapshot 读取两处，只改 JSON/dataclass 不生效。
- 重试与产物变化：manifest 身份绑定（`register_artifact` 固定 `artifact_id={run_id}-nvidia-frames`，attempt 无关幂等键），重试同参重放靠幂等表通过；产物变化 → 409 失败提示人工处理，**不自动清表**（`_invalidate_force_outputs` 是自建强制重试逻辑，NVIDIA 不可复用）。幂等键 attempt 无关，新 attempt 固定顺序全量导入靠 upsert 收敛，不建断点账本——部分失败后 retry 已入的帧会幂等覆盖，不产生重复。**幂等哈希算法本身变更时，旧 attempt 写入的 `api_idempotency` 行存的是旧算法 hash，修复上线后重放同键仍判"同键不同参数"409**（凭证类字段如 lease_token 进出 hash 排除集时必踩）：清掉该 run 的旧账本行（`operation LIKE 'frames-bulk:<run>%'` 精确范围，只删账本不删 `DatasetFrame`——帧数据靠 upsert 幂等覆盖无损）再 retry，代码修复只管未来 run、治不了存量行。**改 `canonical_request_hash` 的排除集前必须先确认每个走 `idempotent_replay` 的端点有独立于 hash 的凭证/租约校验**（如 `require_valid_task_lease` 在回放前单独跑），否则把凭证排除出 hash 会真实弱化安全。FRAME_IMPORT 的每个文件读取（clip 枚举/CSV/描述 cat/find 校验）都经 A100 SSH：高负载下瞬断按"读取次数 × 单连接失败率"命中，per-clip 读取必须走只读瞬断重试通道（见 local-verify 的 SSH 传输层条目），否则一次断连即整段失败；该通道强制捕获输出（瞬断判定依赖 stderr，关闭捕获会让重试静默失效），且远端读命令禁止 `|| true` 吞错（会把连接失败伪装成"数据缺失"）。
- A100 真实产物现存位置是 `/home/xmyf/wzd/data/nvidia_infer_dataset/`（非 test 目录）；对已有数据手动试跑导入前必须盘点产物完整性（现存历史批次可能整体缺 `clip_discription_llm.txt`，导入按契约缺描述即失败，属数据问题不是代码 bug）。详见 `references/nvidia-frame-import-trial-run.md`。
- 帧级结果文件在 final 产物内的相对路径（A100 infer_root 与 NAS final 一致，A100 phase2 脚本写死此结构）：`data_sample_chunk{NN}/infer/{clip_id}/data/{inference_index_strict.csv（帧主索引）, inference_index_origin.csv（reduce 前备份）, camera_images/, egomotion/frame_XXXXXX_history.npy、future_gt.npy, navigation/{navi_text.csv, navi_route.csv}}`；clip 级还有 `preprocess_metadata.json`。回答"帧结果在哪"按此结构报路径，不要只说"在文件里"。
- 帧级入库的字段映射与 API 契约：`references/nvidia-frame-import-contract.md`（已按实现更新）；改动 FRAME_IMPORT 行为前先读，不要凭印象重新设计。

## 自建同 bag 重跑配方（用户要求"已有袋新建一条 run 再跑"时）
1. 提交接口 `POST /api/v1/pipeline-runs`，**新 run 不传 `queue_name` 或用 `PHASE0_QUEUE`**：全新 run 必须本地重跑 Phase0 生成自己的 `PHASE0_LOCAL` 副本再上 A100。枚举是 `PHASE0_QUEUE/SELF_BUILT_QUEUE/NVIDIA_QUEUE`（只写 `PHASE0` 会 422）；**手动指定 `SELF_BUILT_QUEUE` 会让新 run 直接从 A100_UPLOAD 起步、秒失败 `INPUT_INVALID: PHASE0_LOCAL source copy is missing and A100 origin is unavailable`**——Phase1 只认"未删除且属于 {None, 本 run_id} 的 PHASE0_LOCAL 副本"或"本 run 已登记的 A100 origin"，旧 run 的 PHASE0_LOCAL 副本在上传完 Phase0 后就 `deleted_at` 软删、A100 origin 登记在旧 run_id 名下，全新 run 两路都拿不到。`SELF_BUILT_QUEUE` 只用于已有 Phase0 产物的重试路由。
2. **bag_uid 禁止凭肉眼/转述手敲**：26 字符里存在肉眼混淆对，手敲后 API 报 `source not found` 但用 DB 读出的值立刻命中。从 backend 容器 ORM 按 name 读出 uid 再经 shell 变量传进 curl（同一层内 `\$VAR` 引用，别在多层 SSH 里重打字符串）。另：bash 的 `UID` 是只读变量，临时变量改名。
3. **测试数据隔离的用户口径（用户已纠正过，别再搞反）**：用户说"在原名后加 test 后缀做区分"指**测试 bag 本身**带后缀、新 run 的**所有产物**按 `{原名}.test` 命名、原 bag 及其历史 A100 产物**零触碰**——不是给旧 run 的 A100 产物改名后让新 run 写回原路径（那个方向做反过，且 mv 旧产物会让旧 PUBLISHED run 的 DB artifact 指向悬空）。正确做法（独立测试 bag）：
   1. NAS ready 根内 `cp -a {bag} {bag}.test`（同文件系统拷贝，5.8G 约 1 分钟；NAS 余量充足时可行，与 A100 禁 cp 的约束不同）；核验 `md5sum` 两个 .db3 一致——`du` 对大文件可能报错（8.0K），字节数 stat 相等 + md5 一致才算拷贝完整，未核验不得登记。
   2. `POST /api/v1/sources/register`（`/public/…` 逻辑路径，backend 容器 MountedNasClient 可读），**不传 `fast_fingerprint`**：拷贝内容与原件同指纹，传了会命中 `existing_by_fingerprint` 分支把**原 bag** 的 `name`/`relative_path` 改写成 `.test`（污染原记录）；不传则 `upsert_bag` 按 `relative_path` 去重 → 新 uid、新 bag、自动建 `PHASE0_QUEUE` run，A100 origin/infer 均为独立 `.test` 路径，天然无 `PRODUCTION_PATH_CONFLICT`。
   3. 登记后必须回查原 bag 的 `name`/`relative_path`/`status`/`bag_uid` 未变、A100 原路径 origin/infer 原位完好，再报"已隔离"。
   若用户明确要"同 bag 同名新 run"而非独立测试记录，才用上一版让路方案：**infer `{infer_root}/{bag_name}`（Phase1/3 输出）必须让出**（生产模式 `PRODUCTION_PATH_CONFLICT` 守卫：路径被旧 run 占用且新 run 无本 run Phase1 状态时直接报错——同文件系统 `mv` 加 `.run<旧run前8位>` 后缀让路，瞬时不占额外盘不删除，禁止 cp 防 A100 /data 爆盘），**origin `{origin_root}/{bag_name}`（Phase0 输入）必须留在原路径**（复用输入，A100_UPLOAD rsync dry-run 指纹比对：一致复用、不一致 `FINGERPRINT_CONFLICT` 拒绝，永不静默改写；挪走 = INPUT_INVALID）。
4. 提交时主动向用户交代三件事，避免事后被追问：① 原始数据来源是 NAS 上那个 bag 本身（`NAS_RAW` 副本，run 无关），A100 origin/infer 都是它的派生产物；② 测试隔离方式（独立 `.test` bag vs infer 后缀让路）；③ 旧 run 的终态交付物（NAS final 目录按 `{source}-{run前12位}-a{attempt}` 独立、DatasetFrame 已落库）不受影响，新 run 会生成独立新 final。
5. 失败诊断先**按 attempt 取最新 StepRun 行的 error 全文**（DB 截断 2000 字符）；`flock -n` 拿不到锁（result 文件 `LOCKED`）时先查同 bag 是否有**孤儿 writer-lock holder**：holder 是 A100 上被 init 收养的独立进程（`sh sessions/<baghash>/<fd>/holder.sh`，等 release 文件或 `ttl=7200` 到点），cancel run 不会杀它——`fuser -v <lock>` 确认持有者，孤儿 holder 按设计机制释放（向 session 目录 `touch release`，或等 TTL），不要直接 kill（跳过 canceled 状态检查分支）。，再定性：`exit status 255` + 只剩 ssh 命令参数文本 = A100 ssh 连接层失败（kex 风暴期，见 status-check 常见陷阱），用探测连发（5 连干净测试）确认 ssh 已恢复再 retry；短文本业务错误（如 `INPUT_INVALID:` 开头）是前置条件不满足，与 ssh 无关——**同一 run 的多个 attempt 行错误形态可能完全不同**（前 attempt 是 255 截断文本、后 attempt 才是真因），别拿旧 attempt 的截断文本给新失败定性。**自建链路撞 ssh 瞬断无自动 attempt 重试，整 run 落 FAILED；NVIDIA 链路会 attempt N+1 自动重试**（同一波风暴两条链路不同命运），向用户解释"为什么一个活一个死"时以此为准。
5. 小时级以上 run 的巡检按上文 cron monitor 方案；terminal `background=true` 不适合长巡检（超时截断、nohup 被拒）。

## 边界
- 与 `rosbag-catalog-status-check`（只读健康/失败新旧巡检）区分：本 skill 覆盖**提交与操作**（生产写）；两者都涉及 NVIDIA run 状态读取但目的不同。
- A100 常满载跑训练（8 卡）：NVIDIA chunk 会排队（`A100_COMPUTE_IO` 容量 1 串行）并竞争 GPU，提交后别把“长时间 NVIDIA_QUEUE”当故障。
