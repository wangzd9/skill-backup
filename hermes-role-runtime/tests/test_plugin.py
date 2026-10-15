import importlib.util
import json
import os
import sqlite3
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

ROOT=Path(__file__).resolve().parents[1]
HERMES=Path.home()/'.hermes/hermes-agent'
sys.path.insert(0,str(ROOT)); sys.path.insert(0,str(HERMES))
spec=importlib.util.spec_from_file_location('role_plugin_test',ROOT/'__init__.py')
module=importlib.util.module_from_spec(spec); sys.modules[spec.name]=module; spec.loader.exec_module(module)
from coord_queue import pending_requests, enqueue


class Adapter:
    def __init__(self): self.messages=[]
    async def send(self,chat_id,text,**kwargs):
        self.messages.append((chat_id,text)); return SimpleNamespace(success=True)


class PluginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.project=self.root/'project'; self.project.mkdir()
        (self.project/'ROLE-BINDINGS.json').write_text(json.dumps({'schema_version':1,'project_root':str(self.project),'control':'ACTIVE',
          'roles':{'COORDINATOR':{'chat_id':'coord','poll_job_id':'native-job'},'A':{'chat_id':'a'}}}))
        registry=self.root/'registry.json';registry.write_text(json.dumps({'projects':[{'project_root':str(self.project)}]}))
        self.plugin=module.RuntimePlugin(registry=registry,metrics_url='http://localhost/metrics',home=self.root,
                   coordinator_chat_ids=['coord'])
        self.adapter=Adapter()
        self.gateway=SimpleNamespace(_is_user_authorized=lambda source:True,_delivery_adapter_for=lambda source:self.adapter)

    def event(self,text,chat='coord'):
        return SimpleNamespace(text=text,message_id='message',internal=False,source=SimpleNamespace(platform=SimpleNamespace(value='feishu'),
                               chat_id=chat,user_id='user',is_bot=False))

    async def test_natural_queries_and_actions_use_model_without_automatic_business_queue(self):
        for text in ('当前状态','给我当前的进展汇总','解释当前阻塞','调整优先级，先做TASK-03','恢复了吗','继续'):
            with patch('cron.jobs.get_job',side_effect=AssertionError('human interaction cannot wait on poll')):
                result=await self.plugin.on_message(self.event(text),gateway=self.gateway)
            self.assertEqual(result['action'],'rewrite')
            self.assertTrue(result['text'].endswith(text))
            self.assertIn('complex-task-coordinator',result['text'])
            self.assertIn('feishu-progress-style',result['text'])
        self.assertFalse(pending_requests(self.project))
        self.assertFalse(self.adapter.messages)

    async def test_manual_turn_does_not_consume_old_pending_requests(self):
        old=enqueue(self.project,message_id='old',chat_id='coord',user_id='user',text='已有业务指令')
        await self.plugin.on_message(self.event('当前状态'),gateway=self.gateway)
        self.assertEqual([r['request_id'] for r in pending_requests(self.project)],[old['request_id']])

    async def test_native_gateway_hook_reaches_model_path_and_preserves_event(self):
        from gateway.run_inbound import GatewayInboundMixin
        from gateway.platforms.event import MessageEvent
        original=self.event('请汇报当前进展')
        event=MessageEvent(text=original.text,source=original.source,message_id=original.message_id)
        result=await self.plugin.on_message(event,gateway=self.gateway)
        with patch('hermes_cli.lifecycle.ainvoke_hook',new=AsyncMock(return_value=[result])):
            continued=await GatewayInboundMixin._hm_pre_gateway_dispatch_hook(self.gateway,event,event.source)
        self.assertIsNotNone(continued)
        self.assertEqual(continued.auto_skill,['complex-task-coordinator','feishu-progress-style'])
        self.assertTrue(continued.text.endswith('请汇报当前进展'))
        self.assertEqual(event.message_id,'message')
        self.assertFalse(self.adapter.messages)

    async def test_manual_turn_is_not_made_readonly_or_a_second_unlocked_business_writer(self):
        result=await self.plugin.on_message(self.event('按已有授权继续'),gateway=self.gateway)
        self.assertIn('共用COORDINATOR租约',result['text'])
        self.assertIn('取得租约后重读',result['text'])
        self.assertIn('owner忙则只保存待办',result['text'])
        self.assertIn('群会话取得租约可直接接续',result['text'])
        self.assertIn('夜间用户主动问话正常答复',result['text'])
        self.assertNotIn('本轮只读业务文档',result['text'])

    async def test_unauthorized_message_cannot_read_metrics_or_add_role_context(self):
        self.gateway._is_user_authorized=lambda source:False
        with patch.object(module,'collect',side_effect=AssertionError('should not read')):
            self.assertIsNone(await self.plugin.on_message(self.event('/llm-load'),gateway=self.gateway))
            self.assertIsNone(await self.plugin.on_message(self.event('核对门禁'),gateway=self.gateway))
        self.assertFalse(pending_requests(self.project));self.assertFalse(self.adapter.messages)

    async def test_mechanical_query_shortcircuits_but_explanation_does_not(self):
        data={'observed_at':'2026-10-13T18:00:00+08:00','running':4,'waiting':3,'kv_percent':12}
        with patch.object(module,'collect',return_value=data):
            result=await self.plugin.on_message(self.event('当前大模型负载情况','a'),gateway=self.gateway)
        self.assertEqual(result['action'],'skip')
        self.assertIn('4 / 3',self.adapter.messages[-1][1])
        self.assertIsNone(await self.plugin.on_message(self.event('分析当前大模型负载','a'),gateway=self.gateway))
        result=await self.plugin.on_message(self.event('分析当前大模型负载'),gateway=self.gateway)
        self.assertEqual(result['action'],'rewrite')

    async def test_unbound_chats_keep_native_model_even_with_legacy_settings(self):
        self.plugin.load_chat_ids={'load'}
        with patch.object(module,'collect',side_effect=AssertionError('unbound chat must not collect metrics')):
            for chat in ('load','unrelated'):
                for text in ('当前大模型负载情况','/llm-load','/progress','当前状态','继续'):
                    self.assertIsNone(await self.plugin.on_message(self.event(text,chat),gateway=self.gateway))
        self.assertFalse(self.adapter.messages)
        self.assertFalse(pending_requests(self.project))

    async def test_native_approval_passes_through(self):
        self.assertIsNone(await self.plugin.on_message(self.event('/approve x'),gateway=self.gateway))

    async def test_model_control_is_immediate_and_never_calls_model_or_queue(self):
        with patch.object(self.plugin.router,'command',return_value='已切换云端') as command:
            result=await self.plugin.on_message(self.event('切换到云端模型','a'),gateway=self.gateway)
        self.assertEqual(result['action'],'skip')
        command.assert_called_once_with('cloud')
        self.assertIn('已切换云端',self.adapter.messages[-1][1])
        self.assertFalse(pending_requests(self.project))

    async def test_model_control_cannot_cross_authorization_or_group_boundary(self):
        with patch.object(self.plugin.router,'command',side_effect=AssertionError('not authorized')):
            self.assertIsNone(await self.plugin.on_message(self.event('/llm cloud','other'),gateway=self.gateway))
            self.gateway._is_user_authorized=lambda source:False
            self.assertIsNone(await self.plugin.on_message(self.event('/llm cloud'),gateway=self.gateway))

    async def test_native_busy_path_applies_control_without_waiting_or_interrupting(self):
        from gateway.run_busy import GatewayBusySessionMixin
        self.gateway._is_user_authorized_for_source=lambda source:True
        self.gateway._admit_bot_message_for_source=lambda source:True
        event=self.event('/llm cloud')
        event.allow_gateway_control=True
        async def invoke(name,**kw):
            self.assertEqual(name,'pre_gateway_busy_dispatch')
            return [await self.plugin.on_busy_message(**kw)]
        with patch('hermes_cli.lifecycle.ainvoke_hook',side_effect=invoke), \
             patch.object(self.plugin.router,'command',return_value='已切换云端') as command:
            handled=await GatewayBusySessionMixin._handle_active_session_busy_message(self.gateway,event,'busy')
        self.assertTrue(handled)
        command.assert_called_once_with('cloud')
        self.assertFalse(pending_requests(self.project))

    async def test_busy_noncontrol_stays_on_native_queue_path(self):
        self.assertIsNone(await self.plugin.on_busy_message(self.event('继续开发'),gateway=self.gateway))

    def test_routing_error_blocks_bound_role_and_leaves_other_sessions_alone(self):
        with patch.object(self.plugin,'api_identity',side_effect=lambda sid: {'role':'A'} if sid=='role' else None), \
             patch.object(self.plugin.router,'config',side_effect=ValueError('bad config')):
            self.assertIsNone(self.plugin.on_route(session_id='other'))
            self.assertIn('error',self.plugin.on_route(session_id='role'))

    async def test_explicit_progress_uses_live_projection_without_business_queue(self):
        with patch('cron.jobs.get_job',return_value={'enabled':False}), \
             patch.object(module,'progress_answer',return_value='**最新文档简报**') as reader:
            result=await self.plugin.on_message(self.event('/progress'),gateway=self.gateway)
        self.assertEqual(result['action'],'skip')
        reader.assert_called_once_with(self.project,{'enabled':False})
        self.assertEqual(self.adapter.messages[-1][1],'**最新文档简报**')
        self.assertFalse(pending_requests(self.project))

    async def test_media_and_reply_identity_are_kept_on_native_event(self):
        event=self.event('确认');event.media_urls=['/test/image.png'];event.reply_to_text='TASK-1 / attempt-2';event.reply_to_message_id='original'
        result=await self.plugin.on_message(event,gateway=self.gateway)
        self.assertEqual(result['action'],'rewrite')
        self.assertEqual(event.media_urls,['/test/image.png'])
        self.assertEqual(event.reply_to_message_id,'original')
        self.assertFalse(pending_requests(self.project))

    async def test_bad_registry_does_not_close_human_conversation(self):
        self.plugin.registry.write_text('broken')
        for chat in ('unrelated','coord'):
            self.assertIsNone(await self.plugin.on_message(self.event('正常问题',chat),gateway=self.gateway))
        self.assertFalse(pending_requests(self.project))

    def test_real_plugin_discovery_is_profile_scoped_and_registers_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp);(home/'plugins').mkdir()
            (home/'plugins/role-workflow-runtime').symlink_to(ROOT,target_is_directory=True)
            (home/'config.yaml').write_text('plugins:\n  enabled: [role-workflow-runtime]\n')
            code='from hermes_cli.plugins import discover_plugins,get_plugin_manager; discover_plugins(force=True); m=get_plugin_manager(); assert any(p.manifest.name=="role-workflow-runtime" for p in m._plugins.values()); assert m.has_hook("pre_gateway_dispatch"); assert m.has_hook("pre_api_request"); print("discovery pass")'
            env={**os.environ,'HERMES_HOME':str(home),'PYTHONPATH':str(HERMES)}
            r=subprocess.run([str(HERMES/'venv/bin/python'),'-c',code],cwd=home,env=env,capture_output=True,text=True,timeout=30)
            self.assertEqual(r.returncode,0,r.stdout+r.stderr)

    def test_native_timing_is_role_scoped_metadata_only_and_retry_safe(self):
        import time_accounting
        session='cron_native-job_20261014_140000'
        self.plugin.on_api(session_id=session,model='qwen3.8',api_request_id='req',started_at=100,
                           request={'authorization':'PRIVATE'},user_message='PRIVATE')
        self.plugin.on_api_error(session_id=session,model='qwen3.8',api_request_id='req',started_at=100,
                                 ended_at=110,retryable=True,retry_count=0,max_retries=2)
        self.plugin.on_api(session_id=session,model='qwen3.8',api_request_id='req',started_at=100)
        self.plugin.on_api_end(session_id=session,model='qwen3.8',api_request_id='req',started_at=100,ended_at=140)
        self.plugin.on_api(session_id='unrelated',model='qwen3.8',api_request_id='other',started_at=100)
        db=self.root/'runtime/role-time.db'
        with sqlite3.connect(db) as conn:
            rows=conn.execute('SELECT * FROM api_requests').fetchall()
        self.assertEqual(len(rows),1)
        self.assertNotIn('PRIVATE',str(rows))
        self.assertIn('COORDINATOR',str(rows))

    def test_native_human_and_recovered_session_attribution_uses_chat_binding(self):
        with sqlite3.connect(self.root/'state.db') as db:
            db.execute('CREATE TABLE sessions(id TEXT,chat_id TEXT,parent_session_id TEXT)')
            db.executemany('INSERT INTO sessions VALUES(?,?,?)', [('human','coord',None),('child',None,'human'),('other','normal',None),
                            ('cron-child',None,'cron_native-job_20261014_140000')])
        expected={'role':'COORDINATOR','project':str(self.project)}
        self.assertEqual(self.plugin.api_identity('human'),expected)
        self.assertEqual(self.plugin.api_identity('child'),expected)
        self.assertEqual(self.plugin.api_identity('cron-child'),expected)
        self.assertIsNone(self.plugin.api_identity('other'))
        self.plugin.registry.write_text('invalid')
        # Timing errors remain observational and never stop the user's model request.
        self.assertIsNone(self.plugin.on_api_end(session_id='human',api_request_id='r',started_at=1,ended_at=2))

if __name__=='__main__': unittest.main()
