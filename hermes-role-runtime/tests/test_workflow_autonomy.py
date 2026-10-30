"""Incident regressions with real SQLite, Git, processes and isolated test execution."""
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import subprocess
import unittest
from unittest import mock

from test_business_ledger import LedgerFixture
from test_workflow_evidence import WorkflowEvidenceTests
from test_workflow_services import ExternalService
from workflow_services import submit_service, collect_service_result, validate_native_action
from workflow_autonomy import extend_scope, sweep, select_grant


class AutonomyTests(LedgerFixture):
    def test_native_requirement_phrasing_keeps_queries_and_negative_instructions_out(self):
        from workflow_intake import explicit_development
        for text in ('开发筛选功能，请自动推进','我需要实现筛选功能','能否帮我修复筛选并自动推进',
                     '我有一个需求：车辆筛选。请实现并自动推进。','新增需求：筛选。请自动推进并交付。'):
            self.assertTrue(explicit_development(text),text)
        for text in ('怎么开发筛选功能？','如果开发筛选会怎样？','不要开发这个功能',
                     '我有一个需求：先讨论，不要开发','说明里写着“请开发”，这是什么意思？'):
            self.assertFalse(explicit_development(text),text)

    def test_prepared_requirement_cannot_turn_negative_merge_consent_into_policy(self):
        from workflow_intake import accept
        branch=self.git('symbolic-ref','--short','HEAD')
        (self.root/'TODO.md').write_text('TNEW: value()==1')
        target={'requirement_id':'new-feature','requirement_root':str(self.root),'repo':str(self.repo),
                'task_ids':['TNEW'],'contract_files':['TODO.md'],'merge_branch':branch}
        source={'authenticated':True,'current_full':False,'message_id':'negated-merge','user_id':'u',
                'primary_text':'开发 new-feature，自动推进，但不要合入 '+branch}
        with self.assertRaisesRegex(ValueError,'MERGE_BOUNDARY_MUST_BE_UNAMBIGUOUS'):
            accept(self.ledger,target,source,command_id='negative-merge')
        self.assertFalse(self.ledger.snapshot()['authorizations'])

    def test_blocked_native_status_is_collectable_but_blocked_start_is_rejected(self):
        from workflow_services import NativeServices
        script=self.root/'blocked_service.py'
        script.write_text('import json;print(json.dumps({"state":"BLOCKED"}))')
        native=NativeServices();native.scripts=self.root
        self.assertEqual('BLOCKED',native._cli(script.name,['status'])['state'])
        with self.assertRaisesRegex(RuntimeError,'SUBMISSION_UNCONFIRMED'):
            native._cli(script.name,['start'])

    def grant(self, **changes):
        spec={'authorization_id':'run-policy','run_id':'run-one','task_ids':['T1','T2'],
              'service_kind':'ocr','max_calls':2,'expires_at':self.now+60,'execution_seconds':3600,**changes}
        return self.ledger.grant_external_authorization(spec,{'authenticated':True,'current_full':True,
            'message_id':'native-policy','user_id':'u','primary_text':'授权本需求全部任务OCR最多两次'},command_id='policy')

    def submitted(self):
        process,dev=self.bind('A'); cid,_=self.candidate(dev);self.close(process,dev)
        _,coord=self.bind('COORDINATOR')
        self.assertCommitted(self.grant())
        adapter=ExternalService(self.root)
        result=self.assertCommitted(submit_service(self.ledger,coord,cid,'ocr',{'background_text':'固定AC背景'},
            action_id='ocr',authorization=None,command_id='submit',adapter=adapter))
        return coord,cid,adapter,result

    def test_admission_expiry_does_not_kill_committed_batches_but_revoke_does(self):
        _,cid,adapter,result=self.submitted()
        path=self.path.parent/'services'/hashlib.sha256(b'ocr').hexdigest()/'invocation.json'
        with mock.patch('workflow_services.time.time',return_value=self.now+120):
            self.assertEqual('ocr',validate_native_action(path,'ocr',{})['action_id'])
        self.now+=120
        with self.assertRaisesRegex(ValueError,'SERVICE_POLICY_OR_EXPLICIT_AUTHORIZATION_REQUIRED'):
            select_grant(self.ledger,self.ledger.snapshot()['candidates'][0],'ocr')
        self.assertCommitted(self.ledger.revoke_authorization('run-policy',{'authenticated':True,'current_full':True,
            'message_id':'revoke','user_id':'u','primary_text':'撤销授权'},command_id='revoke'))
        with mock.patch('workflow_services.time.time',return_value=self.now), self.assertRaisesRegex(ValueError,'AUTHORIZATION_REQUIRED'):
            validate_native_action(path,'ocr',{})

    def test_scope_extension_keeps_original_plan_hash_and_blocks_cross_task_overlap(self):
        _,coord=self.bind('COORDINATOR');_,dev=self.bind('A')
        original=self.ledger.snapshot()['plans'][0]
        changed=self.assertCommitted(extend_scope(self.ledger,coord,'T1',['tests/test_new.py'],
            '固定AC需要新增回归测试',command_id='extend'))
        self.assertIn('tests/test_new.py',changed['scope'])
        self.assertEqual(original,self.ledger.snapshot()['plans'][0])
        self.assertTrue(self.ledger.validate_identity(dev)['valid'])
        denied=extend_scope(self.ledger,coord,'T1',['../outside'], 'bad',command_id='escape')
        self.assertEqual('EXPLICIT_SCOPE_ADDITIONS_AND_REASON_REQUIRED',denied['code'])

    def test_invalid_wait_schema_rejected_and_legacy_repaired_without_ending_owner(self):
        process,dev=self.bind('A')
        rejected=self.ledger.checkpoint(dev,{'waiting_external':'内部工具失败'},command_id='invalid')
        self.assertEqual('INVALID_WAITING_EXTERNAL',rejected['code'])
        with self.ledger._connection() as db:
            db.execute('UPDATE works SET checkpoint=? WHERE work_id=?',
                (json.dumps({'waiting_external':'legacy'}),dev['work_id']))
            db.commit()
        repaired=sweep(self.ledger)
        self.assertEqual(1,len(repaired));self.assertEqual([],sweep(self.ledger))
        current=self.ledger.current_work('A')
        self.assertEqual([],current['checkpoint']['waiting_external'])
        self.assertEqual('legacy',current['checkpoint']['legacy_waiting_external'])
        self.assertTrue(self.ledger.validate_identity(dev)['valid'])

    def test_natural_continue_is_scoped_control_without_full_tool_authority(self):
        from workflow_runtime import submit_control
        source={'authenticated':True,'current_full':False,'message_id':'continue','user_id':'u','chat_id':'group',
                'primary_text':'请继续当前需求'}
        # Fixture has the same canonical pool -> ledger path convention.
        result=submit_control(self.root/'isolated.json','COORDINATOR','continue',{},source)
        self.assertCommitted(result)
        self.assertFalse(self.ledger.snapshot()['controls'][-1]['provenance']['current_full'])
        with self.assertRaises(ValueError):
            submit_control(self.root/'isolated.json','COORDINATOR','continue',{},
                           {**source,'message_id':'negated','primary_text':'不要继续当前需求'})
        with self.assertRaisesRegex(ValueError,'CURRENT_NATIVE_FULL_GRANT_REQUIRED'):
            submit_control(self.root/'isolated.json','COORDINATOR','authorize_service',{},source)

    def test_single_native_acceptance_creates_run_with_frozen_contract_no_switch(self):
        from workflow_intake import accept
        (self.root/'TODO.md').write_text('TASK-NEW: 固定验收：value()==1')
        target={'requirement_id':'new-feature','requirement_root':str(self.root),'repo':str(self.repo),
                'task_ids':['TASK-NEW'],'contract_files':['TODO.md']}
        source={'authenticated':True,'current_full':True,'message_id':'new','user_id':'u',
                'primary_text':'全部权限：开发 new-feature 并交付'}
        accepted=self.assertCommitted(accept(self.ledger,target,source,command_id='new-requirement'))
        self.assertFalse(accepted['selected']);self.assertEqual('run-one',self.ledger.mode()['current_run_id'])
        self.assertEqual(hashlib.sha256((self.root/'TODO.md').read_bytes()).hexdigest(),accepted['sources'][0]['sha256'])
        self.assertEqual('DUPLICATE',accept(self.ledger,target,source,command_id='new-requirement')['status'])


class FrozenEnvironmentTests(WorkflowEvidenceTests):
    def pytest_candidate(self, text):
        # Build a fresh frozen candidate, with an intentionally conflicting host PYTHONPATH.
        (self.repo/'test_selected.py').write_text(text)
        self.git('add','.');self.git('commit','-qm','test contract')
        head=self.git('rev-parse','HEAD').strip()
        with self.ledger._connection() as db:
            db.execute('UPDATE candidates SET head=? WHERE candidate_id=?',(head,self.candidate));db.commit()
        return [sys.executable,'-m','pytest','test_selected.py','-q']

    def test_frozen_pytest_repeats_consistently_despite_host_env_changes(self):
        from delivery_evidence import execute_candidate_tests
        # Separate test sessions are needed because published evidence ends TESTER work.
        argv=self.pytest_candidate('def test_ok():\n    assert 1+1==2\n')
        with mock.patch.dict(os.environ,{'PYTHONPATH':'/nonexistent','PYTEST_ADDOPTS':'--does-not-exist'}):
            result=execute_candidate_tests(self.ledger,self.tester,self.candidate,argv,command_id='pytest-real')
        self.assertEqual('PASS',result['test_stats']['mechanical_result'],result)
        self.assertTrue(result['test_stats']['stability_match'])
        self.assertEqual(2,result['test_stats']['stability_runs'])
        environment=Path(result['runner_evidence']['path']).parent/'environment.json'
        self.assertEqual('READY',json.loads(environment.read_text())['state'])

    def test_pytest_collection_error_is_not_code_fail(self):
        from delivery_evidence import execute_candidate_tests
        argv=self.pytest_candidate('import dependency_that_is_not_installed\n')
        result=execute_candidate_tests(self.ledger,self.tester,self.candidate,argv,command_id='pytest-import')
        self.assertEqual('UNVERIFIED',result['status'])
        self.assertEqual('UNVERIFIED',result['test_stats']['mechanical_result'])
        self.assertEqual([],self.ledger.snapshot()['verifications'])

    def test_missing_plugin_rejected_before_candidate_verification(self):
        from delivery_evidence import execute_candidate_tests
        argv=self.pytest_candidate('def test_ok():\n    assert True\n')+['--definitely-no-such-option']
        result=execute_candidate_tests(self.ledger,self.tester,self.candidate,argv,command_id='pytest-plugin')
        self.assertEqual('TEST_RUNNER_PLUGIN_MISSING',result['code'])
        self.assertFalse(result['repair_candidate'])
        self.assertEqual([],self.ledger.snapshot()['verifications'])


class AutonomousDeliveryTests(LedgerFixture):
    def test_one_native_requirement_drives_real_git_review_test_merge_delivery(self):
        from workflow_intake import accept
        from workflow_merge_policy import authorization
        from workflow_merge import merge_candidate
        from workflow_services import adjudicate_service
        from workflow_integration_recovery import refresh_base
        from delivery_evidence import execute_candidate_tests
        from test_workflow_merge import CHECK
        branch=self.git('symbolic-ref','--short','HEAD')
        (self.root/'NEW.md').write_text('TNEW: app.value()==1; 独立测试与OCR后快进合入')
        source={'authenticated':True,'current_full':False,'message_id':'one-real-native-request','user_id':'u',
                'primary_text':'我有一个需求：new-feature。请实现并自动推进，验收后合入 '+branch+'；不要部署生产。'}
        accepted=self.assertCommitted(accept(self.ledger,{'requirement_id':'new-feature','requirement_root':str(self.root),
            'repo':str(self.repo),'task_ids':['TNEW','TSECOND'],'use_native_requirement':True,'merge_branch':branch},source,command_id='one-request'))
        run=accepted['run_id'];self.assertEqual(run,self.ledger.mode()['current_run_id'])
        cp,coord=self.bind('COORDINATOR')
        self.assertCommitted(self.ledger.publish_plan(run,1,[self.task('TNEW','A',checks=['independent_test','ocr']),
            self.task('TSECOND','B',scope=['extra.py'],checks=['independent_test','ocr'])],
                                                    identity=coord,command_id='auto-plan'))
        original_plan=self.ledger.snapshot(run)['plans'][0]
        bp,bdev=self.bind('B')
        original=self.path.parent/'workspaces'/bdev['work_id'];original.parent.mkdir(parents=True)
        subprocess.run(['git','clone','-q','--no-hardlinks',str(self.repo),str(original)],check=True)
        self.git('config','user.name','Parallel Fixture',cwd=original);self.git('config','user.email','fixture@localhost',cwd=original)
        (original/'extra.py').write_text('EXTRA = 2\n')
        self.git('add','extra.py',cwd=original);self.git('commit','-qm','parallel second task',cwd=original)
        second_head=self.git('rev-parse','HEAD',cwd=original)
        second_proof=self.root/'second-proof.txt';second_proof.write_text('original parallel delivery')
        dp,dev=self.bind('A')
        clone=self.path.parent/'workspaces'/dev['work_id'];clone.parent.mkdir(parents=True,exist_ok=True)
        subprocess.run(['git','clone','-q','--no-hardlinks',str(self.repo),str(clone)],check=True)
        self.git('config','user.name','Autonomy Fixture',cwd=clone);self.git('config','user.email','fixture@localhost',cwd=clone)
        (clone/'app.py').write_text('def value():\n    return 1\n')
        self.git('add','app.py',cwd=clone);self.git('commit','-qm','deliver',cwd=clone)
        head=self.git('rev-parse','HEAD',cwd=clone)
        proof=self.root/'dev-proof.txt';proof.write_text('actual fixed candidate')
        cid=self.assertCommitted(self.ledger.deliver(dev,{'repo':str(clone),'base':self.base,'head':head,
            'ac_version':'ac-sha256-frozen-v1','evidence':[{'path':str(proof)}]},'fixed',command_id='deliver'))['candidate_id']
        self.close(dp,dev)
        adapter=ExternalService(self.root)
        self.assertCommitted(submit_service(self.ledger,coord,cid,'ocr',{'background_text':'value()==1'},
            action_id='automatic-ocr',authorization=None,command_id='automatic-ocr',adapter=adapter))
        self.assertCommitted(collect_service_result(self.ledger,'automatic-ocr',command_id='collect',adapter=adapter))
        self.assertCommitted(adjudicate_service(self.ledger,coord,'automatic-ocr','PASS',
            'isolated advisory finding inspected against fixed fixture AC',command_id='judge'))
        tp,tester=self.bind('TESTER')
        self.assertCommitted(execute_candidate_tests(self.ledger,tester,cid,[sys.executable,'-c',CHECK,'{junit_xml}','1'],
            command_id='independent',report_format='junit'))
        self.close(tp,tester)
        second_cid=self.assertCommitted(self.ledger.deliver(bdev,{'repo':str(original),'base':self.base,'head':second_head,
            'ac_version':'ac-sha256-frozen-v1','evidence':[{'path':str(second_proof)}]},'parallel delivery',command_id='second-original'))['candidate_id']
        self.close(bp,bdev)
        self.assertCommitted(self.ledger.decide(coord,cid,'APPROVE',command_id='approve'))
        from workflow_delivery import complete
        denied=complete(self.ledger,coord,cid,'not yet merged',command_id='premature-complete')
        self.assertEqual('AUTHORIZED_MERGE_AND_POSTMERGE_EVIDENCE_REQUIRED',denied['code'])
        permit=self.assertCommitted(authorization(self.ledger,coord,cid))
        self.assertCommitted(merge_candidate(self.ledger,coord,cid,permit['authorization_id'],command_id='merge'))
        self.assertCommitted(complete(self.ledger,coord,cid,'real isolated Git merge and test receipt',command_id='complete'))
        # The other parallel candidate cannot fast-forward over the first merge.
        # Only audited completed integration permits a base refresh/new attempt.
        refreshed=self.assertCommitted(refresh_base(self.ledger,coord,second_cid,
            '接续第一路已完成合入，保留旧候选并重新验证',command_id='refresh-second'))
        self.assertEqual(head,refreshed['base']);self.assertFalse(refreshed['prior_checks_reused'])
        self.assertEqual(original_plan,self.ledger.snapshot(run)['plans'][0])
        bp,bdev=self.bind('B')
        refreshed_repo=self.path.parent/'workspaces'/bdev['work_id']
        subprocess.run(['git','clone','-q','--no-hardlinks',str(self.repo),str(refreshed_repo)],check=True)
        self.git('config','user.name','Parallel Fixture',cwd=refreshed_repo);self.git('config','user.email','fixture@localhost',cwd=refreshed_repo)
        self.git('fetch','-q',str(original),second_head,cwd=refreshed_repo)
        self.git('cherry-pick',second_head,cwd=refreshed_repo)
        integrated_head=self.git('rev-parse','HEAD',cwd=refreshed_repo)
        second_cid=self.assertCommitted(self.ledger.deliver(bdev,{'repo':str(refreshed_repo),'base':head,
            'head':integrated_head,'ac_version':'ac-sha256-frozen-v1','evidence':[{'path':str(second_proof)}]},
            'same task changes on completed sibling base',command_id='second-integrated'))['candidate_id']
        self.close(bp,bdev)
        self.assertFalse(self.ledger.acceptance(second_cid)['ready'])
        self.assertCommitted(submit_service(self.ledger,coord,second_cid,'ocr',{'background_text':'value()==1; EXTRA==2'},
            action_id='second-ocr',authorization=None,command_id='second-ocr',adapter=adapter))
        self.assertCommitted(collect_service_result(self.ledger,'second-ocr',command_id='second-collect',adapter=adapter))
        self.assertCommitted(adjudicate_service(self.ledger,coord,'second-ocr','PASS',
            'fresh advisory result inspected on integrated candidate',command_id='second-judge'))
        tp,tester=self.bind('TESTER')
        self.assertCommitted(execute_candidate_tests(self.ledger,tester,second_cid,[sys.executable,'-c',CHECK,'{junit_xml}','1'],
            command_id='second-independent',report_format='junit'))
        self.close(tp,tester)
        self.assertCommitted(self.ledger.decide(coord,second_cid,'APPROVE',command_id='second-approve'))
        permit=self.assertCommitted(authorization(self.ledger,coord,second_cid))
        self.assertCommitted(merge_candidate(self.ledger,coord,second_cid,permit['authorization_id'],command_id='second-merge'))
        self.assertCommitted(complete(self.ledger,coord,second_cid,'second task merged after new verification',command_id='second-complete'))
        facts=self.ledger.snapshot(run)
        self.assertTrue(all(task['phase']=='COMPLETED' for task in facts['tasks']))
        self.assertEqual(integrated_head,self.git('rev-parse','HEAD'))
        self.assertEqual(2,len(facts['merges']))
        self.assertEqual(2,adapter.calls)
        self.assertTrue(all(row['provenance']['message_id']=='one-real-native-request' for row in facts['authorizations']))


def load_tests(loader, standard, pattern):
    suite=unittest.TestSuite()
    for cls in (AutonomyTests,FrozenEnvironmentTests,AutonomousDeliveryTests):
        for name in cls.__dict__:
            if name.startswith('test_'):
                suite.addTest(cls(name))
    return suite
