# NVIDIA 帧级入库契约（已实现，改动前必读）

把 NVIDIA final 产物的每帧结果导入 `DatasetFrame` 表，与自建链路同表、同 `sample_id` 规则。契约经用户逐条 review 定稿并已实现（`scripts/nvidia_frame_importer.py` + `nvidia_pipeline.py` 的 `FRAME_IMPORT` 步骤）；改动行为前按本契约核实，偏离须先请示。

## 字段映射（NVIDIA 产物 → 提交给 bulk API 的原始行）

| 原始行列 | 来源 | 规则 |
|---|---|---|
| `frame_id` | `inference_index_strict.csv: frame_id` | reduce 采样后不重编号，直接用；索引内重复 ID → 导入失败 |
| `ego_ts_us` | `ego_ts` 列 | clip 相对 µs，原值入库 |
| `{cam}_f{0-3}_image` | `{cam}_f{0-3}_idx` 列 | 拼 `camera_images/{cam}/{idx:06d}_small.jpg`，相对 clip `data/` 目录 |
| `{cam}_f{0-3}_ts_ms` | 对应相机的 `{cam}_f{0-3}_ts` 列（**不取 ego_ts**） | **转整数 ms：对 µs/1000 按 .5 进位规则取整**（明确 round-half-up，不用 Python `round()` 的银行家舍入），不能把 µs 原值当 ms 提交 |
| `{cam}_f{0-3}_ts_us` | 相机 ts µs | 额外列保留原始精度，随 payload_json 落库 |
| `history_path`/`future_gt_path` | `ego_idx` 列 | 拼 `egomotion/frame_{ego_idx:06d}_history.npy` / `_future_gt.npy` |
| `planning_available`/`planning_frame_id` | 恒 `1` / `= frame_id` | 能进 strict 索引即通过校验；`planning_ego_time_diff_ms` 置空 |
| `navigation` | `navigation/navi_text.csv: final_nav` | 按 frame_id 集合 join；**缺或重复 ID → 失败，首版无容错阈值** |
| `description` | `navigation/clip_discription_llm.txt` | clip 级文本填到该 clip 每帧 |
| `csv_schema_version` | `nvidia-infer-index-v1` | 文档/页面须注明：时间为 clip 相对时间，不是 epoch，禁止跨链路比较绝对时间 |

## API 契约（最容易写错）
- `POST /frames/bulk` 的 `frames` 接收**原始行**，Backend 侧 `frame_service.bulk_upsert_frames` 会再次调 `frame_importer.normalize_row`。适配器若先规范化再提交（如传 `camera_paths_json`/`navigation_text`），字段读不到。与自建链路同路径：产 `*_image`、`*_ts_ms`、`navigation`、`description` 列名。
- `normalize_row` 按 `*_image` 后缀键通配收集相机（不依赖固定 7 相机名单），7 路×4 帧=28 路相机无需 Backend 改动。
- 成功判定必须检查响应全部字段 `inserted/updated/total/rejected/duplicates/errors`：API 非整批原子，可部分成功部分 rejected；`rejected > 0` 或批内 `duplicates > 0` 即失败并上报前 10 条 errors。
- 批次：10k 行/批，`import_batch_id = {run_id}-nvidia-frames`，`idempotency_key = {run_id}:frames:{clip_id}:{offset}`——**幂等键与 attempt 无关**（跨 attempt 重放同一 key，靠 upsert 收敛）。

## 重试语义（不建断点账本）
- 同一 attempt 内超时：同一幂等键 + 完全相同请求体重发（幂等表 replay 兜底）。
- 新 attempt：固定顺序（clip 按名排序、行按文件顺序）从头全量导入，靠 upsert 收敛。
- **manifest 身份绑定（实现实际做法，不是清表重导）**：FRAME_IMPORT 导入任何帧之前先 `register_artifact`（固定 `artifact_id={run_id}-nvidia-frames`、attempt 无关幂等键）持久化本次导入绑定的 `infer_manifest_hash`；重试时同参重放幂等表通过；若产物变化（manifest hash 不同）→ 幂等 409 → **导入失败并提示人工处理，不自动清表**。`_invalidate_force_outputs`（delete DatasetFrame by run）是**自建链路**强制重试逻辑，NVIDIA 明确拒绝强制重试，不可复用。
- `GET /api/v1/frames` 无 batch/run 过滤，不要用它做对账。

## 步骤顺序与配置链路
- 顺序：远端 FINAL_VALIDATING 通过 → `NAS_FINAL`（DONE/SKIPPED，现状不变）→ `FRAME_IMPORT`（代理本地步骤，RUNNING/DONE/FAILED，不新增 run 状态；`frames_import` 关闭时步骤不上报，流水线直接进终态）→ `PUBLISHED` / `NVIDIA_DONE`。
- 重试/恢复：远端已有完成结果时 FRAME_IMPORT 可单独补跑，不重新推理。
- 配置传递：`config/nvidia_pipeline_configs.json` 的 `frames_import`（**必须 JSON 布尔**，`nvidia_configs.py` 非 bool 即拒）→ submit 侧展开进 `pipeline_config_snapshot` → `scripts/a100_host_proxy.py` 从 **`snapshot["frames_import"]`** 读（缺字段保持关闭，不用 `bool("false")` 宽松转换）→ `scripts/nvidia_pipeline.py`。test 默认 true、production 默认 false。
- 数据源：`publish_final_to_nas=True` 读 NAS final 本地路径；否则从 A100 `output_path` 读。clip 产物根是 `{data_root}/infer/{clip_id}/data`——**data_root 已指向 `data_sample_chunk{NN}`，不得再拼 chunk 层**。
- **远端枚举注入（实现中发现的坑）**：`data_root` 为 A100 路径时本地文件系统不可见，clip 发现/文件核对/文本读取都必须走注入回调按 locality 分流（本地 `list_local_clips` iterdir / 远端 `make_list_clips` 走 `find -mindepth 1 -maxdepth 1 -type d`）——与本地分支同契约（只列目录）。对远端 `data_root` 直接 `Path.iterdir()` 会在任何帧导入前 `FileNotFoundError` 挂掉整个 run。

## 校验与验收
- 相机集合从 strict 索引列名推导（`camera_*_f{0-3}_idx` 模式）+ 既定 7 相机 schema 校验；**不枚举现存目录**（整路相机缺失会被目录枚举漏掉）。
- 文件存在性：优先复用 `infer_manifest`（发布前已验证）核对引用集合；A100 分支每 clip 一次批量 `sh -c` 检查，不逐图 SSH。
- 入库帧数 = 各 clip strict 索引**唯一 frame_id 集合**大小之和。不能用 `preprocess_metadata.json` 的 `num_valid_frames`（那是 reduce 前数字，差一个数量级）。navigation 按 frame_id **集合相等**核对，不只比行数。
- 幂等：同 attempt 重发与跨 attempt 重导不产生重复行。

## 非范围
- 自建链路零改动；表结构与 bulk API 本期不动；A100 侧产物生成脚本零改动（不推造 epoch）。
- 历史已发布 run 的回填（独立 backfill 脚本）另开任务；跑前必须盘点产物完整性（见 trial-run 参考）。
- 若页面需按 run 查帧：`list_frames` + 路由加 `pipeline_run_id` 过滤即可，不扩表（列为可选项）。
- 单测用仓库内小型夹具；外部真实样本（`example/processed/data_sample_chunk0`，strict/navi 各 114 行唯一）作可选集成测试。
