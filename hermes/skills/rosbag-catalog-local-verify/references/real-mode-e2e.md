# real 模式真实数据全链路测试

本地 real 栈（`local_stack.sh up --mode real`，宿主机代理 18765/18766）上跑真实 bag 与 NVIDIA chunk 到终态的标准流程。

## 提交
- 自建 bag：整目录 `cp -a` 到 NAS 测试根 `incoming/<新目录名>/`（含 metadata.yaml + db3，字节逐一核对）。Scanner 需两轮稳定性扫描（约 15-20 分钟）才移入 ready 并登记。内容相同（哪怕换目录名）会指纹去重、无新 run。
- NVIDIA：`curl POST http://127.0.0.1:8181/nvidia -d chunk=<n> -d pipeline_config=<cfg>`（web 端口 8181 也代理提交端点，或走 Backend API）。真实下载 27G 级 chunk 耗时数小时。

## 监控（终态判定口径）
- 后台 Python 监控脚本（terminal background + notify），120 秒采样查 MySQL：run 状态 + 该 run 最新 attempt 的步骤分布。表名单数 `pipeline_run`/`step_run`，凭据 `docker exec <mysql> sh -c 'echo $MYSQL_PASSWORD'`。
- 终态集合：`PUBLISHED / FAILED / CANCELLED`（NVIDIA 为 `NVIDIA_DONE`）。注意 **run 级状态会滞后于 attempt 级**：attempt N 失败时 run 仍显示 RUNNING，自动重试或手工 retry 后 attempt N+1 继续——监控必须同时看 `attempt` 字段和步骤表，否则把某个 attempt 的 FAILED 当终态误报。
- 步骤表按 `(step_name, attempt)` 取最新；`GROUP BY step_name` 配非聚合 `status` 列会撞 `only_full_group_by` 报错，用 `MAX(attempt)` + 子查询或按 attempt 过滤。

## GPU OOM 自动重试
Phase0 的 extract/resize/mp4 步骤吃 GPU，显存不够被 OOM killer 杀（`exit code -9`）。后端 retry_policy 识别 `EXIT CODE -9/137` 自动调度下一 attempt（约 60 秒后），attempt 级 FAILED 会自动恢复——属预期行为，不用人工干预；只需确认下一 attempt 真正起来了。反复 OOM 才查显存占用（残留工作目录/进程）。

## ORIGIN_PUBLISH_CONFLICT（NVIDIA 发布冲突）
NVIDIA 跑完全部步骤后 origin 备份发布到 NAS 测试根 `<revision>/data_sample_chunk<n>/`；目标已存在（之前批次的发布）即冲突失败，数据本体安全在 A100 `data/nvidia_origin_dataset/`。处置：
1. 用户确认后 `rm -rf` NAS 测试根该 chunk 目录（测试根可重建；生产 NAS 备份不得动）。
2. `POST /api/v1/pipeline-runs/<id>/retry`（normal 模式）——`--resume` 幂等跳过全部已完成步骤，只收尾发布。

## retry 后进度判读（run.json 滞后）
A100 侧 `runs/nvidia/<id>/run.json` 的 `attempt`/`step`/`status` 可能停留在上一 attempt（如 DONE/FINAL_VALIDATING），因为 resume 判定步骤已完成、直接进收尾，而收尾（origin 发布 rsync）由**本地代理接管**执行。真实进度看：
- 本地代理 `proxy-state/a100/<run_id>.json`（recovery）的 `stage`/`updated_at`/`remote_pid`；
- 活跃进程链：`ps aux | grep run_nvidia_chunk`（远端）+ `rsync --server`（本地代理接收端，父进程是 sshd）；
- NAS 目标目录大小增长（`du -sh`，27G 级传输约 10 分钟）。
`launcher.pid` 指向已死 pid 且 `launcher.log` 为空 ≠ 任务失败——先查是否有新的 run 进程在跑。

## NVIDIA_ORIGIN_VERIFY_FAILED（发布校验 hash 不匹配）
发布后校验（`scripts/nas_publisher.py` 对比 rsync 落地的 file_count/total_size_bytes/manifest_hash 与下载时快照 manifest）失败且 file_count、total_size_bytes 都通过、只有 hash 不匹配时，是内容传输损坏——不要放宽校验（它正是拦住坏数据的机制）；代码已含校验失败自动清空重传一次再复验（`_rsync_and_verify`）。诊断顺序（每一步都决定下一个怀疑对象，别跳）：
1. **先数次数再定性**：首次出现可记"瞬态损坏"并重试；同一签名跨 **≥2 次独立传输**（每次 attempt 的自动重传各算 1 次）仍失败就不是随机毛刺——停止盲目 retry，诊断做完整再决定。连续 3+ 次同签名失败通常指向传输路径里的确定性因素（某参数组合、某时段链路），不指向"运气差"。
2. A100 源端逐文件复核：payload manifest 不在 origin 目录里，在 **run 目录** `runs/nvidia/<id>/download_payload_manifest.json`；用 base64 或 heredoc 把 Python 脚本送过去（SSH 多层引号会炸 f-string），逐文件 sha256 对照 manifest，0 mismatch 即源端干净。源端复核要在**失败时刻之后**再跑一次——只跑失败前的那次不能排除源端后续变化。
3. **复现必须与管道 1:1 参数同款**：`scripts/a100_host_proxy.py::_rsync_from_remote` 用 `--files-from=-` 且清单走 **subprocess stdin**（`input=file_list`）。用 `--files-from=<文件>` 复现走的是不同 rsync 参数路径，其 MATCH=True 不能证明管道 stdin 路径无问题；1:1 复现要用 Python `subprocess.run(cmd, input=filelist)` 调 rsync。命令行的 `echo list | rsync --files-from=-` 在部分 shell 环境下 stdin 到不了 rsync（只传 4K 空目录的旧坑），复现前先 `find` 落地目录确认文件数非 0。
4. 复现"源→NAS CIFS"传输落测试根临时目录 + `sync` 后复核：一致只证明**该时刻**链路健康——CIFS 有小时级吞吐退化窗口（实测 24MB/s 掉到 3MB/s，`du`/整树扫描超时），时间错开的复现成功不覆盖失败时刻；不能用事后复现否定当时链路异常。
**监控长发布期间禁止对 NAS 树跑 `du`/`find`/hash 扫描**：这些 CIFS 读与 rsync 写抢同一链路带宽，会主动拖慢进行中的发布（自伤坑）；查进度只用 `stat` 单目录/单文件，大范围扫描一律等链路空闲。
复核脚本必须排除 manifest 自身三个文件（`.download_contract.json`/`download_manifest.json`/`download_payload_manifest.json`），与 `nas_publisher.ORIGIN_EXCLUDED_NAMES` 对齐，否则计数虚高。

## 发布阶段被代理重启打断
final 发布（COPYING_TO_NAS/NAS_PUBLISH）进行中被 kill 代理：lease 过期被标 FAILED（error 常是 409 Conflict），A100 侧 infer 产物可能已被清理逻辑收走（retry 将从 Phase1 重跑），NAS `final/.incoming/` 留半成品目录。
**"真校验失败"与"进程被打断"在库里表象相似（都 FAILED），不要凭 `local_stack.sh up`/代理重启的时间邻近下结论**——权威判据：查该 run 的 `pipeline_event` 里 HEARTBEAT 是否等间隔连续到失败时刻。心跳连续到 `STEP_FAILED` = 进程一直活着，是真失败（查 error_message 指向的具体步骤）；心跳在失败前戛然而止、`lease_expires_at` 早于 FAILED 写入时刻 = 被打断。`up --mode real` 会 restart 代理但不一定杀到正在跑的 run（preflight `--allow-running-proxy` 允许在跑任务），时间线相近不等于因果。
retry 前必须先清掉该 `.incoming/<source_id>-<run 前 12 位>-aN` 目录，否则残留内容污染下一轮校验/发布。`.incoming` 只属于"尚未原子 replace 的临时区"，清它不动已发布的 final。

## 不可重试类失败
源 bag 缺流水线必需话题（如早期采集无 `/planning/*`）：Phase3_NAV 报 planning.csv 缺列，确定性失败，换数据而非重试。判据：`ros2 bag info` 原始话题表与成功基线 run 的产物列数对比（planning.csv 完整应为 60+ 列）。

## 场景线（切片）real 全流程测试（宿主 executor）
适用：本地 real 栈上用真实 bag 跑 目录→VLM→切片→publish→complete-scene 的场景线 E2E 到终态（不经 Phase0/1/3 A100 重流水线）。驱动是宿主脚本走 Backend HTTP API 逐阶段推进，不是 orchestrator 自动流转。
- **执行模型**：本地栈未配 `SCENE_PROXY_URL` 时 orchestrator 领 scene claim 会 release 回队列，永远无执行体——别只观察队列空转就判应用 bug，先核该配置。宿主驱动按 submit→register→source 升 READY→explicit 建 run→claim→analyze（真实 VLM，单袋 120~180s）→publish→complete-scene 顺序驱动。
- **complete-scene 的文件证据校验在容器内读 publish_root**：发布产物必须落在 backend 容器可见的 bind 点（宿主目录 → 容器路径的 compose 挂载）。宿主普通目录写产物 → 容器读空 → complete-scene 假失败。测试临时加的 compose override 挂载**测完必须 `git checkout` 还原**（主仓 tracked 文件保持零改动，协调/合入要用工作树）。
- **清 bind 挂载点只清子内容，不删挂载点目录本身**：rmtree 掉挂载点会换 inode，容器仍绑着旧的空 inode——症状是"宿主明明有文件、容器读空"，重建容器后文件才"出现"。已发生后：清子内容 + 重建 backend 重绑。这是"半完成轮次残留"类失败的常见根，排查先查宿主目录 mtime/inode 与容器视角是否一致。
- **VLM 调用（Qwen 思考模式）**：`max_tokens` 必须 ≥2048——256 时图像请求 `content=None`（思考输出吃光预算、正式回复空），极易误诊成"模型判不出场景"；模型 ID 必须完整路径（短名 404）。跑全流程前先单帧探针：真实阳性首帧判对 + content 非空，阴性帧不误报，再放行全流程。
- **权威状态断言从 DB 取，不从 API 顶层取**：`source_status` 等字段不落 API 响应顶层，从顶层读 `None` 会把通过的轮次误判失败；`docker exec <mysql> sh -c 'mysql ... -e "SELECT ..."'` 取真值。**DB 取出的值喂进 typed 构造器前要归一类型**：shell mysql 输出全是字符串，`source_generation` 等 int 字段传字符串会让落盘 digest 与 API 侧 `int()` 归一重建的 digest 不一致，complete-scene 报 "发布记录与 manifest digest 不一致（不同版本混放）"——构造 SceneManifest/请求体的每个 int 字段显式 `int()`，同驱动内两条路径（run 请求与 publish manifest）必须同一归一口径。
- **digest 失配类 409 先本地合成往返复现再定性**：`to_dict()→按 parse 映射重建→digest 相等`的进程内探针（几十行）能立刻区分"API 重建逻辑丢字段"与"驱动 payload 类型不纯"；探针 EQUAL 时别再怀疑 app 映射，回头核驱动喂进去的原始类型。落盘 per-slice 记录的 `manifest` 字段类型（str vs int）就是现成的指纹，直接比对。
- **跨轮幂等**：register 幂等复用 source 后 consume 无新 purpose 事件、恒 `consumed:0` 不建 run——建 run 走 explicit run API 且每轮幂等键用 `uuid4().hex`（固定键在 teardown 后复用悬空 run_id → `claimed:False`）。同源再 register 撞 409 "fingerprint matches a derived scene slice" 是**防重绑守卫的正确生产行为**不是 bug：先 teardown 该 source 场景链再走干净流程。
- **teardown FK 顺序**（场景链清行）：bag_derivation → scene_analysis_version / scene_segment / bag_scene_label_evidence → pipeline_event / pipeline_run → bag_record → **ingest_submission**（该表列名是 `status` 不是 `state`、值域是 `PENDING_INPUT` 不是 `PENDING`——写 `state='PENDING'` 会 rc=0 静默删 0 行，下一轮 submit 撞 409 PATH_CLAIMED（relative_path 占位守卫）才发现）；清完以 scene_analysis_version=0、derived slice=0、PENDING 提交=0、父包无场景指纹为准，再重跑。**长周期驱动应把这个 reset 固化成启动步骤（每轮自清）而非依赖每次跑前手工清理**——手工清理恰是嵌套引号静默失手的高发点。**清理 SQL 走 Python 脚本（subprocess 列表传参），不用 `sh -c 'mysql -e "...''...''"'` 嵌套引号**：f-string SQL 的单引号被外层 shell 吞掉，DELETE 静默 0 行且 rc=0，下一轮重跑撞 "fingerprint matches a derived scene slice" 才发现没删干净。清后必须 SELECT COUNT(*) 逐表回读落证据。另：`scene_analysis_version.analysis_version` 列存的是 **digest 十六进制**，版本序号在 `id` 列——按列名直觉断言版本号会误判。
- **发布产物送容器用唯一每轮路径 + docker cp，不用 named volume 下复用固定目录**：backend 的 `/data` 是 named volume，`docker cp <host-dir> <c>:/data/scene-publish` 目标目录已存在时会把宿主目录**嵌进去**（`/data/scene-publish/<宿主目录名>/...`），complete-scene 按固定子路径读不到产物；且卷内文件属主是 root，容器内 `rm` 也删不掉。正确形态：容器目标路径每轮 `/data/scene-publish-<uuid8>`（不存在 → cp 平铺，永不嵌套、免清理权限问题）。
- **claim 租约 TTL 必须 > 最坏执行时长**：VLM 隧道负载高时单袋 analyze 可达 15~20 分钟（正常 2~3 分钟），TTL 给 900s 会在 analyze 中途过期被 orchestrator stale 回收标 SCENE_FAILED，complete-scene 之后 409 "run 已终态且场景结果不一致" 不可逆（该轮只能 teardown 重跑）。高负载期 TTL 给 3600s。配套两条驱动纪律：① claim 前先自愈复位**自己 executor_id** 名下 SCENE_RUNNING 的旧租约（lease_owner 匹配才动，status 回队列、lease 列置 NULL）——驱动崩溃路径不释放租约会把 run 卡死，下轮 claim 恒 claimed:False；② 驱动 fail() 里 best-effort POST release-claim。
- **VLM 判定非确定性（同袋跨轮漂移）与 NO_MATCH 诊断**：同一真实阳性袋不同轮抽帧判定序列可能从"连续坡道"漂移成"坡道/平地交错"，交错时最长连续段低于能力契约的最小时长门 → analyze 返回 NO_MATCH、0 段——这是**负路径门禁按设计生效**（防碎片段误发布），不是 app 回归。诊断必读 work 目录产物 `frame_log.json`（逐帧 label+时间戳）与 `model_results.json`（VLM 原始文本序列）：有完整判定记录且最长连续段低于时长门 = 合法 NO_MATCH，重跑一轮（判定序列大概率收敛回连续区间）；evidence=0 且帧记录缺失才怀疑 VLM 调用失败被收敛。重跑前按上条 teardown 清干净该 source 场景链。
- **UI 默认视图/列表语义变更后，向用户的汇报必须写明既有数据去了哪个视图**："默认展示当前可用切片"这类行为变更会让用户刷新页面看到空列表、误判数据丢失；汇报里逐项给"你熟悉的数据在 X 视图 / 新数据在 Y 视图"的对照，并实测三个视图各自的行数佐证。
- **终态核验（防 HTTP 200 幻觉）**：API 全 200 不等于通过——交叉断言 DB（availability=AVAILABLE、run 终态、derivation 区间）+ 实际产物（rosbag2 db3 可打开且消息数与 DB 记录精确一致、slice_manifest 在位）+ 源袋全程 sha256 before==after，缺一不报 PASS。
- frontend 页 502 而 backend 健康：旧容器缓存旧 backend IP，`--force-recreate frontend` 即可（frontend 不吃 local_stack.sh 的业务 env flag；**backend 的任何手工重建仍必须走 `local_stack.sh up --mode real`**，见 SKILL.md 同族坑）。
