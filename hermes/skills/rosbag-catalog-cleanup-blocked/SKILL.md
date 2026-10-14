---
name: rosbag-catalog-cleanup-blocked
description: Bag 清理 BLOCKED、cleanup session mismatch 排查与解除。
---

# Bag 清理（T-03/T-07）BLOCKED 排查与解除

处理机 8180 页面"清理"走 bag_cleanup 协议：预览 → worker 对账 → 计划 blockers → 确认删除。受阻时按本流程定位并解除。生产库写操作必须先备份再执行，且需用户授权。

## 排查步骤

1. **模拟浏览器取阻断证据**（页面弹框只有文案，完整 blockers 在 API）：
   ```bash
   curl -s -c /tmp/cj.txt -H "Origin: http://192.168.2.134:8180" \
     -X POST http://192.168.2.134:8180/api/v1/sources/{bag_uid}/cleanup-previews
   sleep 25  # 等 worker 对账
   curl -s -b /tmp/cj.txt -H "Origin: http://192.168.2.134:8180" \
     http://192.168.2.134:8180/api/v1/bag-cleanups/{cleanup_id}
   ```
   Origin 必须与 `BAG_CLEANUP_ALLOWED_ORIGINS`（处理机 backend env 可查）完全一致；preview 会种 `rc_cleanup_session` cookie，后续查询带同一 cookie。解析 JSON 的 `blockers` 数组拿到 code+path+message。
2. **按 blocker 代码定性**：
   - `UNKNOWN_STORAGE: source_copy path not in approved matrix` / `artifact path not in approved matrix` → 该 source 的 `source_copy` / `artifact` 表里有指向旧根（test 时代 `/home/xmyf/wzd/data/self-built/*`）的遗留记录，不在生产 approved matrix（`approved_roots_for("production")`：`/data/xl_origin_dataset`、`/data/xl_infer_dataset`、`/data/xl_origin_dataset_staging`、`/home/xmyf/wzd/runs`）。这是 test→production 迁移漏清的僵尸指针，属最高频成因。
   - `CROSS_SOURCE` / `SHARED_DATA` / `ORPHAN_RUN` / `ACTIVE_WRITER` / `UNKNOWN_STATE` → 数据归属或远端状态问题，逐条核对，不做批量数据修正。
   - `COVERAGE_INCOMPLETE` / `REMOTE_UNREACHABLE` → worker/executor 链路问题，先查 worker 容器与隧道。
3. **僵尸行处置**（用户授权后）：处理机 MySQL 容器名是 `rosbag-catalog_mysql_1`（下划线）：
   ```bash
   ssh processing-machine 'docker exec rosbag-catalog_mysql_1 mysql -uroot -p"$(docker exec rosbag-catalog_mysql_1 printenv MYSQL_ROOT_PASSWORD)" rosbag_catalog -e "..."'
   ```
   - 先在 A100 `ls -d` 逐条核实磁盘上不存在（只读）；
   - 备份：`CREATE TABLE IF NOT EXISTS <表>_backup_<日期> LIKE <表>; INSERT ... SELECT ...`；
   - **物理 DELETE，不要软删 `deleted_at`**——plan 核对按 DESIGN 契约显式包含软删记录（"软删除索引不证明远端文件已不存在"，`test_soft_deleted_index_still_checked` 断言软删也阻断），软删无效；
   - `source_copy` 清完后**必须再查 `artifact` 表同路径残留**——两层核对独立，清一层常暴露下一层（本次即如此）；`DATASET_FRAME_INDEX` 类型 artifact 指向同一 phase3 目录也算；
   - 删掉该 source 名下旧的 BLOCKED 操作行（同样先备份 `bag_cleanup_operation`），否则幂等/活跃操作残留影响新预览。
4. **验证**：重新 POST preview → 等 25s → GET 操作，`can_confirm` 应翻 true、blockers 为空；把 targets 的 PRESENT/ABSENT 分布和 preserved 清单汇报给用户（ABSENT 只是对账项不产生删除；ABSENT 数量多 = 历史 run 次数多，属正常）。

## 会话绑定规则（cleanup session mismatch 的成因与处置）

- 清理操作绑定发起浏览器的签名 cookie；**每个 source 同时只能有一条活跃操作**，先点者独占。Agent 用 curl 建的操作会让用户浏览器报 `cleanup session mismatch`——排查结束后必须删掉 agent 建的 BLOCKED 操作（备份后 DELETE），让用户浏览器重新发起；
- `retry` 只对 `RETRYABLE_FAILED`/`NEEDS_RECONCILIATION` 有效，**BLOCKED 不能 retry**，只能删操作重新预览；
- 用户换机器/浏览器删除完全可行：新浏览器 = 新会话，用配置 Origin 访问页面自己发起即可（内网使用者均可确认，无账号限制）；提醒用户操作期间别从其他会话碰同一 bag；
- 确认后的删除是 worker 异步跑（排队→远端删除→核验→DB 收尾），用户需保持发起浏览器页面到终态；Agent 侧用 DB 核验终态：A100 目标零残留、preserved 完好、`bag_cleanup_operation` 一条 SUCCEEDED 审计。

## 坑

- 本地 `scripts/run_bag_cleanup_*` 是 test 环境驱动；处理机生产 8180 的排查一律直接对 `192.168.2.134:8180` API + 处理机 MySQL + A100 只读 ls，不要启动本地 worker 模拟生产数据。
- 生产库备份表留在 `rosbag_catalog` 库内（`*_backup_日期`），删除后告知用户表名供回滚；不要假设 timer 备份能覆盖到行级恢复。
- 页面弹框文案（如"无法执行的原因"）是 blockers 摘要；拿完整 path 列表走 GET 操作 API，不要靠截图猜。
- 改代码解除阻断（如让 plan 跳过软删记录）方向是错的：那是安全网不是 bug，DESIGN 契约 + 测试都钉死了；只走数据修正。
