---
name: processing-machine-host-triage
description: "处理机宿主机排查：桌面弹窗、apport 崩溃、GPU/systemd、sudo 运维。"
---

# 处理机宿主机故障排查（容器外）

## When to Use

用户报告处理机（`dataprocess-Legion-Y9000P-IAX10H`，Ubuntu 22.04 + GNOME）宿主机层面的问题：桌面弹窗、卡死/黑屏、驱动异常、systemd 服务异常，或需要在处理机上执行需 sudo 的运维操作。流水线状态走 `rosbag-catalog-status-check`，部署走 `rosbag-catalog-processing-deploy`，本技能只覆盖宿主机本身。

## 连接与 sudo

- SSH：`ssh -i ~/.ssh/id_ed25519_processing -o IdentitiesOnly=yes -o BatchMode=yes processing-machine`（用户 `data-process`，**无免密 sudo**）。
- sudo 密码在 hermes 凭据环境变量 `PROC_MACHINE_SUDO` 里，用 stdin 投递，不要把密码写进命令行参数（会留在进程列表/日志）：
  `printf '%s\n' "$PROC_MACHINE_SUDO" | ssh ... 'sudo -S <cmd>'`（`-S` 从 stdin 读密码）。
- 后台 terminal 触发审批的命令若 5 分钟无人在线会被 BLOCKED：需 sudo 的写操作用前台调用，或先向用户请示。

## 桌面"对不起，XXX 出现了内部错误"弹窗 = apport 崩溃报告框

这是 Ubuntu apport 的崩溃对话框，不是桌面/显卡故障本身；真正根因在崩溃程序里。诊断步骤：

1. `ls -lt /var/crash/` 找最近的 `*.crash`（时间应与弹窗时间吻合）；文件名即崩溃程序路径（如 `_usr_bin_docker-compose.1000.crash` = `/usr/bin/docker-compose`）。
2. 解析堆栈（.crash 是文本，Apport 格式）：`python3` heredoc 读文件，打印 `ProblemType/Date/Title/Package` 行和 `Traceback` 段——根因通常在 traceback 最后一行（如 `KeyError: 'ContainerConfig'`）。
3. 交叉验证谁在调它：`journalctl --since <窗口> --no-pager` 过滤该时间点，配合 `crontab -l`、部署/巡检脚本的探测逻辑（如 `docker compose` 缺失时回退 `docker-compose`）。
4. 修根因（通常是升级/重装该程序），**不要只关弹窗**：崩溃源还在就反复弹。修完 `sudo rm -f /var/crash/<file>` 清掉已处理的报告。
5. 顺手核验桌面健康度排除误判：`nvidia-smi`（GPU 无 Xid、温度功耗正常）、`journalctl -p err --since "-2 days"`、gnome-shell/Xorg 无崩溃记录。`journalctl` 里高频重复的良性日志（如内核 `nvidia: Enabling HDA controller` 每分钟一次）会淹没真正的错误行，grep 时要排除。

## 自建 Phase0 任务卡死 = 处理机本地 GPU 故障（extract_cameras/xlbp_parse 空转）

自建 run 停在 `PHASE0_RUNNING`、`extract_cameras`/`xlbp_parse` 进程活着但零产出时，先怀疑**处理机本地那张 GPU**（RTX 5080 Laptop，Phase0 相机提取 `--gpu 0` 依赖它），不是 A100（A100 的 VLM 走 8002 SSH 隧道到远端，本机 nvidia-smi 全空是常态，别把两台的 GPU 混淆）。

判定特征（进程层）：`xlbp_parse` 主线程 wchan=`hrtimer_nanosleep`、18 个 `jpeg_XX` 工作线程全部 `futex_wait`（有队列没活）；CPUTIME 数小时几乎不动；6 路相机输出目录 0 张 jpg；最近 N 分钟无文件写入。
判定特征（GPU 层）：处理机 `nvidia-smi` 显示本地卡 Fan `ERR!`、Temp `ERR!`、Pwr/Util `N/A`、卡在 P8、显存只挂 ~66MiB、`No running processes found`——硬件/驱动层故障。卡给不出 compute，extract_cameras 就死等空转。
单槽阻塞：phase0 proxy（`http://127.0.0.1:8765/health` 的 `active_runs`）是**串行单槽**，一个卡死任务占着会让后续所有自建 run 排不上——DB 停在 `PHASE0_QUEUE`、事件在 `CLAIMED→CLAIM_RELEASED` 循环。诊断：`/health` 看 active_runs，`ps -ef | grep <run_id>` 看进程树 etime/CPUTIME 判断是真跑还是空转。

处置（生产硬件操作，需用户当次授权 + sudo，agent 只诊断不擅动）：
1. 释放槽位（用户授权后）：逐级 kill 卡死进程树 `scheduler_phase0 → extract_cameras → xlbp_parse`（xlbp_parse 父进程死后变孤儿、需补杀）。该 run 落 `FAILED`（`exit code -15`），GPU 修好后可重跑。
2. 修 GPU：`printf '%s\n' "$PROC_MACHINE_SUDO" | ssh ... 'sudo -S dmesg' | grep -iE 'nvidia|Xid|gpu'` 查故障码，再决定 `nvidia-smi --gpu-reset` / 重启驱动 / 重启机；GPU 恢复前 Phase0 无法推进。

## 已知环境事实

- 处理机唯一 GPU 是本地 RTX 5080 Laptop（Phase0 相机提取用），与 A100 的 8 卡是两台机器的不同 GPU；本地卡故障会让 Phase0 空转，诊断与处置见上文。
- 开发机（`wang` 主机）是同款 RTX 5080 Laptop；黑屏诊断（Xid 62/PMU 停摆/SBIOS 诱因）与 NVIDIA 驱动版本切换/修复的完整流程在 `nvidia-driver-ops` 技能，两台的 GPU 故障签名通用。
- Docker Engine 29.x + compose v2 插件（5.5.1）是处理机标准 compose 路径；老版 docker-compose v1.29.2 与 Engine 29 不兼容（`ContainerConfig` 字段缺失导致重建容器时 KeyError），仅作历史参考，不应再被任何脚本回退使用。
- 处理机开着 unattended-upgrades：装系统补丁时重载无线网卡驱动可断网 2 分钟以上，足以让流水线心跳超时误判 FAILED（细节见 `rosbag-catalog-status-check`）。
- NAS 走 CIFS 挂载（192.168.2.77），断网/重启后可能需重连，内核日志 `CIFS: VFS: ... has not responded` 是信号。

## 规则

- 宿主机写操作（apt 安装/升级、删文件、重启服务、改 /etc/default）必须用户当次明确授权；只读排查可直接做。
- 涉及 rosbag 部署目录、systemd 代理、数据库的修改仍受 `AGENTS.md` 生产数据保护约束：本技能只诊断，改动走对应专项流程。
- 修复完成后验证"症状消失路径"（如端到端跑一次原来触发崩溃的命令，或 `config`/`version` 探针），并在 HANDOFF 记录根因与修复。
