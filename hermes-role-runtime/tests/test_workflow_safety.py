"""Independent native-authority safety regressions using real temporary ledgers."""
import hashlib
import json
from pathlib import Path
import sys
import time
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT/'tests', Path('/home/wang/.hermes/hermes-agent')):
    sys.path.insert(0, str(directory))
from test_business_ledger import LedgerFixture
import workflow_runtime as workflow


class WorkflowSafetyTests(LedgerFixture):
    def setUp(self):
        super().setUp()
        self.pool_path = self.root/'isolated.json'
        self.home = self.root/'home'; self.home.mkdir()
        self.pool_path.write_text(json.dumps({'schema_version': 1, 'generation': 1,
            'control': 'ACTIVE', 'roles': {role: {'poll_job_id': role+'-poll',
                'chat_id': 'chat-'+role, 'report_job_id': role+'-report'} for role in workflow.ROLES}}))
        self.provenance = {'authenticated': True, 'current_full': True,
            'message_id': 'native-message', 'user_id': 'native-user', 'chat_id': 'chat-COORDINATOR',
            'primary_text': '本轮授予全部权限：暂停当前需求'}

    def test_native_pause_authorization_cannot_be_reinterpreted_as_cancel_or_other_run(self):
        for action in ('cancel', 'select_run', 'resume', 'continue'):
            with self.subTest(action=action), self.assertRaises(ValueError):
                workflow.submit_control(self.pool_path, 'COORDINATOR', action, {'run_id': 'run-one'}, self.provenance)
        (self.root/'other').mkdir()
        self.assertCommitted(self.ledger.create_run('other', self.root/'other', self.repo, ['T1'],
            run_id='other-run', command_id='other-run'))
        with self.assertRaises(ValueError):
            workflow.submit_control(self.pool_path, 'COORDINATOR', 'pause', {'run_id': 'other-run'}, self.provenance)
        self.assertFalse(self.ledger.snapshot()['controls'])
        accepted = workflow.submit_control(self.pool_path, 'COORDINATOR', 'pause', {'run_id': 'run-one'}, self.provenance)
        self.assertCommitted(accepted)
        self.assertEqual(self.ledger.snapshot()['controls'][0]['target'], {'run_id': 'run-one'})

    def test_coordinator_receives_exact_current_plan_tasks_candidates_and_failures(self):
        _, developer = self.bind('A')
        candidate, _ = self.candidate(developer)
        _, coordinator = self.bind('COORDINATOR')
        scope = {**coordinator, 'pool_path': str(self.pool_path), 'kind': 'business', 'workflow_version': 1}
        packet = workflow.current_input(self.pool_path, 'COORDINATOR', scope=scope)
        self.assertEqual({task['task_id'] for task in packet['tasks']}, {'T1', 'T2'})
        self.assertEqual({row['candidate_id'] for row in packet['candidates']}, {candidate})
        self.assertTrue(packet['plans'])
        self.assertIn('controls', packet)
        self.assertIn('external_actions', packet)
        self.assertFalse(packet['archives_loaded'])

    def test_prepared_prompt_hash_covers_final_workspace_and_scope(self):
        _, execution = self.worker()
        prepared = workflow.prepare({'id': 'A-poll', 'role_pool': {'role': 'A'}, 'prompt': 'old'},
            pool_path=self.pool_path, execution=execution, home=self.home, session_id='fixture-session')
        self.assertIsNotNone(prepared)
        packet = json.loads(prepared['prompt'].split('\n', 1)[1])
        payload = {key: value for key, value in packet.items() if key not in
            ('input_sha256', 'input_bytes', 'requires_paging')}
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        self.assertEqual(packet['input_sha256'], hashlib.sha256(encoded).hexdigest())
        self.assertEqual(packet['input_bytes'], len(encoded))
        self.assertEqual(packet['scope']['worktree'], prepared['workdir'])
        self.assertEqual(packet['worktree'], prepared['workdir'])

    def test_negative_native_service_consent_cannot_create_authorization(self):
        developer, identity = self.bind('A')
        candidate, _ = self.candidate(identity)
        self.close(developer, identity)
        for index, text in enumerate(('不要授权当前候选OCR一次', '暂不授权当前候选OCR一次',
                                      '未经我确认不得授权当前候选OCR一次')):
            source = {**self.provenance, 'message_id': 'negative-service-' + str(index),
                      'primary_text': '本轮授予全部权限：' + text}
            with self.subTest(text=text), self.assertRaises(ValueError):
                workflow.submit_control(self.pool_path, 'COORDINATOR', 'authorize_service', {
                    'candidate_id': candidate, 'service_kind': 'ocr', 'max_calls': 1,
                    'expires_at': time.time() + 300}, source)
        self.assertEqual(self.ledger.snapshot()['authorizations'], [])

    def test_compound_control_keeps_affirmative_action_and_exact_role(self):
        source = {**self.provenance, 'message_id': 'compound-pause',
                  'primary_text': '本轮授予全部权限：暂停当前需求，不要取消'}
        with self.assertRaises(ValueError):
            workflow.submit_control(self.pool_path, 'COORDINATOR', 'cancel', {'run_id': 'run-one'}, source)
        self.assertCommitted(workflow.submit_control(self.pool_path, 'COORDINATOR', 'pause',
            {'run_id': 'run-one'}, source))
        role_source = {**self.provenance, 'message_id': 'exact-role',
            'primary_text': '本轮授予全部权限：暂停当前需求的 A'}
        with self.assertRaises(ValueError):
            workflow.submit_control(self.pool_path, 'COORDINATOR', 'pause',
                {'run_id': 'run-one', 'role': 'B'}, role_source)
        self.assertCommitted(workflow.submit_control(self.pool_path, 'COORDINATOR', 'pause',
            {'run_id': 'run-one', 'role': 'A'}, role_source))

    def test_ordered_multiclause_controls_require_predecessor_and_keep_both(self):
        self.assertCommitted(self.ledger.create_run('two',self.root,self.repo,['T1'],run_id='run-two',command_id='second-run'))
        source={**self.provenance,'message_id':'ordered-two','primary_text':'全部权限：停止 A，转 run-two'}
        with self.assertRaisesRegex(ValueError,'PRECEDING_CONTROL_ACTION_REQUIRED'):
            workflow.submit_control(self.pool_path,'COORDINATOR','select_run',{'run_id':'run-two'},source)
        first=workflow.submit_control(self.pool_path,'COORDINATOR','pause',{'run_id':'run-one','role':'A'},source)
        self.assertCommitted(first)
        second=workflow.submit_control(self.pool_path,'COORDINATOR','select_run',{'run_id':'run-two'},source)
        self.assertCommitted(second)
        self.assertEqual(workflow.submit_control(self.pool_path,'COORDINATOR','select_run',{'run_id':'run-two'},source)['status'],'DUPLICATE')
        records=[row for row in self.ledger.snapshot()['controls'] if row['provenance']['message_id']=='ordered-two']
        self.assertEqual([row['provenance']['source_clause'] for row in records],[0,1])

    def test_unknown_outbox_delivery_never_repeats_or_wakes_waiting_model(self):
        from workflow_notifications import drain_business_notifications
        developer, identity = self.bind('A')
        self.close(developer, identity)
        coordinator, coord = self.bind('COORDINATOR')
        inputs = self.ledger.work_inputs(coord, command_id='outbox-wait-input')
        self.assertCommitted(self.ledger.publish_plan('run-one', 2, self.plan,
            identity=coord, command_id='own-plan-event'))
        self.assertCommitted(self.ledger.checkpoint(coord, {'waiting_external': ['fixture-external']},
            processed_through=inputs['through'], yield_execution=True, command_id='outbox-wait'))
        self.close(coordinator, coord)
        self.assertFalse(self.ledger.admission_hint('COORDINATOR')['can_start_model'])
        high_water = self.ledger.snapshot()['event_high_water']
        calls = []

        def unknown(row):
            calls.append(row)
            return {'delivered': False}

        with mock.patch('workflow_runtime.open_ledger', return_value=self.ledger):
            drain_business_notifications(self.pool_path, transport=unknown)
            drain_business_notifications(self.pool_path, transport=unknown)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.ledger.outbox_items()[0]['state'], 'UNKNOWN')
        self.assertEqual(self.ledger.snapshot()['event_high_water'], high_water)
        self.assertFalse(self.ledger.admission_hint('COORDINATOR')['can_start_model'])
