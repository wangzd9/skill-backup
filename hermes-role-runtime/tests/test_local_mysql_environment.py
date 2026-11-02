"""本地数据库接入原独立测试通道，保留身份、证据与重放约束。"""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import subprocess
import sys
from unittest import mock

from tests.test_business_ledger import LedgerFixture
from tests.test_workflow_merge import CHECK
from workflow_contracts import validate_payload, test_execution_rules as execution_rules
from workflow_local_environment import mysql_environment, valid_mysql_environment, verify_candidate


class LocalMySQLTests(LedgerFixture):
    def setUp(self):
        super().setUp()
        process, developer = self.bind('A')
        clone = self.path.parent/'workspaces'/developer['work_id']
        clone.parent.mkdir(parents=True)
        subprocess.run(['git','clone','-q','--no-hardlinks',str(self.repo),str(clone)],check=True)
        self.git('config','user.name','Environment Test',cwd=clone)
        self.git('config','user.email','test@example.invalid',cwd=clone)
        (clone/'app.py').write_text('def value():\n    return 1\n')
        self.git('add','app.py',cwd=clone); self.git('commit','-qm','candidate',cwd=clone)
        receipt = self.root/'dev.txt'; receipt.write_text('self test')
        self.cid = self.assertCommitted(self.ledger.deliver(developer,{'repo':str(clone),
            'base':self.base,'head':self.git('rev-parse','HEAD',cwd=clone),
            'ac_version':'ac-sha256-frozen-v1','evidence':[{'path':str(receipt)}]},
            'candidate',command_id='deliver'))['candidate_id']
        self.close(process,developer)
        self.process,self.tester = self.bind('TESTER')
        self.payload = {'candidate_id':self.cid,'profile':'mysql',
            'argv':[sys.executable,'-c',CHECK,'{junit_xml}','1'],'report_format':'junit'}
        self.sessions = 0; self.closed = 0

    def tearDown(self):
        try:
            if hasattr(self, 'tester'):
                self.close(self.process,self.tester)
        finally:
            super().tearDown()

    @contextmanager
    def session(self, directory):
        self.sessions += 1
        directory.mkdir(parents=True,exist_ok=True)
        marker = directory/'ready.json'; marker.write_text('{"state":"READY","version":"8.4.test"}')
        try:
            yield 'mysql+pymysql://root@localhost/test?unix_socket=/isolated/mysql.sock',marker
        finally:
            self.closed += 1

    def execute(self, payload=None, command_id='mysql'):
        with mock.patch('workflow_supplemental.mysql_session',self.session):
            return verify_candidate(self.ledger,self.tester,payload or self.payload,command_id=command_id)

    def test_schema_and_environment_are_discoverable_and_fixed(self):
        validate_payload('verify',self.payload)
        self.assertEqual('mysql',execution_rules()['local_environment']['mysql']['profile'])
        env = mysql_environment('candidate_mysql','isolated-url')
        self.assertEqual({'DATABASE_URL','ROLE_TEST_MYSQL_URL','MN01_MYSQL_URL'},set(env))
        self.assertTrue(valid_mysql_environment('candidate_mysql',env))
        self.assertFalse(valid_mysql_environment('candidate_mysql',{**env,'DATABASE_URL':'other'}))
        self.assertTrue(valid_mysql_environment('integration_mysql',{'MN01_MYSQL_URL':'old-isolated-url'}))
        with self.assertRaises(ValueError):
            validate_payload('verify',{**self.payload,'url':'external-database'})

    def test_candidate_profile_publishes_independent_test_and_replay_starts_no_database(self):
        assertion = "import os; assert os.environ['DATABASE_URL']==os.environ['ROLE_TEST_MYSQL_URL']==os.environ['MN01_MYSQL_URL']; "
        payload = {**self.payload,'argv':[sys.executable,'-c',assertion+CHECK,'{junit_xml}','1']}
        result = self.assertCommitted(self.execute(payload))
        verification = self.ledger.snapshot()['verifications'][-1]
        self.assertEqual('independent_test',verification['check_name'])
        self.assertEqual('candidate_mysql',verification['evidence']['verification_profile'])
        self.assertEqual(result['publication']['verification_id'],self.execute(payload)['publication']['verification_id'])
        self.assertEqual((1,1),(self.sessions,self.closed))

    def test_skipped_mysql_never_publishes_pass(self):
        script = "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('<testsuite tests=\"1\" skipped=\"1\"><testcase name=\"missing\"><skipped/></testcase></testsuite>')"
        result = self.execute({**self.payload,'argv':[sys.executable,'-c',script,'{junit_xml}']})
        self.assertEqual('UNVERIFIED',result['status'])
        self.assertEqual([],self.ledger.snapshot()['verifications'])
        self.assertIn('REAL_MYSQL_EXECUTION_REQUIRED',result['test_stats']['reasons'])
        self.assertEqual(1,self.closed)

    def test_failure_retains_actual_result_and_cleans_session(self):
        payload = {**self.payload,'argv':[sys.executable,'-c',CHECK,'{junit_xml}','2']}
        self.assertCommitted(self.execute(payload))
        self.assertEqual('FAIL',self.ledger.snapshot()['verifications'][-1]['result'])
        self.assertEqual(1,self.closed)

    def test_environment_prepare_failure_hands_off_once_and_replay_creates_no_session(self):
        from workflow_verification_exceptions import facts
        with mock.patch('workflow_supplemental.mysql_session',side_effect=ValueError('ISOLATED_MYSQL_PREPARE_FAILED')) as session:
            result=verify_candidate(self.ledger,self.tester,self.payload,command_id='prepare-fails')
            replay=verify_candidate(self.ledger,self.tester,self.payload,command_id='prepare-fails')
        self.assertEqual(1,session.call_count)
        self.assertEqual('ENVIRONMENT_PREPARING',result['phase']);self.assertTrue(replay['replayed'])
        rows=facts(self.ledger.snapshot(),'run-one')
        self.assertEqual(1,len(rows));self.assertEqual('COORDINATOR',rows[0]['responsible_role'])
        self.assertEqual([],self.ledger.snapshot()['verifications'])

    def test_cleanup_failure_preserves_pass_and_records_separate_uncertainty(self):
        @contextmanager
        def broken_cleanup(directory):
            with self.session(directory) as resources:
                yield resources
            raise ValueError('ISOLATED_CLEANUP_RECONCILIATION_REQUIRED')
        with mock.patch('workflow_supplemental.mysql_session',broken_cleanup):
            result=verify_candidate(self.ledger,self.tester,self.payload,command_id='cleanup-fails')
        self.assertEqual('ENVIRONMENT_CLEANUP',result['phase'])
        self.assertEqual('PASS',self.ledger.snapshot()['verifications'][-1]['result'])
        from workflow_verification_exceptions import facts,closure_passes
        item=facts(self.ledger.snapshot(),'run-one')[-1]
        self.assertEqual('UNVERIFIED',item['result'])
        self.assertTrue(closure_passes(self.ledger.snapshot(),item)[0]['resource_reconciliation_required'])

    def test_new_command_same_failed_candidate_does_not_execute_test_again(self):
        script="from pathlib import Path; import sys; Path(sys.argv[1]).write_text('<testsuite tests=\"1\" skipped=\"1\"><testcase name=\"missing\"><skipped/></testcase></testsuite>')"
        payload={**self.payload,'argv':[sys.executable,'-c',script,'{junit_xml}']}
        self.assertEqual('UNVERIFIED',self.execute(payload,command_id='failure-1')['status'])
        result=self.execute(payload,command_id='failure-2')
        self.assertEqual('UNCHANGED_VERIFICATION_PREREQUISITES',result['code'])
        self.assertEqual([],self.ledger.snapshot()['verifications'])

    def test_interrupted_frozen_request_is_handed_off_without_starting_again(self):
        with mock.patch('workflow_supplemental.mysql_session',self.session), mock.patch(
                'delivery_evidence.execute_candidate_tests',side_effect=RuntimeError('ISOLATED_CONTROLLED_INTERRUPTION')):
            with self.assertRaisesRegex(RuntimeError,'ISOLATED_CONTROLLED_INTERRUPTION'):
                verify_candidate(self.ledger,self.tester,self.payload,command_id='interrupted')
        result=self.execute(command_id='interrupted')
        self.assertEqual('TEST_EXECUTION_RECOVERY_REQUIRED',result['code'])
        self.assertEqual((1,1),(self.sessions,self.closed))
        self.assertEqual('COMMITTED',result['exception_handoff']['status'])
        self.assertEqual([],self.ledger.snapshot()['verifications'])

    def test_invalid_input_and_wrong_role_start_no_database(self):
        invalid = self.execute({**self.payload,'timeout_seconds':0})
        self.assertEqual('INVALID_NOT_STARTED',invalid['phase'])
        with mock.patch.object(self.ledger,'validate_identity',return_value={'valid':True,'work':{
                'role':'A','kind':'development','candidate_id':self.cid}}):
            with self.assertRaisesRegex(ValueError,'INDEPENDENT_TESTER_WORK_REQUIRED'):
                self.execute()
        self.assertEqual(0,self.sessions)

    def test_incomplete_request_requires_reconciliation_not_new_database(self):
        with mock.patch('workflow_supplemental.mysql_session',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                verify_candidate(self.ledger,self.tester,self.payload,command_id='mysql')
        self.assertEqual('TEST_EXECUTION_RECOVERY_REQUIRED',self.execute()['code'])
        self.assertEqual(0,self.sessions)
        self.assertEqual([],self.ledger.snapshot()['verifications'])

    def test_invalid_frozen_request_starts_no_database(self):
        folder = self.path.parent/'operations'/hashlib.sha256(b'mysql').hexdigest()[:32]
        folder.mkdir(parents=True); (folder/'request.json').write_text('{}')
        with self.assertRaisesRegex(ValueError,'TEST_COMMAND_ID_CONFLICT'):
            self.execute()
        self.assertEqual(0,self.sessions)

    def test_changed_replay_is_rejected_without_new_database(self):
        self.assertCommitted(self.execute())
        with self.assertRaisesRegex(ValueError,'TEST_COMMAND_ID_CONFLICT'):
            self.execute({**self.payload,'timeout_seconds':42})
        self.assertEqual(1,self.sessions)

    def test_local_profile_keeps_existing_runner(self):
        self.assertCommitted(self.execute({**self.payload,'profile':'local'}))
        self.assertEqual(0,self.sessions)
        self.assertNotIn('verification_profile',self.ledger.snapshot()['verifications'][-1]['evidence'])

    def test_simultaneous_same_command_starts_one_database(self):
        with mock.patch('workflow_supplemental.mysql_session',self.session), ThreadPoolExecutor(max_workers=2) as executor:
            calls = [executor.submit(verify_candidate,self.ledger,self.tester,self.payload,command_id='parallel') for _ in range(2)]
            results = [call.result() for call in calls]
        self.assertEqual((1,1),(self.sessions,self.closed))
        self.assertEqual(1,len(self.ledger.snapshot()['verifications']))
        self.assertEqual(results[0]['publication']['verification_id'],results[1]['publication']['verification_id'])

    def test_excessive_socket_path_fails_before_container_creation(self):
        from workflow_supplemental import mysql_session
        with mock.patch('workflow_supplemental.docker') as docker:
            with self.assertRaisesRegex(ValueError,'MYSQL_SOCKET_PATH_TOO_LONG'):
                with mysql_session(self.root/('a'*90)):
                    self.fail('不可启动数据库')
        docker.assert_not_called()
