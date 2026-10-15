# 生产水雾原样脚本调用核验与主动停止

后续用户要求小包全流程验收，已完成真实识别和修复后生产自动闭环；见[小包全流程验收](../spray-script-small-e2e-20261015/REPORT.md)。本文件保留大包主动停止时的历史事实。

2026-10-15，北京时间。用户将本轮目标收敛为验证生产调用方式，要求停止大包；本记录不将调用核验等同完整切片入库验收。

## 已验证事实

- 上游ros2_slice固定deb8be8515ffaa8b556e442332725e876dc2f2ca，原样程序在生产独立容器运行；识别、抽帧、分段算法未修改。
- 生产平台发布107a34d，本地同版本mock闭环PUBLISHED与帧导入通过，stale=false。默认源UID映射及既有编排/接线30项定向测试通过。
- 首次正式run scene-b169261e013f42b09a8f2630cf12因非默认参数无法还原而SCENE_FAILED；守卫在认领前拒绝，原记录保留。
- 修复后以空参数创建scene-efc4b70d23d147e784c2cd047488，正式SCENE_RUNNING，网页200且含对应运行状态。platform-claim.json已核源UID、generation、fingerprint、上游commit、config_hash及run_id一致；成功接续同一已运行脚本，没有再启动模型任务。
- NAS topic计数导致预检缓慢。metadata及双DB3本地副本重新逐文件SHA256校验一致，以只读文件挂载映射到代理原源路径，代理同run恢复后成功认领。原始NAS包未修改。

## 停止结果

用户要求停止后，正式API取消该run，最终为CANCELLED、无租约；原脚本容器exited、Running=false、OOMKilled=false；场景proxy active_runs=[]，独立monitor已退出。停止时间约13:36，保留缓存、计时、数据副本和运行证据。

包装程序退出时触发ROS重复shutdown异常，status.json记录FAILED、容器exit1；平台按用户取消准确收敛为CANCELLED。该清理异常如实保留，不将主动停止描述为自然成功，也不擅改上游算法。

## 阶段性能（并非全包最终结果）

| 指标 | 停止前实测 |
|---|---|
| 样本实际时长 | 537.245805921秒，并非一小时 |
| 有效识别帧 | 122，unknown=0 |
| 单帧最终判断平均耗时 | 46.365秒，含原双pass及重试，排除缓存命中 |
| 中位数 / P95 | 40.596 / 111.398秒 |
| 原PCD | 5373帧，约10Hz |
| 候选抽帧 | 跳前10、每3帧取1，1788帧，约3.333Hz |
| 小图过滤后有效JPEG | 1781帧，约3.322Hz |

## 验收范围和剩余事项

本次已验证生产原样脚本调用、真实推理、正式任务认领及取消；完整大包切片、NAS发布、生产入库尚未跑完，按用户收敛要求停止，不声明全链路无问题。通用自动启动任意源包也未实现，当前为受控既有脚本作业登记接入。

固定OCR prepare成功，start因LEGACY_OCR_BUSY未执行模型审查，按用户此前授权不阻塞水雾发布。全仓四项既有基线失败已独立复现；开发分支保留，未合入main或推送。

证据根：/home/wang/work/.local/ros2-slice-call-20261015/evidence，包含platform-user-stopped-final.json、metrics-user-stopped.json、first-platform-run-failed.json、platform-default-map-tests.log、deploy-default-map.log和processing-default-map-check.log。生产数据作业根：/home/data-process/spray-runtime/20261015-deb8be8/run-large。
