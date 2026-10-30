"""Isolated P0 observation/renewal tests with real PID identities and workers.

Only temporary homes, native ledgers and project roots are written. No model,
production cron, group transport, OCR or strong-review operation is invoked.
"""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch
from zoneinfo import ZoneInfo

import role_observer as observer
import role_runtime as runtime

REPO = Path(runtime.__file__).resolve().parent


class PoolFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)/'home'
        self.home.mkdir()
        self.pool_path = self.home/'role-pool.json'
        self.root = self.home/'requirements/current'
        self.root.mkdir(parents=True)
        self.roles = {role: {'skill': 'test', 'chat_id': 'chat-'+role,
            'poll_job_id': role+'-job', 'session_file': 'sessions/'+role+'.md'} for role in runtime.ROLES}
        self.pool = {'schema_version': 1, 'pool_id': 'test', 'generation': 2, 'control': 'ACTIVE',
            'catalog_path': str(self.home/'requirements/README.md'), 'registry_path': str(self.home/'registry.json'),
            'roles': self.roles, 'current': {'requirement_id': 'current', 'runtime_root': str(self.root),
                'role_roots': {role: str(self.root) for role in runtime.ROLES}}}
        runtime.save(self.pool_path, self.pool)
        (self.home/'requirements/README.md').write_text('| [current](current/README.md) | test |\n')
        (self.root/'README.md').write_text('# Current\n')
        self.bind(self.root, 2)
        (self.home/'cron').mkdir()
        with sqlite3.connect(self.home/'cron/executions.db') as db:
            db.execute('CREATE TABLE executions (id TEXT PRIMARY KEY,job_id TEXT,pid INTEGER,process_started_at INTEGER,status TEXT)')
        self.state = self.home/'cache/test-monitor'

    def bind(self, root, generation):
        runtime.save(root/'ROLE-BINDINGS.json', {'schema_version': 1, 'project_root': str(root),
            'control': 'ACTIVE', 'roles': self.roles,
            'role_pool': {'path': str(self.pool_path), 'generation': generation}})

    def worker(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: child.poll() is None and (child.terminate(), child.wait()))
        return child

    def lease(self, child, *, root=None, role='A', expired=True, **extra):
        root = root or self.root
        value = {'owner': 'worker', 'token': 'kept-secret', 'pid': child.pid,
            'process_start': runtime.pid_identity(child.pid), 'expires_at': time.time()+(-10 if expired else 300), **extra}
        runtime.save(runtime.lease_path(root, role), value)
        runtime.save(root/f'inbox/control/runtime/checkpoints/{role}.json', {
            'schema_version': 1, 'role': role, 'task': 'TASK-01', 'attempt': 'attempt-1', 'status': 'RUNNING'})
        return value

    def native(self, child, *, root=None, generation=2, execution='a'*32):
        root = root or self.root
        scope = {'schema_version': 1, 'pool_path': str(self.pool_path), 'role': 'A',
            'project_root': str(root), 'generation': generation, 'execution_id': execution,
            'job_id': 'A-job', 'session_id': 'test-session', 'kind': 'business'}
        runtime.save(self.home/'runtime/role-pool-executions'/(execution+'.json'), scope)
        with sqlite3.connect(self.home/'cron/executions.db') as db:
            db.execute('INSERT INTO executions VALUES (?,?,?,?,?)',
                       (execution, 'A-job', child.pid, int(runtime.pid_identity(child.pid)), 'running'))
        return scope

    def observe(self, instant=None):
        return observer.observe_pool_leases(self.pool_path, home=self.home, state_dir=self.state,
                                            clock=lambda: instant if instant is not None else time.time())


class LeaseMonitorTests(PoolFixture):
    def test_monitor_sender_uses_existing_chat_transport_receipts_and_never_retries_unknown(self):
        child = self.worker(); lease = self.lease(child)
        day = datetime(2026, 10, 19, 10, tzinfo=ZoneInfo('Asia/Shanghai'))
        lease['expires_at'] = day.timestamp()-1
        runtime.save(runtime.lease_path(self.root, 'A'), lease)
        sent = []
        sender = observer.monitor_sender(self.pool_path,
            transport=lambda row: sent.append(row) or {'delivered': False, 'message_id': None})
        result = observer.observation_tick(self.home, self.pool_path, at=day, sender=sender)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]['chat_id'], 'chat-A')
        self.assertIn('运行观察', sent[0]['text'])
        self.assertEqual(result['deliveries'][0]['state'], 'UNKNOWN')
        observer.observation_tick(self.home, self.pool_path, at=day.timestamp()+60, sender=sender)
        self.assertEqual(len(sent), 1)
        # A pool-wide observer fault uses only the accepted coordinator mapping.
        receipt = observer.monitor_sender(self.pool_path,
            transport=lambda row: sent.append(row) or {'delivered': True, 'message_id': 'fake-confirmed'})(
                {'pool_path': str(self.pool_path), 'type': 'observer_fault', 'source': '/fixture', 'transition': 'first'})
        self.assertTrue(receipt['confirmed'])
        self.assertEqual(sent[-1]['chat_id'], 'chat-COORDINATOR')
        with self.assertRaisesRegex(ValueError, 'POOL_IDENTITY'):
            sender({'pool_path': '/other-pool'})

    def test_native_tick_coalesces_samples_and_sender_is_optional(self):
        child = self.worker(); lease = self.lease(child)
        day = datetime(2026, 10, 19, 10, tzinfo=ZoneInfo('Asia/Shanghai'))
        lease['expires_at'] = day.timestamp()-1
        runtime.save(runtime.lease_path(self.root, 'A'), lease)
        first = observer.observation_tick(self.home, self.pool_path, at=day)
        self.assertTrue(first['sampled'])
        self.assertNotIn('deliveries', first)
        path = next(Path(first['outbox']).glob('*.json'))
        self.assertEqual(runtime.load(path)['delivery_receipt']['state'], 'PENDING')
        with patch.object(observer, 'collect_pool_leases', side_effect=AssertionError('duplicate sampling')):
            result = observer.observation_tick(self.home, self.pool_path, at=day.timestamp()+59)
        self.assertFalse(result['sampled'])
        sent = []
        result = observer.observation_tick(self.home, self.pool_path, at=day.timestamp()+60,
            sender=lambda event: sent.append(event) or {'confirmed': True})
        self.assertTrue(result['sampled'])
        self.assertEqual(len(sent), 1)
        observer.observation_tick(self.home, self.pool_path, at=day.timestamp()+61,
            sender=lambda event: sent.append(event) or {'confirmed': True})
        self.assertEqual(len(sent), 1)

    def test_native_tick_quiet_hours_preserve_unsent_outbox(self):
        child = self.worker(); lease = self.lease(child)
        night = datetime(2026, 10, 19, 23, tzinfo=ZoneInfo('Asia/Shanghai'))
        lease['expires_at'] = night.timestamp()-1
        runtime.save(runtime.lease_path(self.root, 'A'), lease)
        sent = []
        result = observer.observation_tick(self.home, self.pool_path, at=night,
            sender=lambda event: sent.append(event) or {'confirmed': True})
        self.assertEqual(sent, [])
        self.assertTrue(result['quiet_hours'])
        self.assertEqual(result['deliveries'][0]['state'], 'QUIET_DEFERRED')

    def test_real_root_expired_live_owner_dedup_escalation_recovery(self):
        child = self.worker()
        lease = self.lease(child)
        self.native(child)
        original = runtime.lease_path(self.root, 'A').read_bytes()
        first = self.observe(lease['expires_at']+1)
        event = next(event for event in first['transitions'] if event['type'] == 'lease_expired_owner_alive')
        self.assertEqual(event['current_work']['task'], 'TASK-01')
        self.assertEqual(event['transition'], 'first')
        self.assertEqual(first['roles']['A'][0]['project_root'], str(self.root))
        self.assertEqual(self.observe(lease['expires_at']+2)['transitions'], [])
        self.assertEqual(self.observe(lease['expires_at']+901)['transitions'][0]['transition'], 'escalated')
        self.assertEqual(self.observe(lease['expires_at']+902)['transitions'], [])
        self.assertEqual(runtime.lease_path(self.root, 'A').read_bytes(), original)
        lease['expires_at'] += 5000
        runtime.save(runtime.lease_path(self.root, 'A'), lease)
        recovered = self.observe(lease['expires_at']-1000)
        self.assertEqual(recovered['transitions'][0]['transition'], 'recovered')
        self.assertEqual(self.observe(lease['expires_at']-999)['transitions'], [])
        self.assertNotIn('kept-secret', json.dumps(first))

    def test_old_frozen_live_root_remains_observed_after_current_selection(self):
        child = self.worker()
        old = self.home/'requirements/old'
        old.mkdir()
        self.bind(old, 1)
        self.lease(child, root=old)
        self.native(child, root=old, generation=1)
        result = self.observe()
        values = {value['project_root']: value for value in result['roles']['A']}
        self.assertEqual(values[str(old)]['state'], 'expired_owner_alive')
        self.assertEqual(values[str(self.root)]['state'], 'no_lease')
        child.terminate(); child.wait()
        result = self.observe()
        # The fixture has no terminal native row/frozen cgroup proof. An exited
        # parent alone cannot certify that all legacy descendants have drained.
        self.assertIn(str(old), result['sources'])
        self.assertTrue(any(f['type']=='observer_frozen_execution_unverified' for f in result['faults']))

    def test_bad_generation_unbound_and_corrupt_authority_are_not_no_lease(self):
        binding = runtime.load(self.root/'ROLE-BINDINGS.json')
        binding['role_pool']['generation'] = 1
        runtime.save(self.root/'ROLE-BINDINGS.json', binding)
        result = self.observe()
        self.assertTrue(result['faults'])
        self.assertTrue(all(value['state'] == 'unverified' for values in result['roles'].values() for value in values))
        self.bind(self.root, 2)
        binding = runtime.load(self.root/'ROLE-BINDINGS.json')
        del binding['roles']['A']
        runtime.save(self.root/'ROLE-BINDINGS.json', binding)
        self.assertEqual(self.observe()['roles']['A'][0]['state'], 'unverified')
        (self.root/'ROLE-BINDINGS.json').write_text('{bad json')
        self.assertEqual(self.observe()['health'], 'unverified')
        self.pool_path.write_text('[]')
        result = self.observe()
        self.assertEqual(result['faults'][0]['type'], 'observer_pool_unverified')
        self.assertTrue(all(not values for values in result['roles'].values()))

    def test_native_source_blindness_blocks_false_business_recovery(self):
        child = self.worker(); self.lease(child); self.native(child)
        first = self.observe()
        (self.home/'cron/executions.db').write_text('unreadable sqlite')
        second = self.observe()
        self.assertTrue(any(row['type'] == 'observer_executions_unverified' for row in second['faults']))
        self.assertFalse(any(row['type'] == 'lease_expired_owner_alive' and row['transition'] == 'recovered'
                             for row in second['transitions']))

    def test_unbound_lease_and_missing_active_scope_are_explicit_faults(self):
        child = self.worker(); self.native(child)
        runtime.save(runtime.lease_path(self.root, 'A'), {'owner': 'legacy', 'token': 'hidden', 'expires_at': 0})
        (self.home/'runtime/role-pool-executions'/('a'*32+'.json')).unlink()
        result = self.observe()
        kinds = {fault['type'] for fault in result['faults']}
        self.assertIn('observer_lease_owner_unverified', kinds)
        self.assertIn('observer_active_execution_scope_missing', kinds)

    def test_corrupt_monitor_cursor_is_preserved_and_reports_its_own_fault(self):
        self.observe()
        damaged = b'\xff{bad json'
        (self.state/'state.json').write_bytes(damaged)
        result = self.observe()
        self.assertTrue(any(row['type'] == 'observer_cursor_unverified' for row in result['faults']))
        self.assertEqual(next((self.state/'recovery').glob('*.raw')).read_bytes(), damaged)

    def test_gateway_manual_tuple_is_distinct_from_invalid_cron_binding(self):
        child = self.worker()
        owner = 'manual:'+hashlib.sha256(b'session\0turn').hexdigest()
        self.lease(child, owner=owner, manual_session_id='session', manual_turn_id='turn')
        with patch.object(runtime, 'gateway_pid', return_value=child.pid):
            result = self.observe()
            self.assertFalse(any(row['type'] == 'observer_invalid_cron_gateway_binding' for row in result['faults']))
            self.assertEqual(result['roles']['A'][0]['binding_type'], 'manual_native_identity_recorded')
            self.lease(child, owner=owner)  # Prefix alone grants nothing.
            result = self.observe()
            self.assertTrue(any(row['type'] == 'observer_invalid_cron_gateway_binding' for row in result['faults']))

    def test_generation_and_delivery_receipts_quiet_unknown_and_no_duplicate_send(self):
        child = self.worker(); lease = self.lease(child)
        night = datetime(2026, 10, 19, 22, tzinfo=ZoneInfo('Asia/Shanghai'))
        lease['expires_at'] = night.timestamp()-1
        runtime.save(runtime.lease_path(self.root, 'A'), lease)
        result = self.observe(night.timestamp())
        outbox = Path(result['outbox'])
        generated = runtime.load(next(outbox.glob('*.json')))
        self.assertEqual(generated['generation_receipt']['state'], 'GENERATED')
        self.assertEqual(generated['delivery_receipt']['state'], 'QUIET_DEFERRED')
        sent = []
        observer.drain_monitor_notifications(outbox, lambda row: sent.append(row), at=night)
        self.assertEqual(sent, [])
        day = night.replace(hour=10)
        observer.drain_monitor_notifications(outbox, lambda row: sent.append(row) or {'confirmed': True}, at=day)
        observer.drain_monitor_notifications(outbox, lambda row: sent.append(row) or {'confirmed': True}, at=day)
        self.assertEqual(len(sent), 1)
        lease['expires_at'] -= 4000
        runtime.save(runtime.lease_path(self.root, 'A'), lease)
        self.observe(night.timestamp()+1)
        observer.drain_monitor_notifications(outbox, lambda row: {'confirmed': False}, at=day)
        observer.drain_monitor_notifications(outbox, lambda row: sent.append(row), at=day)
        self.assertEqual(len(sent), 1)

    def test_script_runs_in_isolated_home_and_stable_stdout_does_not_age(self):
        child = self.worker(); self.lease(child); self.native(child)
        command = [sys.executable, str(REPO/'lease_observe.py'), '--home', str(self.home)]
        env = dict(os.environ, PYTHONPATH=str(REPO)+os.pathsep+str(Path(observer.__file__).parent))
        first = subprocess.run(command, env=env, text=True, capture_output=True, check=True)
        second = subprocess.run(command, env=env, text=True, capture_output=True, check=True)
        self.assertEqual(first.stdout, second.stdout)
        self.assertIn(str(self.root), first.stdout)
        self.assertNotIn('rosbag-catalog-integration', first.stdout)


class WorkerHeartbeatTests(PoolFixture):
    def start_native_worker(self):
        package = self.home/'stub/cron'
        package.mkdir(parents=True)
        (package/'__init__.py').write_text('')
        # Run as a real python -m cron.scheduler process; all permission checks
        # and the production requirement runtime remain real.
        (package/'scheduler.py').write_text('''import json, os, sqlite3, sys, time
from pathlib import Path
import role_runtime as r
home=Path(sys.argv[1]); root=Path(sys.argv[2]); execution='b'*32
os.environ['HERMES_HOME']=str(home)
os.environ['_HERMES_CRON_EXTERNAL_WORKER']=execution
scope={'schema_version':1,'pool_path':str(home/'role-pool.json'),'role':'A',
 'project_root':str(root),'generation':2,'execution_id':execution,'job_id':'A-job',
 'session_id':'native-test','kind':'business'}
r.save(home/'runtime/role-pool-executions'/(execution+'.json'),scope)
with sqlite3.connect(home/'cron/executions.db') as db:
 db.execute('INSERT INTO executions VALUES (?,?,?,?,?)',(execution,'A-job',os.getpid(),int(r.pid_identity(os.getpid())),'running'))
lease={'owner':'native-worker','pid':os.getpid(),'process_start':r.pid_identity(os.getpid()),'token':'original','expires_at':0}
r.save(r.lease_path(root,'A'),lease)
heartbeat=r.LegacyLeaseHeartbeat(scope,home=home,interval=.05,ttl=30)
print(json.dumps({'ready':True}),flush=True)
for line in sys.stdin:
 command=line.strip()
 if command=='once': result=heartbeat.heartbeat_once()
 elif command=='delete': r.lease_path(root,'A').unlink(); result={'deleted':True}
 elif command=='replace':
  lease=r.load(r.lease_path(root,'A'));lease['token']='other';r.save(r.lease_path(root,'A'),lease);result={'replaced':True}
 elif command=='pause':
  binding=r.load(root/'ROLE-BINDINGS.json');binding['control']='PAUSED';r.save(root/'ROLE-BINDINGS.json',binding);result={'paused':True}
 elif command=='wait':
  with heartbeat: time.sleep(.2)
  result=heartbeat.result
 else: break
 print(json.dumps(result),flush=True)
''')
        plugin = self.home/'plugins'
        plugin.mkdir()
        (plugin/'role-workflow-runtime').symlink_to(REPO, target_is_directory=True)
        env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(package.parent), str(Path(runtime.__file__).parent), str(REPO)]))
        child = subprocess.Popen([sys.executable, '-m', 'cron.scheduler', str(self.home), str(self.root)],
            env=env, text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        def clean():
            if child.poll() is None:
                child.terminate(); child.wait(timeout=5)
            child.stdin.close(); child.stdout.close(); child.stderr.close()
        self.addCleanup(clean)
        ready = child.stdout.readline()
        self.assertTrue(ready, child.stderr.read() if child.poll() is not None else 'worker did not start')
        self.assertTrue(json.loads(ready)['ready'])
        return child

    def ask(self, child, command):
        child.stdin.write(command+'\n'); child.stdin.flush()
        value = child.stdout.readline()
        self.assertTrue(value, child.stderr.read() if child.poll() is not None else 'worker did not reply')
        return json.loads(value)

    def test_own_frozen_worker_renews_expired_token_and_records_source(self):
        child = self.start_native_worker()
        result = self.ask(child, 'once')
        self.assertTrue(result['renewed'], result)
        lease = runtime.load(runtime.lease_path(self.root, 'A'))
        self.assertEqual(lease['token'], 'original')
        self.assertEqual(lease['pid'], child.pid)
        self.assertEqual(lease['renewal_source']['execution_id'], 'b'*32)

    def test_removed_lease_is_never_recreated(self):
        child = self.start_native_worker()
        self.assertTrue(self.ask(child, 'once')['renewed'])
        self.ask(child, 'delete')
        self.assertEqual(self.ask(child, 'once')['state'], 'stopped_unverified')
        self.assertFalse(runtime.lease_path(self.root, 'A').exists())

    def test_replaced_token_and_control_stop_old_heartbeat(self):
        child = self.start_native_worker()
        self.assertTrue(self.ask(child, 'once')['renewed'])
        self.ask(child, 'replace')
        self.assertFalse(self.ask(child, 'once')['renewed'])
        self.assertEqual(runtime.load(runtime.lease_path(self.root, 'A'))['token'], 'other')

    def test_pause_stops_renewal(self):
        child = self.start_native_worker()
        self.ask(child, 'pause')
        self.assertFalse(self.ask(child, 'once')['renewed'])
        self.assertEqual(runtime.load(runtime.lease_path(self.root, 'A'))['expires_at'], 0)

    def test_generation_or_owner_change_stops_renewal_without_borrowing_scope(self):
        child = self.start_native_worker()
        self.assertTrue(self.ask(child, 'once')['renewed'])
        path = runtime.lease_path(self.root, 'A')
        lease = runtime.load(path); lease['owner'] = 'different-owner'
        runtime.save(path, lease)
        before = path.read_bytes()
        self.assertFalse(self.ask(child, 'once')['renewed'])
        self.assertEqual(path.read_bytes(), before)

    def test_changed_pool_generation_is_not_renewed(self):
        child = self.start_native_worker()
        self.assertTrue(self.ask(child, 'once')['renewed'])
        self.pool['generation'] = 3
        runtime.save(self.pool_path, self.pool)
        self.bind(self.root, 3)
        path = runtime.lease_path(self.root, 'A'); before = path.read_bytes()
        self.assertFalse(self.ask(child, 'once')['renewed'])
        self.assertEqual(path.read_bytes(), before)

    def test_non_worker_cannot_renew_a_foreign_live_pid(self):
        child = self.worker(); self.lease(child)
        scope = self.native(child)
        before = runtime.lease_path(self.root, 'A').read_bytes()
        heartbeat = runtime.LegacyLeaseHeartbeat(scope, home=self.home)
        self.assertFalse(heartbeat.heartbeat_once()['renewed'])
        self.assertEqual(runtime.lease_path(self.root, 'A').read_bytes(), before)

    def test_lock_contention_is_bounded_persisted_and_retries_same_worker(self):
        import fcntl
        child=self.start_native_worker()
        path=self.pool_path.with_suffix('.admission.lock')
        with path.open('a+') as held:
            fcntl.flock(held,fcntl.LOCK_EX)
            result=self.ask(child,'once')
            self.assertEqual(result['state'],'deferred_lock_busy')
            receipt=runtime.load(self.home/'runtime/legacy-heartbeats'/('b'*32+'.json'))
            self.assertEqual(receipt['reason'],'LEGACY_HEARTBEAT_LOCK_BUSY')
            self.assertFalse(receipt['stopped'])
            self.assertNotIn('token',receipt)
        self.assertTrue(self.ask(child,'once')['renewed'])
        self.ask(child,'replace')
        self.assertEqual(self.ask(child,'once')['state'],'stopped_unverified')
        receipt=runtime.load(self.home/'runtime/legacy-heartbeats'/('b'*32+'.json'))
        self.assertEqual(receipt['reason'],'LEGACY_HEARTBEAT_LEASE_OWNER_CHANGED')
        self.assertTrue(receipt['last_renewed_at'])

    def test_thread_maintains_lease_during_blocking_tool_or_model_wait(self):
        child = self.start_native_worker()
        result = self.ask(child, 'wait')
        self.assertTrue(result['renewed'], result)
        lease = runtime.load(runtime.lease_path(self.root, 'A'))
        self.assertGreater(lease['last_seen'], time.time()-2)


class Schema2ObservationTests(PoolFixture):
    def setUp(self):
        super().setUp()
        from business_ledger import BusinessLedger
        self.repo = self.home/'repo'; self.repo.mkdir()
        self.git('init', '-q'); self.git('config', 'user.name', 'Observer Fixture')
        self.git('config', 'user.email', 'observer@example.invalid')
        (self.repo/'app.py').write_text('value = 1\n')
        self.git('add', '.'); self.git('commit', '-qm', 'base')
        self.base = self.git('rev-parse', 'HEAD')
        self.ledger = BusinessLedger(self.pool_path.with_suffix('.runtime')/'business.sqlite3')
        self.commit(self.ledger.create_run('current', self.root, self.repo, ['T1'], run_id='run-one', command_id='run'))
        self.commit(self.ledger.publish_plan('run-one', 1, [{'task_id': 'T1', 'role': 'A',
            'dependencies': [], 'scope': ['app.py'], 'base': self.base, 'ac_version': 'frozen',
            'required_checks': ['independent_test']}], command_id='plan'))
        self.commit(self.ledger.select_run('run-one', command_id='select'))
        self.commit(self.ledger.set_mode('ACTIVE', command_id='activate'))

    def commit(self, value):
        self.assertEqual(value['status'], 'COMMITTED', value)
        return value

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True,
                                       stderr=subprocess.DEVNULL).strip()

    def bound_worker(self, role='A', program=None):
        child = subprocess.Popen([sys.executable, '-c', program or 'import time; time.sleep(60)'],
            start_new_session=True, stdout=subprocess.PIPE, text=True)
        def cleanup():
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait(timeout=5)
            child.stdout.close()
        self.addCleanup(cleanup)
        scope = {'execution_id': uuid.uuid4().hex, 'generation': self.ledger.mode()['generation'],
            'pid': child.pid, 'process_start': runtime.pid_identity(child.pid),
            'scope': Path('/proc', str(child.pid), 'cgroup').read_text()}
        identity = self.commit(self.ledger.bind_execution(role, scope, command_id='bind-'+scope['execution_id']))['identity']
        return child, identity

    def test_schema2_reads_current_work_and_live_writer_without_legacy_lease(self):
        child, identity = self.bound_worker()
        before = self.ledger.snapshot()
        result = self.observe()
        self.assertEqual(result['observation_schema'], 2)
        self.assertEqual(result['health'], 'unverified')
        self.assertFalse(result['faults'], result['faults'])
        self.assertTrue(any(row['type'] == 'ACTIVITY_SOURCE_UNVERIFIABLE' for row in result['anomalies']))
        self.assertFalse(any(row['type'] == 'NO_ACTIVITY' for row in result['anomalies']))
        self.assertTrue(result['real_execution_observed'])
        role = result['roles']['A'][0]
        self.assertEqual(role['current_work']['work_id'], identity['work_id'])
        self.assertEqual(role['current_work']['task_id'], 'T1')
        self.assertEqual(role['writer_execution_id'], identity['execution_id'])
        self.assertEqual(role['executions'][0]['owner_process_state'], 'alive')
        self.assertEqual(self.ledger.snapshot(), before)
        self.assertFalse(runtime.lease_path(self.root, 'A').exists())
        self.assertEqual(set(result['roles']), set(runtime.ROLES))

    def test_actual_checkpoint_and_exact_model_execution_are_separate_from_tool_coverage(self):
        from workflow_activity import start_model_coverage, record_model_start, record_model_end
        _, identity = self.bound_worker()
        self.commit(self.ledger.checkpoint(identity, {'summary': 'checked'}, command_id='checkpoint-activity'))
        scope = {**identity, 'pool_path': str(self.pool_path), 'session_id': 'exact-native-session'}
        runtime.save(self.home/'runtime/role-pool-executions'/(identity['execution_id']+'.json'), scope)
        now = time.time()
        start_model_coverage(self.home, scope, at=now-5)
        record_model_start(self.home, scope, 'physical-one', 'request-one', at=now-2)
        record_model_end(self.home, scope, 'physical-one', 'request-one', at=now-1)
        unrelated = {**scope, 'execution_id': 'another-execution'}
        start_model_coverage(self.home, unrelated, at=now-5)
        record_model_start(self.home, unrelated, 'physical-other', 'must-not-merge', at=now)
        before = self.ledger.snapshot()
        result = self.observe(now)
        activity = next(value for value in result['activity'].values() if value['identity']['role'] == 'A')
        self.assertIsNotNone(activity['checkpoint_age'])
        checkpoint_at = max(row['created_at'] for row in before['events'] if row['kind'] == 'CHECKPOINT_SAVED')
        self.assertEqual(activity['last_activity_at'], max(checkpoint_at, now-1))
        self.assertTrue(next(source for source in activity['source_coverage'] if source['source_id'] == 'model_requests')['valid'])
        self.assertIn('POLICY_UNCONFIGURED', activity['reason_codes'])
        self.assertEqual(activity['state'], 'UNVERIFIABLE')
        tools = next(source for source in activity['source_coverage'] if source['source_id'] == 'tools')
        self.assertFalse(tools['complete'])
        self.assertEqual(self.ledger.snapshot(), before)

    def test_notification_transport_runs_after_tick_lock_is_released(self):
        import fcntl
        worker, _ = self.bound_worker()
        worker.terminate(); worker.wait(timeout=5)
        day = datetime(2026, 10, 19, 10, tzinfo=ZoneInfo('Asia/Shanghai'))
        lock = self.home/'cache/role-lease-monitor'/observer.digest(str(self.pool_path.resolve()))/'tick.lock'
        sent = []
        def send(event):
            with lock.open('a') as handle:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(handle, fcntl.LOCK_UN)
            sent.append(event)
            return {'confirmed': True}
        observer.observation_tick(self.home, self.pool_path, at=day, sender=send)
        self.assertTrue(sent)

    def test_real_lifecycle_collector_drives_no_activity_and_recovery_through_monitor(self):
        from workflow_activity import (start_coverage, record_tool_start, record_tool_end,
            start_model_coverage, record_model_start, record_model_end)
        _, identity = self.bound_worker()
        scope = {**identity, 'pool_path': str(self.pool_path), 'session_id': 'exact-session'}
        runtime.save(self.home/'runtime/role-pool-executions'/(identity['execution_id']+'.json'), scope)
        runtime.save(self.pool_path.with_suffix('.runtime')/'observation-policy.json',
                     {'schema_version': 1, 'no_activity_seconds': 60})
        now = time.time()
        start_coverage(self.home, scope, at=now-180)
        start_model_coverage(self.home, scope, at=now-180)
        record_tool_start(self.home, scope, 'read-one', 'read_file', at=now-90)
        record_tool_end(self.home, scope, 'read-one', at=now-80, outcome='SUCCESS')
        record_model_start(self.home, scope, 'physical-main', 'request', at=now-70)
        record_model_end(self.home, scope, 'physical-main', 'request', at=now-60)
        before = self.ledger.snapshot()
        first = self.observe(now+10)
        self.assertTrue(any(row['type'] == 'NO_ACTIVITY' and row['transition'] == 'first' for row in first['transitions']), first)
        record_tool_start(self.home, scope, 'read-two', 'read_file', at=now+20)
        record_tool_end(self.home, scope, 'read-two', at=now+21, outcome='SUCCESS')
        recovered = self.observe(now+22)
        self.assertTrue(any(row['type'] == 'NO_ACTIVITY' and row['transition'] == 'recovered' for row in recovered['transitions']), recovered)
        self.assertEqual(self.ledger.snapshot(), before)

    def test_open_compaction_and_persisted_request_gap_cannot_certify_inactivity(self):
        from workflow_activity import (start_coverage, record_tool_start, record_tool_end,
            start_model_coverage, record_model_start, record_model_end)
        _, identity = self.bound_worker()
        scope = {**identity, 'pool_path': str(self.pool_path), 'session_id': 'exact-session'}
        scope_file = self.home/'runtime/role-pool-executions'/(identity['execution_id']+'.json')
        runtime.save(scope_file, scope)
        runtime.save(self.pool_path.with_suffix('.runtime')/'observation-policy.json',
                     {'schema_version': 1, 'no_activity_seconds': 60})
        now = time.time()
        start_coverage(self.home, scope, at=now-180)
        start_model_coverage(self.home, scope, at=now-180)
        record_tool_start(self.home, scope, 'read-one', 'read_file', at=now-90)
        record_tool_end(self.home, scope, 'read-one', at=now-80, outcome='SUCCESS')
        record_model_start(self.home, scope, 'compact-physical', 'compact-logical',
                           at=now-70, kind='compaction')
        before = self.ledger.snapshot()
        open_wait = self.observe(now+10)
        self.assertFalse(any(row['type'] == 'NO_ACTIVITY' for row in open_wait['anomalies']))
        self.assertTrue(any(row['type'] == 'ACTIVITY_SOURCE_UNVERIFIABLE' for row in open_wait['anomalies']))
        record_model_end(self.home, scope, 'compact-physical', 'compact-logical', at=now+20)
        ended = self.observe(now+22)
        self.assertFalse(any(row['type'] in {'NO_ACTIVITY', 'ACTIVITY_SOURCE_UNVERIFIABLE'}
                             for row in ended['anomalies']), ended)
        scope_file.unlink()
        missing_binding = self.observe(now+100)
        self.assertFalse(any(row['type'] == 'NO_ACTIVITY' for row in missing_binding['anomalies']))
        self.assertTrue(any(row['type'] == 'ACTIVITY_SOURCE_UNVERIFIABLE' for row in missing_binding['anomalies']))
        runtime.save(scope_file, scope)
        # A later valid request does not erase a missed physical-attempt ID.
        record_model_start(self.home, scope, None, 'missing-attempt', at=now+23)
        record_model_start(self.home, scope, 'valid-after-gap', 'request-after-gap', at=now+24)
        record_model_end(self.home, scope, 'valid-after-gap', 'request-after-gap', at=now+25)
        gap = self.observe(now+101)
        self.assertFalse(any(row['type'] == 'NO_ACTIVITY' for row in gap['anomalies']))
        self.assertTrue(any(row['type'] == 'ACTIVITY_SOURCE_UNVERIFIABLE' for row in gap['anomalies']))
        self.assertEqual(self.ledger.snapshot(), before)

    def test_schema2_dead_parent_and_living_descendant_never_certifies_writer_exit(self):
        child, identity = self.bound_worker(program='import subprocess,sys,time; c=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"]); print(c.pid,flush=True); time.sleep(60)')
        descendant = int(child.stdout.readline())
        child.terminate(); child.wait(timeout=5)
        result = self.observe()
        writer = result['roles']['A'][0]['executions'][0]
        self.assertEqual(writer['owner_process_state'], 'exited_or_reused')
        self.assertIn(descendant, writer['descendant_pids'])
        self.assertTrue(writer['writers_present'])
        self.assertFalse(any(row['type'] == 'observer_business_writer_exited' for row in result['faults']))
        self.assertEqual(self.ledger.snapshot()['executions'][0]['state'], 'ACTIVE')

    def test_schema2_separate_registered_tool_group_remains_writer_after_owner_exit(self):
        child, identity = self.bound_worker()
        tool = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
        self.addCleanup(lambda: tool.poll() is None and (tool.terminate(), tool.wait(timeout=5)))
        self.commit(self.ledger.register_tool_execution(identity, {'tool_id': 'separate-tool',
            'pid': tool.pid, 'process_start': runtime.pid_identity(tool.pid),
            'cgroup': Path('/proc', str(tool.pid), 'cgroup').read_text()}, command_id='registered-tool'))
        child.terminate(); child.wait(timeout=5)
        result = self.observe()
        writer = result['roles']['A'][0]['executions'][0]
        self.assertTrue(writer['writers_present'])
        self.assertIn(tool.pid, writer['descendant_pids'])
        self.assertTrue(writer['tools'][0]['identity_verified'])
        self.assertFalse(any(row['type'] == 'observer_business_writer_exited' for row in result['faults']))

    def ended_write_incident(self, *, count=1, tool=False):
        import workflow_diagnostics as diagnostics
        child, identity = self.bound_worker()
        if tool:
            worker = subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],start_new_session=True)
            self.addCleanup(lambda: worker.poll() is None and (worker.terminate(),worker.wait(timeout=5)))
            self.commit(self.ledger.register_tool_execution(identity,{'tool_id':'ended-tool','pid':worker.pid,
                'process_start':runtime.pid_identity(worker.pid),
                'cgroup':Path('/proc',str(worker.pid),'cgroup').read_text()},command_id='ended-tool'))
            worker.terminate(); worker.wait(timeout=5)
        for index in range(count):
            with diagnostics.operation_context({**identity,'pool_path':str(self.pool_path)},
                    operation='checkpoint',capability='capability-'+str(index)):
                diagnostics.record_result(self.ledger.path,
                    {'status':'UNVERIFIED','code':'BUSINESS_SOURCE_UNAVAILABLE'})
        before = self.observe()
        self.assertEqual(count,sum(row['type']=='write_authority_unverified' for row in before['anomalies']))
        child.terminate(); child.wait(timeout=5)
        self.commit(self.ledger.end_execution(identity,command_id='ended-fixture'))
        return identity

    def test_schema2_terminated_write_closures_use_prior_incident_lookup_beyond_64(self):
        self.ended_write_incident(count=65)
        after = self.observe()
        outputs = [row for row in after['transitions'] if row['type']=='write_authority_unverified']
        self.assertEqual(65,len(outputs))
        self.assertTrue(all(row['transition']=='closed_with_execution' for row in outputs))
        self.assertLessEqual(after['write_closure_page']['queried'],128)
        self.assertFalse(any(row['type']=='write_authority_unverified' for row in after['anomalies']))

    def test_schema2_partial_cache_rebuild_cannot_turn_missing_incident_into_recovery(self):
        import workflow_diagnostics as diagnostics
        self.ended_write_incident()
        partial = {'rule_version':diagnostics.VERSION,'source':str(diagnostics.store_path(self.pool_path)),
            'source_state':'OBSERVED','write_paths':[],'anomalies':[],'closures':[],'verification':[]}
        page = {'source_state':'OBSERVED','closures':[],'queried':1,'next_after':'','has_more':False}
        with patch.object(diagnostics,'observe',return_value=partial), \
                patch.object(diagnostics,'query_closures',return_value=page):
            after = self.observe()
        self.assertTrue(any(row['type']=='write_authority_unverified' for row in after['anomalies']))
        self.assertFalse(any(row['type']=='write_authority_unverified' for row in after['transitions']))

    def test_schema2_actual_new_checkpoint_supplies_exact_write_recovery_proof(self):
        import workflow_diagnostics as diagnostics
        _, identity = self.bound_worker()
        scope = {**identity,'pool_path':str(self.pool_path)}
        with diagnostics.operation_context(scope,operation='checkpoint',capability='checkpoint'):
            diagnostics.record_result(self.ledger.path,
                {'status':'UNVERIFIED','code':'BUSINESS_SOURCE_UNAVAILABLE'})
        self.observe()
        with diagnostics.operation_context(scope,operation='checkpoint',capability='checkpoint'):
            self.commit(self.ledger.checkpoint(identity,{'next':'actual successful business write'},
                                               command_id='new-business-write'))
        after = self.observe()
        self.assertEqual(['recovered'],[row['transition'] for row in after['transitions']
            if row['type']=='write_authority_unverified'])

    def test_schema2_partial_proc_scan_cannot_certify_ended_scope_empty(self):
        identity = self.ended_write_incident()
        with sqlite3.connect(self.ledger.path) as db:
            db.execute('UPDATE executions SET pid=?,process_start=? WHERE execution_id=?',
                (os.getpid(),'old-reused-start',identity['execution_id']))
        original = Path.read_text
        def partial(path,*args,**kwargs):
            if str(path) == '/proc/1/stat':
                raise PermissionError('isolated unreadable process')
            return original(path,*args,**kwargs)
        with patch.object(Path,'read_text',partial):
            after = self.observe()
        self.assertNotIn(identity['execution_id'],after['closed_execution_ids'])
        self.assertTrue(any(row['type']=='write_authority_unverified' for row in after['anomalies']))
        self.assertFalse(any(row['type']=='write_authority_unverified' and row['transition']!='first'
                             for row in after['transitions']))

    def test_schema2_unverifiable_owner_or_tool_scope_cannot_close_ended_write_path(self):
        identity = self.ended_write_incident(tool=True)
        with sqlite3.connect(self.ledger.path) as db:
            original = db.execute('SELECT writer FROM executions WHERE execution_id=?',
                                  (identity['execution_id'],)).fetchone()[0]
            unknown = json.loads(original); unknown['dedicated_process_group'] = False
            db.execute('UPDATE executions SET writer=? WHERE execution_id=?',(json.dumps(unknown),identity['execution_id']))
        owner_unknown = self.observe()
        self.assertNotIn(identity['execution_id'],owner_unknown['closed_execution_ids'])
        with sqlite3.connect(self.ledger.path) as db:
            db.execute('UPDATE executions SET writer=? WHERE execution_id=?',(original,identity['execution_id']))
            tool_writer = json.loads(db.execute('SELECT writer FROM execution_tools WHERE execution_id=?',
                                               (identity['execution_id'],)).fetchone()[0])
            tool_writer['dedicated_process_group'] = False
            db.execute('UPDATE execution_tools SET writer=? WHERE execution_id=?',
                       (json.dumps(tool_writer),identity['execution_id']))
        tool_unknown = self.observe()
        self.assertNotIn(identity['execution_id'],tool_unknown['closed_execution_ids'])
        self.assertTrue(any(row['type']=='observer_tool_writer_unverified' for row in tool_unknown['faults']))
        self.assertTrue(any(row['type']=='write_authority_unverified' for row in tool_unknown['anomalies']))

    def action(self, authorization):
        _, developer = self.bound_worker()
        (self.repo/'app.py').write_text('value = 2\n')
        self.git('add', '.'); self.git('commit', '-qm', 'candidate')
        evidence = self.home/'self-test.txt'; evidence.write_text('actual local fixture evidence\n')
        candidate = self.commit(self.ledger.deliver(developer, {'repo': str(self.repo),
            'base': self.base, 'head': self.git('rev-parse', 'HEAD'), 'ac_version': 'frozen',
            'evidence': [{'path': str(evidence)}]}, '冻结测试候选', command_id='deliver'))['candidate_id']
        _, coordinator = self.bound_worker('COORDINATOR')
        grant = self.commit(self.ledger.grant_external_authorization({'authorization_id': 'isolated-grant',
            'candidate_id': candidate, 'service_kind': 'strong_review', 'max_calls': 1, 'expires_at': time.time()+300},
            {'authenticated': True, 'current_full': True, 'message_id': 'fixture-grant', 'user_id': 'fixture-user',
             'primary_text': '本轮全部权限：对此冻结候选审查一次'}, command_id='grant'))
        auth = {'authorization_id': grant['authorization_id']}
        self.commit(self.ledger.register_external_action(coordinator, 'isolated-action', 'strong_review',
            candidate, auth, command_id='register-action'))
        if authorization:
            # Adapter persistence contract fixture, actual service observation
            # is still read from systemd. Never written into a production home.
            native_id = uuid.uuid4().hex
            spec = self.home/'native-spec.json'
            runtime.save(spec, {'unit': authorization['unit'], 'call_id': native_id})
            current = next(row for row in self.ledger.snapshot()['candidates'] if row['candidate_id'] == candidate)
            path = self.ledger.path.parent/'services'/hashlib.sha256(b'isolated-action').hexdigest()/'invocation.json'
            runtime.save(path, {'schema_version': 1, 'action_id': 'isolated-action', 'kind': 'strong_review',
                'candidate_id': candidate, 'frozen': {'action_id': 'isolated-action', 'candidate': current,
                    'authorization_id': grant['authorization_id']},
                'service_identity': {'token': 'private'}, 'reference': {'unit': authorization['unit'],
                    'native_id': native_id, 'spec': {'path': str(spec), 'sha256': hashlib.sha256(spec.read_bytes()).hexdigest()}}})

    def test_schema2_missing_independent_service_identity_is_observer_fault(self):
        self.action({})
        result = self.observe()
        self.assertTrue(any(row['type'] == 'observer_service_identity_unverified' for row in result['faults']))
        self.assertEqual(result['services'][0]['observation'], 'unverified')
        self.assertNotIn('private', json.dumps(result))

    def test_schema2_real_independent_systemd_service_does_not_publish_business_result(self):
        unit = 'role-observer-fixture-'+uuid.uuid4().hex+'.service'
        launched = subprocess.run(['systemd-run', '--user', '--quiet', '--no-ask-password',
            '--unit='+unit, '--property=Type=exec', '/bin/sleep', '60'], capture_output=True, text=True)
        self.assertEqual(launched.returncode, 0, launched.stderr)
        self.addCleanup(lambda: subprocess.run(['systemctl', '--user', 'stop', unit],
            capture_output=True, text=True, timeout=10))
        self.action({'unit': unit})
        before = self.ledger.snapshot()
        result = self.observe()
        self.assertEqual(result['services'][0]['observation'], 'active_independent_service', result)
        self.assertEqual(self.ledger.snapshot(), before)
        self.assertEqual(before['external_actions'][0]['state'], 'SUBMITTED')


if __name__ == '__main__':
    unittest.main()
