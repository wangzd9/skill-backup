#!/usr/bin/env python3
"""Lease regressions; use isolated projects and real short-lived worker processes."""
import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import role_runtime as runtime


class LeaseRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        runtime.save(self.root / 'ROLE-BINDINGS.json', {
            'schema_version': 1, 'project_root': str(self.root), 'control': 'ACTIVE',
            'roles': {'COORDINATOR': {'skill': 'complex-task-coordinator', 'session_file': 'sessions/COORDINATOR.md'}},
        })

    def execute(self, action, **overrides):
        args = argparse.Namespace(
            action=action, project=str(self.root), role='COORDINATOR',
            owner='worker', pid=None, token=None, ttl=300, timeout=60,
            confirmed_stopped=False, evidence=None, command=[],
        )
        for name, value in overrides.items():
            setattr(args, name, value)
        return runtime.execute(args)

    def lease(self):
        return runtime.load(runtime.lease_path(self.root, 'COORDINATOR'), {})

    def save_lease(self, **updates):
        row = self.lease()
        row.update(updates)
        runtime.save(runtime.lease_path(self.root, 'COORDINATOR'), row)
        return row

    def stopped_worker(self):
        worker = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        self.addCleanup(lambda: worker.poll() is None and (worker.terminate(), worker.wait()))
        claimed = self.execute('claim', pid=worker.pid)
        worker.terminate()
        worker.wait()
        return claimed

    def test_same_live_owner_renews_expired_token_without_sleep(self):
        claimed = self.execute('claim', pid=os.getpid())
        self.save_lease(expires_at=time.time() - 1)
        renewed = self.execute('renew', token=claimed['token'])
        self.assertTrue(renewed['renewed'])
        self.assertGreater(renewed['expires_at'], time.time())
        self.assertEqual(self.lease()['process_start'], runtime.pid_identity(os.getpid()))

    def test_expiration_cannot_take_over_a_live_worker(self):
        claimed = self.execute('claim', pid=os.getpid())
        self.save_lease(expires_at=0)
        result = self.execute('claim', owner='competing-worker', pid=os.getpid())
        self.assertFalse(result['claimed'])
        self.assertEqual(self.lease()['token'], claimed['token'])

    def test_proven_dead_worker_reclaimed_immediately_before_ttl(self):
        previous = self.stopped_worker()
        self.assertGreater(previous['expires_at'], time.time())
        result = self.execute('claim', owner='next-worker', pid=os.getpid())
        self.assertTrue(result['claimed'])
        self.assertNotEqual(result['token'], previous['token'])
        records = list((self.root / 'inbox/control/runtime/recoveries').glob('*.json'))
        self.assertEqual(len(records), 1)
        record = runtime.load(records[0])
        self.assertEqual(record['reason'], 'BOUND_OWNER_EXITED_OR_PID_REUSED')
        self.assertNotIn('token', record['lease'])
        with self.assertRaisesRegex(ValueError, 'token'):
            runtime.valid_lease(self.root, 'COORDINATOR', previous['token'])

    def test_dead_bound_owner_cannot_renew_or_authorize_writes(self):
        claimed = self.stopped_worker()
        with self.assertRaisesRegex(ValueError, 'Owner process'):
            self.execute('renew', token=claimed['token'])
        with self.assertRaisesRegex(ValueError, 'Owner process'):
            runtime.valid_lease(self.root, 'COORDINATOR', claimed['token'])
        marker = self.root / 'should-not-exist'
        with self.assertRaisesRegex(ValueError, 'Owner process'):
            self.execute('run', token=claimed['token'], command=[sys.executable, '-c', f'open({str(marker)!r}, "w").close()'])
        self.assertFalse(marker.exists())
        self.assertTrue(self.execute('release', token=claimed['token'])['released'])

    def test_reused_pid_cannot_renew_inherited_token(self):
        claimed = self.execute('claim', pid=os.getpid())
        with mock.patch.object(runtime, 'pid_identity', return_value='replacement-start'):
            with self.assertRaisesRegex(ValueError, 'Owner process'):
                self.execute('renew', token=claimed['token'])

    def test_exited_unreaped_worker_is_not_mistaken_for_live_owner(self):
        worker = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        self.addCleanup(worker.wait)
        self.execute('claim', pid=worker.pid)
        worker.terminate()
        os.waitid(os.P_PID, worker.pid, os.WEXITED | os.WNOWAIT)
        self.assertIsNone(runtime.pid_identity(worker.pid))
        self.assertTrue(self.execute('claim', owner='next', pid=os.getpid())['claimed'])

    def test_unverifiable_pid_is_neither_a_new_claim_nor_exit_evidence(self):
        with mock.patch.object(runtime, 'pid_identity', return_value='UNVERIFIABLE'):
            with self.assertRaisesRegex(ValueError, 'verifiably alive'):
                self.execute('claim', pid=os.getpid())
        claimed = self.execute('claim', pid=os.getpid())
        self.save_lease(expires_at=0)
        with mock.patch.object(runtime, 'pid_identity', return_value='UNVERIFIABLE'):
            self.assertFalse(self.execute('claim', owner='other')['claimed'])
            with self.assertRaisesRegex(ValueError, 'Owner process'):
                self.execute('renew', token=claimed['token'])

    def test_legacy_unbound_claim_and_expired_same_token_renew_remain_compatible(self):
        claimed = self.execute('claim')
        self.assertIsNone(claimed['pid'])
        self.save_lease(expires_at=0)
        self.assertFalse(self.execute('claim', owner='other')['claimed'])
        self.assertTrue(self.execute('renew', token=claimed['token'])['renewed'])
        self.assertTrue(self.execute('release', token=claimed['token'])['released'])

    def test_current_unbound_token_can_bind_live_worker_during_renewal(self):
        claimed = self.execute('claim')
        self.save_lease(expires_at=0)
        self.execute('renew', token=claimed['token'], pid=os.getpid())
        self.assertEqual(self.lease()['pid'], os.getpid())
        self.assertEqual(self.lease()['process_start'], runtime.pid_identity(os.getpid()))

    def test_live_lease_cannot_be_transferred_by_renew_pid(self):
        worker = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        self.addCleanup(lambda: worker.poll() is None and (worker.terminate(), worker.wait()))
        claimed = self.execute('claim', pid=worker.pid)
        with self.assertRaisesRegex(ValueError, 'Cannot transfer'):
            self.execute('renew', token=claimed['token'], pid=os.getpid())
        self.assertEqual(self.lease()['pid'], worker.pid)

    def test_explicit_dead_owner_recovery_needs_evidence_but_not_ttl_sleep(self):
        self.stopped_worker()
        with self.assertRaisesRegex(ValueError, 'verification evidence'):
            self.execute('recover', confirmed_stopped=True)
        evidence = self.root / 'owner-check.md'
        evidence.write_text('Bound PID/start exited, verified by test subprocess wait.\n')
        result = self.execute('recover', confirmed_stopped=True, evidence=str(evidence))
        self.assertTrue(result['recovered'])
        self.assertFalse(runtime.lease_path(self.root, 'COORDINATOR').exists())

    def test_unbound_recovery_keeps_expiry_and_explicit_evidence_gate(self):
        self.execute('claim')
        evidence = self.root / 'owner-check.md'
        evidence.write_text('Manual check.\n')
        with self.assertRaisesRegex(ValueError, 'not expired'):
            self.execute('recover', confirmed_stopped=True, evidence=str(evidence))

    def test_run_preserves_short_command_cap_and_true_exit_code(self):
        claimed = self.execute('claim', pid=os.getpid())
        with mock.patch.object(runtime.subprocess, 'run', return_value=argparse.Namespace(returncode=7)) as run:
            self.assertEqual(self.execute('run', token=claimed['token'], timeout=3600, command=['false']), 7)
        self.assertEqual(run.call_args.kwargs['timeout'], 90)

    def test_cli_concurrent_claims_have_exactly_one_owner(self):
        def claim(owner):
            result = subprocess.run([
                sys.executable, str(Path(runtime.__file__)), 'claim', '--project', str(self.root),
                '--role', 'COORDINATOR', '--owner', owner, '--pid', str(os.getpid()),
            ], text=True, capture_output=True, check=True)
            return json.loads(result.stdout)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, ['first', 'second']))
        self.assertEqual(sum(row['claimed'] for row in results), 1)


if __name__ == '__main__':
    unittest.main()
