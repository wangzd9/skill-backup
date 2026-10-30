"""Isolated regressions for the effectiveness/stall audits; no live verdicts."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from test_business_ledger import LedgerFixture


class Contracts(unittest.TestCase):
    def test_three_observed_invalid_combinations_have_exact_correction(self):
        import workflow_decision_context as c
        cases=[({'operation':'contract','action':'verify_integration','decision_id':'d','expected_fingerprint':'f'*64},
                ['decision_id','expected_fingerprint'],{'operation':'contract','action':'verify_integration'}),
               ({'operation':'prepare','decision_id':'d','action':'verify'},['action'],{'operation':'prepare','decision_id':'d'})]
        for args,unexpected,corrected in cases:
            with patch('workflow_runtime.guard',side_effect=AssertionError('invalid calls cannot enter ledger')):
                result=c.query({'kind':'business'},args)
            self.assertEqual('INVALID_NOT_STARTED',result['phase'])
            self.assertEqual(unexpected,result['query_contract']['unexpected_fields'])
            self.assertEqual(corrected,result['query_contract']['corrected_arguments'])
        from jsonschema import Draft202012Validator
        self.assertTrue(Draft202012Validator(c.query_schema()).is_valid(corrected))
        self.assertFalse(Draft202012Validator(c.query_schema()).is_valid(args))

    def test_junit_rules_shared_by_contract_schema_and_preflight(self):
        from workflow_contracts import payload_schema
        from workflow_decision_context import _contract
        from delivery_evidence import preflight_test_request
        for action in ('verify','verify_integration'):
            schema=payload_schema(action)
            self.assertIn('{junit_xml}',schema['properties']['argv']['description'])
            self.assertIn('{junit_xml}',_contract('TESTER',action)['execution_rules']['junit']['argv'])
        bad=preflight_test_request(['existing-runner'],300,{},'junit')
        self.assertEqual('JUNIT_PLACEHOLDER_REQUIRED',bad['code'])
        self.assertIsNone(preflight_test_request(['existing-runner','{junit_xml}'],300,{},'junit'))
        self.assertEqual('JUNIT_PATH_RUNTIME_MANAGED',preflight_test_request(['pytest','--junitxml=x'],300,{},'pytest')['code'])

    def test_directory_classifies_superseded_and_global_fences(self):
        from test_semantic_decision import synthetic
        from workflow_decision_context import directory
        p,s=synthetic();p['candidates'][0]['state']='SUPERSEDED'
        p['external_actions'][0]['state']='INCOMPLETE'
        p['global_review_fences']=[{'action_id':'foreign','state':'UNKNOWN'}]
        blockers=directory(p,s)['blocker_index']
        self.assertEqual([],blockers['nonterminal_actions'])
        self.assertEqual(1,len(blockers['historical_review_actions']))
        self.assertEqual(p['global_review_fences'],blockers['global_review_fences'])
        self.assertEqual(3,len(blockers['verification_results']))

    def test_metric_windows_end_on_removal_and_material_change(self):
        from workflow_decision_metrics import observe,request,_key
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);ledger=types.SimpleNamespace(path=root/'business.sqlite3')
            scope={'kind':'business','run_id':'r','work_id':'w','execution_id':'e','role':'TESTER'}
            item={'decision_id':'d','material_fingerprint':'a'*64,'action':'verify'}
            observe(ledger,scope,'prepare',{'decision_id':'d','material':{}},[item])
            observe(ledger,scope,'directory',{},[{**item,'material_fingerprint':'b'*64}])
            request(ledger,scope,{'physical_attempt_id':'p','api_request_id':'l'})
            state=json.loads((root/'decision-metrics/executions'/(_key('e')+'.json')).read_text())
            self.assertEqual([],state['decision_ids']);self.assertEqual(1,len(state['retired']))
            self.assertFalse((root/'decision-metrics/requests').exists())

    def test_source_change_ends_metric_window_even_when_ledger_fingerprint_is_same(self):
        from workflow_decision_metrics import observe,_key
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);ledger=types.SimpleNamespace(path=root/'business.sqlite3')
            scope={'kind':'business','run_id':'r','work_id':'w','execution_id':'e','role':'TESTER'}
            item={'decision_id':'d','material_fingerprint':'a'*64,'action':'verify'}
            observe(ledger,scope,'prepare',{'decision_id':'d','material':{}},[item])
            observe(ledger,scope,'prepare',{'decision_id':'d','material':{},'source_status':'CHANGED','refresh_required':True},[item])
            state=json.loads((root/'decision-metrics/executions'/(_key('e')+'.json')).read_text())
            self.assertEqual([],state['decision_ids'])


class NativeLifecycle(LedgerFixture):
    def test_native_runner_blocks_a_second_unverified_operation_but_recovers_original(self):
        import sys
        from delivery_evidence import execute_candidate_tests
        dp,dev=self.bind('A');cid,_=self.candidate(dev);self.close(dp,dev)
        _,tester=self.bind('TESTER')
        code="from pathlib import Path;import sys;Path(sys.argv[1]).write_text('<testsuite tests=\"0\"></testsuite>');sys.exit(1)"
        argv=[sys.executable,'-c',code,'{junit_xml}']
        original=execute_candidate_tests(self.ledger,tester,cid,argv,report_format='junit',command_id='first')
        self.assertEqual('UNVERIFIED',original['status'])
        repeated=execute_candidate_tests(self.ledger,tester,cid,argv,report_format='junit',command_id='second')
        self.assertEqual('UNCHANGED_VERIFICATION_PREREQUISITES',repeated['code']);self.assertFalse(repeated['test_started'])
        self.assertEqual('COORDINATOR',repeated['coordination_handoff']['owner'])
        self.assertEqual(1,sum(e['kind']=='VERIFICATION_RETRY_BLOCKED' for e in self.ledger.snapshot()['events']))
        folder=self.path.parent/'operations'/hashlib.sha256(b'second').hexdigest()[:32]
        self.assertFalse((folder/'result.evidence.json').exists())
        self.assertEqual(original,execute_candidate_tests(self.ledger,tester,cid,argv,report_format='junit',command_id='first'))

    def test_mechanical_pages_scan_farther_without_skipping_semantics_or_ack(self):
        _,identity=self.bind('A')
        with self.ledger._connection() as db:
            for i in range(180):self.ledger._event(db,'EXECUTION_ENDED',{'n':i},run_id='run-one')
            checkpoint=self.ledger._event(db,'CHECKPOINT_SAVED',{'checkpoint':{'UNVERIFIED':'must read'}},run_id='run-one')
            for i in range(180):self.ledger._event(db,'EXECUTION_BOUND',{'n':i},run_id='run-one')
        high=self.ledger.snapshot()['event_high_water']
        expected=self.ledger.events(through=high,run_id='run-one')
        page=self.assertCommitted(self.ledger.work_inputs(identity,limit=100,through=high,compact_mechanical=True,command_id='compact'))
        expanded=[]
        for e in page['events']:
            if e['kind']=='MECHANICAL_AUDIT_SUMMARY':
                ref=e['full_events'];raw=Path(ref['path']).read_bytes()
                self.assertEqual(ref['sha256'],hashlib.sha256(raw).hexdigest())
                rows=json.loads(raw);self.assertEqual(e['event_ids'],[r['event_id'] for r in rows]);expanded+=rows
            else:expanded.append(e)
        self.assertEqual(expected,expanded);self.assertIn(checkpoint,[e['event_id'] for e in expanded])
        self.assertEqual(0,self.ledger.current_work('A')['processed_through'])
        self.assertEqual(high,page['through']);self.assertFalse(page['has_more'])
        self.assertLess(len(json.dumps(page['events']).encode()),65536)
        self.assertEqual(page['events'],self.ledger.work_inputs(identity,limit=100,through=high,compact_mechanical=True,command_id='compact')['events'])
        self.assertCommitted(self.ledger.ack_events(identity,page['through'],command_id='ack'))

    def test_business_progress_does_not_turn_ack_or_checkpoint_into_delivery(self):
        from workflow_business_progress import progress
        _,identity=self.bind('A')
        self.ledger.checkpoint(identity,{'summary':'analysis only'},command_id='cp')
        work=self.ledger.current_work('A');snapshot=self.ledger.snapshot()
        value=progress(snapshot,work,self.now)
        self.assertFalse(value['effective_action_observed'])
        self.assertIsNone(value['last_business_transition'])
        with self.ledger._connection() as db:
            eid=self.ledger._event(db,'CANDIDATE_DELIVERED',{'isolated':'synthetic observation fixture'},run_id='run-one',work_id=work['work_id'])
        value=progress(self.ledger.snapshot(),work,self.now)
        self.assertEqual(eid,value['first_effective_action_in_execution']['event_id'])

    def test_evidence_preflight_reports_absolute_path_and_hash_errors_together(self):
        from workflow_delivery import preflight,evidence_directory
        from business_ledger import _Denied
        file=self.root/'proof.txt';file.write_text('actual isolated proof')
        with self.assertRaises(_Denied) as caught:
            preflight(self.ledger,{'evidence':[{'path':'relative.txt'},{'path':str(file),'sha256':'0'*64}]})
        errors=caught.exception.details['errors']
        self.assertEqual(['candidate.evidence.0.path','candidate.evidence.1.path'],[e['field'] for e in errors])
        self.assertEqual(['EVIDENCE_ABSOLUTE_PATH_REQUIRED','EVIDENCE_HASH_MISMATCH'],[e['code'] for e in errors])
        path=evidence_directory(self.ledger,'w',create=True)
        self.assertTrue(path.is_dir());self.assertFalse(path.is_relative_to(self.repo))
        self.assertEqual(0o700,path.stat().st_mode & 0o777)

    def test_unknown_notifications_have_complete_counts_bounded_samples_and_real_owner(self):
        from workflow_notification_registry import diagnostics
        with self.ledger._connection() as db:
            for i in range(300):
                db.execute('INSERT INTO outbox VALUES(?,?,?,?,?,?,?,?,?)',
                    (str(i),1,'PERIODIC_SUMMARY','TESTER','run-one','{}','UNKNOWN',None,self.now-10000))
        before=self.ledger.outbox_items(states=('UNKNOWN',),limit=10000)
        value=diagnostics(self.ledger)
        self.assertEqual(300,value['counts_by_state']['UNKNOWN']);self.assertEqual(44,value['samples_omitted'])
        self.assertEqual(256,len(value['samples']))
        self.assertTrue(all(r['owner']!='UNREGISTERED_CONSUMER' for r in value['samples']))
        self.assertEqual(before,self.ledger.outbox_items(states=('UNKNOWN',),limit=10000))

    def test_rejected_service_requires_stable_zero_dispatch_proof_and_identity(self):
        from workflow_terminal_observation import terminal
        candidate={'candidate_id':'c','run_id':'r','task_id':'t','attempt':1,'base':'a','head':'b','ac_version':'v'}
        action={'action_id':'a','state':'REJECTED','candidate_id':'c'}
        root=self.path.parent/'services'/hashlib.sha256(b'a').hexdigest();root.mkdir(parents=True)
        value={'action_id':'a','candidate_id':'c','frozen':{'candidate':candidate},'state':'REJECTED','error':'PreDispatchRejected'}
        (root/'invocation.json').write_text(json.dumps(value))
        result=terminal(action,candidate,{'verifications':[]},ledger_dir=self.path.parent)
        self.assertEqual('rejection_no_dispatch_verified',result['observation']);self.assertFalse(result['business_pass'])
        (root/'native').mkdir();(root/'native/dispatch-intent.json').write_text('{}')
        with self.assertRaisesRegex(ValueError,'ZERO_DISPATCH'):terminal(action,candidate,{'verifications':[]},ledger_dir=self.path.parent)
        value['frozen']['candidate']={**candidate,'head':'changed'};(root/'invocation.json').write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,'IDENTITY'):terminal(action,candidate,{'verifications':[]},ledger_dir=self.path.parent)


class StorageAndRetry(unittest.TestCase):
    def test_real_ocr_test_only_preview_keeps_original_rules_and_explicit_excludes(self):
        import subprocess
        from workflow_ocr_selection import freeze
        import ocr_runtime
        if not ocr_runtime.OCR.is_file():self.skipTest('Installed OCR preview unavailable')
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);repo=root/'repo';repo.mkdir();home=root/'home';home.mkdir()
            def git(*args):return subprocess.check_output(['git','-C',str(repo),*args],stderr=subprocess.DEVNULL,text=True).strip()
            git('init','-q');git('config','user.name','Isolated OCR Fixture');git('config','user.email','fixture@example.invalid')
            (repo/'tests').mkdir();file=repo/'tests/test_fixture.py';file.write_text('def test_fixture():\n    assert 1 == 1\n')
            (repo/'.opencodereview').mkdir();rule=repo/'.opencodereview/rule.json'
            rule.write_text(json.dumps({'rules':[{'path':'**/*.py','rule':'Preserve this original isolation contract.'}]}))
            git('add','.');git('commit','-qm','isolated base');base=git('rev-parse','HEAD')
            file.write_text('def test_fixture():\n    assert 2 == 2\n');git('add','.');git('commit','-qm','isolated test change');head=git('rev-parse','HEAD')
            initial=ocr_runtime.preview(repo,base,head)
            self.assertEqual([],ocr_runtime.selected(initial))
            with patch('workflow_ocr_selection.Path.home',return_value=home):selection=freeze(repo,initial,root/'frozen')
            selected=ocr_runtime.preview(repo,base,head,selection_rule=selection)
            self.assertEqual(['tests/test_fixture.py'],ocr_runtime.selected(selected))
            background=root/'background.md';background.write_text('Original AC: test isolation and repeatability.')
            rules=ocr_runtime.resolved_rules(repo,base,head,{'files':['tests/test_fixture.py'],'exclude':[]},background,selection_rule=selection)
            self.assertIn('Preserve this original isolation contract.',json.dumps(rules))
            self.assertEqual('',git('status','--porcelain'))
            rule.write_text(json.dumps({'exclude':['tests/**'],'rules':[]}))
            with patch('workflow_ocr_selection.Path.home',return_value=home):
                self.assertIsNone(freeze(repo,ocr_runtime.preview(repo,base,head),root/'explicit-excluded'))

    def test_capacity_caches_complete_inventory_and_marks_partial_lower_bounds(self):
        from workflow_capacity import observe
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);db=root/'business.sqlite3';db.write_bytes(b'db');Path(str(db)+'-wal').write_bytes(b'wal')
            folder=root/'input-pages';folder.mkdir();(folder/'a').write_bytes(b'attachment')
            ledger=types.SimpleNamespace(path=db)
            first=observe(ledger,at=1000);self.assertTrue(first['attachments_complete'])
            self.assertEqual(15,first['total_bytes_lower_bound'])
            self.assertTrue(observe(ledger,at=1001)['cached'])
            (folder/'b').write_bytes(b'new')
            second=observe(ledger,at=1400)
            self.assertGreater(second['attachment_growth_bytes_per_second'],0)
            partial=observe(ledger,at=1800,max_entries=1)
            self.assertFalse(partial['attachments_complete']);self.assertIsNone(partial['attachment_growth_bytes_per_second'])
            self.assertEqual(b'db',db.read_bytes());self.assertEqual(b'attachment',(folder/'a').read_bytes())

    def test_same_environment_failure_is_not_replayed_new_candidate_or_input_can_run(self):
        from workflow_verification_retry import basis,claim,finish
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);ledger=types.SimpleNamespace(path=root/'business.sqlite3')
            candidate={'repo':str(root),'run_id':'r','candidate_id':'c','head':'h','attempt':1}
            folder=root/'op1';folder.mkdir();(folder/'verification.json').write_text('{"isolated_result":"unverified"}')
            spec=root/'config';spec.write_text('original')
            args=(ledger,candidate,['pytest','case'],'mysql_migration','pytest',{'config':[str(spec)]},{'python':'frozen'})
            key=basis(*args);self.assertIsNone(claim(ledger,key,folder))
            finish(ledger,key,folder,{'test_stats':{'mechanical_result':'UNVERIFIED','case_fingerprint':'same'},'validation':{'valid':False}})
            refusal=claim(ledger,key,root/'op2')
            self.assertEqual('UNCHANGED_VERIFICATION_PREREQUISITES',refusal['code'])
            self.assertTrue(refusal['prior_evidence_verified']);self.assertFalse(refusal['test_started'])
            self.assertIsNone(claim(ledger,key,folder))  # recovery of original operation
            spec.write_text('actual changed configuration')
            self.assertNotEqual(key,basis(*args));self.assertIsNone(claim(ledger,basis(*args),root/'op2'))

    def test_new_disposable_mysql_id_does_not_look_like_a_fixture_fix(self):
        from workflow_verification_retry import basis
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);ledger=types.SimpleNamespace(path=root/'business.sqlite3')
            candidate={'repo':str(root),'candidate_id':'c','head':'h'}
            def key(instance):
                folder=root/'mysql-sessions'/instance;folder.mkdir(parents=True)
                path=folder/'ready.json';path.write_text(json.dumps({'container_id':instance,'image':'same-image','version':'8.4','network':'none','database':'test'}))
                return basis(ledger,candidate,['pytest'],'mysql_migration','pytest',{'inputs':[str(path)]},{})
            self.assertEqual(key('first'),key('second'))
