# 本地 mock 栈 MySQL 造测试数据

适用：手工往本地隔离栈（`rosbag-catalog-local-mysql-1`）插记录以测试页面行为（分页、边界、状态渲染）。只用于本地 mock 数据，生产数据库严禁。

## 表名（单数，容易猜错）

- `pipeline_run`、`step_run`、`pipeline_event`、`bag_record`、`artifact`（不是 `step_runs`/`pipeline_events`；`SHOW TABLES` 确认）。
- 必填列速查：
  - `pipeline_run`：`pipeline_run_id`(varchar 64)、`source_id`(varchar 26，超长直接插入失败)、`queue_name`、`status`、`priority`、`attempt`、`error_message`(NOT NULL 无默认，填 `''`)、`created_at`/`updated_at`、`pipeline_type`、`pipeline_config_snapshot_json`(NOT NULL，填 `'{}'`)。
  - `step_run`：`pipeline_run_id`、`step_name`、`attempt`、`status`、`error_message`(NOT NULL 填 `''`)、`started_at`/`finished_at`。
  - `pipeline_event`：`pipeline_run_id`、`event_type`、`payload_json`(NOT NULL 填 `'{}'`)、`created_at`。
  - `bag_record`：`name`、`relative_path`、`dataset_type`、`status`、`source_status`、`vehicle_link_status`(填 `'UNMATCHED'`)、`metadata_json`(`'{}'`)、`duration_ns`/`total_size_bytes`/`message_count`(填 0)、`note`(`''`)、`row_version`(填 1)、`updated_at`。多个 NOT NULL 无默认列的报错是一条一条来的（顺序 duration_ns → vehicle_link_status → note → row_version），别逐条试错，首次就把必填列全写齐。NVIDIA 数据集行用 `dataset_type='nvidia_physicalai'`、`status='READY'`、`source_status='DISCOVERED'`。
  - `artifact`：`artifact_id`、`source_id`、`pipeline_run_id`、`artifact_type`(如 `'NVIDIA_FINAL'`)、`path`、`total_size_bytes`、`file_count`、`created_at`。
- Bag 可缺失：`source_id` 无对应 `bag_record` 时详情页 Bag 显示 "—"，页面仍 200；造 run 不必先造 Bag。反之测 bag 列表的流水线派生信息（状态/版本/产物大小）时，同一 `source_id` 要同时造 `bag_record` + `pipeline_run`(`pipeline_config_snapshot_json` 里放 `dataset_revision`) + `artifact`(type `NVIDIA_FINAL`) 三行才完整渲染。
- 同秒批量插入的 `created_at`（秒级 datetime）排序不稳定：测分页只需数量正确，同秒内条目顺序不保证，断言别依赖。

## 插入命令（stdin 陷阱）

`docker exec mysql ... < file` 的 stdin 重定向发生在宿主机 shell，容器内 mysql 客户端拿到的是空输入，退出码 0 静默失败（插了 0 行还不报错）。正确做法：

```bash
PASS=$(docker exec rosbag-catalog-local-mysql-1 sh -c 'echo $MYSQL_PASSWORD')
docker cp /tmp/test_data.sql rosbag-catalog-local-mysql-1:/tmp/test_data.sql
docker exec -e MYSQL_PWD="$PASS" rosbag-catalog-local-mysql-1 sh -c "mysql -urosbag_catalog rosbag_catalog < /tmp/test_data.sql"
# 立即核对行数（不核对会以为插成功）
docker exec -e MYSQL_PWD="$PASS" rosbag-catalog-local-mysql-1 mysql -urosbag_catalog rosbag_catalog -e "SELECT COUNT(*) FROM step_run WHERE pipeline_run_id='<id>';"
```

- 凭据用 `MYSQL_PWD` 环境变量传，避免密码出现在命令行/日志；用户名/库名从 `docker exec ... env | grep MYSQL_` 取，不要猜。
- SQL 行数多时用 Python 脚本批量生成（如 50 步骤+50 事件），不要手敲；生成时同时把每条记录的序号写进 `step_name`/`event_type`（如 `TEST_STEP_01`），翻页后肉眼核对当前页条目范围。
- 造完在浏览器验证：分页摘要（第 x / y 页，共 n 条）、点页码只刷新目标区块、两分页区互不重置，与 SKILL.md 翻页验证清单一致。
