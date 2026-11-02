"""真实隔离账本覆盖漏报、回执、静默、输入预算和无效补审；不访问真实群。"""
import copy
import importlib.util
from datetime import datetime
import json
from pathlib import Path
import sys
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,'/home/wang/.hermes/hermes-agent')
from tests.test_business_ledger import LedgerFixture
from tests.test_workflow_services import WorkflowServiceTests, ExternalService
from workflow_reports import transform_coordinator_notice, acknowledge_native_report
from workflow_notifications import drain_business_notifications
from workflow_views import fit_report, size


COORDINATOR_REPORT = '''**协调者进展 · 2026-10-29 10:00 +08:00**

**整体目标与进度**
- **进度：** 本轮审查中断，业务门禁保持。

**执行者进度**
- **执行者 A（工作中）：** 按原范围处理返修。

**测试者进度**
- **测试者（等待）：** 等待固定候选测试结果。

**OCR情况和后续计划**
- **OCR状态：** 当前没有新增OCR结论。

**强审查情况**
- **强审查状态：** 尚无新的强审查结果。

**风险**
- **风险：** 未确认的门禁继续保留。

**下一步计划**
- **下一步：** 协调者核对证据后推进原计划。'''


class CoordinatorNoticeTests(LedgerFixture):
    def setUp(self):
        super().setUp()
        self.pool=self.root/'isolated.json'
        self.pool.write_text(json.dumps({'roles':{'COORDINATOR':{'chat_id':'isolated-coordinator'}}}))
        patch=mock.patch('workflow_runtime.open_ledger',return_value=self.ledger)
        patch.start();self.addCleanup(patch.stop)
        self.scope={'kind':'business','workflow_version':1,'pool_path':str(self.pool),
                    'role':'COORDINATOR','execution_id':'isolated-business','run_id':'run-one'}

    def test_meaningful_final_enters_outbox_once_and_sends_with_message_receipt(self):
        before=self.ledger.snapshot()['event_high_water']
        for _ in range(2):
            result=transform_coordinator_notice(self.scope,'isolated-business',
                COORDINATOR_REPORT,self.root,
                kind='COORDINATOR_PROGRESS')
            self.assertTrue(result['suppress'])
            self.assertEqual('BUSINESS_PROGRESS_QUEUED',result['metadata']['reason'])
        calls=[]
        def transport(row):
            calls.append(row)
            return {'delivered':True,'message_id':'isolated-confirmed-message'}
        for _ in range(2):drain_business_notifications(self.pool,transport=transport)
        self.assertEqual(1,len(calls))
        notice=self.ledger.outbox_items(include_terminal=True)[0]
        self.assertEqual('SENT',notice['state'])
        self.assertEqual('isolated-confirmed-message',notice['receipt']['message_id'])
        self.assertEqual(before,self.ledger.snapshot()['event_high_water'])

    def test_identical_report_in_another_execution_is_not_queued_again(self):
        text=COORDINATOR_REPORT
        transform_coordinator_notice(self.scope,'isolated-business',text,self.root,kind='COORDINATOR_PROGRESS')
        other={**self.scope,'execution_id':'another-execution'}
        result=transform_coordinator_notice(other,'another-execution',text,self.root,kind='COORDINATOR_PROGRESS')
        self.assertEqual('DUPLICATE_COORDINATOR_PROGRESS',result['metadata']['reason'])
        self.assertEqual(1,len(self.ledger.outbox_items(include_terminal=True)))

    def test_native_plugin_routes_frozen_business_and_no_agent_report(self):
        spec=importlib.util.spec_from_file_location('coordinator_reporting_plugin_test',
            Path(__file__).resolve().parents[1]/'__init__.py')
        module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
        plugin=module.RuntimePlugin(registry=self.root/'registry',home=self.root,
            metrics_url='http://unused',pool_path=self.pool)
        with mock.patch.object(plugin,'_frozen_scope',return_value=self.scope):
            result=plugin.transform_cron_report(job={'role_pool':{'kind':'business','role':'COORDINATOR'}},
                execution_id='isolated-business',agent_identity={'session_id':'bound'},final_response=COORDINATOR_REPORT)
        self.assertEqual('BUSINESS_PROGRESS_QUEUED',result['metadata']['reason'])
        folder=self.root/'runtime/role-pool-executions';folder.mkdir(parents=True)
        (folder/'isolated-brief.json').write_text(json.dumps(
            {**self.scope,'kind':'report','execution_id':'isolated-brief'}))
        result=plugin.transform_cron_report(job={'no_agent':True,
            'role_pool':{'kind':'report','role':'COORDINATOR'}},execution_id='isolated-brief',
            final_response='A：空闲')
        self.assertFalse(result['suppress'])
        self.assertTrue(result['metadata']['claimed'])
        self.assertIn('当前需求：requirement',result['content'])
        missing=plugin.transform_cron_report(job={'no_agent':True,
            'role_pool':{'kind':'report','role':'COORDINATOR'}},execution_id='missing',final_response='bad')
        self.assertEqual('REPORT_SCOPE_UNVERIFIED',missing['metadata']['reason'])
        with mock.patch.object(plugin,'_frozen_scope',return_value={**self.scope,
                'kind':'report','execution_id':'failed-morning'}):
            failed=plugin.transform_cron_report(job={'role_pool':{'kind':'report','role':'COORDINATOR'}},
                execution_id='failed-morning',agent_identity={'session_id':'morning'},success=False,
                final_response='不可信原始错误正文')
        self.assertFalse(failed['suppress'])
        self.assertIn('报告失败',failed['content'])
        self.assertNotIn('不可信原始错误正文',failed['content'])
        self.assertTrue(failed['metadata']['claimed'])
        (folder/'failed-brief.json').write_text(json.dumps(
            {**self.scope,'kind':'report','execution_id':'failed-brief'}))
        script_failure=plugin.transform_cron_report(job={'no_agent':True,
            'role_pool':{'kind':'report','role':'COORDINATOR'}},execution_id='failed-brief',
            final_response='不可信原始脚本输出',success=False)
        self.assertFalse(script_failure['suppress'])
        self.assertIn('报告失败',script_failure['content'])
        self.assertNotIn('不可信原始脚本输出',script_failure['content'])
        (folder/'broken.json').write_text('invalid-json')
        result=plugin.transform_cron_report(job={'no_agent':True,
            'role_pool':{'kind':'report','role':'COORDINATOR'}},execution_id='broken',final_response='bad')
        self.assertEqual('COORDINATOR_NOTICE_UNVERIFIED',result['metadata']['reason'])

    def test_silent_and_mismatched_execution_cannot_create_notifications(self):
        for text,identifier in [('[SILENT]','isolated-business'),('有意义的报告','wrong-execution')]:
            result=transform_coordinator_notice(self.scope,identifier,text,self.root,kind='COORDINATOR_PROGRESS')
            self.assertTrue(result['suppress'])
        self.assertFalse(self.ledger.outbox_items(include_terminal=True))

    def test_night_progress_is_retained_but_never_sent(self):
        self.now=datetime(2026,10,19,22,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        transform_coordinator_notice(self.scope,'isolated-business',COORDINATOR_REPORT,self.root,
                                     kind='COORDINATOR_PROGRESS')
        drain_business_notifications(self.pool,transport=lambda _:self.fail('夜间不得发送'))
        self.assertEqual('SUPPRESSED',self.ledger.outbox_items(include_terminal=True)[0]['state'])

    def test_suppressed_night_notice_does_not_hide_new_daytime_progress(self):
        self.now=datetime(2026,10,19,22,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        text=COORDINATOR_REPORT
        transform_coordinator_notice(self.scope,'isolated-business',text,self.root,kind='COORDINATOR_PROGRESS')
        self.now=datetime(2026,10,20,8,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        scope={**self.scope,'execution_id':'morning-business'}
        transform_coordinator_notice(scope,'morning-business',text,self.root,kind='COORDINATOR_PROGRESS')
        rows=self.ledger.outbox_items(include_terminal=True)
        self.assertEqual({'SUPPRESSED','PENDING'},{r['state'] for r in rows})

    def test_english_internal_poll_and_mixed_silent_report_are_suppressed(self):
        english=('r65 checkpoint committed. No new business fact and no action this poll.\n[SILENT]')
        scope={**self.scope,'execution_id':'english-business'}
        result=transform_coordinator_notice(scope,'english-business',english,self.root,
                                             kind='COORDINATOR_PROGRESS')
        self.assertTrue(result['suppress'])
        self.assertEqual('SILENT_MARKER_MIXED_WITH_TEXT',result['metadata']['reason'])
        self.assertFalse(self.ledger.outbox_items(include_terminal=True))

    def test_unstructured_chinese_progress_uses_fresh_structured_fallback(self):
        scope={**self.scope,'execution_id':'short-business'}
        result=transform_coordinator_notice(scope,'short-business',
                                             '说明：role_live_progress 持续报 QUERY_METADATA_EXCEEDS_BYTE_BUDGET（查询视图约99KB超预算，工具无参数可调），本轮以最新checkpoint为准。',self.root,
                                             kind='COORDINATOR_PROGRESS')
        self.assertTrue(result['suppress'])
        self.assertEqual('BUSINESS_PROGRESS_QUEUED',result['metadata']['reason'])
        queued=self.ledger.outbox_items(include_terminal=True)[0]
        for heading in ('整体目标与进度','执行者进度','测试者进度','OCR情况和后续计划',
                        '强审查情况','风险','下一步计划'):
            self.assertIn('**'+heading+'**',queued['payload']['text'])
        self.assertNotIn('QUERY_METADATA_EXCEEDS_BYTE_BUDGET',queued['payload']['text'])
        self.assertNotIn('99KB',queued['payload']['text'])

    def test_fallback_reads_business_content_with_history_dates_and_verified_hashes(self):
        from workflow_query import _artifact
        from workflow_reports import render_live_progress_fallback, _coordinator_progress_format_error
        context = {'run_id': 'run-one', 'event_high_water': 88,
            'tasks': {'T1': {'description': '已交付节点分配事务与并发回归。'}},
            'roles': {'A': {'recorded_at': '2026-10-30T13:00:00+08:00', 'event_id': 87,
                'summary': '已定位分配并发问题。', 'next': '取得独立验证结果后交付候选。'}},
            'completion': {'head': 'a'*40, 'event_id': 86, 'result': 'FAIL',
                'summary': '专项回归已通过，容量并发存在待处理项。',
                'issues': [{'id': 'CODE-01', 'task_id': 'T1', 'summary': '容量并发出现间歇死锁，需要原负责者返修。'}]}}
        ref = _artifact(self.ledger, 'fixture-report-context', context)
        manifest = _artifact(self.ledger, 'fixture-manifest', {'report_context': ref})
        packet = {'checked_at': '2026-10-30T14:00:00+08:00', 'event_high_water': 88,
            'run': {'run_id': 'run-one', 'requirement_id': 'isolated-fixture'},
            'tasks': [{'task_id': 'T1', 'role': 'A', 'phase': 'DEVELOPING', 'attempt': 2}],
            'roles': [], 'artifacts': {'manifest': manifest}, 'completion': {'state': 'REPAIRING'},
            'external_actions': [{'kind':'ocr','run_id':'other-run','task_id':'FOREIGN-TASK',
                                  'action_id':'foreign','state':'INCOMPLETE'}]}
        text = render_live_progress_fallback(packet, 'COORDINATOR')
        for expected in ('节点分配事务', 'CODE-01', '间歇死锁', '取得独立测试结果', '2026-10-30T13:00'):
            self.assertIn(expected, text)
        self.assertNotIn('event:', text)
        self.assertIsNone(_coordinator_progress_format_error(text))
        self.assertNotIn('FOREIGN-TASK', text)
        Path(ref['path']).write_text('{}')
        damaged = render_live_progress_fallback(packet, 'COORDINATOR')
        self.assertNotIn('间歇死锁', damaged)
        self.assertNotIn('节点分配事务', damaged)

    def test_periodic_brief_keeps_original_outlet_and_requires_matching_content_receipt(self):
        scope={**self.scope,'kind':'report'}
        text='A：空闲\nB：等待\nC：阻塞\nTESTER：待核实'
        result=transform_coordinator_notice(scope,'isolated-business',text,self.root,kind='STATUS_BRIEF')
        self.assertFalse(result['suppress'])
        self.assertIn('协调者 · 定时状态',result['content'])
        self.assertIn('当前需求：requirement',result['content'])
        drain_business_notifications(self.pool,transport=lambda _:self.fail('短报不得从事件通道双发'))
        metadata=result['metadata']
        unknown=acknowledge_native_report(metadata,execution_id='isolated-business',outcome='delivered',
            attempted=True,content_sha256='wrong',home=self.root)
        self.assertEqual('UNKNOWN',unknown['state'])
        actual=acknowledge_native_report(metadata,execution_id='isolated-business',outcome='delivered',
            attempted=True,content_sha256=metadata['rendered_sha256'],home=self.root,
            stage='native_queue_terminal')
        self.assertEqual('SENT',actual['state'])
        self.assertTrue(transform_coordinator_notice(scope,'isolated-business',text,self.root,
                                                   kind='STATUS_BRIEF')['suppress'])

    def test_native_failure_notice_has_reason_even_without_input_artifact(self):
        scope={**self.scope,'kind':'report'}
        result=transform_coordinator_notice(scope,'isolated-business','本次定时报告执行失败。',
            self.root,kind='REPORT_FAILURE')
        self.assertFalse(result['suppress'])
        generation=self.ledger.notification(result['metadata']['notification_id'])['payload']['generation']
        self.assertEqual('FAILED',generation['state'])
        self.assertEqual('REPORT_NATIVE_EXECUTION_FAILED',generation['reason_code'])
        self.assertEqual('native_execution',generation['failure_stage'])

    def test_oversize_history_moves_to_verified_artifact_without_losing_current_gates(self):
        current={'checkpoint':{'open_gates':['未关闭 FAIL，不能合入'],
                               'waiting_external':['仍需真实覆盖']}}
        history=[{'event_id':n,'checkpoint':{'done':'历史记录'*300}} for n in range(20)]
        facts={'events':[{'kind':'CHECKPOINT_SAVED','payload':{'done':'历史'*200}} for _ in range(20)],
               'evidence_ids':['event:'+str(n) for n in range(20)],
               'work_checkpoint_events':history,'current_works':[current],
               'role_states':[{'role':'A','label':'阻塞'}],
               'verifications':[{'verification_id':'unclosed-fail','result':'FAIL','supersedes':[]}],
               'input_coverage':{}}
        expected=copy.deepcopy(facts)
        fit_report(self.ledger,facts,10000)
        self.assertLess(size(facts),10000)
        for field in ('current_works','role_states','verifications'):
            self.assertEqual(expected[field],facts[field])
        reference=facts['detail_artifacts']['report_checkpoint_history']
        stored=json.loads(Path(reference['path']).read_text())
        self.assertEqual(history,stored)
        self.assertTrue(facts['input_coverage']['omitted_sections'])


    def test_terminal_authorization_is_archived_but_current_permissions_and_results_survive(self):
        current={'action_id':'current','state':'INCOMPLETE','authorization':{'scope':'当前授权'*120}}
        terminal={'action_id':'old','state':'ENDED','authorization':{'scope':'历史授权'*1500}}
        facts={'events':[],'evidence_ids':[],'input_coverage':{},'external_actions':[terminal,current],
               'verifications':[{'result':'FAIL','supersedes':[],'verification_id':'unclosed'}]}
        fit_report(self.ledger,facts,4000)
        self.assertLess(size(facts),4000)
        self.assertEqual(current,facts['external_actions'][1])
        self.assertEqual('FAIL',facts['verifications'][0]['result'])
        ref=facts['detail_artifacts']['report_terminal_action_history']
        self.assertEqual(terminal,json.loads(Path(ref['path']).read_text())[0])


class ReviewRecoveryTests(WorkflowServiceTests):
    def incomplete_timeout(self, *, parallel_limit=2, nested_deadline=False):
        from workflow_services import collect_service_result
        import hashlib
        class Partial(ExternalService):
            def submit(adapter,kind,frozen,request,directory):
                reference=super().submit(kind,frozen,request,directory)
                native=directory/'native';native.mkdir()
                reference['native_directory']=str(native)
                (native/'spec.json').write_text(json.dumps({'run_id':'isolated-timeout-run','retry_depth':0,
                    'request_timeout_seconds':900,'concurrency_policy':{'idle_limit':parallel_limit},
                    'batches':[{'id':f'b{i:02}','files':[f'isolated-{i}.py']} for i in range(1,8)]}))
                (native/'state.json').write_text(json.dumps({'state':'REVIEW_INCOMPLETE','batches':[
                    {'id':f'b{i:02}','state':('HARD_DEADLINE' if nested_deadline and i==4 else
                                            'REVIEW_COMPLETE' if i<=3 else 'REVIEW_INCOMPLETE'),
                     'returncode':0 if i<=3 else 1} for i in range(1,6)]}))
                for i in (4,5):
                    (native/f'b{i:02}-diagnosis.json').write_text(json.dumps({
                        'manifest_failures':[{'classification':'timeout'}]}))
                return reference
            def collect(adapter,kind,reference):
                value=super().collect(kind,reference)
                value['sources'].extend({'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
                    for p in Path(reference['native_directory']).iterdir())
                return value
        adapter=Partial(self.root,incomplete=True)
        self.assertCommitted(self.submit(adapter))
        self.assertCommitted(collect_service_result(self.ledger,'ocr-one',command_id='collect-timeout',adapter=adapter))
        return Path(next(self.path.parent.glob('services/*/native')))

    def test_timeout_recovery_explains_failed_and_unstarted_batches_without_writing(self):
        from workflow_service_recovery import resume_readiness
        self.incomplete_timeout()
        before=self.ledger.snapshot()
        recovery=resume_readiness(self.ledger,'ocr-one')
        self.assertTrue(recovery['allowed'])
        self.assertEqual('SERIAL_PARTIAL_RECOVERY_READY',recovery['code'])
        self.assertEqual(1,recovery['repair']['max_parallel_batches'])
        self.assertEqual(['b01','b02','b03'],recovery['diagnosis']['completed_batches'])
        self.assertEqual(['b04','b05'],recovery['diagnosis']['request_timeouts'])
        self.assertEqual(['b06','b07'],recovery['diagnosis']['unstarted_batches'])
        self.assertFalse(recovery['diagnosis']['business_pass'])
        self.assertEqual(before,self.ledger.snapshot())

    def test_timeout_recovery_uses_native_owner_command_with_exact_action_scope(self):
        from workflow_progression import next_actions
        self.incomplete_timeout()
        snapshot=self.ledger.snapshot()
        packet={**snapshot,'role':'COORDINATOR','run':snapshot['runs'][0],
                'work':self.ledger.current_work('COORDINATOR')}
        action=next(a for a in next_actions(packet,self.ledger) if a.get('action_id')=='ocr-one')
        self.assertEqual('service_resume',action['action'])
        self.assertEqual('ocr-one',action['payload']['action_id'])
        self.assertEqual('ocr-one-resume1',action['payload']['new_action_id'])
        self.assertFalse(self.ledger.snapshot()['verifications'])

    def test_timeout_diagnosis_tamper_cannot_authorize_recovery(self):
        from workflow_service_recovery import resume_readiness
        native=self.incomplete_timeout()
        (native/'b04-diagnosis.json').write_text('{}')
        self.assertEqual('RESUME_DIAGNOSIS_UNVERIFIED',resume_readiness(self.ledger,'ocr-one')['code'])

    def test_fresh_submission_cannot_bypass_partial_recovery_or_consume_authorization(self):
        from workflow_services import submit_service
        self.incomplete_timeout()
        before=self.ledger.snapshot()
        adapter=ExternalService(self.root)
        adapter.preflight=mock.Mock(side_effect=AssertionError('不可启动新全量审查'))
        result=submit_service(self.ledger,self.coord,self.candidate_id,'ocr',
            {'background_text':'换背景或编号不构成修复'},action_id='fresh-loop',
            authorization=self.authorization,command_id='fresh-loop',adapter=adapter)
        self.assertEqual('OCR_PARTIAL_RECOVERY_REQUIRED',result['code'])
        self.assertFalse(result['dispatch_performed'])
        self.assertFalse(result['authorization_consumed'])
        adapter.preflight.assert_not_called()
        self.assertEqual(before,self.ledger.snapshot())

    def test_linked_partial_recovery_still_uses_the_native_submission_boundary(self):
        from workflow_service_recovery import resume
        self.incomplete_timeout()
        adapter=ExternalService(self.root)
        with mock.patch('workflow_services.NativeServices',return_value=adapter):
            result=resume(self.ledger,self.coord,'ocr-one','linked-retry','隔离测试已核串行补审方案',
                          authorization=self.authorization,command_id='linked-retry')
        self.assertCommitted(result)
        self.assertEqual(1,adapter.calls)
        self.assertEqual('DUPLICATE_REVIEW',__import__('workflow_service_recovery').resume_readiness(self.ledger,'ocr-one')['code'])

    def test_native_entry_also_rejects_a_stale_clients_fresh_dispatch(self):
        from workflow_services import _directory, validate_native_action
        self.incomplete_timeout()
        # Simulate a client loaded before the new submit gate, entirely in tmp.
        with mock.patch('workflow_query.review_action_projection',return_value={'attention':[]}):
            self.assertCommitted(self.submit(ExternalService(self.root),action='stale-client'))
        path=_directory(self.ledger,'stale-client')/'invocation.json'
        value=json.loads(path.read_text());value['state']='DISPATCHING'
        path.write_text(json.dumps(value));path.chmod(0o600)
        before=self.ledger.snapshot()
        with mock.patch('workflow_services.time.time',return_value=self.now):
            with self.assertRaisesRegex(ValueError,'OCR_PARTIAL_RECOVERY_REQUIRED'):
                validate_native_action(path,'ocr',{})
        self.assertEqual(before,self.ledger.snapshot())

    def test_already_serial_or_nested_deadline_does_not_consume_retry(self):
        from workflow_service_recovery import resume_readiness, resume
        self.incomplete_timeout(parallel_limit=1, nested_deadline=True)
        before=self.ledger.snapshot()
        self.assertEqual('ROOT_CAUSE_REPAIR_REQUIRED',resume_readiness(self.ledger,'ocr-one')['code'])
        with mock.patch('workflow_services.submit_service',side_effect=AssertionError('不可派发')):
            result=resume(self.ledger,self.coord,'ocr-one','blocked-retry','隔离测试拒绝原样重跑',
                          command_id='blocked-retry')
        self.assertFalse(result['authorization_consumed'])
        self.assertEqual(before,self.ledger.snapshot())

    def test_already_serial_timeout_requires_actual_repair(self):
        from workflow_service_recovery import resume_readiness
        self.incomplete_timeout(parallel_limit=1)
        before=self.ledger.snapshot()
        self.assertEqual('ROOT_CAUSE_REPAIR_REQUIRED',resume_readiness(self.ledger,'ocr-one')['code'])
        self.assertEqual(before,self.ledger.snapshot())

    def test_review_failure_ages_escalate_once_per_level_without_user_question(self):
        from workflow_coordination import attention
        self.incomplete_timeout()
        initial=self.assertCommitted(attention(self.ledger)[0])
        self.assertEqual('DUPLICATE',attention(self.ledger)[0]['status'])
        self.now+=901
        first=self.assertCommitted(attention(self.ledger)[0])
        self.assertEqual('DUPLICATE',attention(self.ledger)[0]['status'])
        self.now+=2700
        second=self.assertCommitted(attention(self.ledger)[0])
        notices=[o for o in self.ledger.outbox_items(include_terminal=True)
                 if o['kind']=='COORDINATION_ATTENTION_REQUIRED']
        self.assertEqual([0,1,2],[o['payload']['escalation_level'] for o in notices])
        self.assertEqual(3,len({initial['event_id'],first['event_id'],second['event_id']}))
        self.assertFalse(self.ledger.snapshot()['verifications'])
        self.assertTrue(all(not n['payload']['user_questions'] for n in notices))

    def test_partial_failure_notice_explains_coverage_in_chinese(self):
        from workflow_reports import render_notification
        self.incomplete_timeout()
        notice=next(o for o in self.ledger.outbox_items(include_terminal=True)
                    if o['kind']=='SERVICE_ARTIFACT_RECORDED')
        before=self.ledger.snapshot()
        text=render_notification(self.ledger,notice)
        self.assertIn('共7批，已完成3批，未完成2批，另有2批尚未开始',text)
        self.assertIn('2批发生模型请求超时',text)
        self.assertNotIn('INCOMPLETE',text)
        self.assertNotIn('attempt',text)
        self.assertEqual(before,self.ledger.snapshot())

    def test_only_later_complete_same_candidate_review_retires_old_attention(self):
        from copy import deepcopy
        from workflow_coordination import facts
        from workflow_query import review_action_projection
        self.incomplete_timeout()
        original=self.ledger.snapshot()
        prior=original['external_actions'][-1]
        # Clearly isolated synthetic projections: no real review or result is recorded.
        for state in ('SUBMITTED','UNKNOWN','INCOMPLETE','FAILED','RESULT_READY','ENDED'):
            snapshot=deepcopy(original)
            snapshot['external_actions'].append({**prior,'action_id':'synthetic-successor',
                'state':state,'created_at':prior['created_at']+1})
            view=review_action_projection(snapshot,'run-one')
            old=next(a for a in view['actions'] if a['action_id']=='ocr-one')
            self.assertEqual(state not in ('RESULT_READY','ENDED'),old['current_attention'],state)
            reviews=facts(snapshot,'run-one',self.now)['reviews']
            self.assertEqual(old['current_attention'],any(a['action_id']=='ocr-one' for a in reviews),state)
            if state in ('RESULT_READY','ENDED'):
                from workflow_progression import next_actions
                packet={**snapshot,'role':'COORDINATOR','run':snapshot['runs'][0],
                        'work':self.ledger.current_work('COORDINATOR')}
                actions=next_actions(packet,self.ledger,snapshot=snapshot)
                self.assertFalse(any(a.get('action_id')=='ocr-one' for a in actions))
        self.assertEqual(original,self.ledger.snapshot())

    def test_completed_task_retires_old_failure_but_keeps_global_unknown_gate(self):
        from copy import deepcopy
        from workflow_query import review_action_projection
        self.incomplete_timeout()
        snapshot=deepcopy(self.ledger.snapshot())
        prior=snapshot['external_actions'][-1]
        next(t for t in snapshot['tasks'] if t['task_id']=='T1')['phase']='COMPLETED'
        snapshot['external_actions'].append({**prior,'action_id':'synthetic-unknown','state':'UNKNOWN'})
        view=review_action_projection(snapshot,'run-one')
        self.assertEqual(['synthetic-unknown'],[a['action_id'] for a in view['attention']])
        self.assertEqual('INCOMPLETE',next(a for a in view['actions'] if a['action_id']=='ocr-one')['state'])

    def incomplete(self,action='ocr-one'):
        from workflow_services import collect_service_result, _directory
        adapter=ExternalService(self.root,incomplete=True)
        self.assertCommitted(self.submit(adapter,action=action))
        self.assertCommitted(collect_service_result(self.ledger,action,command_id='collect-'+action,adapter=adapter))
        native=_directory(self.ledger,action)/'native';native.mkdir()
        (native/'spec.json').write_text(json.dumps({'retry_depth':0}))
        (native/'state.json').write_text(json.dumps({'state':'HARD_DEADLINE'}))
        return native

    def test_incomplete_service_alert_is_queued_without_claiming_code_failure(self):
        self.incomplete()
        notices=self.ledger.outbox_items(include_terminal=True)
        notice=next(row for row in notices if row['kind']=='SERVICE_ARTIFACT_RECORDED')
        self.assertEqual('COORDINATOR',notice['role'])
        self.assertEqual('INCOMPLETE',notice['payload']['state'])
        self.assertFalse(notice['payload']['business_pass'])
        self.assertFalse(self.ledger.snapshot()['verifications'])

    def test_incomplete_next_step_reads_existing_evidence_instead_of_collecting_again(self):
        from workflow_progression import next_actions
        self.incomplete()
        snapshot=self.ledger.snapshot()
        packet={**snapshot,'role':'COORDINATOR','run':snapshot['runs'][0],
                'work':self.ledger.current_work('COORDINATOR')}
        actions=next_actions(packet,self.ledger)
        repair=next(row for row in actions if row.get('action_id')=='ocr-one')
        self.assertEqual('read_file',repair['action'])
        self.assertTrue(Path(repair['evidence']['path']).is_file())
        self.assertIn('decide REWORK',repair['reason'])
        self.assertFalse(self.ledger.snapshot()['verifications'])

    def test_fixed_deadline_is_not_resumed_or_charged_again(self):
        from workflow_service_recovery import resume, resume_readiness
        native=self.incomplete()
        self.assertEqual('ROOT_CAUSE_REPAIR_REQUIRED',resume_readiness(self.ledger,'ocr-one')['code'])
        before=self.ledger.snapshot()
        with mock.patch('workflow_services.submit_service',side_effect=AssertionError('不可再次派发')):
            result=resume(self.ledger,self.coord,'ocr-one','another-run','仅写原因不代表已修复',
                          authorization=self.authorization,command_id='refuse-unchanged')
        self.assertEqual('REJECTED',result['status'])
        self.assertFalse(result['authorization_consumed'])
        self.assertEqual(before,self.ledger.snapshot())
        (native/'spec.json').write_text(json.dumps({'retry_depth':1}))
        self.assertEqual('RETRY_LIMIT',resume_readiness(self.ledger,'ocr-one')['code'])
