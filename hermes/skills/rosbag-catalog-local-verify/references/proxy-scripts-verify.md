# pipeline 执行代理脚本改动：函数级驱动验证

本地 mock 栈的 `local_pipeline_proxy.py` 是简化模拟代理，直接 POST 状态机推进，不经过 `phase0_host_proxy.py` / `a100_host_proxy.py` / `self_built_pipeline.py` 的真实执行与失败收敛代码。所以改这些脚本的行为（尤其错误文案、失败原因提取）时，验证只能靠单元级驱动，不依赖 Compose 栈。

## 远端失败原因存放位置（A100 侧契约）

- Phase1：`<A100_RUN_ROOT>/<run_id>/phase1_run.json` 的 `error`/`exit_code` 字段，日志同目录 `phase1.log`。
- Phase3：`<A100_RUN_ROOT>/<run_id>/phase3_run.json` 的 `error` 字段（具体门禁/clip 原因），日志 `phase3_nav.log` / `phase3_cot.log` / `phase3_cot_llm.log` / `phase3_plot.log` / `phase3_description.log`。
- 这些文件在 A100 测试根的 Git 工作区脚本（`run_phase1_job.py` / `run_phase3_job.py`）里维护，平台仓库 `scripts/self_built_pipeline.py` 只通过 SSH `cat`/`tail` 读取；改失败文案时两边契约要对齐，读不到的分支必须有兜底措辞。

## ssh 桩写法

`deps.ssh` 的 mock 用 `SimpleNamespace` 返回，按命令特征分流：

```python
from types import SimpleNamespace

def ssh(command, **kwargs):
    joined = " ".join(command)
    if command and command[-1] == "cot" and not kwargs.get("capture"):
        raise subprocess.CalledProcessError(2, command)   # 模拟步骤命令失败
    if "cat" in command and "phase3_run.json" in joined:
        return SimpleNamespace(stdout=json.dumps({"status": "FAILED", "error": "...具体原因..."}), stderr="")
    if "tail -c" in joined:
        return SimpleNamespace(stdout="log line 1\nlog line 2\n", stderr="")
    return SimpleNamespace(stdout="", stderr="")
```

注意：真实 `run_ssh` 只在 `capture=True` 时返回带 `.stdout` 的 CompletedProcess，桩要覆盖成功路径的 `capture` 调用，否则 `json.loads(...)` 拿不到 stdout。

## 轮询函数的桩约束

`start_or_reconcile_phase1` / `start_or_reconcile_nvidia` 内部是 `while True` 轮询，异常分支 `except Exception` 后继续 sleep 重试。测试桩若用 `iter([...])` 耗尽抛 `StopIteration`，会被这个 except 吞掉，测试无报错挂到 timeout。必须用恒定返回值的 lambda（`lambda *_args: {"status": "FAILED", ...}`）或保证状态序列永不耗尽。

## 快速冒烟（不进 pytest）

改动后可用 venv 解释器直接驱动函数、真实打印新错误文案：

```bash
.venv/bin/python -c '<脚本：import + 构造桩 + try/except 打印>'
```

每个失败分支（门禁逐项、远端 FAILED/EXITED、步骤命令失败）各跑一条，确认文案含具体原因关键字，再跑全量 pytest。

## 自动重试策略约束

`app/domain/retry_policy.py` 对 `error_message`（大写后）做子串匹配：

- 非可重试标记：`QUALITY_FAILED`、`INPUT_INVALID`、`FINGERPRINT_CONFLICT` 等 → 质量门禁失败必须保留 `*_QUALITY_FAILED` 前缀，不能被改文案丢掉。
- 可重试瞬态：`EXIT CODE -9` / `EXIT CODE 137` → 进程被杀场景的文案里要带 `exit code N` 字样。
- 新文案写完跑 `tests/unit/test_retry_policy.py` 和 `tests/test_pipeline_api.py` 确认分类行为未变。

## manifest hash 校验失败：先怀疑拼接顺序，别急着怪字节损坏

发布/校验步骤报 `*_VERIFY_FAILED: manifest_hash`（或任何 digest 不匹配）但 `file_count` 与 `total_size_bytes` 都对得上时，字节几乎肯定没问题——**先怀疑摘要的拼接顺序**。`local_manifest` 会对文件排序后把 `path\tsize\tsha256` 逐行连接再取 hash；只要排序规则与远端 manifest 生成器不同，每个文件都逐字节正确，总摘要也必然不同。

- **Path 对象排序 ≠ 字符串排序**，在前缀碰撞目录上顺序相反：`calibration/camera_intrinsics/…` 与 `calibration/camera_intrinsics.offline/…` 并存时——按 Path 组件排是 `camera_intrinsics` < `camera_intrinsics.offline`（组件串比较，`.` 排在末尾）；按完整路径字符串排是 `…intrinsics.offline/…` < `…intrinsics/…`（`.`=0x2E < `/`=0x2F）。一个 chunk 只要同时含 `X/` 和 `X.offline/`，跨排序规则校验就 100% 失败，重传多少次都没用。
- **不同链路的排序规则不同，必须核对源端 manifest 而非假设**：NVIDIA origin 下载清单按相对路径**字符串**排序（源端 `quality_validator`），自建 Phase3 final 清单按 **Path** 排序。调 `local_manifest` 时 `sort_key` 要对齐该 source 的实际生成规则——拿真实 manifest 文件用两种排序各重算一次声明 hash，哪个匹配哪个就是真规则。
- **诊断陷阱（本轮最贵的教训）**：手写复现脚本若**自己内联实现**了 hash 校验，会和生产的 `local_manifest` 走不同的排序分支，于是复现永远 MATCH=True、生产稳定失败，看起来像“传输/CIFS 随机损坏”。排查生产 verify 失败时，复现必须**直接调用那个生产函数**（`local_manifest` / `publish_nvidia_origin_to_nas`）或对它的算法逐行 diff；“rsync 命令一致”不等于“校验算法一致”。
- 修好后加一条含 `X/…` 与 `X.offline/…` 前缀碰撞布局的回归用例——普通平铺路径测不出这个 bug。
