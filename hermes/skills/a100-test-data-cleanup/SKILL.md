---
name: a100-test-data-cleanup
description: Use when 清理 A100 测试盘 rosbag-catalog 数据，对照 run 终态删可清工作副本。
---

# A100 测试环境数据清理

## 适用条件
- 用户要求清理 A100 **测试盘**（`/home/xmyf/wzd/test/rosbag-catalog`，`/dev/sda2`，878G）空间。
- 不适用：生产根 `/data/xl_*`、`/data/nvidia_*`、`/data/nvidia_infer_dataset` 等 `/data` 下路径——一律不碰（生产数据保护规则）。

## 连接（开发机执行）
```bash
ssh -i ~/.ssh/id_ed25519_a100_wzd -o IdentitiesOnly=yes -o BatchMode=yes -p 60022 xmyf@a100-wzd '<命令>'
```
注：`-p 60022` 是测试隧道端口；生产代理是 8766。A100 本地 `curl :8766` 健康检查是**生产**代理，与测试无关。

## 清理前必查（四项，缺一不动手）
1. **本地测试 A100 代理活动 run 为空**（开发机）：`curl -s http://127.0.0.1:18766/health` → `active_runs: []`。
2. **没有正在执行的验证/复现脚本持有数据**：开发机 `ps` 查 `sort_fix_verify`、`stdin_repro` 等 /tmp 脚本；这类脚本常读 `nvidia_origin_dataset`，删了会中断且复现成本极高。
3. **GPU 训练/推理进程的数据路径**：`nvidia-smi --query-compute-apps=...` + `ps -ef | grep torchrun`。关键教训：**训练进程可能只碰生产根 `/data/nvidia_*`，与测试盘同名目录（`test/.../data/nvidia_infer_dataset`）是不同路径**——必须看进程 args 里的绝对路径判断，不能只看目录名相同就停手，也不能没核对就删。
4. **本地 DB run 终态对照**（开发机，`MYSQL_PASSWORD` 从 mysql 容器 env 取）：
   `SELECT pipeline_run_id,status,attempt,pipeline_type,source_identity FROM pipeline_run ORDER BY updated_at DESC`

## 数据目录语义（`/home/xmyf/wzd/test/rosbag-catalog`）
- `data/self-built/{origin,infer,phase3}/<bag 或 run_id>`：自建流水线**工作副本**。run 到终态（PUBLISHED/FAILED-不可重试/NAS 已有源）即可整组删。
- `data/nvidia_origin_dataset/data_sample_chunkNN`：**源数据**，默认保留（重试与验证依赖它）；仅用户明确要求时删。
- `data/nvidia_infer_dataset/data_sample_chunkNN`：推理产物，NVIDIA_DONE 且已备份 NAS 后可删。
- `runs/`、`maps/`、`staging/`：日志/中间态，体量小，一般保留。
- 磁盘分布先 `du -sh data/*` → 再 `du -sh data/self-built/*/`，按目录定位大头。

## 删除
- 一条 ssh 内 `set -e` + 逐个显式 `rm -rf <完整路径>`（不写 `data/*` 通配），路径清单在提交前与终态表逐项核对。
- `rm -rf` 大目录（100G+）会超 600s 前台上限 → terminal background + notify 跑。
- 删除命令被审批拦截（BLOCKED）≠失败：5 分钟无人回应会放弃执行，先确认审批结果再判断。

## 删后复核
1. `df -h /dev/sda2`：已用率应明显下降（基线记录在 HANDOFF，如 2026-09-26 89%）。
2. `du -sh data/*`：确认保留项（origin_dataset）仍在。
3. 训练进程仍正常（若之前有）：`nvidia-smi` 进程数不变。
4. 更新 HANDOFF 磁盘基线数字。

## 停止条件
- 目标目录删除完成 + df 余量满足用户预期即停；不顺手清 runs/maps 等未点名目录。
