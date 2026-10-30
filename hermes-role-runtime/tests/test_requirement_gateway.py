import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(Path.home()/'.hermes/hermes-agent')]
spec=importlib.util.spec_from_file_location('requirement_gateway_test_plugin',ROOT/'__init__.py')
module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)


class RequirementGatewayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.home=Path(temporary.name);self.project=self.home/'old';self.project.mkdir()
        environment=patch.dict(os.environ,HERMES_HOME=str(self.home));environment.start();self.addCleanup(environment.stop)
        roles={role:{'chat_id':role,'poll_job_id':'job-'+role} for role in ('COORDINATOR','A','B','C','TESTER')}
        (self.project/'ROLE-BINDINGS.json').write_text(json.dumps({'schema_version':1,'project_root':str(self.project),'control':'ACTIVE','roles':roles}))
        registry=self.home/'registry.json';registry.write_text(json.dumps({'projects':[{'project_root':str(self.project)}]}))
        self.plugin=module.RuntimePlugin(registry=registry,metrics_url='unused',home=self.home)
        self.plugin.pool_path.write_text('{}')
        with sqlite3.connect(self.home/'state.db') as db:
            db.execute('CREATE TABLE sessions(id TEXT,chat_id TEXT,parent_session_id TEXT)')
            db.executemany('INSERT INTO sessions VALUES(?,?,NULL)',[(role,role) for role in roles])
        self.answers=[]
        async def send(chat,text,**kwargs):
            self.answers.append(text);return SimpleNamespace(success=True)
        self.gateway=SimpleNamespace(_is_user_authorized=lambda source:True,
                                     _delivery_adapter_for=lambda source:SimpleNamespace(send=send))
        self.runtime=SimpleNamespace(list_requirements=Mock(return_value=[{'id':'new','title':'新需求','aliases':[]}]),
            status=Mock(return_value={'state':'ACTIVE'}),request=Mock(return_value={'state':'PENDING','requirement_id':'new'}),
            validate_role_scope=Mock())
        self.patch=patch.dict(sys.modules,requirement_runtime=self.runtime);self.patch.start();self.addCleanup(self.patch.stop)
        scope=patch.object(self.plugin,'_frozen_scope',return_value=None);scope.start();self.addCleanup(scope.stop)

    async def ingress(self,text,*,role='COORDINATOR',message='m',proven=True,busy=False):
        event=SimpleNamespace(text=text,message_id=message,internal=False,metadata={},
            source=SimpleNamespace(platform=SimpleNamespace(value='feishu'),chat_id=role,user_id='u',is_bot=False))
        primary={'schema_version':1,'platform':'feishu','chat_id':role,'user_id':'u','message_id':message,
                 'primary_text':text,'is_bot':False}
        if busy:
            with patch('gateway.native_turn_input.native_primary_input',return_value=primary if proven else {}):
                result=await self.plugin.on_busy_message(event,gateway=self.gateway)
        else:
            result=await self.plugin.on_message(event,gateway=self.gateway,native_primary_input=primary if proven else None)
        info={'trusted_gateway':True,'platform':'feishu','chat_id':role,'user_id':'u','message_id':message}
        return result,info

    async def test_plain_selection_is_readonly_for_every_role(self):
        for role in ('COORDINATOR','A','B','C','TESTER'):
            result,info=await self.ingress('切换需求 new',role=role)
            self.assertEqual(result['action'],'rewrite')
            self.plugin.on_llm(session_id=role,turn_id=role,trusted_turn_input=info)
            self.assertEqual(self.plugin.on_tool(tool_name='role_requirement_select',session_id=role,turn_id=role)['action'],'block')
        self.runtime.request.assert_not_called()

    async def test_full_allows_each_roles_business_actions_only_after_lease(self):
        for role in ('COORDINATOR','A','B','C','TESTER'):
            _,info=await self.ingress('本轮授予全部权限：处理已派任务',role=role)
            self.plugin.on_llm(session_id=role,turn_id='full',trusted_turn_input=info)
            self.assertIsNone(self.plugin.on_tool(tool_name='role_manual_lease',session_id=role,turn_id='full'))
            with patch.object(module.manual_business,'owns',return_value=True):
                self.assertIsNone(self.plugin.on_tool(tool_name='terminal',session_id=role,turn_id='full'))
            self.plugin.on_turn_end(session_id=role,turn_id='full')
            self.assertEqual(self.plugin.on_tool(tool_name='terminal',session_id=role,turn_id='next')['action'],'block')

    async def test_trusted_direct_selection_is_only_control_request(self):
        with patch.object(module.manual_business,'control') as claim:
            result,_=await self.ingress('本轮授予全部权限：切换需求 new')
            self.assertEqual(result['reason'],'role-runtime-requirement-selection')
            claim.assert_not_called()
        args,kwargs=self.runtime.request.call_args
        self.assertEqual(args[1],'new');self.assertTrue(args[2]['current_full']);self.assertTrue(kwargs['trusted_ingress'])
        self.assertFalse((self.project/'inbox').exists())

    async def test_busy_direct_selection_persists_without_steering_old_turn(self):
        result,_=await self.ingress('本轮授予全部权限：切换需求 new',busy=True)
        self.assertEqual(result['action'],'skip');self.runtime.request.assert_called_once()
        self.assertEqual(self.plugin.manual_sources,{})

    async def test_missing_primary_receipt_cannot_select_even_when_busy(self):
        result,_=await self.ingress('本轮授予全部权限：切换需求 new',busy=True,proven=False)
        self.runtime.request.assert_not_called()
        self.assertNotEqual(result.get('reason'),'role-runtime-requirement-selection')

    async def test_ordinary_busy_question_stays_in_native_fifo(self):
        result,_=await self.ingress('现在做哪个需求？',busy=True)
        self.assertEqual(result['action'],'queue');self.runtime.request.assert_not_called()

    async def test_selection_tool_requires_exact_authorized_coordinator_turn(self):
        _,info=await self.ingress('本轮授予全部权限：请按目录开展新需求 new')
        self.plugin.on_llm(session_id='COORDINATOR',turn_id='full',trusted_turn_input=info)
        self.assertIsNone(self.plugin.on_tool(tool_name='role_requirement_select',session_id='COORDINATOR',turn_id='full'))
        result=json.loads(self.plugin.selection_tool({'target':'new'},session_id='COORDINATOR',turn_id='full'))
        self.assertEqual(result['state'],'PENDING')
        self.plugin.on_turn_end(session_id='COORDINATOR',turn_id='full')
        with self.assertRaises(ValueError):self.plugin.selection_tool({'target':'new'},session_id='COORDINATOR',turn_id='next')
        _,executor=await self.ingress('全部权限：处理 new',role='A')
        self.plugin.on_llm(session_id='A',turn_id='full',trusted_turn_input=executor)
        with self.assertRaises(ValueError):self.plugin.selection_tool({'target':'new'},session_id='A',turn_id='full')

    async def test_selection_error_is_explicit_and_never_falls_through(self):
        self.runtime.request.side_effect=ValueError('ambiguous')
        result,_=await self.ingress('全部权限：切换需求 new')
        self.assertEqual(result['reason'],'role-runtime-requirement-selection-error')
        self.assertIn('未确认成功',self.answers[-1])

    def test_background_keeps_scope_authority_without_human_grant(self):
        sid='cron_job-COORDINATOR_20261019_110000'
        self.assertIsNone(self.plugin.on_tool(tool_name='terminal',session_id=sid,turn_id='cron'))
        self.assertIn('无需用户每轮',self.plugin.on_llm(session_id=sid,turn_id='cron'))

    def test_frozen_old_scope_is_attributed_and_stale_business_write_blocked(self):
        self.plugin._frozen_scope.return_value={'role':'A','project_root':'/old','pool_path':str(self.plugin.pool_path),
                                               'generation':2,'kind':'business'}
        self.assertEqual(self.plugin.api_attribution_details('cron_job-A_20261019_110000')['project'],'/old')
        self.runtime.validate_role_scope.side_effect=ValueError('stale')
        decision=self.plugin.on_tool(tool_name='terminal',session_id='cron_job-A_20261019_110000',turn_id='old')
        self.assertEqual(decision['action'],'block')


if __name__=='__main__':unittest.main()
