"""The model sees the real command fields after native schema sanitization."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, '/home/wang/.hermes/hermes-agent')
from tools.schema_sanitizer import sanitize_tool_schemas
from tools.tool_search import dispatch_tool_describe
from tools.registry import ToolRegistry
from jsonschema import Draft202012Validator
from workflow_contracts import command_schema, validate_payload


class NativeCommandSchemaTests(unittest.TestCase):
    def test_all_three_role_commands_remain_discoverable_after_native_sanitizer(self):
        for name, actions in (
            ('role_coordination_command', ['publish_plan','decide','checkpoint','safe_release',
                'service_submit','service_collect','service_decide','refresh_development_base','merge','append_development_log','commit_documentation','prepare_requirement','activate_requirement','resolve_user_decision']),
            ('role_development_command', ['deliver','checkpoint','safe_release']),
            ('role_test_command', ['verify','verify_supplemental','close_verification','checkpoint','safe_release'])):
            with self.subTest(name=name):
                raw={'type':'function','function':{'name':name,'description':'本单专用工具',
                    'parameters':command_schema(actions)}}
                sanitized=sanitize_tool_schemas([raw])
                registry=ToolRegistry()
                registry.register(name=name,toolset='role_workflow',schema=raw['function'],handler=lambda **kw: None)
                with patch('tools.registry.registry',registry):
                    described=json.loads(dispatch_tool_describe({'names':[name]},current_tool_defs=sanitized))
                params=described['tools'][name]['parameters']
                self.assertEqual({'action','payload','command_id'},set(params['properties']))
                self.assertEqual({'action','payload','command_id'},set(params['required']))
                self.assertEqual(actions,params['properties']['action']['enum'])
                self.assertNotIn('oneOf',params)
                self.assertEqual(actions,[s['title'] for s in params['properties']['payload']['oneOf']])
                example={'action':'checkpoint','payload':{'checkpoint':{'waiting_external':['real dependency']},
                    'processed_through':12},'command_id':'checkpoint-1'}
                Draft202012Validator(params).validate(example)
                self.assertTrue(list(Draft202012Validator(params).iter_errors({})))

    def test_original_action_payload_pairing_remains_strict(self):
        schema=command_schema(['checkpoint','safe_release'])
        invalid={'action':'checkpoint','payload':{'reason':'wrong action payload'},'command_id':'x'}
        self.assertTrue(list(Draft202012Validator(schema).iter_errors(invalid)))
        with self.assertRaisesRegex(ValueError,'INVALID_COMMAND_PAYLOAD'):
            validate_payload(invalid['action'],invalid['payload'])

    def test_staged_plan_schema_carries_provider_contract_and_risk_evidence(self):
        provider={'task_id':'T1','role':'A','scope':['provider.py'],'base':'a'*40,
            'ac_version':'ac1','provided_contracts':[{'contract_id':'T1-api-v1','version':'v1',
                'interface':{'function':'load()->Result'}}]}
        consumer={'task_id':'T2','role':'B','scope':['consumer.py'],'base':'a'*40,
            'ac_version':'ac1','dependencies':['T1'],
            'stage_dependencies':{'development_start':[],'integration':['T1'],'acceptance':['T1']},
            'dependency_contracts':[{'task_id':'T1','contract_id':'T1-api-v1','version':'v1',
                'interface':{'function':'load()->Result'},'simulation':{'module':'stub'}}],
            'minimum_reading_set':['API contract v1'],'risk_scenarios':[{'id':'R1',
                'scenario':'provider changes its result shape','invariant':'consumer rejects invalid result',
                'verification':'contract test'}],
            'test_environment':{'python':'3.11','command':'python -m unittest'},
            'escalation_conditions':['provider contract version changes'],
            'ac':['consumer preserves contract'],'acceptance':['fixed final candidate passes independent contract checks'],
            'inputs':['provider v1 interface'],'stop_conditions':['provider interface changes']}
        payload={'version':2,'tasks':[provider,consumer]}
        validate_payload('publish_plan',payload)

    def test_staged_missing_acceptance_and_dependency_errors_report_together_before_effects(self):
        task={'task_id':'T2','role':'B','scope':['consumer.py'],'base':'a'*40,'ac_version':'ac1',
            'dependencies':['T1'],'stage_dependencies':{'development_start':['T1'],'integration':[],'acceptance':['T3']}}
        with self.assertRaisesRegex(ValueError,'INVALID_COMMAND_PAYLOAD') as caught:
            validate_payload('publish_plan',{'version':1,'tasks':[task]})
        errors=json.loads(str(caught.exception).split(': ',1)[1])
        self.assertTrue(any('acceptance' in e['message'] for e in errors))
        self.assertTrue(any('STAGED_DEPENDENCY_GATES_NOT_MONOTONIC' in e['message'] for e in errors))
        self.assertTrue(any('STAGED_DEPENDENCIES_MUST_COVER_DEPENDENCIES' in e['message'] for e in errors))
        advertised=sanitize_tool_schemas([{'type':'function','function':{'name':'role_coordination_command',
            'description':'role command','parameters':command_schema(['publish_plan'])}}])[0]['function']['parameters']
        self.assertTrue(list(Draft202012Validator(advertised).iter_errors({
            'action':'publish_plan','command_id':'new-plan','payload':{'version':1,'tasks':[task]}})))
        validate_payload('publish_plan',{'version':1,'tasks':[{k:v for k,v in task.items() if k not in ('stage_dependencies','dependencies')}]})

    def test_staged_late_dependency_needs_matching_provider_contract(self):
        from workflow_contracts import plan_contract_errors
        provider={'task_id':'T1','provided_contracts':[{'contract_id':'api','version':'v1','interface':{'method':'load'}}]}
        consumer={'task_id':'T2','dependencies':['T1'],
            'stage_dependencies':{'development_start':[],'integration':['T1'],'acceptance':['T1']}}
        self.assertIn('PRESTART_DEPENDENCY_CONTRACT_REQUIRED',plan_contract_errors([provider,consumer])[0]['message'])
        consumer['dependency_contracts']=[{'task_id':'T1','contract_id':'api','version':'v1','interface':{'method':'load'},'simulation':{'module':'stub'}}]
        self.assertEqual([],plan_contract_errors([provider,consumer]))
        consumer['dependency_contracts'][0]['version']='v2'
        self.assertIn('DEPENDENCY_INTERFACE_CONTRACT_MISMATCH',plan_contract_errors([provider,consumer])[0]['message'])

    def test_rejected_transaction_and_unstarted_validation_have_distinct_id_recovery(self):
        from workflow_recovery import guidance
        scope={'role':'COORDINATOR'}
        rejected={'status':'REJECTED','code':'STAGED_TASK_CONTRACT_INCOMPLETE',
            'decision':{'decision_phase':'TRANSACTION_GATE','side_effect_state':'NONE'}}
        frozen=guidance(scope,rejected,action='publish_plan')
        self.assertTrue(frozen['new_command_id_required'])
        self.assertFalse(frozen['retryable'])
        invalid=guidance(scope,{'status':'REJECTED','code':'INVALID_COMMAND_PAYLOAD',
            'phase':'INVALID_NOT_STARTED'},action='publish_plan')
        self.assertFalse(invalid['new_command_id_required'])
        self.assertTrue(invalid['retryable'])


if __name__ == '__main__':unittest.main()
