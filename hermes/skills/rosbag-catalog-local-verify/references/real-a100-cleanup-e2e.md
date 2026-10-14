# Bag 清理真实 A100 测试根 E2E（ssh transport）

用户已授权 A100 **测试根**真实删除验收（"真实测试环境测试全流程"）时执行。入口 `scripts/run_bag_cleanup_real_a100_test.py`（仓库内）。单次 12-15 分钟（120s 租约节奏），`background=true`+run 日志，进度看制品（worker.log/ctl/DB），不看 stdout（长跑缓冲不可信）。

## 架构（全真实，无 mock）
- 独立 SQLite backend（uvicorn 子进程）+ 真实 worker 子进程：`BAG_CLEANUP_EXECUTOR_TRANSPORT=ssh`、`BAG_CLEANUP_REMOTE_EXECUTION_ENABLED=1`、`BAG_CLEANUP_AUTHORIZE_SECRET=<随机 hex>`（内存生成，不落盘不进日志）。
- A100 测试根 `<root>/bag-cleanup-exec/` mini repo，只放 `scripts/bag_cleanup_executor.py` + `app/services/remote_bag_lock.py`（executor 自带 sys.path 引导；remote_bag_lock 纯 stdlib、头部无 app 依赖）；**不需要** `bag_writer_lock.py`（删除链路不 import）。从工作区 HEAD scp，部署后 `md5sum` 与本地逐字节核对。
- ssh 别名 `a100-wzd`（BatchMode、专用密钥）；A100 python：`<root>/envs/rosbags/bin/python`。
- worker `BAG_CLEANUP_EXECUTOR_REMOTE_CMD`：`cd <exec_dir> && BAG_CLEANUP_AUTHORIZE_SECRET=<secret> <python> scripts/bag_cleanup_executor.py`——远端命令经远端 shell 执行，POSIX 环境前缀赋值可用（hex 无元字符）。
- 夹具：`tag=int(time.time())` 唯一命名（如 `bc_real_<key>_<tag>`），每 bag 5 目标按 `approved_roots_for("test")` 矩阵生成（origin/infer 下 `<bag>`、run_root 下 `<rid>`、staging/phase3 下 `<rid>/<bag>`）；**批量单次 ssh** mkdir + echo tag 写文件（限流）；5 根下既有条目一律不碰。

## 装配规则（每条都是实测坑）
1. env 在首次 import app 之前设好（settings 单例冻结，见主文 Bag 清理节）。
2. preview/confirm/retry 路由返回 **202**，断言含 202。
3. confirm/retry 前 GET 必须 `?regenerate_nonce=true`，否则 nonce=None → 422。
4. `start_preview` 只在请求 cookie 同 session 时复用会话：驱动建多个 op 必须把首个 preview 的 Set-Cookie 带给后续 preview POST，否则跨 op 读 403、wait_status 静默循环（DB 全 PREVIEW_READY 但驱动读不到）。
5. ssh transport EXECUTE 无 `BAG_CLEANUP_AUTHORIZE_SECRET` fail-closed（INSPECT 放行、EXECUTE 日志"缺少 BAG_CLEANUP_AUTHORIZE_SECRET"）：worker 用 make_auth_token 签 token、A100 executor 验签，两侧同一 secret。
6. 长驻 holder（writer 互斥模拟）：禁 `nohup X &`（sshd 通道等后台 fd → 客户端超时连 X 一起杀）。用 `flock -x <lock> -c 'sleep 600' >/dev/null 2>&1 & echo $!` 经 Popen（stdout=PIPE）取远端 holder PID，ssh 即退出；释放 `kill <pid>`（flock 随进程死释放）；持有验证用独立 `flock -n` 探针（应 rc≠0）。finally 兜底 kill（中途异常否则残留至 sleep 600 结束）。
7. 连续短 ssh 连接触发 A100 远端限流（`kex_exchange_identification: Connection closed by remote`）：mkdir/文件生成合并单次 ssh；ssh 包装器对连接关闭特征重试 3 次。
8. ssh 上 `pkill -f <模式>` 的模式若出现在自己的远端命令文本里 → 杀掉自己的 ssh 会话（空输出、无报错）。按记录的 PID kill。
9. 长跑驱动布 `faulthandler.dump_traceback_later(300, repeat=True)`：/proc fd/wchan 采样不可靠（sleep 间隙无 socket fd、do_select≠卡死），watchdog 转储钉住等待行。
10. 成功删除后嵌套目标（staging/phase3 的 `<rid>/<bag>`）留空壳目录：finally 清理按 tag 通配覆盖 5 根全部模式（`bc_real_*_<tag>` / `bc_real_run_*_<tag>`），别只 rm 目标本身；跑前对 5 根拍快照（条目+mtime），跑后复核既有条目未动。

## 场景与断言
- **A 成功**：confirm → ssh EXECUTE 真删 → SUCCEEDED；文件级断言 5 目标 0 存活（独立 ssh `test -e`，不信 DB 状态）。
- **B 部分失败→retry**：FIFO 在 EXECUTE 窗口注入（同本地层规则：PREVIEW_READY 后、confirm 前）→ holder PARTIAL → NEEDS_RECONCILIATION；retry 用**有界碰撞**（移除 FIFO 后 worker reconcile 会瞬时翻 REMOTE_DELETING 持租约 → 409）：GET → 非 NEEDS_RECONCILIATION 则 sleep 5s 跳过 → POST retry → 2xx 即落地，上限 ~120 次；续删 EXECUTE gen+1 → SUCCEEDED。
- **CANCEL**：API 无 cancel 端点，前端取消=关弹窗；DB 行留 PREVIEW_READY 永不执行 → 终态 5/5 保留断言。
- **D writer 互斥**：持锁期间 EXECUTE 拒 `rc=5 EXECUTE_BLOCKED: holder refused (LOCKED) detail=flock held by another process`（worker.log 行=权威证据）+ 持锁期间 5/5 零删除断言 → kill holder → 在途再仲裁 ~120s 重驱自动收敛 SUCCEEDED（无需 retry）。
- 跑前确认 A100 无同 bag 写任务；D 场景持锁上限 600s（finally 兜底 kill 防孤儿）。

## 跑后复核
- A100：flock/executor 孤儿进程 0；5 根无 `bc_*_<tag>` 残留；既有条目数与 mtime 未变。
- 本机：worker/backend 子进程已死（孤儿会让后续 run 抢同一 claim）；`.local/browser-test/bc-real-*/` 栈目录 + run 日志保留为证据。
- 驱动 commit 按流程跑 ocr；HANDOFF/TODO 更新，口径保持：测试环境验收 ≠ 生产放行（GATE-LOCK、生产根、处理机部署另行授权）。
