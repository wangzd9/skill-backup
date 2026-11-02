"""Isolated scheduling/decision/report flow; synthetic completion never leaves tmp."""
import json
from unittest import mock

from test_business_ledger import LedgerFixture
from workflow_coordination import attention, dependencies, facts, reconcile, request_questions
from workflow_development_policy import enable
from workflow_user_decisions import answer_source, pending, record, resolve
from workflow_reports import render_notification, _live_bullet
from workflow_report_text import excerpt


class CoordinationFlowTests(LedgerFixture):
    def setUp(self):
        super().setUp()
        self.source = {'platform': 'codex', 'source': 'current_user_workflow_instruction',
                       'authenticated': True, 'message_id': 'isolated-policy',
                       'primary_text': 'Isolated synthetic test authorization for local development'}
        self.assertCommitted(enable(self.ledger, 'run-one', 'workflow/consumer', self.source, command_id='policy-one'))
        (self.root / 'upstream').mkdir()
        self.assertCommitted(self.ledger.create_run('upstream-package', self.root / 'upstream', self.repo,
            ['U1'], run_id='run-up', command_id='up-run'))
        self.assertCommitted(self.ledger.select_run('run-up', command_id='up-select'))
        process, coord = self.bind('COORDINATOR')
        self.assertCommitted(self.ledger.publish_plan('run-up', 1,
            [self.task('U1', 'B', scope=['upstream.py'])], identity=coord, command_id='up-plan'))
        self.close(process, coord)
        self.assertCommitted(enable(self.ledger, 'run-up', 'workflow/upstream', self.source, command_id='policy-up'))
        self.assertCommitted(self.ledger.select_run('run-one', command_id='back-select'))
        process, coord = self.bind('COORDINATOR')
        plan = [dict(self.plan[0], inputs='DEP-UP-01 固定产物',
                     minimum_reading_set=['doc/prd/upstream-package/README.md']), self.plan[1]]
        self.assertCommitted(self.ledger.publish_plan('run-one', 2, plan, identity=coord, command_id='consumer-plan'))
        self.assertCommitted(self.ledger.checkpoint(coord, {'next': '等待前置产物',
            'waiting_external': ['DEP-UP-01 upstream-package 固定产物尚未交付']}, command_id='coord-wait'))
        self.close(process, coord)
        process, dev = self.bind('A')
        self.assertCommitted(self.ledger.checkpoint(dev, {'next': '前置产物就绪后继续',
            'waiting_external': ['DEP-UP-01']}, command_id='dev-wait'))
        self.close(process, dev)
        self.consumer_work = self.ledger.current_work('A')
        self.policy = self.path.parent / 'coordination-policy.json'
        self.policy.write_text(json.dumps({'version': 1, 'enabled': True}))

    def test_frozen_dependency_selects_only_an_accepted_prerequisite(self):
        rows = dependencies(self.ledger.snapshot(), 'run-one', self.now)
        self.assertEqual(1, len(rows))
        self.assertEqual(('run-up', ['U1'], False),
            tuple(rows[0][key] for key in ('upstream_run_id', 'required_task_ids', 'requires_user_approval')))
        before = self.ledger.snapshot()
        result = reconcile(self.ledger)
        self.assertEqual('DEPENDENCY_RUN_SCHEDULED', result['state'])
        after = self.ledger.snapshot()
        self.assertEqual('run-up', after['current_run_id'])
        for key in ('authorizations', 'decisions', 'verifications', 'candidates'):
            self.assertEqual(before[key], after[key])
        self.assertEqual('UPSTREAM_IN_PROGRESS', reconcile(self.ledger)['state'])

    def test_roundtrip_restores_exact_work_checkpoint_and_event_cursors(self):
        reconcile(self.ledger)
        # Isolated synthetic terminal prerequisite fact tests the scheduler only.
        # Existing acceptance suites exercise real validation/merge production.
        with self.ledger._connection() as db:
            db.execute("UPDATE tasks SET phase='COMPLETED' WHERE run_id='run-up'")
        self.assertEqual('DEPENDENCY_RUN_RETURNED', reconcile(self.ledger)['state'])
        self.assertEqual('run-one', self.ledger.mode()['current_run_id'])
        self.assertEqual('PARKED_DEPENDENCY_WORK_RESUMPTION', self.ledger.admission_hint('A')['reason'])
        _, identity = self.bind('A')
        current = self.ledger.current_work('A')
        for key in ('work_id', 'checkpoint', 'processed_through', 'issued_through', 'task_id', 'attempt'):
            self.assertEqual(self.consumer_work[key], current[key], key)
        self.assertEqual(self.consumer_work['work_id'], identity['work_id'])
        claims = [e for e in self.ledger.snapshot()['events'] if e['kind'] == 'WORK_CLAIMED'
                  and e['run_id'] == 'run-one']
        self.assertEqual(1, len(claims))

    def test_live_writer_defers_handoff_without_clearing_owner(self):
        process, coord = self.bind('COORDINATOR')
        self.assertEqual('WAITING_SAFE_POINT', reconcile(self.ledger)['state'])
        self.assertEqual(coord['execution_id'], next(r for r in self.ledger.snapshot()['roles']
            if r['role'] == 'COORDINATOR')['writer_execution_id'])
        self.close(process, coord)
        self.assertEqual('DEPENDENCY_RUN_SCHEDULED', reconcile(self.ledger)['state'])

    def test_idle_polls_cannot_starve_handoff_while_another_writer_finishes(self):
        process, dev = self.bind('A')
        for _ in range(3):
            self.assertEqual('WAITING_SAFE_POINT', reconcile(self.ledger)['state'])
            for role in ('COORDINATOR', 'B', 'C', 'TESTER'):
                hint = self.ledger.admission_hint(role)
                self.assertFalse(hint['can_start_model'])
                self.assertEqual('DEPENDENCY_HANDOFF_PENDING', hint['reason'])
            self.assertTrue(self.ledger.validate_identity(dev)['valid'])
        self.close(process, dev)
        self.assertEqual('DEPENDENCY_RUN_SCHEDULED', reconcile(self.ledger)['state'])
        self.assertTrue(self.ledger.admission_hint('COORDINATOR')['can_start_model'])
        self.assertTrue(self.ledger.admission_hint('B')['can_start_model'])

    def test_handoff_barrier_preserves_answer_review_and_default_off(self):
        native = {'platform': 'feishu', 'chat_id': 'synthetic-chat', 'user_id': 'synthetic-user',
                  'message_id': 'synthetic-barrier-answer', 'authenticated': True, 'primary_text': '两个都批'}
        self.assertCommitted(record(self.ledger, {}, native, command_id='barrier-answer'))
        self.assertEqual('PENDING_USER_ANSWER_REVIEW', self.ledger.admission_hint('COORDINATOR')['reason'])
        self.policy.unlink()
        self.assertNotEqual('DEPENDENCY_HANDOFF_PENDING', self.ledger.admission_hint('A')['reason'])

    def test_return_barrier_waits_for_final_acceptance(self):
        reconcile(self.ledger)
        with self.ledger._connection() as db:
            db.execute("UPDATE tasks SET phase='COMPLETED' WHERE run_id='run-up'")
        with mock.patch('workflow_completion.facts', return_value={'plan': {'validation_task_id': 'FINAL'}, 'state': 'VERIFYING'}):
            self.assertNotEqual('DEPENDENCY_HANDOFF_PENDING', self.ledger.admission_hint('COORDINATOR')['reason'])
        with mock.patch('workflow_completion.facts', return_value={'plan': {'validation_task_id': 'FINAL'}, 'state': 'COMPLETED'}):
            self.assertEqual('DEPENDENCY_HANDOFF_PENDING', self.ledger.admission_hint('COORDINATOR')['reason'])
            self.assertEqual('DEPENDENCY_RUN_RETURNED', reconcile(self.ledger)['state'])

    def test_policy_default_disabled_and_manual_pause_preserved(self):
        self.policy.unlink()
        self.assertEqual('DEPENDENCY_SCHEDULER_DISABLED', reconcile(self.ledger)['state'])
        self.policy.write_text(json.dumps({'version': 1, 'enabled': True}))
        with self.ledger._connection() as db:
            db.execute("UPDATE runs SET control='PAUSED' WHERE run_id='run-up'")
        self.assertEqual([], dependencies(self.ledger.snapshot(), 'run-one', self.now))
        self.assertEqual('NO_DEPENDENCY_HANDOFF', reconcile(self.ledger)['state'])
        with self.ledger._connection() as db:
            db.execute("UPDATE runs SET control='DRAINING' WHERE run_id='run-up'")
            db.execute("UPDATE runs SET control='PAUSED' WHERE run_id='run-one'")
        self.assertEqual('CONTROL_BLOCKED', reconcile(self.ledger)['state'])

    def test_prerequisite_final_acceptance_is_scheduled_after_leaf_tasks_complete(self):
        with self.ledger._connection() as db:
            db.execute("UPDATE tasks SET phase='COMPLETED' WHERE run_id='run-up'")
        with mock.patch('workflow_completion.facts', return_value={'plan': {'validation_task_id': 'FINAL'}, 'state': 'VERIFYING'}):
            self.assertEqual('DEPENDENCY_RUN_SCHEDULED', reconcile(self.ledger)['state'])
            self.assertEqual('UPSTREAM_FINAL_ACCEPTANCE_PENDING', reconcile(self.ledger)['state'])

    def test_return_does_not_abandon_a_pending_native_answer(self):
        reconcile(self.ledger)
        native = {'platform': 'feishu', 'chat_id': 'synthetic-chat', 'user_id': 'synthetic-user',
                  'message_id': 'synthetic-upstream-answer', 'authenticated': True, 'primary_text': '两个都批'}
        self.assertCommitted(record(self.ledger, {}, native, command_id='upstream-answer'))
        with self.ledger._connection() as db:
            db.execute("UPDATE tasks SET phase='COMPLETED' WHERE run_id='run-up'")
        self.assertEqual('UPSTREAM_USER_DECISION_PENDING', reconcile(self.ledger)['state'])
        self.assertTrue(self.ledger.admission_hint('COORDINATOR')['can_start_model'])

    def test_unverified_service_and_delivery_recovery_remain_admissible(self):
        from workflow_coordination import admission_barrier
        snapshot = self.ledger.snapshot()
        snapshot['external_actions'] = [{'state': 'UNKNOWN'}]  # isolated unresolved service fact
        with mock.patch.object(self.ledger, 'snapshot', return_value=snapshot):
            self.assertIsNone(admission_barrier(self.ledger, 'COORDINATOR', None))
        with mock.patch('workflow_final_delivery.states', return_value=[{'state': 'UNVERIFIED'}]):
            self.assertIsNone(admission_barrier(self.ledger, 'COORDINATOR', None))

    def test_explicit_selection_interrupts_the_automatic_return(self):
        reconcile(self.ledger)
        self.assertCommitted(self.ledger.select_run('run-one', command_id='explicit-user-select'))
        self.assertEqual('INTERRUPTED', reconcile(self.ledger)['state'])
        self.assertEqual('run-one', self.ledger.mode()['current_run_id'])

    def test_return_waits_for_applicable_requirement_final_acceptance(self):
        reconcile(self.ledger)
        with self.ledger._connection() as db:
            db.execute("UPDATE tasks SET phase='COMPLETED' WHERE run_id='run-up'")
        with mock.patch('workflow_completion.facts', return_value={'plan': {'validation_task_id': 'FINAL'}, 'state': 'VERIFYING'}):
            self.assertEqual('UPSTREAM_FINAL_ACCEPTANCE_PENDING', reconcile(self.ledger)['state'])
        self.assertEqual('run-up', self.ledger.mode()['current_run_id'])

    def test_explicit_reselection_of_upstream_cancels_automatic_return(self):
        reconcile(self.ledger)
        # Isolated fixture: a native applied selection can choose the same run.
        with self.ledger._connection() as db:
            self.ledger._event(db, 'CONTROL_APPLIED', {'action': 'select_run',
                'result': {'run_id': 'run-up', 'unchanged': True}}, run_id='run-up')
        self.assertEqual('INTERRUPTED', reconcile(self.ledger)['state'])
        self.assertEqual('run-up', self.ledger.mode()['current_run_id'])

    def test_native_answer_is_pending_during_upstream_execution_and_resolved_once(self):
        native = {'platform': 'feishu', 'chat_id': 'synthetic-chat', 'user_id': 'synthetic-user',
                  'message_id': 'synthetic-answer', 'authenticated': True, 'primary_text': '两个都批'}
        answer = self.assertCommitted(record(self.ledger, {}, native, command_id='native-answer'))
        reconcile(self.ledger)
        self.assertEqual(answer['request_id'], pending(self.ledger.snapshot(), 'run-up')[0]['payload']['request_id'])
        _, coord = self.bind('COORDINATOR')
        import workflow_runtime
        from workflow_decision_context import directory, _objects
        with mock.patch('workflow_runtime.open_ledger', return_value=self.ledger):
            packet = workflow_runtime.current_input(self.root / 'unused-pool', 'COORDINATOR', model_view=False)
        snapshot = self.ledger.snapshot()
        self.assertIn(answer['request_id'], [a['request_id'] for a in directory(packet, snapshot)['blocker_index']['pending_user_answers']])
        self.assertIn(str(answer['event_id']), _objects(packet, snapshot)['event'])
        payload = {'request_id': answer['request_id'], 'decision_id': 'original-question',
                   'summary': '仅在隔离夹具核对原文，不据此生成候选裁决', 'evidence': {'question': 'fixture'}}
        self.assertCommitted(resolve(self.ledger, coord, payload, command_id='resolve-original'))
        self.assertEqual([], pending(self.ledger.snapshot(), 'run-up'))
        self.assertEqual([], self.ledger.snapshot()['decisions'])

    def test_attention_deduplicates_changing_wait_prose_and_uses_native_outbox(self):
        first = attention(self.ledger)[0]
        self.assertCommitted(first)
        with self.ledger._connection() as db:
            row = db.execute('SELECT checkpoint FROM works WHERE work_id=?',
                             (self.consumer_work['work_id'],)).fetchone()
            cp = json.loads(row[0]); cp['waiting_external'] = ['第99轮等待同一前置依赖']
            db.execute('UPDATE works SET checkpoint=? WHERE work_id=?',
                       (json.dumps(cp), self.consumer_work['work_id']))
        self.assertEqual('DUPLICATE', attention(self.ledger)[0]['status'])
        notices = [o for o in self.ledger.snapshot()['outbox'] if o['kind'] == 'COORDINATION_ATTENTION_REQUIRED']
        self.assertEqual(1, len(notices))
        text = render_notification(self.ledger, notices[0])
        self.assertIn('不要求用户逐项批准', text)
        self.assertIn('upstream_run_id', json.dumps(notices[0]['payload']))

    def test_real_user_question_is_durable_includes_options_and_resumes_daytime(self):
        _, coord = self.bind('COORDINATOR')
        question = {'decision_id': 'fixture-scope', 'question': '是否扩大已冻结范围？',
                    'options': ['保持原范围', '另立需求'], 'reason': '涉及原需求以外的新工作'}
        self.now += 9 * 3600  # isolated clock: 21:00, native notices suppressed
        self.assertCommitted(self.ledger.checkpoint(coord, {'user_questions': [question]}, command_id='question'))
        self.assertEqual([], attention(self.ledger))
        self.now += 11 * 3600  # 08:00 next day, unresolved questions are surfaced
        self.assertCommitted(attention(self.ledger)[0])
        notice = next(o for o in self.ledger.snapshot()['outbox'] if o['kind'] == 'COORDINATION_ATTENTION_REQUIRED')
        text = render_notification(self.ledger, notice)
        self.assertIn(question['question'], text)
        self.assertTrue(all(option in text for option in question['options']))

    def test_resolved_question_notice_is_suppressed_before_transport(self):
        _, coord = self.bind('COORDINATOR')
        question = {'decision_id': 'fixture-scope', 'question': '如何处理范围？',
                    'options': ['保持', '另立需求'], 'reason': '新工作不在原合同内'}
        self.assertCommitted(self.ledger.checkpoint(coord, {'user_questions': [question]}, command_id='question'))
        native = {'platform': 'feishu', 'chat_id': 'synthetic-chat', 'user_id': 'synthetic-user',
                  'message_id': 'synthetic-answer', 'authenticated': True, 'primary_text': 'B'}
        answer = self.assertCommitted(record(self.ledger, {}, native, command_id='answer'))
        self.assertCommitted(resolve(self.ledger, coord, {'request_id': answer['request_id'],
            'decision_id': question['decision_id'], 'summary': '原范围保持', 'evidence': {'question': 'fixture'}}, command_id='resolved'))
        notice = next(o for o in self.ledger.snapshot()['outbox'] if o['kind'] == 'USER_DECISION_REQUESTED')
        from workflow_notification_registry import suppress_stale
        result = self.assertCommitted(suppress_stale(self.ledger, notice))
        self.assertEqual('SUPPRESSED', result['state'])
        self.assertEqual('COORDINATION_NOTICE_RESOLVED_OR_REPLACED', result['receipt']['reason'])

    def test_complete_report_units_do_not_cut_identifiers_or_show_only_a_heading(self):
        text = excerpt('一、交付结论\n\n已实现异步队列，独立验证待执行。\n二、交付内容\n' + '更多说明；' * 100)
        self.assertIn('已实现异步队列', text)
        self.assertNotIn('…', text)
        self.assertLessEqual(len(text), 205)
        bullet = _live_bullet('接续条件', '需等待 DEP-FOUNDATION-03 固定产物。' + '后续详细条件；' * 200)
        self.assertIn('DEP-FOUNDATION-03', bullet)
        self.assertNotIn('…', bullet)
        self.assertIn('完整记录', bullet)
        for value in ('两个都批', '两项都批准', '不同意', '1'):
            self.assertEqual(value, answer_source(value)['answer'])
