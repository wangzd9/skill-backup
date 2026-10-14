#!/usr/bin/env python3
"""Exercise report routing against isolated task/dispatch documents, no Hermes calls."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).with_name('report_gate.py')


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'inbox/1号').mkdir(parents=True)
        self.write('ROLE-BINDINGS.json', json.dumps({'project_root': str(self.root), 'control': 'ACTIVE'}))
        self.write('TODO.md', '| TASK-01 | ✅ | result | AC-1 | none | DONE | 1号 |\n')

    def write(self, name, content):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    def run_mode(self, mode='inspect'):
        return subprocess.check_output([sys.executable, str(SCRIPT), '--project', str(self.root),
                                        '--role', 'A', '--mode', mode], text=True)

    def idle(self):
        return json.loads(self.run_mode())['confirmed_idle']

    def test_no_task_skips_model_and_outputs_one_line(self):
        self.assertTrue(self.idle())
        self.assertFalse(json.loads(self.run_mode('active-gate'))['wakeAgent'])
        text = self.run_mode('idle-message')
        self.assertEqual(len(text.strip().splitlines()), 1)
        self.assertIn('**执行者 A：无任务、无变化', text)

    def test_active_owner_keeps_report(self):
        self.write('TODO.md', '| TASK-01 | 🏃 | result | AC-1 | none | RUNNING | 1号 |\n')
        self.assertFalse(self.idle())
        self.assertEqual(self.run_mode('idle-message'), '')

    def test_child_task_keeps_identity_when_parent_is_accepted(self):
        self.write('TODO.md','| TASK-16C | ✅ | parent | AC | none | DONE | 协调者 |\n'
                   '| TASK-16C-A 查询接口 | REQ | TASK-16C | behavior | RUNNING / 1号 |\n')
        self.write('inbox/1号/TASK-16C-A/attempt-1-dispatch.md','# 派单\n- status: ACKED\n')
        result=json.loads(self.run_mode())
        self.assertFalse(result['confirmed_idle'])
        self.assertIn('TASK-16C-A',str(result['reasons']))

    def test_blocked_assigned_task_is_not_idle(self):
        self.write('TODO.md', '| TASK-01 | 🚫 | result | AC-1 | none | BLOCKED | 1号 |\n')
        self.assertFalse(self.idle())

    def test_new_dispatch_reopens_even_after_old_task_done(self):
        self.write('inbox/1号/TASK-02/attempt-1-dispatch.md', '# 派单\n- status: PENDING\n')
        self.assertFalse(self.idle())

    def test_unclassified_dispatch_keeps_report(self):
        self.write('inbox/1号/TASK-02/attempt-1.md', '# 派单：TASK-02\n')
        self.assertFalse(self.idle())

    def test_accepted_dispatch_and_done_parent_release_slot(self):
        self.write('inbox/1号/TASK-02/attempt-1-dispatch.md', '# 派单\n- status: ACCEPTED\n')
        self.write('inbox/1号/TASK-01/attempt-1-dispatch.md', '# 派单\n- status: ACKED\n')
        self.write('TODO.md', '| TASK-01 | ✅ | result | AC-1 | none | DONE | 1号 |\n')
        self.assertTrue(self.idle())

    def test_new_dispatch_for_done_task_needs_reclassification(self):
        self.write('inbox/1号/TASK-01/attempt-2-dispatch.md', '# 派单\n- status: PENDING\n')
        todo_mtime = (self.root / 'TODO.md').stat().st_mtime
        os.utime(self.root / 'inbox/1号/TASK-01/attempt-2-dispatch.md', (todo_mtime + 1, todo_mtime + 1))
        self.assertFalse(self.idle())

    def test_lease_unknown_liveness_is_not_idle(self):
        self.write('inbox/control/runtime/leases/A.json', json.dumps({'owner': 'unknown', 'expires_at': 0}))
        self.assertFalse(self.idle())

    def test_unreadable_task_table_fails_to_normal_report(self):
        self.write('TODO.md', 'Unreadable table')
        self.assertFalse(json.loads(self.run_mode())['confirmed_idle'])
        self.assertTrue(json.loads(self.run_mode('active-gate'))['wakeAgent'])

    def test_exact_acceptance_identity_resolves_legacy_dispatch(self):
        self.write('inbox/1号/TASK-02/post-merge-red-files.md', '# 派单\n- status: TEST_REPORT_PENDING\n')
        self.write('inbox/coordinator/TASK-02/result.md', '- status: ACCEPT\n- task/attempt: TASK-02 / post-merge-red-files\n')
        self.assertTrue(self.idle())

    def test_unrelated_acceptance_mention_does_not_close_dispatch(self):
        self.write('inbox/1号/TASK-02/attempt-1-dispatch.md', '# 派单\n- status: PENDING\n')
        self.write('inbox/coordinator/other.md', '- status: ACCEPT\n- task/attempt: TASK-01 / attempt-1\nNext: inbox/1号/TASK-02/attempt-1-dispatch.md\n')
        self.assertFalse(self.idle())

    def test_missing_binding_fails_open_for_report(self):
        (self.root / 'ROLE-BINDINGS.json').unlink()
        self.assertTrue(json.loads(self.run_mode('active-gate'))['wakeAgent'])
        self.assertEqual(self.run_mode('idle-message'), '')


if __name__ == '__main__':
    unittest.main()
