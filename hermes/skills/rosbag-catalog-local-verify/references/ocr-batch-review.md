# OCR批量审查材料与结果核验

本机执行统一遵循[固定OCR流程](/home/wang/.codex/skills/complex-task-coordinator/references/ocr-runtime.md)：prepare/start/status，独立systemd service、全链双锁、固定身份、配置只读、失败停链与一次显式补审。本文补充历史批次整理与意见核验，不提供第二套启动/恢复命令。过去shell runner、后台terminal、nohup/setsid、sentinel队列及恢复隧道命令已退役；原经验全文保存在2026-10-13迁移备份中，不能照抄执行。

## 范围与批次

- 历史多commit窗口先确认用户授权的起止、代码提交和当前已审基线；累计from..to diff不是逐commit差异的并集，被后续还原的文件可能不出现，明确汇报真实范围。
- 列文件须取真实git输出，避免逐次terminal空返回误计、grep反选^恒0等统计错误；prepare以实际JSON preview确定入选/排除，人工列表不能代替它。纯测试或排除文件须写排除依据和对应独立测试，不能声称OCR完成。
- 准备完整背景/REQ/AC、固定base/head及干净交付worktree；默认逐文件，有关契约可显式二维批次，每批最多12文件且全体入选必须恰好覆盖。单文件大并不自动切开，不能用低max-tokens替代职责拆分。
- 固定入口前置检查实际在途和锁；启动后查status的unit/PID/start ticks/session。pgrep命令文本可能匹配wrapper，自报启动、0字节stdout或mtime不能独立证明进展/失败/通过。

## 失败与覆盖

- 原始session及manifest终因优先；max-tokens、60%/80%压缩与resume边界见[OCR契约](/home/wang/.codex/skills/complex-task-coordinator/references/ocr-and-merge.md) §3.7/5。round1/2不是耗尽两轮，cancelled不能直接归因网络、队列或模型能力。
- 退出0或0finding可能伴随部分失败；必须核selected/completed/reused/failed/waived、完整manifest、固定base/head及文件指纹。files_reviewed、planning/压缩摘要都不能补齐completed。
- 失败立即停止后批，不自动换参/加超时/继续；保存有效结果与未覆盖范围。只有原run退出、根因已处理、调用授权仍有效时显式reuse-run补审，完整身份一致才复用成功批，失败文件重新派发。同一身份最多初审加一次补审，再失败升级。
- 没有目标log先查session与批次台账，不能认定从未启动；未收口及未知进程保持门禁。AWAITING_DECISION仅表示有效覆盖，须核findings/跨文件契约/测试后裁决。

## 意见核验

- `ocr session comments UUID`不支持--format；以原session读取真实意见并保留文件行号/级别/范围。comments是证据，不是自动放行。
- 先核被审快照的行为，再看当前HEAD。历史缺陷后来修复标“已修复（commit）”，不能叫误报；真正误报须说明被审版本中前提不成立或已存在正确处理的机制。
- 路径已删除/行号变化不等于行为消失；重构后沿逻辑特征追到新位置。子代理结论/计数/自称落盘均须核明细与文件，真实状态还需原代码、日志或运行证据。
- 数据库引擎语义断言按授权派独立测试，用隔离的真实引擎、原始DDL/SQL与数据验证；静态推断不能冒充MySQL实测。协调者不自行承担日常验收测试。
- 不按severity一刀切：行为后果（崩溃、错结果、丢信息、非法状态）按原REQ/AC判CORRECTION；证据缺口派测试；风格/未来要求不扩本期范围。代码返修按当前派单/用户授权，不自动应用OCR patch。

## 输出与收口

每批记录task/attempt、固定base/head、全体入选/排除、unit/PID/session、实际终态/覆盖与findings裁决，注明未执行/未覆盖范围。原始日志不覆盖。测试、必要强审及合入条件满足后由协调者收口；工程验收不能替代生产启用授权。
