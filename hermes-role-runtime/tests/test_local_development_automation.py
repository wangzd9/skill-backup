"""Real Git, ledger and native-process regressions for autonomous development."""
import json
import sys
from unittest import mock

from tests.test_business_ledger import LedgerFixture
from tests.test_workflow_merge import WorkflowMergeTests
from workflow_development_policy import enable
from workflow_merge_policy import authorization
from workflow_progression import merge_permission, next_actions
from workflow_verification_lifecycle import reconcile


SOURCE={'platform':'codex','source':'current_user_workflow_instruction','authenticated':True,
        'message_id':'actual-maintenance-instruction','primary_text':'取消内部逐项授权，在已接受需求内全自动开发、本地合入并交付。'}


class LocalAutomationTests(LedgerFixture):
    def enable(self):
        return self.assertCommitted(enable(self.ledger,'run-one','feat/autonomous',SOURCE,command_id='enable'))

    def test_policy_covers_all_accepted_tasks_and_attempts_without_renewing_ocr_or_production(self):
        policy=self.enable()
        dp,dev=self.bind('A');cid,_=self.candidate(dev);self.close(dp,dev)
        cp,coord=self.bind('COORDINATOR')
        permit=self.assertCommitted(authorization(self.ledger,coord,cid))
        self.assertEqual(policy['authorization_id'],permit['parent_policy_id'])
        self.assertEqual('ACCEPTANCE_GATES_NOT_CLEAR',self.ledger.merge_readiness(coord,cid,permit['authorization_id'])['code'])
        self.assertCommitted(self.ledger.decide(coord,cid,'REWORK',command_id='rework'))
        dp,dev=self.bind('A');cid2,_=self.candidate(dev,2);self.close(dp,dev)
        self.now+=10*86400
        permit2=self.assertCommitted(authorization(self.ledger,coord,cid2))
        self.assertEqual(policy['authorization_id'],permit2['parent_policy_id'])
        grants=self.ledger.snapshot()['authorizations']
        self.assertFalse(any(g['kind']=='external' for g in grants))
        spec=next(g['spec'] for g in grants if g['authorization_id']==policy['authorization_id'])
        self.assertEqual(['T1','T2','T3'],spec['task_ids'])
        self.assertFalse(spec['production_permission']);self.assertFalse(spec['push_permission'])
        candidate=next(c for c in self.ledger.snapshot()['candidates'] if c['candidate_id']==cid2)
        self.assertTrue(merge_permission(self.ledger.snapshot(),candidate,self.now)['available'])
        revoked={'authenticated':True,'current_full':True,'message_id':'revoke','user_id':'u','primary_text':'撤销授权'}
        self.assertCommitted(self.ledger.revoke_authorization(policy['authorization_id'],revoked,command_id='revoke'))
        self.assertFalse(merge_permission(self.ledger.snapshot(),candidate,self.now)['available'])
        self.assertEqual('PARENT_AUTHORIZATION_REVOKED_OR_EXPIRED',self.ledger.merge_readiness(coord,cid2,permit2['authorization_id'])['code'])

    def test_new_development_request_has_local_branch_without_full_permission_magic(self):
        from workflow_intake import accept
        source={'authenticated':True,'current_full':False,'message_id':'native-new','user_id':'u',
                'primary_text':'开发 new-feature，自动推进并交付本地。'}
        target={'requirement_id':'new-feature','requirement_root':str(self.root),'repo':str(self.repo),
                'task_ids':['TNEW'],'use_native_requirement':True}
        accepted=self.assertCommitted(accept(self.ledger,target,source,command_id='new'))
        self.assertTrue(accepted['development_automation']['branch'].startswith('workflow/new-feature-'))
        self.assertEqual(self.base,self.git('rev-parse','HEAD'))
        policy=next(g for g in self.ledger.snapshot()['authorizations'] if g['kind']=='development_policy')
        self.assertEqual(['TNEW'],policy['spec']['task_ids'])
        # OCR retains its original finite budget and expiry.
        ocr=next(g for g in self.ledger.snapshot()['authorizations'] if g['kind']=='external')
        self.assertEqual(self.now+72*3600,ocr['spec']['expires_at'])

    def test_candidate_only_and_explicit_later_destination_are_respected(self):
        from workflow_intake import accept
        for i,restriction in enumerate(('暂不合入，只交付候选','合入分支由我明确指定')):
            source={'authenticated':True,'current_full':False,'message_id':'native-'+str(i),'user_id':'u',
                    'primary_text':'开发 new-feature'+str(i)+'，'+restriction}
            target={'requirement_id':'new-feature'+str(i),'requirement_root':str(self.root),'repo':str(self.repo),
                    'task_ids':['TNEW'],'use_native_requirement':True}
            accepted=self.assertCommitted(accept(self.ledger,target,source,command_id='new'+str(i)))
            self.assertIsNone(accepted['development_automation'])
        self.assertFalse(any(g['kind']=='development_policy' for g in self.ledger.snapshot()['authorizations']))

    def test_empty_placeholder_cannot_be_delivered_or_claimed_by_tester(self):
        _,dev=self.bind('A');proof=self.root/'proof.txt';proof.write_text('wrong-head tests cannot authorize empty delivery')
        result=self.ledger.deliver(dev,{'repo':str(self.repo),'base':self.base,'head':self.base,
            'ac_version':'ac-sha256-frozen-v1','evidence':[{'path':str(proof)}]},'占位',command_id='placeholder')
        self.assertEqual('EMPTY_DEVELOPMENT_CANDIDATE',result['code'])
        self.assertEqual('DEVELOPING',self.ledger.current_work('A') and self.ledger.snapshot()['tasks'][0]['phase'])
        self.assertFalse(self.ledger.admission_hint('TESTER')['can_start_model'])


class VerificationLifecycleTests(WorkflowMergeTests):
    def legacy_placeholder(self):
        # Actual integration plus historical invalid candidate/work reproduces
        # the production incident. New deliver() refuses this historical shape.
        from workflow_merge import merge_candidate
        from workflow_delivery import complete
        grant=self.grant();self.approve()
        self.assertCommitted(merge_candidate(self.ledger,self.coord,self.candidate_id,grant,command_id='merge'))
        self.assertCommitted(complete(self.ledger,self.coord,self.candidate_id,'integrated',command_id='complete'))
        with self.ledger._connection() as db:
            db.execute("UPDATE tasks SET phase='REVIEW' WHERE run_id='run-one' AND task_id='T2'")
            db.execute("INSERT INTO candidates SELECT 'legacy-placeholder',run_id,'T2',1,role,repo,base,base,ac_version,evidence,'legacy placeholder','CURRENT',created_at FROM candidates WHERE candidate_id=?",(self.candidate_id,))
            task=self.ledger._one(db,'tasks','task_id','T2')
            self.ledger._new_work(db,'run-one','TESTER','verification',task,'legacy-placeholder')
            db.commit()

    def test_live_tester_retires_at_own_safe_point_then_refresh_and_next_development_proceed(self):
        from workflow_integration_recovery import refresh_base
        self.legacy_placeholder()
        tp,tester=self.bind('TESTER')
        first=self.assertCommitted(refresh_base(self.ledger,self.coord,'legacy-placeholder','routine baseline',command_id='refresh-live'))
        self.assertEqual('WAITING_FOR_VERIFICATION_SAFE_EXIT',first['state'])
        self.assertFalse(first['requires_user_approval'])
        self.assertFalse(self.ledger.validate_identity(tester)['valid'])
        self.assertTrue(self.ledger.validate_identity(tester,allow_drain=True)['valid'])
        self.assertEqual('safe_release',next_actions({'role':'TESTER','work':self.ledger.current_work('TESTER')},self.ledger)[0]['action'])
        reconcile(self.ledger)
        self.assertIsNone(tp.poll())
        self.assertEqual(tester['execution_id'],self.ledger.current_work('TESTER')['active_execution_id'])
        self.assertCommitted(self.ledger.release_work(tester,reason='obsolete fixed candidate',command_id='own-release'))
        second=self.assertCommitted(refresh_base(self.ledger,self.coord,'legacy-placeholder','routine baseline',command_id='refresh-draining'))
        self.assertEqual('WAITING_FOR_VERIFICATION_SAFE_EXIT',second['state'])
        self.close(tp,tester)
        reconcile(self.ledger)
        done=self.assertCommitted(refresh_base(self.ledger,self.coord,'legacy-placeholder','routine baseline',command_id='refresh-exited'))
        self.assertEqual(2,done['attempt']);self.assertEqual(self.head,done['base'])
        self.assertFalse(done['prior_checks_reused'])
        _,dev=self.bind('A');self.assertEqual(2,dev['attempt'])
        self.assertEqual(self.head,self.ledger.snapshot()['tasks'][1]['base'])

    def test_exit_without_cooperative_release_is_retired_before_rebinding(self):
        self.legacy_placeholder()
        tp,tester=self.bind('TESTER')
        reconcile(self.ledger)
        self.close(tp,tester)
        self.assertEqual('VERIFICATION_RETIREMENT_SAFE_RELEASE_REQUIRED',self.ledger.admission_hint('TESTER')['reason'])
        reconcile(self.ledger)
        self.assertIsNone(self.ledger.current_work('TESTER'))
        self.assertFalse(self.ledger.admission_hint('TESTER')['can_start_model'])
        self.assertEqual([], [v for v in self.ledger.snapshot()['verifications'] if v['candidate_id']=='legacy-placeholder'])
        self.assertTrue(any(e['kind']=='VERIFICATION_RETIRED' for e in self.ledger.snapshot()['events']))

    def test_tool_children_keep_fence_until_native_clearance(self):
        self.legacy_placeholder()
        tp,tester=self.bind('TESTER')
        child,spec=self.worker()
        self.assertCommitted(self.ledger.register_tool_execution(tester,{'tool_id':'live-test','pid':spec['pid'],'process_start':spec['process_start'],'cgroup':spec['scope']},command_id='tool'))
        reconcile(self.ledger)
        tp.terminate();tp.wait()
        rejected=self.ledger.end_execution(tester,command_id='child-still-live')
        self.assertIn(rejected['code'],('CHILD_WRITER_STILL_ALIVE','WRITER_STILL_ALIVE'))
        reconcile(self.ledger)
        self.assertIsNotNone(self.ledger.current_work('TESTER'))
        child.terminate();child.wait()
        self.assertCommitted(self.ledger.end_execution(tester,command_id='child-exited'))
        reconcile(self.ledger)
        self.assertIsNone(self.ledger.current_work('TESTER'))

    def test_run_policy_integrates_in_managed_worktree_and_preserves_shared_head(self):
        from workflow_merge import merge_candidate
        from workflow_delivery import complete
        policy=self.assertCommitted(enable(self.ledger,'run-one','feat/autonomous',SOURCE,command_id='enable-run'))
        permit=self.assertCommitted(authorization(self.ledger,self.coord,self.candidate_id))
        self.assertEqual(policy['authorization_id'],permit['parent_policy_id'])
        self.approve()
        result=self.assertCommitted(merge_candidate(self.ledger,self.coord,self.candidate_id,permit['authorization_id'],command_id='managed-merge'))
        receipt=json.loads(__import__('pathlib').Path(result['evidence']).read_text())
        self.assertIn('integration_worktree',receipt['frozen'])
        self.assertEqual(self.base,self.git('rev-parse','HEAD'))
        self.assertEqual(self.head,self.git('rev-parse','feat/autonomous'))
        self.assertCommitted(complete(self.ledger,self.coord,self.candidate_id,'managed integration',command_id='complete'))
        _,dev=self.bind('A')
        self.assertEqual(self.head,next(t['base'] for t in self.ledger.snapshot()['tasks'] if t['task_id']=='T2'))

    def test_newly_eligible_task_uses_integrated_base_without_empty_delivery(self):
        from workflow_merge import merge_candidate
        from workflow_delivery import complete
        grant=self.grant();self.approve()
        self.assertCommitted(merge_candidate(self.ledger,self.coord,self.candidate_id,grant,command_id='merge'))
        self.assertCommitted(complete(self.ledger,self.coord,self.candidate_id,'integrated',command_id='complete'))
        _,dev=self.bind('A')
        task=next(t for t in self.ledger.snapshot()['tasks'] if t['task_id']=='T2')
        self.assertEqual(self.head,task['base']);self.assertEqual(1,task['attempt'])
        self.assertEqual('DEVELOPING',task['phase'])

    def test_undelivered_parallel_work_refreshes_at_developer_safe_point(self):
        from workflow_merge import merge_candidate
        from workflow_delivery import complete
        from workflow_integration_recovery import refresh_undelivered
        snapshot=self.ledger.snapshot()
        first=snapshot['plans'][0]['spec'][0]
        second=self.task('T2','B',scope=['extra.py'])
        self.assertCommitted(self.ledger.publish_plan('run-one',2,[first,second],identity=self.coord,command_id='parallel-plan'))
        bp,dev=self.bind('B');old=dev['work_id']
        grant=self.grant();self.approve()
        self.assertCommitted(merge_candidate(self.ledger,self.coord,self.candidate_id,grant,command_id='merge'))
        self.assertCommitted(complete(self.ledger,self.coord,self.candidate_id,'integrated',command_id='complete'))
        wait=self.assertCommitted(refresh_undelivered(self.ledger,self.coord,'T2','integrated dependency',command_id='refresh-live'))
        self.assertEqual('WAITING_FOR_DEVELOPER_SAFE_POINT',wait['state'])
        actions=next_actions({'role':'B','work':self.ledger.current_work('B'),'selected_run_id':'run-one'},self.ledger)
        self.assertEqual('checkpoint',actions[0]['action'])
        self.assertTrue(actions[0]['payload']['yield_execution'])
        self.assertCommitted(self.ledger.checkpoint(dev,{'next':'preserved real source head'},yield_execution=True,command_id='dev-yield'))
        self.close(bp,dev)
        refreshed=self.assertCommitted(refresh_undelivered(self.ledger,self.coord,'T2','integrated dependency',command_id='refresh-idle'))
        self.assertEqual(self.head,refreshed['base'])
        self.assertEqual([old],refreshed['preserved_source_work_ids'])
        _,nextdev=self.bind('B')
        self.assertNotEqual(old,nextdev['work_id'])
        self.assertEqual(1,nextdev['attempt'])


def load_tests(loader, standard, pattern):
    # WorkflowMergeTests provides a fixture, not another copy of its tests.
    suite=loader.loadTestsFromTestCase(LocalAutomationTests)
    for name in VerificationLifecycleTests.__dict__:
        if name.startswith('test_'):
            suite.addTest(VerificationLifecycleTests(name))
    return suite
