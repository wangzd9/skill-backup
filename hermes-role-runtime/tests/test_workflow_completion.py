"""Final acceptance with real Git, native writers, isolated tests and receipts."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from unittest import mock

from test_business_ledger import LedgerFixture
from test_workflow_merge import CHECK
from delivery_evidence import execute_candidate_tests
from workflow_completion import reconcile, facts, verify, deliver, complete, target_current
from workflow_development_policy import enable
from workflow_merge_policy import authorization
from workflow_merge import merge_candidate
from workflow_delivery import complete as complete_task


class CompletionTests(LedgerFixture):
    def setUp(self):
        super().setUp()
        self.cp,self.coord=self.bind('COORDINATOR')
        source={'platform':'codex','source':'current_user_workflow_instruction','authenticated':True,
            'message_id':'isolated-fixture-consent','primary_text':'本隔离测试允许自动本地开发和合入。'}
        self.policy=self.assertCommitted(enable(self.ledger,'run-one','feat/final',source,command_id='local-policy'))
        self.acceptance=self.root/'frozen-ACCEPTANCE.md'
        self.acceptance.write_text('**TEST-AC-01**: value() returns the accepted value; local only.\n')
        ref={'path':str(self.root/'ACCEPTANCE.md'),'frozen_path':str(self.acceptance),
             'sha256':hashlib.sha256(self.acceptance.read_bytes()).hexdigest()}
        with self.ledger._connection() as db:
            self.ledger._event(db,'REQUIREMENT_ACCEPTED',{'sources':[ref],'task_ids':['T1','T2']},run_id='run-one')
        dp,dev=self.bind('A')
        self.cid,self.candidate_spec=self.candidate(dev)
        self.close(dp,dev)
        tp,tester=self.bind('TESTER')
        self.assertCommitted(execute_candidate_tests(self.ledger,tester,self.cid,
            [sys.executable,'-c',CHECK,'{junit_xml}','1'],report_format='junit',command_id='leaf-test'))
        self.close(tp,tester)
        self.assertCommitted(self.ledger.decide(self.coord,self.cid,'APPROVE',command_id='approve'))
        permit=self.assertCommitted(authorization(self.ledger,self.coord,self.cid))
        self.assertCommitted(merge_candidate(self.ledger,self.coord,self.cid,permit['authorization_id'],command_id='merge-leaf'))
        self.assertCommitted(complete_task(self.ledger,self.coord,self.cid,'isolated leaf completed',command_id='complete-leaf'))
        self.base=self.candidate_spec['head']
        dp,dev=self.bind('A')
        second,_=self.candidate(dev,2)
        self.close(dp,dev)
        tp,tester=self.bind('TESTER')
        self.assertCommitted(execute_candidate_tests(self.ledger,tester,second,
            [sys.executable,'-c',CHECK,'{junit_xml}','2'],report_format='junit',command_id='leaf2-test'))
        self.close(tp,tester)
        self.assertCommitted(self.ledger.decide(self.coord,second,'APPROVE',command_id='approve2'))
        permit=self.assertCommitted(authorization(self.ledger,self.coord,second))
        self.assertCommitted(merge_candidate(self.ledger,self.coord,second,permit['authorization_id'],command_id='merge-leaf2'))
        self.assertCommitted(complete_task(self.ledger,self.coord,second,'second leaf completed',command_id='complete-leaf2'))

    def queue(self):
        rows=reconcile(self.ledger)
        self.assertEqual('VERIFICATION_QUEUED',facts(self.ledger.snapshot(),'run-one')['state'])
        self.assertEqual('INTEGRATION_VERIFICATION_READY',self.ledger.admission_hint('TESTER')['reason'])
        return rows

    def run_final(self,expected='2',identifier='final-test'):
        tp,tester=self.bind('TESTER')
        result=verify(self.ledger,tester,{'argv':[sys.executable,'-c',CHECK,'{junit_xml}',expected],
            'report_format':'junit'},command_id=identifier)
        self.assertCommitted(result['publication'])
        return tp,tester,result['publication']['event_id']

    def payload(self,eid,*,result='PASS',issues=None):
        return {'summary':'Isolated final contract result',
            'checks':[{'id':'TEST-AC-01','result':result,'mode':'local',
                       'test_event_ids':[eid] if eid else [],'notes':'Actual unittest assertion in frozen checkout'}],
            'issues':issues or []}

    def test_native_queue_is_automatic_idempotent_and_planner_stays_manual(self):
        self.queue()
        snapshot=self.ledger.snapshot()
        self.assertEqual('manual',facts(snapshot,'run-one')['plan']['planner_trigger'])
        self.assertFalse(reconcile(self.ledger))
        self.assertEqual(len(snapshot['works']),len(self.ledger.snapshot()['works']))
        self.assertEqual('COMPLETED',snapshot['tasks'][0]['phase'])
        self.assertFalse(facts(snapshot,'run-one')['completion_event_id'])

    def test_real_final_evidence_and_writer_exit_are_required_before_native_closure(self):
        self.queue();tp,tester,eid=self.run_final()
        self.assertEqual('integration_verification',self.ledger.current_work('TESTER')['kind'])
        self.assertCommitted(deliver(self.ledger,tester,self.payload(eid),command_id='final-delivery'))
        closure={'summary':'local requirement accepted','review':'Checked final AC evidence and original leaf gates','historical_failures':[]}
        self.assertEqual('REQUIREMENT_TESTER_WRITER_NOT_CLEARED',complete(self.ledger,self.coord,closure,command_id='live-writer')['code'])
        self.close(tp,tester)
        self.assertCommitted(complete(self.ledger,self.coord,closure,command_id='closure'))
        state=facts(self.ledger.snapshot(),'run-one')
        self.assertEqual('COMPLETED',state['state'])
        row=next(e for e in self.ledger.snapshot()['events'] if e['kind']=='REQUIREMENT_COMPLETED')
        self.assertFalse(row['payload']['production_complete']);self.assertFalse(row['payload']['push_performed'])
        self.assertTrue(Path(row['payload']['receipt']['path']).exists())
        self.assertFalse(reconcile(self.ledger))

    def test_code_failure_reopens_original_owner_once_after_tester_exit(self):
        self.queue();tp,tester,eid=self.run_final('3')
        issue={'id':'FINAL-001','kind':'code','task_id':'T1','test_event_ids':[eid],'summary':'Actual contract assertion failed'}
        self.assertCommitted(deliver(self.ledger,tester,self.payload(eid,result='FAIL',issues=[issue]),command_id='failed-final'))
        self.assertFalse(reconcile(self.ledger))
        self.close(tp,tester)
        rows=reconcile(self.ledger)
        self.assertTrue(rows)
        task=self.ledger.snapshot()['tasks'][0]
        self.assertEqual(('A','PENDING',2),tuple(task[k] for k in ('role','phase','attempt')))
        self.assertEqual(self.plan[0]['scope'],task['scope'])
        self.assertEqual(self.plan[0]['required_checks'],task['required_checks'])
        self.assertFalse(reconcile(self.ledger))
        self.assertEqual('REPAIRING',facts(self.ledger.snapshot(),'run-one')['state'])
        self.assertTrue(self.ledger.admission_hint('A')['can_start_model'])
        old=next(v for v in self.ledger.snapshot()['events'] if v['kind']=='INTEGRATION_VERIFICATION_REPORTED')
        self.assertEqual('FAIL',old['payload']['result'])

    def test_fail_requires_real_failed_evidence_and_original_task_mapping(self):
        self.queue();tp,tester,eid=self.run_final()
        issue={'id':'FALSE-DEFECT','kind':'code','task_id':'T1','test_event_ids':[eid],'summary':'Cannot label passing evidence as a defect'}
        self.assertEqual('INTEGRATION_ISSUE_OUTSIDE_ACCEPTED_SCOPE',deliver(self.ledger,tester,
            self.payload(eid,result='FAIL',issues=[issue]),command_id='false-fail')['code'])

    def test_empty_and_changed_evidence_cannot_pass(self):
        self.queue();tp,tester,eid=self.run_final()
        self.assertEqual('INTEGRATION_EXECUTED_EVIDENCE_REQUIRED',deliver(self.ledger,tester,self.payload(None),command_id='empty')['code'])
        row=next(e for e in self.ledger.snapshot()['events'] if e['event_id']==eid)
        Path(row['payload']['evidence']['path']).write_text('<testsuite tests="1"/>')
        self.assertEqual('INTEGRATION_TEST_EVIDENCE_CHANGED',deliver(self.ledger,tester,self.payload(eid),command_id='changed')['code'])

    def test_frozen_source_and_revoked_policy_invalidate_old_target(self):
        self.queue();tp,tester=self.bind('TESTER')
        self.acceptance.write_text('changed accepted bytes')
        self.assertFalse(target_current(self.ledger,self.ledger.snapshot(),self.ledger.current_work('TESTER')))
        self.assertEqual('INTEGRATION_SNAPSHOT_CHANGED',deliver(self.ledger,tester,self.payload(None),command_id='stale-source')['code'])
        self.assertCommitted(self.ledger.release_work(tester,reason='fixed source changed',command_id='release-stale'))

    def test_revocation_and_pause_never_queue_or_rebind_final_verification(self):
        source={'authenticated':True,'current_full':True,'message_id':'revoke-fixture','user_id':'u','primary_text':'撤销授权'}
        self.assertCommitted(self.ledger.revoke_authorization(self.policy['authorization_id'],source,command_id='revoke'))
        self.assertFalse(reconcile(self.ledger))
        self.assertFalse(self.ledger.admission_hint('TESTER')['can_start_model'])

    def test_environment_gap_is_unverified_and_retries_are_bounded(self):
        self.queue()
        for attempt in range(3):
            tp,tester=self.bind('TESTER')
            issue={'id':'ENV-1','kind':'environment','test_event_ids':[],'summary':'Isolated service unavailable'}
            self.assertCommitted(deliver(self.ledger,tester,self.payload(None,result='UNVERIFIED',issues=[issue]),command_id=f'env-{attempt}'))
            self.close(tp,tester)
            self.assertFalse(reconcile(self.ledger))
            self.now+=301
            rows=reconcile(self.ledger)
            if attempt<2:self.assertTrue(rows)
            else:self.assertFalse(rows)
        self.assertEqual(1,self.ledger.snapshot()['tasks'][0]['attempt'])
        self.assertEqual('COMPLETED',self.ledger.snapshot()['tasks'][0]['phase'])

    def test_schema_is_shared_and_rejects_role_and_permission_overrides(self):
        from workflow_contracts import command_schema,validate_payload
        actions=['verify_integration','deliver_integration','complete_requirement']
        schema=command_schema(actions)
        self.assertEqual(actions,schema['properties']['action']['enum'])
        with self.assertRaises(ValueError):validate_payload('verify_integration',{'argv':['pytest'],'role':'TESTER'})
        with self.assertRaises(ValueError):validate_payload('complete_requirement',{'summary':'x','review':'x','push_permission':True})

    def test_defect_repair_passes_original_gates_then_automatically_requeues_final_snapshot(self):
        self.queue();tp,tester,eid=self.run_final('3')
        issue={'id':'FINAL-T2','kind':'code','task_id':'T2','test_event_ids':[eid],'summary':'Final accepted value must be three'}
        self.assertCommitted(deliver(self.ledger,tester,self.payload(eid,result='FAIL',issues=[issue]),command_id='failed-final'))
        self.close(tp,tester);reconcile(self.ledger)
        self.base=facts(self.ledger.snapshot(),'run-one')['target']['head']
        dp,dev=self.bind('A');repaired,_=self.candidate(dev,3);self.close(dp,dev)
        tp,tester=self.bind('TESTER')
        self.assertCommitted(execute_candidate_tests(self.ledger,tester,repaired,
            [sys.executable,'-c',CHECK,'{junit_xml}','3'],report_format='junit',command_id='repair-independent-test'))
        self.close(tp,tester)
        self.assertCommitted(self.ledger.decide(self.coord,repaired,'APPROVE',command_id='approve-repair'))
        permit=self.assertCommitted(authorization(self.ledger,self.coord,repaired))
        self.assertCommitted(merge_candidate(self.ledger,self.coord,repaired,permit['authorization_id'],command_id='merge-repair'))
        self.assertCommitted(complete_task(self.ledger,self.coord,repaired,'Repaired and independently verified',command_id='complete-repair'))
        self.assertTrue(reconcile(self.ledger))
        queued=facts(self.ledger.snapshot(),'run-one')
        self.assertEqual('VERIFICATION_QUEUED',queued['state'])
        self.assertEqual(2,next(c for c in queued['target']['suppliers'] if c['task_id']=='T2')['attempt'])
        tp,tester,new_eid=self.run_final('3','final-test2')
        self.assertCommitted(deliver(self.ledger,tester,self.payload(new_eid),command_id='passed-final2'));self.close(tp,tester)
        self.assertCommitted(complete(self.ledger,self.coord,{'summary':'local acceptance complete',
            'review':'Reviewed final and original independent test evidence','historical_failures':[]},command_id='close-after-repair'))
        reports=[e for e in self.ledger.snapshot()['events'] if e['kind']=='INTEGRATION_VERIFICATION_REPORTED']
        self.assertEqual(['FAIL','PASS'],[e['payload']['result'] for e in reports])

    def test_mysql_contract_cannot_be_passed_with_only_local_evidence(self):
        self.acceptance.write_text('TEST-AC-01: 隔离 MySQL 8.4 真实事务。')
        with self.ledger._connection() as db:
            ref={'path':str(self.root/'ACCEPTANCE.md'),'frozen_path':str(self.acceptance),
                 'sha256':hashlib.sha256(self.acceptance.read_bytes()).hexdigest()}
            self.ledger._event(db,'REQUIREMENT_ACCEPTED',{'sources':[ref],'task_ids':['T1','T2']},run_id='run-one')
        self.queue();tp,tester,eid=self.run_final()
        self.assertEqual('INTEGRATION_REAL_ISOLATED_MYSQL_REQUIRED',deliver(self.ledger,tester,self.payload(eid),command_id='mock-mysql')['code'])

    def test_final_closure_keeps_historical_failures_and_requires_explicit_proof(self):
        self.queue();tp,tester,eid=self.run_final()
        self.assertCommitted(deliver(self.ledger,tester,self.payload(eid),command_id='passed-final'));self.close(tp,tester)
        # Clearly isolated legacy state fixture; no real record is changed.
        with self.ledger._connection() as db:
            db.execute("INSERT INTO verifications SELECT 'legacy-failure',candidate_id,check_name,role,'FAIL',evidence,'[]',NULL,created_at FROM verifications LIMIT 1")
        closure={'summary':'local acceptance','review':'Reviewed legacy failure against final test','historical_failures':[]}
        self.assertEqual('REQUIREMENT_HISTORICAL_FAILURE_DISPOSITION_REQUIRED',complete(self.ledger,self.coord,closure,command_id='no-disposition')['code'])
        closure['historical_failures']=[{'verification_id':'legacy-failure','test_event_ids':[eid],'reason':'Original assertion is now covered by final frozen checkout execution'}]
        self.assertCommitted(complete(self.ledger,self.coord,closure,command_id='with-disposition'))
        self.assertEqual('FAIL',next(v for v in self.ledger.snapshot()['verifications'] if v['verification_id']=='legacy-failure')['result'])
