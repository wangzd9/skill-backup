# 本机 OCR 固定执行流程（runtime 1.5.0）

适用：本机 Hermes 协调者的常规 OpenCodeReview。入口是 `/home/wang/.codex/skills/complex-task-coordinator/scripts/ocr_runtime.py`，只使用已核验 v1.12.13、`vllm-local` 永久LAN端点。其它项目/平台先验证工具与运行环境，不能硬套本机路径。强模型审查、开发、独立测试和合入职责不变。

## 固定顺序

1. **冻结交付并写背景**：核对用户当前授权、task/attempt、开发交付与独立测试条件；取得实际 base/head，使用无已跟踪改动且HEAD等于交付的隔离worktree。背景文件引用原REQ/AC与版本、关键不变量、允许行为、明确本期范围、测试证据及待核实风险。不是把规划摘要当代码，也不另造需求。
2. **prepare，零模型预检**：入口解析完整SHA，核对差异、OCR版本、provider/model/config哈希、背景与实际解析规则；以JSON preview确定入选/排除。默认每文件一批；确有相关契约时提供二维JSON批次（每批1–12文件），必须恰好覆盖全体入选文件，无重复/漏审，逐批preview再次验证。大文件仍不拆内部；必要完整职责/跨文件取证见 `ocr-and-merge.md` §3。将生成的只读spec、diff、背景、预检规则与脚本副本保存在本任务reviews新目录，禁止改冻结材料。
3. **start，提交独立服务**：同run重复start只返回原状态。入口拒绝现有OCR进程或全局/项目锁占用；service自身再次加锁，全链持有稳定锁。由 `systemd-run --user` 创建独立 `.service`，不继承cron scope；Restart=no，不自动续审。worker只有在对应独立服务cgroup内才能执行。群聊与cron都只调用同一短入口，不等待完整审查。
4. **status，核真实进展**：查unit、PID/start ticks、session身份/mtime、每批日志和状态。SUBMITTED不等于已经开始请求；日志为空不等于死亡。服务退出而结果未收口显示INTERRUPTED/UNKNOWN，不能判通过或重复启动。启动确认写回任务现有批次台账和自身检查点，之后由原poll观察，不新建cron或后台等待链。
5. **逐批自动校验并停链**：每批启动前重核ACTIVE、worktree、配置、preview和规则；固定review参数，独立日志不覆盖。收口必须有完整manifest、文件/指纹匹配、确切base/head、provider/model匹配且rc=0。任何不完整、超时、身份变化或错误立即停止后批；保留已完成范围，不自动换参或重试。
6. **AWAITING_DECISION，人工语义裁决**：只有全批有效覆盖才进入该状态，它不是PASS。协调者核实findings、排除依据、跨文件/跨职责契约、独立测试与适用强门禁；真实问题回派owner，证据缺口派测试者，误报注明依据。全部原门禁满足后才批准合入，必要合入后验证仍按原流程。

## 命令契约

以下是参数模板，替换大写占位值后执行；`ROOT` 是绑定项目根，`RUN_DIR` 是新的reviews材料目录，`AUTH_SOURCE`引用已有有效授权来源，不是生成新授权。

```text
python3 /home/wang/.codex/skills/complex-task-coordinator/scripts/ocr_runtime.py prepare --project ROOT --repo FROZEN_WORKTREE --base BASE_SHA --head HEAD_SHA --task TASK_ID --attempt ATTEMPT --authorization AUTH_SOURCE --background-file BACKGROUND_MD --output RUN_DIR
python3 /home/wang/.codex/skills/complex-task-coordinator/scripts/ocr_runtime.py start --run RUN_DIR
python3 /home/wang/.codex/skills/complex-task-coordinator/scripts/ocr_runtime.py status --run RUN_DIR
```

确需按模块合批时，prepare加 `--batches-file BATCHES_JSON`，内容如 `[["app/a.py","app/b.py"],["scripts/c.py"]]`，必须来自全体实际入选文件；默认逐文件并不省略跨文件契约。工具自身仍可能归组，不能把批次文件数当token大小或认为max-tokens会拆单文件。

只有需要停止自己的明确run时才使用：

```text
python3 /home/wang/.codex/skills/complex-task-coordinator/scripts/ocr_runtime.py stop --run RUN_DIR
```

定向停止该run的unit及其子进程，不杀其它OCR，不删除锁；停止后保存真实覆盖，不当通过。用户暂停范围按原控制协议决定对已有审查取证或停止，不把pause误当新授权。

## 固定参数及停止条件

| 项目 | 固定值/规则 |
|---|---|
| provider / endpoint | `vllm-local` / `http://192.168.2.45:8002/v1`；入口只读配置，错配直接BLOCKED。禁止恢复18002或每批备份/改写/恢复整份共享配置。 |
| 模型 | prepare从实际provider配置冻结model，worker显式传入并核对manifest。 |
| max-tokens / effort / concurrency | `0`采用已核验配置/模板默认；`medium`；`1`，避免一条批次内部再并行争抢。不开放弱模型临时改参。 |
| timeout / request timeout | 每任务60分钟；`OCR_LLM_TIMEOUT=900`秒；外层单批3900秒硬截止。不是预计耗时，也不是保证能完成。 |
| 服务寿命 | 独立service，全链RuntimeMaxSec=批数×3900+120；Restart=no，KillMode=control-group。跨调用会话/cron回合继续；机器重启或服务被停后保持INTERRUPTED，不自动恢复付费调用。 |
| 全局及项目串行 | `/home/wang/.local/state/ocr-runtime/ocr.lock`与绑定 `inbox/ocr.lock`，全链持有稳定inode，锁忙立即返回OCR_BUSY，不另造等待队列。 |
| 重复run | 同spec幂等；同快照、全体入选、背景、解析规则、配置已有run时，新目录也拒绝重复调用。台账位于project的runtime/ocr，只记录审查运行身份，不替代TODO。 |

这些值针对当前已验证链路。需要改变参数、工具版本或provider时，由获授权维护者先验证并版本化入口；协调者不能临时编写另一runner来绕过失败条件。60分钟达到后保留门禁，不无限加超时。

## 失败与补审

- 先读取 `bNN-diagnosis.json` 与原session；manifest终因优先，正确区分压缩、任务截止、请求超时、预算、宿主回收及身份拒绝。`context canceled`和`round 1/2`只是症状。步骤见 `ocr-and-merge.md` §5。
- 入口不会自动调用resume。确认服务和原进程已退出，并有根因处理与仍有效授权后，创建新目录prepare，显式加 `--reuse-run PREVIOUS_RUN --retry-reason REASON`。只在base/head、全体入选、背景、规则及配置身份完全一致时，复核并复用原链已完成批次的manifest；失败文件重审，不从失败工具调用续跑。
- 同一身份最多初审加一次补审；再失败显示RETRY_LIMIT并升级。不能更换task名、另建目录或换超时绕过。输入/背景/规则/配置真正发生变化时是新审查身份，旧结果不能自动复用；记录实质变化与关联历史，不用改几句背景冒充修复。
- 已进入AWAITING_DECISION时处理findings，不重跑求“干净”。部分发现/规划/压缩摘要可核实，但不补齐未完成覆盖；退出0、零发现或服务Result=success不等于业务通过。

## 禁止的旧路径与迁移

本机协调者不得直接 `ocr review`，不得 `terminal(background=true)` 跑长OCR，不得 `nohup/setsid` 脱管、裸shell等待链、复制旧runner恢复隧道或删除锁。prepare内部的preview/delegate规则导出由脚本统一执行，均零模型。强审仍走strong_review.py及原授权/额度；不以宿主代审冒充弱OCR通过。

已在途的旧runner只观察并保留原身份、日志、覆盖与门禁，不因切换入口杀掉重跑。本次12S-B旧入口已退役以阻止再启动；旧进程的内存finally仍可能写回18002，由独立一次性迁移收尾在它自然退出后仅纠正废弃URL，不恢复整份配置、不启动新OCR。尚未收口不能宣称遗留风险已消失。

## 实际验证

`python3 .../scripts/test_ocr_runtime.py` 使用隔离Git/配置/锁与假OCR，测试真实systemd服务跨启动scope结束存活、重复阻断、退出0但部分失败停链、补审复用及限额、固定身份、配置不改写和定向停止；零真实模型调用。诊断脚本另有真实session验证。实施证据：`/home/wang/work/doc/analysis/ocr-runtime-20261013/REPORT.md`。
