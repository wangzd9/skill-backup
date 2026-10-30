"""A resolved no-rerun decision safely releases only its own tester work."""
import json

from test_business_ledger import LedgerFixture
from workflow_user_decisions import record
from workflow_verification_lifecycle import apply_resolved_decisions, reconcile


class DecisionRetirementTests(LedgerFixture):
    def test_resolved_short_hash_ruling_retires_old_work_and_admits_next_candidate(self):
        # Keep the test focused on candidate identity rather than dependency
        # ordering; both candidates remain in the same accepted run.
        with self.ledger._connection() as db:
            db.execute("UPDATE tasks SET dependencies='[]' WHERE run_id='run-one' AND task_id='T2'")
            db.commit()

        _, old_dev = self.bind('A')
        old_candidate, old_spec = self.candidate(old_dev, value=1)
        self.close(old_dev_process := self.workers[-1], old_dev)

        _, next_dev = self.bind('A')
        next_candidate, next_spec = self.candidate(next_dev, value=2)
        self.close(self.workers[-1], next_dev)

        with self.ledger._connection() as db:
            old_task = next(row for row in self.ledger.snapshot()['tasks']
                            if row['task_id'] == 'T1')
            old_work = self.ledger._new_work(db, 'run-one', 'TESTER',
                'supplemental_verification', old_task, old_candidate)
            queued_duplicate = self.ledger._new_work(db, 'run-one', 'TESTER',
                'supplemental_verification', old_task, old_candidate)
            db.execute('UPDATE role_current SET current_work_id=? WHERE role=\'TESTER\'',
                       (old_work['work_id'],))
            db.commit()

        tester_process, tester = self.bind('TESTER')
        self.assertEqual(old_work['work_id'], tester['work_id'])
        original_events = [event['event_id'] for event in self.ledger.snapshot()['events']
            if event['work_id'] == old_work['work_id']]
        next_head = next(row['head'] for row in self.ledger.snapshot()['candidates']
                         if row['candidate_id'] == next_candidate)
        answer = (f"裁决：对 {old_work['work_id']} 执行 close_verification，结果记录为 "
            f"UNVERIFIED / TEST_RESULT_NONDETERMINISTIC，保留全部证据，不再重跑；"
            f"释放 TESTER 后先对 {next_head[:7]} 完成 independent_test，结果落账前不要 APPROVE 或合入。")
        provenance = {'platform':'feishu','chat_id':'coord-chat','user_id':'user-1',
            'message_id':'native-ruling-1','authenticated':True,'current_full':False,
            'primary_text':answer}
        recorded = record(self.ledger, {}, provenance, command_id='native-ruling')
        self.assertEqual('COMMITTED', recorded['status'])

        _, coordinator = self.bind('COORDINATOR')
        resolution = {'request_id':recorded['request_id'],'decision_id':'close_verification',
            'summary':'按已验证的对象和原文执行安全退休，不改变验收结果。',
            'evidence':{'related_objects':{'work_running':old_work['work_id'],
                # Production resolution evidence often decorates IDs with a
                # task label and head; the bridge must normalize that stable ID
                # prefix while still cross-checking the user's short head.
                'candidate_unverified':old_candidate+'（MN-04 a6）',
                'candidate_next_test':next_candidate+'（MN-05 a3，head '+next_head+'）'}}}
        # Recreate the production case: decision resolution committed before
        # the lifecycle bridge was available, so the background sweep repairs it.
        with self.ledger._connection() as db:
            self.ledger._event(db,'USER_DECISION_RESOLVED',resolution,
                run_id='run-one',work_id=coordinator['work_id'],role='COORDINATOR')
            db.commit()

        applied = apply_resolved_decisions(self.ledger)
        self.assertEqual(1, len(applied))
        self.assertTrue(applied[0]['requested'], applied[0])
        self.assertEqual('UNVERIFIED', applied[0]['result'])
        self.assertTrue(any(item['work_id'] == queued_duplicate['work_id']
                            for item in applied[0]['queued_duplicates']))

        # A live native writer retains the fence. The ruling creates only a
        # drain request; it cannot start a second TESTER execution or kill this one.
        hint = self.ledger.admission_hint('TESTER')
        self.assertEqual('WRITER_FENCE_PRESENT', hint['reason'])
        waiting = reconcile(self.ledger)
        self.assertTrue(any(item.get('state') == 'WAITING_FOR_NATIVE_WRITER_EXIT'
                            for item in waiting))
        self.assertEqual('RUNNING', next(row for row in self.ledger.snapshot()['works']
            if row['work_id'] == old_work['work_id'])['state'])

        self.assertCommitted(self.ledger.release_work(tester,
            reason='按已登记裁决安全退出同候选补验',command_id='tester-safe-release'))
        self.close(tester_process, tester)
        reconcile(self.ledger)
        snapshot = self.ledger.snapshot()
        retired = next(event for event in snapshot['events']
            if event['kind'] == 'VERIFICATION_RETIRED' and event['work_id'] == old_work['work_id'])
        result = retired['payload']['business_result']
        self.assertEqual('UNVERIFIED', result['result'])
        self.assertEqual('TEST_RESULT_NONDETERMINISTIC', result['reason_code'])
        self.assertEqual(original_events, result['preserved_source_event_ids'])
        self.assertFalse(result['rerun_authorized'])
        self.assertTrue(next(event for event in snapshot['events']
            if event['kind'] == 'VERIFICATION_RETIRED'
            and event['work_id'] == queued_duplicate['work_id']))
        self.assertFalse(any(row['candidate_id'] == old_candidate and row['result'] == 'PASS'
                             for row in snapshot['verifications']))
        self.assertFalse(any(row['candidate_id'] == old_candidate
                             for row in snapshot['decisions']))

        hint = self.ledger.admission_hint('TESTER')
        self.assertEqual('CANDIDATE_VERIFICATION_READY', hint['reason'])
        _, next_tester = self.bind('TESTER')
        snapshot = self.ledger.snapshot()
        bound_work = next(row for row in snapshot['works'] if row['work_id'] == next_tester['work_id'])
        self.assertEqual('verification', bound_work['kind'])
        self.assertEqual(next_candidate, bound_work['candidate_id'])
