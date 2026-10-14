---
name: rosbag-catalog-scene-ingest
description: "Use when 场景识别(spray)交付数据、触发切片流程或排查SCENE_FAILED。"
---

# 场景识别数据交付与运行排障（rosbag-catalog）

生产链路：提交 API（PENDING_INPUT）→ 文件放 NAS incoming → Scanner 指纹/稳定轮次移入 ready → 登记绑定（SCHEDULED）→ Orchestrator 建 scene run → 处理机 `rosbag-scene-proxy` 容器执行 → 切片发布 NAS + 子包入库。失败修复走"父 bag 绑车 + 正式 retry"。

## 前置核查（交付前必做，全部只读）

1. 本地袋体检：db3 非空、`metadata.yaml` 可解析（0 字节袋禁止入列，只会挂 PENDING 永不触发）；支持雷达 topic `/iv_packets_front_mid`（seyond/msg/SeyondScan）是否存在——缺雷达的包正常结束但 outcome=NOT_APPLICABLE（"缺少水雾输入 topic"），**属预期不是故障**，汇报与真阳性分开归类。
2. 车辆：spray 入口 `source_metadata.vehicle_code` 必填，必须是库内已登记 Vehicle（`vehicle` 表，受控验证车 code=`test` id=1）；无车辆编号的 bag 不猜车（盘库/目录名/日期都不得作为依据），缺车辆走"待补资料"不执行。
3. 幂等提交复用：`ingest_submission` 已有同 `relative_path` 的 PENDING_INPUT 提交时直接复用（文件入列即触发），不自建重复提交。

## 交付步骤

1. 提交：`POST http://192.168.2.134:8180/api/v1/ingest-submissions`
   ```json
   {"profile_id":"scene-spray","relative_path":"<bag目录名>","idempotency_key":"<唯一>",
    "source_metadata":{"schema_version":"scene-input/v1","vehicle_code":"test",
    "collection_timezone":"Asia/Shanghai","batch_id":"<批次>",
    "sensor_profile":"seyond-front-mid-approved-v1"}}
   ```
   201=PENDING_INPUT 成功（purposes/scenes 缺省走 profile 默认 scene_slice+spray-v1）；422 带错误码（缺车/未知车）；409=同幂等键不同内容。
2. 放文件：整袋 `cp -a <本地bag> /mnt/nas_backup/rosbag_scene_processing/incoming/spray/.<bag>.tmp` → `mv .<bag>.tmp <bag>`（原子入列，Scanner 稳定轮次 ~2 轮后自动移 ready；开发机挂载点即 NAS 生产树）。**relative_path 必须与提交完全一致——生产口径为 `incoming/spray/<bag>` 无日期层**；多带一层（如 `20261012/<bag>`）时 entry_relative 与提交对不上，bind 不命中用户提交而走系统兜底提交（空 source_metadata），run 秒败"缺车辆绑定"。
3. 状态推进：`ingest_submission.status` PENDING_INPUT→BOUND→SCHEDULED（`bound_incoming_path/bound_ready_path` 回写），`bag_record.source_status=READY`，随后 `scene-` run 出现。推进慢时查 Scanner journal（处理机容器内 `/data/scanner-state-scene.json`，MOVED_PENDING_REGISTER 未消=登记对账卡住）。
4. 执行机**单并发**（SCENE_EXEC_HOST cap 恒 1）：排队 run 事件是 CLAIM/CLAIM_RELEASED（reason=`SCENE_EXEC_HOST busy`）循环，10 分钟级正常，别当故障。大包（50min+ 行程）解码+模型推理按 10~60 分钟级预期。

## SCENE_FAILED 修复（缺车辆绑定，实证路径）

proxy 日志特征：`docker logs rosbag-scene-proxy 2>&1 | grep -vE 'EGL|Open3D'` → `scene run <id> attempt N FAILED at RESOLVING: spray 场景缺车辆绑定（source_metadata.vehicle_code 未随父包传递），fail-closed 不执行（不猜车）`。

根因是**父 bag_record 未绑车**（目录名无车辆编号时 vehicle_id=NULL/UNMATCHED；由系统兜底提交触发的 run 必然如此）。修复：
1. 官方 Web 入口绑车（处理机本机 18000 端口，非 8180 前端）：`curl -X POST http://127.0.0.1:18000/bags/<bag_uid>/annotations --data-urlencode row_version=<库内当前值> --data-urlencode vehicle_id=1 --data-urlencode note="<批次>受控验证绑test车"`（303 成功；row_version 冲突 409 需重取）；验证 `vehicle_id=1, vehicle_link_status=MANUAL, row_version+1`。
2. 正式 retry：`POST /api/v1/pipeline-runs/<scene-...>/retry` body `{}`（**不带 retry_from**；重发前先拉详情看 attempt 防双触发）→ 同 run 新 attempt，resume 复用已完成的识别结果只补后续步骤。
3. 用户提交（带 source_metadata 的）只在其仍 PENDING_INPUT 时可 PATCH 修订元数据；BOUND/SCHEDULED 后 409 SUBMISSION_BOUND——此时只能改父 bag 车辆（步骤 1）。

## 切片语义与产物验证

- 场景产物粒度是**时间区间**，落 `scene_analysis_version`/`scene_segment`/`bag_derivation` 表组 + `bag_record` 子包行（bag_role=SCENE_SLICE）；**不写 dataset_frame**（那是 VLA 帧表）。原始 bag 登记两者共用 `bag_record`（靠 `relative_path` 前缀区分：scene=`rosbag_scene_processing/ready/...`，VLA=`vla_dataset_builder_backup/...`；dataset_type 都是 self_built 不能用于区分）。
- 切片发布目录：`/mnt/nas_backup/rosbag_scene_processing/slices/<vehicle_key>/spray/<父bag_uid>_<子bag_uid>/`（含 `rosbag2_0.db3`+`metadata.yaml`+`slice_manifest.json`；当前生产落在 `test/` 子目录=受控 test 车口径）。NAS 侧路径与容器内 `/public/...` 同物理位置（开发机/处理机挂载点都是 `/mnt/nas_backup`，容器内叫 `/public`）——处理机宿主机无 `/public` 目录，查 slices 要进 `rosbag-scene-proxy` 容器或走处理机 `/mnt/nas_backup`。
- 切片长度 ≠ `window_size`：`window_size=15` 是**分析窗口**，切片边界=enter(3帧异常进入)/exit_(5帧正常退出) + `lead_seconds=3` 前置 + `overlap_merge_seconds=7` 合并。源包短于窗口 → 切片≈源包占比（4s 源出 3~4s 切片正常）；长包全段异常 → 按 15s 切段（53s 源出 4 个 15s 切片正常）。用户质疑"怎么不是 15 秒"先查源包 duration 再查 `scene_segment.duration_ns`。
- 验证：run 详情 `SCENE_RUN_SCENE_DONE` payload 的 `outcomes.<scene>`（SUCCESS/NOT_APPLICABLE）；切片目录文件非空 + `slice_manifest.json`；`bag_record` 子包行 `availability=AVAILABLE` + `bag_derivation` 父子血缘齐全。

## 常见陷阱

- **CIFS 上直接 `sqlite3.connect(路径).write` 必败 `database is locked`**——即使无并发（CIFS `nounix,soft` 挂载下 fcntl 锁失效，容器内同挂载点最小复现实证）；本地盘同代码 0.03s 通过。任何"写 SQLite 到 NAS"的链路必须两段式：本地盘写完整库并 commit+close → 拷 CIFS `.staging` → 逐文件摘要一致 → 同盘 rename 发布。改 CIFS mount 选项不是正解。
- 多会话并行交付同批 bag：每次写动作前重查 `ingest_submission` 状态 + NAS 目录现势 + proxy 容器活性（`docker ps | grep scene`；"No such container"多为在开发机误跑 docker 命令——mysql/proxy 容器在处理机，脚本先 scp 再执行）。
- 空袋（0 字节 db3/metadata）：禁入列、禁识别（交付契约明文）；已有挂 PENDING 提交不处理即可。
- 同名 bag 的 `_pro` 重跑版本：帧表里是另一 source_id、结构化字段（ego/planning/nav/quality/时间戳）100% 相同、仅 VLM description 文本 ~58% 不同（重跑非确定性）；"重复数据只算一次"按结构化真值判即可；description 含 clip 目录名嵌入（`# Clip: ..._pro_clipNNN`），逐帧比对前把 bag/clip 名规范化再 hash，否则假差异满屏。
