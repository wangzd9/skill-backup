import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
HERMES=Path.home()/'.hermes/hermes-agent'
sys.path[:0]=[str(ROOT),str(HERMES)]
spec=importlib.util.spec_from_file_location('manual_gateway_test',ROOT/'__init__.py')
module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
import manual_business


class ManualGatewayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name); self.project=self.home/'project';self.project.mkdir()
        roles={role:{'chat_id':role,'poll_job_id':'job-'+role} for role in ('COORDINATOR','A','B','C','TESTER')}
        (self.project/'ROLE-BINDINGS.json').write_text(json.dumps({'schema_version':1,'project_root':str(self.project),'control':'ACTIVE','roles':roles}))
        registry=self.home/'registry.json';registry.write_text(json.dumps({'projects':[{'project_root':str(self.project)}]}))
        self.plugin=module.RuntimePlugin(registry=registry,metrics_url='unused',home=self.home)
        with sqlite3.connect(self.home/'state.db') as db:
            db.execute('CREATE TABLE sessions(id TEXT,chat_id TEXT,parent_session_id TEXT)')
            db.executemany('INSERT INTO sessions VALUES(?,?,NULL)',[(role,role) for role in roles])
        self.gateway=SimpleNamespace(_is_user_authorized=lambda source:True)

    async def request(self, text, *, role='A', message='m', trusted=True):
        event=SimpleNamespace(text=text,message_id=message,internal=False,
            source=SimpleNamespace(platform=SimpleNamespace(value='feishu'),chat_id=role,user_id='u',is_bot=False))
        primary={'schema_version':1,'platform':'feishu','chat_id':role,'user_id':'u','message_id':message,'primary_text':text,'is_bot':False}
        result=await self.plugin.on_message(event,gateway=self.gateway,native_primary_input=primary if trusted else None)
        info={'schema_version':1,'platform':'feishu','chat_id':role,'user_id':'u','message_id':message,'trusted_gateway':True,'session_kind':'gateway'}
        return result,info

    def tool(self, role='A',turn='t',tool='terminal',info=None):
        return self.plugin.on_tool(session_id=role,turn_id=turn,tool_name=tool,trusted_turn_input=info)

    async def test_all_five_roles_questions_and_continue_are_readonly_without_business_writes(self):
        for role in ('COORDINATOR','A','B','C','TESTER'):
            result,info=await self.request('继续，帮我修一下',role=role)
            self.assertIn('默认只读',result['text'])
            self.plugin.on_llm(session_id=role,turn_id='t',trusted_turn_input=info)
            for tool in ('terminal','write_file','patch','execute_code','delegate_task','send_message','role_manual_lease','unknown'):
                self.assertEqual(self.tool(role=role,tool=tool,info=info)['action'],'block')
            for tool in ('read_file','skill_view','tool_search','tool_describe','role_live_progress'):
                self.assertIsNone(self.tool(role=role,tool=tool,info=info))
            self.plugin.on_turn_end(session_id=role,turn_id='t')
        self.assertFalse((self.project/'inbox').exists())

    async def test_full_turn_needs_shared_lease_then_next_plain_message_resets(self):
        _,info=await self.request('本轮授予全部权限：执行已派任务')
        self.plugin.on_llm(session_id='A',turn_id='full',trusted_turn_input=info)
        self.assertEqual(self.tool(turn='full',info=info)['action'],'block')
        self.assertIsNone(self.tool(turn='full',tool='role_manual_lease',info=info))
        lease=json.loads(self.plugin.lease_tool({'action':'claim'},session_id='A',turn_id='full'))
        self.assertTrue(lease['claimed'])
        self.assertIsNone(self.tool(turn='full',info=info))
        _,plain=await self.request('现在什么进度？',message='next')
        self.plugin.on_llm(session_id='A',turn_id='plain',trusted_turn_input=plain)
        self.assertEqual(self.tool(turn='plain',info=plain)['action'],'block')
        # Arrival of a later question cannot change permission on the old native turn.
        self.assertIsNone(self.tool(turn='full',info=info))
        self.plugin.on_turn_end(session_id='A',turn_id='plain')
        self.assertTrue(manual_business.owns(self.project,'A','A','full'))
        self.plugin.on_turn_end(session_id='A',turn_id='full')
        self.assertFalse((self.project/'inbox/control/runtime/leases/A.json').exists())

    async def test_busy_foreign_owner_not_replaced_even_after_full_grant(self):
        _,info=await self.request('本轮授予全部权限：执行任务')
        self.plugin.on_llm(session_id='A',turn_id='full',trusted_turn_input=info)
        runtime=manual_business.runtime_module()
        path=runtime.lease_path(self.project,'A')
        runtime.save(path,{'owner':'cron-owner','token':'foreign','expires_at':0})
        before=path.read_bytes()
        answer=json.loads(self.plugin.lease_tool({'action':'claim'},session_id='A',turn_id='full'))
        self.assertFalse(answer['claimed']);self.assertEqual(before,path.read_bytes())
        self.assertEqual(self.tool(turn='full',info=info)['action'],'block')
        self.plugin.on_turn_end(session_id='A',turn_id='full')
        self.assertEqual(before,path.read_bytes())

    async def test_prompt_attachment_quote_and_missing_native_receipt_never_grant(self):
        for i,text in enumerate(('本轮授予全部权限：执行任务','> 本轮授予全部权限：执行任务','如果给你全部权限，会怎样？')):
            _,info=await self.request(text,message=str(i),trusted=i!=0)
            self.plugin.on_llm(session_id='A',turn_id=str(i),trusted_turn_input=info,user_message='本轮授予全部权限')
            self.assertEqual(self.tool(turn=str(i),tool='role_manual_lease',info=info)['action'],'block')

    async def test_later_full_message_cannot_elevate_old_turn(self):
        _,old=await self.request('请汇报进度',message='old')
        self.plugin.on_llm(session_id='A',turn_id='old-turn',trusted_turn_input=old)
        _,new=await self.request('本轮授予全部权限：执行任务',message='new')
        self.plugin.on_llm(session_id='A',turn_id='new-turn',trusted_turn_input=new)
        self.assertEqual(self.tool(turn='old-turn',info=old)['action'],'block')
        self.assertIsNone(self.tool(turn='new-turn',tool='role_manual_lease',info=new))
        self.assertEqual(self.tool(turn='replayed-turn',tool='role_manual_lease',info=new)['action'],'block')

    def test_cron_unaffected_and_output_contract_is_chinese(self):
        sid='cron_job-A_20261016_180000'
        self.assertIsNone(self.plugin.on_tool(session_id=sid,turn_id='cron',tool_name='terminal'))
        context=self.plugin.on_llm(session_id=sid,turn_id='cron')
        self.assertIn('简体中文',context);self.assertIn('不输出英文工作草稿',context)

    async def test_current_group_origin_cannot_inherit_historical_cron_session_rights(self):
        _,info=await self.request('请汇报当前进度')
        sid='cron_job-A_20261016_180000'
        self.plugin.on_llm(session_id=sid,turn_id='restored',trusted_turn_input=info)
        self.assertEqual(self.plugin.on_tool(session_id=sid,turn_id='restored',tool_name='terminal',trusted_turn_input=info)['action'],'block')
        with patch.object(module,'live_progress_tool_packet',return_value={'fresh':True}) as reader:
            self.plugin.live_tool({},session_id=sid,turn_id='restored')
            reader.assert_called_once_with(str(self.project),'A')

    async def test_native_and_database_role_conflict_blocks_without_grant(self):
        _,info=await self.request('本轮授予全部权限：执行任务',role='B')
        self.plugin.on_llm(session_id='A',turn_id='conflict',trusted_turn_input=info)
        self.assertEqual(self.plugin.on_tool(session_id='A',turn_id='conflict',tool_name='role_manual_lease',trusted_turn_input=info)['action'],'block')
        with self.assertRaises(ValueError):self.plugin.lease_tool({'action':'claim'},session_id='A',turn_id='conflict')
        self.assertFalse((self.project/'inbox').exists())

    async def test_native_business_slash_cannot_bypass_default_readonly(self):
        self.gateway._delivery_adapter_for=lambda source:SimpleNamespace(send=self.fake_send)
        for text in ('/cron run business','/approve x','/model cloud','/skills install example'):
            result,_=await self.request(text,message=text)
            self.assertEqual(result['action'],'skip')
        self.assertFalse((self.project/'inbox').exists())

    def test_live_tool_uses_native_binding_and_rejects_model_selected_scope(self):
        with patch.object(module,'live_progress_tool_packet',return_value={'fresh':True}) as reader:
            self.assertTrue(json.loads(self.plugin.live_tool({},session_id='A'))['fresh'])
            reader.assert_called_once_with(str(self.project),'A')
            with self.assertRaises(ValueError):self.plugin.live_tool({'role':'B'},session_id='A')
            with self.assertRaises(ValueError):self.plugin.live_tool({},session_id='unknown')

    async def test_coordinator_manual_progress_prompt_requires_all_seven_sections(self):
        identity={'role':'COORDINATOR','project':str(self.project),'session_kind':'group_chat'}
        with patch.object(self.plugin,'bind_manual_turn',return_value=identity), \
             patch.object(module.workflow,'active',return_value=True):
            context=self.plugin.on_llm(session_id='coord',turn_id='progress')
        for heading in ('整体目标与进度','执行者进度','测试者进度','OCR情况和后续计划',
                        '强审查情况','风险','下一步计划'):
            self.assertIn('**'+heading+'**',context)
        self.assertIn('不得只挑少数主题',context)
        self.assertIn('不复述错误码、预算、字节数',context)
        self.assertIn('每条正文都要把字段名',context)

    def test_executor_and_tester_manual_progress_contract_stays_role_scoped(self):
        for role in ('A','B','C','TESTER'):
            text=module._manual_progress_instruction(role)
            for heading in ('当前工作','本次完成','阻塞与依赖','下一步计划'):
                self.assertIn('**'+heading+'**',text)
            self.assertNotIn('**整体目标与进度**',text)

    def test_direct_progress_intent_does_not_capture_questions_about_report_style(self):
        recognizes=module._manual_progress_request
        for role in ('COORDINATOR','A','TESTER'):
            self.assertTrue(recognizes(role,'请汇报当前进展'))
            self.assertTrue(recognizes(role,'现在什么进度？'))
        self.assertFalse(recognizes('COORDINATOR','为什么协调者汇报进展时会混入英文？'))
        self.assertFalse(recognizes('A','请帮我修改汇报模板'))

    def test_invalid_manual_progress_is_replaced_by_fresh_structured_readonly_facts(self):
        packet={'role':'COORDINATOR','checked_at':'2026-10-29T20:00:00+08:00',
            'run':{'requirement_id':'requirement-1'},
            'tasks':[{'task_id':'TASK-1','role':'A','phase':'DEVELOPING','attempt':2}],
            'roles':[{'role':role,'control':'ACTIVE','current_work_id':None,'writer_execution_id':None}
                     for role in ('COORDINATOR','A','B','C','TESTER')],
            'ocr_serial_gate':{'blocked':False,'blocking_count':0},
            'ocr_version_gate':{'state':'OPEN'},'open_gates':[],'model_admission':{}}
        identity={'role':'COORDINATOR','project':str(self.project),'session_kind':'group_chat'}
        self.plugin.manual_turns[('coord','bad-progress')]=identity
        self.plugin.manual_progress_turns[('coord','bad-progress')]='COORDINATOR'
        with patch.object(module.workflow,'active',return_value=True), \
             patch('workflow_query.live_progress',return_value=packet):
            result=self.plugin.transform_manual_progress_output(
                'r65 checkpoint committed. This poll has no business change.\n[SILENT]',
                session_id='coord',turn_id='bad-progress')
        for heading in ('整体目标与进度','执行者进度','测试者进度','OCR情况和后续计划',
                        '强审查情况','风险','下一步计划'):
            self.assertIn('**'+heading+'**',result)
        self.assertNotIn('[SILENT]',result)
        self.assertIn('TASK-1 attempt 2 开发中',result)
        self.assertTrue(all(not line.startswith('- ') or line.startswith('- **') for line in result.splitlines()))

    def test_final_reply_hook_leaves_ordinary_questions_untouched(self):
        identity={'role':'COORDINATOR','project':str(self.project),'session_kind':'group_chat'}
        self.plugin.manual_turns[('coord','ordinary')]=identity
        self.assertIsNone(self.plugin.transform_manual_progress_output(
            'plain answer',session_id='coord',turn_id='ordinary'))

    def test_unavailable_live_query_reports_distinct_gaps_without_claiming_business_states(self):
        from workflow_reports import _progress_report_format_error
        for role in ('COORDINATOR','A','B','C','TESTER'):
            identity={'role':role,'project':str(self.project),'session_kind':'group_chat'}
            key=(role,'unavailable-progress')
            self.plugin.manual_turns[key]=identity
            self.plugin.manual_progress_turns[key]=role
            with patch.object(module.workflow,'active',return_value=True), \
                 patch('workflow_query.live_progress',side_effect=RuntimeError('internal-query-detail')):
                result=self.plugin.transform_manual_progress_output(
                    '只读工具仍不可用，直接读最新分页。',session_id=key[0],turn_id=key[1])
            self.assertIsNone(_progress_report_format_error(result,role))
            bullets=[line for line in result.splitlines() if line.startswith('- ')]
            self.assertEqual(len(bullets),len(set(bullets)))
            self.assertNotIn('internal-query-detail',result)
            self.assertNotIn('当次只读核验未完成，相关事实待核实',result)
            self.assertIn('本次查询不改变业务调度',result)

    def test_live_fallback_with_retained_failed_verifications_and_gates_passes_its_own_contract(self):
        from workflow_reports import render_live_progress_fallback, _progress_report_format_error
        packet={'checked_at':'2026-10-30T09:30:00+08:00',
            'run':{'requirement_id':'requirement-1'},
            'tasks':[{'task_id':'MN-01','role':'A','phase':'COMPLETED','attempt':7}],
            'roles':[{'role':role,'control':'ACTIVE','current_work_id':None,'writer_execution_id':None}
                     for role in ('COORDINATOR','A','B','C','TESTER')],
            'ocr_serial_gate':{'blocked':False,'blocking_count':0},
            'ocr_version_gate':{'state':'OPEN'},
            'open_gates':[{'code':'TASK_UNRESOLVED','task_id':'MN-04'}],
            'supplemental_verifications':[
                {'task_id':'MN-01','state':'ENDED','result':'FAIL'},
                {'task_id':'MN-04','state':'ENDED','result':'NOT_VERIFIED'}]}
        for role in ('COORDINATOR','A','TESTER'):
            report=render_live_progress_fallback(packet,role)
            self.assertIsNone(_progress_report_format_error(report,role),(role,report))
            self.assertIn('TASK_UNRESOLVED',report)
        report=render_live_progress_fallback(packet,'COORDINATOR')
        self.assertIn('MN-01 已结束 / 失败',report)
        self.assertIn('MN-04 已结束 / 未验证',report)

    async def test_active_coordinator_manual_progress_prompt_names_all_seven_sections(self):
        with patch.object(module.workflow,'active',return_value=True):
            result,_=await self.request('请汇报当前整体进展',role='COORDINATOR')
        self.assertEqual('role-runtime-native-role',result['reason'])
        for heading in ('整体目标与进度','执行者进度','测试者进度','OCR情况和后续计划',
                        '强审查情况','风险','下一步计划'):
            self.assertIn('**'+heading+'**',result['text'])
        self.assertIn('工具报错时仍按角色完整分区',result['text'])

    async def test_model_change_requires_current_trusted_primary_full_request(self):
        self.gateway._delivery_adapter_for=lambda source:SimpleNamespace(send=self.fake_send)
        with patch.object(self.plugin.router,'command',return_value='已切换云端') as command:
            result,_=await self.request('本轮授予全部权限：切换到云端模型')
            self.assertEqual(result['action'],'skip');command.assert_called_once_with('cloud')

    async def fake_send(self,*args,**kwargs):return SimpleNamespace(success=True)

    async def test_reload_discards_permission_and_end_only_cleans_exact_owned_lease(self):
        _,info=await self.request('本轮授予全部权限：执行任务')
        self.plugin.on_llm(session_id='A',turn_id='full',trusted_turn_input=info)
        self.plugin.lease_tool({'action':'claim'},session_id='A',turn_id='full')
        fresh=module.RuntimePlugin(registry=self.plugin.registry,metrics_url='unused',home=self.home)
        self.assertEqual(fresh.on_tool(session_id='A',turn_id='full',tool_name='terminal')['action'],'block')
        fresh.on_turn_end(session_id='A',turn_id='full')
        self.assertFalse((self.project/'inbox/control/runtime/leases/A.json').exists())

    async def test_mysql_native_instruction_is_persisted_before_model_and_busy_owner_is_not_claimed(self):
        self.plugin.native_ready=True
        text='MN-01 迁移 MySQL 双向补验归属 TESTER 隔离 MySQL 会话'
        with patch.object(module.workflow,'active',return_value=True), \
             patch.object(module.workflow,'submit_control',return_value={'status':'COMMITTED','work_id':'queued'}) as submit, \
             patch.object(module.workflow,'request_role_wakeup',return_value={'status':'SUBMITTED'}) as wake:
            result,_=await self.request(text,role='COORDINATOR',message='native')
            self.assertEqual('role-runtime-supplemental-request',result['reason'])
            self.assertIn('queued',result['text'])
            self.assertEqual('request_supplemental_verification',submit.call_args.args[2])
            self.assertFalse(submit.call_args.args[4]['current_full'])
            self.assertEqual('native',submit.call_args.args[4]['message_id'])
            wake.assert_called_once()
            submit.reset_mock();wake.reset_mock()
            await self.request(text,role='COORDINATOR',message='untrusted',trusted=False)
            submit.assert_not_called();wake.assert_not_called()
        self.assertFalse((self.project/'inbox').exists())

    async def test_short_coordinator_decision_is_recorded_before_model_and_wakes_owner(self):
        self.plugin.native_ready=True
        with patch.object(module.workflow,'active',return_value=True), \
             patch.object(module.workflow,'submit_control',return_value={'status':'COMMITTED','request_id':'decision-1'}) as submit, \
             patch.object(module.workflow,'request_role_wakeup',return_value={'status':'SUBMITTED'}) as wake:
            result,_=await self.request('按解读b',role='COORDINATOR',message='decision')
            self.assertEqual('role-runtime-user-decision',result['reason'])
            self.assertIn('COMMITTED',result['text'])
            self.assertEqual('record_user_decision',submit.call_args.args[2])
            self.assertEqual({},submit.call_args.args[3])
            self.assertFalse(submit.call_args.args[4]['current_full'])
            self.assertEqual('按解读b',submit.call_args.args[4]['primary_text'])
            wake.assert_called_once()

            submit.reset_mock();wake.reset_mock()
            await self.request('按解读b',role='COORDINATOR',message='untrusted',trusted=False)
            submit.assert_not_called();wake.assert_not_called()

            submit.reset_mock();wake.reset_mock()
            await self.request('按解读b',role='A',message='wrong-role')
            submit.assert_not_called();wake.assert_not_called()

    async def test_explicit_close_ruling_without_decision_prefix_is_recorded_from_native_message(self):
        self.plugin.native_ready=True
        text=('请对 work-224df688 执行 close_verification，结果记录为 UNVERIFIED / '
              'TEST_RESULT_NONDETERMINISTIC，保留全部证据，不再重跑；释放后先完成 26cc19d '
              'independent_test，结果落账前不要 APPROVE 或合入。')
        with patch.object(module.workflow,'active',return_value=True), \
             patch.object(module.workflow,'submit_control',return_value={'status':'COMMITTED','request_id':'close-1'}) as submit, \
             patch.object(module.workflow,'request_role_wakeup',return_value={'status':'SUBMITTED'}):
            result,_=await self.request(text,role='COORDINATOR',message='close-ruling')
        self.assertEqual('role-runtime-user-decision',result['reason'])
        self.assertEqual('record_user_decision',submit.call_args.args[2])
        self.assertEqual({},submit.call_args.args[3])
        self.assertFalse(submit.call_args.args[4]['current_full'])
        self.assertEqual(text,submit.call_args.args[4]['primary_text'])

if __name__=='__main__':unittest.main()
