# real 模式 run 误伤后的恢复顺序

适用：本地 real 栈（backend=real 镜像 + 两个 systemd 宿主机代理 `rosbag-catalog-test-phase0`/`rosbag-catalog-test-a100`，端口 18765/18766）被误操作打断——漏 `--mode real` 切回 mock、代理被停、运行中 run 被 `TASK_LEASE_EXPIRED` 标 FAILED——需要把数据面（A100 上已在跑的进程）和控制面（backend/orchestrator/代理）重新对回。

## 恢复顺序（乱序会卡死）

1. **先停 mock 侧污染源**：`docker compose --env-file /dev/null -p rosbag-catalog-local -f compose.yaml -f compose.local.yaml stop scanner orchestrator`，防止 mock 编排继续 claim/注入演示数据。
2. **移开 a100 代理的陈旧 recovery 文件**：`mv .local/rosbag-catalog/proxy-state/a100/<run_id>.json /tmp/`——里面持有可能已失效的 lease_token，代理重启会重放它（症状：`A100 callback failed ... original=HTTP Error 409`）。
3. **重建 real backend**：`docker compose ... build backend && up -d mysql backend`（compose env 里 `TEST_EXECUTION_MODE=${LOCAL_TEST_MODE}`，必须 `export LOCAL_TEST_MODE=real LOCAL_BACKEND_PORT=18080 LOCAL_WEB_PORT=8181 LOCAL_PHASE0_PROXY_PORT=18765 LOCAL_A100_PROXY_PORT=18766` 等与 `local_stack.sh` 头部一致的变量），等 backend healthy。`local_stack.sh up --mode real` 的 preflight `remote_activity` 会在 A100 上有孤儿进程时拒起，所以此时手工执行等价步骤而非跑脚本。
4. **处理 A100 孤儿进程（必须杀，否则 a100 代理 systemd 起不来——其 ExecStartPre 跑同一 preflight）**：杀前**必须**先把 `runs/nvidia/<run_id>/run.json` 的 `status` 改为 `FAILED`（sed 原地改即可），并删 `launcher.pid`、`launch.lock`。机制：`remote_nvidia_state` 对"pid 已死且 status≠FAILED"判 `EXITED` 直接抛失败；`archive_failed_state` 只在 status=FAILED 时归档旧状态——不标 FAILED 的话每个新 attempt 都秒失败在同一份陈旧状态，retry 多少次都一样。
5. **起两个代理**：`systemctl --user start rosbag-catalog-test-phase0.service rosbag-catalog-test-a100.service`，curl `/health` 确认 `active_runs: []`。失败时别只看 `systemctl status`——unit 的 ExecStartPre 跑 `local_a100_preflight.py`，其 `remote_repositories` 探针要求 A100 仓库 HEAD **精确等于** `config/local-a100-real.env` 里的 `A100_SELF_BUILT_REVISION`/`A100_NVIDIA_REVISION` 锚点；`update_a100_script_repositories.sh` 的 `pull --ff-only` 拿到新提交后锚点即漂移，服务起不来且 blocker 只回显仓库列表不点明原因。手动跑 preflight 对 `expected` 与 `actual` revision，锚点落后（A100 HEAD 是锚点的后代，reflog 里是正常 pull）则更新 env 锚点为 A100 实际 HEAD 再启动——**不要回退 A100 仓库**。注意该脚本的 origin 是 GitLab 上的 fork 仓库（`wangzhendong/dataset_build.git`），与开发机工作区 origin 可能历史分叉，`Already up to date` 不代表内容一致；改本地代理代码（开发机工作区）只需重启 service 即生效，与 A100 仓库同步无关。
6. **补 scanner/orchestrator/frontend**（real 模式不启 mock 代理容器）：`docker compose ... up -d scanner orchestrator frontend`。
7. **对两个 FAILED run 走 `POST /api/v1/pipeline-runs/<id>/retry`（`{"retry_mode":"normal"}`）**：NVIDIA 段 `--resume` 会从 run.json 记录的 step 续跑（下载/推理产物在 A100 盘上都在，不重做）；自建 run 若 Phase0 从未启动（`phase0-work/<run_id>/` 只有 ~100K 占位、step_run 无 Phase0 记录）则从头重跑 Phase0。

## real 数据测试的两种预期失败（不是环境故障，别当 bug 修）

- **自建 Phase3_NAV 失败：`planning.csv 缺列: ['sample_valid', 'task_context.routing_path_index']`**——原始 bag 的 `/planning/learning_sample` 话题 message_count=0（Phase0 `planning_done.json` 可证实），planning.csv 只有 4 个时间戳列。流水线如实拦截，正确处置是**换一个 planning 话题非空的 bag**，retry 原 bag 无意义。
- **NVIDIA `NVIDIA_ORIGIN_PUBLISH_CONFLICT: <nas path>`**——A100 侧全步骤已 DONE，最后发布 origin 备份到 NAS 测试根时目标目录已存在（此前批次发布的同 revision+chunk 备份），按不可变规则拒绝覆盖。处置：清掉测试根的旧备份后 retry（只重发，不重推理），或接受"数据已处理、未发布"收口。

## 发布校验失败（`*_VERIFY_FAILED: manifest_hash` 等）的诊断流程

三层对照，逐层排除，不要凭单次失败直接定性"瞬态损坏"：

1. **源端层**：到 A100 上按 `download_payload_manifest.json` 的 files 逐文件算 sha256（对照声明的 sha256），并用 `scripts/lib/manifest.py::local_manifest` 的**完全相同算法**（`path\tsize\tsha256` 排序后 join、排除 manifest 自身与合同文件）重算 `manifest_hash`。源端对得上说明数据没坏。
2. **传输层受控复现**：用管道同款参数复现一次——文件清单从 manifest 取（`sorted files[].path`），rsync 参数与 `a100_host_proxy._rsync_from_remote` 一致（`-a --delete --partial --files-from=<file> --prune-empty-dirs --delete-excluded -e ssh`）。**清单务必先落文件再用 `--files-from=<path>`**：脚本里 stdin 管道传清单极易被其他命令串扰（曾出现 filelist 只剩 1 行、rsync rc=0 但 0 字节落地的假成功）；复现脚本跑完先看"filelist 行数 == 声明文件数"和落地 `du` 再信 rsync 的 rc=0。
3. **NAS 存储层**：复现落地后隔 30s 重扫一次 hash，两次一致且等于声明值 → 存储稳；CIFS 慢（扫 14G 要分钟级）用后台跑，别前台卡死。

判读：源端+复现+存储都对而管道失败 → 失败窗口内链路退化（CIFS 慢窗口内连续两次传输都坏是可能的，自动重传只兜单次），等链路恢复后 retry 即可，**不要放宽校验、也不要无限重试**；失败时间线与 `local_stack.sh up`/代理重启重合 → 重启 kill 了 rsync 子进程，incoming 留半成品后校验自然不过，按本文件恢复顺序处理。证据链习惯：`_rsync_and_verify` 失败会 rmtree 清空 incoming（残留为空不是"没传"）；报错是 `manifest_hash` 而非 `file_count`/`total_size` 说明文件数和总大小都对、只有内容错——这是真损坏签名，别与"传了个空目录"（那会报 file_count）混淆。

## 核验清单

恢复后逐一对：backend `TEST_EXECUTION_MODE=real`、两代理 `/health` ok、A100 上有对应 `run_*.py` 进程（`ps aux | grep run_nvidia_chunk`）、run 状态离开 FAILED 且 attempt+1、`step_run` 有新进展。全部对上才向用户报"已恢复"，任何一项缺失如实报残留。