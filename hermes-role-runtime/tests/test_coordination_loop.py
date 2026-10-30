"""Real ledger/Git regressions for the repeated-approval incident."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest import mock

from test_business_ledger import LedgerFixture
from business_ledger import BusinessLedger
import pool_entry
import workflow_runtime as workflow
from workflow_progression import next_actions
from workflow_reports import build_facts, render_periodic, COORDINATOR_SECTIONS
from workflow_views import fit_report, size, work_packet


class CoordinationLoopTests(LedgerFixture):
    def setUp(self):
        super().setUp()
        self.pool=self.root/'isolated.json';self.pool.write_text('{}')
        clocked=mock.patch('workflow_runtime.open_ledger',return_value=self.ledger)
        clocked.start();self.addCleanup(clocked.stop)
        self.provenance={'authenticated':True,'current_full':True,'message_id':'fixture',
                         'user_id':'fixture-user','chat_id':'fixture-chat','primary_text':'isolated test authorization'}

    def ready(self):
        p,dev=self.bind('A');cid,_=self.candidate(dev);self.close(p,dev)
        p,tester=self.bind('TESTER');evidence,run=self.actual_test(cid)
        self.assertEqual(0,run.returncode)
        self.assertCommitted(self.ledger.publish_verification(tester,cid,'independent_test','PASS',
                              evidence,command_id='pass'))
        self.close(p,tester)
        _,coord=self.bind('COORDINATOR')
        return cid,coord

    def test_duplicate_approval_short_circuits_acceptance_and_business_events(self):
        cid,coord=self.ready()
        first=self.assertCommitted(self.ledger.decide(coord,cid,'APPROVE',command_id='first'))
        before=self.ledger.snapshot()
        with mock.patch.object(self.ledger,'_acceptance',side_effect=AssertionError('duplicate revalidated')):
            second=self.ledger.decide(coord,cid,'APPROVE',command_id='second')
        self.assertEqual('DUPLICATE',second['status'])
        self.assertEqual(first['decision_id'],second['decision_id'])
        self.assertEqual('APPROVE',second['recorded_decision'])
        self.assertFalse(second['acceptance_revalidated'])
        after=self.ledger.snapshot()
        self.assertEqual(before['decisions'],after['decisions'])
        self.assertEqual(before['events'],after['events'])
        self.assertEqual(1,len(after['decisions']))
        self.assertEqual('REVIEW',after['tasks'][0]['phase'])

    def test_first_approval_still_checks_gates(self):
        p,dev=self.bind('A');cid,_=self.candidate(dev);self.close(p,dev)
        _,coord=self.bind('COORDINATOR')
        result=self.ledger.decide(coord,cid,'APPROVE',command_id='premature')
        self.assertEqual('ACCEPTANCE_GATES_NOT_CLEAR',result['code'])
        self.assertEqual([],self.ledger.snapshot()['decisions'])

    def test_duplicate_keeps_identity_and_evidence_conflict_checks(self):
        cid,coord=self.ready()
        self.assertCommitted(self.ledger.decide(coord,cid,'APPROVE',command_id='first',evidence={'reason':'one'}))
        result=self.ledger.decide(coord,cid,'APPROVE',command_id='different',evidence={'reason':'two'})
        self.assertEqual('DECISION_EVIDENCE_CONFLICT',result['code'])
        result=self.ledger.decide({**coord,'execution_generation':-1},cid,'APPROVE',command_id='forged')
        self.assertNotIn(result['status'],('COMMITTED','DUPLICATE'))
        self.assertEqual(1,len(self.ledger.snapshot()['decisions']))

    def test_progression_advances_from_approval_to_authorization_then_merge(self):
        cid,coord=self.ready()
        packet=workflow.current_input(self.pool,'COORDINATOR')
        self.assertFalse(packet['candidate_approvals'][cid]['approved'])
        self.assertEqual(['decide'],[a['action'] for a in packet['next_actions'] if a.get('candidate_id')==cid])
        self.assertCommitted(self.ledger.decide(coord,cid,'APPROVE',command_id='approve'))
        packet=workflow.current_input(self.pool,'COORDINATOR')
        self.assertTrue(packet['candidate_approvals'][cid]['approved'])
        self.assertFalse(any(a['action']=='decide' for a in packet['next_actions']))
        self.assertEqual('merge',next(a for a in packet['next_actions'] if a['action']=='authorization_required')['kind'])
        from workflow_merge_policy import authorization
        denied=authorization(self.ledger,coord,cid)
        self.assertEqual('ACCEPTED_REQUIREMENT_MERGE_POLICY_REQUIRED',denied['code'])
        repeated=authorization(self.ledger,coord,cid)
        self.assertEqual('REJECTED',repeated['status'])
        self.assertTrue(repeated['replayed'])
        self.assertCommitted(self.ledger.grant_merge_authorization({'run_id':'run-one','task_id':'T1',
            'attempt':1,'repo':str(self.repo),'branch':self.git('branch','--show-current'),
            'base':self.base,'ac_version':'ac-sha256-frozen-v1','expires_at':self.now+3600},
            self.provenance,command_id='grant'))
        packet=workflow.current_input(self.pool,'COORDINATOR')
        self.assertEqual(['merge'],[a['action'] for a in packet['next_actions'] if a.get('candidate_id')==cid])
        permitted=authorization(self.ledger,coord,cid)
        self.assertEqual('COMMITTED',permitted['status'])
        self.assertIn('authorization_id',permitted)
        self.now+=3601
        packet=workflow.current_input(self.pool,'COORDINATOR')
        self.assertFalse(any(a['action']=='merge' for a in packet['next_actions']))
        self.assertEqual('REJECTED',authorization(self.ledger,coord,cid)['status'])

    def test_gate_failure_is_not_hidden_by_historical_approval(self):
        cid,coord=self.ready()
        self.assertCommitted(self.ledger.decide(coord,cid,'APPROVE',command_id='approve'))
        with mock.patch.object(self.ledger,'acceptance',return_value={'ready':False}):
            actions=next_actions(workflow.current_input(self.pool,'COORDINATOR'),self.ledger)
        self.assertFalse(any(a['action'] in ('merge','complete') for a in actions))

    def test_structured_external_wait_sleeps_until_new_business_event(self):
        cid,coord=self.ready()
        self.assertCommitted(self.ledger.decide(coord,cid,'APPROVE',command_id='approve'))
        packet=workflow.current_input(self.pool,'COORDINATOR')
        wait=next(a for a in packet['next_actions'] if a['action']=='checkpoint')
        page=self.ledger.work_inputs(coord,command_id='inputs')
        self.assertCommitted(self.ledger.checkpoint(coord,wait['payload']['checkpoint'],
            processed_through=page['through'],yield_execution=True,command_id='wait'))
        p=next(p for p in self.workers if p.pid==next(e['pid'] for e in self.ledger.snapshot()['executions']
                                                    if e['execution_id']==coord['execution_id']))
        self.close(p,coord)
        self.assertEqual('WAITING_EXTERNAL_WITHOUT_NEW_BUSINESS_EVENT',self.ledger.admission_hint('COORDINATOR')['reason'])
        self.assertCommitted(self.ledger.control_request('continue',{'run_id':'run-one'},self.provenance,command_id='new-event'))
        self.assertTrue(self.ledger.admission_hint('COORDINATOR')['can_start_model'])

    def test_history_projection_is_bounded_and_hash_recoverable(self):
        packet={'execution_history':[{'execution_id':str(i),'state':'ENDED','native_outcome':'SUCCESS',
                                     'writer_cleared':True} for i in range(1500)],
                'candidates':[{'candidate_id':'old','state':'SUPERSEDED','summary':'历史'*5000}],
                'verifications':[{'candidate_id':'old','check_name':'ocr','result':'FAIL',
                                  'supersedes':[],'evidence':{'raw':'failure'*2000}}]}
        work_packet(self.ledger,packet)
        self.assertEqual(1500,packet['execution_history_total'])
        self.assertEqual(1490,packet['execution_history_omitted'])
        self.assertEqual(10,len(packet['execution_history']))
        self.assertEqual('FAIL',packet['verifications'][0]['result'])
        self.assertEqual([],packet['verifications'][0]['supersedes'])
        for ref in packet['detail_artifacts'].values():
            self.assertEqual(ref['sha256'],hashlib.sha256(Path(ref['path']).read_bytes()).hexdigest())
        self.assertLess(size(packet),10000)

    def test_report_checkpoint_limit_is_explicit_and_body_not_duplicated(self):
        _,coord=self.bind('COORDINATOR')
        for i in range(30):
            self.assertCommitted(self.ledger.checkpoint(coord,{'done':'历史成果'+str(i)+'x'*2000},command_id='cp'+str(i)))
        before=self.ledger.snapshot()
        facts=build_facts(self.ledger,'COORDINATOR',self.now-1,self.now+1,
                         usage_index=self.root/'usage.db',project=self.root)
        self.assertEqual(30,facts['checkpoint_events_total'])
        self.assertEqual(10,facts['checkpoint_events_omitted'])
        self.assertEqual(20,len(facts['work_checkpoint_events']))
        self.assertLess(size(facts),81920)
        self.assertEqual(before,self.ledger.snapshot())
        for event in facts['events']:
            if event['kind']=='CHECKPOINT_SAVED':
                self.assertNotIn('done',event['payload']['checkpoint'])
        analysis={'accomplishments':'无新增交付','issues':'历史正文未展开','next_steps':'核当前门禁',
                  'sections':dict.fromkeys(COORDINATOR_SECTIONS,'待核实'),'evidence_ids':[]}
        text=render_periodic(facts,analysis)
        self.assertIn('共 30 条',text);self.assertIn('历史 10 条',text)

    def test_report_overflow_uses_explicit_evidence_pages(self):
        facts={'events':[{'event_id':i,'kind':'FAILURE','payload':{'reason':'x'*1000}} for i in range(150)],
               'evidence_ids':['event:'+str(i) for i in range(150)],'input_coverage':{},
               'tasks':[{'required_checks':['ocr']}],'verifications':[{'result':'FAIL'}]}
        fit_report(self.ledger,facts,20000)
        self.assertLess(size(facts),20000)
        self.assertEqual('FAIL',facts['verifications'][0]['result'])
        self.assertEqual(150,facts['input_coverage']['events_total'])
        self.assertEqual(150,sum(len(json.loads(Path(ref['path']).read_text()))
                                for ref in facts['input_coverage']['event_pages']))
        self.assertIn('未分析',facts['input_coverage']['notice'])

    def test_small_checkpoint_keeps_original_event_and_references_survive_overflow(self):
        _,coord=self.bind('COORDINATOR')
        result=self.assertCommitted(self.ledger.checkpoint(coord,{'next':'small checkpoint'},command_id='small'))
        original=next(e for e in self.ledger.snapshot()['events'] if e['event_id']==result['event_id'])
        facts=build_facts(self.ledger,'COORDINATOR',self.now-1,self.now+1,
                         usage_index=self.root/'usage.db',project=self.root)
        self.assertIn(original,facts['events'])
        row=facts['work_checkpoint_events'][0]
        self.assertIn('same_as',row['checkpoint'])
        for i in range(150):
            facts['events'].append({'event_id':10000+i,'kind':'FAILURE','payload':{'reason':'x'*1000}})
        fit_report(self.ledger,facts,20000)
        self.assertEqual({'next':'small checkpoint'},facts['work_checkpoint_events'][0]['checkpoint'])
        self.assertEqual([],facts['events'])

    def test_report_keeps_real_failed_check_when_history_moves_to_pages(self):
        p,dev=self.bind('A');cid,_=self.candidate(dev);self.close(p,dev)
        p,tester=self.bind('TESTER');evidence,run=self.actual_test(cid,expected=2)
        self.assertNotEqual(0,run.returncode)
        failed=self.assertCommitted(self.ledger.publish_verification(tester,cid,'independent_test','FAIL',
                                  evidence,command_id='real-fail'))
        facts=build_facts(self.ledger,'COORDINATOR',self.now-1,self.now+1,
                         usage_index=self.root/'usage.db',project=self.root)
        for i in range(150):
            facts['events'].append({'event_id':10000+i,'kind':'CHECKPOINT_SAVED','payload':{'reason':'x'*1000}})
        fit_report(self.ledger,facts,20000)
        check=next(v for v in facts['verifications'] if v['verification_id']==failed['verification_id'])
        self.assertEqual('FAIL',check['result'])
        self.assertEqual([],check['supersedes'])
        self.assertFalse(self.ledger.acceptance(cid)['ready'])

    def test_cursor_survives_execution_and_reader_process_changes(self):
        p,coord=self.bind('COORDINATOR')
        page=self.assertCommitted(self.ledger.work_inputs(coord,command_id='page',limit=1))
        self.assertCommitted(self.ledger.checkpoint(coord,{'next':'continue'},processed_through=page['through'],
                                                   yield_execution=True,command_id='yield'))
        self.close(p,coord)
        _,new=self.bind('COORDINATOR')
        code='from business_ledger import BusinessLedger;import json,sys;print(json.dumps(BusinessLedger(sys.argv[1]).work_inputs(json.loads(sys.argv[2]),command_id="new-process",limit=1)))'
        result=json.loads(subprocess.check_output([sys.executable,'-c',code,str(self.path),json.dumps(new)],text=True))
        self.assertEqual('COMMITTED',result['status'])
        self.assertEqual(page['through'],result['processed_through'])
        old=self.ledger.work_inputs(new,command_id='old',after=0)
        self.assertEqual('PAGE_AFTER_ALREADY_PROCESSED',old['code'])
        jump=self.ledger.work_inputs(new,command_id='jump',after=100000)
        self.assertEqual('PAGE_AFTER_NOT_PREVIOUSLY_ISSUED',jump['code'])
        self.assertIn('processed_through',old)

    def test_history_read_does_not_issue_or_ack_events(self):
        _,coord=self.bind('COORDINATOR')
        root=Path(__file__).resolve().parents[1]
        spec=importlib.util.spec_from_file_location('coordination_loop_plugin',root/'__init__.py',
                                                   submodule_search_locations=[str(root)])
        module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
        plugin=module.RuntimePlugin(registry=self.root/'registry',home=self.root,metrics_url='unused',pool_path=self.pool)
        scope={**coord,'workflow_version':1,'kind':'business','pool_path':str(self.pool),'run_id':'run-one'}
        before=self.ledger.snapshot()
        with mock.patch.object(plugin,'_frozen_scope',return_value=scope):
            value=json.loads(plugin.work_input_tool({'history':True,'after':0},session_id='fixture'))
        self.assertFalse(value['can_ack']);self.assertTrue(value['events'])
        self.assertEqual(before,self.ledger.snapshot())

    def test_budget_default_and_first_write_gate_unchanged(self):
        import workflow_sessions as sessions
        _,coord=self.bind('COORDINATOR')
        scope={**coord,'kind':'business','workflow_version':1}
        self.assertEqual(128000,sessions.DEFAULT_POLICY['checkpoint_input_tokens'])
        sessions.observe(self.root,scope,approx_input_tokens=128001)
        self.assertEqual('SESSION_CHECKPOINT_REQUIRED',sessions.tool_decision(self.root,scope,
            'role_coordination_command',{'action':'decide'})['reason'])


class PoolEntryDiagnosticsTests(unittest.TestCase):
    def test_exception_preserves_type_and_bounded_original_message(self):
        err=io.StringIO()
        with mock.patch.object(sys,'argv',['pool_entry','morning','--role','COORDINATOR']), \
             mock.patch.object(pool_entry,'report_scope',side_effect=ValueError('REPORT_BUDGET: '+'x'*700)), \
             contextlib.redirect_stderr(err):
            self.assertEqual(1,pool_entry.main())
        value=json.loads(err.getvalue())
        self.assertEqual('ValueError',value['error'])
        self.assertTrue(value['message'].startswith('REPORT_BUDGET:'))
        self.assertEqual(500,len(value['message']))
