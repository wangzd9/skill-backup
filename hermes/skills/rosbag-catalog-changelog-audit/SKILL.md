---
name: rosbag-catalog-changelog-audit
description: "rosbag-catalog 更新日志盘点：对账 commit 范围与已认领条目，合并/新建 pending 候选。"
---

# rosbag-catalog 更新日志范围盘点

仓库 `/home/wang/work/rosbag-catalog`。日志工作流本体（分级、表达规则、批准/发布/回退）以仓库 Skill `.agents/skills/feature-changelog/SKILL.md` 为准；本技能只覆盖"给定 commit 范围，判断并执行候选更新"这条路径。

## 步骤

1. **列范围**：`git log --oneline <range-start>..HEAD`。
2. **对账已认领 commit**：解析 `changelog/pending/*.yaml`、`changelog/approved/*.yaml` 和 `changelog/releases.yaml`（releases 里每个 entry 的 `commits`）中全部 commit（按前 7 位前缀匹配），标出范围内哪些 commit 已被某条日志总结。用 execute_code + yaml 一次跑完，不要人肉比对——范围常有 50+ commit。
3. **分类未认领 commit**（只看未认领的）：
   - 跳过：HANDOFF/docs/chore/changelog 操作提交、纯 OCR 内部修复（安全加固、测试脚本、devflow 工具链）、已 released 条目的收尾微修。
   - 合并进现有 pending：同一功能点的后续修复/增强（如分页组件的 CSS 微调、同一列表页的状态筛选修正）。
   - 新建候选：用户可感知的独立能力变化（新资源池带来的并发行为、新表单校验等）。
   分类依据是实际 `git show --stat` 的改动文件面，不是 commit 标题；拿不准的先看 diff 再定。稳定性/运维类只有能说清对业务连续性的实际影响才建候选（仓库 Skill 的分级规则）。
4. **向用户确认方案**：用 clarify 一次性给出"合并到哪些候选 + 新建什么 + 跳过多少"，默认选项=建议执行方案。用户点头才动文件。
5. **执行 stage**：
   - 合并：把该候选的**完整最新表述**（title/summary/全部 changes，含既有点和新增点）+ 追加 commits 写成临时 yaml，`python3 scripts/changelog.py stage <tmp.yaml>`。merge 语义：candidate 的 changes 按 title 覆盖同名点、追加新点，commits 去重合并，review_id 保留原编号。只写增量 diff 进去会丢既有分点。反之，重写时若新分点 title 与旧分点不同名，stage 会**追加**而不是替换——想删掉旧分点（如把含内部指标的旧点换成干净表述）必须改 title 与既有点完全一致走覆盖，或直接编辑 pending yaml 删旧点（保留 review_id 行）后 validate + render。
   - 新建：feature_key 取新的（snake_case 功能名），review_id 不写——stage 自动分配 F-(max+1)。
   - **多天/多模块功能的条目必须体现实际交付量，不能写通用安全口吻**：落笔前先量化范围（`wc -l` 新增实现文件、`git log -p <range> -- tests/ | grep -c "^+def test_"` 统计范围内净新增用例数——直接数测试文件现有用例数会把历史遗留算进本条、夸大交付量；另有迁移文件数、OCR 轮数），changes 按交付链路逐模块/逐环节写（规划→台账→锁协议→执行→worker→页面，至多 8 条），末条汇总审查与测试规模。用户对一天规模的功能只给 4 条泛化分点会打回要求"突出干了什么"。但"体现工作量"只能用业务口径（真实数据验收通过、覆盖几类场景、多轮迭代收敛），内部技术指标不进正文：chunk 编号、帧数/clip 数、代码行数、测试用例数、迭代轮数、exit 0、根因层数等都会被用户指出不适合——仓库 feature-changelog Skill 明文禁止测试/文件数量、代码行数、内部路径，落笔前逐条对照检查。
   - stage 会拒绝：commit 已被 released/approved 其他条目总结、feature_key 已在非 pending 状态、编号跨功能重复。撞了就是分类错了，回第 3 步。
6. **收尾**：`python3 scripts/changelog.py validate` + `render`（自动刷新 `changelog/REVIEW.md`），报告审查编号入口；纯 `changelog/` 目录提交（AGENTS.md 明确豁免 OCR）。commit 信息格式参考既有：`Stage changelog audit for <range>: merge N commits into F-xxx... and add F-yyy...`（N 用**候选实际覆盖的 commit 数**=范围总数−跳过数，不写范围原始数）。**推送前必须 `git log --oneline github/main..main | wc -l` 披露实际推送范围**：本仓 origin=GitLab、github=镜像 remote 更新滞后，`git push github main` 会一次推走区间内所有并行会话的积压 commit（几十上百个），向用户的汇报要写清"本次推送含 N 个既有场景线积压 commit + 本 changelog"，只报 1 个 commit 会误导。

## 大批量范围的 commit 分配
- 范围 90+ commit 时用 execute_code 脚本做全集对账：候选分配 dict + skip 集合，程序断言 `dup=[]、missing=[]、extra=[]`（每个未认领 commit 恰好归一处、无越界 sha）后才写 yaml——人肉比对在这个量级必漏。merge commit 与功能 commit 同归一个候选（用户可见面在功能侧）。

## 陷阱

- 盘点前必须先 `git log` 确认 HEAD 有没有推进（devflow worker 会并行自动提交，范围会漂移）。
- 新建候选 yaml 必须有 ISO 8601 的 `generated_at`（缺了 stage 直接拒绝"generated_at 必须是 ISO 8601 时间"），写文件时顺手加 `$(date -Iseconds)`；`commits` 列表里纯数字哈希（如 `3723893`）YAML 会解析成 int，stage 报"commits 中的提交不能为空"，数字开头的项加引号。
- 重写/合并既有 pending 条目时**必须保留文件末尾的 `review_id: F-xxx` 行**：整文件覆盖后漏掉会被 stage/validate 拒（"pending 日志缺少审查编号，请通过 stage 写入候选"），补回原编号即可。
- `changelog.py validate` 成功时**无任何输出**（仅 exit 0）：链 `&& echo ok` 或显式 `echo $?` 确认，否则看不出是否通过。`stage <file>` 只接受可读文件路径，传错名字仅报"无法读取更新日志"。
- 多会话仓库里 `git add` 只挑 changelog 文件：并行 WIP（services/scripts/tests 改动）会出现在 `git status`，整目录 add 会把他人半成品带进提交。
- releases.yaml 很大（全部历史条目），解析时只取 `releases[].entries[].commits`（顶层键是 `releases`，直接找顶层 `entries` 会取到 0 条），别把整文件读进上下文——用代码提取。
- 一个 commit 归多个候选时，按"用户可见面主要在哪"归一处，其余不重复引用；stage 的 commit 去重检查会强制这一点。
- 用户微调表述后，稳定的语言偏好回写仓库 feature-changelog Skill（用户所有，需经用户同意），一次性措辞只留在当前候选里。
