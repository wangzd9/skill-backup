# 效果复核与 MN 交付停滞：机制修复实施记录

日期：2026-10-30，Asia/Shanghai。依据 [效果复核](POST-OPTIMIZATION-EFFECTIVENESS-REVIEW.md) 第 8 节及 [MN 停滞根因](MN-DELIVERY-STALL-ROOT-CAUSE-20261030.md)，在 0.15.1 / `9d8c3a6` 上实施 0.16.0。源代码发布、Gateway 采用与业务最终验收分别核对。

## 本轮修复

| 问题 | 实际改动 | 保留的边界 |
| --- | --- | --- |
| decision 工具字段与执行入口不一致 | 统一分操作 schema/允许字段/必填项/最小示例，拒绝回执返回具体纠正参数 | 不忽略错参、不跳过运行时验证 |
| JUnit 占位合同缺失 | verify/verify_integration 共用 pytest/junit 说明、示例及预检纠正 | JUnit 取证与 pytest 两次稳定性运行保留 |
| 历史续读占满技术会话 | 原生页最多扫描 512 条，连续五类机械记录合并；原文按不可变文件保存，完整 ID/冻结边界/显式 ACK 保留；启动目录优先列当前合法动作 | CHECKPOINT 与业务事件不折叠，不跳游标、不自动 ACK |
| 消费进展冒充交付进展 | 另列最近业务转换、当前执行首次有效动作、owner；会话阈值拦截单独记录原因和未启动动作 | ACK 原含义及安全交接阈值保持，不用 checkpoint/worker 退出证明 work 完成 |
| 同前提反复验证 | 新操作按候选、命令、实际环境、声明文件建立重复保护，非通过/未知不换 ID 再跑；原操作仍可恢复 | 变更前提后重新验证；同版隔离库新实例不冒充修复；旧 35 次记录不重写 |
| 只拒绝重试却无人接手 | 拒绝时持久登记 VERIFICATION_RETRY_BLOCKED，目录提供原证据及 COORDINATOR 责任；仍沿原任务生命周期调查、返修或退休 | 不把拒绝判为代码 FAIL，不自动完成 work、清 writer 或生成新裁决 |
| 交付证据导致 dirty/相对路径反复纠错 | 本单工作区外持久证据目录，受限 terminal 可写；交付逐字段预检绝对路径/文件/哈希，dirty 回执列具体项 | commit/base/AC/scope/hash/clean 检查不放宽；既有候选证据仍可核验 |
| test-only 候选没有 OCR 入选文件 | 无业务文件入选且仅默认排除测试源码时，冻结精确 include；沿 OCR 公开 --rule 入口贯穿预览、规则解析和实际执行 | 保留最高优先级显式过滤及项目/全局/语言规则；不扩大业务改动、不空审或免审 |
| 目录仍混入历史审查 | 复用 0.15.1 当前 task/attempt/candidate 分类，历史债单列，全局 OCR SUBMITTED/UNKNOWN 单列 | 原动作/失败关闭关系与全局围栏不改写 |
| REJECTED 被索要服务运行回执 | 独立核验既有零派发证明、冻结身份及读取稳定性；健康分类与有证据的旧观察归档分别处理 | 缺证明仍未核实；拒绝不是业务通过，不按枚举白名单全部清除 |
| UNKNOWN 通知隐藏 | 全量按状态/种类聚合，列最老年龄、责任、确认口径及有界样本/省略数 | PERIODIC_SUMMARY 保留独立消费者，UNKNOWN 不自动重发，不推断送达或丢失 |
| 材料关联长期沿用 | 目录移除、材料变化后终止 execution 关联；新账本事件后等重新查询再关联物理请求；记录查询阶段时延和字节 | 元数据关联不当作因果成本；共享物理请求只计一次，不保存提示/内部推理 |
| 容量只看 SQLite | 缓存、有界采样数据库/WAL/不可变附件，标扫描完整性和容量下界；完整同口径样本才算附件增长 | 不在线删除/VACUUM；尚未实施离线归档、引用恢复或历史压缩 |

源码涉及 `workflow_decision_context/contracts/input_pages/business_progress/delivery/verification_retry/ocr_selection/capacity` 及既有原生入口、观察与激活边界。用户/开发指南更新为 3.28；当前事务协议和两张受影响流程图同步。没有更换模型、降低推理预算或裁剪原 AC。

## 已核实的当前业务事实

18:46 的只读核对已见旧 MN-07 TESTER 安全释放事件 `14228`；MN-04 a8 的 `independent_test PASS` 于事件 `14239` 登记，候选仍为 `e82af00254ddd4dc67a823b142f0f7c6c4d5d596`。当时 MN-04 为 REVIEW，OCR `mn04-a8-ocr-001` 为 REJECTED，其零派发回执代码为 `NO_REVIEWABLE_FILES_OR_DUPLICATES`。这修正了 18:14 审计“尚无 a8 独立结果”的时间性结论，不倒改旧快照。

18:51 对同一 a8/head 做真实 OCR 无模型预览：原入选为空；精确 include 后入选 `tests/test_mn01_migration.py`。仅验证选择适配，不执行付费审查、测试、裁决、派单、ACK 或通知，不代表 OCR PASS。隔离回归另确认实际 OCR delegate rule 仍保留原项目规则，显式 exclude 不被覆盖。

本轮沿用既有补验异常处置和安全退休入口，没有代角色关掉 e14173、合入 a8 或完成 MN-07/需求验收。相关后续必须由原角色和真实回执证明。

## 回归、发布及采用

- 相关全套回归 **275 项通过，234.751 秒**；最后补充材料源变化的关联失效检查后，相关生命周期回归 **43 项通过，14.951 秒**。后者含前一组的部分用例，不合计为 318 个独立用例。编译、`git diff --check`、指南本地链接及 SVG XML 检查通过。
- 运行时 **0.16.0** 已提交：`4efd7b7859e956d828c242f66ad1e0f34d04be5a`，父版本 `9d8c3a67adfb00d6aadf408346c1a9e435c94d31`。源码逐文件原子发布，Git 跟踪工作区干净；原有未跟踪 `evidence/` 未纳入提交。
- **18:56:37 +08:00**，Gateway PID **3612975** 保持运行、飞书连接正常；激活状态 `ACTIVATED / future_calls_only`，**68 项源文件 SHA 全部匹配**，文件后端同 PID 为 ACTIVATED，调度钩子同 PID 为 COLLECTED。本轮无需重启；已进入的旧帧不据此追认采用新合同。
- **18:57:04 / event 14245**，直接只读分类核实：40 项服务终态回执已验证，7 项拒绝已有完整零派发证明；历史动作 `coordinator-ocr-rccd03-c-attempt1-resub1` 仍为 `REJECTION_ZERO_DISPATCH_UNVERIFIED`。通知为 SENDING 1、SENT 265、SUPPRESSED 88、UNKNOWN 15；历史 INCOMPLETE 5 项、当前审查关注项 0。这里的“当前关注项”不代表 OCR 通过。此次未取得新保存的自然观察器健康快照，不宣称所有健康项已绿。
- 同次快照中，MN-04 a8 仍为 REVIEW，独立验证 PASS，原 OCR 请求 REJECTED，异常关闭数为 0；其余五个开发叶任务 COMPLETED。旧 TESTER 释放及 a8 独立 PASS 来自原角色事件，不是维护操作代写。e14173、a8 OCR/合入、同谱系补验和最终集成仍按原合同接续。

脱敏后的版本、测试摘要、激活哈希、a8 选择预览及现场分类见 [发布核验记录](effectiveness-stall-rollout.json)。机制源码、六个角色/显示 Skill、指南和明确选取的审计材料通过既有 [GitHub 备份仓库](https://github.com/wangzd9/skill-backup) 的 `main` 快照同步；`SOURCE-REVISIONS.json` 记录原始源码提交，快照本身不触发激活。数据库、凭据和原始会话不进入快照。

## 尚未据此证明的效果

- 没有以字节/函数数量断言 token 或交付时间降低 10%～20%；真实任务的每交付成本、等待耗时、首次验收和返工仍需持续采样。
- work_memory 仍是可选历史定位，`current_verified=false`；没有宣称现场已产生复用收益，也没有据此减少必要核证。
- 重复保护对本版新执行建立索引，不扫描导入或改变历史 35 次结果。受控 MySQL 的同一次操作内仍复跑两次，夹具必须恢复自身状态。
- 不自动把环境问题转为代码返修。普通验证遇到未核实前提时，协调者按现有 task/work 状态选择调查、变更前提后重验或合法返修；拒绝重试并不自动释放角色。
- 容量是有界观测；附件未遍历完时是下界，不能作为已完成归档或全量增长率。实际离线一致归档及还原验证仍需另做。
- 热加载只作用于未来调用；已经冻结的 OCR worker、原工作事实和在途帧按原身份收口，不重建旧提示，不强制接管。
