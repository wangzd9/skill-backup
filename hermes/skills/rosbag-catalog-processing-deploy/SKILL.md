---
name: rosbag-catalog-processing-deploy
description: "授权后发布 rosbag-catalog 到处理机：干净工作区部署、代理重启与本地收敛。"
---

# rosbag-catalog 处理机生产部署闭环

## When to Use

用户当次明确授权 rosbag-catalog 生产部署（"升级生产环境/发布/部署处理机"，且 changelog 有 approved 条目待发布）时。命令基线与故障表见仓库 `.agents/skills/processing-machine-deployment` 与 `docs/changelog-workflow.md`；本技能记录仓库文档未写明、本机会实际踩到的执行坑。工作目录 `/home/wang/work/rosbag-catalog`。

## 前置：changelog 批准与发布（用户指定编号时）

1. `approve <F-xxx> --user-approved` 后 `deploy-check` 必须通过（所有 approved 条目要么编入本次 release，要么 hold 回 pending；遗漏会拒绝部署）。
2. release 用 `--revision <目标 commit>` 与 `--released-at "$(date -Iseconds)"`；`--entry` 可重复指定编号做选择性发布。
3. 批准 commit 与 release commit 分开提交（状态目录 `changelog/pending|approved|archive` 各自为一批），再 `git push github main`。release 的 `--revision` 用批准 commit 的 sha（不含 release 自身的 changelog 提交），归档批次名形如 `changelog/archive/<released-at>-<revision>/`；随后部署基线应是 release 推送后的 main 新 tip，需要重建 worktree 检出新 tip 再跑部署（worktree 里做 release 提交后原检出已落后）。
4. 用户批准 changelog 不等于授权生产部署——两者是独立决策，缺部署授权时条目停在 approved。

## 部署执行

- **部署前看 `git status --short`：工作区有他人在改的未提交文件时，不得直接从主工作区 rsync 部署**（部署脚本同步的是工作区内容，不是 HEAD）。做法：`git worktree add /tmp/rc-deploy-wt HEAD`（detached，只读同步用），在 worktree 里跑 `./scripts/deploy_processing_machine.sh --apply --production`；部署前 `ln -sfn /home/wang/work/rosbag-catalog/.venv /tmp/rc-deploy-wt/.venv` 供脚本内的本地测试/local_stack 用。**worktree 删除时机**：部署闭环后、且在从该 worktree 启动的后台 OCR 审查**完成之后**再 `git worktree remove --force && git worktree prune`——OCR 审查期间 `git show`/`git grep` 都 chdir 到工作区，中途删掉会让审查降级（工具调用全失败）且 session 文件不可再取，只能靠日志尾部结论。
- 部署脚本远超 600 秒前台上限（同步+两镜像 build+远端健康+本地 up/test），必然被转后台：直接 `background=true` 跑，日志重定向到 scratch，轮询日志尾部看 `部署闭环完成` 行与 exit code。
- 处理机已装 compose v2 插件（`docker compose` 5.5.1，2026-09-28 安装），脚本探测优先走 v2，不会再回退老版 docker-compose v1。若部署日志再出现 compose traceback 或 `KeyError: 'ContainerConfig'`，说明插件环境被破坏，先 `ssh processing-machine 'docker compose version'` 核实再判断，不是已知良性现象。
- **部署 unhealthy 时先读 `docker logs rosbag-catalog-backend-1` 的报错行再下结论**：backend 启动命令是 `alembic upgrade head && uvicorn`，迁移 DDL 失败（1101/1071/OperationalError）会在 alembic 阶段崩、容器反复重启——这不是你本次改动的回归，而是被带上的已提交 schema 问题；DB 停在失败迁移前的 `alembic_version`，且 create_table DDL 阶段失败不留半截表。修复后重新部署即从旧 version 干净追平。
- 脚本步骤 [7/7] 的本地 up/test 在 worktree 目录的 `.local` 下执行（会留下临时本地栈状态）：闭环后在**主工作区**再跑一次 `./scripts/local_stack.sh up && ./scripts/local_stack.sh test` 把常驻本地栈收敛到生产同版本，确认 `stale=false`，然后清掉 worktree。
- 部署脚本只重建容器：**宿主机 systemd 代理（`rosbag-a100-proxy`/`rosbag-phase0-proxy`）不会被更新**。部署后必须 kill 两个代理 MainPID（`systemctl restart` 需交互 sudo；kill 后 `Restart=always` 自动拉起，无 sudo 可行），等新 PID `active` 后核对 `/health` 返回 `active_runs=[]` 且 `/proc/<pid>/environ` 无残留测试变量（如 `NVIDIA_TEST_CLIPS`）。用户授权话术里"启动最新代理"即指此步。代理属 data-process 用户、部署脚本本身就以 data-process 跑 SSH：优先直接 `kill $(systemctl show -p MainPID --value <unit>)`，**不触发审批**；`printf 密码 | sudo -S` 路径会走审批且用户不在线 5 分钟即 BLOCKED，留作 kill 被拒时的回退。
- **回滚到旧 commit 前必须手动清处理机 deploy 目录残留**：部署 rsync 不带 `--delete`，deploy 目录里比目标 commit 多出的文件（尤其是 alembic 迁移）会留在原地并被打进新镜像——回滚镜像变成"旧 models + 新迁移"的混合体，照样崩，且报错让你误判回滚无效。正确顺序：`ssh processing-machine` 对比 `git ls-tree --name-only <目标sha> -- alembic/versions/` 与 deploy 目录实际迁移文件，删多出的迁移后部署；回滚目标必须选在坏迁移**首次合入 main 之前**的 sha（`git merge-base --is-ancestor <坏迁移首提交> <回滚sha>` 反查）。**前进部署（重部署到修复后的后代 commit）不需要此清理**：rsync 同路径覆盖即可顶替坏文件，修复部署直接走正常流程。

## 部署前迁移闸门（backend 启动跑 `alembic upgrade head`）

- 本地 pytest 是 **SQLite**：MySQL 8.4 专属 DDL 不兼容全绿也拦不住。历史两类：`TEXT NOT NULL server_default(...)` → 1101（MySQL 拒绝 BLOB/TEXT/JSON 列带 DEFAULT）；宽 VARCHAR/TEXT（utf8mb4 4 字节/字符）进唯一索引超 3072 字节 → 1071。凡 main 上含你未审过的新/改 alembic 迁移，部署前必须在**隔离 MySQL 库**（与 compose 同版本 `docker.m.daocloud.io/mysql:8.4`）实跑 `alembic upgrade head` 验证。起临时 docker 容器若被审批卡住，退路是部署脚本 [7/7] 本地栈收敛（全新 MySQL 8.4 建库）——它同样会在坏 DDL 上崩，但崩在部署中途。
- 修复模式（改的是他人已合入的迁移时同样适用）：1101 → 去 `server_default` 保留 `nullable=False`，插入值由 model 的 Python `default` 保证（与 DB 无关）；1071 → 唯一约束移到**应用层计算的哈希列**（`String(64)`，插入点算 `hashlib.sha256(path.encode()).hexdigest()`），**不要用 DB 生成列**：本地栈/测试的 SQLite 无内置 `SHA2()`，`create_all` 会红。修后必过：隔离 MySQL 库实测 DDL + 建表/插入、SQLite 全量测试、ruff。
- worktree 里跑部署脚本，[1/7] 本地验证可能有 **devflow 沙箱测试伪失败**（worktree 的 `.git` 是指向主仓库的文件而非目录，bwrap 命令断言不符；同代码在主工作区全绿）：主工作区已跑过全量测试时，worktree 部署加 `--skip-local-tests`；它只跳部署前重复测试，[7/7] 部署后本地栈收敛（真实 MySQL 8.4 闸门）仍执行，不能靠它免检 schema。
- main 是多会话共享主干：部署 HEAD 等于继承并行会话已合入的全部 schema。部署前 `git log <上次部署sha>..HEAD -- alembic/versions/ app/models.py` 扫一眼迁移变更面，有新迁移就走闸门，不要假设"我没动 schema"。

## 部署后验证（全部过才报完成）

- 处理机：backend/mysql/frontend `Up (healthy)`、`http://127.0.0.1:18000/health/ready` 与 `http://192.168.2.134:8180` Web `environment=production`。
- 本地：主工作区 `local_stack.sh test` 全链路 PUBLISHED、revision 对账 `changed=[] stale=false`，本地 platform revision 等于生产。
- **部署源不一定是 main**：`--apply --production` 同步的是**当前工作区**内容。工作区检出于功能分支（或别人的 worktree）时，生产会跑一个任何远端都没有的提交，而 main/origin 仍停在别处——这会让后续所有「生产 = main = 已发布」的推断失效。部署后必须同时记录三项身份：工作区 `git rev-parse HEAD`、deploy 目录特征文件的 sha256、main 与 origin/main 的 tip；生产版本不在 main 线上时，明确写进 HANDOFF 与汇报，不要只写一个 sha。`.deployed-git-revision` 只记录写入意图（会被后续同步/回滚覆盖），核实生产真实内容一律用特征文件 sha256 对账，方法见 `rosbag-catalog-status-check` 技能的多环境版本对账参考文件（env-version-reconcile）。
- **`local_stack.sh status` 的 `stale=false` 不等于本地栈已收敛到目标版本**：它只对比主仓记录值与容器声明的 revision；本地栈被其他 worktree `up` 过之后，容器实际跑的是那个 worktree 的代码，status 仍稳定报旧值（页面渲染出的版本号也只代表 backend 容器所在目录）。声称「本地已收敛到生产同版本」前，逐容器查 `com.docker.compose.project.working_dir` 并在容器内对关键文件 sha256 对账。
- **有滞留 run 待收尾时（如 A100 已 DONE 但平台 FAILED）：新代码上线后对 run 发普通 retry（`X-Idempotency-Key` 带日期后缀），挂监视器到终态并核对 `artifacts` 列表补全——产物登记 422 类修复只有在 attempt N+1 真实走完收尾才算验证通过，不是"已部署"就算修好。**部署后滞留 run 的首次 retry 是 A100 sshd 限流的高风险窗口**（帧导入步骤发数千条 SSH）：若失败签名是"数据缺失类"（如 `infer 下没有 clip 目录`），先按 `rosbag-catalog-status-check` 第 8 条做 SSH 连发探针排除掩盖式瞬断，不要当新代码回归反复改码重部。
- **重新生成部署快照**：`./scripts/check_processing_machine.sh > /tmp/pm_check.txt && python3 scripts/generate_deployment_snapshot.py --check-output /tmp/pm_check.txt`。`docs/deployment-snapshot.md` 是部署版本（平台 revision、Alembic 迁移、A100 脚本锚点）的唯一事实来源，由脚本生成、禁止手工编辑；漏了这步，架构/状态页之外的 Agent 会继续引用过期版本。
- HANDOFF 顶部记录：处理机 revision、本地 revision、代理新 PID 与启动时间、changelog release 编号与 commit、滞留 run 的收尾结果、快照已重新生成；单独 commit 并推送。

## 常见陷阱

- 处理机桌面弹"出现了内部错误"是 apport 崩溃对话框，不是桌面/显卡故障：`/var/crash/*.crash` 文件名即崩溃程序，读 Traceback 定位（如 docker-compose v1 与新 engine 不兼容 → 装 `docker-compose-plugin`），修复后清掉崩溃文件。需 sudo 的 OS 级维护（apt install 等）：data-process 无免密 sudo，用凭据 env `PROC_MACHINE_SUDO` 经 `printf '%s\n' "$PROC_MACHINE_SUDO" | ssh processing-machine 'sudo -S ...'` 传入。
- 直接主工作区部署把他人未提交改动带上生产——生产目录只允许已提交版本。
- worktree 没链 `.venv` 时脚本 [1/7] 本地验证直接失败退出，远端未动，补软链重跑即可。
- 代理重启漏掉 phase0（只重启 a100）：两代理跑同一份 deploy 目录代码，部署后都要拉起新进程。
- 把"容器已更新"当"代理已更新"：代理是宿主机进程读 deploy 目录脚本，只有重启后才加载新代码；不重启则生产仍跑旧逻辑（历史事故：测试变量残留、422 修复不上线）。
- **a100 代理 kill-MainPID 重启会按 recovery state 自动恢复上一 run**（attempt 数可能 +1、甚至直接重跑步骤），随后再发手工 retry 会再 +1——"我以为是 attempt N"与响应里的 `attempt` 字段对不上属正常。一切以 retry 响应 JSON 返回的 `attempt`/`status` 为准，监视器也按该值过滤步骤行；若自动恢复的那次 attempt 跑在**旧代码**上失败（部署+重启之间窗口的恢复），属预期损耗，手工 retry 一次即由新代码接管。
