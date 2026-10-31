# 机制恢复说明

本仓库为静态快照。恢复时先备份目标机器现有文件，在隔离目录核对版本与路径；复制源码不会自动恢复业务任务或启动网关。

## 2026-10-30 机制快照

本次维护源、字节数及哈希见 [MECHANISM-SNAPSHOT.json](MECHANISM-SNAPSHOT.json)，插件提交 `734c3a8851cfbd7769867efb4c3dd46c43155f32`，core 提交 `c3901f5f8606b6449abd5442d866e28c5015f295`。`hermes-core/local-changes.patch`、`hermes-core/SNAPSHOT.json` 与完整文件已一并更新；使用下面的累计补丁即可还原记录的 core 版本，无需叠加旧覆盖层。

本次包含六个角色/显示 Skill、展开后的共享脚本、运行时全部 Git 跟踪文件、用户指南及流程图。凭据、群绑定、数据库、outbox、锁和运行证据未导出；配置模板保持原采样，仅供字段参考。源码复制或 GitHub 推送不代表 Gateway 已加载，亦不清理旧通知。启用后应核对常驻 Gateway 自身的激活回执和源码哈希。

## Hermes 核心

上游为 https://github.com/NousResearch/hermes-agent 。基线、快照分支和文件列表记录在 `hermes-core/SNAPSHOT.json`。

在独立的上游克隆中检出基线 `5f47c35d37a40fb651e4a00571d03e12b23d11f9`，然后执行（将 `/path/to/skill-backup` 替换为实际路径）：

```sh
git apply --check /path/to/skill-backup/hermes-core/local-changes.patch
git apply /path/to/skill-backup/hermes-core/local-changes.patch
```

补丁包含模型路由通用 hook 和本机已有的 cron 角色静默通知策略，包括未在源仓提交的策略文件。它是整个当前工作树相对基线的累计补丁，不能再叠加同一模型路由提交。`hermes-core/files/` 保存修改后的完整文件，已验证应用补丁所得文件与快照逐字节一致。其他上游版本先检查差异，不能盲目覆盖。

## 插件与 Skill

将 `hermes-role-runtime/` 恢复到维护目录，按原插件方式将 `~/.hermes/plugins/role-workflow-runtime` 链接到该目录。依照其 README 安装依赖与配置；自定义角色 Skill 从 `codex/skills/` 恢复，普通 Hermes Skill 从 `hermes/skills/` 恢复，保留目录分类。

原软链接在技能快照中展开为实际文件，原目标在 `BACKUP-MANIFEST.json` 的 `materialized_symlink` 中。恢复完成后将插件共享脚本重新链接到插件的维护源，避免长期修改两份副本。OCR固定执行入口仍是协调者 Skill 的 `scripts/ocr_runtime.py`；当前共享实现位于 canonical `hermes-role-runtime/`，按清单重建软链接，不维护独立副本。

## 路由、OCR与本机配置

`config-templates/` 提供需要合入的字段，不能替代整个现有配置。调整本机路径、模型路径、服务地址、群ID和角色项目注册表；供应商密钥自行填入权限600的 `~/.hermes/.env`，变量名见 `credentials.env.example`。本地模型如需要凭据，另行配置，替换模板占位符。

合入 `hermes-model-routing.yaml`，并将 `opencodereview-providers.json` 中的供应商字段合入本机 OCR 配置；调整 `api_key_cmd` 中的 Python、插件和 Hermes home 路径。凭据通过命令私下读取，不能写进审查spec或日志。`model-routing-state.json` 仅保存采样时的模式和云端选择；当前 `ocr_profile` 是人工指定的独立审查供应商，保留原样。希望OCR随主供应商切换时，按插件文档检查该覆盖设置。

如恢复角色夜间通知策略，核对 `role-reporting-policy.json` 后单独安装；运行任务、群绑定、cron任务、授权账本、会话数据库和运行锁须重新配置或按业务恢复流程处理。不要根据文档中的历史PID、ACK或审查记录复活在途任务。已冻结OCR run可能绑定旧脚本与配置哈希，不能用本快照替换它们来绕过冻结校验。

启用前核对依赖、配置、插件hook与权限；如需重启，等待在途工作自然结束，再按正常运维方式执行。飞书统一模型控制使用 `/llm` 或插件规定的中文完整指令；原生 `/model` 不同步共享路由状态。

需求包在 https://github.com/wangzd9/sweep-zero-prd ，源码文档的本机绝对路径和历史外部引用保留原值，跨机器使用需调整。清单是文件快照的完整性记录，不能作为业务验收或授权凭证。
