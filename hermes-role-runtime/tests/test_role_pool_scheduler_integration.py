"""Actual installed requirement runtime + scheduler ledger + role observer.

Only isolated files and local child processes are used: no model, group delivery,
service submission, production configuration or task execution.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

RUNTIME_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = Path('/home/wang/.hermes/hermes-agent')
SCRIPT_ROOT = RUNTIME_ROOT
for directory in (RUNTIME_ROOT, RUNTIME_ROOT/'tests', CORE_ROOT, SCRIPT_ROOT):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import requirement_runtime as runtime
from cron import role_pool_scheduler as pools
import role_observer as observer
import role_runtime
from test_requirement_runtime import fixture


class ActualRolePoolIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root, self.path, self.pool, fake = fixture(self.temp.name)
        self.home = self.root/'hermes-home'
        (self.home/'plugins').mkdir(parents=True)
        (self.home/'plugins/role-workflow-runtime').symlink_to(RUNTIME_ROOT, target_is_directory=True)
        (self.home/'cron').mkdir()
        self.jobs = list(fake.jobs.values())
        runtime.save_json(self.home/'cron/jobs.json', {'jobs': self.jobs})
        with sqlite3.connect(self.home/'cron/executions.db') as con:
            con.execute('CREATE TABLE executions (id TEXT, job_id TEXT, pid INTEGER, '
                        'process_started_at TEXT, status TEXT)')
        environment = {key: '' for key in pools.SCOPE_ENV.values()}
        environment.update(HERMES_HOME=str(self.home), _HERMES_CRON_EXTERNAL_WORKER='')
        patch = mock.patch.dict(os.environ, environment)
        patch.start(); self.addCleanup(patch.stop)
        # Independent review ledgers are home-scoped; do not read host history.
        patch = mock.patch.object(Path, 'home', return_value=self.home/'user')
        patch.start(); self.addCleanup(patch.stop)
        patch = mock.patch('strong_review.Ledger.status', return_value={'authorization': 'MANUAL_ONLY'})
        patch.start(); self.addCleanup(patch.stop)

    def request(self, target='new'):
        return runtime.request(self.path, target, {
            'platform': 'feishu', 'chat_id': self.pool['roles']['COORDINATOR']['chat_id'],
            'user_id': 'isolated-test-user', 'message_id': 'select-'+target,
            'primary_text': '全部权限：切换需求 '+target,
            'authenticated': True, 'current_full': True, 'role': 'COORDINATOR',
        }, trusted_ingress=True)

    def execution(self, identifier, role='COORDINATOR', pid=None, kind='business'):
        pid = os.getpid() if pid is None else pid
        start = (Path('/proc')/str(pid)/'stat').read_text().rsplit(')', 1)[1].split()[19]
        job_id = self.pool['roles'][role]['poll_job_id' if kind == 'business' else 'report_job_id']
        with sqlite3.connect(self.home/'cron/executions.db') as con:
            con.execute('INSERT INTO executions VALUES (?,?,?,?,?)',
                        (identifier, job_id, pid, start, 'running'))
        return {'id': job_id, 'prompt': '只处理当前明确接收需求',
                'role_pool': {'path': str(self.path), 'role': role, 'kind': kind}}

    def prepare(self, job, identifier):
        os.environ['_HERMES_CRON_EXTERNAL_WORKER'] = identifier
        return pools.prepare_role_pool_run(job, execution_id=identifier,
                                          session_id='cron-isolated-'+identifier, home=self.home)

    def test_unclaimed_live_worker_defers_then_actual_preflight_applies_and_fences_old_scope(self):
        child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'], stdin=subprocess.PIPE)
        self.addCleanup(child.stdin.close)
        self.addCleanup(lambda: child.poll() is None and (child.kill(), child.wait()))
        self.execution('b'*32, role='A', pid=child.pid)
        old_binding = copy.deepcopy(runtime.read_json(self.root/'old/ROLE-BINDINGS.json'))
        before_jobs = (self.home/'cron/jobs.json').read_bytes()
        self.request()
        pending = observer.pool_poll(self.path, 'A', reconcile=False)
        self.assertFalse(pending['wakeAgent'])
        self.assertEqual(pending['reason'], 'role_pool_selection_pending')
        identifier = 'c'*32
        job = self.execution(identifier)
        with self.prepare(job, identifier) as scoped:
            self.assertIsNone(scoped)
        self.assertEqual(runtime.read_json(self.path)['generation'], 0)
        self.assertEqual(runtime.status(self.path)['pending'][0]['status'], 'WAITING_SAFE_POINT')
        child.stdin.close(); child.wait(timeout=5)
        with sqlite3.connect(self.home/'cron/executions.db') as con:
            con.execute("UPDATE executions SET status='completed' WHERE id=?", ('b'*32,))
        # Admission -> reconcile -> adapter.switch_guard reenters the actual
        # shared flock rather than deadlocking on a second independent inode.
        with self.prepare(job, identifier) as scoped:
            self.assertEqual(scoped['workdir'], str(self.root/'new'))
            self.assertEqual(scoped['_role_pool_scope']['generation'], 1)
            self.assertEqual(os.environ['HERMES_ROLE_PROJECT_ROOT'], str(self.root/'new'))
        self.assertEqual((self.home/'cron/jobs.json').read_bytes(), before_jobs)
        self.assertFalse((self.root/'new/inbox/control/runtime/leases/COORDINATOR.json').exists())
        self.assertEqual(runtime.status(self.path)['pending'], [])
        with mock.patch.dict(os.environ, {
                'HERMES_ROLE_POOL_PATH': str(self.path), 'HERMES_ROLE_POOL_GENERATION': '0',
                'HERMES_ROLE_NAME': 'A', 'HERMES_ROLE_PROJECT_ROOT': str(self.root/'old')}):
            with self.assertRaisesRegex(ValueError, 'STALE_OR_INACTIVE'):
                role_runtime.guard(old_binding, 'A')

    def test_readonly_report_neither_applies_nor_blocks_switch_and_keeps_historical_attribution(self):
        self.request()
        identifier = 'd'*32
        report = self.execution(identifier, role='A', kind='report')
        before_pool = self.path.read_bytes()
        with self.prepare(report, identifier) as scoped:
            self.assertEqual(scoped['workdir'], str(self.root/'old'))
            self.assertEqual(self.path.read_bytes(), before_pool)
        adapter = pools.CronRolePoolAdapter(self.path, home=self.home)
        self.assertEqual(runtime.reconcile(self.path, adapter, apply=True)['state'], 'APPLIED')
        frozen = pools.scope_for_session('cron-isolated-'+identifier, self.home)
        self.assertEqual(frozen['requirement_id'], 'old')
        self.assertEqual(frozen['project_root'], str(self.root/'old'))

    def test_prepared_recovery_with_newer_selection_skips_intermediate_business_scope(self):
        self.request()
        identifier = 'f'*32
        job = self.execution(identifier)
        os.environ['_HERMES_CRON_EXTERNAL_WORKER'] = identifier
        adapter = pools.CronRolePoolAdapter(self.path, safe_execution_id=identifier, home=self.home)
        original = runtime.save_json

        def fail_after_prepared(path, value):
            original(path, value)
            if Path(path) == runtime._journal_path(self.path):
                raise OSError('isolated interruption after PREPARED')

        with mock.patch.object(runtime, 'save_json', side_effect=fail_after_prepared):
            with self.assertRaises(OSError):
                runtime.reconcile(self.path, adapter, apply=True)
        self.request('navigation')
        with self.prepare(job, identifier) as scoped:
            self.assertIsNone(scoped)
        self.assertEqual(runtime.read_json(self.path)['current']['requirement_id'], 'new')
        self.assertFalse((self.home/'runtime/role-pool-executions'/(identifier+'.json')).exists())
        with self.prepare(job, identifier) as scoped:
            self.assertEqual(scoped['_role_pool_scope']['requirement_id'], 'navigation')
            self.assertEqual(scoped['_role_pool_scope']['requirement_root'], str(self.root/'navigation'))
            self.assertEqual(scoped['workdir'], str(self.root/'legacy'))
            self.assertEqual(os.environ['HERMES_ROLE_REQUIREMENT_ROOT'], str(self.root/'navigation'))

    def test_same_runtime_root_selection_wakes_once_and_exact_ack_converges(self):
        pool = runtime.read_json(self.path)
        pool['requirements']['navigation']['runtime_root'] = str(self.root/'old')
        runtime.save_json(self.path, pool)
        (self.root/'old/TODO.md').write_text('| TASK-01 | ✅ | old work | AC | none | DONE | 1号 |\n')
        (self.root/'navigation/TODO.md').write_text('本包任务编号定位：`TASK-02`\n')
        first = observer.observe(self.root/'old', 'COORDINATOR')
        self.assertIn('fingerprint', first, first)
        observer.acknowledge(self.root/'old', 'COORDINATOR', first['fingerprint'])
        self.assertFalse(observer.observe(self.root/'old', 'COORDINATOR')['wakeAgent'])
        # A valid, still-runnable legacy continuation remains intact in the
        # shared root but must not repeatedly wake a different selected package.
        import context_input
        frozen = self.root/'old/old-input.md'
        frozen.write_text('TASK-01 原需求未完成材料\n')
        row = {'schema_version': 1, 'role': 'COORDINATOR', 'status': 'PENDING',
               'continuation_id': 'COORDINATOR:old-task', 'task': 'TASK-01', 'attempt': 'attempt-1',
               'checkpoint': 'old-input.md', 'next': '核对原需求证据', 'runnable': True,
               'checkpoint_sha256': hashlib.sha256(frozen.read_bytes()).hexdigest()}
        row['input_sha256'] = context_input._fingerprint(row)
        continuation = self.root/'old/inbox/control/runtime/continuations/COORDINATOR'/(
            hashlib.sha256(row['continuation_id'].encode()).hexdigest()+'.json')
        runtime.save_json(continuation, row)
        original = continuation.read_bytes()
        self.request('navigation')
        identifier = '9'*32
        job = self.execution(identifier)
        with self.prepare(job, identifier):
            packet = observer.pool_poll(self.path, 'COORDINATOR')
            self.assertTrue(packet['wakeAgent'], packet)
            self.assertNotEqual(packet['fingerprint'], first['fingerprint'])
            snapshot = runtime.read_json(Path(packet['snapshot']))
            self.assertEqual(snapshot['tasks'], [])
            self.assertEqual(snapshot['continuations'], [])
            observer.acknowledge(self.root/'old', 'COORDINATOR', packet['fingerprint'])
            self.assertFalse(observer.pool_poll(self.path, 'COORDINATOR')['wakeAgent'])
        self.assertEqual(continuation.read_bytes(), original)

    def test_readme_only_selection_wakes_coordinator_without_tasks_or_worker_acks(self):
        (self.root/'new/TODO.md').unlink()
        self.request()
        identifier = 'e'*32
        coordinator = self.execution(identifier)
        with self.prepare(coordinator, identifier) as scoped:
            self.assertEqual(scoped['workdir'], str(self.root/'new'))
            packet = observer.pool_poll(self.path, 'COORDINATOR')
            self.assertTrue(packet['wakeAgent'], packet)
            self.assertIn('先依据需求README', packet['instruction'])
            snapshot = runtime.read_json(Path(packet['snapshot']))
            self.assertIsNone(snapshot['sources']['TODO.md'])
            self.assertEqual(snapshot['tasks'], [])
            self.assertEqual(snapshot['dispatches'], {})
        for index, role in enumerate(('A', 'B', 'C', 'TESTER')):
            identifier = str(index+1)*32
            job = self.execution(identifier, role=role)
            with self.prepare(job, identifier):
                packet = observer.pool_poll(self.path, role)
                self.assertFalse(packet['wakeAgent'], packet)
                self.assertEqual(packet['reason'], 'role_pool_waiting_requirement_preparation')
                state = runtime.read_json(self.root/f'new/inbox/control/runtime/observer/{role}.json')
                self.assertTrue(state.get('pending'))
                self.assertNotIn('acknowledged', state)
        self.assertFalse((self.root/'new/TODO.md').exists())
        self.assertFalse((self.root/'new/inbox/control/runtime/leases').exists())
        dispatch = self.root/'new/inbox/1号/TASK-PREP/attempt-1-dispatch.md'
        dispatch.parent.mkdir(parents=True)
        dispatch.write_text('# 派单 TASK-PREP\n- status: PENDING\n')
        with mock.patch.dict(os.environ, {'HERMES_ROLE_POOL_PATH': ''}):
            packet = observer.pool_poll(self.path, 'A', reconcile=False)
        self.assertTrue(packet['wakeAgent'], packet)


if __name__ == '__main__':
    unittest.main()
