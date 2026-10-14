---
name: rosbag-catalog-prod-test-run
description: Use when 生产环境用新记录测数据流程，隔离已有 PUBLISHED 产物。
---

# 生产数据流程测试 run 提交

适用：用户要求"以生产环境为准，用 X 数据集对整个流程测试"。X 可能是已有自建 bag（要重跑全流程）或英伟达 chunk（可能是新/不存在的数据集）。工作目录 `/home/wang/work/rosbag-catalog`。全程处理机直连 `ssh -o IdentitiesOnly=yes -o BatchMode=yes -i ~/.ssh/id_ed25519_processing processing-machine`，API 走本机 `http://127.0.0.1:18000`；A100 走 `a100-wzd`（密钥 `~/.ssh/id_ed25519_a100_wzd`）。

## 隔离口径（用户强约束，最高优先）

已有 PUBLISHED 数据**只能原封保留**；"做区分"= 给**新的测试数据/记录**加后缀，**不是**给旧 run 的 A100 产物改名让路。流水线产物路径按 bag 名硬编码（infer/origin = `{root}/{bag_name}`），所以唯一干净的做法是让测试数据本身成为新名字的新记录：

- 自建 bag：NAS ready 根下 `cp -a <bag_dir> <bag_dir>.test`（md5sum 双端校验；NAS CIFS 慢，长 timeout）→ 新 bag 名 = `{原名}.test`，后续所有 A100 产物路径自然带 `.test`，与旧 run 零交集。
- 禁止：把旧 run 的 origin/infer `mv` 走再让新 run 写原路径（旧 artifact 指向悬空，违反隔离要求）；也禁止把旧产物改名当"区分"手段（方向反了）。
- 一旦误动过旧产物：立即 `mv` 还原原位（同文件系统瞬时），再走正确的隔离路径；如实汇报误操作。

## 登记新 bag 记录

`POST /api/v1/sources/register` body `{"path":"/public/...ready_rosbags/<日期>/<bag>.test"}`（backend 容器内 NAS_STORAGE_ROOT=/public；物理挂载在 /nas）：

- **内容相同的副本绝不传 `fast_fingerprint`**：register 的指纹分支会按 fast_fingerprint 匹配到**原 bag** 并改写其 name/relative_path，污染原记录。不传指纹则走 upsert_bag 按 relative_path 去重 → `.test` 是新路径 → 新建独立 bag + 自动建 `PHASE0_QUEUE` run。
- 指纹在 scanner 容器算：`/app` 下 `from app.services.fingerprint import fingerprint_bag`（不是 app.scanner.fingerprint）。
- 提交前核实 backend 能读该路径（`from app.scanner.clients import nas_client` list_dir + download_text metadata.yaml）。

## 提交 run 的队列语义（易错）

- 全新记录（无任何 Phase0 产物）按默认队列提交：`POST /api/v1/pipeline-runs` `{"source_id":"<bag_uid>","note":"用户授权: ..."}` 不带 queue_name（默认 PHASE0_QUEUE：宿主机 Phase0 → A100 Phase1/3 → 发布）。
- **手动指定 `SELF_BUILT_QUEUE` 会让全新 run 秒失败** `INPUT_INVALID: PHASE0_LOCAL source copy is missing and A100 origin is unavailable`（SELF_BUILT_QUEUE = 已有 Phase0 产物、从 A100 起步；新 run 本地无副本、A100 无自己的 origin）。重试已有 run 用 retry API（服务端自选队列），不要手动挑。
- source_id 是 BagRecord.bag_uid（26 位 hex 大写）：**从 DB 读出再拼进 curl**，手抄易错（同形字符），且 bash 变量名避开只读的 `UID`（用 `SBUID` 之类）。
- NVIDIA chunk：`POST /api/v1/nvidia/chunks` `{"chunk":N}`（先 `/chunks/preview` 看 can_create/code_revision）。"A100 本地无此 chunk" ≠ 数据不存在——DOWNLOAD 步从 HF 镜像拉取，以真实结果为准。

## 长任务监控（cron + monitor 门控）

- 写确定性 monitor 脚本到 `~/.hermes/scripts/patrol_monitor.sh`：只打印 `nvidia=<status> self=<status>` 粗粒度状态（无时间戳、无 error 细节；ssh 失败打 `READERR`）——monitor 按精确字节 hash 门控，带时间戳会每 tick 误唤醒 agent。
- `cronjob_manage action=create`：schedule `every 30m`、monitor 传**相对文件名**（必须放在 `~/.hermes/scripts/` 下）、prompt 里写明 run_id、终态集合（自建 PUBLISHED/FAILED/CANCELLED/EXPIRED；NVIDIA 含 NVIDIA_DONE）、到终态后的动作（抓 steps/events/StepRun 全文 → 核对 A100 产物与隔离完整性 → 删本 cron job → 中文汇报）。deliver origin。
- 不要用 background terminal 盯数小时任务：timeout 600s 上限、nohup 被禁、scratch 24h 清理。
- 多层 SSH 测连通性时远程循环变量用 `printf "...echo pong..." | sh` 方式，别在双引号 ssh 里塞 `$i`（外层展开/丢失 → 假 0/N）。

## 汇报纪律

- "在跑"必须先实证：DB 状态 + 进程 CPUTIME 增量 + 产物文件计数。小时级 etime 配分钟级 CPUTIME = 空转，按 Phase0 空转诊断（`rosbag-catalog-status-check` 专节）下钻，GPU 故障走硬件处置、需用户授权。
- 测试结束交付：终态 + 关键步骤证据 + 隔离核验（原 bag 记录与 PUBLISHED 产物未动，md5/路径对照）+ 测试数据（.test bag、A100 `.test` 产物、NAS final 新目录）的去留建议。
