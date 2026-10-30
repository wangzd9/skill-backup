"""Isolated regressions for rejected review and exception follow-up liveness."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

import workflow_decision_context as context
import workflow_sessions as sessions
from workflow_service_recovery import coverage_recovery, coverage_blocker
from workflow_verification_exceptions import repair_followup
from test_semantic_decision import synthetic


class RecoveryTests(unittest.TestCase):
    def test_readonly_loop_must_yield_at_threshold_without_ack_or_owner_change(self):
        with tempfile.TemporaryDirectory() as root:
            scope={'kind':'business','workflow_version':1,'role':'COORDINATOR','execution_id':'e','work_id':'w'}
            self.assertFalse(sessions.observe(root,scope,approx_input_tokens=127999)['checkpoint_required'])
            self.assertIsNone(sessions.tool_decision(root,scope,'read_file',{}))
            sessions.observe(root,scope,approx_input_tokens=128000)
            for tool in ('read_file','search_files','role_work_input','role_decision_context','role_coordination_command'):
                self.assertEqual('SESSION_CHECKPOINT_REQUIRED',sessions.tool_decision(root,scope,tool,{})['reason'])
            for action,payload in [('checkpoint',{'checkpoint':{'next':'actual unresolved gates'},'yield_execution':True}),
                                   ('safe_release',{'reason':'controlled retirement'})]:
                self.assertIsNone(sessions.tool_decision(root,scope,'role_coordination_command',{'action':action,'payload':payload}))
            self.assertIsNone(sessions.tool_decision(root,{**scope,'kind':'report'},'read_file',{}))

    def test_guidance_is_not_a_command_and_points_to_actual_repair(self):
        p,s=synthetic()
        p['next_actions']=[{'action':'follow_verification_repair','observation_event_id':42,
            'repair_followup':{'candidate_id':'c','phase':'REVIEW','owner':'COORDINATOR'}},
            {'action':'service_submit','candidate_id':'c','kind':'ocr'}]
        rows=context.directory(p,s)['items']
        self.assertEqual('guidance',rows[0]['execution_kind'])
        self.assertEqual('command',rows[1]['execution_kind'])
        self.assertEqual([rows[1]['decision_id']],rows[0]['related_decision_ids'])
        self.assertFalse(context._contract('COORDINATOR','follow_verification_repair')['executable'])
        self.assertEqual('c',context.prepare(p,s,rows[0])['facts']['repair_followup']['candidate_id'])
        with self.assertRaisesRegex(ValueError,'NOT_ALLOWED'):
            context._contract('TESTER','follow_verification_repair')
        from workflow_contracts import validate_payload
        with self.assertRaises(ValueError):validate_payload('follow_verification_repair',{})

    def refusal(self,root,code='OCR_REVIEW_COVERAGE_UNAVAILABLE'):
        candidate={'candidate_id':'c','run_id':'r','task_id':'t','attempt':8,'repo':'/isolated','base':'a','head':'b'}
        action={'kind':'ocr','state':'REJECTED','action_id':'refused','candidate_id':'c'}
        folder=Path(root)/'services'/hashlib.sha256(b'refused').hexdigest();folder.mkdir(parents=True)
        value={'action_id':'refused','candidate_id':'c','state':'REJECTED','error':'PreDispatchRejected',
            'frozen':{'candidate':candidate},'request':{'background_text':'Original accepted AC and independent PASS evidence.'},
            'preflight_rejection':{'schema':'ocr.preflight-rejection/v1','stage':'preflight','operation':'prepare',
                'side_effects':0,'dispatch_performed':False,'code':code}}
        (folder/'invocation.json').write_text(json.dumps(value))
        (folder/'background.md').write_text(value['request']['background_text'])
        return types.SimpleNamespace(path=Path(root)/'business.sqlite3'),candidate,[action],folder,value

    def test_repaired_preview_unsticks_old_and_current_rejection_without_dispatch(self):
        for code in ('NO_REVIEWABLE_FILES_OR_DUPLICATES','OCR_REVIEW_COVERAGE_UNAVAILABLE'):
            with tempfile.TemporaryDirectory() as root:
                ledger,candidate,actions,folder,value=self.refusal(root,code)
                empty={'files':[{'path':'tests/test_fixture.py','will_review':False,'exclude_reason':'default_path'}]}
                fixed={'files':[{'path':'tests/test_fixture.py','will_review':True}]}
                with patch('ocr_runtime.preview',side_effect=[empty,fixed]) as preview, \
                     patch('workflow_ocr_selection.freeze',return_value={'path':'/temporary-rule','sha256':'f'*64,'test_files':['tests/test_fixture.py']}), \
                     patch('workflow_services.NativeServices.submit',side_effect=AssertionError('read-only query must not dispatch')):
                    result=coverage_recovery(ledger,candidate,actions)
                self.assertEqual('READY',result['status']);self.assertEqual(2,preview.call_count)
                self.assertFalse(result['gate_satisfied']);self.assertFalse(result['dispatch_performed'])
                self.assertNotIn('path',result['current_preview']['selection'])
                self.assertEqual('tests/test_fixture.py',result['current_preview']['files'][0]['repository_path'])
                self.assertNotIn('path',result['current_preview']['files'][0])
                # Exercise the real decision material walk: metadata must not
                # become a false unhashed evidence source; bad evidence must
                # still fail closed.
                p,s=synthetic()
                p['next_actions']=[{'action':'service_submit','candidate_id':'c','kind':'ocr',
                    'rejection_recovery':result}]
                material=context.prepare(p,s,context.directory(p,s)['items'][0])
                self.assertEqual('FOUND',material['source_status'])
                self.assertEqual([str(folder/'invocation.json')],[row['path'] for row in material['source_checks']])
                p['candidates'][0]['evidence']=[{'path':str(folder/'unhashed-proof')}]
                material=context.prepare(p,s,context.directory(p,s)['items'][0])
                self.assertEqual('UNVERIFIED',material['source_status'])
                self.assertIn('IMMUTABLE_HASH_REQUIRED',[row['reason'] for row in material['source_checks']])
                self.assertEqual(value,json.loads((folder/'invocation.json').read_text()))

    def test_empty_preview_still_blocks_and_dispatch_proof_never_allows_retry(self):
        with tempfile.TemporaryDirectory() as root:
            ledger,candidate,actions,folder,value=self.refusal(root)
            with patch('ocr_runtime.preview',return_value={'files':[]}),patch('workflow_ocr_selection.freeze',return_value=None):
                self.assertEqual('BLOCKED',coverage_blocker(ledger,candidate,actions)['status'])
            (folder/'native').mkdir();(folder/'native/dispatch-intent.json').write_text('{}')
            with patch('ocr_runtime.preview',side_effect=AssertionError('unknown effect must not be previewed for retry')):
                self.assertEqual('OCR_REJECTION_PROOF_UNVERIFIED',coverage_blocker(ledger,candidate,actions)['code'])

    def test_changed_background_and_missing_test_coverage_stay_unverified(self):
        with tempfile.TemporaryDirectory() as root:
            ledger,candidate,actions,folder,value=self.refusal(root)
            (folder/'background.md').write_text('changed')
            with patch('ocr_runtime.preview',side_effect=AssertionError('do not preview changed evidence')):
                self.assertEqual('UNVERIFIED',coverage_blocker(ledger,candidate,actions)['status'])

    def test_preview_timeout_is_diagnostic_instead_of_breaking_current_input(self):
        import subprocess
        with tempfile.TemporaryDirectory() as root:
            ledger,candidate,actions,folder,value=self.refusal(root)
            with patch('ocr_runtime.preview',side_effect=subprocess.TimeoutExpired('ocr',30)):
                result=coverage_blocker(ledger,candidate,actions)
            self.assertEqual('UNVERIFIED',result['status'])
            self.assertEqual('TimeoutExpired',result['preview_error']['error'])

    def test_followup_tracks_current_candidate_and_original_mysql_lineage(self):
        item={'repair':{'task_id':'t','attempt':8},'disposition':{'run_id':'r'},'request_event_id':10}
        s={'tasks':[{'task_id':'t','run_id':'r','attempt':8,'phase':'REVIEW','role':'A'}],
            'candidates':[{'task_id':'t','run_id':'r','attempt':8,'state':'CURRENT','candidate_id':'a8','head':'b'}],'events':[]}
        before=copy.deepcopy(s)
        self.assertEqual(('a8','COORDINATOR','REPAIR_REVIEW_REQUIRED'),tuple(repair_followup(s,item)[k] for k in ('candidate_id','owner','code')))
        self.assertEqual(before,s)
        s['tasks'][0]['phase']='COMPLETED'
        self.assertEqual('SUPPLEMENTAL_REVALIDATION_RECONCILE_REQUIRED',repair_followup(s,item)['code'])
        s['events']=[{'kind':'SUPPLEMENTAL_REVALIDATION_QUEUED','work_id':'mysql','payload':{'candidate_id':'a8','original_request_event_id':10}}]
        self.assertEqual(('TESTER','mysql'),tuple(repair_followup(s,item)[k] for k in ('owner','work_id')))

    def test_second_rework_requires_native_lineage_but_can_still_close_exception(self):
        from workflow_verification_exceptions import repair_descendant
        item={'repair':{'task_id':'t','attempt':8}}
        base={'task_id':'t','run_id':'r','attempt':8,'candidate_id':'a8'}
        later={**base,'attempt':10,'candidate_id':'a10'}
        s={'candidates':[base,{**base,'attempt':9,'candidate_id':'a9'}],
            'attempts':[{'run_id':'r','task_id':'t','attempt':9,'previous_candidate_id':'a8'},
                {'run_id':'r','task_id':'t','attempt':10,'previous_candidate_id':'a9'}]}
        self.assertTrue(repair_descendant(s,item,later))
        s['attempts'][1]['previous_candidate_id']='unrelated'
        self.assertFalse(repair_descendant(s,item,later))
        self.assertFalse(repair_descendant(s,item,{**later,'task_id':'other'}))
        self.assertFalse(repair_descendant(s,item,{**later,'run_id':'other'}))


class CompletionDiagnosticTests(unittest.TestCase):
    def test_same_guard_exposes_actual_stale_target_reason_and_owner(self):
        from workflow_completion import refresh_status
        s={'current_run_id':'r','tasks':[{'run_id':'r','task_id':'t','attempt':8,'phase':'REVIEW','role':'A'}]}
        before=copy.deepcopy(s)
        with patch('workflow_completion.frozen_target',side_effect=ValueError('COMPLETION_DEVELOPMENT_NOT_COMPLETE')):
            value=refresh_status(None,s,{'version':1})
        self.assertFalse(value['ready']);self.assertEqual('COMPLETION_DEVELOPMENT_NOT_COMPLETE',value['code'])
        self.assertEqual('t',value['unfinished_suppliers'][0]['task_id']);self.assertEqual(before,s)
        with patch('workflow_completion.frozen_target',return_value={'head':'new'}):
            self.assertEqual('new',refresh_status(None,s,{'version':1})['target']['head'])
