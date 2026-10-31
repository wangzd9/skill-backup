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
