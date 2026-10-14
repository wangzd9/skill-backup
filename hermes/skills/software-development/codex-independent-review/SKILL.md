---
name: codex-independent-review
description: "codex 作独立只读代码审查者，git 增量多轮复审。"
metadata:
  version: "1.1.0"
---

# codex 独立代码审查者

让本机 codex CLI 对**固定的 git `base..HEAD` 增量**做独立、只读的第二意见审查。Use when 需要弱模型 OCR 之外、付费强模型之外的第二个独立审查视角，或用户说『调用codex审查/和之前一样调用codex』。只做审查不改码。

它和协调者工作流里的两套审查机制互补、不替代：
- **OCR（弱模型，`ocr` 工具）**：见 `complex-task-coordinator/references/ocr-and-merge.md`，串行、拆批、`--max-tokens`。
- **付费强模型**：人工触发、最小审查包，见 `strong-review.md`。
- **codex（本 skill）**：独立 LLM 审查者，带只读沙箱 + 自写探针，适合对已交付/已合入的固定快照做深度复核和多轮返修收敛。

不要用本 skill 委托 codex **写代码**（那是 `autonomous-ai-agents/codex` 的职责，方向相反）。审查者角色只读不改。

## 协调者调用约束（优先于下文历史命令示例）

协调者自动调用本Skill的Codex也属于强审查额度，不能因“第二意见”名称另算。用户明确给自定义时段/当前任务授权后，统一通过 `/home/wang/.codex/skills/complex-task-coordinator/scripts/strong_review.py` 起跑，滚动60分钟跨项目最多5次且在途串行；无有效授权仍请用户人工触发，不按历史示例裸起Codex或自动续期。该入口加载complex-task-reviewer并绑定原AC，只做最小纠错；正式输出结论使用该角色模板，旧APPROVED标签仅为历史证据，不直接批准合入。调用/授权/token与费用同时记录在协调者二十分钟报告和适用开发日志中。

## 环境（本机已核实）
- CLI：`/home/wang/.npm-global/bin/codex`（v0.162.x）。
- **无需 `~/.codex/auth.json`、无需 `OPENAI_API_KEY`**——走 hermes-managed codex OAuth 会话。起跑前 `codex --version` 核对即可，别因缺 key 误判不可用。
- 全量/最终态轮约 **10~20 分钟、10 万~13 万 token**；纯注释/单行增量收尾确认轮快得多（**3~5 分钟、4~5 万 token**），向用户报时长预期时按增量规模区分。只读沙箱下 codex 常无网络/无 pytest/ruff——它会声明"未独立复跑测试"，**这些 run 声明必须你自己复跑核实**，不能采信。

## 流程

1. **建快照 worktree**（不碰开发者分支）：
   `git worktree add /home/wang/.hermes/cache/scratch/codex-review-<n> HEAD`
   记下 `base`（上一审查快照或交付起点）与 `HEAD`。审查范围 = `base..HEAD`。

2. **起跑（只读沙箱 + 后台 + pty + notify）**：
   ```
   cd <worktree> && codex exec --sandbox read-only "<reviewer prompt>" \
     > /home/wang/.hermes/cache/scratch/codex-review-<n>.log 2>&1; echo "codex-exit=$?" >> ...log
   ```
   用 terminal `background=true, pty=true, notify=true`。**pty=true 必须**（无 pty 会 hang）。若 sandbox 报 bubblewrap/uid-map/`RTM_NEWADDR` 错，按 `autonomous-ai-agents/codex` 的降级口径改 `--dangerously-bypass-approvals-and-sandbox` 或 `--sandbox danger-full-access`（审查场景可接受，因无写意图）。

3. **reviewer prompt 模板**（固定结构，务必给足背景让它独立重验而非复述）：
   - 第一句："你是本次提交的独立代码审查者（reviewer），只做审查不做修改。"
   - 仓库事实：技术栈 + app/迁移/测试目录布局 + 生产/测试数据库与连接语义（如 SQLAlchemy/PyMySQL 默认 `CLIENT_FOUND_ROWS` 与否——这类边界要写进 prompt，否则它会误标）。
   - 审查范围：`base..HEAD`（N 文件 +X/-Y），"用 `git diff base..HEAD` 查看"。
   - **若为返修复审**：贴上一轮你（codex）的发现 + 协调者裁决的修复**口径**，要求逐条验证"是否真修对、修彻底"，并提醒"不要假设前轮结论仍成立，基于当前 HEAD 独立重验"。
   - 具体编号审查维度（迁移链/夹具/no-op 检测/回归充分性/连接语义/通用+组合问题）。强调"单看某 commit 看不到的组合问题"。
   - 输出格式：逐条结论 + 文件:行号 + 理由（含它的探针实测输出），问题按 `[blocker|high|medium|low|info]` 分级，最后总结论 `APPROVED / APPROVED_WITH_COMMENTS / REWORK`。

4. **收口后独立裁决**（关键——codex 的每条 finding 是**声明**，不是事实）：
   - `grep 总结论 <log>` 定位结论段，`sed -n` 读正文；token 用量在 `tokens used` 行，收口时间用 `stat` mtime / `date` 实查（跨天表格带日期）。
   - **先读它的"本轮实际完成 / 未复跑"声明再决定自己补验哪些**：codex 只读沙箱的工具可用性逐轮不同（同一项目里有的轮次无 ruff/pytest 只能静态+探针，有的轮次能跑 ruff、内存编译、内存 SQLite 升级、真实服务函数探针）——不要预设它跑或没跑，以当轮声明为准，只独立补验"未复跑"部分；它声称跑过的探针输出也要抽查可复现性。
   - **逐条 re-derive**：边界值矩阵、AST 比对、grep、复跑 pytest/ruff、查存活库/迁移链等。codex 探针可信但你要能复现；它"已修对/未修对"的判定要自己核代码坐实。
   - **"存在库已应用旧状态/存在引用残留"类反例前提重点核**：先查全部存活库 + 隔离测试库生命周期——隔离库用完即毁，"TESTER 曾加宽列后应用过旧 revision"不构成残留反例，此类意见可改判为"迁移/代码记录适用边界 + 人工升级语句"而非回退代码（曾两度按此驳回 codex 的版本映射建议）。
   - 分诊：真问题→回派开发 owner；需运行证明的疑点→派 TESTER；误报→注明代码/运行依据；范围外建议→不自动进 TODO、不派单、不阻塞。
   - **文档残留（仓库外报告/回执表述错误）协调者直接改，不开派单**：当前生效文档改正文消除自相矛盾；**历史材料只在顶部追加统一纠错标注、不篡改正文**（保留可追溯性）。仓库内测试/代码文件的 docstring 残留按"协调者不改代码"红线回派 owner。连接语义类表述错误要 grep 全仓（reports/receipts/HANDOFF/注释）确认全清，并写进下一轮 prompt 与强审查包，防下游材料继承错误口径。修正某文件时顺带整文件自扫占位时间（`x`/`xx`/`HH:MMx`）和同类旧错——审查轮揪出文件 A 一处表述错误时，同文件相邻行或同系列其他文件常还有第二处（时间占位、同一误称换行复现）。

5. **落裁决 + 派单**：`reviews/codex-<id>-<date>-verdict.md`（DECIDED，含 codex 结论、你独立核实结果、分诊处置、记账的未复跑项）。回派用 inbox 下 attempt-N 返修单，写明 base、口径、验证条件、解除门禁。

6. **清理** worktree：`git worktree remove <scratch> --force`。

## 多轮收敛与停止条件
- 质量随轮次收敛是正常模式（实测：R1 REWORK 3 条 → R3 REWORK 1 阻塞 → R4 APPROVED_WITH_COMMENTS 0 功能问题 → R5 仅 1 条 info）。
- **停止条件**：codex 给 `APPROVED` 或 `APPROVED_WITH_COMMENTS` 且无 blocker/high，你独立核实通过 → 代码侧审查链收口，剩门禁交 OCR/强模型/测试者。
- **单行/纯注释收尾修复不再起新一轮 codex**：协调者用 AST 比对（去掉 docstring 后 `ast.dump` 相等）+ 定向测试直接验收合入即可，不要为一行注释陪跑一个 10 万 token 审查轮。**但用户显式重复同一"调用 codex"指令时，该指令优先于本启发式**：R2 末段用户对 rework-8、rework-9 两笔单行 docstring 合入都下了"和之前一样调用codex"——用户建立的是"每笔新合入都过 codex 确认"的节奏，照跑收尾确认轮即可（增量小、几分钟），不要在 TODO 里写"不再起下一轮"的预判——用户会连续打回。协调者/化身写收口记录时，对"是否再起下一轮"保持开放表述（"若用户继续指令则按增量起"），不替用户预判停轮。
- **用户重复同一指令而 `上次审查head..HEAD` 零增量、inbox 无在途交付时，不跑空 diff 轮**：改为**全链最终态收口确认**（范围 = 原始 base..HEAD，prompt 模板见 [最终态收口确认轮](references/final-state-review.md)——A 节逐条复核历轮意见全部落地且无回退，B 节全链视角找单 commit 审不到的组合问题）。若上一轮已是最终态 APPROVED 且仍无新交付，如实说明无可审增量并给状态，不硬造轮次。
- **历轮意见"修完又被后续轮覆盖回去"是多轮返修的典型回退点**：每轮 prompt 必须要求 codex 重点核"口径一致性"（注释与代码、断言与实现、历轮修复互不冲突），最终态轮尤其如此。
- **时间精度**：面向用户的结论与落盘文档里所有时间用真实 `date` 值（HH:MM），禁止 `xx`/`1x`/`HH:MMx` 占位；发送前自扫一遍正文。

## 常见坑
- 忘 `pty=true` → codex hang，误判为慢/卡死。
- 把 codex "未独立复跑" 的测试/ruff 声明当成通过 → 必须自己复跑。
- 返修复审 prompt 只说"验证上轮"却没贴裁决口径 → codex 会按自己的理解验，漏掉你认定的边界。
- 连接/方言语义（`CLIENT_FOUND_ROWS`、DATETIME 精度、SQLite 不校验列宽）是反复出问题的根源 → 写进 prompt 并在裁决时实测核实，别靠推断。
- 审查期间 `base..HEAD` 若被推进（新合入），旧快照审查不再覆盖新产物 → 用新 base 重跑受影响范围。
