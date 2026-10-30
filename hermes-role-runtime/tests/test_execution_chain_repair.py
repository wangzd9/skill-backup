"""Regressions for the live execution-chain incident; no production effects."""
import importlib.util
import json
import os
from pathlib import Path
import time
import unittest
from unittest import mock

from test_workflow_integration import WorkflowIntegrationTests
from test_workflow_dispatch import WakeupTests
from test_business_ledger import LedgerFixture
import workflow_runtime as workflow
import workflow_sessions as sessions
import workflow_gateway_wakeup as gateway_wakeup


class BridgeRepairTests(WorkflowIntegrationTests):
    def test_bridge_checkpoint_passes_same_role_and_session_fences(self):
        import sys
        root=Path(__file__).resolve().parents[1]
        spec=importlib.util.spec_from_file_location('execution_chain_plugin',root/'__init__.py',
                                                   submodule_search_locations=[str(root)])
        module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
        plugin=module.RuntimePlugin(registry=self.home/'registry.json',home=self.home,metrics_url='unused')
        worker,identity=self.bind('COORDINATOR')
        scope=self.scope(identity)
        sessions.observe(self.home,scope,api_call_count=50)
        with mock.patch.object(plugin,'bind_manual_turn',return_value={'role':'COORDINATOR'}), \
             mock.patch.object(plugin,'_frozen_scope',return_value=scope):
            args={'action':'checkpoint','payload':{'checkpoint':{'next':'wait for explicit OCR renewal'},
                  'yield_execution':True},'command_id':'bridge-yield'}
            # Registry tool discovery is a production precondition of the bridge.
            from tools.registry import registry
            with mock.patch.object(registry,'get_entry',return_value=type('Entry',(),{'toolset':'role_coordination'})()):
                decision=plugin.on_tool(tool_name='tool_call',args={'calls':[
                    {'name':'role_coordination_command','arguments':args}]},session_id='native')
            self.assertIsNone(decision)
            result=workflow.command(scope,**args)
            self.assertCommitted(result)
            self.assertEqual('QUEUED',self.ledger.current_work('COORDINATOR')['state'])
            self.assertFalse(self.ledger.admission_hint('COORDINATOR')['can_start_model'])
            self.assertEqual('block',plugin.on_tool(tool_name='tool_call',args={'calls':[
                {'name':'role_work_input','arguments':{}},{'name':'role_coordination_command','arguments':args}]},
                session_id='native')['action'])


class ReworkRepairTests(LedgerFixture):
    def test_idle_work_has_no_activity_alarm_but_unknown_writer_still_requires_evidence(self):
        import role_observer
        dev_process,dev=self.bind('A')
        candidate,_=self.candidate(dev);self.close(dev_process,dev)
        tester_process,tester=self.bind('TESTER')
        self.assertCommitted(self.ledger.checkpoint(tester,{'waiting_external':['OCR']},
                              yield_execution=True,command_id='idle'))
        self.close(tester_process,tester)
        facts=self.ledger.snapshot()
        with mock.patch('workflow_activity.read_activity',side_effect=AssertionError('idle is not execution activity')):
            value=role_observer.workflow_activities(self.path,self.root/'pool.json',self.root,facts,self.now)
        self.assertEqual([],value['activities'])
        # Missing execution evidence alongside an asserted writer remains UNKNOWN.
        next(row for row in facts['roles'] if row['role']=='TESTER')['writer_execution_id']='missing'
        value=role_observer.workflow_activities(self.path,self.root/'pool.json',self.root,facts,self.now)
        self.assertEqual(1,len(value['activities']))
        self.assertTrue(any(not source['complete'] for source in value['activities'][0]['sources']))

    def test_explicit_rework_retires_idle_verification_but_never_live_writer(self):
        dev_process,dev=self.bind('A')
        candidate,_=self.candidate(dev);self.close(dev_process,dev)
        tester_process,tester=self.bind('TESTER')
        coord_process,coord=self.bind('COORDINATOR')
        self.assertCommitted(self.ledger.checkpoint(tester,{'next':'wait for OCR'},
                              yield_execution=True,command_id='wait'))
        rejected=self.ledger.decide(coord,candidate,'REWORK',command_id='live-rework')
        self.assertEqual('WAITING_FOR_VERIFICATION_SAFE_EXIT',rejected['state'])
        self.assertFalse(rejected['requires_user_approval'])
        self.close(tester_process,tester)
        result=self.assertCommitted(self.ledger.decide(coord,candidate,'REWORK',
            evidence={'reason':'explicit fixed-candidate repair decision'},command_id='idle-rework'))
        self.assertEqual([tester['work_id']],result['retired_verification_work_ids'])
        self.assertIsNone(self.ledger.current_work('TESTER'))
        self.assertEqual([],self.ledger.snapshot()['verifications'])
        dev2_process,dev2=self.bind('A')
        self.assertEqual(2,self.ledger.current_work('A')['attempt'])


class GatewayForwardingTests(WakeupTests):
    def test_worker_commit_is_forwarded_without_native_claim_then_gateway_consumes(self):
        before=self.ledger.snapshot()['event_high_water']
        marker={'at':time.time(),'monotonic_ns':time.monotonic_ns(),'event_id':before}
        with mock.patch.dict(os.environ,{'_HERMES_CRON_EXTERNAL_WORKER':'offline-owner'}), \
             mock.patch('workflow_dispatch.observer_binding',side_effect=AssertionError('worker must not claim')):
            result=workflow.request_role_wakeup(self.pool_path,'A',home=self.home,
                                               source='business_commit',committed_at=marker)
        self.assertEqual('PENDING_GATEWAY',result['state'])
        self.assertEqual(before,self.ledger.snapshot()['event_high_water'])
        value=gateway_wakeup.pending(self.home,self.pool_path,'A')
        self.assertEqual(marker,{key:value['first_commit'][key] for key in marker})
        self.assertEqual(os.getpid(),value['first_commit']['origin_process']['pid'])
        with self.native_body(lambda job,**kw:True),mock.patch.dict(os.environ):
            os.environ.pop('_HERMES_CRON_EXTERNAL_WORKER',None)
            result=workflow.request_role_wakeup(self.pool_path,'A',home=self.home)
            self.assertEqual('SUBMITTED',result['state'],result)
            self.wait_idle()
        self.assertIsNone(gateway_wakeup.pending(self.home,self.pool_path,'A'))
    def test_new_commit_is_not_erased_by_old_ack_and_other_profile_is_isolated(self):
        first=gateway_wakeup.submit(self.home,self.pool_path,'A',{'event_id':1,'at':1,'monotonic_ns':1})
        second=gateway_wakeup.submit(self.home,self.pool_path,'A',{'event_id':2,'at':2,'monotonic_ns':2})
        gateway_wakeup.acknowledge(self.home,self.pool_path,'A',first['request_id'])
        value=gateway_wakeup.pending(self.home,self.pool_path,'A')
        self.assertEqual((1,2),(value['first_commit']['event_id'],value['latest_commit']['event_id']))
        self.assertIsNone(gateway_wakeup.pending(self.home/'other',self.pool_path,'A'))
        gateway_wakeup.acknowledge(self.home,self.pool_path,'A',second['request_id'])
        self.assertIsNone(gateway_wakeup.pending(self.home,self.pool_path,'A'))


def load_tests(loader, standard, pattern):
    # Shared fixtures are inherited; count/run each regression only once.
    suite=unittest.TestSuite()
    for cls in (BridgeRepairTests,ReworkRepairTests,GatewayForwardingTests):
        for name in cls.__dict__:
            if name.startswith('test_'):
                suite.addTest(cls(name))
    return suite
