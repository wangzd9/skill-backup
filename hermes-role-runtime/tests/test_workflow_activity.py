"""Actual local hook receipts, exact scope isolation and conservative coverage."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from activity_signals import activity_step
import workflow_activity as collector


class NativeToolActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.scope = {'pool_path': str(self.home/'pool.json'), 'generation': 1,
            'run_id': 'run-one', 'role': 'A', 'work_id': 'work-one', 'task_id': 'T1',
            'attempt': 1, 'execution_id': 'exec-one', 'execution_generation': 1}

    def observation(self, now):
        result = collector.read_activity(self.home, self.scope, now)
        result.update(identity={**self.scope, 'pool': self.scope['pool_path']},
                      current_work_valid=True, checkpoint_at=0,
                      required_sources=['tools', 'model_requests'])
        result['sources'].append({'source_id': 'model_requests', 'source_version': 'fixture-native/1',
            'check_status': 'VERIFIED', 'complete': True, 'last_verified_at': now,
            'max_gap_seconds': 120, 'coverage_started_at': 0})
        return result

    def test_read_without_marker_is_unknown_and_does_not_create_cache(self):
        result = collector.read_activity(self.home, self.scope, 100)
        self.assertFalse(result['sources'][0]['complete'])
        self.assertEqual(result['sources'][0]['check_status'], 'UNVERIFIED')
        self.assertFalse((self.home/'runtime').exists())

    def test_real_start_end_and_restart_preserve_epoch_and_last_activity(self):
        first = collector.start_coverage(self.home, self.scope, 0)
        collector.record_tool_start(self.home, self.scope, 'call-one', 'read_file', 100)
        collector.record_tool_end(self.home, self.scope, 'call-one', 200, 'SUCCESS')
        second = collector.start_coverage(self.home, self.scope, 700)
        self.assertEqual(first, second)
        value = self.observation(801)
        self.assertTrue(value['sources'][0]['complete'])
        self.assertEqual(value['lifecycles'][0]['kind'], 'file_read')
        facts, events = activity_step({}, value, 801, threshold_seconds=600)
        self.assertEqual(facts['last_activity_at'], 200)
        self.assertEqual([event['type'] for event in events], ['NO_ACTIVITY'])

    def test_new_collector_cannot_use_old_checkpoint_as_no_activity_proof(self):
        collector.start_coverage(self.home, self.scope, 2000)
        facts, events = activity_step({}, self.observation(2100), 2100, threshold_seconds=600)
        self.assertNotIn('NO_ACTIVITY', [event['type'] for event in events])
        self.assertFalse(facts['source_coverage'][0]['inactivity_window_verified'])
        facts, events = activity_step(facts, self.observation(2601), 2601, threshold_seconds=600)
        self.assertIn('NO_ACTIVITY', [event['type'] for event in events])

    def test_missing_post_stays_incomplete_but_preserves_verified_start(self):
        collector.start_coverage(self.home, self.scope, 0)
        collector.record_tool_start(self.home, self.scope, 'call-one', 'terminal', 900)
        value = self.observation(2000)
        self.assertTrue(value['sources'][0]['complete'])
        facts, events = activity_step({}, value, 2000, threshold_seconds=600)
        self.assertEqual(facts['last_activity_at'], 900)
        self.assertNotIn('NO_ACTIVITY', [event['type'] for event in events])
        self.assertEqual(facts['state'], 'OBSERVATION_OVERDUE')
        collector.start_coverage(self.home, self.scope, 2100)
        self.assertTrue(collector.read_activity(self.home, self.scope, 2200)['sources'][0]['complete'])
        collector.record_tool_end(self.home, self.scope, 'call-one', 2201, 'ERROR')
        self.assertTrue(collector.read_activity(self.home, self.scope, 2300)['sources'][0]['complete'])

    def test_enforced_wait_is_visible_during_warmup_and_expires_without_inactivity(self):
        collector.start_coverage(self.home, self.scope, 100)
        collector.record_tool_start(self.home, self.scope, 'call-one', 'terminal', 101, wait_until=2000)
        facts, events = activity_step({}, self.observation(200), 200, threshold_seconds=600)
        self.assertEqual(len(facts['normal_waits']), 1)
        self.assertNotIn('NO_ACTIVITY', [event['type'] for event in events])
        facts, events = activity_step(facts, self.observation(1000), 1000, threshold_seconds=600)
        self.assertEqual(facts['state'], 'NORMAL_WAIT')
        facts, events = activity_step(facts, self.observation(2001), 2001, threshold_seconds=600)
        self.assertEqual([event['type'] for event in events], ['OPERATION_WAIT_EXPIRED'])
        collector.record_tool_end(self.home, self.scope, 'call-one', 2100, 'CANCELLED')
        facts, events = activity_step(facts, self.observation(2200), 2200, threshold_seconds=600)
        self.assertIn(('OPERATION_WAIT_EXPIRED', 'recovered'),
                      [(event['type'], event['transition']) for event in events])

    def test_missing_id_or_orphan_post_is_a_persistent_gap(self):
        for kind in ('missing-id', 'orphan-end'):
            scope = {**self.scope, 'execution_id': kind}
            collector.start_coverage(self.home, scope, 0)
            if kind == 'missing-id':
                result = collector.record_tool_start(self.home, scope, None, 'read_file', 10)
            else:
                result = collector.record_tool_end(self.home, scope, 'no-start', 10)
            self.assertEqual(result['state'], 'INCOMPLETE')
            collector.start_coverage(self.home, scope, 20)
            collector.record_tool_start(self.home, scope, 'later', 'read_file', 30)
            collector.record_tool_end(self.home, scope, 'later', 40)
            self.assertFalse(collector.read_activity(self.home, scope, 1000)['sources'][0]['complete'])

    def test_receipt_failure_is_visible_after_database_recovers(self):
        collector.start_coverage(self.home, self.scope, 0)
        with patch.object(collector, '_database', side_effect=sqlite3.OperationalError('locked fixture')):
            failed = collector.record_tool_start(self.home, self.scope, 'lost', 'read_file', 10)
        self.assertEqual(failed['state'], 'INCOMPLETE')
        value = collector.read_activity(self.home, self.scope, 1000)
        self.assertFalse(value['sources'][0]['complete'])
        self.assertIn('TOOL_ACTIVITY_RECEIPT_UNAVAILABLE', value['sources'][0]['reason_codes'])

    def test_execution_generation_and_pool_are_exactly_isolated(self):
        collector.start_coverage(self.home, self.scope, 0)
        collector.record_tool_start(self.home, self.scope, 'call-one', 'read_file', 100)
        collector.record_tool_end(self.home, self.scope, 'call-one', 200)
        for changed in ({'execution_id': 'exec-two', 'execution_generation': 2},
                        {'generation': 2}, {'pool_path': str(self.home/'other-pool.json')}):
            scope = {**self.scope, **changed}
            value = collector.read_activity(self.home, scope, 1000)
            self.assertFalse(value['sources'][0]['complete'])
            self.assertEqual(value['lifecycles'], [])
        self.assertTrue(collector.read_activity(self.home, self.scope, 1000)['sources'][0]['complete'])

    def test_duplicate_hooks_do_not_create_fresh_activity(self):
        collector.start_coverage(self.home, self.scope, 0)
        collector.record_tool_start(self.home, self.scope, 'same', 'read_file', 100)
        collector.record_tool_end(self.home, self.scope, 'same', 200)
        self.assertEqual(collector.record_tool_start(self.home, self.scope, 'same', 'read_file', 500)['state'], 'DUPLICATE')
        self.assertEqual(collector.record_tool_end(self.home, self.scope, 'same', 600)['state'], 'DUPLICATE')
        value = collector.read_activity(self.home, self.scope, 1000)
        self.assertEqual(len(value['lifecycles']), 1)
        self.assertEqual(value['lifecycles'][0]['ended_at'], 200)

    def test_coordination_uses_explicit_not_applicable_task_and_attempt(self):
        scope = {**self.scope, 'role': 'COORDINATOR', 'task_id': None, 'attempt': None}
        self.assertEqual(collector.start_coverage(self.home, scope, 0)['state'], 'COVERED')
        self.assertTrue(collector.read_activity(self.home, scope, 1000)['sources'][0]['complete'])
        bad = {**self.scope, 'task_id': None, 'attempt': None}
        self.assertEqual(collector.start_coverage(self.home, bad, 0)['state'], 'INCOMPLETE')
        for field in ('work_id', 'run_id', 'execution_id', 'generation'):
            bad = {**scope, field: None}
            self.assertEqual(collector.start_coverage(self.home, bad, 0)['state'], 'INCOMPLETE')

    def test_allowlisted_metadata_never_stores_unknown_tool_or_outcome_payload(self):
        collector.start_coverage(self.home, self.scope, 0)
        sentinel = 'private prompt and command argument fixture'
        collector.record_tool_start(self.home, self.scope, 'safe-call-id', sentinel, 100)
        collector.record_tool_end(self.home, self.scope, 'safe-call-id', 200, sentinel)
        value = collector.read_activity(self.home, self.scope, 1000)
        self.assertEqual(value['lifecycles'][0]['kind'], 'other_tool')
        self.assertEqual(value['lifecycles'][0]['outcome'], 'UNKNOWN')
        for path in (self.home/'runtime/workflow-activity').iterdir():
            self.assertNotIn(sentinel.encode(), path.read_bytes())
        self.assertEqual((self.home/'runtime/workflow-activity/tools.sqlite3').stat().st_mode & 0o777, 0o600)

    def test_synthetic_terminal_is_not_real_end_or_recovered_coverage(self):
        collector.start_coverage(self.home, self.scope, 0)
        collector.record_tool_start(self.home, self.scope, 'still-running', 'terminal', 100)
        result = collector.record_tool_end(self.home, self.scope, 'still-running', 200,
                                           'UNKNOWN', end_verified=False)
        self.assertEqual(result['state'], 'INCOMPLETE')
        value = self.observation(1000)
        self.assertFalse(value['sources'][0]['complete'])
        self.assertIsNone(value['lifecycles'][0]['ended_at'])
        self.assertEqual(value['terminal_reports'], [{'operation_id': 'still-running',
            'reported_at': 200, 'outcome': 'UNKNOWN', 'end_verified': False}])
        facts, events = activity_step({}, value, 1000, threshold_seconds=600)
        self.assertEqual(facts['last_activity_at'], 100)
        self.assertNotIn('NO_ACTIVITY', [event['type'] for event in events])
        # Even a later actual end cannot rewrite the interval whose termination
        # was unknown, or let a new unrelated success erase that gap.
        collector.record_tool_end(self.home, self.scope, 'still-running', 1100, 'SUCCESS')
        collector.record_tool_start(self.home, self.scope, 'other', 'read_file', 1200)
        collector.record_tool_end(self.home, self.scope, 'other', 1300, 'SUCCESS')
        collector.start_coverage(self.home, self.scope, 1400)
        self.assertFalse(collector.read_activity(self.home, self.scope, 2000)['sources'][0]['complete'])


class NativeModelActivityTests(unittest.TestCase):
    setUp = NativeToolActivityTests.setUp
    def observation(self, now):
        value = collector.read_model_activity(self.home, self.scope, now)
        value.update(identity={**self.scope, 'pool': self.scope['pool_path']},
            current_work_valid=True, checkpoint_at=0, required_sources=['model_requests'])
        return value

    def test_model_source_requires_its_own_real_coverage(self):
        collector.start_coverage(self.home, self.scope, 0)
        value = collector.read_model_activity(self.home, self.scope, 1000)
        self.assertFalse(value['sources'][0]['complete'])
        first = collector.start_model_coverage(self.home, self.scope, 1000)
        second = collector.start_model_coverage(self.home, self.scope, 1100)
        self.assertEqual(first, second)
        self.assertTrue(collector.read_model_activity(self.home, self.scope, 1200)['sources'][0]['complete'])
        facts, events = activity_step({}, self.observation(1200), 1200, threshold_seconds=600)
        self.assertNotIn('NO_ACTIVITY', [row['type'] for row in events])
        self.assertFalse(facts['source_coverage'][0]['inactivity_window_verified'])

    def test_real_physical_retries_do_not_collapse_into_logical_request(self):
        collector.start_model_coverage(self.home, self.scope, 0)
        collector.record_model_start(self.home, self.scope, 'physical-one', 'logical', 100)
        collector.record_model_end(self.home, self.scope, 'physical-one', 'logical', 200, 'ERROR')
        collector.record_model_start(self.home, self.scope, 'physical-two', 'logical', 210)
        value = self.observation(1000)
        self.assertTrue(value['sources'][0]['complete'])
        self.assertEqual({row['operation_id'] for row in value['lifecycles']}, {'physical-one', 'physical-two'})
        facts, events = activity_step({}, value, 1000, threshold_seconds=600)
        self.assertEqual(facts['last_activity_at'], 210)
        self.assertNotIn('NO_ACTIVITY', [row['type'] for row in events])
        collector.record_model_end(self.home, self.scope, 'physical-two', 'logical', 1100)
        value = self.observation(1701)
        self.assertTrue(value['sources'][0]['complete'])
        self.assertEqual(len(value['lifecycles']), 2)
        facts, events = activity_step({}, value, 1701, threshold_seconds=600)
        self.assertEqual([row['type'] for row in events], ['NO_ACTIVITY'])
        collector.record_model_end(self.home, self.scope, 'physical-two', 'logical', 1800)
        self.assertEqual(max(row['ended_at'] for row in self.observation(1900)['lifecycles']), 1100)

    def test_open_compaction_and_stream_handle_never_certify_inactivity(self):
        collector.start_model_coverage(self.home, self.scope, 0)
        collector.record_model_start(self.home, self.scope, 'compress', 'logical-compress', 100, 'compaction')
        facts, events = activity_step({}, self.observation(1000), 1000, threshold_seconds=600)
        self.assertEqual(facts['last_activity_at'], 100)
        self.assertEqual(facts['state'], 'IN_FLIGHT')
        self.assertNotIn('NO_ACTIVITY', [row['type'] for row in events])
        collector.record_model_end(self.home, self.scope, 'compress', 'logical-compress', 1100,
                                   end_verified=False)
        value = self.observation(2000)
        self.assertIsNone(value['lifecycles'][0]['ended_at'])
        self.assertFalse(value['terminal_reports'][0]['end_verified'])
        # A late success is an actual new fact, not proof about the old blind interval.
        collector.record_model_end(self.home, self.scope, 'compress', 'logical-compress', 2100)
        self.assertFalse(self.observation(3000)['sources'][0]['complete'])

    def test_missing_model_ids_or_orphan_post_leave_persistent_gap(self):
        for physical, logical in ((None, 'logical'), ('physical', None)):
            scope = {**self.scope, 'execution_id': str(physical)+'-'+str(logical)}
            collector.start_model_coverage(self.home, scope, 0)
            self.assertEqual(collector.record_model_start(self.home, scope, physical, logical, 100)['state'], 'INCOMPLETE')
            collector.record_model_start(self.home, scope, 'good', 'good-logical', 200)
            collector.record_model_end(self.home, scope, 'good', 'good-logical', 300)
            self.assertFalse(collector.read_model_activity(self.home, scope, 1000)['sources'][0]['complete'])
        scope = {**self.scope, 'execution_id': 'orphan'}
        collector.start_model_coverage(self.home, scope, 0)
        collector.record_model_end(self.home, scope, 'missing', 'logical', 100)
        self.assertIn('MODEL_START_RECEIPT_MISSING',
                      collector.read_model_activity(self.home, scope, 1000)['sources'][0]['reason_codes'])

    def test_model_receipt_failure_is_persistent_and_independent_of_tools(self):
        collector.start_coverage(self.home, self.scope, 0)
        collector.start_model_coverage(self.home, self.scope, 0)
        with patch.object(collector, '_model_database', side_effect=sqlite3.OperationalError('locked fixture')):
            result = collector.record_model_start(self.home, self.scope, 'lost', 'logical', 100)
        self.assertEqual(result['state'], 'INCOMPLETE')
        self.assertFalse(self.observation(1000)['sources'][0]['complete'])
        self.assertTrue(collector.read_activity(self.home, self.scope, 1000)['sources'][0]['complete'])

    def test_model_scope_conflict_privacy_and_new_execution(self):
        collector.start_model_coverage(self.home, self.scope, 0)
        collector.record_model_start(self.home, self.scope, 'physical', 'logical', 100,
                                     'PRIVATE-PROMPT-TEXT')
        collector.record_model_end(self.home, self.scope, 'physical', 'logical', 200, 'PRIVATE-RESULT-TEXT')
        value = self.observation(1000)
        self.assertEqual(value['lifecycles'][0]['kind'], 'auxiliary_request')
        self.assertEqual(value['lifecycles'][0]['outcome'], 'UNKNOWN')
        root=self.home/'runtime/workflow-activity/model_requests'
        for path in root.iterdir():
            self.assertNotIn(b'PRIVATE-PROMPT-TEXT', path.read_bytes())
            self.assertNotIn(b'PRIVATE-RESULT-TEXT', path.read_bytes())
        self.assertEqual((root/'attempts.sqlite3').stat().st_mode & 0o777, 0o600)
        other = {**self.scope, 'execution_id': 'new', 'execution_generation': 2}
        self.assertEqual(collector.read_model_activity(self.home, other, 1000)['lifecycles'], [])
        self.assertFalse(collector.read_model_activity(self.home, other, 1000)['sources'][0]['complete'])
        self.assertEqual(collector.record_model_start(self.home, self.scope, 'physical', 'other-logical', 1100)['state'], 'INCOMPLETE')


if __name__ == '__main__':
    unittest.main()
