"""Hermes 现有 hook 扩展：人工对话与业务执行分离、最新简报、提示指纹只读观察。"""
import asyncio
import logging
from pathlib import Path
import re
import sqlite3
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent))
from load_metrics import load_query, collect, render
from prefix_audit import PrefixAudit
from .progress_query import progress_query, progress_answer
from runtime_common import read_json
from time_accounting import record_api_start, record_api_end, record_api_error

logger=logging.getLogger(__name__)


class RuntimePlugin:
    def __init__(self,*,registry,metrics_url,home,coordinator_chat_ids=()):
        self.registry=Path(registry); self.metrics_url=metrics_url; self.home=Path(home)
        self.coordinator_chat_ids=set(coordinator_chat_ids)
        self.audit=PrefixAudit(self.home/'runtime/prefix-audit')

    def api_identity(self, session_id):
        """Attribute only registered role sessions; no prompt or message access."""
        bindings=self.bindings()
        def cron_bindings(identity):
            cron=re.fullmatch(r'cron_([^_]+)_\d{8}_\d{6}', identity or '')
            return [r for r in bindings if r.get('poll_job_id')==cron[1]] if cron else []
        matches=cron_bindings(session_id)
        database=self.home/'state.db'
        if not matches and database.is_file():
            with sqlite3.connect(database.as_uri()+'?mode=ro', uri=True, timeout=.2) as db:
                current=session_id; seen=set()
                for _ in range(8):
                    if not current or current in seen: break
                    seen.add(current)
                    matches=cron_bindings(current)
                    if matches: break
                    row=db.execute('SELECT chat_id,parent_session_id FROM sessions WHERE id=?',(current,)).fetchone()
                    if not row: break
                    matches=[r for r in bindings if r.get('chat_id')==row[0]]
                    if matches: break
                    current=row[1]
        if len(matches)!=1: return None
        return {'role':matches[0]['role'],'project':str(matches[0]['root'])}

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
        self.record_api_time('end',**metadata)

    def on_api_error(self, **metadata):
        self.record_api_time('error',**metadata)

    def bindings(self):
        rows=[]
        for entry in read_json(self.registry,{'projects':[]})['projects']:
            root=Path(entry['project_root']).resolve()
            binding=read_json(root/'ROLE-BINDINGS.json')
            if not binding or Path(binding['project_root']).resolve()!=root:
                raise ValueError('绑定根不一致')
            for role,item in binding['roles'].items():
                rows.append({'root':root,'role':role,'control':binding['control'],**item})
        return rows

    async def reply(self,gateway,event,text):
        adapter=gateway._delivery_adapter_for(event.source)
        if adapter is None: raise ValueError('原会话投递 adapter 不可用')
        outcome=await adapter.send(event.source.chat_id,text,reply_to=event.message_id)
        if getattr(outcome,'success',False) is not True:
            raise RuntimeError('回复投递未确认；请求如已落盘仍保留')

    async def on_message(self,event,gateway=None,**kwargs):
        source=event.source; text=(event.text or '').strip()
        if gateway is None or source is None or getattr(event,'internal',False): return None
        platform=getattr(getattr(source,'platform',None),'value',None)
        if platform!='feishu' or getattr(source,'is_bot',False): return None
        # 控制/审批 slash 原样走 Hermes，不能被入队拦截造成死锁。
        if text.startswith('/') and text not in ('/llm-load','/progress'): return None
        # Hook 位于原生鉴权之前，必须复用同一原生鉴权；失败不自行授予权限。
        if not gateway._is_user_authorized(source): return None
        try:
            bindings=self.bindings()
            matches=[r for r in bindings if r['chat_id']==source.chat_id]
            # 只处理实际角色绑定；诊断私聊/普通群不因查询文本被纳入工作流。
            if not matches: return None
            if load_query(text):
                try:
                    snapshot=await asyncio.to_thread(collect,self.metrics_url,self.home/'runtime/qwen-load.json')
                    answer=render(snapshot)
                except Exception as exc:
                    answer='**千问负载：采集失败**\n- **状态：** 未知，不能据此判断空闲。\n- **错误：** '+type(exc).__name__+'；本次未调用模型。'
                await self.reply(gateway,event,answer)
                return {'action':'skip','reason':'role-runtime-load-no-model'}
            coordinators=[r for r in matches if r['role']=='COORDINATOR']
            if not coordinators: return None
            if len(coordinators)!=1: raise ValueError('协调者项目绑定有歧义，未派发')
            item=coordinators[0]
            self.coordinator_chat_ids.add(source.chat_id)
            if progress_query(text):
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
            event.auto_skill=list(dict.fromkeys([*current,'complex-task-coordinator','feishu-progress-style']))
            note=(
                '[协调者人工会话] 项目：'+str(item['root'])+'\n'
                '用skill_view读取最新版complex-task-coordinator与feishu-progress-style，直接回应用户。'
                '问答不自动入队、不等巡检；夜间用户主动问话正常答复。'
                '旧规则“普通消息只入队”已失效。明确业务动作才保存原消息身份和原文；'
                '群与cron共用COORDINATOR租约，取得租约后重读任务和处理证据；'
                '群会话取得租约可直接接续，owner忙则只保存待办并解释。'
                '不得抢占、恢复暂停、重复执行或跳过原审核/生产门禁。'
                '答复突出加粗结论、重点进展、阻塞和下一步，不贴原始材料。\n'
                '原生身份：chat_id='+str(source.chat_id)+'；user_id='+str(source.user_id)+'；message_id='+str(event.message_id)+'\n'
                '用户原文及附件/引用如下：\n'+(event.text or ''))
            return {'action':'rewrite','text':note,'reason':'role-runtime-native-coordinator'}
        except Exception:
            logger.exception('角色入口核对失败')
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
        self.record_api_time('start',session_id=session_id,model=model,**metadata)
        if 'qwen3.8' not in model: return None
        # 原生 hook 的 request 经脱敏/限长，工具观测只使用实际可取得的字段。
        body=(request or {}).get('body')
        if not isinstance(body,dict): body={}
        row=self.audit.record(session_id=session_id,request=body,system_prompt=system_prompt,model=model,**metadata)
        if row and (row['system_changed'] or row['tools_changed']):
            logger.info('提示前缀变化 session=%s system=%s tools=%s；仅观察，不重建或改写请求',
                        session_id,row['system_changed'],row['tools_changed'])
        return None


def register(ctx):
    from hermes_constants import get_hermes_home
    home=Path(get_hermes_home())
    plugin=RuntimePlugin(registry=ctx.get_config('registry',str(home/'role-projects.json')),
                         metrics_url=ctx.get_config('metrics_url','http://192.168.2.45:8002/metrics'),home=home,
                         coordinator_chat_ids=ctx.get_config('coordinator_chat_ids',[]))
    ctx.register_hook('pre_gateway_dispatch',plugin.on_message)
    ctx.register_hook('pre_api_request',plugin.on_api)
    ctx.register_hook('post_api_request',plugin.on_api_end)
    ctx.register_hook('api_request_error',plugin.on_api_error)
