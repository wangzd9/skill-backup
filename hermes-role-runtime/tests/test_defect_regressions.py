"""Behavior regressions for audited failures; isolated resources, no external messages."""
import contextvars
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import control_intents
import execution_retention
import monitor_notifications
import role_observer
import role_runtime
from scripts.runtime_layout import reconcile, wrapper_layout
from tests import test_workflow_migration as migration_tests
from tests import test_business_ledger as ledger_tests
import workflow_migration as migration
import workflow_runtime

DAY = datetime(2026,10,20,12,tzinfo=ZoneInfo('Asia/Shanghai'))


class NotificationBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.outbox=Path(self.temp.name)

    def row(self, n, **event):
        path=self.outbox/(str(n).zfill(5)+'.json')
        role_runtime.save(path, {'event': {'event_id':str(n), 'severity':1, **event},
            'delivery_receipt': {'state':'QUIET_DEFERRED' if n % 2 else 'PENDING'}})
        return path

    def test_125_old_notifications_are_summarized_with_source_receipts_preserved(self):
        for n in range(125): self.row(n, occurred_at='2020-01-01T00:00:00+08:00')
        sent=[]
        result=monitor_notifications.drain(self.outbox,lambda e:sent.append(e) or {'confirmed':True},
            at=DAY,max_messages=7,max_seconds=5)
        self.assertEqual(len(sent),1)
        self.assertEqual(sent[0]['type'],'observer_history_summary')
        self.assertEqual(sent[0]['member_count'],125)
        states=[role_runtime.load(p)['delivery_receipt']['state'] for p in self.outbox.glob('*.json')]
        self.assertEqual(states.count('SENT'),1)
        self.assertEqual(states.count('SUPERSEDED'),125)
        self.assertTrue(all(r['state']=='SENT' for r in result))

    def test_hung_transport_is_backpressured_context_preserved_unknown_not_retried(self):
        self.row(0); self.row(1)
        release=threading.Event(); entered=threading.Event(); completed=threading.Event()
        self.addCleanup(release.set)
        identity=contextvars.ContextVar('fixture-profile'); identity.set('profile-A')
        calls=[]
        def send(event):
            calls.append((event['event_id'],identity.get()))
            entered.set(); release.wait(10); completed.set()
            return {'confirmed':True}
        monitor_notifications.prepare(self.outbox,at=DAY)
        started=time.monotonic()
        result=monitor_notifications.drain(self.outbox,send,at=DAY,max_seconds=2)
        self.assertTrue(entered.is_set()); self.assertLess(time.monotonic()-started,4)
        self.assertEqual(result[0]['state'],'UNKNOWN')
        for _ in range(4): monitor_notifications.drain(self.outbox,send,at=DAY,max_seconds=.1)
        self.assertEqual(calls,[('0','profile-A')])
        release.set(); self.assertTrue(completed.wait(3))
        deadline=time.monotonic()+3
        while role_runtime.load(self.outbox/'00000.json')['delivery_receipt']['state']=='UNKNOWN' and time.monotonic()<deadline:
            time.sleep(.01)
        self.assertEqual(role_runtime.load(self.outbox/'00000.json')['delivery_receipt']['state'],'SENT')

    def test_held_delivery_lock_returns_within_budget_without_transport(self):
        self.row(0)
        with (self.outbox/'delivery.lock').open('a+') as held:
            fcntl.flock(held,fcntl.LOCK_EX)
            result=monitor_notifications.drain(self.outbox,lambda e:self.fail('send while locked'),at=DAY,max_seconds=.1)
            self.assertEqual(result,[{'state':'BACKPRESSURE','reason':'DELIVERY_LOCK_BUSY'}])

    def test_semantic_coalescing_preserves_episode_generation_and_recovery(self):
        base={'pool_path':'/fixture/pool','generation':1,'role':'A','execution_id':'a'*32,
              'episode':1,'transition':'START','identity':'anomaly'}
        self.row(0,**base); self.row(1,**base)
        self.row(2,**{**base,'generation':2}); self.row(3,**{**base,'transition':'RECOVER'})
        sent=[]
        monitor_notifications.drain(self.outbox,lambda e:sent.append(e) or {'confirmed':True},at=DAY,
            delivery_policy={'group_gap_seconds':0})
        self.assertEqual(len(sent),3)
        self.assertEqual(role_runtime.load(self.outbox/'00001.json')['delivery_receipt']['state'],'SUPERSEDED')
        self.assertEqual(role_runtime.load(self.outbox/'00001.json')['delivery_receipt']['superseded_by'],'0')


class SourceClosureTests(unittest.TestCase):
    def test_registry_scan_routes_pool_monitor_through_three_state_collector(self):
        from test_lease_monitor import PoolFixture
        fixture=PoolFixture(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        registry=fixture.home/'role-projects.json'
        role_runtime.save(registry,{'projects':[]})
        child=fixture.worker(); fixture.native(child)
        child.terminate(); child.wait()
        with patch.object(role_observer, 'observe_pool_leases', wraps=role_observer.observe_pool_leases) as observed:
            result=role_observer.scan_registry(registry)
        monitor=next(row for row in result if row.get('mode')=='lease_monitor')
        self.assertTrue(observed.called)
        self.assertEqual(monitor['reason'],'mechanical_observation_only')
        self.assertGreaterEqual(monitor['fault_counts'].get('observer_frozen_execution_unverified',0),1)
        snapshot=Path(monitor['full_snapshot'])
        self.assertTrue(snapshot.is_file())
        saved=role_runtime.load(snapshot)
        fault=next(f for f in saved['faults'] if f['type']=='observer_frozen_execution_unverified')
        self.assertEqual(fault['observation_state'],'UNKNOWN')
        self.assertIn('TERMINATION_PROOF_UNVERIFIED',fault['reason_codes'])

    def test_registry_cli_preserves_unknown_reasons_in_full_snapshot(self):
        from test_lease_monitor import PoolFixture
        fixture=PoolFixture(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        registry=fixture.home/'role-projects.json'
        role_runtime.save(registry,{'projects':[]})
        child=fixture.worker(); fixture.native(child)
        child.terminate(); child.wait()
        # Same interpreter and --registry shape as the service; only fixture paths.
        check=subprocess.run(['/usr/bin/python3','-B',str(Path(role_observer.__file__).resolve()),
            'scan','--registry',str(registry)],capture_output=True,text=True,cwd='/tmp',timeout=30)
        self.assertEqual(check.returncode,0,check.stderr)
        monitor=next(row for row in json.loads(check.stdout) if row.get('mode')=='lease_monitor')
        snapshot=role_runtime.load(Path(monitor['full_snapshot']))
        unknown=[f for f in snapshot['faults'] if f.get('observation_state')=='UNKNOWN']
        self.assertTrue(unknown)
        self.assertTrue(all('TERMINATION_PROOF_UNVERIFIED' in f['reason_codes'] for f in unknown))

    def test_skill_cold_process_has_complete_imports_without_requirement_warmup(self):
        layout=tempfile.TemporaryDirectory(); self.addCleanup(layout.cleanup)
        entry=Path(layout.name)/'skills/complex-task-coordinator/scripts'
        reconcile(entry,apply=True)
        self.assertTrue(reconcile(entry)['valid'])
        code="import sys; sys.path.insert(0,sys.argv[1]); import role_observer,role_status,business_ledger,workflow_observation,report_gate,activity_report,migration_contracts,monitor_notifications; print(role_status.runtime_module().__file__)"
        result=subprocess.run([sys.executable,'-I','-B','-c',code,str(entry)],capture_output=True,text=True,cwd='/tmp')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue(Path(result.stdout.strip()).resolve().is_relative_to(Path(migration.__file__).parent))

    def test_report_and_service_helpers_keep_the_candidate_source_root(self):
        program="""
import json,sys
from pathlib import Path
root=Path(sys.argv[1]);sys.path.insert(0,str(root))
import runtime_tools,report_summary,progress_query
from workflow_services import NativeServices
activity=report_summary.activity_module()
assert Path(activity.__file__).resolve().parent==root
for helper in (activity.time_accounting_module,activity.usage_accounting_module,activity.observation_store_module):
 assert Path(helper().__file__).resolve().parent==root
import ocr_runtime
assert ocr_runtime.MODEL_SELECTOR.parent==root
assert NativeServices().scripts==root
assert str(Path.home()/'.codex/skills/complex-task-coordinator/scripts') not in sys.path
print(json.dumps({'activity':activity.__file__,'service_scripts':str(NativeServices().scripts)}))
"""
        root=Path(role_observer.__file__).resolve().parent
        check=subprocess.run([sys.executable,'-I','-B','-c',program,str(root)],
            capture_output=True,text=True,cwd='/tmp',timeout=30)
        self.assertEqual(check.returncode,0,check.stderr)

    def test_copied_ocr_worker_retains_frozen_selector_and_rejects_changed_bytes(self):
        import shutil
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder);worker=folder/'worker';worker.mkdir()
            root=Path(role_observer.__file__).resolve().parent
            for name in ('ocr_runtime.py','ocr_results.py','ocr_session_status.py'):
                shutil.copy2(root/name,worker/name)
            route={'provider':'fixture','model':'fixture-model','endpoint':'https://fixture.invalid'}
            selector=folder/'selector.py'
            selector.write_text('import json;print(json.dumps('+repr({**route,'allowed_routes':[route]})+'))')
            program="""
import importlib.util,json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
spec=importlib.util.spec_from_file_location('copied_ocr',sys.argv[2])
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
selector=Path(sys.argv[3]);route=json.loads(sys.argv[4])
frozen={'project':str(selector.parent),'model_routing':{
 'selector_path':str(selector),'selector_sha256':module.digest(selector.read_bytes()),'allowed_routes':[route]}}
assert not module.MODEL_SELECTOR.exists()
assert module.batch_route(frozen)==route
selector.write_text('# changed')
try: module.batch_route(frozen)
except RuntimeError as error: assert str(error)=='MODEL_SELECTOR_CHANGED'
else: raise AssertionError('changed selector accepted')
"""
            check=subprocess.run([sys.executable,'-I','-B','-c',program,str(root),str(worker/'ocr_runtime.py'),
                str(selector),json.dumps(route)],capture_output=True,text=True,cwd='/tmp',timeout=30)
            self.assertEqual(check.returncode,0,check.stderr)

    def test_independent_observer_actual_interpreters_with_staged_and_active_authority(self):
        from test_lease_monitor import PoolFixture
        fixture=PoolFixture(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        from business_ledger import BusinessLedger
        db=fixture.pool_path.with_suffix('.runtime')/'business.sqlite3'
        ledger=BusinessLedger(db)
        layout=tempfile.TemporaryDirectory(); self.addCleanup(layout.cleanup)
        entry=Path(layout.name)/'skills/complex-task-coordinator/scripts'
        reconcile(entry,apply=True)
        program="import sys;sys.path.insert(0,sys.argv[1]);import role_observer; v=role_observer.collect_pool_leases(sys.argv[2],home=sys.argv[3]);print(v.get('business_mode',v.get('mode')),v.get('observation_schema'))"
        for mode in ('STAGED','ACTIVE'):
            if mode=='ACTIVE': self.assertEqual(ledger.set_mode('ACTIVE',command_id='test-cold-active')['status'],'COMMITTED')
            for interpreter in (sys.executable,'/usr/bin/python3'):
                result=subprocess.run([interpreter,'-I','-B','-c',program,str(entry),str(fixture.pool_path),str(fixture.home)],
                    cwd='/tmp',capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertIn(mode,result.stdout)
                if mode=='ACTIVE': self.assertIn('2',result.stdout)

    def test_null_sessions_and_actual_legacy_report_entries_do_not_crash(self):
        from cron.role_pool_scheduler import scope_for_session
        from test_lease_monitor import PoolFixture
        import report_gate,activity_report
        fixture=PoolFixture(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        (fixture.root/'TODO.md').write_text('| TASK-01 | ✅ | result | AC-1 | none | DONE | 1号 |\n')
        (fixture.root/'inbox').mkdir(exist_ok=True)
        (fixture.root/'inbox/PROTOCOL.md').write_text('# Protocol\n')
        with patch.dict(os.environ,{'HERMES_HOME':str(fixture.home)}):
            for value in (None,'','  ',123):
                self.assertIsNone(scope_for_session(value,fixture.home))
            report_gate.routing(fixture.root,'A')
            result=activity_report.build(fixture.root,'A','phase',at=DAY,collect_current=False,include_progress=False)
            self.assertIsInstance(result,dict)

    def test_retention_requires_native_identity_and_complete_empty_dedicated_scope(self):
        scope={'execution_id':'a'*32,'job_id':'A-job'}
        unit='hermes-worker-cron-A-job-exec-'+scope['execution_id']+'.scope'
        native={'id':scope['execution_id'],'job_id':'A-job','pid':40000,'process_started_at':123,
                'status':'completed','writer_scope': {'pid':40000,'process_start':123,'cgroup':'0::/fixture/'+unit}}
        self.assertIsNone(execution_retention.termination(scope,None,{}))
        self.assertIsNone(execution_retention.termination(scope,{**native,'writer_scope':None},{}))
        self.assertIsNotNone(execution_retention.termination(scope,native,{}))
        child={45:{'state':'S','process_start':'999','cgroup':'0::/fixture/'+unit+'/child'}}
        self.assertIsNone(execution_retention.termination(scope,native,child))
        owner={40000:{'state':'S','process_start':'123','cgroup':'0::/elsewhere'}}
        self.assertIsNone(execution_retention.termination(scope,native,owner))


class InventoryBoundaryTests(unittest.TestCase):
    setUp = migration_tests.MigrationTests.setUp
    git = migration_tests.MigrationTests.git
    write = migration_tests.MigrationTests.write
    inspect = migration_tests.MigrationTests.inspect
    staged = migration_tests.MigrationTests.staged
    def test_hot_observations_are_fresh_but_policy_and_terminal_service_results_are_fenced(self):
        observer=self.write(self.root/'inbox/control/runtime/observer/A.json',{'checked_at':1})
        state=self.write(self.root/'reviews/fixture/state.json',{'unit':'missing-fixture.service','state':'RUNNING','updated_at':1})
        self.write(state.with_name('spec.json'),{'unit':'missing-fixture.service','run_id':'fixed'})
        manifest=self.inspect()
        observer.write_text('{"checked_at":2}')
        saved=json.loads(state.read_text()); saved['updated_at']=2; state.write_text(json.dumps(saved))
        jobs=json.loads((self.home/'cron/jobs.json').read_text()); jobs['jobs'][0]['last_run_at']='new sampling'
        self.write(self.home/'cron/jobs.json',jobs)
        changed,fresh=migration._sources_unchanged(manifest)
        self.assertEqual(changed,[])
        self.assertNotEqual(manifest['sources'],fresh['sources'])
        self.assertEqual(migration.prepare(manifest,self.proposal)['result']['status'],'COMMITTED')
        jobs['jobs'][0]['paused']=True; self.write(self.home/'cron/jobs.json',jobs)
        self.assertIn(str(self.home/'cron/jobs.json'),migration._sources_unchanged(manifest)[0])
        saved['state']='COMPLETED'; state.write_text(json.dumps(saved))
        self.assertIn(str(state),migration._sources_unchanged(manifest)[0])

    def test_unlisted_acceptance_pool_and_database_override_are_rejected(self):
        manifest,ledger=self.staged(False)
        item={'proof':{'pool_path':str(self.pool),'home':str(self.home)}}
        with self.assertRaisesRegex(ValueError,'DECLARED_ACCEPTANCE'):
            migration._verify_gate_proof('P0',item,manifest,ledger.path,self.now)
        with self.assertRaisesRegex(ValueError,'TARGET_DATABASE_MISMATCH'):
            migration.prepare(manifest,self.proposal,database=self.home/'other.sqlite3')


class ControlContractTests(unittest.TestCase):
    def test_ordered_exact_selection_and_rejections(self):
        plan=control_intents.selection_plan('停止 A，转 raw-collection',['raw-collection'])
        self.assertEqual(plan,{'target':'raw-collection','pre_controls':[{'action':'pause','role':'A'}]})
        for text in ('不要停止 A，转 raw-collection','如果停止 A，转 raw-collection',
                     '停止 A，转 unknown','转 raw-collection，转 raw-collection','他说转 raw-collection'):
            self.assertIsNone(control_intents.selection_plan(text,['raw-collection']),text)


class InstalledCronEntryTests(unittest.TestCase):
    def test_regular_entries_pass_native_script_guard_and_cold_observer_import(self):
        root=Path(__file__).resolve().parents[1]
        core=Path.home()/'.hermes/hermes-agent'
        with tempfile.TemporaryDirectory() as folder:
            home=Path(folder)
            plugin=home/'plugins/role-workflow-runtime'; plugin.parent.mkdir()
            plugin.symlink_to(root,target_is_directory=True)
            result=wrapper_layout(home,apply=True)
            self.assertTrue(result['valid'])
            for row in result['entries']:
                self.assertFalse(Path(row['entry']).is_symlink())
            # Use the real native resolver without running any production job.
            script='''
import json,sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,sys.argv[1])
from cron import scheduler,scheduler_script
with patch.object(scheduler,'_get_hermes_home',return_value=Path(sys.argv[2])):
 for entry in json.loads(sys.argv[3]):
  path,error=scheduler_script._resolve_script_path(entry)
  assert path is not None and error is None,(entry,error)
'''
            check=subprocess.run([sys.executable,'-I','-B','-c',script,str(core),str(home),
                json.dumps([row['entry'] for row in result['entries']])],cwd=folder,capture_output=True,text=True,timeout=30)
            self.assertEqual(check.returncode,0,check.stderr)
            check=subprocess.run(['/usr/bin/python3','-I','-B',str(home/'scripts/lease_observe.py'),'--help'],
                cwd=folder,capture_output=True,text=True,timeout=30)
            self.assertEqual(check.returncode,0,check.stderr)
            self.assertIn('--pool',check.stdout)
