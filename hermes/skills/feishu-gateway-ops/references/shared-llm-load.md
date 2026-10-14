# 共享本地模型负载诊断（多会话同时变慢/卡住时）

适用：本profile使用共享vLLM（qwen3.8-27b-fp8），开发机当前直连 `http://192.168.2.45:8002/v1`，不再使用18002 SSH隧道。先 `curl --noproxy '*' -sS -m 5 http://192.168.2.45:8002/v1/models` 核验可达，再结合实际推理和metrics判断负载。迁移证据见 `/home/wang/work/doc/analysis/hermes-qwen-load-20261012/DIRECT-CONNECTION-20261013.md`。

## 标准诊断步骤

1. **本机负载**：`uptime`（24 核，load 通常 <2 就排除 CPU）、`free -h`。机器本身极少是瓶颈。
2. **模型队列（权威信号）**：`curl -s http://192.168.2.45:8002/metrics` 读四个 gauge：
   - `vllm:num_requests_running` / `vllm:num_requests_waiting`（waiting 原因看 `num_requests_waiting_by_reason{reason="capacity"}` = 并发槽满）；
   - `vllm:kv_cache_usage_perc`（容量满但 KV 很低 = 长上下文/长生成占槽，不是显存问题）；
   - `vllm:num_preemptions_total`（>0 说明已发生抢占，更严重）。并发上限看 `cache_config_info` 里的 `kv_cache_max_concurrency`（≈4，由长上下文请求驱动，KV 占用 11% 时也已打满）；服务端 `--max-num-seqs` 等槽位参数写死在远端启动里，本机改不了。
3. **延迟探针**：发一个最小 chat completion（max_tokens≈40），记总耗时和 `usage`。排队时几十字符的提示也要 30~60s；健康时 e2e 均值约 10s（终身均值可从 `e2e_request_latency_seconds_sum/count` 两个 counter 算）。
4. **谁在占槽**：
   - 本机连接：`ss -tnp dst 192.168.2.45:8002`定位客户端PID，再结合会话及cron记录归属。其他客户端和后端拓扑须用经授权的服务端证据确认，不把匿名metrics中的全部负载归给本机。
   - 自己的会话：`sqlite3 ~/.hermes/state.db` 查 `sessions`（按 `last_activity_at` 取活跃会话）+ `messages`（`active=1` 的 `LENGTH(content)` 总和，中英混排约 1 token ≈ 2~3 字符）。最大的几个活跃会话 + 并发消息就是队列主体。
5. **历史症状对照**：审批超时、流无finish_reason和预算耗尽要分别核验客户端限额、网络、服务日志与队列，不能统一认定为排队。当前审批timeout以config.yaml为准；journal可定位症状。

## 缓解手段（按权限边界）

- **本机可改**：`auxiliary.approval.timeout` 30s → 90~120s（config.yaml，改后需用户发 `/restart`）；用户侧建议交互会话控制在 2~3 个活跃、重会话 `/new` 或 `/compress`、后台 ocr/codex 任务错开时间。
- **需服务属主**：`--max-num-seqs` 调大（80G 显存 ×4 TP 跑 27B fp8，KV 余量大，8 可行）；重启服务会影响所有在用者，先确认时机。
- **不可绕过**：不要自己另起模型、不要建议用户换云 API 当"修复"，除非用户主动问成本/替代方案。

## Pitfalls

- 用 `/v1/models` 的响应时间判断负载会误判：它不走推理队列，永远 0.2s 左右；只有 chat completion 探针能反映排队。
- `num_requests_waiting` 里的请求**可能大部分就是用户自己其他会话**（5 个会话 = 槽位 4 + 辅助审批调用，自锁死）；先查自己的会话再怀疑别人。
- metrics 计数器是服务终身累计（`_created` 时间戳可见），算均值前先确认量级合理，别把累计当实时。
- 远端 vLLM 进程在 `sysadmin` 账户下（`/home/sysadmin/miniconda3/envs/qwen36-vllm`），A100 上没有 nvidia-smi 权限看它（A100 本机 8 卡是空闲的别的卡）——负载判断只能靠 :8002 的 `/metrics`，不要拿 A100 本地 GPU 利用率下结论。
