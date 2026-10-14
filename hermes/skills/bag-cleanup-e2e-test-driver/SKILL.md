---
name: bag-cleanup-e2e-test-driver
description: Use when 跑 rosbag-catalog bag-cleanup E2E 驱动测试（本地栈/A100 删除）.
---

# Bag Cleanup E2E 驱动测试

驱动入口（仓库 `scripts/`，均已提交可直接复用/改造）：
- 本地栈（local transport 真删临时树 + 无头 Chrome CDP 全 AC）：`run_bag_cleanup_browser_test.py` + `bag_cleanup_browser_test.mjs`
- 真实 A100 测试根（ssh transport 真删 A100 夹具）：`run_bag_cleanup_real_a100_test.py`

## 前置装配（A100 真实层）
1. A100 端 mini repo：`<test_root>/bag-cleanup-exec/` 放 `scripts/bag_cleanup_executor.py` + `app/services/remote_bag_lock.py`（executor 自带 sys.path 引导，remote_bag_lock 纯 stdlib）。不动 A100 上的 Git 仓库；python 用 A100 venv（`envs/rosbags/bin/python`）。scp 后先跑一次 import 冒烟。
2. 夹具：按 `_matrix_paths(bag_name, run_id)` 在 5 个 approved roots 下建唯一 tag 目录（每目标 1 文件，内容=tag 便于审计）。**启动前先清旧 tag**——崩掉的驱动不会跑 finally，夹具必然残留且会混进 `ls`。
3. worker env 冻结 7 参数：`BAG_CLEANUP_EXECUTOR_TRANSPORT=ssh`、`BAG_CLEANUP_REMOTE_EXECUTION_ENABLED=1`（仅用户当次明确授权真实删除后才开）、HOST/USER/PORT/KEY、`BAG_CLEANUP_EXECUTOR_REMOTE_CMD="cd <exec_dir> && BAG_CLEANUP_AUTHORIZE_SECRET=<hex> <python> scripts/bag_cleanup_executor.py"`、OPERATION_ROOT。REMOTE_CMD 经远端 shell 执行，`&&`/`VAR=x cmd` 前缀都合法（secret 是 hex，无 shell 元字符）。
4. **两道门都满足才删**：`BAG_CLEANUP_REMOTE_EXECUTION_ENABLED`（EXECUTE 是否触发）+ `BAG_CLEANUP_AUTHORIZE_SECRET`（worker 签发短期 token、远端 executor 验签，两侧必须同一值）。INSPECT（读）无 secret 也放行；EXECUTE 缺 secret fail-closed 拒删——看到 `execute 被拒：ssh transport 缺少 BAG_CLEANUP_AUTHORIZE_SECRET` 是门禁正常工作，不是 bug。secret 内存生成，不落盘不进日志。
5. backend 子进程的 env 必须在**首次 import app 之前**设置（settings 单例在 import 时按 os.environ 冻结），否则 seed/preview 写到错误配置，表现为 source not found。

## API 驱动契约（踩坑最多的部分）
- preview POST 返回 **202**，且**只在 preview 响应下发会话 cookie**。后续 preview 必须携带首个 preview 拿到的 cookie，否则每个 op 各绑独立 session——用 A 的 cookie 读 B/C/D 全 403，wait 循环静默挂死（无日志、进程 `do_select`、fd 表无 socket，极难诊断）。断言 cookie 非空后再继续。
- confirm/retry body 必须带 nonce：先 GET `?regenerate_nonce=true` 并断言 nonce 非空，否则 422（string_type）。confirm/retry 也是 **202**。
- **preview 临 confirm 前才发**：preview 计划 TTL = `bag_cleanup_confirm_ttl_seconds`（默认 300s）。多 op 串行场景（op1 真删 ~2min + op2 部分失败+retry ~4min）后发 op3 的 confirm 必撞 `PLAN_EXPIRED: preview no longer valid`。批量预发 preview 只适合"发完立即 confirm"的单 op 流程。
- **没有 cancel API**：前端"取消"=关弹窗，行留 PREVIEW_READY 永不执行；终态断言是"文件 5/5 保留 + 状态仍 PREVIEW_READY"。
- retry 会撞 **409**（worker 瞬时 REMOTE_DELETING 持租约对账中）：用有界碰撞循环——re-GET，等 5s，成功即 break，给 120 次上限；不要单次 retry 后硬断言。循环两条规则：① GET 本身非 200（瞬时 500 / cookie 过期 401）必须当等待分支处理——拿错误体 `{detail:...}` 去构造 retry 会 `KeyError plan_digest` 掩盖真实故障；② **`NEEDS_RECONCILIATION`（目标 UNKNOWN）和 `RETRYABLE_FAILED`（目标确定 FAILED）都允许 retry**，只认前者会让后者烧满整个循环上限后误报"retry 未受理"。
- 等待预算：claim 租约 ttl=120s 硬编码，同一 cleanup 阶段切换有 ~120s 死区 → 各阶段等待 ≥240s；不同 cleanup 不受限。

## writer 锁互斥构造（D 场景）
- **持锁用前台阻塞 ssh，不用后台**：驱动 daemon 线程跑 `ssh <host> "mkdir -p $(dirname <lockfile>) && flock -x <lockfile> -c 'sleep N'"`（不退出）。后台方案（`& echo $!`）有两重实测缺陷：`$!` 是后台**子 shell** 的 PID 而非 flock（kill 子 shell 后 flock 变孤儿继续持锁）；且 `flock -c` fork 的 sleep 子进程继承锁 fd，只杀 flock 时 sleep 仍持锁，fuser 永远报占用。
- 定位真 flock：扫 `/proc/*/cmdline`（`head -c 200 | tr '\0' ' '`）匹配 `^flock -x <lockfile>`（锚定开头，否则 sshd 派生的 bash -c 命令行也含该字符串，需排除 `$$`/`$PPID`）。释放 = 杀该 PID **加其 `/proc/<pid>/task/*/children` 全部子进程**。
- 释放必须是 **kill→fuser 校验→未 free 再杀**的循环（30~90 次）：A100 sshd 偶发 kex 风暴，单次 kill 的退避重试可能全部断连（命令未执行），短窗口循环跨风暴自愈。
- 持锁连接的 ssh 助手必须 **retries=1**：kex 断连后重试会新起 ssh→新 flock 排队同一把锁，杀掉当前 waiter 后下一个 waiter 接手，worker 反复 `LOCKED` 永不收敛（生产实测两个 waiter 实例）。
- 独立验证：`flock -n <lockfile> -c 'echo X'` 必须 rc≠0（证明锁真被占，而非"还没轮到删"）。finally 兜底按同一 /proc 定位释放，防孤儿。
- **锁释放后 .lock 文件留盘是正常**（flock 语义，历史 op 的锁文件都累积在 `locks/bag/<env>/`）：终态断言必须用"无人持有"（`fuser <lock> || echo LOCKFREE` / `flock -n` 能拿），不能用 `test ! -e <lockfile>`——后者恒假。
- daemon 线程里的 ssh 超时（`subprocess.TimeoutExpired`）要捕获落日志——线程异常被 threading 静默吞掉，writer 死亡不留痕会让"持锁期零删除"断言以错误原因通过/失败。
- 互斥证据必须落 worker 日志：`execute <cid> 被拒 rc=5 error=EXECUTE_BLOCKED: holder refused (LOCKED) ... flock held by another process`；断言持锁期间 0 删除；释放后 worker 在途再仲裁（~2min 自动重驱）收敛 SUCCEEDED，**无需用户 retry**。注意 worker 是**单 claim 串行**：一个卡住的 op 会阻塞全部 cleanup 队列（见收尾的 expire-before-delete 规则）。

## 生产全链路验收（处理机常驻栈 + 生成夹具）
- 驱动跑在**处理机**上（data-process 用户 `/usr/bin/python3 /tmp/<driver>.py`），直连常驻 Backend `127.0.0.1:18000` + 常驻 `bag-cleanup-worker`；开发机没有生产 MySQL 直连（root 登录被拒，deploy `.env` 是业务账号），DB 侧操作用 `docker exec -i rosbag-catalog-backend-1 python - <<'EOF'`（容器内 /app 有完整 ORM + 生产连接）。
- 生产矩阵与测试不同：生产 phase3 原地并入 infer，**单 bag 4 个删除目标**（origin/infer/run/staging 嵌套），不是测试的 5 根；approved_roots 以 `approved_roots_for('production')` 实际返回为准。
- **并发部署会撞验收窗口**：多会话并行时其他会话可能正在处理机重建应用容器组（backend/scanner/orchestrator/worker 同镜像重建、frontend/mysql 不动）。驱动若报空错误 + `docker exec … is not running`，先 `docker inspect` 看 RestartCount/StartedAt/CreatedAt 判定是重建窗口而非驱动 bug，等 healthy 后整轮重跑即可；`.env` 通常不被 rsync 覆盖（deploy 脚本 `--exclude=.env`），重建后清理开关仍在。

## 远端 ssh 通用坑
- 连续短连接会触发 A100 限流（`kex_exchange_identification: Connection closed by remote host`）——夹具 mkdir/写文件合并成**单次 ssh**（for 循环 / && 链），ssh 助手对连接类错误做 3 次退避重试。
- `ssh host 'pkill -f PATTERN'` 会自匹配 ssh 命令行本身 → 杀掉自己的会话（表现为命令空输出、exit 15）：先 ps 取 PID 再 `kill <pid>`，或用不在命令行里的 pattern。
- 驱动挂死诊断：入口加 `faulthandler.dump_traceback_later(300, repeat=True)` + 所有 print 加 `flush=True`。进程状态 `do_select` 且 fd 表无 socket 多半是采样在 sleep 间隙，别据此判"卡网络"——以转储的行号为准。
- `pgrep -f` 首匹配常是 zsh 包装进程（`-lic`），定位 python 用 `pgrep -f "python scripts/<name>"` 精确化；把 kill 与 pgrep 拆成两条命令执行，避免自匹配把自己的 shell 杀掉。

## 收尾
- **先 expire 非终态 op，再删 fence/bag 行**（finally 和预检清理都适用）：`finalize_cleanup` 前置条件是"围栏仍由本 cleanup 持有"——abort 时若先删了 fence 而 op 还在 REMOTE_DELETING/DB_FINALIZING，worker 会无限 409 re-claim，单 claim 串行下**卡死整个 cleanup 队列**（生产实测需人工把 op 置 EXPIRED 解卡）。正确顺序：op 置 EXPIRED（abort 终态，语义上不是真成功）→ 删 fence/bag/run/子表行。
- **幂等行清理按真实键域**：`operation` 键为 `cleanup-preview:{bag_uid}` / `cleanup-confirm:{cleanup_id}` / `cleanup-retry:{cleanup_id}`，preview 键含 bag_uid，confirm/retry 键是 server 生成的 ULID（**不含 bag_uid**，按 uid 前缀 LIKE 匹配不到）——须经 op 行的 cleanup_id 反查键名再删。stale 判据不能只看 BagRecord（finalize 会自清 bag 行，留下 op/幂等行时"bag 无残留"早退会漏清）。**孤儿幂等行兜底**：confirm/retry 键是 ULID，当旧版驱动已把父 op 行删掉时无法经 op 反查，这些行会永久漏清、净零承诺跨轮次破口——解法是驱动对每个 POST 注入自有 `Idempotency-Key` 前缀（该前缀落在 idempotency 表的 `idempotency_key` 列），它是"只属于本驱动行"的确定性标记，父记录消失后仍能按 `idempotency_key LIKE '前缀-%'` 精准圈定自己的孤儿行，stale 判据要同时纳入它。
- 成功路径：A100 文件系统终态逐目标 `test ! -e` 核对（该删的删净），审计目录有记录；本机栈 finally 杀 backend/worker 子进程。
- **生产/运行根不比对静态基线计数**：生产数据根是运行目录，验收窗口内业务随时合法新建（实测 staging 哈希目录 +1）。终态断言口径 = "本驱动夹具前缀（如 `bc_prod_*`）四根零残留"，基线只作日志参照；业务目录绝不可动。测试独占根才可用"计数与建夹具前一致"。
- **finally 的 rm 列表必须带夹具前缀守卫**：路径推导函数在 identity 字典未填满时（name/run 为空串）会返回生产根本身，且空串拼接出的路径仍 truthy 过 `if path` 守卫——`rm -rf` 会直接打到生产存储根。规则：rm 前逐路径段检查夹具前缀，列表为空即拒绝执行。
- **安全门不用裸 assert 当唯一防线**：`python -O/-OO` 会剥离 assert，无校验直通生产删除。驱动入口加 `if not __debug__: raise SystemExit`（或关键门改 `if ... raise RuntimeError`）。
- find 空壳扫描注意：`find <起点> -maxdepth 1 -name PATTERN` **会匹配起点自身**（depth 0）——对 `<root>/<tag>` 逐目录扫 tag 名是误判"扫不到"，本地复现 + 生产日志实证均正常。
- 宣称"既有数据未受扰"前先按 **mtime** 核对：条目早于任务开始=既有，不得把"现在看起来少"当正常。嵌套夹具（phase3/staging 下 `<root>/<rid>/<bag>`）删目标后会留 `<rid>` 层空壳，finally 清理用 tag 通配一次 `rm -rf` 覆盖全部 5 个 root 的两种命名层级，不要逐个 rm 5 个目标——空壳残留会混进下一轮的"既有数据"核对。
- 失败路径：四类残留必须按 tag/PID 精准清理——旧 tag 夹具（A100，含 `<rid>` 层空壳）、远端 flock 孤儿（A100 按 /proc cmdline 定位、连子进程杀）、本机孤儿进程（uvicorn/worker）、**MySQL 孤儿 op + 幂等行**（按 expire-then-delete 顺序）。别用 pkill -f 广撒。崩溃发生在夹具创建前的轮次不用清 A100/MySQL。
