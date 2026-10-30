#!/usr/bin/env python3
"""Lease regressions; use isolated projects and real short-lived worker processes."""
import argparse
import concurrent.futures
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import role_runtime as runtime


class LeaseRecoveryTests(unittest.TestCase):
    """Lease lifecycle only.

    PID provenance has its own class below: here every claim would otherwise need
    a real `cron.scheduler` ancestor, which a unit test process can never have.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        runtime.save(self.root / 'ROLE-BINDINGS.json', {
            'schema_version': 1, 'project_root': str(self.root), 'control': 'ACTIVE',
            'roles': {'COORDINATOR': {'skill': 'complex-task-coordinator', 'session_file': 'sessions/COORDINATOR.md'}},
        })
        pin = mock.patch.object(runtime, 'verify_worker_pid', return_value=None)
        pin.start()
        self.addCleanup(pin.stop)

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

    def test_unbound_claim_is_marked_and_warns(self):
        """An unbound lease is a weaker guarantee, so it must not look identical."""
        claimed = self.execute('claim')
        self.assertIsNone(claimed['pid'])
        self.assertEqual(claimed['binding'], 'unbound')
        self.assertIn('no process identity', claimed['warning'])
        self.assertEqual(self.lease()['binding'], 'unbound')

    def test_bound_claim_carries_no_unbound_marker(self):
        claimed = self.execute('claim', pid=os.getpid())
        self.assertNotIn('binding', claimed)
        self.assertNotIn('warning', claimed)

    def test_cli_concurrent_claims_have_exactly_one_owner(self):
        """Two racing CLI runs must produce one owner; this exercises the file lock.

        The claim is unbound here (no --pid) so the test measures locking alone and
        does not depend on this process's ancestry.
        """
        def claim(owner):
            result = subprocess.run([
                sys.executable, str(Path(runtime.__file__)), 'claim', '--project', str(self.root),
                '--role', 'COORDINATOR', '--owner', owner,
            ], text=True, capture_output=True)
            return json.loads(result.stdout) if result.returncode == 0 else {'claimed': False,
                                                                            'err': result.stderr[:120]}

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, ['first', 'second']))
        self.assertEqual(sum(row['claimed'] for row in results), 1, results)


class WorkerPidProvenanceTests(unittest.TestCase):
    """Only this session's own cron worker may own a lease.

    These run against the real /proc tree: a fake long-lived PID is exactly what
    stranded roles in production, so mocking the check would test nothing.
    """

    @classmethod
    def setUpClass(cls):
        # A real cron-shaped worker we can attach to, plus a plausible decoy.
        cls.worker = subprocess.Popen(
            [sys.executable, '-c', 'import time; time.sleep(30)', 'cron.scheduler', '--external-worker-file', '/dev/null'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        if cls.worker.poll() is None:
            cls.worker.terminate()
            cls.worker.wait()

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

    def test_shared_gateway_pid_is_refused(self):
        gateway = self.worker.pid
        with mock.patch.object(runtime, 'gateway_pid', return_value=gateway):
            with self.assertRaisesRegex(ValueError, 'shared gateway PID'):
                self.execute('claim', pid=gateway)
        self.assertFalse(runtime.lease_path(self.root, 'COORDINATOR').exists())

    def test_gateway_detected_from_cmdline_when_systemd_is_unavailable(self):
        """Without systemd metadata the command line alone must still catch it."""
        gatewayish = subprocess.Popen(
            [sys.executable, '-c', 'import time; time.sleep(30)', 'hermes_cli.main', 'gateway', 'run'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: gatewayish.poll() is None and (gatewayish.terminate(), gatewayish.wait()))
        time.sleep(0.2)
        with mock.patch.object(runtime, 'gateway_pid', return_value=None):
            with self.assertRaisesRegex(ValueError, 'shared gateway PID'):
                self.execute('claim', pid=gatewayish.pid)

    def test_non_worker_process_is_refused(self):
        """A live shell or unrelated daemon dies before the turn: refuse it."""
        other = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        self.addCleanup(lambda: other.poll() is None and (other.terminate(), other.wait()))
        with mock.patch.object(runtime, 'gateway_pid', return_value=os.getppid()):
            with self.assertRaisesRegex(ValueError, 'is not a cron worker'):
                self.execute('claim', pid=other.pid)

    def test_foreign_worker_is_refused(self):
        """A real worker belonging to another turn must not be borrowed."""
        with mock.patch.object(runtime, 'gateway_pid', return_value=os.getppid()):
            with self.assertRaisesRegex(ValueError, 'not this session'):
                self.execute('claim', pid=self.worker.pid)

    def test_own_worker_in_ancestor_chain_is_accepted(self):
        with mock.patch.object(runtime, 'gateway_pid', return_value=os.getppid()), \
             mock.patch.object(runtime, 'ancestry', return_value=iter([self.worker.pid, os.getppid()])):
            claimed = self.execute('claim', pid=self.worker.pid)
        self.assertTrue(claimed['claimed'])
        self.assertEqual(claimed['pid'], self.worker.pid)
        self.assertEqual(claimed['process_start'], runtime.pid_identity(self.worker.pid))
        self.assertNotIn('binding', claimed)

    def test_unbound_claim_still_works_and_is_flagged(self):
        claimed = self.execute('claim')
        self.assertTrue(claimed['claimed'])
        self.assertEqual(claimed['binding'], 'unbound')
        self.assertIn('no process identity', claimed['warning'])

    def test_renew_cannot_rebind_to_gateway(self):
        claimed = self.execute('claim')
        with mock.patch.object(runtime, 'gateway_pid', return_value=self.worker.pid):
            with self.assertRaisesRegex(ValueError, 'shared gateway PID'):
                self.execute('renew', token=claimed['token'], pid=self.worker.pid)
        self.assertIsNone(runtime.load(runtime.lease_path(self.root, 'COORDINATOR'))['pid'])


class PoolScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pool = self.root/'pool.json'
        self.data = {'project_root': str(self.root), 'control':'ACTIVE', 'roles':{},
                     'role_pool':{'path':str(self.pool),'generation':2}}
        clean = mock.patch.dict(os.environ, {}, clear=True)
        clean.start(); self.addCleanup(clean.stop)

    def test_binding_generation_checked_without_group_full_gate(self):
        module = mock.Mock()
        with mock.patch.object(runtime,'requirement_module',return_value=module):
            runtime.guard(self.data,'A')
        module.validate_role_scope.assert_called_once_with(str(self.pool),'A',str(self.root),2)

    def test_old_worker_cannot_claim_new_root_or_generation(self):
        environment = {'HERMES_ROLE_POOL_PATH':str(self.pool), 'HERMES_ROLE_POOL_GENERATION':'1',
                       'HERMES_ROLE_NAME':'A', 'HERMES_ROLE_PROJECT_ROOT':str(self.root)}
        with mock.patch.dict(os.environ,environment):
            with self.assertRaisesRegex(ValueError,'GENERATION_CHANGED'):
                runtime.guard(self.data,'A')
        environment['HERMES_ROLE_POOL_GENERATION']='2'
        environment['HERMES_ROLE_PROJECT_ROOT']=str(self.root/'old')
        with mock.patch.dict(os.environ,environment):
            with self.assertRaisesRegex(ValueError,'FROZEN_SCOPE_MISMATCH'):
                runtime.guard(self.data,'A')

    def test_missing_runtime_does_not_allow_pooled_write(self):
        with mock.patch.object(runtime,'requirement_module',side_effect=ValueError('missing pool runtime')):
            with self.assertRaisesRegex(ValueError,'missing pool runtime'):
                runtime.guard(self.data,'A')

    def test_active_business_authority_fences_old_tokens_but_staged_preserves_legacy(self):
        from business_ledger import BusinessLedger
        ledger = BusinessLedger(self.pool.with_suffix('.runtime')/'business.sqlite3')
        module = mock.Mock(__file__='/home/wang/work/hermes-role-runtime/requirement_runtime.py')
        with mock.patch.object(runtime, 'requirement_module', return_value=module):
            runtime.guard(self.data, 'A')  # STAGED remains the old authority.
            result = ledger.set_mode('ACTIVE', command_id='isolated-activation')
            self.assertEqual(result['status'], 'COMMITTED')
            with self.assertRaisesRegex(ValueError, 'LEGACY_WRITER_DISABLED'):
                runtime.guard(self.data, 'A')

    def test_legacy_claim_waits_for_activation_lock_then_rechecks_authority(self):
        from business_ledger import BusinessLedger
        ledger = BusinessLedger(self.pool.with_suffix('.runtime')/'business.sqlite3')
        self.data.update(schema_version=1, roles={'A': {'skill': 'fixture', 'session_file': 'A.md'}})
        runtime.save(self.root/'ROLE-BINDINGS.json', self.data)
        admission = self.pool.with_suffix('.admission.lock')
        original_lock, waiting = runtime.locked, threading.Event()
        @contextlib.contextmanager
        def observed_lock(path):
            if Path(path) == admission and threading.current_thread() is not threading.main_thread():
                waiting.set()
            with original_lock(path):
                yield
        args = argparse.Namespace(action='claim', project=str(self.root), role='A', owner='old-owner', pid=None, ttl=300)
        module = mock.Mock(__file__='/home/wang/work/hermes-role-runtime/requirement_runtime.py')
        with mock.patch.object(runtime, 'requirement_module', return_value=module), \
             mock.patch.object(runtime, 'locked', side_effect=observed_lock), \
             concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            with original_lock(admission):
                future = executor.submit(runtime.execute, args)
                self.assertTrue(waiting.wait(timeout=3), 'legacy mutation skipped shared admission lock')
                self.assertFalse(future.done())
                self.assertEqual(ledger.set_mode('ACTIVE', command_id='while-claim-waits')['status'], 'COMMITTED')
            with self.assertRaisesRegex(ValueError, 'LEGACY_WRITER_DISABLED'):
                future.result(timeout=3)
        self.assertFalse(runtime.lease_path(self.root, 'A').exists())


if __name__ == '__main__':
    unittest.main()
