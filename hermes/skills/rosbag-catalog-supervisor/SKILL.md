---
name: rosbag-catalog-supervisor
description: 巡检 rosbag-catalog 流水线失败、自动重试计划和服务健康；只通过现有 Backend API 操作。
---

# rosbag-catalog 监督

工作目录固定为 `/home/wang/work/rosbag-catalog`。先运行
`python3 /home/wang/.hermes/scripts/rosbag_catalog_monitor.py` 获取稳定摘要，再按需读取
`GET /api/v1/pipeline-runs/{pipeline_run_id}` 的详情。

规则：

1. `unreachable` 只告警，不重试；先报告 Backend URL 和健康检查失败。
2. 只对 `FAILED` 且没有 `auto_retry_at`、`auto_retry_count` 未超过服务端预算的运行，结合详情中的错误分类决定是否调用现有 `POST /api/v1/pipeline-runs/{id}/retry`。
3. 输入、质量、冲突、磁盘和配置错误不自动重试；网络/进程被杀且服务端明确允许的失败才可重试。
4. 重试必须使用 Backend API，禁止直连数据库、删除 NAS 文件、重置容器或绕过租约/幂等键。
5. 每轮输出运行 ID、状态、错误摘要、自动重试次数/计划和实际 API 响应；没有变化时输出“无新增异常”。

告警文本要简洁中文，包含运行 ID、Bag（若详情可得）、失败步骤、错误分类、下一次自动重试时间和建议动作。
