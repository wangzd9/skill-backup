# Skill 备份

备份本机当前使用的 Hermes、Codex 技能和复杂任务协作配套资源。

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
