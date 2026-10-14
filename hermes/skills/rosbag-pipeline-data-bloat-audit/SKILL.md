---
name: rosbag-pipeline-data-bloat-audit
description: "Use when 审计自建/NVIDIA 流水线各阶段数据膨胀、定位冗余与无用数据、产出可清理清单。"
version: 1.0.0
author: hermes-agent
license: MIT
metadata:
  hermes:
    tags: []
    related_skills:
      - a100-test-data-cleanup
      - rosbag-catalog-prod-delete-triage
---

# 自建数据集流水线数据膨胀审计

## When to Use

用户问"各步骤数据膨胀多少""哪些数据无用/可清""A100 盘快满了"。**只读审计**：本 skill 只产出清单与判定，删除动作走 `a100-test-data-cleanup`（测试盘）或 `rosbag-catalog-prod-delete-triage`（生产），不在审计里顺手 rm。

## 连接
- A100 生产：`ssh -i ~/.ssh/id_ed25519_a100_wzd -o IdentitiesOnly=yes -o BatchMode=yes a100-wzd '<命令>'`（默认端口即生产；测试隧道 60022 见 cleanup skill）。
- NAS 生产根（处理机视角）`/public/vla_dataset_builder_backup/{raw,ready,final}`；NAS 测试根（开发机）`/mnt/nas_backup/vla_dataset_builder_test/*`。
- 开发机 CIFS 挂 NAS 很慢：逐 bag `du` 全量循环会超 300s 超时——**只 `du` 一层 + 抽样几个目录**，或 terminal background+notify。

## 审计步骤（按此顺序）
1. **容量基线先行**：`df -h` 各数据根（A100 `/data`、`/home/xmyf/wzd/test`、NAS、处理机）。已用率 >90% 的盘，报告里把"紧急"维度放在第一位。
2. **一层目录总量**：`du -sh <root>/*` 按大小排序，确定自建 vs NVIDIA 占盘比（生产 `/data/xl_*` 自建、`/data/nvidia_*` NVIDIA，别混报）。
3. **逐 bag 拆层**：`du -sh /data/xl_origin_dataset/*/` 和 `/data/xl_infer_dataset/*/` 各来一遍，得到单 bag 在 origin/infer/final 三层的落盘序列（final 在 NAS，抽样 1-2 个 run 的 `du -sh <final>/<run_id>/*`）。
4. **抽样 clip 内部构成**：取 1 个 bag 的 1 个 clip，`du -sh <clip>/*` 排序——大头占比一目了然（已知签名见下节）。
5. **跨 clip 指纹比对定重复度**：`ls -la <bag>/*clip*/ins.json | awk '{print $5}' | sort -u`（大小唯一=内容重复）、抽 2 个 `md5sum` 确认。重复度 × clip 数 = 该 bag 的冗余量，乘 bag 数 = 全盘冗余量。
6. **跨层残留对比**：origin 有而 infer 没有的文件类型（如 mp4 vs jpg）= 上游中间产物；infer 的 done.json/标记文件 = 可忽略小项。
7. **run 终态对照**（判"可清"必需，开发机本地 DB）：`SELECT pipeline_run_id,status,attempt,pipeline_type FROM pipeline_run ORDER BY updated_at DESC`——PUBLISHED 的 run 对应 A100 工作副本才可列入可清；FAILED/在途的一律标注"保留"。

## 已知膨胀签名（2026-10 实测，复审计时先验这些、再实测数字）
- **ins.json（bag 级完整 IMU，~250-280MB/bag）在 origin/infer/final 三层 × 每个 clip 全量复制**：单 bag 冗余可达三层合计的 60%+，是全流程绝对大头。clip 实际只需自己时间段的 IMU 段（~1-2MB），膨胀比≈clip 数。**是否可裁减取决于下游训练消费语义**（按 DatasetFrame/dataset_index.csv 消费则可改 clip 级切片）——审计只报告事实与潜在节省量，改格式属自建脚本基线变更，单独立任务。
- **相机 mp4（origin 层，~3-4GB/bag）**：Phase1 抽帧成 jpg 后（infer 层 mp4=0）不再被引用；可删但失去 Phase1 重跑能力（重跑需完整 Phase0，源 db3 在 NAS ready）——标注"低价值保险数据"。
- **`<bag>_pro` 重复袋**：同一源 bag 的第二处理版本，origin+infer 全套各一份；是否可清看哪个版本的 run 被 NAS final 引用（对 final 目录内 clip 命名前缀）。
- **NAS final 的 phase3_manifest.json 可达几十 MB/run**：正常产物，不算膨胀。
- 单 bag 全链路落盘 ≈ 源 bag 的 2-3 倍（origin+infer+final 三层，其中 ~80%+ 是重复 IMU 与可裁项），报告时给"有效数据 vs 冗余"两个数。
- **重复文件是否真冗余用 inode 实锤**：`find <bag目录> -name ins.json -printf "%i %p\n" | sort -n | uniq -c -w 15 | awk '$1==1' | wc -l` 与文件总数对比——全部唯一 inode = 独立副本可 dedupe；已有共享的 inode 不重复计。审计时先算这个再报可回收量，别按字节估算。
- **CIFS 挂载上直接建 SQLite 文件必现 `database is locked`（即使无任何并发写者）**：CIFS（尤其 `nounix` 挂载）的 fcntl 锁语义不可靠，SQLite 默认共享锁路径直接失败。复现探针：在目标 CIFS 路径 `sqlite3.connect` + CREATE/INSERT/commit 一条命令即可判定。正确口径是**两段式**：本地盘写完 → 关闭校验（integrity_check/摘要）→ 拷到 CIFS 再原子 rename 发布；不要试图用 PRAGMA locking_mode=EXCLUSIVE 或改挂载参数绕过（不可靠）。任何"写失败: database is locked"先按本条排除文件系统，再查并发。

## 输出格式（用户要"告诉我哪些无用"）
- 一张落盘表：层 × 位置 × 大小 × 内容构成（哪个文件占大头）。
- 按无用程度排序的 Top N 膨胀项，每项：实测证据（md5/du 数字）、判定（可裁/保险数据/保留）、潜在节省量、前提条件（如下游消费语义确认、run 终态核对）。
- 容量告警单列：哪块盘 >90%、按当前膨胀率还能吃几个 bag。
- 建议分优先级：立即可清（低风险）/ 需决策（产品语义）/ 测试环境随手清。

## 治理方案选型（用户问"怎么省"时给口径，执行另授权）
- **硬链接共享（优先推荐）**：同文件系统内同内容副本改成一个 inode 多链接，数据零变化、下游读法零变化、字节级节省重复量。前提逐条核：①两层目录同 device（`stat -c "%d"` 对比）；②文件生成后全链路只读（grep 各阶段脚本确认无原地写，硬链接"改一份=改全部"）；③跨机传输的 rsync 要加 `-H`（否则硬链接关系传过去散成独立文件）；④**CIFS 不支持硬链接**——NAS 发布层省不了，只能改发布结构，单列不混入本方案收益。
- **clip 级切片**：把 bag 级全量文件裁成 clip 时间窗内数据。节省最大但要改生成侧代码 + 可能动下游消费语义（先确认下游按什么读），是二期方案，不与硬链接混报。
- 存量 dedupe 脚本固定四步：取 canonical → 全量摘要比对（不等绝不合并）→ `ln -f` 替换 → 校验 link count；是生产盘写操作，需用户授权并走 cleanup 专项口径。

## 规则
- 全程只读；**审计 ≠ 清理**，删除清单交用户确认后才走 cleanup 专项 skill。
- 生产根（`/data/xl_*`、`/data/nvidia_*`、NAS ready/final）受生产数据保护：报告里逐条标注"需用户授权"，不预设可删。
- 数字必须来自本次实测 du/md5，不引用上次审计的绝对值（数据在长）；可引用的是结构性签名（哪个文件重复、mp4→jpg 关系）。
- **rsync 无 `-H` 时硬链接关系跨机散成独立文件**：审计"共享方案可行性"时除同 device/只读前提外，必查现有传输链路的 rsync 参数（`grep rsync <代理脚本>`），生产链路缺 `-H` 要把"改传输参数"列入方案改动项，不能只算存储侧收益。
- 大目录 du 超时的命令改 background+notify，别前台干等 600s。
