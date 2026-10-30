"""Real isolated Git/ledger/runner uncertainty -> retirement -> repair loop."""
import json
from pathlib import Path
import subprocess
import sys
from unittest import mock

from test_supplemental_verification import SupplementalVerificationTests
from test_workflow_merge import CHECK
from delivery_evidence import execute_candidate_tests
from workflow_development_policy import enable
from workflow_merge import merge_candidate
from workflow_merge_policy import authorization
from workflow_delivery import complete
from workflow_supplemental import request,queue_repairs,get_context,summary
from workflow_verification_exceptions import facts,recover,reconcile,resolve,blockers,closure_passes
from workflow_verification_lifecycle import reconcile as retire
from workflow_progression import next_actions


class VerificationExceptionTests(SupplementalVerificationTests):
    # The base setup performs real candidate testing/merge/completion. Inherited
    # tests remain in their own module; only these tests are loaded here.
    def uncertain(self):
        self.assertCommitted(request(self.ledger,self.target,self.source,command_id='exception-request'))
        proc,identity=self.bind('TESTER')
        program='import pathlib,sys;pathlib.Path(sys.argv[1]).write_text(\'<testsuite tests="2" skipped="1"><testcase name="real"/><testcase name="missing"><skipped/></testcase></testsuite>\')'
        result=execute_candidate_tests(self.ledger,identity,self.cid,[sys.executable,'-c',program,'{junit_xml}'],
            command_id='exception-run',report_format='junit',verification_profile='mysql_migration')
        self.assertEqual('UNVERIFIED',result['status'])
        self.assertCommitted(result['exception_handoff'])
        return proc,identity,facts(self.ledger.snapshot(),'mysql-run')[-1]

    def policy(self):
        branch=self.git('symbolic-ref','--short','HEAD')
        return self.assertCommitted(enable(self.ledger,'mysql-run',branch,
            {'platform':'codex','source':'current_user_workflow_instruction','authenticated':True,'message_id':'isolated-policy',
             'primary_text':'本隔离测试已授权原范围开发与本地合入','explicit_primary_branch':branch},command_id='policy'))

    def repair(self,item,**overrides):
        return {'observation_event_id':item['observation_event_id'],'disposition':'repair',
            'candidate_id':self.cid,'affected_paths':['app.py'],'reason':'isolated confirmed fixture repair',**overrides}

    def test_unverified_handoff_preserves_result_requests_retirement_and_releases_only_owner(self):
        proc,tester,item=self.uncertain()
        snap=self.ledger.snapshot();self.assertEqual('RUNNING',self.ledger.current_work('TESTER')['state'])
        self.assertEqual('AWAITING_COORDINATOR_DISPOSITION',item['state'])
        self.assertFalse(any(v['check_name']=='supplemental_mysql' for v in snap['verifications']))
        self.assertEqual('UNVERIFIED',summary(snap,'mysql-run')[-1]['result'])
        self.assertEqual('safe_release',next_actions({'role':'TESTER','work':self.ledger.current_work('TESTER'),
            'selected_run_id':'mysql-run'},self.ledger)[0]['action'])
        self.assertFalse(retire(self.ledger)[0].get('retired'))
        released=self.assertCommitted(self.ledger.release_work(tester,reason='evidence handed off',command_id='release'))
        self.assertTrue(released['writer_clearance_required']);self.assertFalse(self.ledger.admission_hint('TESTER')['can_start_model'])
        self.close(proc,tester);retire(self.ledger)
        self.assertIsNone(self.ledger.current_work('TESTER'))
        self.assertTrue(facts(self.ledger.snapshot(),'mysql-run')[-1]['blocking'])

    def test_worker_exit_alone_does_not_create_business_repair(self):
        proc,tester,item=self.uncertain();self.close(proc,tester)
        work=next(w for w in self.ledger.snapshot()['works'] if w['work_id']==tester['work_id'])
        self.assertEqual('QUEUED',work['state'])
        self.assertEqual([],reconcile(self.ledger))
        retire(self.ledger)
        self.assertEqual(1,next(t for t in self.ledger.snapshot()['tasks'] if t['task_id']=='MN-01')['attempt'])
        self.assertEqual('COMPLETED',next(t for t in self.ledger.snapshot()['tasks'] if t['task_id']=='MN-01')['phase'])

    def test_completed_task_repair_waits_for_writer_then_queues_once_without_scope_or_ac_change(self):
        proc,tester,item=self.uncertain();self.policy()
        before=next(t for t in self.ledger.snapshot()['tasks'] if t['task_id']=='MN-01')
        result=self.assertCommitted(resolve(self.ledger,self.coord,self.repair(item),command_id='disposition'))
        self.assertEqual('PENDING_NATIVE_REPAIR',result['state']);self.assertFalse(result['requires_user_approval'])
        self.assertEqual([],reconcile(self.ledger));self.close(proc,tester);retire(self.ledger)
        queued=reconcile(self.ledger);self.assertEqual(1,len(queued));self.assertTrue(queued[0]['queued'])
        after=next(t for t in self.ledger.snapshot()['tasks'] if t['task_id']=='MN-01')
        self.assertEqual((2,'PENDING'),(after['attempt'],after['phase']))
        for key in ('scope','ac_version','required_checks','role','contract'):self.assertEqual(before[key],after[key])
        self.assertEqual([],reconcile(self.ledger));self.assertEqual('REPAIR_QUEUED',facts(self.ledger.snapshot(),'mysql-run')[-1]['state'])

    def test_missing_policy_and_outside_scope_reject_atomically(self):
        _,_,item=self.uncertain();before=self.ledger.snapshot()['tasks']
        rejected=resolve(self.ledger,self.coord,self.repair(item),command_id='no-policy')
        self.assertEqual('EXCEPTION_REPAIR_LOCAL_POLICY_REQUIRED',rejected['code'])
        self.policy()
        rejected=resolve(self.ledger,self.coord,self.repair(item,affected_paths=['unaccepted.py']),command_id='outside')
        self.assertEqual('EXCEPTION_REPAIR_MUST_STAY_IN_ORIGINAL_SCOPE',rejected['code'])
        self.assertEqual(before,self.ledger.snapshot()['tasks'])
        self.assertEqual('AWAITING_COORDINATOR_DISPOSITION',facts(self.ledger.snapshot(),'mysql-run')[-1]['state'])

    def test_cross_task_supplier_still_waits_for_original_verification_writer(self):
        _,tester,item=self.uncertain()
        # Isolated, explicitly synthetic disposition tests the cross-task wait
        # fence. It does not represent a real coordinator business ruling.
        with self.ledger._connection() as db:
            self.ledger._event(db,'VERIFICATION_EXCEPTION_DISPOSITION_RECORDED',{
                'observation_event_id':item['observation_event_id'],'disposition':'repair',
                'task_id':'isolated-other-supplier','source_work_id':tester['work_id'],
                'isolated_synthetic_wait_fixture':True},run_id='mysql-run',role='COORDINATOR')
        with mock.patch('workflow_verification_exceptions._repair_context',side_effect=AssertionError('source writer is live')):
            self.assertEqual([],reconcile(self.ledger))

    def test_native_recovery_is_idempotent_and_does_not_reexecute_runner(self):
        from runtime_common import read_json
        self.assertCommitted(request(self.ledger,self.target,self.source,command_id='recover-request'))
        proc,tester=self.bind('TESTER')
        program='import pathlib,sys;pathlib.Path(sys.argv[1]).write_text(\'<testsuite tests="1" skipped="1"><testcase name="missing"><skipped/></testcase></testsuite>\')'
        # Simulate a crash at the pre-existing gap: result persisted, handoff absent.
        with mock.patch('workflow_verification_exceptions.record',return_value={'status':'UNVERIFIED'}):
            execute_candidate_tests(self.ledger,tester,self.cid,[sys.executable,'-c',program,'{junit_xml}'],
                command_id='legacy-result',report_format='junit',verification_profile='mysql_migration')
        self.assertEqual([],facts(self.ledger.snapshot(),'mysql-run'))
        with mock.patch('delivery_evidence.execute_with_evidence',side_effect=AssertionError('reexecuted')):
            recovered=recover(self.ledger);self.assertEqual(1,len(recovered));self.assertEqual([],recover(self.ledger))
        self.assertEqual('UNVERIFIED',facts(self.ledger.snapshot(),'mysql-run')[0]['result'])

    def test_uncertainty_cannot_be_closed_by_old_independent_pass(self):
        _,_,item=self.uncertain()
        old=self.ledger.snapshot()['verifications'][0]['verification_id']
        result=resolve(self.ledger,self.coord,{'observation_event_id':item['observation_event_id'],
            'disposition':'close','reason':'must not pass','verification_id':old},command_id='fake-closure')
        self.assertEqual('EXCEPTION_CLOSURE_REQUIRES_REAL_SUPPLEMENTAL_PASS',result['code'])
        self.assertTrue(facts(self.ledger.snapshot(),'mysql-run')[0]['blocking'])

    def test_wait_chain_has_named_owner_and_registered_next_action(self):
        _,_,item=self.uncertain();snapshot=self.ledger.snapshot()
        packet={'role':'COORDINATOR','work':self.ledger.current_work('COORDINATOR'),
            'run':snapshot['runs'][-1],'selected_run_id':'mysql-run','tasks':snapshot['tasks'],
            'candidates':snapshot['candidates'],'verifications':snapshot['verifications'],'external_actions':[]}
        actions=next_actions(packet,self.ledger,snapshot=snapshot)
        self.assertEqual('resolve_verification_exception',actions[0]['action'])
        problem=blockers(snapshot,'mysql-run')[0]
        self.assertEqual('COORDINATOR',problem['owner']);self.assertFalse(problem['requires_user_approval'])
        self.assertIn('COORDINATOR:resolve_verification_exception',problem['wait_chain'])

    def test_investigation_due_wakes_once_even_with_external_wait_checkpoint(self):
        _,_,item=self.uncertain()
        ruling=self.assertCommitted(resolve(self.ledger,self.coord,{
            'observation_event_id':item['observation_event_id'],'disposition':'investigate',
            'reason':'inspect isolated runner artifacts','next_action':'inspect the frozen repetition mismatch'},
            command_id='investigate'))
        self.assertEqual('INVESTIGATING',ruling['state'])
        self.assertCommitted(self.ledger.checkpoint(self.coord,{'waiting_external':['legacy external wait']},
            command_id='waiting'))
        self.close(self.cp,self.coord)
        self.now+=301
        hint=self.ledger.admission_hint('COORDINATOR')
        self.assertEqual('VERIFICATION_EXCEPTION_REVIEW_DUE',hint['reason'])
        self.assertEqual([item['observation_event_id']],hint['observation_event_ids'])
        proc,coord=self.bind('COORDINATOR')
        self.close(proc,coord)
        self.assertNotEqual('VERIFICATION_EXCEPTION_REVIEW_DUE',self.ledger.admission_hint('COORDINATOR')['reason'])

    def test_revoked_policy_becomes_a_named_blocker_without_partial_attempt(self):
        proc,tester,item=self.uncertain();policy=self.policy()
        self.assertCommitted(resolve(self.ledger,self.coord,self.repair(item),command_id='will-revoke'))
        self.close(proc,tester);retire(self.ledger)
        with self.ledger._connection() as db:
            db.execute('UPDATE authorizations SET revoked_at=? WHERE authorization_id=?',(self.now,policy['authorization_id']))
        results=reconcile(self.ledger)
        self.assertEqual('EXCEPTION_REPAIR_LOCAL_POLICY_REQUIRED',results[0]['code'])
        self.assertEqual('BLOCKED_REFRESH_REQUIRED',facts(self.ledger.snapshot(),'mysql-run')[0]['state'])
        self.assertEqual([],reconcile(self.ledger))
        self.assertEqual(1,next(t for t in self.ledger.snapshot()['tasks'] if t['task_id']=='MN-01')['attempt'])

    def test_real_repair_revalidated_and_explicitly_closed_retaining_original_unknown(self):
        # Include the fixed profile test in this original accepted test fixture's
        # scope, before invoking repair; no production scope is changed.
        from business_ledger import _json
        path='tests/test_mn01_migration.py'
        with self.ledger._connection() as db:
            db.execute('UPDATE tasks SET scope=? WHERE run_id=? AND task_id=?',
                (_json(['app.py',path]),'mysql-run','MN-01'))
        proc,tester,item=self.uncertain();self.policy()
        self.assertCommitted(resolve(self.ledger,self.coord,self.repair(item,affected_paths=[path]),command_id='repair-disposition'))
        self.close(proc,tester);retire(self.ledger);self.assertTrue(reconcile(self.ledger)[0]['queued'])
        dp,dev=self.bind('A');clone=self.path.parent/'workspaces'/dev['work_id']
        subprocess.run(['git','clone','-q','--no-hardlinks',str(self.repo),str(clone)],check=True)
        self.git('config','user.name','Repair',cwd=clone);self.git('config','user.email','test@example.invalid',cwd=clone)
        (clone/'tests').mkdir();(clone/path).write_text('# repaired fixture\n')
        self.git('add',path,cwd=clone);self.git('commit','-qm','fix fixture',cwd=clone)
        proof=self.root/'repair-proof';proof.write_text('isolated self-check')
        candidate=self.assertCommitted(self.ledger.deliver(dev,{'repo':str(clone),'base':self.git('rev-parse','HEAD'),
            'head':self.git('rev-parse','HEAD',cwd=clone),'ac_version':'ac-sha256-frozen-v1',
            'evidence':[{'path':str(proof)}]},'fixture repair',command_id='deliver-repair'))['candidate_id']
        self.close(dp,dev)
        tp,tester=self.bind('TESTER')
        self.assertCommitted(execute_candidate_tests(self.ledger,tester,candidate,[sys.executable,'-c',CHECK,'{junit_xml}','1'],
            command_id='repair-independent',report_format='junit'))
        self.close(tp,tester)
        self.assertCommitted(self.ledger.decide(self.coord,candidate,'APPROVE',command_id='repair-approve'))
        permit=self.assertCommitted(authorization(self.ledger,self.coord,candidate))
        self.assertCommitted(merge_candidate(self.ledger,self.coord,candidate,permit['authorization_id'],command_id='repair-merge'))
        self.assertCommitted(complete(self.ledger,self.coord,candidate,'repair integrated',command_id='repair-complete'))
        queued=self.assertCommitted(queue_repairs(self.ledger));self.assertEqual(1,len(queued['queued']))
        tp,tester=self.bind('TESTER');work=self.ledger.current_work('TESTER');context=get_context(self.ledger,work)
        self.assertEqual([item['observation_event_id']],context['exception_event_ids']);self.assertEqual([],context['supersedes'])
        self.assertEqual('VERIFIED',summary(self.ledger.snapshot(),'mysql-run')[-1]['context_state'])
        checked=self.assertCommitted(execute_candidate_tests(self.ledger,tester,candidate,[sys.executable,'-c',CHECK,'{junit_xml}','1'],
            command_id='repair-supplemental',report_format='junit',verification_profile='mysql_migration'))
        self.close(tp,tester)
        self.assertEqual('COMMITTED',checked['publication']['status'])
        verification=checked['publication']['verification_id']
        options=closure_passes(self.ledger.snapshot(),facts(self.ledger.snapshot(),'mysql-run')[0])
        self.assertEqual([verification],[p['verification_id'] for p in options])
        # Later current PASS options also apply to an older repair observation.
        historical={**item,'repair':{'task_id':'MN-01','attempt':0}}
        self.assertEqual(options,closure_passes(self.ledger.snapshot(),historical))
        self.assertCommitted(resolve(self.ledger,self.coord,{'observation_event_id':item['observation_event_id'],
            'disposition':'close','verification_id':verification,'reason':'read new real supplemental proof'},command_id='close-exception'))
        current=facts(self.ledger.snapshot(),'mysql-run')[0]
        self.assertEqual('CLOSED',current['state']);self.assertEqual('UNVERIFIED',current['result']);self.assertFalse(current['blocking'])


# Prevent the fixture parent's tests from running a second time in this module.
for _name in tuple(name for name in vars(SupplementalVerificationTests) if name.startswith('test_')):
    setattr(VerificationExceptionTests,_name,None)
