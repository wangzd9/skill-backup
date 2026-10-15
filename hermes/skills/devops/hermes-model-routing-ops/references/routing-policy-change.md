# 改选路策略（choose_route）的完整流程

## 归属

- `~/.hermes/plugins/role-workflow-runtime` 是指向源码仓库 `/home/wang/work/hermes-role-runtime` 的软链——编辑源码仓库即编辑生效插件。
- 策略函数：`model_routing.py::choose_route`；模式校验与回执文案在 `ModelRouter.set_mode` / `ModelRouter.command`。
- 复用同一策略的第二个调用方：`ocr_model_route.py::select`（OCR 每批启动前调用 `router.decision()`）。
- 配置面：`~/.hermes/config.yaml` → `plugins.entries.role-workflow-runtime.settings.model_routing`；模式面：`~/.hermes/runtime/model-routing-state.json`。

## 步骤

1. 确认仓库干净：`cd /home/wang/work/hermes-role-runtime && git status --short`。非空时先分清哪些改动属于本任务，只提交本任务文件；同环境经常有并行会话在改同一批配置和仓库，不要把自己的改动和别人的混在一起。
2. 枚举翻转输入（先做，再改代码）：列出改动前后 `choose_route` 输出不同的输入组合，逐格确认未知输入（`running`/`waiting` 为 `None`）仍然判忙，并用真实指标跑一遍当前判定留证（只读 GET metrics，不调用模型；产出脚本放分析目录，比在对话里手算可信）。翻转落在饱和区是知情项、不是否决理由（见 SKILL.md 同名条目）。
   - **用户说"改掉这个限制，只看 X"时，先把当时真正在拦的所有闸门都找出来，不是只改他点名的那一个**：用户指出的往往是最近刚看到的那道（如流水线忙闲），而同一时刻另一道（如白天时间窗）也在拦——只改点名的那道，用户的问题依旧存在，等于白改一轮。先按当时时刻把三道闸门逐个算一遍（哪个返回 cloud 就打印它的 reason），把"实际拦截者"清单报出来，再按用户意图一次性移除他不要的所有闸门。
3. 改 `model_routing.py`，只动必要条件；同时对齐 `ModelRouter.command()` 的回执文案与 `README.md`/PRD 里的"自动规则"描述——用户会照回执与文档理解行为，文案与代码不一致会被当成 bug。
4. 同步测试断言：`tests/test_model_routing.py` 里直接枚举 `(metrics, expected)` 与 `(mode, expected)` 组合断言 cloud/local（当前为 `test_local_admission_uses_queue_only` 与 `test_mode_short_circuits_cloud_without_probe`），改闸门必须同步改这里，否则单测红。**删闸门时同步删掉模拟该闸门的用例与 `datetime(...)`/`hour` 参数**，不要让旧闸门的断言留在文件里——下一个会话会照它以为那道闸门还在。
5. 跑单测（必须用 hermes 自带 venv 的 python，系统 python3 缺依赖）：
   ```bash
   cd /home/wang/work/hermes-role-runtime && \
   /home/wang/.hermes/hermes-agent/venv/bin/python -m unittest discover -s tests
   ```
   判定只看末行 `OK`/`FAILED`；输出中间可能出现 `ValueError: bad config` 之类的回溯，那是用例主动触发的异常路径后打印的现场，不是失败。单文件快跑：`... -m unittest discover -s tests -p 'test_model_routing.py'`。
6. 评估 OCR 影响：同一 `decision()` 也决定 OCR 走本地还是云端。方向别搞反——**OCR 本就该用弱模型、优先本地**，落到本地 Qwen 是期望行为，不要写成"代价"。要交代的是相反情况：新策略把 OCR 推到强云端，或 `ocr_profile` 的取值让两者选路不一致。
7. 热加载插件（代码改动不会自动生效，也不要靠重启网关——本机重启会短时中断全部通道）：
   ```python
   import sys; sys.path.insert(0, '/home/wang/.hermes/hermes-agent')
   from pathlib import Path
   from gateway.control_socket import reload_gateway_plugins
   print(reload_gateway_plugins(Path('/home/wang/.hermes')))
   ```
   期望返回 `reloaded` 为真且该插件 hooks 处于激活态。
8. 验证四件：`hermes config get plugins.entries.role-workflow-runtime.settings.model_routing`（配置面）、`cat ~/.hermes/runtime/model-routing-state.json`（模式面）、群内 `查看模型状态` 回执的"原因"文案与 "模式" 是否与新规则一致（控制指令不调用模型）、真实生效看 `~/.hermes/state.db` 的 `session_model_usage.model` 是否按新规则变化。策略类改动另跑 `scripts/verify_routing_decision.py`（只读）留一条真实判定＋原因作证。
   - **改完若 `state.mode` 是 `cloud`，策略变化看不到任何效果**（`cloud` 短路、不看队列）。用同一仓库的 `ModelRouter.set_mode('auto')` 切回 auto（保留 `cloud_profile`）后再验；这改变全部已绑定角色的选路，切前告知用户。
9. 回退：`git revert` 或恢复原提交后再热加载一次；**不要整份覆盖 `config.yaml`**，会踩掉其他并行会话的改动。

## 坑

- **热加载会重建 hook、不会打断在途回合**：`reload_gateway_plugins` 走 `discover_plugins(force=True)`，先 `unload()` 清掉插件子模块再重新 exec（`_clear_plugin_submodules`）。实测一次热加载后 hook 全部重挂，且不需要重启网关（用 `systemctl --user show hermes-gateway -p MainPID -p ActiveEnterTimestamp` 对比前后证明 PID/启动时刻未变，比口头声称更硬）。
- 单测全绿不等于闸门正确：`running`/`waiting` 是请求前快照，检查与提交之间存在竞态；改动只能收窄或放宽语义，不能消除竞态，也不能声称"不会再排队"。
- `auto`/`local` 现在走同一判定（只看队列），所以"改完负载条件后白天没变化"不再是预期——队列空就该变本地。若实测没变，检查两件事：`state.mode` 是否被钉在 `cloud`，以及那一刻 `waiting` 是否真的为 0（排队意味着自动回云端，不是改动失效）。
- 负载闸门的可观测依据是 `llm-service-load-check` 里的端点指标；承诺"改完会用上本地"前先看当下 `running`/`waiting`，否则等于用规则换了个说法。
