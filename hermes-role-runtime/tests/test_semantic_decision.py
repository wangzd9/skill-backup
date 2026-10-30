"""Isolated decision preparation/ACK regressions; no provider or live business calls."""
import ast
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from test_business_ledger import LedgerFixture
import workflow_decision_context as context
import workflow_runtime as workflow
import workflow_work_memory as memory


def synthetic():
    packet={'generation':1,'checked_at':'2026-10-30T17:00:00+08:00','role':'COORDINATOR',
        'run':{'run_id':'r','repo':'/isolated'},'work':{'work_id':'w','run_id':'r'},
        'tasks':[{'run_id':'r','task_id':'t','phase':'REVIEW','attempt':1}],
        'candidates':[{'candidate_id':'c','run_id':'r','task_id':'t','attempt':1,'head':'a'*40,'ac_version':'v1','state':'CURRENT'}],
        'external_actions':[{'action_id':'a','candidate_id':'c','kind':'ocr','state':'RESULT_READY'}],
        'verifications':[{'verification_id':s,'candidate_id':'c','check_name':'independent_test','result':s,'supersedes':[]} for s in ('FAIL','UNKNOWN','UNVERIFIED')],
        'next_actions':[{'action':'service_decide','action_id':'a','reason':'read original report'}],
        'completion':{},'controls':[],'historical_failures':[{'verification_id':'FAIL'}]}
    snapshot={'generation':1,'event_high_water':2,'events':[
        {'event_id':1,'run_id':'r','kind':'USER_DECISION_RECORDED','payload':{'request_id':'u','answer':'B'}},
        {'event_id':2,'run_id':'other','kind':'ISOLATED_EVENT','payload':{}}],
        'authorizations':[],'decisions':[],'candidates':packet['candidates']}
    return packet,snapshot


class PreparationTests(unittest.TestCase):
    def test_directory_enumerates_all_adverse_results_answers_and_stable_ids(self):
        p,s=synthetic();first=context.directory(p,s)
        self.assertEqual(['FAIL','UNKNOWN','UNVERIFIED'],[v['result'] for v in first['blocker_index']['verification_results']])
        self.assertEqual('u',first['blocker_index']['pending_user_answers'][0]['request_id'])
        s['event_high_water']=999
        s['events'].append({'event_id':999,'run_id':'r','kind':'CHECKPOINT_SAVED','payload':{}})
        self.assertEqual(first,context.directory(p,s)|{'sampling':first['sampling']})
        p['candidates'][0]['head']='b'*40
        changed=context.directory(p,s)
        self.assertEqual(first['items'][0]['decision_id'],changed['items'][0]['decision_id'])
        self.assertNotEqual(first['items'][0]['material_fingerprint'],changed['items'][0]['material_fingerprint'])

    def test_targets_include_nested_candidate_and_duplicate_is_unambiguous(self):
        p,s=synthetic();p['next_actions']=[{'action':'decide','payload':{'candidate_id':c}} for c in ('one','two','one')]
        rows=context.directory(p,s)['items']
        self.assertEqual(2,len(rows));self.assertNotEqual(rows[0]['decision_id'],rows[1]['decision_id'])

    def test_prepare_does_not_fill_verdict_or_failure_closure(self):
        p,s=synthetic();item=context.directory(p,s)['items'][0]
        result=context.prepare(p,s,item)
        self.assertEqual({'action_id':'a'},result['payload_draft'])
        self.assertNotIn('result',result['payload_draft'])
        self.assertNotIn('supersedes',result['payload_draft'])
        self.assertEqual('service_decide',result['contract']['action'])

    def test_final_review_rows_preserve_actual_coverage_but_no_verdict(self):
        p,s=synthetic();coverage=[{'assertion':'real assertion','case_id':'case'}]
        p['completion']={'plan':{'obligations':[{'id':'AC:closure','ac_id':'AC','phase':'local_closure','scenario':'S'}]},
            'report':{'event_id':11,'payload':{'checks':[{'id':'AC','coverage':coverage}]}}}
        p['next_actions']=[{'action':'complete_requirement'}]
        row=context.prepare(p,s,context.directory(p,s)['items'][0])['semantic_review_rows'][0]
        self.assertEqual(('AC:closure',11,coverage,None,None),tuple(row[k] for k in
            ('obligation_id','validation_event_id','actual_evidence','result','reason')))

    def test_contract_is_same_schema_and_role_boundary(self):
        from workflow_contracts import payload_schema
        self.assertEqual(payload_schema('verify'),context._contract('TESTER','verify')['payload_schema'])
        with self.assertRaisesRegex(ValueError,'NOT_ALLOWED'):context._contract('TESTER','merge')

    def test_bound_objects_do_not_return_other_candidate_or_run(self):
        p,s=synthetic();s['candidates'].append({'candidate_id':'foreign','run_id':'other'})
        p['candidates']=p['candidates'][:1]
        objects=context._objects(p,s)
        self.assertNotIn('foreign',objects['candidate']);self.assertNotIn('2',objects['event'])

    def test_oversize_retains_status_pagination_and_exact_content(self):
        with tempfile.TemporaryDirectory() as root:
            ledger=types.SimpleNamespace(path=Path(root)/'ledger.sqlite3')
            original={'status':'CHANGED','items_total':42,'next_offset':16,'fingerprint':'x'*64,'text':'中'*40000}
            result=context._bounded(ledger,original)
            self.assertEqual(('CHANGED',42,16),tuple(result[k] for k in ('status','items_total','next_offset')))
            ref=result['full_material'];raw=Path(ref['path']).read_bytes()
            self.assertEqual(ref['sha256'],hashlib.sha256(raw).hexdigest());self.assertEqual(original,json.loads(raw))

    def test_source_statuses_regular_file_only_streamed_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/'proof';p.write_bytes(b'original')
            ref={'path':str(p),'sha256':hashlib.sha256(b'original').hexdigest()}
            self.assertEqual('FOUND',context._source_checks([ref,ref])[0]['status'])
            self.assertEqual(1,len(context._source_checks([ref,ref])))
            p.write_bytes(b'changed');self.assertEqual('CHANGED',context._source_checks(ref)[0]['status'])
            p.unlink();self.assertEqual('MISSING',context._source_checks(ref)[0]['status'])
            os.mkfifo(p);self.assertEqual('UNVERIFIED',context._source_checks(ref)[0]['status'])
            self.assertFalse(context._source_checks(ref)[0]['content_read_by_model'])

    def test_source_replaced_while_hashing_is_changed(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'proof';path.write_text('original')
            ref={'path':str(path),'sha256':hashlib.sha256(b'original').hexdigest()}
            original=hashlib.sha256
            class MutatingHasher:
                def __init__(self):self.hasher=original()
                def update(self,chunk):
                    self.hasher.update(chunk)
                    replacement=path.with_name('replacement');replacement.write_text('modified')
                    os.replace(replacement,path)
                def hexdigest(self):return self.hasher.hexdigest()
            with patch.object(context.hashlib,'sha256',side_effect=MutatingHasher):checks=context._source_checks(ref)
            self.assertEqual('CHANGED',checks[0]['status']);self.assertEqual('SOURCE_CHANGED_DURING_READ',checks[0]['reason'])

    def test_frozen_reference_hashes_frozen_bytes(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'frozen';path.write_text('frozen')
            check=context._source_checks({'path':'/original/changed','frozen_path':str(path),
                'sha256':hashlib.sha256(b'frozen').hexdigest()})[0]
            self.assertEqual('FOUND',check['status']);self.assertEqual(str(path),check['path'])

    def test_malformed_query_is_concise_and_does_not_call_ledger(self):
        for args in (None,{'operation':[]},{'operation':'evidence','extra':True}):
            result=context.query({'kind':'business'},args)
            self.assertEqual('REJECTED',result['status']);self.assertFalse(result['can_ack'])

    def test_memory_rejects_invalid_dependency_types_hashes_and_event_lists(self):
        base={'version':1,'entries':[{'id':'m','conclusion':'historical','dependencies':{}}]}
        for deps in ({'candidate_id':[]},{'sources':[{'path':'p','sha256':'z'*64}]},{'head':False}):
            bad=copy.deepcopy(base);bad['entries'][0]['dependencies']=deps
            with self.assertRaises(ValueError):memory.validate(bad)
        for refs in ([True],[1,1],list(range(1,70))):
            bad=copy.deepcopy(base);bad['entries'][0]['source_event_ids']=refs
            with self.assertRaises(ValueError):memory.validate(bad)

    def test_memory_never_certifies_a_copied_hash_as_read_or_current(self):
        p,s=synthetic();s['events']=[]
        work={'role':'COORDINATOR','run_id':'r','checkpoint':{'work_memory':{'version':1,'entries':[
            {'id':'m','conclusion':'old conclusion','dependencies':{'candidate_id':'c','head':'a'*40,
            'sources':[{'path':'/not/registered','sha256':'0'*64}]}}]}}}
        with patch.object(context,'_source_checks',side_effect=AssertionError('unregistered source must not be read')):
            row=memory.inspect(s,work)['entries'][0]
        self.assertFalse(row['current_verified']);self.assertIn('READ_OR_DECISION_RECEIPT_UNVERIFIED',row['unverified_dependencies'])
        work['checkpoint']['work_memory']['entries'][0]['dependencies']['head']='b'*40
        self.assertIn('HEAD_CHANGED',memory.inspect(s,work)['entries'][0]['invalidations'])
        work['checkpoint']['work_memory']['entries']=[{'broken':True}]
        self.assertEqual('INVALID_LEGACY_MEMORY',memory.inspect(s,work)['status'])

    def test_short_recovery_keeps_unknown_and_diagnostic(self):
        from workflow_recovery import guidance
        result=guidance({'role':'COORDINATOR'},{'code':'UNKNOWN','decision':{'decision_id':'diag',
            'side_effect_state':'UNKNOWN'}},action='service_submit')
        self.assertFalse(result['retryable']);self.assertIsNone(result['invocation_started'])
        self.assertEqual('UNKNOWN',result['side_effect_state']);self.assertEqual('diag',result['diagnostic_id'])
        corrected=guidance({'role':'COORDINATOR'},{'code':'BAD_FIELD','phase':'INVALID_NOT_STARTED'})
        self.assertTrue(corrected['retryable']);self.assertFalse(corrected['new_command_id_required'])

    def test_request_metrics_keep_shared_physical_request_once_and_no_content(self):
        from workflow_decision_metrics import observe,request
        with tempfile.TemporaryDirectory() as root:
            ledger=types.SimpleNamespace(path=Path(root)/'ledger.sqlite3');p,s=synthetic()
            scope={'kind':'business','work_id':'w','execution_id':'e','role':'COORDINATOR','run_id':'r'}
            items=context.directory(p,s)['items']
            for item in items:observe(ledger,scope,'prepare',{'decision_id':item['decision_id'],'material':{}},items)
            meta={'physical_attempt_id':'physical','api_request_id':'logical','prompt':'do not save this'}
            request(ledger,scope,meta);request(ledger,scope,meta)
            paths=list((Path(root)/'decision-metrics/requests').glob('*.json'));self.assertEqual(1,len(paths))
            body=paths[0].read_text();self.assertNotIn('do not save this',body)
            self.assertEqual('shared_context_no_cost_split',json.loads(body)['attribution'])
            self.assertEqual(0o700,(Path(root)/'decision-metrics').stat().st_mode & 0o777)
            observe(ledger,scope,'directory',{},[])
            works=json.loads(next((Path(root)/'decision-metrics/works').glob('*.json')).read_text())
            self.assertEqual('NOT_IN_LATEST_DIRECTORY_NOT_A_VERDICT',next(iter(works['items'].values()))['state'])

    def test_activation_lists_new_helpers_and_real_plugin_boundaries(self):
        from mechanism_activation import TARGETS
        root=Path(__file__).resolve().parents[1]
        for name in ('workflow_decision_context.py','workflow_work_memory.py','workflow_decision_metrics.py','workflow_recovery.py','workflow_verification_exceptions.py'):
            functions={n.name for n in ast.parse((root/name).read_text()).body if isinstance(n,ast.FunctionDef)}
            self.assertEqual(functions,set(TARGETS[name]))
        self.assertIn('RuntimePlugin.work_input_tool',TARGETS['__init__.py'])
        self.assertIn('RuntimePlugin.decision_context_tool',TARGETS['__init__.py'])

    def test_native_tool_selection_and_runtime_read_allowlist_include_new_tool(self):
        from test_token_optimization import ToolDiscoveryTests
        from workflow_toolsets import configure
        self.assertIn('role_decision_context',workflow.READ_TOOLS)
        selection=ToolDiscoveryTests()
        for role in ('COORDINATOR','TESTER','A','B','C'):
            self.assertIn('role_decision_context',selection.selected(role))
            self.assertNotIn('role_decision_context',selection.selected(role,True))


class DecisionLifecycleTests(LedgerFixture):
    def setUp(self):
        super().setUp();self.pool=self.root/'pool.json';self.pool.write_text('{}')
        patcher=patch('workflow_runtime.open_ledger',return_value=self.ledger);patcher.start();self.addCleanup(patcher.stop)

    def scope(self,role='COORDINATOR'):
        _,identity=self.bind(role)
        return {**identity,'kind':'business','workflow_version':1,'pool_path':str(self.pool)}

    def test_two_ack_paths_count_equal_progress_including_same_timestamp(self):
        for mode in ('ack','checkpoint'):
            # Each role owns independent work; both share the fixed test clock.
            role='A' if mode=='ack' else 'COORDINATOR'
            for i in range(3):
                proc,identity=self.bind(role)
                page=self.assertCommitted(self.ledger.work_inputs(identity,command_id=mode+'page'+str(i)))
                if i==2:
                    if mode=='ack':self.assertCommitted(self.ledger.ack_events(identity,page['through'],command_id=mode))
                    else:self.assertCommitted(self.ledger.checkpoint(identity,{'next':'continue'},
                        processed_through=page['through'],command_id=mode))
                self.close(proc,identity)
            self.assertTrue(self.ledger.admission_hint(role)['can_start_model'])

    def test_issued_only_still_backs_off_and_new_result_wakes(self):
        for i in range(3):
            proc,identity=self.bind('A')
            self.ledger.work_inputs(identity,command_id='issued'+str(i));self.close(proc,identity)
        result=self.ledger.admission_hint('A')
        self.assertEqual('NO_PROGRESS_RESUMPTION_BACKOFF',result['reason'])
        self.assertEqual(0,result['processed_through']);self.assertGreater(result['issued_through'],0)
        with self.ledger._connection() as db:self.ledger._event(db,'ISOLATED_NEW_RESULT',{},run_id='run-one')
        self.assertTrue(self.ledger.admission_hint('A')['can_start_model'])

    def test_legacy_writer_fallback_resumes_after_checkpoint_progress(self):
        for i in range(3):
            proc,identity=self.bind('A');page=self.ledger.work_inputs(identity,command_id='legacy-page'+str(i))
            if i==2:self.ledger.checkpoint(identity,{},processed_through=page['through'],command_id='legacy-progress')
            self.close(proc,identity);self.now+=1
        with self.ledger._connection() as db:
            for row in db.execute('SELECT execution_id,writer FROM executions').fetchall():
                writer=json.loads(row['writer']);writer.pop('processed_through_at_bind',None)
                db.execute('UPDATE executions SET writer=? WHERE execution_id=?',(json.dumps(writer),row['execution_id']))
        self.assertTrue(self.ledger.admission_hint('A')['can_start_model'])

    def test_query_uses_snapshot_and_is_readonly_without_ack(self):
        scope=self.scope();before=self.ledger.snapshot('run-one')
        with patch.object(self.ledger,'events',side_effect=AssertionError('must reuse snapshot')):
            result=context.query(scope,{'operation':'directory'})
        self.assertEqual('FOUND',result['status']);self.assertFalse(result['can_ack'])
        self.assertEqual(before,self.ledger.snapshot('run-one'))
        self.assertEqual('one_read_transaction',result['sampling']['ledger_facts'])

    def test_query_rejects_stale_fingerprint(self):
        scope=self.scope()
        result=context.query(scope,{'operation':'directory','expected_fingerprint':'0'*64})
        self.assertEqual('CHANGED',result['status']);self.assertTrue(result['refresh_required'])

    def test_prepared_evidence_change_is_visible_in_outer_status(self):
        scope=self.scope();p,s=synthetic()
        p['external_actions'][0]['evidence']={'path':str(self.root/'missing-proof'),'sha256':'0'*64}
        item=context.directory(p,s)['items'][0]
        with patch.object(workflow,'current_input',return_value=p):
            result=context.query(scope,{'operation':'prepare','decision_id':item['decision_id']})
        self.assertEqual('MISSING',result['status']);self.assertEqual('MISSING',result['source_status'])
        self.assertTrue(result['refresh_required']);self.assertFalse(result['can_ack'])

    def test_metrics_fault_does_not_block_readonly_material(self):
        scope=self.scope()
        with patch('workflow_decision_metrics.observe',side_effect=RuntimeError('isolated telemetry fault')):
            result=context.query(scope,{'operation':'directory'})
        self.assertEqual('FOUND',result['status'])

    def plugin(self):
        root=Path(__file__).resolve().parents[1];name='isolated_semantic_plugin'
        spec=importlib.util.spec_from_file_location(name,root/'__init__.py',submodule_search_locations=[str(root)])
        module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module)
        return module.RuntimePlugin(registry=self.root/'registry',home=self.root,metrics_url='unused',pool_path=self.pool)

    def test_events_only_keeps_page_identity_and_never_builds_full_state(self):
        scope=self.scope();plugin=self.plugin()
        plugin_workflow=plugin.work_input_tool.__func__.__globals__['workflow']
        with patch.object(plugin,'_frozen_scope',return_value=scope),patch.object(plugin_workflow,'open_ledger',return_value=self.ledger),patch.object(plugin_workflow,'current_input',side_effect=AssertionError('full state requested')):
            first=json.loads(plugin.work_input_tool({'events_only':True},session_id='isolated'))
            again=json.loads(plugin.work_input_tool({'events_only':True,'through':first['frozen_through']},session_id='isolated'))
        self.assertEqual(first['events'],again['events']);self.assertEqual('DUPLICATE',again['status'])
        self.assertTrue(first['can_ack']);self.assertEqual(0,self.ledger.current_work('COORDINATOR')['processed_through'])

    def test_envelope_error_is_not_started_and_no_business_command(self):
        scope=self.scope();plugin=self.plugin()
        with patch.object(plugin,'_frozen_scope',return_value=scope),patch.object(workflow,'command',side_effect=AssertionError('invalid envelope executed')):
            result=json.loads(plugin.work_command_tool({'action':'merge','payload':[],'command_id':'id'},session_id='isolated'))
        self.assertEqual('INVALID_NOT_STARTED',result['phase']);self.assertFalse(result['recovery']['invocation_started'])

    def test_native_decision_handler_serializes_real_bound_query(self):
        scope=self.scope();plugin=self.plugin()
        with patch.object(plugin,'_frozen_scope',return_value=scope):
            result=json.loads(plugin.decision_context_tool({'operation':'contract','action':'publish_plan'},session_id='isolated'))
        self.assertEqual('publish_plan',result['action']);self.assertIn('tasks',result['payload_schema']['properties'])

    def test_log_auto_references_are_scoped_immutable_and_idempotent(self):
        from workflow_coordination_log import append
        scope=self.scope();event=self.ledger.snapshot('run-one')['events'][-1]
        first=self.assertCommitted(append(self.ledger,scope,'synthetic explanation',source_event_ids=[event['event_id']],command_id='log'))
        path=Path(first['path']);original=path.read_text()
        self.assertIn(event['kind'],original);self.assertIn('synthetic explanation',original)
        self.assertEqual('DUPLICATE',append(self.ledger,scope,'synthetic explanation',source_event_ids=[event['event_id']],command_id='log')['status'])
        self.assertEqual(original,path.read_text())
        with self.ledger._connection() as db:foreign=self.ledger._event(db,'ISOLATED_FOREIGN',{},run_id='foreign')
        self.assertEqual('LOG_SOURCE_EVENT_OUTSIDE_RUN_OR_MISSING',append(self.ledger,scope,'x',source_event_ids=[foreign],command_id='foreign-log')['code'])
        self.assertEqual(original,path.read_text())


if __name__=='__main__':unittest.main()
