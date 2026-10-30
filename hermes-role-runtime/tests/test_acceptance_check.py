import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from acceptance_check import check_acceptance, freeze_verification


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root/'repo'
        self.repo.mkdir()
        self.git('init', '-q')
        (self.repo/'migration.py').write_text('buggy downgrade')
        self.git('add', '.')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@invalid', 'commit', '-qm', 'base')
        self.head = self.git('rev-parse', 'HEAD')
        self.folder = self.root/'test-receipts/TASK-1'
        self.folder.mkdir(parents=True)
        self.receipt = self.folder/'mysql.md'
        self.receipt.write_text('MySQL downgrade 1553 FAIL')
        self.event('failure', status='DELIVERED', summary='MySQL downgrade D FAIL', head=self.head)
        self.spec = {'task': 'TASK-1', 'repo': str(self.repo), 'head': self.head}

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True).strip()

    def event(self, identifier, **fields):
        path = self.root/'events/TESTER'/(identifier+'.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {'role': 'TESTER', 'task': 'TASK-1', 'attempt': 'attempt-1',
               'event_id': 'TESTER:'+identifier, 'status': 'DELIVERED', 'summary': '实际测试结果', **fields}
        path.write_text(json.dumps(row))
        return path

    def passed(self, supersedes=('TESTER:failure',), identifier='pass', receipt=None):
        if receipt is not None:
            self.receipt = receipt
        self.receipt.write_text('MySQL upgrade/downgrade verified PASS on '+self.head)
        verification = freeze_verification(self.root, 'TESTER', {'head': self.head, 'result': 'PASS',
            'receipt': str(self.receipt.relative_to(self.root)), 'supersedes': list(supersedes)}, 'TASK-1')
        self.event(identifier, verification=verification, summary='前次失败经实际复验已修复')

    def test_ocr_clean_or_new_commit_never_clears_prior_independent_fail(self):
        (self.repo/'identity.py').write_text('new identity fix, migration unchanged')
        self.git('add', '.')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@invalid', 'commit', '-qm', 'identity only')
        self.spec['head'] = self.git('rev-parse', 'HEAD')
        ocr = self.root/'reviews/ocr.json'
        ocr.parent.mkdir()
        ocr.write_text(json.dumps({'findings': [], 'head': self.spec['head']}))
        result = check_acceptance(self.root, self.spec)
        self.assertEqual('BLOCKED', result['state'])
        self.assertIn('TESTER:failure', result['failure_event_ids'])
        self.assertIn('UNRESOLVED_TEST_FAILURE_OR_GATE', result['reasons'])

    def test_exact_current_head_pass_requires_explicit_failure_closure(self):
        self.passed(supersedes=())
        self.assertEqual('BLOCKED', check_acceptance(self.root, self.spec)['state'])
        self.passed()
        result = check_acceptance(self.root, self.spec)
        self.assertEqual('TEST_GATE_CLEAR', result['state'])
        self.assertEqual(['TESTER:pass'], result['pass_event_ids'])

    def test_new_receipt_or_changed_receipt_invalidates_previous_pass(self):
        self.passed()
        self.assertEqual('TEST_GATE_CLEAR', check_acceptance(self.root, self.spec)['state'])
        (self.folder/'new-failure.md').write_text('new failure before merge')
        self.assertEqual('BLOCKED', check_acceptance(self.root, self.spec)['state'])
        (self.folder/'new-failure.md').unlink()
        self.receipt.write_text('replaced receipt')
        self.assertEqual('BLOCKED', check_acceptance(self.root, self.spec)['state'])

    def test_different_head_dirty_worktree_and_unknown_role_never_clear(self):
        self.passed()
        (self.repo/'migration.py').write_text('work after snapshot')
        self.assertIn('CANDIDATE_WORKTREE_DIRTY', check_acceptance(self.root, self.spec)['reasons'])
        with self.assertRaisesRegex(ValueError, '独立测试者'):
            freeze_verification(self.root, 'COORDINATOR', {}, 'TASK-1')
        self.spec['head'] = 'main'
        with self.assertRaisesRegex(ValueError, '完整冻结'):
            check_acceptance(self.root, self.spec)

    def test_new_failure_event_cannot_be_swallowed_by_prior_pass(self):
        self.passed()
        self.event('late-fail', summary='FAIL new independent finding')
        result = check_acceptance(self.root, self.spec)
        self.assertEqual('BLOCKED', result['state'])
        self.assertIn('TESTER:late-fail', result['failure_event_ids'])

    def test_collection_race_is_detected(self):
        self.passed()
        from acceptance_check import _inputs
        calls = 0
        def changed(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.event('race', summary='FAIL arrived during check')
            return _inputs(*args)
        with patch('acceptance_check._inputs', side_effect=changed):
            result = check_acceptance(self.root, self.spec)
        self.assertIn('TEST_INPUT_CHANGED_DURING_CHECK', result['reasons'])

    def test_fresh_pass_can_replace_stale_pass_after_new_receipt(self):
        self.passed(identifier='pass-old')
        (self.folder/'additional.md').write_text('Additional acceptance case PASS')
        self.passed(identifier='pass-new')
        result = check_acceptance(self.root, self.spec)
        self.assertEqual('TEST_GATE_CLEAR', result['state'])
        self.assertEqual(['TESTER:pass-new'], result['pass_event_ids'])
        self.assertEqual([], result['reasons'])
        self.assertIn('TEST_RECEIPT_CHANGED_OR_MISSING:TESTER:pass-old',
                      result['historical_invalid_candidates'])

    def test_missing_historical_receipt_does_not_prevent_fresh_proof(self):
        self.passed(identifier='pass-old')
        self.receipt.unlink()
        self.passed(identifier='pass-new', receipt=self.folder/'new-mysql.md')
        result = check_acceptance(self.root, self.spec)
        self.assertEqual('TEST_GATE_CLEAR', result['state'])
        self.assertEqual(['TESTER:pass-new'], result['pass_event_ids'])
        self.assertIn('UNAVAILABLE:FileNotFoundError', [item['sha256'] for item in result['sources']])

    def test_supersedes_requires_existing_same_task_events(self):
        for identifiers in (['TESTER:future'], ['TESTER:failure', 'TESTER:failure'], [{}]):
            with self.subTest(identifiers=identifiers), self.assertRaises(ValueError):
                freeze_verification(self.root, 'TESTER', {'head': self.head, 'result': 'PASS',
                    'receipt': str(self.receipt.relative_to(self.root)), 'supersedes': identifiers}, 'TASK-1')
        self.event('other-task', task='TASK-2', summary='FAIL from other task')
        with self.assertRaisesRegex(ValueError, '其他任务'):
            self.passed(supersedes=['TESTER:other-task'])

    def test_superseded_failure_is_hash_bound_and_cannot_change_after_pass(self):
        self.passed()
        path = self.root/'events/TESTER/failure.json'
        value = json.loads(path.read_text())
        value['summary'] = 'FAIL changed details after independent PASS'
        path.write_text(json.dumps(value))
        result = check_acceptance(self.root, self.spec)
        self.assertEqual('BLOCKED', result['state'])
        self.assertIn('CURRENT_HEAD_INDEPENDENT_PASS_REQUIRED', result['reasons'])

    def test_old_pass_without_frozen_superseded_event_hash_is_not_current_proof(self):
        self.passed()
        path = self.root/'events/TESTER/pass.json'
        value = json.loads(path.read_text())
        del value['verification']['supersedes_event_sha256']
        path.write_text(json.dumps(value))
        self.assertEqual('BLOCKED', check_acceptance(self.root, self.spec)['state'])

    def test_candidate_head_changed_during_collection_is_blocked(self):
        self.passed()
        from acceptance_check import _inputs
        calls = 0
        def changed(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                (self.repo/'identity.py').write_text('new implementation during check')
                self.git('add', '.')
                self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@invalid',
                         'commit', '-qm', 'racing candidate')
            return _inputs(*args)
        with patch('acceptance_check._inputs', side_effect=changed):
            result = check_acceptance(self.root, self.spec)
        self.assertIn('CANDIDATE_HEAD_CHANGED_DURING_CHECK', result['reasons'])

    def test_candidate_dirty_during_collection_is_blocked(self):
        self.passed()
        from acceptance_check import _inputs
        calls = 0
        def changed(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                (self.repo/'migration.py').write_text('uncommitted implementation during check')
            return _inputs(*args)
        with patch('acceptance_check._inputs', side_effect=changed):
            result = check_acceptance(self.root, self.spec)
        self.assertIn('CANDIDATE_WORKTREE_CHANGED_DURING_CHECK', result['reasons'])
        self.assertIn('CANDIDATE_WORKTREE_DIRTY', result['reasons'])


if __name__ == '__main__':
    unittest.main()
