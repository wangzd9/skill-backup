import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from delivery_evidence import execute_with_evidence, check_evidence
from operation_jobs import inspect_state


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root/'code'
        self.repo.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.name', 'Isolated test')
        self.git('config', 'user.email', 'test@localhost')
        (self.repo/'code.py').write_text('value = 1\n')
        self.git('add', 'code.py')
        self.git('commit', '-qm', 'isolated fixture')
        self.config = self.root/'config.json'
        self.config.write_text('{"setting": 1, "password": "DO_NOT_PRINT_SECRET"}')
        self.state = self.root/'operations/test/result.json'
        self.plan = {'operation_id': 'test-1', 'cwd': str(self.repo), 'role': 'TESTER',
                     'task': 'TASK-1', 'attempt': 'attempt-1',
                     'steps': [{'argv': [sys.executable, '-c', 'print("isolated result")']}],
                     'evidence': {'config': [str(self.config)], 'environment': ['QUALITY_TEST_ENV']}}

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), *args], capture_output=True, check=True)

    def run_plan(self):
        result = execute_with_evidence(self.plan, self.state)
        return result, Path(result['evidence'])

    def test_success_proves_only_mechanical_match_and_does_not_leak_config(self):
        result, evidence = self.run_plan()
        checked = check_evidence(evidence)
        self.assertTrue(checked['mechanical_match'])
        self.assertFalse(checked['business_pass'])
        self.assertFalse(result['business_pass'])
        self.assertNotIn('DO_NOT_PRINT_SECRET', evidence.read_text())
        with self.assertRaisesRegex(ValueError, '禁止重新执行'):
            self.run_plan()

    def test_code_change_after_testing_invalidates_evidence(self):
        result, evidence = self.run_plan()
        (self.repo/'code.py').write_text('value = 2\n')
        checked = check_evidence(evidence)
        self.assertFalse(checked['mechanical_match'])
        self.assertIn('当前代码', ' '.join(checked['reasons']))

    def test_dirty_and_staged_work_are_bound_to_actual_content(self):
        (self.repo/'code.py').write_text('value = 2\n')
        self.git('add', 'code.py')
        (self.repo/'code.py').write_text('value = 3\n')
        result, evidence = self.run_plan()
        row = json.loads(evidence.read_text())
        self.assertTrue(row['before']['identity']['dirty'])
        self.assertTrue(check_evidence(evidence)['mechanical_match'])
        self.git('reset', '-q', 'HEAD')
        # Worktree bytes unchanged, but the independently recorded index changed.
        self.assertFalse(check_evidence(evidence)['mechanical_match'])

    def test_code_mutation_during_test_is_not_verified(self):
        self.plan['steps'] = [{'argv': [sys.executable, '-c', 'from pathlib import Path; Path("code.py").write_text("value = 4\\n")']}]
        result, evidence = self.run_plan()
        self.assertEqual(result['exit_code'], 0)
        self.assertFalse(result['snapshot_match'])
        self.assertFalse(check_evidence(evidence)['mechanical_match'])

    def test_failure_stops_later_steps_and_records_real_exit(self):
        self.plan['steps'] = [{'argv': [sys.executable, '-c', 'raise SystemExit(7)']},
                              {'argv': [sys.executable, '-c', 'from pathlib import Path; Path("forbidden").touch()']}]
        result, evidence = self.run_plan()
        self.assertEqual(result['exit_code'], 7)
        self.assertFalse((self.repo/'forbidden').exists())
        self.assertEqual(json.loads(evidence.read_text())['exit_code'], 7)
        self.assertFalse(check_evidence(evidence)['mechanical_match'])

    def test_config_env_log_and_untracked_changes_invalidate(self):
        with patch.dict(os.environ, {'QUALITY_TEST_ENV': 'secret-value'}):
            result, evidence = self.run_plan()
            self.assertNotIn('secret-value', evidence.read_text())
            self.assertTrue(check_evidence(evidence)['mechanical_match'])
            with patch.dict(os.environ, {'QUALITY_TEST_ENV': 'changed'}):
                self.assertFalse(check_evidence(evidence)['mechanical_match'])
            (self.repo/'new.py').write_text('new = 1\n')
            self.assertFalse(check_evidence(evidence)['mechanical_match'])
            (self.repo/'new.py').unlink()
            self.config.write_text('{"setting":2}')
            self.assertFalse(check_evidence(evidence)['mechanical_match'])
            Path(result['log']).write_text('tampered output')
            self.assertIn('log 文件缺失或已变化', check_evidence(evidence)['reasons'])

    def test_output_inside_repository_is_rejected_before_execution(self):
        with self.assertRaisesRegex(ValueError, '代码仓库外'):
            execute_with_evidence(self.plan, self.repo/'output/result.json')
        self.assertFalse((self.repo/'output/result.before.json').exists())

    def test_worker_snapshot_failure_is_terminal_not_permanent_pending(self):
        self.plan['evidence']['inputs'] = [str(self.root/'missing-input')]
        spec = self.root/'spec.json'
        spec.write_text(json.dumps(self.plan))
        self.state.parent.mkdir(parents=True)
        self.state.write_text(json.dumps({'status': 'PENDING', 'operation_id': 'test-1'}))
        encoded = json.dumps(self.plan, ensure_ascii=False, sort_keys=True).encode()
        result = subprocess.run([sys.executable, str(ROOT/'runtime_tools.py'), 'worker', '--spec', str(spec),
                                 '--state', str(self.state), '--sha256', hashlib.sha256(encoded).hexdigest()],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(inspect_state(self.state)['status'], 'FAILED')
        self.assertFalse(self.state.with_suffix('.before.json').exists())

    def test_worker_malformed_spec_does_not_leave_permanent_pending(self):
        spec = self.root/'invalid.json'
        spec.write_text('{')
        self.state.parent.mkdir(parents=True)
        self.state.write_text(json.dumps({'status': 'PENDING', 'operation_id': 'test-1'}))
        result = subprocess.run([sys.executable, str(ROOT/'runtime_tools.py'), 'worker', '--spec', str(spec),
                                 '--state', str(self.state), '--sha256', 'irrelevant'], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(inspect_state(self.state)['status'], 'FAILED')


if __name__ == '__main__':
    unittest.main()
