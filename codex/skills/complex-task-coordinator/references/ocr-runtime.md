# 本机 OCR 固定执行流程（运行格式1.7.0，结果核验增强）

适用：本机 Hermes 协调者的常规 OpenCodeReview。入口是 `/home/wang/.codex/skills/complex-task-coordinator/scripts/ocr_runtime.py`，使用已核验 v1.13.0（CLI沿用v1.12.13契约）。已绑定项目的新run逐批读取Hermes模型切换设置；本地仍为`vllm-local`永久LAN端点，云端复用已配置供应商和同一模型。其它项目/平台先验证工具与运行环境，不能硬套本机路径。强模型审查、开发、独立测试和合入职责不变。

群聊发起回合默认只读；已有审查授权不等于本回合全部权限。下述准备/启动/裁决只供cron，或当前用户明确全部权限且已取得同一业务租约的群回合执行；拿不到租约不动作、不写队列。只读人工进度使用`role_live_progress`，不自行调用terminal。

## 固定顺序

1. **冻结交付并写背景**：核对用户当前授权、task/attempt、开发交付与独立测试条件；取得实际 base/head，使用无已跟踪改动且HEAD等于交付的隔离worktree。背景文件引用原REQ/AC与版本、关键不变量、允许行为、明确本期范围、测试证据及待核实风险。不是把规划摘要当代码，也不另造需求。
2. **prepare，零模型预检**：入口解析完整SHA，核对差异、OCR版本、配置哈希、准许的供应商/模型/端点集合与选路器哈希、背景与实际解析规则；以JSON preview确定入选/排除。默认每文件一批；同一受管service最多同时运行两个批次。每次准入前读取本地waiting：`waiting≤1` 时上限2，`waiting>1` 或指标未知时上限1；每个子进程内部并发固定为1。降档只停止后续准入，不中断已运行批次；确有相关契约时提供二维JSON批次（每批1–12文件），必须恰好覆盖全体入选文件，无重复/漏审，逐批preview再次验证。大文件仍不拆内部；必要完整职责/跨文件取证见 `ocr-and-merge.md` §3。将生成的只读spec、diff、背景、预检规则与脚本副本保存在本任务reviews新目录，禁止改冻结材料。
3. **start，提交独立服务**：同run重复start只返回原状态。入口拒绝现有OCR进程或全局/项目锁占用；service自身再次加锁，全链持有稳定锁。由 `systemd-run --user` 创建独立 `.service`，不继承cron scope；Restart=no，不自动续审。worker只有在对应独立服务cgroup内才能执行。获授权的cron或本回合全权且已持租约的群回合使用同一短入口，不等待完整审查。
4. **status，核真实进展**：查unit、PID/start ticks、session身份/mtime、每批日志和状态。SUBMITTED不等于已经开始请求；日志为空不等于死亡。服务退出而结果未收口显示INTERRUPTED/UNKNOWN，不能判通过或重复启动。启动确认写回任务现有批次台账和自身检查点，之后由原五分钟观察器读取JSON语义终态并向协调者原pending排队，不新建cron或后台等待链。首次读取历史结果先核已有裁决，不能重新启动求零问题。
5. **逐批自动校验并停链**：每批启动前重核ACTIVE、worktree、配置、preview和规则；固定review参数，独立日志不覆盖。收口必须有完整manifest、文件/指纹匹配、确切base/head、该批记录的实际provider/model匹配且rc=0。任何不完整、超时、身份变化或错误立即停止新准入；已在途同伴批次保留结果后收口，取消则停止本run全部子进程；保留已完成范围，不自动换参或重试。
6. **results，完整取证后语义裁决**：全批有效覆盖只进入AWAITING_DECISION，不是PASS。先运行 `results --run RUN_DIR`，逐批核对spec/state/session/diagnosis与真实comments，覆盖完成与问题数量分开。缺失、截断、身份冲突或未完成显示INSUFFICIENT_EVIDENCE，未知总数不得写成0；已有可读问题仍保留。该命令当前核验v1.13.0结果，旧工具版本未支持时明确证据不足，已有有效历史裁决不因此自动重开。它只读、不调用模型、不自动裁决。协调者核实findings、排除依据、跨文件/跨职责契约、独立测试与适用强门禁；真实问题回派owner，证据缺口派测试者，误报注明依据。全部原门禁满足后才批准合入，必要合入后验证仍按原流程。

## 命令契约

以下是参数模板，替换大写占位值后执行；`ROOT` 是绑定项目根，`RUN_DIR` 是新的reviews材料目录，`AUTH_SOURCE`引用已有有效授权来源，不是生成新授权。

```text
python3 /home/wang/.codex/skills/complex-task-coordinator/scripts/ocr_runtime.py prepare --project ROOT --repo FROZEN_WORKTREE --base BASE_SHA --head HEAD_SHA --task TASK_ID --attempt ATTEMPT --authorization AUTH_SOURCE --background-file BACKGROUND_MD --output RUN_DIR
python3 /home/wang/.codex/skills/complex-task-coordinator/scripts/ocr_runtime.py start --run RUN_DIR
python3 /home/wang/.codex/skills/complex-task-coordinator/scripts/ocr_runtime.py status --run RUN_DIR
python3 /home/wang/.codex/skills/complex-task-coordinator/scripts/ocr_runtime.py results --run RUN_DIR
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
| provider / endpoint | 新run每批启动前读取同一Hermes模式及云端选择；本地为`vllm-local` / `http://192.168.2.45:8002/v1`，云端为`hermes-sub2api`、`hermes-xcoding`等已配置provider。端点/协议错配或超出冻结准许集合即停，不回退本地。入口只读OCR配置，不逐批改写或恢复共享配置。 |
| 模型 | 冻结准许的供应商/模型/端点集合；每批保存`execution_route`，显式传入模型，并按该批实际身份核对manifest、diagnosis与结果JSON。切换不重做已完成覆盖，在途批次先结束。1.5旧冻结runner保持原模型；读取和既有门禁兼容。 |
| max-tokens / effort / concurrency | `0`采用配置/模板默认；普通模型`medium`，gpt-6.1-sol为`high`；每子进程并发`1`，受管run逐次GET本地`/metrics`：`waiting≤1`时最多2批，其他（含未知）最多1批；降档自然排空在途，不杀有效审查。x-coding通过原生provider extra_body发送`reasoning.effort=high`与`store=false`。不开放角色临时改参。 |
| timeout / request timeout | 每任务60分钟；`OCR_LLM_TIMEOUT=900`秒；外层单批3900秒硬截止。不是预计耗时，也不是保证能完成。 |
| 服务寿命 | 独立service，全链RuntimeMaxSec=批数×3900+120；Restart=no，KillMode=control-group。跨调用会话/cron回合继续；机器重启或服务被停后保持INTERRUPTED，不自动恢复付费调用。 |
| 全局及项目单run | `/home/wang/.local/state/ocr-runtime/ocr.lock`与绑定 `inbox/ocr.lock`，全链持有稳定inode；run内准入上限按每批启动前的本地waiting动态决定（最多2批），共享该service预算；锁忙立即返回OCR_BUSY，不另造等待队列。 |
| 重复run | 同spec幂等；同快照、全体入选、背景、解析规则、配置已有run时，新目录也拒绝重复调用。台账位于project的runtime/ocr，只记录审查运行身份，不替代TODO。 |

这些值针对当前已验证链路。需要改变参数、工具版本或provider时，由获授权维护者先验证并版本化入口；协调者不能临时编写另一runner来绕过失败条件。60分钟达到后保留门禁，不无限加超时。

## 失败与补审

- **BLOCKED/作废 run 的目录名不能复用来 re-prepare**：一个 run 一旦登记过 `inbox/control/runtime/ocr/<review_identity>.json`（即使竞态 BLOCKED 未真正审查），其目录名就绑定了一个索引。若把 BLOCKED run 目录 `rm -rf` 后用同名 re-prepare 成新身份，会新建第二个 `<identity>.json` 指向同一目录 → `role_live_progress`/report 报 `OCR conflicting runtime index`，巡检读数全废。正确：BLOCKED/作废 run 用**新目录名**重跑（如 `-retry`/`-v2`），或先经入口 stop 清理其索引。已误复用目录名时：备份后删除指向已失效旧 unit 的孤儿索引文件（`inbox/control/runtime/ocr/<旧identity>.json`），保留指向当前 spec.json review_identity 的那个，再重跑 live_progress 验证冲突解除。本教训源自 2026-10-17 R2：复用 BLOCKED run 35e09225 的目录名 re-prepare 成 0145306b，孤儿索引致 live_progress 冲突，清理后恢复。
- 先读取 `bNN-diagnosis.json` 与原session；manifest终因优先，正确区分压缩、任务截止、请求超时、预算、宿主回收及身份拒绝。`context canceled`和`round 1/2`只是症状。步骤见 `ocr-and-merge.md` §5。
- 入口不会自动调用resume。确认服务和原进程已退出，并有根因处理与仍有效授权后，创建新目录prepare，显式加 `--reuse-run PREVIOUS_RUN --retry-reason REASON`。只在base/head、全体入选、背景、规则及配置身份完全一致时，复核并复用原链已完成批次的manifest；失败文件重审，不从失败工具调用续跑。
- 固定 HARD_DEADLINE 下，保持相同代码、参数、配置与硬截止，仅补写 retry_reason 不算根因处理；工作流 resume 在派发前拒绝原样重跑。先核已发现问题，成立则原授权返修，新快照仍完整审查；入口参数变化另做真实验证与版本化，不编辑冻结 spec。复用批次标明 reused，不能声称独立两审一致；后续预检拒绝不等于早前会话的失败原因。
- 同一身份最多初审加一次补审；再失败显示RETRY_LIMIT并升级。不能更换task名、另建目录或换超时绕过。输入/背景/规则/配置真正发生变化时是新审查身份，旧结果不能自动复用；记录实质变化与关联历史，不用改几句背景冒充修复。
- 已进入AWAITING_DECISION时处理findings，不重跑求“干净”。部分发现/规划/压缩摘要可核实，但不补齐未完成覆盖；退出0、零发现或服务Result=success不等于业务通过。

## 禁止的旧路径与迁移

本机协调者不得直接 `ocr review`，不得 `terminal(background=true)` 跑长OCR，不得 `nohup/setsid` 脱管、裸shell等待链、复制旧runner恢复隧道或删除锁。prepare内部的preview/delegate规则导出由脚本统一执行，均零模型。强审仍走strong_review.py及原授权/额度；不以宿主代审冒充弱OCR通过。

已在途的旧runner只观察并保留原身份、日志、覆盖与门禁，不因切换入口杀掉重跑。本次12S-B旧入口已退役以阻止再启动；旧进程的内存finally仍可能写回18002，由独立一次性迁移收尾在它自然退出后仅纠正废弃URL，不恢复整份配置、不启动新OCR。尚未收口不能宣称遗留风险已消失。

## 实际验证

`python3 .../scripts/test_ocr_runtime.py` 使用隔离Git/配置/锁与假OCR，测试真实systemd服务跨启动scope结束存活、重复阻断、退出0但部分失败停链、补审复用及限额、固定身份、配置不改写和定向停止；零真实模型调用。诊断脚本另有真实session验证。实施证据：`/home/wang/work/doc/analysis/ocr-runtime-20261013/REPORT.md`。

## 飞书切换与凭据

`/llm cloud sub2api`、`/llm cloud xcoding`、`/llm auto`、`/llm local`同时控制角色主推理和新OCR批次。OCR每批启动前执行维护仓库的`ocr_model_route.py select`，复用夜间＋流水线＋vLLM准入；云端不可用或凭据缺失不抢占本地。程序返回选路元数据，不返回密钥。原生OCR `api_key_cmd`私下读取本机凭据，不把密钥冻结进spec或日志；运行时配置仍只读。

修改供应商配置/准许模型集合后，旧冻结run可能因配置或选路器身份变化停链，应核实已完成覆盖和原授权后重新prepare，不能改旧spec。切换已冻结集合中的供应商仅影响后续批次，不重启在途批次。真实隔离代码审查已完成一次x-coding/gpt-6.1-sol调用，见`/home/wang/work/doc/analysis/hermes-model-routing-20261015/REPORT.md`；这不是对生产任务的业务批准。


安装入口排查：Skill scripts 的 ocr_runtime.py 为维护仓软链接时，冻结依赖还包括同目录 ocr_compat.py 与 ocr_compat_baseline.json；两者应指向 canonical 维护仓并逐字核对 SHA。prepare 在创建目录后报 FileNotFoundError 且无 spec/state 时，先核这些静态依赖，不当已运行审查或耗用补审；保留原目录，新冻结使用新目录名。不能临时降级兼容检查或编造 baseline。

2026-10-29：格式1.7.0对新prepare生效，队列快照及实际准入保存在state各批admission。并行session按最终manifest文件集合、base/head、provider/model逐批关联，不按mtime抢最新。1.5/1.6旧冻结run仍可读取并沿原串行策略执行；不修改在途spec或重启审查。
