# Hermes 模型切换实施记录

核对时间：2026-10-15 16:47:28 +0800。**已实现并启用**。网关PID `1193362`，状态 `running`，飞书 `connected`。没有主动发送真实飞书测试消息。

## 当前使用

当前模式为自动，选定云端 **x-coding / gpt-6.1-sol / High**，主推理与新OCR批次读取同一持久设置。北京时间21:00—次日08:00，仅流水线明确空闲且vLLM运行／等待均零才用本地；其他或未知状态用选定云端。保留人工云端及本地优先，本地优先不绕过忙闲保护。

绑定角色群可发送：`查看模型状态`、`切换到GPT`、`切换到DeepSeek`、`切换到云端模型`、`恢复自动切换`、`切换到本地模型`。对应 `/llm status`、`/llm cloud xcoding`、`/llm cloud sub2api`、`/llm cloud`、`/llm auto`、`/llm local`。精确控制由程序处理，鉴权且群绑定匹配才操作；忙碌入口也处理，不入业务队列、不调用模型。设置影响本机全部绑定角色和后台poll及可追溯子会话。

Sub2API目录含deepseek-v4-pro及多种flash型号；现场原生Agent曾验证pro的流式、终端工具和结果接续成功。部署过程中当前Sub2API模型配置另变为deepseek-v4.1-flash，保留该已有配置；切回DeepSeek时使用当时配置，不擅自覆写。numenume保留配置，目录仅flash/pro，实测推理502／超时，不作为默认。

x-coding目录列出gpt-5.5和gpt-6.1-sol等；gpt-5.5真实调用报当前上游账户不支持。按用户最终指示改为gpt-6.1-sol，Hermes原生reasoning_overrides为high，OCR原生provider extra_body为reasoning.effort=high、store=false。供应商偶有暂时不可用，真实Hermes验证经原有限重试后成功，不承诺零等待或实时抢占。

## 代码与边界

插件维护源 `/home/wang/work/hermes-role-runtime`，分支 `feature/model-routing`。策略在 `model_routing.py`，OCR桥接在 `ocr_model_route.py`。插件由现有软链接安装。Hermes核心提供通用 `resolve_model_route` 和 `pre_gateway_busy_dispatch` hook；策略仍在插件，不用monkeypatch。

每个物理主推理请求构造前重新选路，含工具循环与重试；原生switch_model更新客户端和元数据，保留冻结system前缀与已执行工具结果，覆盖本次request_overrides并清除过时fallback。任一策略否决优先，异常／超时／不完整路由阻止原供应商请求。白天和人工云端模式跳过负载GET；夜间准入最多流水线与指标两次GET，各超时2秒。

OCR是OpenCodeReview代码审查，原入口 `~/.codex/skills/complex-task-coordinator/scripts/ocr_runtime.py` 升级1.6.0。新run冻结准许的provider/model/endpoint集合和选路器哈希；**每个新批次**读取同一模式与供应商选择，显式传provider/model，保存execution_route，并按该批身份核对manifest、诊断和结果JSON。在途批次先结束，不中途重启或重做覆盖。原双锁、独立systemd、任务身份、全覆盖、不完整停链、补审限额与语义裁决保持。旧1.5冻结runner继续旧行为，历史读取兼容。

OCR原生命名provider使用api_key_cmd从同一home读取凭据；运行时不逐批改写共享配置，密钥不进入spec或日志。新增provider是在本次维护阶段一次写入；后续修改共享OCR配置或选路器会使冻结run停链，须核原授权和已完成范围后重新prepare，不能篡改旧spec。

本方案是准入检查，不能消除检查和提交之间的竞态，不能抢占已发出请求。独立辅助调用、外部强模型审查及数据流水线沿用自身配置，未冒称全部受此主推理hook控制。

## 验证证据

- 插件全套 **165项通过**：包含时间边界、忙/未知、模式和供应商持久化、权限/群隔离、原生busy入口、真实A→B→A配置/凭据隔离、OCR同源选择和端点漂移拒绝。日志 `/home/wang/work/.local/hermes-model-routing-20261015/plugin-tests.log`。
- Hermes规范runner相关5文件 **127项通过**。日志 `/home/wang/work/.local/hermes-model-routing-20261015/core-tests.log`；测试依赖装在独立test-venv，未修改运行venv。
- OCR固定runner **17项通过**，真实隔离systemd验证跨批次模型变更后coverage/manifest一致；包含1.5历史可读性、锁、覆盖、失败停链和重复阻断。OCR结果取证 **14项通过**，路由缺失/越权/manifest模型不符继续证据不足。日志 `/home/wang/work/.local/hermes-model-routing-20261015/ocr-runtime-tests.log` 与 `ocr-results-tests.log`。
- 条件变异：在隔离副本倒置流水线判忙条件，原测试确实失败；原实现保持并通过，不削弱保护断言。
- 原生HTTP桩端到端：本地Chat Completions返回terminal调用，真实执行一次printf；同一工具循环下一请求切Responses，收到一次真实工具结果；system前缀一致、store=false、SSE正常；再切回本地不泄漏云端参数。脚本 `hermes-role-runtime/scripts/validate_model_routing.py`，日志 `/home/wang/work/.local/hermes-model-routing-20261015/e2e.log`。
- 真实Sub2API：原生Hermes Responses流式、一次终端工具与结果接续成功。
- 真实x-coding：原生Hermes gpt-6.1-sol High流式、一次终端工具与结果接续成功；供应商暂时不可用曾触发原有限重试。
- 真实OCR隔离Git小函数审查：使用hermes-xcoding / gpt-6.1-sol完成，退出0、status=complete、一次code_comment工具。`/home/wang/work/.local/hermes-model-routing-20261015/real-xcoding-ocr-2.log`。初次60秒诊断超时保留在 `real-xcoding-ocr.log`；按正式900秒请求超时再验成功，未降低应用门槛。这仅是接口与执行验证，不是生产功能审查/验收批准。
- 网关原生reload-plugins返回role-workflow-runtime的两个新hook已激活、Feishu adapter已重接线。未凭fake投递宣称真实飞书自然消息已送达。

## 部署与回退

开始时维护排空停留过久导致用户看到draining提示，已解除。最终用一次性`restart-when-idle.py`等待期间保持聊天开放，聚合在途工作数为0才短暂排空重启；日志 `/home/wang/work/.local/hermes-model-routing-20261015/deployment.log` 记新PID及Feishu connected，维护标记最终清除。没有强杀在途角色或OCR，没有更改角色任务/ACK/启停。

供应商及路由配置由hermes_cli.config.atomic_config_write维护，凭据在本机.env，权限600。供应商彼此命名隔离；未修改本机Codex配置，未把Codex TOML里的goals/WSL/review_model误映射为Hermes行为。

正常止损可在群发 `/llm cloud sub2api` 切到另一已配置云端；或 `/llm cloud xcoding` 保持GPT。要停用功能，先核在途请求和OCR冻结配置，再将model_routing.enabled/ocr_enabled置false；停用会恢复原模型路径，必须评估本地资源。代码回退只撤销本次通用核心补丁与插件改动，保留原用户cron修改。配置备份在 `/home/wang/work/.local/hermes-model-routing-20261015/config.before.yaml`、`ocr-config.before.json`，可能含凭据，禁止公开或整份盲目覆盖后续用户配置。

OCR维护源未在Git中，本次源码快照保存在 `/home/wang/work/.local/hermes-model-routing-20261015/ocr-source-final`，身份如下：

- `/home/wang/.codex/skills/complex-task-coordinator/scripts/ocr_runtime.py`：`dbc903860ec7b6a33d4e7493594a5e0a2cd867309fb389c57a888e7e264a9006`
- `/home/wang/.codex/skills/complex-task-coordinator/scripts/ocr_results.py`：`3c2fb58fbe370f27d8d97e28759e98d45692be0ae1a86c2e1dc2151de103c651`
- `/home/wang/.codex/skills/complex-task-coordinator/scripts/test_ocr_runtime.py`：`5bfd5adca444e5e123560f854b67b2f0726dd609cd3171cd4076972fab9514ca`
- `/home/wang/.codex/skills/complex-task-coordinator/scripts/test_ocr_results.py`：`4ec6a50ce34581ce5bf740501dd868a4563bd92b6d9ddde2ad82b9ea8d7a8c66`

提交：Hermes核心 `a101a612e3b7a119f47507cb21e1e1772276dc8d`；插件 `a5b5335f57a45defe3ca260dddcbe07a88003cd3`。两仓均在feature/model-routing；未推送。用户原有cron修改保留在工作树，未纳入提交。

最终复核：网关PID `1259230`，running、Feishu connected，两个通用选路/忙碌控制hook再次确认激活，维护标记不存在。期间另观察到网关重新启动及用户在飞书操作原生/model与/stop；没有改写这些消息或业务任务。统一切换请使用本文/llm指令，原生/model不改本插件的共享选路状态。
