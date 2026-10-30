from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import live_progress
from runtime_common import save_json

AT = datetime.fromisoformat('2026-10-16T18:00:00+08:00')


class LiveProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.base = self.root/'inbox/control/runtime'
        save_json(self.root/'ROLE-BINDINGS.json', {'schema_version': 1, 'project_root': str(self.root),
            'control': 'ACTIVE', 'roles': {role: {} for role in ('COORDINATOR', *live_progress.ROLES)}})
        self.runtime = live_progress.runtime_module()
        import strong_review
        import ocr_runtime
        self.strong = strong_review; self.ocr = ocr_runtime
        ledger = strong_review.Ledger(self.root/'isolated-ledger')
        self.ledger = patch('strong_review.Ledger', return_value=ledger).start()
        self.addCleanup(patch.stopall)
        for role in live_progress.ROLES:
            self.write(f'dispatch-index/{role}.json', {'schema_version': 1, 'role': role,
                'observed_at': AT.isoformat(), 'entries': [], 'errors': [], 'verified_no_task': True})

    def write(self, source, value):
        path = self.base/source; save_json(path, value); return path

    def checkpoint(self, **extra):
        return self.write('checkpoints/A.json', {'schema_version': 1, 'role': 'A',
            'task': 'TASK-01', 'attempt': 'attempt-1', 'status': 'RUNNING', 'recorded_at': AT.isoformat(), **extra})

    def collect(self):
        return live_progress.collect_live_runtime(self.root, at=AT)

    def test_readonly_empty_roles_and_unconfigured_ledger_create_nothing(self):
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        value = self.collect()
        self.assertEqual(value['state'], 'VERIFIED')
        self.assertEqual(value['roles']['A']['availability']['status'], 'IDLE')
        self.assertEqual(value['strong_review']['state'], 'NOT_CONFIGURED')
        self.assertEqual(value['task_observation']['state'], 'NOT_CONFIGURED')
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_owner_death_and_pid_reuse_are_not_reported_as_working(self):
        self.checkpoint()
        self.write('leases/A.json', {'owner': 'worker', 'pid': 999999999, 'process_start': 'frozen'})
        for actual in (None, 'new-start'):
            with self.subTest(actual=actual), patch.object(self.runtime, 'pid_identity', return_value=actual):
                value = self.collect()['roles']['A']
                self.assertEqual(value['owner']['state'], 'exited_or_reused')
                self.assertEqual(value['state'], 'UNVERIFIED')
                self.assertEqual(value['availability']['status'], 'UNKNOWN')

    def test_fresh_operations_override_cached_active_claim_and_do_not_rerun(self):
        self.checkpoint()
        self.write('operations/A/run/result.json', {'operation_id': 'run', 'role': 'A',
            'task': 'TASK-01', 'attempt': 'attempt-1', 'status': 'RUNNING', 'pid': 999999999,
            'process_start': 'dead', 'boot_id': 'old'})
        value = self.collect()['roles']['A']
        self.assertEqual(value['operations'][0]['status'], 'UNKNOWN')
        self.assertIn('operation_unverifiable: run', value['errors'])
        self.assertEqual(value['state'], 'UNVERIFIED')

    def test_fixed_git_reads_show_actual_dirty_head_without_changing_checkpoint(self):
        worktree = self.root/'worktree'; worktree.mkdir()
        def git(*args):
            return subprocess.check_output(['git', '-C', str(worktree), *args], text=True, stderr=subprocess.DEVNULL).strip()
        git('init', '-b', 'task'); (worktree/'tracked').write_text('base')
        git('add', 'tracked'); git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-m', 'base')
        head = git('rev-parse', 'HEAD')
        checkpoint = self.checkpoint(worktree=str(worktree), head=head)
        (worktree/'tracked').write_text('changed')
        before = checkpoint.read_bytes()
        value = self.collect()['roles']['A']
        self.assertTrue(value['worktree']['dirty'])
        self.assertTrue(value['worktree']['checkpoint_head_match'])
        self.assertEqual(value['worktree']['head'], head)
        self.assertEqual(checkpoint.read_bytes(), before)
        self.assertNotIn('PASS', json.dumps(value))

    def test_missing_worktree_refers_to_checkpoint_even_with_operations(self):
        self.checkpoint()
        self.write('operations/A/run/result.json', {'operation_id': 'run', 'role': 'A',
            'task': 'TASK-01', 'attempt': 'attempt-1', 'status': 'SUCCEEDED'})
        value = self.collect()['roles']['A']
        self.assertEqual(value['worktree']['state'], 'NOT_CONFIGURED')
        self.assertEqual(value['worktree']['source'], 'inbox/control/runtime/checkpoints/A.json')

    def test_new_checkpoint_during_collection_is_visible_not_accepted_as_stable(self):
        path = self.checkpoint(worktree=str(self.root/'fake-worktree'))
        def changed(_):
            row = json.loads(path.read_text()); row['attempt'] = 'attempt-2'; save_json(path, row)
            return {'head': 'f'*40, 'dirty': False}
        with patch('live_progress._git', side_effect=changed):
            value = self.collect()
        self.assertEqual(value['state'], 'UNVERIFIED')
        self.assertIn('source_changed_during_collection: inbox/control/runtime/checkpoints/A.json', value['errors'])

    def test_git_timeout_is_explicit_without_echoing_raw_command_diagnostics(self):
        self.checkpoint(worktree=str(self.root/'worktree'))
        with patch('live_progress._git', side_effect=subprocess.TimeoutExpired('git', 3)):
            value = self.collect()['roles']['A']
        self.assertEqual(value['state'], 'UNVERIFIED')
        self.assertTrue(any('TimeoutExpired' in error for error in value['errors']))

    def test_worktree_or_active_operation_identity_conflicts_remain_explicit(self):
        self.checkpoint(worktree=str(self.root/'worktree'), head='a'*40)
        operation = self.write('operations/A/run/result.json', {'operation_id': 'run', 'role': 'A',
            'task': 'TASK-02', 'attempt': 'attempt-2', 'status': 'RUNNING', 'pid': os.getpid(),
            'process_start': 'start', 'boot_id': 'boot'})
        with patch('operation_jobs.process_identity', return_value={'process_start': 'start', 'boot_id': 'boot'}), \
                patch('live_progress._git', return_value={'head': 'b'*40, 'dirty': False}):
            value = self.collect()['roles']['A']
        self.assertEqual(value['state'], 'UNVERIFIED')
        self.assertIn('worktree_HEAD_differs_from_recorded_checkpoint', value['errors'])
        self.assertIn('active_operation_checkpoint_identity_conflict: run', value['errors'])

    def test_new_operation_state_during_inspection_is_not_hidden(self):
        self.checkpoint()
        source = self.write('operations/A/run/result.json', {'operation_id': 'run', 'role': 'A',
            'task': 'TASK-01', 'attempt': 'attempt-1', 'status': 'PENDING'})
        def changed(root, role):
            if role != 'A':
                return []
            save_json(source, {'operation_id': 'run', 'status': 'FAILED'})
            return [{'operation_id': 'run', 'status': 'PENDING', 'source': str(source)}]
        with patch('live_progress.operation_states', side_effect=changed):
            value = self.collect()
        self.assertEqual(value['state'], 'UNVERIFIED')
        self.assertIn('source_changed_during_collection: inbox/control/runtime/operations/A/run/result.json', value['errors'])

    def test_ocr_service_and_process_conflicts_are_fresh_and_readonly(self):
        directory = self.root/'reviews/ocr/run'; unit = 'coordinator-ocr-'+'a'*32+'.service'
        self.write('ocr/review.json', {'run': str(directory), 'unit': unit})
        save_json(directory/'spec.json', {'schema': 'coordinator.ocr-run/v1', 'project': str(self.root),
            'run_id': 'a'*32, 'unit': unit, 'task': 'TASK-01', 'attempt': 'attempt-1',
            'review_identity': 'fixed-review', 'base': 'a'*40, 'head': 'b'*40})
        save_json(directory/'state.json', {'state': 'RUNNING', 'pid': 999999999, 'run_id': 'a'*32, 'unit': unit})
        with patch.object(self.ocr, 'status', return_value={'state': 'INTERRUPTED',
                'service': {'ActiveState': 'inactive'}}) as observed:
            value = self.collect()['ocr'][0]
        self.assertEqual(observed.call_args.args, (directory,))
        self.assertEqual(value['state'], 'UNVERIFIED')
        self.assertEqual(value['recorded_state'], 'RUNNING')
        self.assertEqual(value['observed_state'], 'INTERRUPTED')
        save_json(directory/'state.json', {'state': 'AWAITING_DECISION', 'pid': 999999999, 'run_id': 'a'*32, 'unit': unit})
        with patch.object(self.ocr, 'status', return_value={'state': 'AWAITING_DECISION',
                'service': {'ActiveState': 'inactive'}}):
            terminal = self.collect()['ocr'][0]
        self.assertEqual(terminal['state'], 'VERIFIED')
        self.assertEqual(terminal['worker_process_state'], 'not_applicable')

    def test_legacy_ocr_without_runtime_index_is_still_checked(self):
        directory = self.root/'reviews/legacy/run'; unit = 'coordinator-ocr-'+'a'*32+'.service'
        save_json(directory/'spec.json', {'schema': 'coordinator.ocr-run/v1', 'project': str(self.root),
            'run_id': 'a'*32, 'unit': unit, 'task': 'TASK-01', 'attempt': 'attempt-1',
            'review_identity': 'fixed-review', 'base': 'a'*40, 'head': 'b'*40})
        save_json(directory/'state.json', {'state': 'RUNNING', 'run_id': 'a'*32, 'unit': unit})
        with patch.object(self.ocr, 'status', return_value={'state': 'INTERRUPTED', 'service': {'ActiveState': 'inactive'}}) as observed:
            result = self.collect()
        self.assertEqual(len(result['ocr']), 1)
        observed.assert_called_once_with(directory)
        self.assertEqual(result['ocr'][0]['observed_state'], 'INTERRUPTED')
        self.assertEqual(result['ocr'][0]['state'], 'UNVERIFIED')

    def ocr_record(self, name, state, stamp):
        directory = self.root/'reviews'/name
        unit = 'coordinator-ocr-'+('b' if name == 'old-prepared' else 'a')*32+'.service'
        save_json(directory/'spec.json', {'schema': 'coordinator.ocr-run/v1', 'project': str(self.root),
            'run_id': name, 'unit': unit, 'task': 'TASK-01', 'attempt': 'attempt-1',
            'review_identity': 'fixed-review', 'base': 'a'*40, 'head': 'b'*40})
        save_json(directory/'state.json', {'state': state, 'run_id': name, 'unit': unit, 'updated_at': stamp})
        return directory

    def test_superseded_prepared_legacy_run_is_not_polled_as_inflight(self):
        self.ocr_record('old-prepared', 'PREPARED', '2026-10-15T10:00:00+08:00')
        current = self.ocr_record('current', 'AWAITING_DECISION', '2026-10-16T17:46:00+08:00')
        with patch.object(self.ocr, 'status', return_value={'state': 'AWAITING_DECISION',
                'service': {'ActiveState': 'inactive'}}) as status:
            value = self.collect()['ocr']
        status.assert_called_once_with(current)
        self.assertEqual(len(value), 1)
        self.assertFalse(value[0]['in_flight'])
        self.assertEqual(value[0]['recorded_at'], '2026-10-16T17:46:00+08:00')

    def test_latest_prepared_record_is_explicitly_not_inflight(self):
        self.ocr_record('prepared', 'PREPARED', AT.isoformat())
        with patch.object(self.ocr, 'status', return_value={'state': 'PREPARED',
                'service': {'ActiveState': 'inactive'}}):
            value = self.collect()['ocr'][0]
        self.assertEqual(value['state'], 'VERIFIED')
        self.assertFalse(value['in_flight'])
        self.assertFalse(value['service_active'])

    def test_prepared_service_conflict_and_discovery_errors_remain_unverified(self):
        self.ocr_record('prepared', 'PREPARED', AT.isoformat())
        with patch.object(self.ocr, 'status', return_value={'state': 'PREPARED',
                'service': {'ActiveState': 'active'}}):
            value = self.collect()['ocr'][0]
        self.assertEqual(value['state'], 'UNVERIFIED')
        self.assertIsNone(value['in_flight'])
        self.assertIn('OCR_prepared_record_service_active_conflict', value['errors'])
        result = live_progress.collect_live_runtime(self.root, at=AT,
            ocr_candidates=[{'source': '../outside/state.json'}])
        self.assertEqual(result['state'], 'UNVERIFIED')
        self.assertIn('OCR_candidate_unavailable:', result['ocr'][0]['errors'][0])

    def test_malformed_ocr_index_does_not_erase_other_live_observations(self):
        self.write('ocr/bad.json', {'run': '../outside'})
        result = live_progress.collect_live_runtime(self.root, at=AT, ocr_candidates=[])
        self.assertEqual(result['state'], 'UNVERIFIED')
        self.assertEqual(result['ocr'][0]['source'], 'inbox/control/runtime/ocr/bad.json')
        self.assertEqual(result['roles']['A']['owner']['state'], 'none')

    def test_failed_business_discovery_still_checks_healthy_legacy_run_and_role(self):
        current = self.ocr_record('healthy', 'AWAITING_DECISION', AT.isoformat())
        self.write('ocr/bad.json', {'run': '../outside'})
        with patch.object(self.ocr, 'status', return_value={'state': 'AWAITING_DECISION',
                'service': {'ActiveState': 'inactive'}}) as status:
            result = self.collect()
        status.assert_called_once_with(current)
        healthy = [row for row in result['ocr'] if row.get('run') == 'reviews/healthy']
        self.assertEqual('VERIFIED', healthy[0]['state'])
        self.assertEqual('VERIFIED', result['roles']['A']['state'])
        self.assertEqual([], result['errors'])
        self.assertEqual('UNVERIFIED', result['state'])
        self.assertTrue(any(row['source'] == 'OCR发现索引' for row in result['ocr']))

    def test_unbound_role_is_not_read_from_old_migrated_checkpoint(self):
        binding = json.loads((self.root/'ROLE-BINDINGS.json').read_text())
        binding['roles'].pop('A')
        save_json(self.root/'ROLE-BINDINGS.json', binding)
        self.checkpoint(status='PRIVATE_OLD_ROLE_STATE')
        result = self.collect()
        self.assertNotIn('A', result['roles'])
        self.assertNotIn('PRIVATE_OLD_ROLE_STATE', json.dumps(result))

    def test_strong_service_identity_failure_is_never_claimed_normal(self):
        database = self.root/'ledger.db'; database.touch()
        ledger = self.strong.Ledger()
        ledger.database = database
        with patch.object(ledger, 'status', return_value={'active_calls': [
                {'id': 'running', 'service_identity': 'UNKNOWN'}]}):
            value = self.collect()['strong_review']
        self.assertEqual(value['state'], 'UNVERIFIED')
        self.assertEqual(value['errors'], ['strong_service_identity_unverifiable: running'])

    def task_spec(self):
        spec = {'observation_id': 'existing-run', 'run_id': 'run-01', 'task': 'TASK-01', 'attempt': 1,
            'container': 'existing-container', 'remote_alias': 'configured-host', 'notify_roles': ['COORDINATOR'],
            'backend_url': 'http://127.0.0.1:8000', 'storage_path': '/mnt/existing', 'business_timezone': 'Asia/Shanghai'}
        return self.write('task-observation-spec.json', spec), spec

    def test_configured_task_is_sampled_now_without_publishing_or_reusing_latest(self):
        import task_observation
        path, spec = self.task_spec()
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch.object(task_observation, 'sample', return_value={'errors': {}, 'observed_at': AT.isoformat(),
                'pipeline': {'status': 'RUNNING'}}) as sample, \
                patch.object(task_observation, '_publish', side_effect=AssertionError('no notices')), \
                patch.object(task_observation, 'sample_registered', side_effect=AssertionError('no publishing wrapper')):
            value = self.collect()['task_observation']
        self.assertEqual(sample.call_args.args, (spec,))
        self.assertEqual(sample.call_args.kwargs, {'now': AT})
        self.assertEqual(value['state'], 'VERIFIED')
        self.assertEqual(value['value']['pipeline']['status'], 'RUNNING')
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_probe_failure_and_changed_configuration_are_visible_without_retry(self):
        import task_observation
        path, spec = self.task_spec()
        with patch.object(task_observation, 'sample', return_value={'errors': {'remote': {'type': 'TimeoutExpired'}}}) as sample:
            value = self.collect()['task_observation']
        self.assertEqual(sample.call_count, 1)
        self.assertEqual(value['state'], 'UNVERIFIED')
        self.assertEqual(value['errors'], ['remote: TimeoutExpired'])
        def changed(*args, **kwargs):
            save_json(path, {**spec, 'run_id': 'run-02'}); return {'errors': {}}
        with patch.object(task_observation, 'sample', side_effect=changed):
            result = self.collect()
        self.assertEqual(result['state'], 'UNVERIFIED')
        self.assertIn('source_changed_during_collection: inbox/control/runtime/task-observation-spec.json', result['errors'])


if __name__ == '__main__':
    unittest.main()
