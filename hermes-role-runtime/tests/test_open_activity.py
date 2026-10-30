"""Regress normal in-flight calls, lost receipts and concurrent native hooks."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from activity_signals import activity_step
import workflow_activity as collector


class OpenActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.scope = {'pool_path': str(self.home/'pool.json'), 'generation': 1,
            'run_id': 'run', 'role': 'A', 'work_id': 'work', 'task_id': 'T', 'attempt': 1,
            'execution_id': 'execution', 'execution_generation': 1}
        collector.start_coverage(self.home, self.scope, 0)
        collector.start_model_coverage(self.home, self.scope, 0)

    def observation(self, now):
        tools = collector.read_activity(self.home, self.scope, now)
        models = collector.read_model_activity(self.home, self.scope, now)
        return {'identity': {**self.scope, 'pool': self.scope['pool_path']},
            'current_work_valid': True, 'required_sources': ['tools', 'model_requests'],
            'sources': tools['sources']+models['sources'],
            'lifecycles': tools['lifecycles']+models['lifecycles'], 'checkpoint_at': 0}

    def test_short_open_calls_are_in_flight_without_end_or_enforced_wait_claims(self):
        collector.record_tool_start(self.home, self.scope, 'tool', 'read_file', 100)
        collector.record_model_start(self.home, self.scope, 'physical', 'logical', 101)
        value = self.observation(120)
        self.assertTrue(all(source['complete'] for source in value['sources']))
        facts, events = activity_step({}, value, 120, threshold_seconds=60)
        self.assertEqual('IN_FLIGHT', facts['state'])
        self.assertEqual([], events)
        self.assertEqual([], facts['normal_waits'])
        self.assertEqual(2, len(facts['in_flight']))
        self.assertTrue(all(not row['completion_verified'] for row in facts['in_flight']))
        self.assertTrue(all(row['wait_until'] is None and row['ended_at'] is None for row in value['lifecycles']))

    def test_tool_observation_grace_is_persisted_and_test_deadline_is_extended(self):
        self.assertEqual(180, collector.observation_grace_for_tool('terminal', {}))
        self.assertEqual(3780, collector.observation_grace_for_tool(
            'role_test_command', {'payload': {'timeout_seconds': 3600}}))
        collector.record_tool_start(self.home, self.scope, 'long-test', 'role_test_command', 100,
                                    observation_grace_seconds=3780)
        value = self.observation(3700)
        lifecycle = next(row for row in value['lifecycles'] if row['operation_id'] == 'long-test')
        self.assertEqual(3780, lifecycle['observation_grace_seconds'])
        facts, events = activity_step({}, value, 3700, threshold_seconds=60)
        self.assertEqual('IN_FLIGHT', facts['state'])
        self.assertEqual([], events)
        facts, events = activity_step(facts, self.observation(3881), 3881, threshold_seconds=60)
        self.assertEqual(['OPERATION_OBSERVATION_OVERDUE'], [row['type'] for row in events])

    def test_grace_expires_once_and_only_same_verified_end_closes_episode(self):
        collector.record_model_start(self.home, self.scope, 'p', 'logical', 100)
        facts, events = activity_step({}, self.observation(1000), 1000, threshold_seconds=60)
        self.assertEqual('IN_FLIGHT', facts['state']); self.assertEqual([], events)
        facts, events = activity_step(facts, self.observation(1001), 1001, threshold_seconds=60)
        self.assertEqual(['OPERATION_OBSERVATION_OVERDUE'], [row['type'] for row in events])
        self.assertEqual('OBSERVATION_OVERDUE', facts['state'])
        facts, events = activity_step(facts, self.observation(1002), 1002, threshold_seconds=60)
        self.assertEqual([], events)
        blind = self.observation(1003); blind['lifecycles'] = []; blind['sources'][1]['complete'] = False
        facts, events = activity_step(facts, blind, 1003, threshold_seconds=60)
        self.assertFalse(any(e['type']=='OPERATION_OBSERVATION_OVERDUE' and e['transition']=='recovered' for e in events))
        collector.record_model_end(self.home, self.scope, 'p', 'logical', 1010)
        facts, events = activity_step(facts, self.observation(1011), 1011, threshold_seconds=60)
        self.assertIn(('OPERATION_OBSERVATION_OVERDUE', 'recovered'), [(e['type'],e['transition']) for e in events])

    def test_real_wait_contract_overrides_grace_but_still_expires(self):
        collector.record_tool_start(self.home, self.scope, 't', 'terminal', 100, wait_until=2000)
        facts, events = activity_step({}, self.observation(1500), 1500, threshold_seconds=60)
        self.assertEqual('NORMAL_WAIT', facts['state']); self.assertEqual([], events)
        facts, events = activity_step(facts, self.observation(2001), 2001, threshold_seconds=60)
        self.assertEqual(['OPERATION_WAIT_EXPIRED'], [e['type'] for e in events])

    def test_new_request_does_not_reset_old_open_age(self):
        collector.record_model_start(self.home, self.scope, 'old', 'one', 100)
        collector.record_model_start(self.home, self.scope, 'new', 'two', 1000)
        facts, events = activity_step({}, self.observation(1001), 1001, threshold_seconds=60)
        self.assertEqual('OBSERVATION_OVERDUE', facts['state'])
        self.assertEqual('old', events[0]['member'])
        self.assertEqual('new', facts['in_flight'][0]['operation_id'])

    def test_genuine_receipt_loss_is_not_cleared_by_a_new_open_request(self):
        with patch.object(collector, '_database', side_effect=sqlite3.OperationalError('locked')):
            collector.record_tool_start(self.home, self.scope, 'lost', 'read_file', 100)
        collector.record_model_start(self.home, self.scope, 'p', 'logical', 110)
        value = self.observation(120)
        self.assertFalse(value['sources'][0]['complete'])
        self.assertIn('TOOL_ACTIVITY_RECEIPT_UNAVAILABLE', value['sources'][0]['reason_codes'])
        facts, events = activity_step({}, value, 120, threshold_seconds=60)
        self.assertEqual('UNVERIFIABLE', facts['state'])
        self.assertIn('ACTIVITY_SOURCE_UNVERIFIABLE', [e['type'] for e in events])

    def test_coverage_warmup_is_neither_blindness_nor_inactivity(self):
        facts, events = activity_step({}, self.observation(10), 10, threshold_seconds=600)
        self.assertEqual('WARMING_UP', facts['state'])
        self.assertEqual([], events)
        self.assertIn('INACTIVITY_WINDOW_WARMING_UP', facts['reason_codes'])
        facts, events = activity_step(facts, self.observation(601), 601, threshold_seconds=600)
        self.assertEqual(['NO_ACTIVITY'], [e['type'] for e in events])

    def test_native_startup_grace_is_bounded_and_does_not_hide_other_source_gaps(self):
        value = self.observation(10)
        for source in value['sources']:
            source.update(check_status='UNVERIFIED',complete=False,coverage_grace_until=60,
                reason_codes=[('TOOL' if source['source_id']=='tools' else 'MODEL')+'_COLLECTOR_COVERAGE_NOT_STARTED'])
        facts,events=activity_step({},value,10,threshold_seconds=600)
        self.assertEqual('WARMING_UP',facts['state']);self.assertEqual([],events)
        facts,events=activity_step(facts,value,61,threshold_seconds=600)
        self.assertEqual('UNVERIFIABLE',facts['state'])
        value['sources'][0]['reason_codes']=['EXECUTION_SESSION_UNVERIFIED']
        facts,events=activity_step({},value,10,threshold_seconds=600)
        self.assertEqual('UNVERIFIABLE',facts['state'])

    def test_blocked_before_start_is_not_orphan_executed_tool(self):
        result = collector.record_tool_end(self.home, self.scope, 'blocked', 100, 'BLOCKED')
        self.assertEqual('BLOCKED_BEFORE_EXECUTION', result['disposition'])
        value = collector.read_activity(self.home, self.scope, 110)
        self.assertTrue(value['sources'][0]['complete'])
        self.assertEqual([], value['lifecycles'])
        self.assertEqual('BLOCKED', value['terminal_reports'][0]['outcome'])
        collector.record_tool_end(self.home, self.scope, 'orphan', 120, 'SUCCESS')
        self.assertIn('TOOL_START_RECEIPT_MISSING',
                      collector.read_activity(self.home, self.scope, 130)['sources'][0]['reason_codes'])

    def test_native_blocked_hook_preserves_no_start_disposition(self):
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location('open_activity_plugin', root/'__init__.py',
            submodule_search_locations=[str(root)])
        module = importlib.util.module_from_spec(spec); sys.modules[spec.name] = module; spec.loader.exec_module(module)
        plugin = module.RuntimePlugin(home=self.home, registry=self.home/'registry.json', metrics_url='unused')
        with patch.object(plugin, '_frozen_scope', return_value={**self.scope, 'kind':'business', 'workflow_version':1}), \
             patch.object(module.time, 'time', return_value=100):
            result = plugin.on_tool_end(session_id='session', tool_call_id='policy-blocked', status='blocked')
        self.assertEqual('BLOCKED_BEFORE_EXECUTION', result['disposition'])

    def test_concurrent_tool_and_model_receipts_have_no_loss(self):
        def operation(i):
            return [collector.record_tool_start(self.home,self.scope,str(i),'read_file',100),
                collector.record_model_start(self.home,self.scope,str(i),'request-'+str(i),100),
                collector.record_tool_end(self.home,self.scope,str(i),101,'SUCCESS'),
                collector.record_model_end(self.home,self.scope,str(i),'request-'+str(i),101)]
        with ThreadPoolExecutor(max_workers=16) as executor:
            rows = list(executor.map(operation, range(64)))
        self.assertTrue(all(r['state']=='RECORDED' for row in rows for r in row), rows)
        value = self.observation(110)
        self.assertTrue(all(s['complete'] for s in value['sources']))
        self.assertEqual(128, len(value['lifecycles']))
        self.assertTrue(all(row['ended_at']==101 for row in value['lifecycles']))

    def test_separate_processes_share_receipt_lock(self):
        program = '''import json,sys
from pathlib import Path
import workflow_activity as c
h=Path(sys.argv[1]);s=json.loads(sys.argv[2]);prefix=sys.argv[3]
for i in range(12):
 t=prefix+str(i)
 assert c.record_tool_start(h,s,t,'read_file',100)['state']=='RECORDED'
 assert c.record_tool_end(h,s,t,101,'SUCCESS')['state']=='RECORDED'
'''
        processes = [subprocess.Popen([sys.executable,'-c',program,str(self.home),json.dumps(self.scope),str(i)+'-'],
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for i in range(4)]
        try:
            for process in processes:
                output,error = process.communicate(timeout=20)
                self.assertEqual(0,process.returncode,error)
        finally:
            for process in processes:
                if process.poll() is None: process.kill()
                process.wait()
        value = collector.read_activity(self.home,self.scope,110)
        self.assertTrue(value['sources'][0]['complete'])
        self.assertEqual(48,len(value['lifecycles']))

    def test_false_terminal_stays_unknown_even_with_short_open(self):
        collector.record_model_start(self.home,self.scope,'p','logical',100)
        collector.record_model_end(self.home,self.scope,'p','logical',101,end_verified=False)
        facts,events = activity_step({},self.observation(110),110,threshold_seconds=60)
        self.assertEqual('UNVERIFIABLE',facts['state'])
        self.assertNotIn('NO_ACTIVITY',[e['type'] for e in events])

    def test_stale_source_and_missing_policy_are_not_hidden_by_in_flight(self):
        collector.record_model_start(self.home,self.scope,'p','logical',100)
        value = self.observation(110);value['sources'][1]['last_verified_at']=0
        facts,events = activity_step({},value,200,threshold_seconds=60)
        self.assertEqual('UNVERIFIABLE',facts['state'])
        value=self.observation(110);value['lifecycles'][0].pop('observation_grace_seconds')
        facts,events=activity_step({},value,110,threshold_seconds=60)
        self.assertEqual('UNVERIFIABLE',facts['state'])
