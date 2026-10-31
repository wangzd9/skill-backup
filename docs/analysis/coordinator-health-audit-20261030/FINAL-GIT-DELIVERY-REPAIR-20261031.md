# 最终 Git 交付原生路径修复（2026-10-31）

## 结论与当次基线

确认为机制缺口。群不开放通用 shell 是既定角色隔离；但没有最终 Git 交付控制动作、精确持久授权、后台执行器及结果投影，把候选合入授权误用到已完成需求后没有合法接续路径。

2026-10-31 09:56 +08:00 当次 current_input：水位15017，需求 prepared-rosbag-catalog-a100-multi-node，本地验收 COMPLETED（事件14612），冻结版本 e82af00254ddd4dc67a823b142f0f7c6c4d5d596，next_actions为空。不是历史checkpoint推断。

Git fetch github后：main=2313bb14，feat/a100-multi-node=e82af002，分叉10/14提交；main独有提交只涉及HANDOFF.md、compose.processing.yaml、docs/data-upload-guide.md、docs/dataset-consumer-handoff.md，与MN实现/测试零文件交集。merge-tree --write-tree真实预演退出0，树0e64e053a5f96346e2e559882df2383feaf6c5f6。未操作处理机。

## 根因与改动

1. 原控制enum和schema没有最终交付动作；authorize_merge绑定task/attempt/base/AC，合法范围是候选→集成分支，不能覆盖feat→main与push。
2. 新增request_final_delivery：当前可信原文明示source、main、remote和本地栈；按已完成需求冻结准确SHA、remote摘要、脚本哈希及分叉审查。使用独立消息幂等槽，旧错误候选授权不占用本动作。
3. 新增后台finalize_delivery(request_id)，仍要求原生COORDINATOR身份、当前ACTIVE代次和writer；固定fetch、merge、push及远端SHA核验、mock up/test、quality_gate，不开放任意shell/argv。
4. Git与本地栈各阶段独立START/真实执行结果/证据哈希；原生工具子进程登记。重复command或新回合不重复成功步骤；中断有成功回执先对账，没有真实结果保留UNVERIFIED，失败BLOCKED。每阶段重新核原验收、分支、remote及实际dirty。
5. next_actions在需求验收完成后继续返回真正交付命令；role_live_progress、progress_context和七区回退分别报告最终交付及失败步骤，不把已验收特性分支冒充main已发布。
6. 最长操作观察宽限按固定步骤实际deadline总和加180秒。子进程移除Git/Docker/本地路径和测试数据库继承覆盖，固定本机Docker与mock测试根。

## 验证与上线证据

隔离回归使用真实Git和bare remote、显式标记的合成验收与本地栈替身，覆盖分叉、FF、冲突、dirty、源漂移、远端推进、角色与原文授权、失败留痕、重复幂等、中断不重放、成功回执对账及独立消息槽。原需求验收及writer/测试/OCR门禁仍由原有回归核验，不用合成结果替代生产验收。

回归日志：/home/wang/work/.local/final-git-delivery-20261031/regression.log。源码发布、Gateway热加载和真实交付结果见同目录发布回执及本文件后续记录。

## 边界

只操作本地仓库和本地mock栈；push仅冻结remote/main，不force，不部署处理机，不授予生产权限。冲突或双方文件交集停止待审，git成功/容器健康/历史checkpoint都不等于全部验证通过。BLOCKED/UNVERIFIED必须保留已完成结果和下一真实阻塞，不通过更换ID盲重试。

### 已上线采样（10:06 +08:00）

运行时commit c6d2979fc58820303832965ed6fd44ab4d59b3fb，版本0.17.0。140项回归及7项子测试通过（81.92秒）。Gateway PID3612975原生热加载，69项源码SHA一致、飞书connected，未重启。生产preflight通过；维护入口按本次直接用户授权登记e15032（不伪造飞书消息身份），current_input已给出finalize_delivery；当次原writer仍运行，wakeup保留请求等待原owner。此采样尚未合入/push，不冒充完成。


## 最终核验：10:15 +08:00

- 当前机制版本0.17.2，commit `a784b48252aed84cc964967c11b833905aaec772`；包含最初交付入口 `c6d2979` 及后续投影修复 `0b672f7`。
- 原生COORDINATOR在原业务work内实际执行了请求，不由维护者冒充其writer。e15032登记请求，之后各阶段START、tool process及结果均原生入账；e15054记录质量门禁阻塞。完整事件ID与实际PID、退出码、文件/日志SHA见[上线回执](final-git-delivery-rollout-20261031.json)。
- main最终HEAD及GitHub远端均为 `ba9e367bee4bb0db48581cdb2a88b1fac3bc9c47`，merge和push退出0，仓库干净。真实分叉合并没有冲突，没有重写main既有10个提交。
- `local_stack.sh --mode mock up` 与 `test` 均退出0；模拟流水线 `bd4a220fac584dfb89fe58978ca4084f` 达到PUBLISHED、DatasetFrame已导入，本地版本对账没有漂移。没有连接处理机或部署生产。
- `local_quality_gate.sh` 真实失败，退出1。首个检查Ruff发现12处E501：`tests/test_task16cc_scene_detail.py`（148/149/198/353）与`tests/test_task_rcc05_compose_integration.py`（265/266/267/268/284/287/440/568）。已用Git blob核验这两份文件与合并前main完全相同，属于已有全仓规范问题；不能把它说成此次MN合并导致的业务失败。
- 因首步失败，后续格式检查、编译、pytest、浏览器和Compose质量检查尚未执行。上面的本地模拟链路通过不代替这些检查，不宣布完整交付成功。
- 当前明确阻塞为 `BLOCKED / quality_gate / FINAL_DELIVERY_QUALITY_GATE_FAILED`。下一项是修正上述原有测试格式问题后重新完成全量质量门禁；不要重复合并、push或冒充该门禁通过。未扩大本轮到与MN无关的测试源码修改，也未改业务实现。

### 生产进度投影追加修复

完整阶段回执最初直接加入8KiB进度核心，真实生产数据使其超过预算。后续先保留逐步artifact、压缩单项历史，再将live入口投影为当前state/stage/head/code、已完成数量和push核验事实；完整阶段、证据和授权原件仍在哈希artifact内。生产最终响应8080字节，水位15054，真实入口已恢复；不增加预算、不丢任务/角色/门禁、不用历史checkpoint兜底。失败阶段之后的新执行回执还会显式保留真实退出码及哈希引用；原e15054历史没有改写，原退出1在不可变上线证据中留存。

补丁回归27项通过；Gateway PID3612975再次热加载，69项源码SHA一致、Feishu connected，未重启。代码/文档快照另同步既有skill-backup GitHub仓库，发布及当前结果分别记账。
