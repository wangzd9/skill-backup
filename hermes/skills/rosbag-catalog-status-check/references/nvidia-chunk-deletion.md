# NVIDIA chunk 数据与记录全删（生产）

用户给出 chunk 目录名（如 `data_sample_chunk45`）要求"删数据及对应记录"时使用。A100 生产数据目录 + NAS origin 副本 + 平台 DB 记录三处都要动，属生产数据保护边界，每步前需用户当次授权范围。

## 1. 范围对账（只读，先做，向用户报清单再动手）

- 平台：`GET /api/v1/pipeline-runs/<run_id>`（或按 source 找 run）确认 status、error_message；`source_copy` 两条路径 = A100_NVIDIA_RESULT（`/data/nvidia_infer_dataset/<chunk>`）+ NVIDIA_ORIGIN_NAS（`/mnt/nas_backup/vla_dataset_builder_backup/nvidia_origin/<source ULID>/<chunk>`）。
- **chunk 在 `bag_record` 表也有行**（`bag_uid = 'nvidia-chunk-N'`），全删清单必须包含；deploy 库**没有 `source` 表**，对 source 查/备会 1146 报错——表清单按实际 `information_schema` 探测，不凭猜测列名。
- 注意 chunk 可能**已导入大量帧**（DatasetFrame 按 source_id + pipeline_run_id 查）——NVIDIA_DONE 不是"未处理样本"，删帧 = 数据集永久少这部分，必须在授权问题里明示帧数让用户拍板删到哪一层。
- **帧数权威口径用 backend 容器内 raw SQL，别用 ORM 导入**：`docker exec -e PYTHONPATH=/app rosbag-catalog-backend-1 python3 /tmp/<script>.py`，脚本 `from app.database import SessionLocal` 后 `S.execute(text("SELECT COUNT(*) FROM dataset_frame WHERE pipeline_run_id=:a"))`（按 run、按 source、全表 count 三档作护栏）。**`from app.db.session import SessionLocal` 会 ModuleNotFoundError、`from app.models import db` 会 ImportError——处理机 deploy 的 app 包只有 `app.database.SessionLocal`**。
- **帧数=0 且 A100 侧 `run.json` status=DONE = 成果已算完、只是没走到 FRAME_IMPORT**：此时删 A100 infer 目录丢失的是已算完的成果（DESCRIPTION 都过了），resume 救不回、想 PUBLISHED 须重算——授权选项里明说这笔代价，别当成"删未处理样本"。
- **同 source 常挂多个 run（含历史 FAILED）**：`pipeline_run WHERE source_id=<nvidia-chunk-N>` 全列出；清"平台 DB 记录"时把"只删本 run vs 连同失败 run"作为选项让用户拍板，不默认全清。
- **NAS origin 副本在 CIFS、不占 `/data`**：目标是腾 A100 `/data` 时只有删 A100 infer+origin 两目录有收益；授权选项里标注各层对磁盘的贡献，别让用户以为删 NAS 也腾 /data。
- A100 侧核对：`du -sh` 两个目录、`/data/nvidia_pipeline_runs/<run_id>/`（run 状态目录，仅日志/manifest，默认保留）、有无相关进程/活跃租约（`ps -eo ... | grep <chunk>`、ResourceLease 表）——**有在途 run 或活跃租约就停手报告**。
- NAS 路径以处理机 `/mnt/nas_backup/...` 为准（backend 容器内 `/nas` 视图可能为空，见 SKILL 回传节）。

## 2. DB 预演 + 备份 + 删除（backend 容器内跑 Python）

写脚本 scp 到处理机 → `docker cp` 进 backend → `docker exec -e PYTHONPATH=/app rosbag-catalog-backend-1 python3 /tmp/<script>.py`（session 用 `from app.database import SessionLocal`，时间用 `app.clock.now()`；预演/删除均可走 raw `text()` SQL，避免 ORM 导入踩 `app.models.db`/`app.db.session` 不存在的坑）。四阶段单脚本：

1. **备份**：逐表 `CREATE TABLE <t>_backup_<YYYYMMDD> LIKE <t>; INSERT ... SELECT * WHERE <目标条件>`，每张校验备份行数 == 预演目标行数。dataset_frame 大表只备份关键列（id/source_id/pipeline_run_id/clip_id/frame_id/artifact_id/quality_status），整表 `SELECT *` 备份会把生产 MySQL 再撑爆一份额外的冗余空间。
2. **终核对**：重查每表目标行数（与预演一致才继续）+ 护栏：目标行数 ≤ 全表行数、`artifact WHERE source_id=... AND pipeline_run_id<><run>` == 0、`pipeline_run WHERE source_id=...` 列出确认该 source 无其他 run。
3. **删除**：单事务——**用 `engine.begin()`**（`app.database` 导出 engine），不要 `Session.begin()`：Session 执行过查询后 autobegin 已开启，`S.begin()` 会抛 `A transaction is already begun on this Session`。表序 pipeline_run → step_run → pipeline_event → artifact → source_copy → dataset_frame → bag_record；帧的 WHERE 用 **source_id AND pipeline_run_id 双条件**，bag_record 用 `bag_uid=:s`。每张 rowcount 与期望值比对，任一不符整体回滚（事务未提交）。删除脚本写成幂等：备份表已存在则跳过建表、只校验行数；崩溃后重跑安全。
4. **验证**：目标条件行数全 0、全表行数 == 删前全表 − 删前目标（如 pipeline_run 19→17）、备份表行数完好。

## 3. A100 + NAS 目录删除

- **精确路径**：`rm -rf` 用完整目录名，删前后 `ls -d <父目录>/*chunk<数字>*` 比对，防 `*chunk4*` 式 glob 误伤邻居（chunk4 与 chunk45 并存时尤其危险）。
- 顺序：A100 infer + origin（NVMe 上秒级）→ NAS origin 副本（CIFS 慢，后台跑并 notify）。NAS 删完用处理机侧 `test -d` 确认 GONE + 列父目录确认邻居 chunk 副本仍在。
- 收尾验证：平台 `GET /api/v1/pipeline-runs/<run_id>` 返回 404；抽 2-3 个相邻 chunk 的 run 详情确认未受影响；`df -h /data` 报磁盘收益。

## 坑

- 用户说"删 X 及对应记录"≠ 授权删 NAS：数据在 A100、origin 在 NAS、记录在 DB 三处，范围要拆开逐项让用户确认（含"NAS origin 副本保不保留"选项）。
- 备份表命名带日期，删后保留（用户未要求清备份前不删备份表）；向用户报备份表名，给"要恢复随时可以"的出口。
- 后台 `rm -rf` NAS 目录若路径打空（父目录不存在）RC 仍是 0——删完必须 `test -d` 回读，不能信 RC。
- 用户要求"先预演、没问题再动手"时，预演输出固定包含：逐表目标行数与全表行数（删后预期）、备份表名、帧数、A100 两目录 du 与 `df /data` 预期收益、NAS 副本体量（标注不占 /data）、邻居目录清单（精确全名防 glob）、护栏结论（无租约/无进程）——报完等用户确认范围再进执行段，预演本身只读。
- 预演脚本按第 1 节 information_schema 实际表清单动态生成 DELETE/备份语句，不要硬编码表名再临时补——多查一张不存在的表整个脚本 1146 退出，前面的预演输出也拿不干净。
