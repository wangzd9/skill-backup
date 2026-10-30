"""Production incident regressions using isolated SQLite, files and live owners."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
CORE=Path('/home/wang/.hermes/hermes-agent')
for path in (ROOT,ROOT/'tests',CORE):
    sys.path.insert(0,str(path))

from test_business_ledger import LedgerFixture
import test_monitor_closeout as monitor_tests
DAY,NOW=monitor_tests.DAY,monitor_tests.NOW
import monitor_queue as queue
import monitor_notifications as notifications
import scheduler_observation as adoption
import workflow_runtime as workflow
import workflow_sessions as sessions


class DeliveryRepairTests(unittest.TestCase):
    setUp=monitor_tests.QueueTests.setUp
    add=monitor_tests.QueueTests.add
    def test_small_per_role_backlog_is_compacted_without_losing_original_bytes(self):
        originals={}
        for n in range(7):
            self.add(n,'A')
            path=self.outbox/(str(n).zfill(5)+'.json'); originals[path.name]=path.read_bytes()
        prepared=notifications.prepare(self.outbox,at=DAY)
        self.assertEqual((1,0),(prepared['pending_summary'],prepared['pending_ordinary']))
        rows=[json.loads(p.read_text()) for p in self.outbox.glob('*.json')]
        summary=next(r for r in rows if r['event'].get('summary_key'))
        self.assertEqual(7,summary['event']['member_count'])
        for member in summary['summary_members']:
            sha=member['source_sha256']
            self.assertEqual(originals[member['name']],(self.outbox/'evidence'/sha[:2]/(sha+'.json')).read_bytes())
        self.assertEqual(0,notifications.prepare(self.outbox,at=DAY)['compacted_summary_groups'])

    def test_readonly_status_explains_group_exhaustion_with_pool_capacity_left(self):
        for n in range(3):
            self.add(n)
            notifications.drain(self.outbox,lambda e:{'confirmed':True},at=NOW+n*301)
        self.add(4)
        result=notifications.drain(self.outbox,lambda e:self.fail('group window exhausted'),at=NOW+904)
        self.assertTrue(any(r.get('reason')=='ROLLING_WINDOW_BUDGET' for r in result))
        before=(self.outbox/'.delivery-index.sqlite3').read_bytes()
        value=queue.delivery_status(self.outbox,at=NOW+904)
        self.assertEqual(3,value['attempts_in_window'])
        self.assertEqual(['GROUP_WINDOW'],value['groups'][0]['blocked_by'])
        self.assertEqual(NOW+3600,value['groups'][0]['next_eligible_at'])
        self.assertEqual(before,(self.outbox/'.delivery-index.sqlite3').read_bytes())


class AdoptionRepairTests(unittest.TestCase):
    def test_later_worker_probe_preserves_verified_adoption_and_rejects_dead_owner(self):
        with tempfile.TemporaryDirectory() as folder:
            home=Path(folder)
            adoption.receipt(home,'COLLECTED',workflow_tick_submitted=True,observation_at=123)
            adoption.receipt(home,'TRIGGERED')
            observed=adoption.capability(home)
            self.assertTrue(observed['adopted'])
            self.assertEqual(123,observed['observation_at'])
            path=home/'runtime/role-observation/hook-receipt.json'
            value=json.loads(path.read_text()); value['process_start']='wrong-start'
            path.write_text(json.dumps(value))
            self.assertFalse(adoption.capability(home)['adopted'])

    def test_probe_without_real_event_submission_never_claims_adoption(self):
        with tempfile.TemporaryDirectory() as folder:
            adoption.receipt(folder,'COLLECTED',workflow_tick_submitted=False,observation_at=123)
            self.assertFalse(adoption.capability(folder)['adopted'])


class SessionRepairTests(LedgerFixture):
    def test_threshold_allows_durable_yield_and_same_work_resumes_with_fresh_budget(self):
        worker,identity=self.bind('A')
        scope={**identity,'kind':'business','workflow_version':1,'session_policy':sessions.DEFAULT_POLICY}
        self.assertFalse(sessions.observe(self.root,scope,api_call_count=2)['checkpoint_required'])
        self.assertTrue(sessions.observe(self.root,scope,api_call_count=47)['checkpoint_required'])
        self.assertEqual('block',sessions.tool_decision(self.root,scope,'terminal',{})['action'])
        payload={'checkpoint':{'next':'continue exact task','pending':['unresolved gate']},'yield_execution':True}
        self.assertIsNone(sessions.tool_decision(self.root,scope,'role_development_command',{'action':'checkpoint','payload':payload}))
        result=self.ledger.checkpoint(identity,payload['checkpoint'],yield_execution=True,command_id='yield')
        self.assertCommitted(result)
        self.assertFalse(self.ledger.admission_hint('A')['can_start_model'])
        self.close(worker,identity)
        new_worker,new_identity=self.bind('A')
        self.assertEqual(identity['work_id'],new_identity['work_id'])
        self.assertEqual(payload['checkpoint'],self.ledger.current_work('A')['checkpoint'])
        new_scope={**scope,**new_identity}
        self.assertFalse(sessions.observe(self.root,new_scope,api_call_count=0)['checkpoint_required'])

    def test_token_threshold_and_profile_configuration_are_isolated(self):
        for name,limit in [('first',1000),('second',2000)]:
            home=self.root/name; home.mkdir()
            (home/'config.yaml').write_text('plugins:\n  entries:\n    role-workflow-runtime:\n      settings:\n        session_policy: {checkpoint_input_tokens: '+str(limit)+'}\n')
        scope={'kind':'business','workflow_version':1,'work_id':'same-work','execution_id':'same-execution','role':'A'}
        for name,expected in [('first',True),('second',False),('first',True)]:
            value=sessions.observe(self.root/name,scope,approx_input_tokens=1500)
            self.assertEqual(expected,value['checkpoint_required'])
        self.assertIsNone(sessions.observe(self.root,{'kind':'report','workflow_version':1}))


class OutcomeRepairTests(LedgerFixture):
    def test_unknown_native_outcome_is_distinct_from_verified_writer_exit(self):
        worker,identity=self.bind('A')
        source=self.root/'native.sqlite3'
        with sqlite3.connect(source) as db:
            db.execute('CREATE TABLE executions(id TEXT,pid INTEGER,process_started_at TEXT,status TEXT)')
            execution=next(e for e in self.ledger.snapshot()['executions'] if e['execution_id']==identity['execution_id'])
            db.execute('INSERT INTO executions VALUES(?,?,?,?)',(identity['execution_id'],execution['pid'],execution['process_start'],'unknown'))
        self.assertCommitted(self.ledger.observe_execution_outcome(identity['execution_id'],source,command_id='unknown'))
        self.assertFalse(self.ledger.admission_hint('A')['can_start_model'])
        self.close(worker,identity)
        observed=self.ledger.snapshot()['executions'][0]
        self.assertEqual(('ENDED','UNKNOWN',True),(observed['state'],observed['native_outcome'],observed['writer_cleared']))
        self.assertEqual('QUEUED',self.ledger.current_work('A')['state'])

    def test_active_observer_does_not_read_legacy_fingerprints_or_reconcile_legacy(self):
        import role_observer
        home=self.root/'home';home.mkdir()
        pool=home/'role-pool.json';pool.write_text('{}')
        database=workflow.ledger_path(pool);database.parent.mkdir()
        with sqlite3.connect(self.path) as source,sqlite3.connect(database) as target:source.backup(target)
        project=home/'project';project.mkdir()
        (project/'ROLE-BINDINGS.json').write_text(json.dumps({'schema_version':1,'project_root':str(project),
            'control':'ACTIVE','roles':{'A':{}},'role_pool':{'path':str(pool)}}))
        registry=home/'registry.json';registry.write_text(json.dumps({'projects':[{'project_root':str(project)}]}))
        monitor={'health':'ok','faults':[],'transitions':[],'outbox':str(home/'outbox')}
        with (mock.patch.object(role_observer,'observe',side_effect=AssertionError('legacy scan')),
             mock.patch.object(role_observer,'observe_pool_leases',return_value=monitor),
             mock.patch.object(role_observer.runtime,'requirement_module',side_effect=AssertionError('legacy reconcile'))):
            rows=role_observer.scan_registry(registry)
            self.assertEqual('ACTIVE',rows[0]['mode'])
            self.assertEqual('ACTIVE',role_observer.scan_pool(pool)[0]['mode'])


class SessionAttributionTests(unittest.TestCase):
    def test_direct_message_and_unrelated_chat_cannot_inherit_parent_group_attribution(self):
        spec=importlib.util.spec_from_file_location('mechanism_repair_plugin',ROOT/'__init__.py',submodule_search_locations=[str(ROOT)])
        module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as folder:
            home=Path(folder);project=home/'project';project.mkdir()
            (project/'ROLE-BINDINGS.json').write_text(json.dumps({'schema_version':1,'project_root':str(project),
                'control':'ACTIVE','roles':{'A':{'chat_id':'role-chat'}}}))
            registry=home/'registry.json';registry.write_text(json.dumps({'projects':[{'project_root':str(project)}]}))
            with sqlite3.connect(home/'state.db') as db:
                db.execute('CREATE TABLE sessions(id TEXT,chat_id TEXT,parent_session_id TEXT,chat_type TEXT)')
                db.executemany('INSERT INTO sessions VALUES(?,?,?,?)',[
                    ('group','role-chat',None,'group'),('dm','role-chat','group','dm'),
                    ('unrelated','other-chat','group','group'),('compressed',None,'group',None)])
            plugin=module.RuntimePlugin(registry=registry,home=home,metrics_url='unused')
            self.assertIsNone(plugin.api_identity('dm'))
            self.assertIsNone(plugin.api_identity('unrelated'))
            self.assertEqual('A',plugin.api_identity('compressed')['role'])


class ActivationRepairTests(LedgerFixture):
    def test_query_activation_manifest_covers_transitive_local_helpers(self):
        # Fresh imports mask missing helpers in an already-running gateway.
        # Keep the future-call manifest closed over the query's local functions.
        import ast
        import mechanism_activation
        tree=ast.parse((ROOT/'workflow_query.py').read_text())
        definitions={node.name:node for node in tree.body if isinstance(node,ast.FunctionDef)}
        targets=set(mechanism_activation.TARGETS['workflow_query.py'])
        for name in targets:
            dependencies={node.id for node in ast.walk(definitions[name])
                if isinstance(node,ast.Name) and isinstance(node.ctx,ast.Load)} & definitions.keys()
            self.assertFalse(dependencies-targets,(name,sorted(dependencies-targets)))

    def test_live_query_and_report_work_after_upgrading_an_old_module_namespace(self):
        import mechanism_activation
        import workflow_query
        from workflow_reports import render_live_progress_fallback, _progress_report_format_error
        worker,identity=self.bind('A')
        before=self.ledger.snapshot()
        missing=('_paged_artifact','_history_index','_task_summary','_role_summary')
        previous={name:getattr(workflow_query,name) for name in missing}
        pool=self.root/'isolated.json'
        pool.write_text('{}')
        try:
            for name in missing:
                delattr(workflow_query,name)
            # Reproduce the actual pre-upgrade query failure in isolation.
            with self.assertRaises(NameError):
                workflow_query.live_progress(pool,'COORDINATOR')
            activation=mechanism_activation.activate()
            bindings=activation['readonly_query_bindings']
            self.assertEqual('READY',bindings['state'])
            self.assertTrue(set(missing)<=set(bindings['functions']))
            packet=workflow_query.live_progress(pool,'COORDINATOR')
            report=render_live_progress_fallback(packet,'COORDINATOR')
            self.assertIsNone(_progress_report_format_error(report,'COORDINATOR'))
            self.assertIn('T1 attempt 1 开发中',report)
            self.assertNotIn('当次只读核验未完成',report)
            self.assertLess(len(json.dumps(packet,ensure_ascii=False).encode()),8192)
            manifest=json.loads(Path(packet['artifacts']['manifest']['path']).read_text())
            history=json.loads(Path(manifest['history']['path']).read_text())
            self.assertEqual(1,history['work_count'])
            self.assertEqual(before,self.ledger.snapshot())
            self.assertIsNone(worker.poll())
        finally:
            for name,value in previous.items():
                setattr(workflow_query,name,value)

    def test_activation_refreshes_plan_schema_constants(self):
        import mechanism_activation
        import workflow_contracts
        import workflow_query
        previous_task=workflow_contracts.TASK
        previous_payloads=workflow_contracts.PAYLOADS
        previous_target=workflow_contracts.CONTROL_TARGET
        previous_paged=getattr(workflow_query,'_paged_artifact',None)
        try:
            workflow_contracts.TASK={'stale':True}
            workflow_contracts.PAYLOADS={'publish_plan':{'stale':True}}
            workflow_contracts.CONTROL_TARGET={'stale':True}
            delattr(workflow_query,'_paged_artifact')
            result=mechanism_activation.activate()
            self.assertEqual('ACTIVATED',result['state'])
            self.assertIn('stage_dependencies',workflow_contracts.TASK['properties'])
            self.assertIn('provided_contracts',workflow_contracts.TASK['properties'])
            self.assertIn('stage_dependencies',workflow_contracts.PAYLOADS['publish_plan']['properties']['tasks']['items']['properties'])
            self.assertTrue(callable(workflow_query._paged_artifact))
        finally:
            workflow_contracts.TASK=previous_task
            workflow_contracts.PAYLOADS=previous_payloads
            workflow_contracts.CONTROL_TARGET=previous_target
            if previous_paged is not None:
                workflow_query._paged_artifact=previous_paged

    def test_activation_preserves_live_owner_dispatch_slots_and_business_facts(self):
        import mechanism_activation
        import workflow_dispatch
        worker,identity=self.bind('A')
        before=self.ledger.snapshot()
        state=workflow_dispatch._STATE
        locks={key:id(state[key]) for key in ('lock','slots','inflight')}
        result=mechanism_activation.activate()
        self.assertEqual('ACTIVATED',result['state'])
        self.assertEqual(before['event_high_water'],self.ledger.snapshot()['event_high_water'])
        self.assertEqual(identity['execution_id'],self.ledger.current_work('A')['active_execution_id'])
        self.assertIsNone(worker.poll())
        self.assertIs(state,workflow_dispatch._STATE)
        self.assertEqual(locks,{key:id(state[key]) for key in locks})
        self.assertCommitted(self.ledger.checkpoint(identity,{'next':'same owner continues'},command_id='after-activation'))
