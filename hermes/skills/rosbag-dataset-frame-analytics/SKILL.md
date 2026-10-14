---
name: rosbag-dataset-frame-analytics
description: "Use when 统计/帧级时间过滤/跨版本比对 rosbag 业务库 dataset_frame 帧数据。"
version: 1.0.0
---

# dataset_frame 帧数据统计与一致性比对

## When to Use

用户问"自建数据集一共有多少帧、按质量分几类""按时刻剔除某时段的帧还剩多少""两个版本(如 `<bag>` vs `<bag>_pro`)的数据是否完全一致""某 bag 帧内容对比"。对象是**业务库 `dataset_frame` 表**(帧级 VLA 数据)。

与相邻 skill 的分工:
- 流水线状态/失败下钻 → `rosbag-catalog-status-check`(那里也有"核实某 run 进了多少帧"用 backend 容器内 count 的口径)。
- 磁盘膨胀/目录冗余(A100 `xl_*`、NAS final 的大小) → `rosbag-pipeline-data-bloat-audit`。
- 本 skill 只处理 **DB 帧表** 的统计与内容比对,不碰磁盘 du。

## 连接与基础口径

- 生产库查询:`docker exec rosbag-catalog_mysql_1 sh -c "mysql -uroot -p\$MYSQL_ROOT_PASSWORD rosbag_catalog -e \"...\""`,**必须 scp 脚本到处理机再 `bash` 执行**,不在开发机本地 exec、不在多层 SSH 里内联复杂 SQL。
- **容器名是下划线 `rosbag-catalog_mysql_1`**(compose 服务 `mysql` + 实例后缀 `_1`)。`docker exec` 报 `No such container` 而 `docker ps` 看得到 = 在开发机本地跑了 exec,或容器名拼错。
- 复杂 SQL 一律落脚本文件(scp 到处理机)——多层 SSH→sh→mysql 内联时 `$` 被外层展开、引号反复解转义,会把 WHERE 静默打碎成空结果。
- `dataset_frame` 关键列:`source_id`(bag_uid)、`pipeline_run_id`、`clip_id`、`frame_id`、`timestamp_ns`、`quality_status`、`planning_available`、`ego_ts_us`、`planning_frame_id`、`routing_path_index`、`navigation_text`、`description_text`、`camera_paths_json`、`payload_json`。
- bag 名/起始时刻:`JOIN bag_record b ON b.bag_uid=f.source_id`;`b.name` 形如 `rosbag2_<车号>_<日期>-<时分秒>`;`b.started_at` 是 datetime(业务时间=北京时间)。
- **`frame_id` 是 clip 内帧号**(每 clip 从 ~10 递增、按 30 帧一采样),不是全 bag 全局编号;跨 clip 对齐用 `timestamp_ns`,不要用 frame_id。
- `quality_status` 取值:`VALID` / `INVALID_COC` / `MISSING_PLANNING` 等,按此分桶。

## 帧级时间过滤(按时刻剔除,如"某时点之后的不算")

1. **先按袋窗口预判,只有跨界袋才逐帧判断**。整袋采集窗口(`started_at` → `started_at + duration_seconds`)全在截止时刻前的全保留、全在之后的全剔,只有**跨越截止时刻**的袋需要做帧级排序计数。这一步把要算的袋从 N 降到 2~3 个,是避免超时的关键。
2. **帧时刻换算用纯整数运算**。`timestamp_ns` 是绝对 epoch 纳秒。MySQL 里对 3 万+ 行逐帧跑 `TIMESTAMP(...,'+08')` / 时间函数 / floor 组合会**超时(420s+ 被杀)**。改为:`FLOOR(timestamp_ns/1000000000)` 得 epoch 秒;截止时刻的 epoch 秒算一次;帧侧 `(epoch秒 + 8*3600) % 86400` 与截止秒数比大小,全程整数。
3. **跨界袋只数数量不逐帧取值**:`COUNT(CASE WHEN 帧秒偏移 > 截止秒 THEN 1 END)` 一次聚合即可,不要 SELECT 出所有帧行。
4. **跨日帧按绝对时刻判断**(epoch 连续),不要只比当日时分。

## 跨版本一致性比对(两个 bag 是否同一份数据)

1. **双表 34k×34k JOIN 无索引会超时**——改为**单表各导一份 TSV** 再本地 Python 按 `timestamp_ns` 建 dict 对齐。导出:`mysql -N --batch -e "SELECT ts, q, ..., col1, col2 ..."`(逐字段 tab 分隔;`--batch` 保证字段内特殊字符/换行转义正确)。
2. **最大的坑:naive 整段内容哈希会 100% 报不同**。`description_text`(VLM 生成)内嵌了 bag/clip 路径名、clip 命名变体(如 `_pro` 段)、采样范围、模型路径,两版 clip 命名不同,直接 MD5(整段)会把"路径名/版本号不同"误判成"内容不同"。**比对前必须规范化**:把文本里的 bag 全名和版本特定的 clip 命名变体替换成统一占位符,再比。
3. **规范化后逐字段拆,定位真实差异在哪个列**。先比 `quality/planning/ego/nav/camera` 这些**结构化真值**列,再比 `description` 文本列;差异只落在某一列时直接定性。
4. **取样佐证快于全量**:挑 1 个共同 `timestamp_ns` 帧,两版并排打印各列肉眼确认,比跑全量 hash 更快。
5. **`_pro` 版本已知结论(先验、再实测数字)**:`_pro` 是同一物理数据的二次处理版本,Phase3 DESC(VLM 场景描述)重跑过——**结构化真值(ego/planning/camera/nav/quality)100% 相同,仅 `description_text` 有措辞级差异(约 50~60% 帧,描述同一场景的同义换法,VLM 非确定性)**。判定"是否同一份"按**训练实际消费哪部分**:只吃结构化真值 → 同一份、去重算一次;吃 description 文本 → 两版 caption 不同是有区分度的样本,不当纯重复丢。

## 常见陷阱

- `desc` 是 MySQL 保留字,不能当列别名(`SELECT ... AS desc` 语法错);描述文本列叫 `description_text`。
- 大字段导出后在**处理机本地** Python 比对,减少一次大文件 scp 往返;读取用 `encoding="utf-8", errors="replace"` 容错。
- 建 dict 按 ts 对齐时若同一 ts 被多 clip 引用会覆盖,确认 key 是否需要纳入 clip 维度。
- 库里 `pipeline_run_id` 对帧查询有效,但 `/api/v1/frames` 不支持 `pipeline_run_id` 查询参数(会被静默忽略)——核实帧数走 DB,别走那个 API。

## 输出格式

- 时间过滤:一张逐袋表(开始时刻 / 采集窗口 / 判定=PRE全留·POST全剔·跨界帧级 / 保留帧 / 剔除帧 / VALID 帧),末行合计 + 口径备注(截止线、去重规则)。
- 版本比对:逐字段一致/差异表(列 × 是否全同),差异列给出"多少帧不同 + 差异本质(路径名 vs 措辞 vs 真值)",末行给"是否同一份数据"的判定及其前提(取决于训练消费哪些列)。
