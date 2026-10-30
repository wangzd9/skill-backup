"""Native request -> existing TESTER -> frozen supplemental result, real Git/DB."""
import sys
import sqlite3
import subprocess
from contextlib import contextmanager
from unittest import mock

from tests.test_business_ledger import LedgerFixture
from tests.test_workflow_merge import CHECK
from delivery_evidence import execute_candidate_tests
from workflow_merge import merge_candidate
from workflow_delivery import complete
from workflow_supplemental import get_context, request, source_task, summary, verify
from workflow_progression import next_actions
import workflow_runtime as workflow


class SupplementalVerificationTests(LedgerFixture):
    def setUp(self):
        super().setUp()
        self.assertCommitted(self.ledger.create_run('mysql',self.root,self.repo,['MN-01','MN-04'],run_id='mysql-run',command_id='mysql-run'))
        self.assertCommitted(self.ledger.select_run('mysql-run',command_id='select-mysql'))
        cp,coord=self.bind('COORDINATOR')
        self.assertCommitted(self.ledger.publish_plan('mysql-run',1,[self.task('MN-01','A')],identity=coord,command_id='mysql-plan'))
        self.close(cp,coord)
        dp,dev=self.bind('A')
        clone=self.path.parent/'workspaces'/dev['work_id'];clone.parent.mkdir(parents=True)
        subprocess.run(['git','clone','-q','--no-hardlinks',str(self.repo),str(clone)],check=True)
        self.git('config','user.name','Supplemental Test',cwd=clone)
        self.git('config','user.email','test@example.invalid',cwd=clone)
        (clone/'app.py').write_text('def value():\n    return 1\n')
        self.git('add','app.py',cwd=clone);self.git('commit','-qm','candidate',cwd=clone)
        receipt=self.root/'dev.txt';receipt.write_text('development checks')
        self.cid=self.assertCommitted(self.ledger.deliver(dev,{'repo':str(clone),
            'base':self.base,'head':self.git('rev-parse','HEAD',cwd=clone),
            'ac_version':'ac-sha256-frozen-v1','evidence':[{'path':str(receipt)}]},
            'isolated candidate',command_id='deliver'))['candidate_id']
        self.close(dp,dev)
        tp,tester=self.bind('TESTER')
        self.assertCommitted(execute_candidate_tests(self.ledger,tester,self.cid,
            [sys.executable,'-c',CHECK,'{junit_xml}','1'],command_id='first-check',report_format='junit'))
        self.close(tp,tester)
        self.cp,self.coord=self.bind('COORDINATOR')
        source={'authenticated':True,'current_full':True,'message_id':'merge','user_id':'u','primary_text':'授权当前固定候选'}
        grant=self.assertCommitted(self.ledger.grant_merge_authorization({'run_id':'mysql-run','task_id':'MN-01',
            'attempt':1,'repo':str(self.repo),'branch':self.git('symbolic-ref','--short','HEAD'),'base':self.base,
            'ac_version':'ac-sha256-frozen-v1','expires_at':self.now+3600},source,command_id='grant'))
        self.assertCommitted(self.ledger.decide(self.coord,self.cid,'APPROVE',command_id='approve'))
        self.assertCommitted(merge_candidate(self.ledger,self.coord,self.cid,grant['authorization_id'],command_id='merge'))
        self.assertCommitted(complete(self.ledger,self.coord,self.cid,'real completed migration fixture',command_id='complete'))
        self.source={'authenticated':True,'current_full':False,'message_id':'native-mysql-owner','user_id':'u','chat_id':'c',
            'primary_text':'MN-01 迁移 MySQL 双向补验归属 TESTER 隔离 MySQL 会话'}
        self.target={'run_id':'mysql-run','task_id':'MN-01','profile':'mysql_migration'}

    def test_plain_native_request_is_durable_deduplicated_and_claimed_without_new_permission(self):
        self.assertFalse(self.ledger.admission_hint('TESTER')['can_start_model'])
        with mock.patch('workflow_runtime.open_ledger',return_value=self.ledger):
            result=self.assertCommitted(workflow.submit_control(self.root/'pool.json','COORDINATOR',
                'request_supplemental_verification',self.target,self.source))
            again=workflow.submit_control(self.root/'pool.json','COORDINATOR',
                'request_supplemental_verification',self.target,self.source)
        self.assertEqual('DUPLICATE',again['status'])
        self.assertEqual('SUPPLEMENTAL_VERIFICATION_READY',self.ledger.admission_hint('TESTER')['reason'])
        self.assertIsNone(self.ledger.current_work('TESTER'))
        tp,tester=self.bind('TESTER')
        self.assertEqual(result['work_id'],tester['work_id'])
        before=self.ledger.current_work('TESTER')
        other=self.assertCommitted(request(self.ledger,self.target,{**self.source,'message_id':'repeat'},command_id='repeat'))
        self.assertTrue(other['already_requested'])
        self.assertEqual(before,self.ledger.current_work('TESTER'))
        self.assertTrue(self.ledger.validate_identity(tester)['valid'])
        actions=next_actions({'role':'TESTER','work':before,'selected_run_id':'mysql-run'},self.ledger)
        self.assertEqual('verify_supplemental',actions[0]['action'])
        with self.assertRaisesRegex(ValueError,'SUPPLEMENTAL_PROFILE_ENTRY_REQUIRED'):
            execute_candidate_tests(self.ledger,tester,self.cid,[sys.executable,'-c',CHECK,'{junit_xml}','1'],
                                    command_id='generic-bypass',report_format='junit')

    def test_result_is_a_separate_check_and_does_not_reopen_completed_task(self):
        self.assertCommitted(request(self.ledger,self.target,self.source,command_id='request'))
        tp,tester=self.bind('TESTER')
        result=self.assertCommitted(execute_candidate_tests(self.ledger,tester,self.cid,
            [sys.executable,'-c',CHECK,'{junit_xml}','1'],command_id='profile-run',report_format='junit',
            verification_profile='mysql_migration'))
        self.assertEqual('supplemental_mysql',self.ledger.snapshot()['verifications'][-1]['check_name'])
        self.assertEqual('COMPLETED',next(t for t in self.ledger.snapshot()['tasks'] if t['task_id']=='MN-01')['phase'])
        self.assertEqual('ENDED',next(w for w in self.ledger.snapshot()['works'] if w['work_id']==tester['work_id'])['state'])
        self.close(tp,tester)
        self.assertFalse(self.ledger.admission_hint('TESTER')['can_start_model'])

    def test_merged_descendant_repair_automatically_revalidates_and_closes_original_failure(self):
        from workflow_supplemental import queue_repairs
        self.assertCommitted(request(self.ledger,self.target,self.source,command_id='request'))
        tp,tester=self.bind('TESTER')
        self.assertCommitted(execute_candidate_tests(self.ledger,tester,self.cid,
            [sys.executable,'-c',CHECK,'{junit_xml}','2'],command_id='mysql-fail',report_format='junit',
            verification_profile='mysql_migration'))
        self.close(tp,tester)
        failure=self.ledger.snapshot()['verifications'][-1]['verification_id']
        oldhead=self.git('rev-parse','HEAD');path='tests/test_mn01_migration.py'
        original=self.task('MN-01','A')
        repair={**self.task('MN-04','A',scope=[path]),'base':oldhead}
        self.assertCommitted(self.ledger.publish_plan('mysql-run',2,[original,repair],identity=self.coord,command_id='repair-plan'))
        dp,dev=self.bind('A');clone=self.path.parent/'workspaces'/dev['work_id']
        subprocess.run(['git','clone','-q','--no-hardlinks',str(self.repo),str(clone)],check=True)
        self.git('config','user.name','Repair',cwd=clone);self.git('config','user.email','test@example.invalid',cwd=clone)
        (clone/'tests').mkdir();(clone/path).write_text('# actual repair fixture\n')
        self.git('add',path,cwd=clone);self.git('commit','-qm','repair helper',cwd=clone)
        proof=self.root/'repair.txt';proof.write_text('repair self checks')
        cid=self.assertCommitted(self.ledger.deliver(dev,{'repo':str(clone),'base':oldhead,
            'head':self.git('rev-parse','HEAD',cwd=clone),'ac_version':'ac-sha256-frozen-v1',
            'evidence':[{'path':str(proof)}]},'repair',command_id='repair-deliver'))['candidate_id']
        self.close(dp,dev)
        self.assertEqual([],queue_repairs(self.ledger)['queued']) # never test live/unmerged code
        tp,tester=self.bind('TESTER')
        self.assertCommitted(execute_candidate_tests(self.ledger,tester,cid,
            [sys.executable,'-c',CHECK,'{junit_xml}','1'],command_id='repair-check',report_format='junit'))
        self.close(tp,tester)
        grant=self.assertCommitted(self.ledger.grant_merge_authorization({'run_id':'mysql-run','task_id':'MN-04',
            'attempt':1,'repo':str(self.repo),'branch':self.git('symbolic-ref','--short','HEAD'),'base':oldhead,
            'ac_version':'ac-sha256-frozen-v1','expires_at':self.now+3600},
            {'authenticated':True,'current_full':True,'message_id':'repair-grant','user_id':'u',
             'primary_text':'授权当前固定候选'},command_id='repair-grant'))
        self.assertCommitted(self.ledger.decide(self.coord,cid,'APPROVE',command_id='repair-approve'))
        self.assertCommitted(merge_candidate(self.ledger,self.coord,cid,grant['authorization_id'],command_id='repair-merge'))
        self.assertCommitted(complete(self.ledger,self.coord,cid,'repaired helper integrated',command_id='repair-complete'))
        queued=self.assertCommitted(queue_repairs(self.ledger));self.assertEqual(1,len(queued['queued']))
        self.assertEqual([],queue_repairs(self.ledger)['queued'])
        # The older revalidation event did not carry a profile field; the root
        # request event is the only allowed source for recovery.
        with self.ledger._connection() as db:
            db.execute('DROP TRIGGER work_contexts_no_update')
            db.execute('DROP TRIGGER work_contexts_no_delete')
            db.execute('DROP TABLE work_contexts')
        tp,tester=self.bind('TESTER');work=self.ledger.current_work('TESTER')
        context=get_context(self.ledger,work)
        self.assertEqual(cid,work['candidate_id']);self.assertEqual([failure],context['supersedes'])
        displayed=next(row for row in summary(self.ledger.snapshot(),'mysql-run') if row['work_id']==work['work_id'])
        self.assertEqual('VERIFIED',displayed['context_state'])
        self.assertEqual([failure],displayed['supersedes'])
        self.assertCommitted(execute_candidate_tests(self.ledger,tester,cid,
            [sys.executable,'-c',CHECK,'{junit_xml}','1'],command_id='repair-mysql-pass',report_format='junit',
            verification_profile='mysql_migration',supersedes=context['supersedes']))
        self.close(tp,tester)
        self.assertEqual([],self.ledger.acceptance(self.cid)['open_failure_ids'])
        self.assertEqual([],queue_repairs(self.ledger)['queued'])

    def test_checkpoint_does_not_replace_protected_supplemental_context(self):
        self.assertCommitted(request(self.ledger,self.target,self.source,command_id='request-context'))
        _,tester=self.bind('TESTER')
        current=self.ledger.current_work('TESTER')
        before=get_context(self.ledger,current)
        displayed=next(row for row in summary(self.ledger.snapshot(),'mysql-run') if row['work_id']==current['work_id'])
        self.assertEqual('VERIFIED',displayed['context_state'])
        self.assertCommitted(self.ledger.checkpoint(tester,{'summary':'checkpoint body only'},command_id='ordinary-checkpoint'))
        after_work=self.ledger.current_work('TESTER')
        after=get_context(self.ledger,after_work)
        self.assertEqual(before,after)
        self.assertNotIn('profile',after_work['checkpoint'])
        self.assertEqual(self.cid,after['candidate_id'])
        with self.ledger._connection() as db:
            with self.assertRaisesRegex(sqlite3.IntegrityError,'WORK_CONTEXT_IMMUTABLE'):
                db.execute("UPDATE work_contexts SET context='{}' WHERE work_id=?",(after_work['work_id'],))

        @contextmanager
        def mysql_session(_directory):
            yield ('isolated-test-url',self.root/'mysql-ready.json')

        with mock.patch('workflow_supplemental.mysql_session',mysql_session), \
             mock.patch('delivery_evidence.execute_candidate_tests',return_value={'status':'COMMITTED'}) as execute:
            self.assertEqual({'status':'COMMITTED'},verify(self.ledger,tester,command_id='verify-protected-context'))
        self.assertEqual('mysql_migration',execute.call_args.kwargs['verification_profile'])
        self.assertEqual(self.cid,execute.call_args.args[2])

    def test_legacy_supplemental_context_recovers_from_original_request_event(self):
        self.assertCommitted(request(self.ledger,self.target,self.source,command_id='request-legacy'))
        _,tester=self.bind('TESTER')
        self.assertCommitted(self.ledger.checkpoint(tester,{'summary':'legacy replacement checkpoint'},command_id='legacy-checkpoint'))
        with self.ledger._connection() as db:
            db.execute('DROP TRIGGER work_contexts_no_update')
            db.execute('DROP TRIGGER work_contexts_no_delete')
            db.execute('DROP TABLE work_contexts')
        work=self.ledger.current_work('TESTER')
        legacy_reader=type(self.ledger)(self.path,readonly=True,clock=lambda:self.now)
        self.assertEqual([],legacy_reader.snapshot()['work_contexts'])
        recovered=get_context(legacy_reader,work)
        self.assertEqual('mysql_migration',recovered['profile'])
        self.assertEqual(self.cid,recovered['candidate_id'])
        self.assertEqual({'summary':'legacy replacement checkpoint'},work['checkpoint'])

        @contextmanager
        def mysql_session(_directory):
            yield ('isolated-test-url',self.root/'mysql-ready.json')

        with mock.patch('workflow_supplemental.mysql_session',mysql_session), \
             mock.patch('delivery_evidence.execute_candidate_tests',return_value={'status':'COMMITTED'}) as execute:
            self.assertEqual({'status':'COMMITTED'},verify(self.ledger,tester,command_id='verify-legacy-context'))
        self.assertEqual('mysql_migration',execute.call_args.kwargs['verification_profile'])

    def test_quotes_questions_negation_unknown_task_profile_and_production_urls_cannot_request(self):
        for text in ('请问怎么做？','不要MN-01 迁移 MySQL 双向补验归属 TESTER 隔离 MySQL 会话',
                     '> '+self.source['primary_text'],self.source['primary_text']+' mysql://production'):
            self.assertIsNone(source_task(text))
        for i,target in enumerate(({**self.target,'profile':'production'}, {**self.target,'task_id':'MN-02'},
                                  {**self.target,'url':'mysql://production'}, {**self.target,'run_id':'run-one'})):
            self.assertEqual('REJECTED',request(self.ledger,target,self.source,command_id='bad'+str(i))['status'])
        self.assertFalse(self.ledger.admission_hint('TESTER')['can_start_model'])

    def test_skipped_supplement_is_unverified_and_never_published_as_mysql_pass(self):
        self.assertCommitted(request(self.ledger,self.target,self.source,command_id='request'))
        _,tester=self.bind('TESTER')
        program='import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(\'<testsuite tests="2" skipped="1"><testcase name="real"/><testcase name="missing"><skipped/></testcase></testsuite>\')'
        result=execute_candidate_tests(self.ledger,tester,self.cid,[sys.executable,'-c',program,'{junit_xml}'],
            command_id='skip',report_format='junit',verification_profile='mysql_migration')
        self.assertEqual('UNVERIFIED',result['status'])
        self.assertIn('REAL_MYSQL_EXECUTION_REQUIRED',result['test_stats']['reasons'])
        self.assertFalse(any(v['check_name']=='supplemental_mysql' for v in self.ledger.snapshot()['verifications']))
