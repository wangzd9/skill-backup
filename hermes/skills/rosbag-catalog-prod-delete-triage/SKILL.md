---
name: rosbag-catalog-prod-delete-triage
description: Use when 生产 Bag 删除受阻/报错，定位四卡点处置。
---

# 生产 Bag 删除受阻排查

## When to Use
- 用户报"Bag 清理/删除受阻/报错"，操作停在 BLOCKED、EXECUTE 被拒、或 DB_FINALIZING 反复回滚
- 预览弹 `UNKNOWN_STORAGE`、`cleanup session mismatch`、`flock held by another process`
- 仅 rosbag-catalog **生产环境** Bag 全量清理（self_built）。测试盘清理走 `a100-test-data-cleanup`，状态巡检走 `rosbag-catalog-status-check`

## 安全边界（必须遵守）
- 每个处置都是生产写操作：DB 物理删、改 run 状态、释放 A100 锁。**执行前必须 ①备份 ②磁盘/进程核实 ③用户当次明确授权**，不得自动盲跑
- 备份表命名 `*_backup_YYYYMMDD`（处理机 MySQL 内建）；物理删前用 `CREATE TABLE ... LIKE` + `INSERT ... SELECT` 留档
- 保留集（NAS final / NAS raw / 处理机本地 Phase0）在任何处置中绝不动
- 处理机 MySQL 容器名 `rosbag-catalog_mysql_1`（非 rosbag-catalog-mysql-1），密码 `docker exec rosbag-catalog_mysql_1 printenv MYSQL_ROOT_PASSWORD`

## 四个卡点决策树

### 1. 预览 BLOCKED：`UNKNOWN_STORAGE: ... not in approved matrix`
- **根因**：source 挂着 test 时代根（`/home/xmyf/wzd/data/self-built/*`）的历史 DB 记录，不在 production approved matrix（`/data/xl_*`）。常见于 test→production 迁移 + bag 换名（uid 不变 name 变）遗留
- **两张表都要查**（只清 source_copy 会漏 artifact，解阻后再弹第二轮）：
  - `source_copy WHERE source_id=? AND path LIKE '/home/xmyf/wzd/data/self-built/%'`
  - `artifact WHERE source_id=? AND path LIKE '/home/xmyf/wzd/data/self-built/%'`
- **软删无效**：DESIGN 明确"软删除索引不证明远端文件已不存在"，plan 核对刻意包含 deleted_at 非空记录（`test_soft_deleted_index_still_checked` 断言）。**只能物理 DELETE**
- 核实：A100 `ls` 确认这些路径磁盘不存在；备份后物理删；重新预览验证解阻

### 2. `cleanup session mismatch`（403）
- **根因**：该 source 已有活跃清理操作，且 session_hash 与当前浏览器 cookie 不匹配。**别处发起的操作会污染**——包括 agent 用 curl 排查时建的（cookie 在 agent 侧）
- 每个 source 同时只有一条活跃操作；`retry` 仅收 RETRYABLE_FAILED/NEEDS_RECONCILIATION，BLOCKED 不能 retry
- 处置：备份后物理删 `bag_cleanup_operation WHERE source_id=?`（含 BLOCKED/EXPIRED 僵尸），让用户浏览器重新点清理
- **agent 排查预览后必须清理自己 curl 建的操作**，否则用户页面报 mismatch

### 3. EXECUTE 被拒：`flock held by another process`（worker 每 ~2 分钟重驱动，不上报、状态停 REMOTE_DELETING）
- **根因**：孤儿写者 session 锁。`holder.sh`（续锁看门狗）的父进程死了被 init 收养（PPID=1），无限 `sleep 1` 续持 flock，永不释放
- **定位持锁进程**（A100）：
  ```
  for f in /home/xmyf/wzd/rosbag-cleanup-operations/locks/bag/production/*.lock; do
    h=$(fuser "$f" 2>/dev/null|tr -d ' '); [ -n "$h" ] && echo "HOLD $f: $h"
  done
  ps -o pid,ppid,etimes,cmd -p <pid>
  ```
- **确认是本 bag 的锁**：锁文件名 = `sha1("bag-baglock-v2|<env>|<bag_uid>")[:24]`，算一下比对
- **确认是死 session**（释放前必查，防删与真写入并发）：PPID=1、etimes 远超 ttl、A100 无活跃 self-built/phase 进程、session 目录无真实工作
- 处置：`touch <session 目录>/release`（holder.sh 检测到即 `RELEASED` 自行退出放锁）；30s 未退再 kill holder。worker 下一轮重驱动自动继续

### 4. DB_FINALIZING 反复回滚（状态不变、runs 数不减、worker 无错误日志）
- **根因**：`_finalize_deletion_guard` 命中 ACTIVE_RUN_EXISTS 409——存在非终态且非 FAILED 的僵尸 run（停在中途如 PHASE1_DONE，lease/auto_retry 均 NULL）
- **定位**：`SELECT pipeline_run_id,status,attempt,lease_expires_at,auto_retry_at FROM pipeline_run WHERE source_id=? AND status NOT IN ('PUBLISHED','FAILED','CANCELLED','QUARANTINED','NVIDIA_DONE','FINGERPRINT_CONFLICT','INPUT_INVALID','INPUT_UNSTABLE')`
- **cancel API 不适用**（只收 QUEUED_STATES）
- 处置：备份 run 行后 `UPDATE pipeline_run SET status='QUARANTINED', updated_at=NOW()`（QUARANTINED 是 PHASE1_DONE 的合法迁移目标之一、属 TERMINAL_STATES），并 INSERT 一条 pipeline_event 记录隔离原因。下一轮 FINALIZE 放行
- TERMINAL_STATES = {CANCELLED, FINGERPRINT_CONFLICT, INPUT_INVALID, INPUT_UNSTABLE, NVIDIA_DONE, PUBLISHED, QUARANTINED}

## 验证（声明 SUCCEEDED 前必须）
- DB：`pipeline_run/dataset_frame/source_copy/artifact/bag_record WHERE source_id=?` 全 0
- A100：计划内 PRESENT 目标 `ls` 全 ABSENT（空壳 staging 父目录残留属正常，不在目标内）
- 保留集：NAS final×N、NAS raw 目录仍在且内容完好（`docker exec rosbag-catalog-backend-1 ls -d /nas/...`）
- 审计：`bag_cleanup_operation WHERE source_id=? AND status='SUCCEEDED'` 恰好一条
- worker 日志无新错误

## 坑
- 80-run 历史 bag 常见（早期验收反复提交失败建 run），删除计划会列出 160+ ABSENT 对账目标，属正常，只有 PRESENT 的真删
- 远端删除成功但 DB 收尾失败时，worker 只重删 DB 不重删远端（幂等安全），不要误以为"删了一半"
- 改 run 状态 / 物理删 DB 行属生产数据保护边界，必须用户当次授权，不得以"研发测试数据"自决
- 排查顺序：先 curl 预览拿到 BLOCKED 的完整 blockers（`GET /api/v1/bag-cleanups/{id}`，带 agent 自己的 cookie），比让用户截图快
