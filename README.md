# Skill 备份

备份本机当前使用的 Hermes、Codex 技能和复杂任务协作配套资源。

2026-10-31 机制源码快照见 [MECHANISM-SNAPSHOT.json](MECHANISM-SNAPSHOT.json)：运行时 `c6d2979`、Hermes core `c3901f5`，包括六个角色/显示 Skill、共享脚本和[用户指南](docs/ROLE-WORKFLOW-USER-GUIDE.md)。核心累计补丁已在隔离目录从记录的基线还原并逐文件核验。

长测试观察宽限期按受控任务期限加 180 秒计算；诊断异常和恢复各稳定 120 秒后通知。时段总结按完整冻结快照采样窗口，校验保留分号条件及具体错误分区；协调者七区包含同水位业务内容。0.13.1补齐审计F1～F8的阶段义务、覆盖矩阵、游标接续、输入页去重、问题处置、通知及监控生命周期；上线采用与剩余证据债见[实施记录](docs/analysis/coordinator-health-audit-20261030/IMPLEMENTATION.md)。0.14.0增加保真包内去重、终态授权归档、角色工具发现与可见字节观测；安全重启及新PID采用证据见[Token优化记录](docs/analysis/coordinator-health-audit-20261030/TOKEN-OPTIMIZATION-IMPLEMENTATION.md)。0.15.0增加只读裁决材料、事件续读、统一ACK、保守历史记忆和补验UNVERIFIED异常移交/安全退休/原范围返修；实际采用和验证边界见[裁决材料实施记录](docs/analysis/coordinator-health-audit-20261030/SEMANTIC-DECISION-IMPLEMENTATION.md)。0.15.1修正旧候选INCOMPLETE误列当前审查、区分RESULT_READY与不完整结果，保留全局在途围栏并在风险/下一步列补验责任；见[汇报投影修复](docs/analysis/coordinator-health-audit-20261030/REVIEW-PROGRESS-PROJECTION-REPAIR.md)。0.16.0补齐工具合同、机械段发行/真实业务进展、证据目录、同前提重复验证保护、test-only OCR精确入审、拒绝/通知健康分类及容量观测；见[效果停滞修复](docs/analysis/coordinator-health-audit-20261030/EFFECTIVENESS-STALL-REPAIR-IMPLEMENTATION.md)。0.16.1修复观察嵌套通知明细超限使进度入口与报告回退同时失败，保留准确总数、UNKNOWN/UNVERIFIED和可哈希核验的完整引用；见[只读入口修复](docs/analysis/coordinator-health-audit-20261030/LIVE-PROGRESS-OBSERVATION-BUDGET-REPAIR.md)。0.16.2修复超限只读空转、拒绝后新预览重核、责任提示与当前返修动作关联、二次返修谱系及失效快照冻结诊断；见[接续修复](docs/analysis/coordinator-health-audit-20261030/WORKFLOW-RECOVERY-CLOSEOUT-REPAIR.md)。0.16.3进一步区分OCR选择元数据与不可变证据引用，修正生产准备材料误报UNVERIFIED，保留真实证据哈希门禁。0.16.4补齐审查结果返回后通过原生action归属关联异常返修裁决动作。0.16.5将相同路径分类覆盖到历史拒绝回执中的排除文件，保留原件哈希及真实证据校验。0.16.6在同源MySQL复验PASS后将异常责任提示切回协调者显式关闭，避免等待已完成验证。0.17.0新增精确授权的最终Git交付、逐步真实回执与本地mock收敛，修复已完成需求只能提供shell命令却不能执行的机制缺口；见[最终交付修复](docs/analysis/coordinator-health-audit-20261030/FINAL-GIT-DELIVERY-REPAIR-20261031.md)。快照同步本身不激活 Gateway。其他技能和配置模板保留原采样版本；恢复步骤见 [RESTORE.md](RESTORE.md)。

| 路径 | 内容 |
|---|---|
| `codex/skills/` | 自定义技能及 Codex 系统技能 |
| `hermes/skills/` | Hermes 已安装技能，保留原分类目录 |
| `codex/plugins/agent-skills/` | 当前安装的 agent-skills 0.6.12 技能、公共引用与许可证 |
| `hermes-role-runtime/` | 角色运行插件、工具与原有测试 |
| `docs/` | 统一开发流程指南、图示及本次相关实施记录 |
| `hermes-core/` | Hermes 核心累计补丁、基线身份及修改文件快照 |
| `config-templates/` | 去除凭据和群绑定的路由／OCR配置模板 |

重点入口：

- [规划者](codex/skills/complex-task-planner/SKILL.md)
- [协调者](codex/skills/complex-task-coordinator/SKILL.md)
- [执行者](codex/skills/complex-task-executor/SKILL.md)
- [独立测试者](codex/skills/complex-task-tester/SKILL.md)
- [强模型审查者](codex/skills/complex-task-reviewer/SKILL.md)
- [飞书显示规范](codex/skills/feishu-progress-style/SKILL.md)
- [千问开发交接](codex/skills/qwen-dev-handoff/SKILL.md)
- [完整流程指南](docs/SKILL-DEVELOPMENT-WORKFLOW.md)

## 恢复说明

按需将 `codex/skills/` 中的技能目录复制到 `~/.codex/skills/`，将 `hermes/skills/` 中的目录复制到 `~/.hermes/skills/`。复制前备份目标位置的现有版本；系统和第三方插件技能优先通过对应工具的安装方式恢复。

复杂任务角色技能在本机的来源是 `~/.codex/skills/`，与 Hermes 普通技能分开保存。按现有角色配置加载这些目录；本备份不自动创建群绑定、定时任务或运行角色。

协调者脚本原来指向 `hermes-role-runtime` 的绝对软链接已展开成实际文件，原目标记录在清单中。完整插件源码也单独保存，恢复使用时应按当前运行方式重新建立插件链接和脚本链接。

源文档中的绝对路径、服务地址及历史引用保持原值，跨机器使用时需调整。本仓库是静态快照，不会自动同步；不是包含凭据和机器配置的一键部署包。

[BACKUP-MANIFEST.json](BACKUP-MANIFEST.json) 记录每个文件的来源、SHA-256、软链接处理及排除项。未备份 `.env`、认证配置、Hermes 会话数据库、技能索引缓存、使用统计、运行锁或 Python 缓存。第三方资源保留原有许可证与署名。

## 2026-10-15 更新

同步全部现有技能、角色运行插件与开发指南，包括飞书模型切换、流水线优先准入、OCR代码审查路由及角色进度机制。PRD另保存在 [sweep-zero-prd](https://github.com/wangzd9/sweep-zero-prd)；[模型路由实施记录](docs/analysis/hermes-model-routing-20261015/REPORT.md) 保留现场验证与已知边界。

完整恢复次序见 [RESTORE.md](RESTORE.md)。配置模板反映本次采样时的实际配置，包含后续人工设定（如独立OCR供应商），不回写或改变运行机器。
