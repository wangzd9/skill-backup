"""Resolve the existing single-tool bridge without widening role authority."""
import json


def resolve(tool_name, args):
    if tool_name!='tool_call':
        return tool_name,args,None
    from tools.tool_search import resolve_underlying_call, CONNECTOR_BATCH_SENTINEL
    name,arguments,error=resolve_underlying_call(args or {})
    if error or not name or name==CONNECTOR_BATCH_SENTINEL:
        return tool_name,args,{'action':'block','reason':'ROLE_TOOL_BRIDGE_INVALID',
            'message':'ROLE_TOOL_BRIDGE_INVALID: '+str(error or 'CONNECTOR_NOT_ROLE_TOOL')[:150]+
            '。直接调用本角色 command 工具；旧会话用单个 calls 元素，保留 command_id。'}
    return name,arguments,None


def usage(role, *, include_background=False):
    from workflow_runtime import COMMAND_TOOLS
    name=COMMAND_TOOLS[role]
    example={'calls':[{'name':name,'arguments':{'action':'checkpoint','payload':{
        'checkpoint':{'next':'真实下一步与未决门禁'},'yield_execution':True},'command_id':'本动作唯一ID'}}]}
    text=('直接调用 '+name+'，不需要 tool_call 包装。旧会话兼容单个 calls 元素：'
          +json.dumps(example,ensure_ascii=False)+'。只填已核实的事实。'
          '收到环境不可用时修复验证环境，不把它当代码缺陷返修。'
          'next_actions为safe_release且有原生退休请求时，TESTER自行收口并退出，不等待用户批准或先刷新基线。')
    if role in {'A','B','C'}:
        text+=('开发用 write_file/patch 在本单私有仓库的冻结文件范围内编辑；内容通过 stdin 进入受限沙箱。'
               'terminal 用于自测及提交；不要反复重试被 hardline 拦截的大段内联命令。')
    elif role=='COORDINATOR':
        text+=('completion是原生需求级验收计划，叶任务完成不等于需求完成。前置齐备由系统自动派TESTER最终集成验证，'
               '无需用户裁决或手动派发。FAIL按问题ID回原owner新attempt，保留原测试/OCR/审查/合入门禁；'
               '最终PASS后用complete_requirement复核逐AC、本次证据及historical_failures处置并本地收口；coverage_review逐项给closure obligation_id、validation_event_id、result、reason，明确场景/断言/环境语义是否对应；issue_dispositions给issue_id/reason/evidence。'
               '环境预算耗尽后，取得真实恢复证据再用environment_recovered(validation_event_id,summary,evidence)登记新预算，不改原FAIL或UNVERIFIED。'
               '不得用开发任务完成代替验收，不把本地收口称生产启用。规划者仍由用户手动触发。')
        text+=('协调者的普通 terminal/write_file/patch 按角色禁用；这不是用户授权缺失，不要请求开放 shell。'
               '本地自动开发按local_development_automation持久执行，派单/验证/返修/安全收口/基线刷新/本地合入不逐项征求同意。'
               'verification_retirements是内部收口请求，TESTER自己safe_release，observer核真实writer退出后清理；不得发明eNNNN a/b用户审批。'
               '未交付开发基线滞后用refresh_development_base(task_id,reason)，不指示A交付空占位候选。'
               '任务级合入授权在原run/task/branch/期限和冻结合同内跨attempt持续；新候选重新验收后merge自动派生精确凭据。'
               '不能因refresh_task_base就让用户重授；先查merge_task_policy及next_actions.permission，显式限定候选/attempt的授权仍按限定。'
               '普通文档审核与提交属于协调职责：role_repo_state.documentation_review提供逐文件正文、diff和sha256的只读artifact。'
               '读后核对事实、任务关联、敏感信息及是否改变AC；合理文档用commit_documentation提交，不凭文件名猜归属或一律等别的会话。'
               'payload={"expected_head":"当次HEAD","expected_branch":"当次分支","summary":"文档提交说明",'
               '"files":[{"path":"docs/文件.md","sha256":"审核时SHA256","review":"核对了哪些事实及为何可提交"}]}。'
               '仅提交这些精确字节，保留其他暂存/未暂存代码；变化后需重读。指令/PRD/AC/契约不走普通文档提交。'
               '合入用 merge：保留共享主仓全部未提交改动；入口支持同一授权仓库/分支的受管独立工作树，不要仅因主仓dirty永久等待。'
               '已批准且授权有效时调用merge判断；同基线main可无损释放主仓feature绑定，然后在integration-worktrees合入。'
               '实际执行位置以merge回执frozen.integration_worktree为准；operations是日志目录。'
               'runtime_capabilities.coordination_actions不能替代工具schema；refresh_task_base/complete/commit_documentation仍可按next_actions及工具合同执行。'
               'Git porcelain的两份blob是HEAD/暂存区，不能用它们相同断言仅mtime变化；看真实diff证据。'
               '文件归属需派单/交付证据，不从文件名推断其他会话；未确认可弃不得建议删除。'
               '开发日志用 append_development_log，payload={"content":"真实里程碑正文"}，固定追加本需求 DEVELOPMENT-LOG.md。'
               '补验UNVERIFIED看verification_exceptions，用resolve_verification_exception显式repair/investigate/close；'
               'repair需observation_event_id、当前candidate_id、原scope内affected_paths及核证reason，原writer退出后原生落实新attempt。'
               '已完成任务不走extend_task_scope，不等MN-07先失败；内部机制缺口不等于缺用户授权。'
               'ACTIVE 启动原因看 admission_hint，不从旧 pool.current 推断；writer fence 表示同角色写执行占用。'
               'waiting_external 会定期重新核验；工具拒绝码和条件证据须逐项读取，不把历史 checkpoint 当实时事实。')
    elif role=='TESTER':
        text+=('integration_verification是最终集成验证，固定目标/原合同见integration_validation。'
               '用verify_integration分批执行，payload含argv、profile=local/mysql、report_format及timeout_seconds；'
               'mysql由受控临时8.4实例提供MN01_MYSQL_URL，不接生产连接。结果保留INTEGRATION_TEST_RECORDED事件。'
               '按integration_validation.plan.obligations区分local_validation/local_closure/real_nodes/production，不能用协调收口作测试前置。'
               '逐AC完成本地义务后deliver_integration，checks逐项填id/result/mode/test_event_ids/notes；coverage逐项给obligation_id、phase、contract_sha256、scenario、assertion、environment、test_event_id、case_id（JUnit的classname::name）。AC10报告索引用event_ids。'
               'issues填id/kind/task_id/test_event_ids/summary及blocking/confidence/accepted_scope/ac_ids/disposition/owner。仅已确认且阻断本地合同的code issue自动返修。非阻断或范围外须rationale和review_required=true，协调者逐项复核才能收口。'
               '替身与实物分开写明，未执行/环境失败为UNVERIFIED，不可用skip或一句总结冒充PASS。')
        text+=('收到supplemental_verification工作时，用verify_supplemental，payload={}；固定候选MySQL8.4隔离补验由受控入口提供数据库，'
               '不能改用普通verify跳过profile，不能把skip写成MySQL通过；无需普通shell或用户提供生产数据库。'
               'UNVERIFIED保留原证据并移交协调者，收到退休请求用safe_release退出，不在旧work等待新候选或重复重跑。')
    if include_background:
        text+=(' 用户裁决以USER_DECISION_RECORDED及next_actions为准；用resolve_user_decision记录原提问关联、语义核对和代码依据，'
               '然后更新checkpoint，不能把已登记短答复留作待用户重发；登记不替代原验收门禁或授权。')
        text+=(' OCR 背景材料无需 write_file：service_submit 的 request 可用 '
               '{"background_text":"完整审查背景正文"}，服务在受管 action 目录冻结正文；'
               '也兼容 requirement_root 内既有 background_file。已授权 run/task 策略可自动选择；没有策略时才需要单独授权。')
    return text
