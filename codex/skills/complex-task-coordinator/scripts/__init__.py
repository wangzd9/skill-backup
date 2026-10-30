"""Hermes 现有 hook 扩展：人工对话与业务执行分离、最新简报、提示指纹只读观察。"""
import asyncio
import logging
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
import time

sys.path.insert(0,str(Path(__file__).resolve().parent))
from load_metrics import load_query, collect, render
from prefix_audit import PrefixAudit
from .progress_query import progress_query, progress_answer, live_progress_tool_packet
from runtime_common import read_json
from time_accounting import record_api_start, record_api_end, record_api_error
from model_routing import ModelRouter, parse_command
from . import usage_accounting
from .manual_access import ManualAccess, FULL, explicit_full_permission, full_permission_action
from . import manual_business
from .binding_contract import registry_bindings
from . import workflow_runtime as workflow

logger=logging.getLogger(__name__)


def _manual_progress_instruction(role):
    if role == 'COORDINATOR':
        return ('用户在协调者会话请求进度/汇报进展时，最终答复必须按当前巡检格式完整输出七个加粗标题，'
            '顺序固定为 **整体目标与进度**、**执行者进度**、**测试者进度**、**OCR情况和后续计划**、'
            '**强审查情况**、**风险**、**下一步计划**；不得只挑少数主题，不得改成关键路径/环境/待拍板等自选分区替代，也不得省略区块。'
            '整体区说明目标、已完成/已验收与剩余；执行者A/B/C各自一组，名字后列状态并说明工作、结果、阻塞和下一步；'
            '测试者单独说明范围、固定版本、已完成验证和缺项；OCR与强审查分别说明当前状态、覆盖/结论及接续条件。'
            '每区用单层短bullet；每条正文都要把字段名、角色状态、结论、阻塞/风险、下一动作和关键字段使用Markdown粗体突出。'
            '即使没有新增事实或只读工具不可用，仍保留七区；只将受影响的事实标为“待核实”，写明可用证据来源，'
            '旧checkpoint须标明时间且不能冒充当前状态。工具异常仅用“当次进度核验未完成”概括，不复述错误码、预算、字节数、实现限制或原始日志；'
            '只有用户直接询问工具问题，或该错误确实阻断已授权动作时，才用一句简短说明。')
    if role in ('A','B','C','TESTER'):
        return ('执行者/测试者被问本人进度时，按 **当前工作**、**本次完成**、**阻塞与依赖**、**下一步计划** 四区完整答复；'
            '各区标题和结论、状态、阻塞、关键字段加粗，使用单层短bullet。无新增或工具失效时保留全部四区并标具体缺口/待核实；'
            '仅报告本人及直接依赖，不扩写全局巡检。')
    return ''


def _manual_progress_request(role, text):
    """Recognize direct requests for a current role progress report, not discussion about reporting."""
    if role not in ('COORDINATOR', 'A', 'B', 'C', 'TESTER') or not isinstance(text, str):
        return False
    value=re.sub(r'\s+', '', text).strip()
    if not value or re.search(r'(?:为什么|为何|怎么会|如何改|如何修|规则|格式|模板|条理|加粗|原理)', value):
        return False
    prefix=r'^(?:(?:协调者|执行者[ABC]|测试者)[，,:：]?)?(?:请|麻烦|帮我|给我|现在|立即|我想|我需要)?'
    direct=re.compile(prefix+r'(?:汇报|报告|查看|查一下|核对|说一下|说下|讲一下|讲下|告诉我|总结一下|看下).{0,20}(?:当前|现在|目前|本轮|整体|项目)?(?:进度|进展|状态)')
    noun=re.compile(r'^(?:(?:协调者|执行者[ABC]|测试者)[，,:：]?)?(?:(?:当前|现在|目前|本轮|整体|项目)(?:的)?)?(?:(?:是什么|什么|怎么样|如何|到哪了|到哪里了)?)(?:进度|进展|状态)(?:情况|呢|如何|怎么样|到哪了|到哪里了)?[？?。！!]*$')
    return bool(direct.search(value) or noun.fullmatch(value))


def activate_current_runtime():
    """Read this release's activation manifest even if its module is cached.

    Handler discovery is only the entry point. Named function replacement and
    cached module aliases are updated by the helper; running frames keep their
    existing code. Function-local imports are not the hot-activation contract.
    """
    import hashlib
    import types
    from . import mechanism_activation as legacy
    source = Path(legacy.__file__).resolve()
    raw = source.read_bytes()
    name = '_role_runtime_activation_' + hashlib.sha256(raw).hexdigest()
    current = sys.modules.get(name)
    if current is None:
        current = types.ModuleType(name)
        current.__file__ = str(source)
        exec(compile(raw, str(source), 'exec'), current.__dict__)
        current.LOCK = legacy.LOCK
        sys.modules[name] = current
    result = current.activate()
    result['activation_helper_sha256'] = hashlib.sha256(raw).hexdigest()
    return result


def activate_current_native_policy():
    """Refresh only the stateless migration helper, preserving its shared lock.

    A live gateway has already imported its old activation manifest. Importing
    it again after plugin reload would silently keep yesterday's target list.
    Business modules are never reloaded; the versioned helper updates only the
    explicitly named future-call seams in their existing namespaces.
    """
    import hashlib
    import importlib.util
    from agent import native_policy_activation as legacy
    source = Path(legacy.__file__).resolve()
    raw = source.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    name = 'agent._role_native_activation_'+digest
    current = sys.modules.get(name)
    if current is None:
        spec = importlib.util.spec_from_file_location(name, source)
        current = importlib.util.module_from_spec(spec)
        # Compile the exact hashed bytes, avoiding timestamp-based pyc reuse.
        exec(compile(raw, str(source), 'exec'), current.__dict__)
        current._LOCK = legacy._LOCK
        sys.modules[name] = current
    result = current.activate_native_turn_policy()
    result['activation_helper_sha256'] = digest
    return result


class RuntimePlugin:
    def __init__(self,*,registry,metrics_url,home,coordinator_chat_ids=(),native_ready=True,pool_path=None,notification_budget=None):
        self.registry=Path(registry); self.metrics_url=metrics_url; self.home=Path(home)
        self.coordinator_chat_ids=set(coordinator_chat_ids)
        self.audit=PrefixAudit(self.home/'runtime/prefix-audit')
        self.router=ModelRouter(self.home,metrics_url)
        from collections import deque
        self._manual_observations=deque(maxlen=128)
        self.manual=ManualAccess(observer=self.observe_manual_access)
        self.manual_requests={}
        self.manual_turns={}
        self.manual_progress_turns={}
        self.notification_budget=notification_budget or {'max_messages':10,'max_seconds':2,'max_scan':256}
        if set(self.notification_budget)-{'max_messages','max_seconds','max_scan'}:
            raise ValueError('INVALID_NOTIFICATION_BUDGET')
        self.native_ready=native_ready
        self.pool_path=Path(pool_path) if pool_path else self.home/'role-pool.json'
        self.manual_sources={}
        self._binding_rows=[]
        self.binding_issues=[]
        self._workflow_tick_lock=threading.Lock()
        self._workflow_delivery_lock=threading.Lock()
        try:
            from workflow_dispatch import observer_binding
            self._event_observer_manifest=observer_binding()[1]
        except Exception:
            self._event_observer_manifest={'negotiation':'UNSUPPORTED','measurement':'UNKNOWN'}

    def observe_manual_access(self, value):
        """Bounded native metadata receipts; no primary text, grant or business state."""
        from runtime_common import save_json
        import os,time
        self._manual_observations.append({**value,'observed_at':time.time()})
        save_json(self.home/'runtime/role-observation/native-manual-access.json',
                  {'schema_version':1,'pid':os.getpid(),'events':list(self._manual_observations),
                   'authority':False,'max_events':128})

    def manual_identity(self, session_id, trusted_turn_input=None):
        identity=self.api_attribution_details(session_id)
        info=trusted_turn_input or {}
        if info.get('trusted_gateway') is True and info.get('platform')=='feishu':
            matches=[r for r in self.bindings() if r.get('chat_id')==info.get('chat_id')]
            if len(matches)==1:
                item=matches[0]
                if item.get('binding_error'):
                    raise ValueError('ROLE_BINDING_QUARANTINED: '+item['binding_error'])
                current={'role':item['role'],'project':str(item['root']),'session_kind':'group_chat'}
                if identity and identity['session_kind']=='group_chat' and any(identity[k]!=current[k] for k in ('role','project')):
                    raise ValueError('native gateway and stored role binding disagree')
                return current
            # A trusted live group origin cannot inherit a historical cron ID's write rights.
            if identity:
                raise ValueError('current gateway role binding not verified')
        return identity

    def bind_manual_turn(self, session_id, turn_id, trusted_turn_input=None):
        identity=self.manual_identity(session_id,trusted_turn_input)
        previous=self.manual_turns.get((session_id,turn_id))
        if previous and not identity:
            raise ValueError('current native turn role binding disappeared')
        if previous and identity and any(previous[k]!=identity[k] for k in ('role','project','session_kind')):
            raise ValueError('current native turn role binding changed')
        if identity and session_id and turn_id:
            self.manual_turns[(session_id,turn_id)]=identity
        info=trusted_turn_input or {}
        if identity and identity['session_kind']=='group_chat' and info.get('trusted_gateway') is True:
            request=self.manual_requests.get((info.get('chat_id'),info.get('user_id'),info.get('message_id')))
            if request:
                if any(request[k]!=identity[k] for k in ('role','project')):
                    raise ValueError('ingress and current project binding disagree')
                self.manual.bind_turn(session_id=session_id,turn_id=turn_id,request_key=request['key'])
                self.manual_sources[(session_id,turn_id)]=request.get('provenance',{})
                if request.get('progress_intent'):
                    self.manual_progress_turns[(session_id,turn_id)]=identity['role']
        return identity

    def transform_manual_progress_output(self,response_text='',session_id='',turn_id='',**kwargs):
        """Enforce role report shape at the final manual-reply boundary."""
        role=self.manual_progress_turns.get((session_id,turn_id))
        identity=self.manual_turns.get((session_id,turn_id))
        if (not role or not identity or identity.get('session_kind')!='group_chat'
                or role!=identity.get('role')):
            return None
        from workflow_reports import _progress_report_format_error
        reason=_progress_report_format_error(response_text,role)
        if reason is None:
            return None
        logger.warning('人工进度答复未通过分区格式核验；使用当次只读事实生成结构化回退 role=%s reason=%s',role,reason)
        try:
            if workflow.active(self.pool_path):
                from workflow_query import live_progress
                from workflow_reports import render_live_progress_fallback
                return render_live_progress_fallback(live_progress(self.pool_path,role),role)
            from progress_query import live_progress_report
            return live_progress_report(identity['project'],role).get('markdown')
        except Exception:
            logger.warning('人工进度结构化回退的只读查询未完成；只返回待核实分区',exc_info=True)
            return self._manual_progress_unavailable(role)

    @staticmethod
    def _manual_progress_unavailable(role):
        from workflow_reports import COORDINATOR_SECTIONS,PERSONAL_SECTIONS
        from datetime import datetime
        from zoneinfo import ZoneInfo
        title={'COORDINATOR':'协调者','A':'执行者 A','B':'执行者 B','C':'执行者 C','TESTER':'测试者'}[role]
        now=datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M +08:00')
        sections=COORDINATOR_SECTIONS if role=='COORDINATOR' else PERSONAL_SECTIONS
        lines=[f'**{title}｜进度核验 · {now}**']
        gaps={
            '整体目标与进度':('需求与完成度','未取得本次权威进度快照，当前需求及完成情况待核实。'),
            '执行者进度':('执行者 A/B/C（待核实）','当前任务、执行状态和接续条件尚未核实。'),
            '测试者进度':('测试者（待核实）','当前验证任务及最新结果尚未核实，不能据此认定通过或失败。'),
            'OCR情况和后续计划':('OCR（待核实）','在途运行、排队及准入状态尚未核实。'),
            '强审查情况':('强审查（待核实）','授权、在途调用及审查结果尚未核实。'),
            '风险':('核验缺口','当前只读入口未完成核验，历史检查点不能代替当前状态。'),
            '下一步计划':('恢复核验','恢复只读入口后重新核对当前账本及有效回执，本次查询不改变业务调度。'),
            '当前工作':('本人状态（待核实）','当前工作单和执行阶段尚未核实。'),
            '本次完成':('新增成果','未取得本次有效回执，暂不能确认新增交付或验证结果。'),
            '阻塞与依赖':('接续条件（待核实）','当前任务依赖及门禁尚未核实。'),
        }
        for section in sections:
            label,detail=gaps[section]
            lines+=['','**'+section+'**','','- **'+label+'：** '+detail]
        return '\n'.join(lines)

    def on_llm(self, session_id='', turn_id='', trusted_turn_input=None, **kwargs):
        try:
            identity=self.bind_manual_turn(session_id,turn_id,trusted_turn_input)
            if not identity: return None
            scope=self._frozen_scope(session_id)
            if scope and scope.get('workflow_version') and scope.get('kind')=='business':
                from workflow_activity import start_coverage,start_model_coverage
                for collector in (start_coverage,start_model_coverage):
                    try:
                        collector(self.home,scope,at=time.time())
                    except Exception:
                        logger.warning('角色生命周期覆盖未能开始',exc_info=True)
            language='面向用户的答复、cron final、完成和定时汇报默认使用简体中文；技术标识、命令、路径和必要错误原文可保留。直接给结论、证据、阻塞和下一步，不输出英文工作草稿或逐步自问自答。'
            progress_format=_manual_progress_instruction(identity['role'])
            if progress_format:
                language+=' '+progress_format
            if identity['session_kind']=='group_chat':
                full=self.manual.access(session_id=session_id,turn_id=turn_id)==FULL
                if workflow.active(self.pool_path):
                    language+=(' 当前全权仅用于role_control_request提交本条消息的具体动作；同一后台owner执行，不领取人工业务租约。' if full else
                               ' 当前群回合可查询；明确的继续/暂停/恢复指令用 role_control_request 登记到后台，'
                               '不需要全部权限口令。明确的新需求开发指令也用 accept_requirement 接入；'
                               '无文档时 use_native_requirement=true 冻结用户原文，并分配任务身份，后台协调者做计划。'
                               '用户说自动推进时使用默认有界审查预算；合入分支须用户明确，生产权限单独确认。')
                    language+=(' 进展先调用 role_live_progress，仓库核验用 role_repo_state，均无需全权。'
                               'MySQL补验归属用request_supplemental_verification入账，target={task_id:"MN-01",profile:"mysql_migration"}；'
                               '明确原文即可登记到既有TESTER，不要求全部权限口令。用户裁决用record_user_decision登记原文；'
                               '按解读b等短答复可target={}入账，由后台核对原提问关联，不要求重发明确对象或全权口令。'
                               '只有COMMITTED/DUPLICATE回执才可称后台已收到。'
                               '以当次 checked_at 和 event_high_water 为准；checkpoint 是历史叙述，'
                               'invocation.json 是提交记录，不得据此宣称当前门禁阻塞。详情按带哈希的 artifact 分段读取。')
                    return language
                language+=(' 当前用户为本回合明确授予全部权限；业务动作先用role_manual_lease领取租约，owner忙则停止业务动作，不抢占。协调者可用role_requirement_select登记需求切换控制请求，该请求等待后台安全点，不代写业务队列。' if full else
                           ' 当前角色群回合只读；仅查询和解答，不领取任务、不写业务材料、不ACK、不派单、不开发、不启动OCR/测试/审查。用role_live_progress获取当次运行核验。')
            elif identity['session_kind']=='business_cron':
                language+=' 后台业务回合沿用已接受需求范围内的执行授权，无需用户每轮再次授予全部权限；按当前需求调度、开发验证和接续，保留原审查及生产门禁。'
            return language
        except Exception:
            logger.exception('角色回合权限绑定失败；保持只读')
            return '角色入口身份核验失败，本回合只读并用简体中文解释；不得执行业务动作。'

    def on_tool(self, tool_name='', args=None, session_id='', turn_id='', trusted_turn_input=None, **kwargs):
        try:
            identity=self.bind_manual_turn(session_id,turn_id,trusted_turn_input)
            if not identity: return None
            scope=self._frozen_scope(session_id)
            if scope and scope.get('workflow_version'):
                from workflow_tool_bridge import resolve
                tool_name,args,bridge_error=resolve(tool_name,args)
                if bridge_error:
                    return bridge_error
                if scope.get('kind')=='business':
                    from workflow_sessions import tool_decision
                    session_decision=tool_decision(self.home,scope,tool_name,args)
                    if session_decision:
                        return session_decision
                    try:
                        from workflow_activity import record_tool_start, observation_grace_for_tool
                        record_tool_start(self.home,scope,kwargs.get('tool_call_id'),tool_name,at=time.time(),
                                          observation_grace_seconds=observation_grace_for_tool(tool_name,args))
                    except Exception:
                        logger.warning('角色工具开始观测缺失',exc_info=True)
                return workflow.tool_decision(scope,tool_name,args,tool_call_id=kwargs.get('tool_call_id'))
            if workflow.active(self.pool_path):
                from workflow_tool_bridge import resolve
                tool_name,args,bridge_error=resolve(tool_name,args)
                if bridge_error:
                    return bridge_error
                if identity['session_kind']=='summary':
                    return None if tool_name in workflow.READ_TOOLS else {
                        'action':'block','message':'报告会话只读，不开放shell、测试、派单或审查。'}
                if identity['session_kind']!='group_chat':
                    return {'action':'block','message':'旧执行不持有当前业务代次，等待安全交接。'}
                if tool_name in workflow.READ_TOOLS: return None
                if tool_name=='role_control_request':
                    from workflow_intake import scoped_control
                    source=self.manual_sources.get((session_id,turn_id),{})
                    if self.manual.access(session_id=session_id,turn_id=turn_id)==FULL or scoped_control(source,(args or {}).get('action')):
                        return None
                return {'action':'block','message':'群会话仅查询或登记当前全权具体动作，由同一后台owner执行。'}
            if identity['session_kind']!='group_chat':
                self._validate_frozen_scope(session_id,identity)
                return None
            if tool_name=='role_requirement_catalog': return None
            if tool_name=='role_requirement_select':
                if identity['role']=='COORDINATOR' and self.manual.access(session_id=session_id,turn_id=turn_id)==FULL:
                    return None
                return {'action':'block','message':'需求切换仅允许当前明确授予全部权限的协调者群回合；本次没有切换。'}
            # 先续期再判门禁：_owned() 只校验 owner/session/turn/pid，不看 expires_at，
            # 所以本回合已过期的租约仍可续回来。若放在门禁之后，第一次写操作会先被
            # "无租约"拒绝、永远没有机会续期——那正是协调者卡死的真实路径。
            self._renew_owned_lease(identity,session_id,turn_id)
            if tool_name=='role_manual_lease' and self.manual.access(session_id=session_id,turn_id=turn_id)==FULL:
                return None
            return self.manual.tool_decision(tool_name=tool_name,session_kind='group_chat',
                session_id=session_id,turn_id=turn_id,
                lease_check=lambda: manual_business.owns(identity['project'],identity['role'],session_id,turn_id))
        except Exception as exc:
            logger.exception('角色工具权限核验失败')
            return {'action':'block','message':'角色会话权限核验失败，工具已阻断；不会自动转为全部权限。',
                    'decision':getattr(exc,'decision',None)}

    def on_tool_end(self,tool_name='',session_id='',tool_call_id=None,status=None,**kwargs):
        """Record only native terminal metadata, never arguments or tool output."""
        try:
            scope=self._frozen_scope(session_id)
            if not scope or not scope.get('workflow_version') or scope.get('kind')!='business':
                return None
            # A terminal may arrive after the tool yielded/revoked its writer.
            # The stored native scope provides attribution, never fresh write rights.
            from workflow_activity import record_tool_end
            outcome={'success':'SUCCESS','ok':'SUCCESS','error':'ERROR','failed':'ERROR',
                     'cancelled':'CANCELLED','canceled':'CANCELLED','blocked':'BLOCKED'}.get(str(status).lower(),'UNKNOWN')
            # Hermes also emits post_tool_call when it abandons a worker or
            # synthesizes a timeout/missing result. That terminal report does
            # not establish that the underlying tool actually ended.
            uncertain_errors={'tool_timeout','thread_missing_result','tool_interrupted',
                              'keyboard_interrupt','abandoned','tool_abandoned'}
            end_verified=(str(status).lower() in {'success','ok','error','failed','blocked'}
                          and kwargs.get('error_type') not in uncertain_errors)
            return record_tool_end(self.home,scope,tool_call_id,at=time.time(),outcome=outcome,
                                   end_verified=end_verified)
        except Exception:
            logger.warning('角色工具终态观测缺失',exc_info=True)
            return None

    def _renew_owned_lease(self, identity, session_id, turn_id):
        """每次工具调用前，只要本回合确实持有该租约就续期。

        把 600 秒 TTL 变成"空闲超时"而不是"整轮预算"：协调者一轮内要连续读材料、
        核基线、派单、推送，单次模型往返实测中位数 56 秒、最长 17 分钟，固定 TTL
        必然在读完材料前到期。续期只对 owner/session/turn/pid 全一致的本回合生效，
        不构成越权。
        """
        try:
            manual_business.renew_after_write(identity['project'], identity['role'],
                                              session_id, turn_id)
        except Exception:
            logger.debug('租约续期失败；本次调用按原门禁判定，不回收', exc_info=True)

    def live_tool(self, args, *, session_id='', turn_id='', **kwargs):
        import json
        identity=self.manual_turns.get((session_id,turn_id)) or self.api_attribution_details(session_id)
        if not identity: raise ValueError('当前原生会话未绑定角色项目')
        if args: raise ValueError('role_live_progress不接受自选角色或项目参数')
        if workflow.active(self.pool_path):
            from workflow_query import live_progress
            packet=live_progress(self.pool_path,identity['role'])
            return json.dumps(packet,ensure_ascii=False)
        packet=live_progress_tool_packet(identity['project'],identity['role'])
        if self.pool_path.is_file():
            from requirement_runtime import status
            packet['requirement_pool']=status(self.pool_path)
        return json.dumps(packet,ensure_ascii=False)

    def repo_tool(self, args, *, session_id='', turn_id='', **kwargs):
        import json
        identity=self.manual_turns.get((session_id,turn_id)) or self.api_attribution_details(session_id)
        if not identity:
            raise ValueError('当前原生会话未绑定角色项目')
        if args:
            raise ValueError('role_repo_state不接受自选路径或命令参数')
        from workflow_query import repo_state
        return json.dumps(repo_state(self.pool_path,identity['role']),ensure_ascii=False)

    def catalog_tool(self,args,*,session_id='',turn_id='',**kwargs):
        import json
        identity=self.manual_turns.get((session_id,turn_id)) or self.api_attribution_details(session_id)
        if not identity or args: raise ValueError('目录查询仅限已核验角色，不接受自选路径')
        from requirement_runtime import list_requirements,status
        return json.dumps({'catalog':list_requirements(self.pool_path),'pool':status(self.pool_path)},ensure_ascii=False)

    def selection_tool(self,args,*,session_id='',turn_id='',**kwargs):
        import json
        identity=self.manual_turns.get((session_id,turn_id))
        if (not identity or identity['session_kind']!='group_chat' or identity['role']!='COORDINATOR'
                or self.manual.access(session_id=session_id,turn_id=turn_id)!=FULL):
            raise ValueError('需要当前协调者群回合明确授予全部权限')
        if set(args)!={'target'} or not isinstance(args['target'],str):
            raise ValueError('仅接受精确需求target')
        from requirement_runtime import request
        source=self.manual_sources.get((session_id,turn_id),{})
        if workflow.active(self.pool_path):
            return json.dumps(workflow.submit_control(self.pool_path,'COORDINATOR','select_run',
                workflow.selected_target(self.pool_path,args['target']),source),ensure_ascii=False)
        return json.dumps(request(self.pool_path,args['target'],source,trusted_ingress=True),ensure_ascii=False)

    def _frozen_scope(self,session_id):
        if not isinstance(session_id,str) or not session_id.strip():
            return None
        # Optional for older Hermes installations; pool-enabled jobs always
        # require the scheduler seam installed with this runtime version.
        try:
            from cron.role_pool_scheduler import scope_for_session
        except ImportError:
            if self.pool_path.is_file(): raise ValueError('角色池调度接续模块未安装')
            return None
        return scope_for_session(session_id,home=self.home)

    def _validate_frozen_scope(self,session_id,identity):
        scope=self._frozen_scope(session_id)
        if scope and identity['session_kind']=='business_cron':
            if scope.get('workflow_version'):
                workflow.guard(scope)
                return
            if workflow.active(self.pool_path):
                raise ValueError('LEGACY_EXECUTION_FENCED')
            from requirement_runtime import validate_role_scope
            validate_role_scope(scope['pool_path'],scope['role'],scope['project_root'],scope['generation'])

    def lease_tool(self, args, *, session_id='', turn_id='', **kwargs):
        import json
        if workflow.active(self.pool_path):
            raise ValueError('MANUAL_BUSINESS_OWNER_RETIRED_USE_CONTROL_REQUEST')
        identity=self.manual_turns.get((session_id,turn_id))
        if not identity or identity['session_kind']!='group_chat' or self.manual.access(session_id=session_id,turn_id=turn_id)!=FULL:
            raise ValueError('仅当前明确授予全部权限的角色群回合可操作租约')
        if set(args)!={'action'}: raise ValueError('仅接受action参数')
        return json.dumps(manual_business.control(identity['project'],identity['role'],session_id,turn_id,args['action']),ensure_ascii=False)

    def work_input_tool(self,args,*,session_id='',turn_id='',**kwargs):
        import json
        scope=self._frozen_scope(session_id)
        if not scope or not scope.get('workflow_version') or set(args)-{'after','through','limit','history'}:
            raise ValueError('CURRENT_BOUND_WORK_REQUIRED')
        workflow.guard(scope,allow_drain=True)
        import hashlib
        ledger=workflow.open_ledger(self.pool_path)
        high=ledger.snapshot()['event_high_water']
        through=args.get('through',high)
        if 'history' in args and type(args['history']) is not bool:
            raise ValueError('INVALID_HISTORY_QUERY')
        if args.get('history'):
            # This is a separate read view, not an issued business prefix. A
            # historical page must never advance issued_through or allow ACK.
            start=args.get('after',0);limit=args.get('limit',100)
            if (type(start) is not int or type(through) is not int or not 0<=start<=through<=high
                    or type(limit) is not int or not 1<=limit<=100):
                raise ValueError('INVALID_HISTORY_QUERY')
            rows=ledger.events(after=start,through=through,run_id=scope['run_id'],limit=limit)
            return json.dumps({'kind':'readonly-work-history','can_ack':False,
                'events':workflow.project_events(ledger,rows),
                'after':start,'displayed_through':rows[-1]['event_id'] if rows else start,
                'frozen_through':through,'may_have_more':len(rows)==limit,
                'notice':'历史回读未改变 issued_through/processed_through；不能据此确认业务事件。'},ensure_ascii=False)
        ledger=workflow.open_ledger(self.pool_path,readonly=False)
        page_args={key:value for key,value in args.items() if key!='history'}
        page_args['through']=through
        suffix=hashlib.sha256(json.dumps(page_args,sort_keys=True).encode()).hexdigest()
        page=ledger.work_inputs(workflow.identity_from_scope(scope),**page_args,
            command_id='tool-input:'+scope['execution_id']+':'+suffix)
        if page.get('status') not in {'COMMITTED','DUPLICATE'}:
            return json.dumps(page,ensure_ascii=False)
        return json.dumps(workflow.current_input(self.pool_path,scope['role'],scope=scope,event_page=page),ensure_ascii=False)

    def scheduler_observation_tick(self,home=None,hook_contract_version=None,**kwargs):
        if Path(home or self.home).resolve()!=self.home.resolve():
            return None
        from scheduler_observation import receipt
        if hook_contract_version != 1:
            return receipt(self.home,'REJECTED',reason='OBSERVER_HOST_CONTRACT_MISMATCH',
                           host_contract=hook_contract_version)
        receipt(self.home,'TRIGGERED',host_contract=hook_contract_version)
        result=self.observation_tick(home=home,**kwargs)
        return result

    def observation_tick(self,home=None,**kwargs):
        if Path(home or self.home).resolve()!=self.home.resolve() or not self.pool_path.is_file():
            return None
        if not self._workflow_tick_lock.acquire(blocking=False):
            return {'scheduled':False,'reason':'ROLE_TICK_IN_FLIGHT'}
        def collect_and_continue():
            observation=None
            collected=None
            try:
                from role_status import runtime_module
                runtime_module()
                import role_observer
                observation=role_observer.observation_tick(self.home,self.pool_path,sender=None)
                if workflow.active(self.pool_path):
                    collected=workflow.collect_event_tick(self.pool_path,home=self.home)
                self._last_workflow_observation=observation
                from scheduler_observation import receipt
            except Exception as exc:
                from scheduler_observation import receipt
                receipt(self.home,'FAILED',reason=type(exc).__name__)
                logger.warning('角色观察/事件接续失败；持久事件保留到下一原生tick',exc_info=True)
            finally:
                self._workflow_tick_lock.release()
            # Neither a synchronous native job nor a slow notification owns the
            # collection lock. No queued notification workers accumulate either.
            try:
                workflow_result=workflow.submit_event_tick(self.pool_path,collected,home=self.home,
                                                           drain_notifications=False)
                from scheduler_observation import receipt
                observed_at=(observation or {}).get('at') or (observation or {}).get('sampled_at')
                receipt(self.home,'COLLECTED',observation_at=observed_at,
                        workflow_tick_submitted=workflow_result is not None,
                        dispatch={role:value.get('state') for role,value in
                                  (workflow_result or {}).get('dispatch',{}).items()})
                if self._workflow_delivery_lock.acquire(blocking=False):
                    try:
                        if observation and observation.get('outbox'):
                            observation['deliveries']=role_observer.drain_monitor_notifications(
                                observation['outbox'],role_observer.monitor_sender(self.pool_path),**self.notification_budget)
                        if workflow_result is not None:
                            from workflow_notifications import drain_business_notifications
                            workflow_result['notifications']=drain_business_notifications(self.pool_path,**self.notification_budget)
                    finally:
                        self._workflow_delivery_lock.release()
            except Exception:
                logger.warning('角色提交/通知失败；持久事件保留到下一原生tick',exc_info=True)
        from agent.memory_provider import spawn_context_thread
        try:
            spawn_context_thread(collect_and_continue,name='role-workflow-native-tick').start()
        except Exception:
            self._workflow_tick_lock.release()
            raise
        return {'scheduled':True,'reason':'EXISTING_NATIVE_TICK'}

    def transform_cron_report(self,job=None,execution_id='',agent_identity=None,final_response='',**kwargs):
        from workflow_reports import transform_native_report, transform_coordinator_notice
        config=(job or {}).get('role_pool') or {}
        try:
            if config.get('kind')=='business':
                bound=(agent_identity or {}).get('session_id','')
                scope=self._frozen_scope(bound)
                if workflow.active(self.pool_path) or (scope or {}).get('workflow_version'):
                    if scope and scope.get('role')=='COORDINATOR':
                        return transform_coordinator_notice(scope,execution_id,
                            final_response or kwargs.get('content',''),self.home,kind='COORDINATOR_PROGRESS')
                    return {'content':'','suppress':True,'metadata':{'reason':'BUSINESS_EVENT_OUTBOX_ONLY'}}
            if config.get('kind')=='report' and (job or {}).get('no_agent') and config.get('role')=='COORDINATOR':
                scope=read_json(self.home/'runtime/role-pool-executions'/(execution_id+'.json'),{})
                if scope.get('workflow_version'):
                    failed=kwargs.get('success') is False
                    return transform_coordinator_notice(scope,execution_id,
                        '本次定时短报脚本失败，未形成有效状态；请核对本次原生执行日志。' if failed else final_response,
                        self.home,kind='REPORT_FAILURE' if failed else 'STATUS_BRIEF')
                if workflow.active(self.pool_path):
                    return {'content':'','suppress':True,'metadata':{'reason':'REPORT_SCOPE_UNVERIFIED'}}
        except Exception:
            logger.exception('协调者通知入账失败；本次未确认投递，保留原生执行证据')
            return {'content':'','suppress':True,'metadata':{'reason':'COORDINATOR_NOTICE_UNVERIFIED'}}
        if config.get('kind')!='report' or (job or {}).get('no_agent'): return None
        identity=agent_identity or {}
        try:
            scope=self._frozen_scope(identity.get('session_id',''))
            if not scope:
                if workflow.active(self.pool_path):
                    return {'content':'','suppress':True,'metadata':{'reason':'REPORT_SCOPE_UNVERIFIED'}}
                return None
            if kwargs.get('success') is False and scope.get('role')=='COORDINATOR':
                return transform_coordinator_notice(scope,execution_id,
                    '本次定时报告生成失败，未形成有效进展总结。请核对本次原生执行日志；原验收门禁保持。'
                    +'\n执行：'+execution_id,self.home,kind='REPORT_FAILURE')
            return transform_native_report(scope,execution_id,final_response,identity)
        except Exception:
            logger.warning('报告产物核验失败，已阻止未经排版的原始模型输出',exc_info=True)
            return {'content':'','suppress':True,'metadata':{'reason':'REPORT_SCOPE_UNVERIFIED'}}

    def acknowledge_cron_report(self,metadata=None,**kwargs):
        from workflow_reports import acknowledge_native_report
        return acknowledge_native_report(metadata or {},**kwargs)

    def work_command_tool(self,args,*,session_id='',turn_id='',**kwargs):
        import json
        scope=self._frozen_scope(session_id)
        if not scope or not scope.get('workflow_version'):
            raise ValueError('CURRENT_BOUND_WORK_REQUIRED')
        if set(args)!={'action','payload','command_id'}:
            raise ValueError('需要action、payload和稳定command_id')
        result=workflow.command(scope,args['action'],args['payload'],command_id=args['command_id'])
        return json.dumps(result,ensure_ascii=False)

    def control_tool(self,args,*,session_id='',turn_id='',**kwargs):
        import json
        identity=self.manual_turns.get((session_id,turn_id))
        from workflow_intake import scoped_control
        source=self.manual_sources.get((session_id,turn_id),{})
        if (not identity or identity['session_kind']!='group_chat' or
                not (self.manual.access(session_id=session_id,turn_id=turn_id)==FULL
                     or scoped_control(source,args.get('action')))):
            raise ValueError('CURRENT_NATIVE_FULL_GRANT_REQUIRED')
        if set(args)-{'action','target'} or 'action' not in args:
            raise ValueError('仅接受action与精确target')
        result=workflow.submit_control(self.pool_path,identity['role'],args['action'],args.get('target'),
                                       self.manual_sources.get((session_id,turn_id),{}))
        # Accept first. A failed/paused immediate dispatch leaves the durable event
        # for the existing periodic job; no new writer or separate runner is made.
        if result.get('status') in {'COMMITTED','DUPLICATE'}:
            committed_at=({'at':time.time(),'monotonic_ns':time.monotonic_ns(),'event_id':result.get('event_id')}
                          if result.get('status')=='COMMITTED' else None)
            result['wakeup']=workflow.request_role_wakeup(self.pool_path,'COORDINATOR',home=self.home,
                                                         source='control',committed_at=committed_at)
        return json.dumps(result,ensure_ascii=False)

    def on_turn_end(self, session_id='', turn_id='', **kwargs):
        try:
            # An explicit manual turn may have restored a historical cron session ID.
            # Re-scan only exact controlled ownership; never rely on that ID's old purpose.
            for item in self.bindings():
                try:
                    manual_business.end_turn(item['root'],item['role'],session_id,turn_id)
                except Exception:
                    logger.warning('该角色人工租约清理未确认；继续核对其他精确owner',exc_info=True)
        except Exception:
            logger.warning('人工回合租约清理未确认；保留原owner，禁止接管',exc_info=True)
        finally:
            self.manual.end_turn(session_id=session_id,turn_id=turn_id)
            self.manual_turns.pop((session_id,turn_id),None)
            self.manual_sources.pop((session_id,turn_id),None)
            self.manual_progress_turns.pop((session_id,turn_id),None)

    def api_identity(self, session_id):
        """Attribute only registered role sessions; no prompt or message access."""
        details=self.api_attribution_details(session_id)
        return {key:details[key] for key in ('role','project')} if details else None

    def api_attribution_details(self, session_id):
        """Read session ownership/purpose only; keep api_identity's public shape stable."""
        frozen=self._frozen_scope(session_id)
        if frozen:
            return {'role':frozen['role'],'project':frozen['project_root'],
                    'parent_session_id':'','session_kind':('summary' if frozen.get('kind')=='report' else 'business_cron')}
        bindings=self.bindings()
        def cron_bindings(identity):
            cron=re.fullmatch(r'cron_(.+)_\d{8}_\d{6}', identity or '')
            fields=('poll_job_id','report_job_id','morning_report_job_id')
            return [(r,'business_cron' if key=='poll_job_id' else 'summary')
                    for r in bindings for key in fields if cron and r.get(key)==cron[1]]
        matches=cron_bindings(session_id)
        parent=''
        database=self.home/'state.db'
        if database.is_file():
            with sqlite3.connect(database.as_uri()+'?mode=ro', uri=True, timeout=.2) as db:
                columns={row[1] for row in db.execute('PRAGMA table_info(sessions)')}
                first=db.execute('SELECT parent_session_id FROM sessions WHERE id=?',(session_id,)).fetchone()
                parent=str(first[0] or '') if first else ''
                current=session_id; seen=set()
                for _ in range(8):
                    if matches: break
                    if not current or current in seen: break
                    seen.add(current)
                    ancestor_scope=self._frozen_scope(current)
                    if ancestor_scope:
                        return {'role':ancestor_scope['role'],'project':ancestor_scope['project_root'],
                                'parent_session_id':parent,'session_kind':('summary' if ancestor_scope.get('kind')=='report' else 'business_cron')}
                    matches=cron_bindings(current)
                    if matches: break
                    chat_type='chat_type' if 'chat_type' in columns else 'NULL'
                    row=db.execute('SELECT chat_id,parent_session_id,'+chat_type+' FROM sessions WHERE id=?',(current,)).fetchone()
                    if not row: break
                    if row[2] and row[2]!='group':
                        return None
                    matches=[(r,'group_chat') for r in bindings if row[0] and r.get('chat_id')==row[0]]
                    if matches: break
                    if row[0]:
                        return None
                    current=row[1]
        if len(matches)>1:
            raise ValueError('AMBIGUOUS_ROLE_SESSION_BINDING')
        if not matches:
            # With a damaged known binding, absence is not proof that this is
            # an unrelated session. Healthy exact matches above remain usable.
            if any(issue.get('identity_unknown', issue['code'] != 'UNKNOWN_ROLE_OR_AUDIT_RECORD') for issue in self.binding_issues):
                raise ValueError('ROLE_SESSION_BINDING_UNVERIFIABLE')
            return None
        item,purpose=matches[0]
        if item.get('binding_error'):
            raise ValueError('ROLE_BINDING_QUARANTINED: '+item['binding_error'])
        return {'role':item['role'],'project':str(item['root']),
                'parent_session_id':parent,'session_kind':purpose}

    def record_api_time(self, kind, *, session_id='', model='', **metadata):
        # Observability failure must never suppress, retry or rewrite a model request.
        try:
            identity=self.api_identity(session_id)
            if not identity or not metadata.get('api_request_id'): return
            fields={k:metadata[k] for k in ('api_request_id','started_at','ended_at','first_chunk_at',
                    'retryable','retry_count','max_retries','retry_pending','provider') if k in metadata}
            action={'start':record_api_start,'end':record_api_end,'error':record_api_error}[kind]
            action(self.home/'runtime/role-time.db',session_id=session_id,model=model,**identity,**fields)
        except Exception:
            logger.warning('角色请求计时失败；模型照常执行',exc_info=True)

    def on_api_end(self, **metadata):
        self.record_model_activity('end',**metadata)
        self.record_api_time('end',**metadata)
        self.record_usage('end',**metadata)

    def on_api_error(self, **metadata):
        self.record_model_activity('error',**metadata)
        self.record_api_time('error',**metadata)
        self.record_usage('error',**metadata)

    def usage_routes(self):
        import yaml
        source=self.home/'config.yaml'
        config=yaml.safe_load(source.read_text()) if source.is_file() else {}
        config=config or {}
        routing=config.get('plugins',{}).get('entries',{}).get('role-workflow-runtime',{}).get('settings',{}).get('model_routing',{})
        providers=config.get('providers',{})
        choices=[('local',routing.get('local_provider','qwen-a100'),routing.get('local_model','')),
                 ('cloud',routing.get('cloud_provider',''),routing.get('cloud_model',''))]
        choices += [('cloud',item.get('cloud_provider',''),item.get('cloud_model',''))
                    for item in routing.get('cloud_profiles',{}).values()]
        return [(route,providers.get(provider.removeprefix('custom:'),{}).get('base_url',''),model)
                for route,provider,model in choices if provider]

    def record_usage(self, kind, *, session_id='', model='', **metadata):
        try:
            if not metadata.get('api_request_id'): return
            frozen=usage_accounting.request_identity(self.home/'runtime/role-usage.db',session_id=session_id,
                physical_attempt_id=metadata.get('physical_attempt_id',''),api_request_id=metadata['api_request_id'])
            identity=({k:frozen[k] for k in ('role','project','parent_session_id','session_kind')} if frozen else
                      self.manual_turns.get((session_id,metadata.get('turn_id',''))) or
                      self.api_attribution_details(session_id))
            if not identity: return
            if metadata.get('aux_task') and not frozen:
                # Auxiliary calls use the owning native session ID. Their
                # explicit parent is that same session, not a fabricated child.
                identity['parent_session_id']=str(session_id)
            if frozen:
                route,alias=frozen['route'],frozen['endpoint_alias']
            else:
                try:
                    routes=self.usage_routes()
                except Exception:
                    logger.warning('用量路由分类不可用；保留unknown分类，不丢弃请求',exc_info=True)
                    routes=[]
                route,alias=usage_accounting.classify_route(metadata.get('base_url'),model,routes)
            fields={k:metadata[k] for k in ('api_request_id','physical_attempt_id','attempt_started_at',
                    'started_at','ended_at','provider','usage','aux_task','retry_count','error',
                    'provider_dispatched') if k in metadata}
            attribution_keys=('run_id','work_id','task_id','business_attempt','execution_id')
            if frozen:
                fields.update({key:frozen.get(key) for key in attribution_keys})
            else:
                work_scope=self._frozen_scope(session_id)
                if work_scope and work_scope.get('workflow_version'):
                    fields.update({key:work_scope.get(key) for key in ('run_id','work_id','task_id','execution_id')})
                    fields['business_attempt']=work_scope.get('attempt')
            if kind=='end': fields.setdefault('provider_dispatched',True)
            if frozen:
                fields['aux_task']=frozen['aux_task']
            usage_accounting.record(self.home/'runtime/role-usage.db',kind,session_id=session_id,
                                    model=model,route=route,endpoint_alias=alias,**identity,**fields)
        except Exception:
            logger.warning('角色用量采集失败；模型照常执行',exc_info=True)

    def record_model_activity(self, phase, *, session_id='', auxiliary=False, **metadata):
        """Independent lifecycle evidence; failed usage/time ledgers cannot certify it."""
        try:
            scope=self._frozen_scope(session_id)
            if not scope or not scope.get('workflow_version') or scope.get('kind')!='business':
                return None
            from workflow_activity import record_model_start,record_model_end
            physical=metadata.get('physical_attempt_id')
            logical=metadata.get('api_request_id')
            if phase=='start':
                kind=('compaction' if metadata.get('aux_task') in {'compression','compaction'}
                      else 'auxiliary_request') if auxiliary else 'main_request'
                return record_model_start(self.home,scope,physical,logical,
                    at=metadata.get('attempt_started_at',metadata.get('started_at',time.time())),kind=kind)
            # Auxiliary streaming post only hands its consumer a stream handle;
            # it does not know whether the stream was consumed or completed.
            verified=not (auxiliary and metadata.get('streaming') is True)
            return record_model_end(self.home,scope,physical,logical,
                at=metadata.get('ended_at',time.time()),
                outcome='ERROR' if phase=='error' else 'SUCCESS',end_verified=verified)
        except Exception:
            logger.warning('角色模型生命周期观测缺失；原请求行为保持',exc_info=True)
            return None

    def on_aux_start(self, **metadata):
        self.record_model_activity('start',auxiliary=True,**metadata)
        self.record_usage('start',**metadata)

    def on_aux_end(self, **metadata):
        self.record_model_activity('error' if metadata.get('error') else 'end',auxiliary=True,**metadata)
        self.record_usage('error' if metadata.get('error') else 'end',**metadata)

    def bindings(self):
        rows,issues=registry_bindings(self.registry,self._binding_rows)
        if issues!=self.binding_issues:
            if issues: logger.error('角色绑定校验发现异常：%s',issues)
            elif self.binding_issues: logger.info('角色绑定校验异常已解除')
        self._binding_rows=rows
        self.binding_issues=issues
        return rows

    async def reply(self,gateway,event,text):
        adapter=gateway._delivery_adapter_for(event.source)
        if adapter is None: raise ValueError('原会话投递 adapter 不可用')
        outcome=await adapter.send(event.source.chat_id,text,reply_to=event.message_id)
        if getattr(outcome,'success',False) is not True:
            raise RuntimeError('回复投递未确认；请求如已落盘仍保留')

    async def on_message(self,event,gateway=None,native_primary_input=None,**kwargs):
        source=event.source; text=(event.text or '').strip()
        selection_attempt=False
        if gateway is None or source is None or getattr(event,'internal',False): return None
        platform=getattr(getattr(source,'platform',None),'value',None)
        if platform!='feishu' or getattr(source,'is_bot',False): return None
        # Native stop/reset and inspection controls are separate from business work.
        routing_action=parse_command(text)
        # Hook 位于原生鉴权之前，必须复用同一原生鉴权；失败不自行授予权限。
        authorize=getattr(gateway,'_is_user_authorized_for_source',None) or gateway._is_user_authorized
        if not authorize(source): return None
        try:
            bindings=self.bindings()
            matches=[r for r in bindings if r.get('chat_id')==source.chat_id]
            # 只处理实际角色绑定；诊断私聊/普通群不因查询文本被纳入工作流。
            if not matches: return None
            if text.startswith('/') and text not in ('/llm-load','/progress','/role-full') and not text.startswith('/role-full ') and routing_action is None:
                safe_commands={'/stop','/cancel','/deny','/new','/reset','/help','/status','/usage'}
                if text.split()[0] in safe_commands: return None
                await self.reply(gateway,event,'角色群默认只读，本次未执行该控制命令。需要执行操作时，请在当前消息明确写“本轮授予全部权限：具体动作”。')
                return {'action':'skip','reason':'role-runtime-native-control-readonly'}
            if routing_action is not None:
                answer=(await asyncio.to_thread(self.router.command,routing_action) if routing_action in {'status','help'} else
                        '角色群默认只读，本次未修改模型路由。若需切换，请在当前消息明确写“本轮授予全部权限：切换到云端模型”。')
                await self.reply(gateway,event,answer)
                return {'action':'skip','reason':'role-runtime-model-control-readonly'}
            if load_query(text):
                try:
                    snapshot=await asyncio.to_thread(collect,self.metrics_url,self.home/'runtime/qwen-load.json')
                    answer=render(snapshot)
                except Exception as exc:
                    answer='**千问负载：采集失败**\n- **状态：** 未知，不能据此判断空闲。\n- **错误：** '+type(exc).__name__+'；本次未调用模型。'
                await self.reply(gateway,event,answer)
                return {'action':'skip','reason':'role-runtime-load-no-model'}
            if len(matches)!=1: raise ValueError('角色项目绑定有歧义，未派发')
            item=matches[0]
            if item.get('binding_error'):
                raise ValueError('ROLE_BINDING_QUARANTINED: '+item['binding_error'])
            if item['role']=='COORDINATOR': self.coordinator_chat_ids.add(source.chat_id)
            primary=native_primary_input or {}
            proven=(primary.get('schema_version')==1 and primary.get('platform')=='feishu'
                    and primary.get('chat_id')==source.chat_id and primary.get('user_id')==source.user_id
                    and primary.get('message_id')==event.message_id and primary.get('is_bot') is False
                    and isinstance(primary.get('primary_text'),str))
            key=self.manual.record_request(project=str(item['root']),role=item['role'],chat_id=str(source.chat_id),
                user_id=str(source.user_id),message_id=str(event.message_id),
                primary_text=primary['primary_text'] if proven else '',authorized=True,native_primary_text=proven and self.native_ready)
            self.manual_requests[(source.chat_id,source.user_id,event.message_id)]={
                'key':key,'project':str(item['root']),'role':item['role'],
                'progress_intent':_manual_progress_request(item['role'],primary.get('primary_text') if proven else text),
                'provenance':{'platform':'feishu','chat_id':str(source.chat_id),'user_id':str(source.user_id),
                    'message_id':str(event.message_id),'primary_text':primary['primary_text'] if proven else '',
                    'role':item['role'],'authenticated':True,'current_full':bool(proven and self.native_ready and explicit_full_permission(primary['primary_text']))}}
            while len(self.manual_requests)>2048:
                self.manual_requests.pop(next(iter(self.manual_requests)))
            full=proven and self.native_ready and explicit_full_permission(primary['primary_text'])
            if full:
                requested=full_permission_action(primary['primary_text']) or ''
                if item['role']=='COORDINATOR' and self.pool_path.is_file() and not workflow.active(self.pool_path):
                    from requirement_gateway import direct_selection,selection_receipt
                    target=direct_selection(primary['primary_text'],self.pool_path)
                    if target is not None:
                        selection_attempt=True
                        from requirement_runtime import request
                        result=await asyncio.to_thread(request,self.pool_path,target,
                            self.manual_requests[(source.chat_id,source.user_id,event.message_id)]['provenance'],trusted_ingress=True)
                        await self.reply(gateway,event,selection_receipt(result))
                        return {'action':'skip','reason':'role-runtime-requirement-selection'}
                full_routing=parse_command(requested)
                if full_routing is not None:
                    answer=await asyncio.to_thread(self.router.command,full_routing)
                    await self.reply(gateway,event,answer)
                    return {'action':'skip','reason':'role-runtime-explicit-model-control'}
                if requested.split() and requested.split()[0] in {'/approve','/deny'}:
                    return {'action':'rewrite','text':requested,'reason':'role-runtime-explicit-native-approval'}
            if proven and self.native_ready and item['role']=='COORDINATOR' and workflow.active(self.pool_path):
                from workflow_user_decisions import answer_source
                if answer_source(primary['primary_text']):
                    provenance=self.manual_requests[(source.chat_id,source.user_id,event.message_id)]['provenance']
                    result=await asyncio.to_thread(workflow.submit_control,self.pool_path,'COORDINATOR',
                        'record_user_decision',{},provenance)
                    if result.get('status') in ('COMMITTED','DUPLICATE'):
                        result['wakeup']=await asyncio.to_thread(workflow.request_role_wakeup,self.pool_path,'COORDINATOR',
                            home=self.home,source='user-decision-control')
                    import json
                    return {'action':'rewrite','reason':'role-runtime-user-decision',
                        'text':'[原生用户裁决登记回执]\n'+json.dumps(result,ensure_ascii=False)+
                        '\n据回执说明已登记或失败；登记不等于关闭门禁。后台核对原提问关联并执行，不要求用户重发。用户原文：\n'+primary['primary_text']}
                from workflow_supplemental import source_task
                task_id=source_task(primary['primary_text'])
                if task_id:
                    provenance=self.manual_requests[(source.chat_id,source.user_id,event.message_id)]['provenance']
                    result=await asyncio.to_thread(workflow.submit_control,self.pool_path,'COORDINATOR',
                        'request_supplemental_verification',{'task_id':task_id,'profile':'mysql_migration'},provenance)
                    if result.get('status') in ('COMMITTED','DUPLICATE'):
                        result['wakeup']=await asyncio.to_thread(workflow.request_role_wakeup,self.pool_path,'TESTER',
                            home=self.home,source='supplemental-control')
                    import json
                    return {'action':'rewrite','reason':'role-runtime-supplemental-request',
                        'text':'[原生隔离MySQL补验请求回执]\n'+json.dumps(result,ensure_ascii=False)+
                        '\n仅按回执说明已入账/拒绝/排队状态；不宣称测试已通过，不让用户重复发送已入账指令。用户原文：\n'+primary['primary_text']}
            if progress_query(text) and item['role']=='COORDINATOR':
                from cron.jobs import get_job
                job=get_job(item['poll_job_id']) or {}
                answer=await asyncio.to_thread(progress_answer,item['root'],job)
                await self.reply(gateway,event,answer)
                return {'action':'skip','reason':'role-runtime-progress-no-model'}
            # 人工消息不自动入业务队列，更不能被脚本回执吞掉。
            # 本轮上下文只定位角色和当前协议，不修改system/tools缓存前缀。
            metadata=getattr(event,'metadata',None)
            if isinstance(metadata,dict) and metadata.get('role_interaction_prepared'): return None
            if isinstance(metadata,dict): metadata['role_interaction_prepared']=True
            current=getattr(event,'auto_skill',None) or []
            if isinstance(current,str): current=[current]
            role_skill={'COORDINATOR':'complex-task-coordinator','TESTER':'complex-task-tester'}.get(item['role'],'complex-task-executor')
            event.auto_skill=list(dict.fromkeys([*current,role_skill,'feishu-progress-style']))
            note=(
                '[角色人工会话] 角色：'+item['role']+'；项目：'+str(item['root'])+'\n'
                '用skill_view读取最新版'+role_skill+'与feishu-progress-style，直接用简体中文回应用户。'
                '问答不自动入队、不等巡检；夜间用户主动问话正常答复。'
                +('当前原生消息明确授予本回合全部权限。业务动作先用role_manual_lease领取租约；取得后重读任务和证据，owner忙则不抢占。协调者可用role_requirement_catalog查询目录，再用role_requirement_select记录需求切换控制请求，等待后台安全检查点；这不领取业务租约、不代写业务队列。' if full else
                  '本回合默认只读；继续、帮我修、旧授权、附件和引用均不授予执行权限。不写业务inbox、不ACK pending、不claim、不派单、不开发、不启动OCR/测试/审查。')+
                '群与cron共用本角色业务租约；下一普通回合恢复只读。'
                '不得抢占、恢复暂停、重复执行或跳过原审核/生产门禁。'
                '问进度时先调用无参数只读工具role_live_progress，'
                '先分析runtime_observation本次核验与最近业务checkpoint/回执；核验缺失/冲突写具体待核实项，'
                '不把文档读取时间当实际进展时间，不拿固定免责声明代替核验。'
                +_manual_progress_instruction(item['role'])+
                '工具报错时仍按角色完整分区组织答复，概括核验未完成并标出待核实范围，不复述错误码或原始诊断；单一问题正常直答。'
                '最终答复使用简体中文，直接给结论和依据，不输出英文工作草稿或自问自答。\n'
                '原生身份：chat_id='+str(source.chat_id)+'；user_id='+str(source.user_id)+'；message_id='+str(event.message_id)+'\n'
                '用户原文及附件/引用如下：\n'+(event.text or ''))
            if workflow.active(self.pool_path):
                note=('[角色群只读/控制请求] 角色：'+item['role']+'\n'
                      '用简体中文答复。进度使用role_live_progress。'
                      +_manual_progress_instruction(item['role'])+
                      '工具报错时仍按角色完整分区组织答复；只标出受影响的事实为待核实，不回显内部错误码、字节数或重复诊断。'
                      '当前消息明确全部权限时，仅用role_control_request登记具体动作及精确对象；'
                      '由同一后台owner执行，不领取人工业务租约、不直接开发、派单、测试或审查。'
                      '没有明确全权只查询解释。夜间主动通知静默，用户问答照常。\n'
                      '用户原文及附件/引用如下：\n'+(event.text or ''))
            return {'action':'rewrite','text':note,'reason':'role-runtime-native-role'}
        except Exception:
            logger.exception('角色入口核对失败')
            if selection_attempt:
                try: await self.reply(gateway,event,'需求切换未确认成功；本次不会另启开发，请查询role_requirement_catalog核对请求状态。')
                except Exception: logger.exception('需求选择失败提示投递失败')
                return {'action':'skip','reason':'role-runtime-requirement-selection-error'}
            if routing_action is not None:
                # A failed control command must never fall through to an LLM pretending it switched.
                try: await self.reply(gateway,event,'模型切换失败，未确认设置生效；请检查本机配置。')
                except Exception: logger.exception('模型控制失败提示投递失败')
                return {'action':'skip','reason':'role-runtime-model-control-error'}
            if not load_query(text) and not progress_query(text):
                # 绑定故障不能把人工会话再次封死；模型必须先核绑定再做写操作。
                return None
            if source.chat_id not in self.coordinator_chat_ids:
                return None
            try:
                await self.reply(gateway,event,'**当前状态无法核实**\n\n- **下一步：** 请直接描述问题，由模型核对；本次未改变任务或调度。')
            except Exception: logger.exception('角色入口失败提示投递失败')
            return {'action':'skip','reason':'role-runtime-intake-error'}

    def on_api(self,session_id='',model='',request=None,system_prompt=None,**metadata):
        try:
            from workflow_sessions import observe
            observe(self.home,self._frozen_scope(session_id),session_id=session_id,
                    api_call_count=metadata.get('api_call_count',0),
                    approx_input_tokens=metadata.get('approx_input_tokens',0))
        except Exception:
            logger.warning('技术会话交接计数未记录；保留原生请求上限',exc_info=True)
        self.record_model_activity('start',session_id=session_id,**metadata)
        self.record_api_time('start',session_id=session_id,model=model,**metadata)
        self.record_usage('start',session_id=session_id,model=model,**metadata)
        if 'qwen3.8' not in model: return None
        # 原生 hook 的 request 经脱敏/限长，工具观测只使用实际可取得的字段。
        body=(request or {}).get('body')
        if not isinstance(body,dict): body={}
        row=self.audit.record(session_id=session_id,request=body,system_prompt=system_prompt,model=model,**metadata)
        if row and (row['system_changed'] or row['tools_changed']):
            logger.info('提示前缀变化 session=%s system=%s tools=%s；仅观察，不重建或改写请求',
                        session_id,row['system_changed'],row['tools_changed'])
        return None

    def on_route(self,session_id='',**metadata):
        try:
            if not self.api_identity(session_id): return None
            config=self.router.config()
            if not config.get('enabled',False): return None
            decision=self.router.decision(config)
            runtime=self.router.runtime(config,decision['route'])
            return {'runtime':runtime,'reason':decision['reason']}
        except Exception:
            logger.warning('角色模型选路失败；阻止本次请求，避免绕过本地保护',exc_info=True)
            return {'error':'角色模型选路失败；请检查云端凭据和供应商配置。未自动抢占本地模型。'}

    async def on_busy_message(self,event,gateway=None,**kwargs):
        source=getattr(event,'source',None)
        if gateway is None or source is None or getattr(event,'internal',False): return None
        if getattr(getattr(source,'platform',None),'value',None)!='feishu' or getattr(source,'is_bot',False): return None
        authorize=getattr(gateway,'_is_user_authorized_for_source',None) or gateway._is_user_authorized
        if not authorize(source): return None
        matches=[row for row in self.bindings() if row.get('chat_id')==source.chat_id]
        if not matches: return None
        text=(event.text or '').strip()
        command=text.split()[0] if text else ''
        if command in {'/stop','/cancel','/deny','/new','/reset','/help','/status','/usage'}: return None
        direct_control=parse_command(text) is not None or load_query(text) or progress_query(text)
        primary=None
        if (not direct_control and len(matches)==1 and matches[0]['role']=='COORDINATOR'
                and self.pool_path.is_file()):
            from requirement_gateway import direct_selection
            from gateway.native_turn_input import native_primary_input
            primary=native_primary_input(event) or {}
            proven=(self.native_ready and primary.get('schema_version')==1 and primary.get('platform')=='feishu'
                and primary.get('chat_id')==source.chat_id and primary.get('user_id')==source.user_id
                and primary.get('message_id')==event.message_id and primary.get('is_bot') is False)
            try: direct_control=bool(proven and direct_selection(primary.get('primary_text'),self.pool_path) is not None)
            except Exception: direct_control=False
        if direct_control:
            from gateway.native_turn_input import native_primary_input
            return await self.on_message(event,gateway=gateway,native_primary_input=primary if primary is not None else native_primary_input(event))
        # A busy user's message starts its own native turn. Steer text belongs
        # to the old turn and cannot carry a new user's permission receipt.
        return {'action':'queue',
                'message':'消息已排队，将在当前群聊回合结束后处理；授权只对新回合生效，不改变正在执行的回合。',
                'queue_full_message':'群聊消息队列已满，本次未执行；不会把新授权注入旧回合。',
                'queue_unavailable_message':'消息暂未入队：当前会话投递通道不可用，本次未执行。'}


def register(ctx):
    from hermes_constants import get_hermes_home
    home=Path(get_hermes_home())
    from runtime_common import save_json
    save_json(home/'runtime/role-observation/mechanism-activation.json',activate_current_runtime())
    native_ready=False
    try:
        activation=activate_current_native_policy()
        native_ready=activation.get('state')=='ACTIVATED'
        from runtime_common import save_json
        save_json(home/'runtime/manual-policy-activation.json',activation)
    except Exception:
        logger.exception('可信人工回合入口激活失败；群聊继续只读，不签发全部权限')
    plugin=RuntimePlugin(registry=ctx.get_config('registry',str(home/'role-projects.json')),
                         metrics_url=ctx.get_config('metrics_url','http://192.168.2.45:8002/metrics'),home=home,
                         coordinator_chat_ids=ctx.get_config('coordinator_chat_ids',[]),native_ready=native_ready,
                         pool_path=ctx.get_config('pool_path',str(home/'role-pool.json')),
                         notification_budget=ctx.get_config('notification_budget',None))
    from workflow_file_tools import install
    try:
        file_backend = install(ctx, plugin)
        from business_ledger import _process
        file_backend['process_start'] = _process(os.getpid())['process_start']
        file_backend['checked_at'] = time.time()
        save_json(home/'runtime/role-file-backends'/(str(os.getpid())+'.json'), file_backend)
    except Exception:
        logger.exception('受限文件后端未激活；文件写工具保持拒绝，terminal 门禁不变')
    ctx.register_hook('pre_gateway_dispatch',plugin.on_message)
    ctx.register_hook('pre_llm_call',plugin.on_llm)
    ctx.register_hook('transform_llm_output',plugin.transform_manual_progress_output)
    ctx.register_hook('pre_tool_call',plugin.on_tool)
    ctx.register_hook('post_tool_call',plugin.on_tool_end)
    from hermes_cli.plugins import VALID_HOOKS
    if 'on_turn_end' in VALID_HOOKS:
        ctx.register_hook('on_turn_end',plugin.on_turn_end)
    ctx.register_hook('pre_api_request',plugin.on_api)
    ctx.register_hook('post_api_request',plugin.on_api_end)
    ctx.register_hook('api_request_error',plugin.on_api_error)
    ctx.register_hook('pre_auxiliary_call',plugin.on_aux_start)
    ctx.register_hook('post_auxiliary_call',plugin.on_aux_end)
    ctx.register_hook('resolve_model_route',plugin.on_route)
    ctx.register_hook('pre_gateway_busy_dispatch',plugin.on_busy_message)
    from scheduler_observation import capability
    probe=capability(home)
    if probe['state'] not in {'READY_AWAITING_TICK','ADOPTED'}:
        logger.warning('角色观察hook能力不匹配，自动链路未验收：%s',probe.get('reason'))
    ctx.register_hook('on_scheduler_tick',plugin.scheduler_observation_tick)
    ctx.register_hook('transform_cron_result',plugin.transform_cron_report)
    ctx.register_hook('post_cron_delivery',plugin.acknowledge_cron_report)
    ctx.register_tool('role_live_progress','role_workflow',
        {'name':'role_live_progress','description':'只读查询当前账本时间、水位、任务和全局OCR门；历史叙述及诊断按哈希引用，不领取或执行任务。回答协调者进度时保留完整七区，执行者/测试者保留本人四区；工具缺口不删区。',
         'parameters':{'type':'object','properties':{},'additionalProperties':False}},plugin.live_tool)
    ctx.register_tool('role_repo_state','role_workflow',
        {'name':'role_repo_state','description':'只读核验当前选中需求代码仓的branch/head/dirty/ahead_behind/worktrees；无需全权，不接受路径或shell。',
         'parameters':{'type':'object','properties':{},'additionalProperties':False}},plugin.repo_tool)
    ctx.register_tool('role_manual_lease','role_workflow',
        {'name':'role_manual_lease','description':'仅当前用户明确授予全部权限的人工回合可领取、续期或释放本角色共享业务租约；owner忙时不接管。',
         'parameters':{'type':'object','properties':{'action':{'type':'string','enum':['claim','renew','release']}},
                       'required':['action'],'additionalProperties':False}},plugin.lease_tool)
    ctx.register_tool('role_requirement_catalog','role_workflow',
        {'name':'role_requirement_catalog','description':'只读查看固定需求目录、当前角色池范围和待切换请求，不领取任务。',
         'parameters':{'type':'object','properties':{},'additionalProperties':False}},plugin.catalog_tool)
    ctx.register_tool('role_requirement_select','role_workflow',
        {'name':'role_requirement_select','description':'仅当前明确全部权限的协调者群回合，可登记用户原文指定的需求切换；后台在安全点执行，不抢占在途任务。',
         'parameters':{'type':'object','properties':{'target':{'type':'string'}},'required':['target'],
                       'additionalProperties':False}},plugin.selection_tool)
    ctx.register_tool('role_work_input','role_workflow',
        {'name':'role_work_input','description':'读取本次原生执行绑定的当前工作、冻结AC、门禁、检查点与事件；不自选身份。',
         'parameters':{'type':'object','properties':{
             'after':{'type':'integer','minimum':0},'through':{'type':'integer','minimum':0},
             'limit':{'type':'integer','minimum':1,'maximum':100},
             'history':{'type':'boolean','description':'只读回看旧事件；不发出可ACK的事件前缀，不改变业务游标。'}},'additionalProperties':False}},plugin.work_input_tool,disclosure='direct')
    from workflow_contracts import control_target_schema
    ctx.register_tool('role_control_request','role_workflow',
        {'name':'role_control_request','description':'可信当前用户明确控制或裁决原文持久登记；由后台owner执行，不需全权口令。',
         'parameters':{'type':'object','properties':{
             'action':{'type':'string','enum':['accept_requirement','select_run','pause','resume','continue','cancel','rework',
                                            'authorize_service','authorize_merge','revoke_authorization','request_supplemental_verification','record_user_decision']},
             'target':control_target_schema()},
             'required':['action'],'additionalProperties':False}},plugin.control_tool,disclosure='direct')
    for name,toolset,actions in (
            ('role_coordination_command','role_coordination',['publish_plan','decide','checkpoint','safe_release',
                'service_submit','service_collect','service_decide','service_resume','service_reject','extend_task_scope','refresh_task_base','refresh_development_base','merge','complete','complete_requirement','append_development_log','commit_documentation','resolve_user_decision']),
            ('role_development_command','role_development',['deliver','checkpoint','safe_release']),
            ('role_test_command','role_testing',['verify','verify_supplemental','verify_integration','deliver_integration','close_verification','checkpoint','safe_release'])):
        from workflow_contracts import command_schema
        ctx.register_tool(name,toolset,{'name':name,
            'description':'向本次执行绑定的工作单提交本角色成果；身份由运行时注入，command_id重试须保持。',
            'parameters':command_schema(actions)},plugin.work_command_tool,disclosure='direct')
