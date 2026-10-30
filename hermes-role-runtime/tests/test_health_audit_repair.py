"""F1–F8 lifecycle regressions in isolated ledgers; never business acceptance.

The MN document fixture tests contract shape only. Its cases below are for the
runtime's lifecycle, not evidence that the actual MN feature passed its ACs.
"""
import hashlib
import json
from pathlib import Path
import sys
from unittest import mock

from test_business_ledger import LedgerFixture
import test_workflow_completion as completion_tests
from test_workflow_merge import CHECK
from delivery_evidence import execute_candidate_tests
from workflow_completion import reconcile, facts, deliver, complete


class InputLifecycleTests(LedgerFixture):
    def test_nested_control_is_rejected_and_receipt_describes_effective_ack_and_yield(self):
        worker,identity=self.bind('A')
        before=self.ledger.current_work('A')
        bad=self.ledger.checkpoint(identity,{'summary':'isolated misuse','processed_through':999,'yield_execution':True},command_id='bad-nested')
        self.assertEqual('CHECKPOINT_CONTROL_FIELDS_MISPLACED',bad['code'])
        self.assertFalse(bad['saved']);self.assertFalse(bad['yielded'])
        self.assertEqual(before,self.ledger.current_work('A'))
        page=self.ledger.work_inputs(identity,command_id='page')
        good=self.assertCommitted(self.ledger.checkpoint(identity,{'summary':'actual isolated progress'},
            processed_through=page['through'],yield_execution=True,command_id='good'))
        self.assertEqual(page['through'],good['processed_through'])
        self.assertEqual(('QUEUED','DRAINING',True,True),tuple(good[k] for k in ('work_state','execution_state','acknowledged','yielded')))
        self.assertFalse(self.ledger.admission_hint('A')['can_start_model'])
        self.close(worker,identity)

    def test_pages_are_bounded_content_addressed_and_replayable_without_ack(self):
        worker,identity=self.bind('A')
        with self.ledger._connection() as db:
            db.execute('BEGIN')
            for n in range(110):
                self.ledger._event(db,'ISOLATED_PAGE_FIXTURE',{'n':n,'text':'x'*1000},run_id='run-one')
            db.commit()
        high=self.ledger.snapshot()['event_high_water']
        pages=[self.assertCommitted(self.ledger.work_inputs(identity,limit=100,through=high,command_id='page-'+str(n))) for n in range(12)]
        self.assertLessEqual(len(json.dumps(pages[0]['events'],ensure_ascii=False).encode()),65536)
        self.assertTrue(pages[0]['has_more']);self.assertEqual(0,self.ledger.current_work('A')['processed_through'])
        with self.ledger._connection() as db:
            stored=[json.loads(r[0]) for r in db.execute("SELECT result FROM commands WHERE verb='work_inputs'")]
        self.assertTrue(all('events' not in r and 'events_ref' in r for r in stored))
        self.assertEqual(1,len({r['events_ref']['sha256'] for r in stored}))
        self.assertLess(sum(len(json.dumps(r)) for r in stored),len(json.dumps(pages[0]['events']))*2)
        replay=self.ledger.work_inputs(identity,limit=100,through=high,command_id='page-0')
        self.assertEqual('DUPLICATE',replay['status']);self.assertEqual(pages[0]['events'],replay['events'])
        next_page=self.ledger.work_inputs(identity,after=pages[0]['next_after'],through=high,command_id='next')
        self.assertGreater(next_page['through'],pages[0]['through'])
        self.assertCommitted(self.ledger.ack_events(identity,next_page['through'],command_id='ack'))
        self.assertEqual(next_page['through'],self.ledger.current_work('A')['processed_through'])

    def test_large_single_event_is_a_verified_reference_not_dropped(self):
        worker,identity=self.bind('A')
        with self.ledger._connection() as db:
            eid=self.ledger._event(db,'ISOLATED_LARGE_EVENT',{'text':'z'*90000},run_id='run-one')
        page=self.ledger.work_inputs(identity,command_id='large-page')
        event=next(e for e in page['events'] if e['event_id']==eid)
        ref=event['payload']['full_event'];raw=Path(ref['path']).read_bytes()
        self.assertEqual(ref['sha256'],hashlib.sha256(raw).hexdigest())
        self.assertEqual('z'*90000,json.loads(raw)['payload']['text'])
        self.assertEqual(0,self.ledger.current_work('A')['processed_through'])

    def test_repeated_resume_backs_off_but_fresh_business_feedback_resumes(self):
        for n in range(3):
            worker,identity=self.bind('A')
            self.assertCommitted(self.ledger.checkpoint(identity,{'summary':'same isolated boundary'},yield_execution=True,command_id='cp-'+str(n)))
            self.close(worker,identity);self.now+=1
        hint=self.ledger.admission_hint('A')
        self.assertFalse(hint['can_start_model']);self.assertEqual('NO_PROGRESS_RESUMPTION_BACKOFF',hint['reason'])
        with self.ledger._connection() as db:self.ledger._event(db,'ISOLATED_NEW_BUSINESS_FEEDBACK',{},run_id='run-one')
        self.assertTrue(self.ledger.admission_hint('A')['can_start_model'])
        self.assertEqual(0,self.ledger.current_work('A')['processed_through'])

    def test_scoped_snapshot_excludes_other_run_history_and_supports_increment(self):
        with self.ledger._connection() as db:
            self.ledger._event(db,'ISOLATED_OTHER_RUN',{},run_id='other-isolated-run')
        high=self.ledger.snapshot()['event_high_water']
        scoped=self.ledger.snapshot('run-one')
        self.assertFalse(any(e['kind']=='ISOLATED_OTHER_RUN' for e in scoped['events']))
        self.assertEqual(high,scoped['event_high_water'])
        self.assertEqual([],self.ledger.snapshot('run-one',event_after=high)['events'])

    def test_invalid_request_does_not_reserve_id_and_corrected_request_executes(self):
        dp,dev=self.bind('A');cid,_=self.candidate(dev);self.close(dp,dev)
        tp,tester=self.bind('TESTER')
        for argv,options in [(['echo'],{}),([sys.executable,'-c','pass'],{'report_format':'junit'}),
                (['pytest'],{'evidence':{'wrong':'not accepted'}}),(['pytest'],{'timeout_seconds':float('nan')})]:
            bad=execute_candidate_tests(self.ledger,tester,cid,argv,command_id='correctable',**options)
            self.assertEqual('INVALID_NOT_STARTED',bad['phase']);self.assertFalse(bad['new_command_id_required'])
        self.assertFalse((self.path.parent/'operations').exists())
        result=execute_candidate_tests(self.ledger,tester,cid,[sys.executable,'-c',CHECK,'{junit_xml}','1'],report_format='junit',command_id='correctable')
        self.assertCommitted(result)
        with self.assertRaisesRegex(ValueError,'TEST_COMMAND_ID_CONFLICT'):
            execute_candidate_tests(self.ledger,tester,cid,[sys.executable,'-c',CHECK,'{junit_xml}','2'],report_format='junit',command_id='correctable')


class PhasedCompletionTests(completion_tests.CompletionTests):
    queue=completion_tests.CompletionTests.queue
    run_final=completion_tests.CompletionTests.run_final
    payload=completion_tests.CompletionTests.payload

    def use_mn_contract(self):
        self.acceptance.write_bytes((Path(__file__).parent/'fixtures/mn-acceptance-phases.md').read_bytes())
        ref={'path':str(self.root/'ACCEPTANCE.md'),'frozen_path':str(self.acceptance),
            'sha256':hashlib.sha256(self.acceptance.read_bytes()).hexdigest()}
        with self.ledger._connection() as db:
            self.ledger._event(db,'REQUIREMENT_ACCEPTED',{'sources':[ref],'task_ids':['T1','T2']},run_id='run-one')
        self.queue()
        # Isolated lifecycle fixture does not use a MySQL contract's actual
        # results. The real-MySQL gate remains independently exercised by the
        # existing CompletionTests regression; no production record is touched.

    def test_real_mn_document_has_ten_local_and_ten_closure_obligations_and_deferred_phases(self):
        self.use_mn_contract();spec=facts(self.ledger.snapshot(),'run-one')['plan']
        for phase in ('local_validation','local_closure','real_nodes','production'):
            self.assertEqual(10,len([o for o in spec['obligations'] if o['phase']==phase]))
        ac10=next(o for o in spec['obligations'] if o['id']=='MN-AC-10:local')
        self.assertEqual(('TESTER','report_index'),(ac10['owner'],ac10['evidence_type']))
        self.assertFalse(any(o['applicable'] for o in spec['obligations'] if o['phase']=='production'))
        self.assertTrue(spec['requires_isolated_mysql'])

    def test_legacy_plan_is_frozen_append_only_without_rewriting_report_or_tasks(self):
        from workflow_completion import ensure_plan
        self.use_mn_contract()
        state=facts(self.ledger.snapshot(),'run-one')
        legacy={k:v for k,v in state['plan'].items() if k not in {
            'obligations','obligation_schema_version','coverage_matrix_required','phase_basis','contract_text_sha256'}}
        with self.ledger._connection() as db:
            original=self.ledger._event(db,'COMPLETION_PLAN_PUBLISHED',legacy,run_id='run-one')
        before=self.ledger.snapshot()
        self.assertCommitted(ensure_plan(self.ledger))
        after=self.ledger.snapshot()
        self.assertEqual(before['tasks'],after['tasks'])
        self.assertEqual(legacy,next(e for e in after['events'] if e['event_id']==original)['payload'])
        frozen=next(e for e in after['events'] if e['kind']=='COMPLETION_OBLIGATIONS_FROZEN')
        self.assertEqual(original,frozen['payload']['base_plan_event_id'])
        self.assertEqual(40,len(frozen['payload']['plan']['obligations']))
        self.assertIsNone(ensure_plan(self.ledger))

    def test_wrong_ac_scenario_and_nonexistent_case_are_rejected(self):
        self.use_mn_contract();tp,tester,eid=self.run_final()
        spec=facts(self.ledger.snapshot(),'run-one')['plan']
        obligations=[o for o in spec['obligations'] if o['phase']=='local_validation']
        checks=[]
        for o in obligations:
            proof={'obligation_id':o['id'],'phase':'local_validation','contract_sha256':spec['contract_text_sha256'],
                'scenario':o['scenario'],'assertion':'Isolated runtime proof only','environment':'local',
                'test_event_id':eid,'case_id':'test_actual_candidate','event_ids':[eid]}
            checks.append({'id':o['ac_id'],'result':'PASS','mode':'local','test_event_ids':[eid],'notes':'isolated shape regression','coverage':[proof]})
        payload={'summary':'Isolated runtime matrix shape; not feature acceptance','checks':checks,'issues':[]}
        checks[5]['coverage'][0]['scenario']=checks[6]['coverage'][0]['scenario']
        result=deliver(self.ledger,tester,payload,command_id='swapped')
        self.assertEqual('INTEGRATION_AC_SCENARIO_MISMATCH',result['code'])
        checks[5]['coverage'][0]['scenario']=obligations[5]['scenario']
        checks[5]['coverage'][0]['case_id']='not_an_executed_case'
        self.assertEqual('INTEGRATION_AC_CASE_NOT_UNIQUE_PASS',deliver(self.ledger,tester,payload,command_id='nonexistent')['code'])
        checks[5]['coverage'][0]['case_id']='test_actual_candidate'
        self.assertEqual('INTEGRATION_REAL_ISOLATED_MYSQL_REQUIRED',deliver(self.ledger,tester,payload,command_id='no-real-mysql')['code'])

    def test_multiphase_local_pass_can_close_after_separate_per_ac_review(self):
        # Real MN phase shape, with an explicitly local-only isolated contract;
        # this fixture does not weaken the real MN MySQL requirement.
        text=(Path(__file__).parent/'fixtures/mn-acceptance-phases.md').read_text().replace('隔离 MySQL','本地数据库')
        self.acceptance.write_text(text)
        ref={'path':str(self.root/'ACCEPTANCE.md'),'frozen_path':str(self.acceptance),'sha256':hashlib.sha256(self.acceptance.read_bytes()).hexdigest()}
        with self.ledger._connection() as db:self.ledger._event(db,'REQUIREMENT_ACCEPTED',{'sources':[ref],'task_ids':['T1','T2']},run_id='run-one')
        self.queue();tp,tester,eid=self.run_final()
        spec=facts(self.ledger.snapshot(),'run-one')['plan']
        checks=[]
        for o in spec['obligations']:
            if o['phase']!='local_validation':continue
            checks.append({'id':o['ac_id'],'result':'PASS','mode':'local','test_event_ids':[eid],'notes':'Isolated runtime fixture',
                'coverage':[{'obligation_id':o['id'],'phase':'local_validation','contract_sha256':spec['contract_text_sha256'],
                    'scenario':o['scenario'],'assertion':'Actual isolated candidate value assertion; lifecycle only','environment':'local',
                    'test_event_id':eid,'case_id':'test_actual_candidate','event_ids':[eid]}]})
        next(c for c in checks if c['id']=='MN-AC-10')['test_event_ids']=[]
        self.assertCommitted(deliver(self.ledger,tester,{'summary':'Isolated local lifecycle only','checks':checks,'issues':[]},command_id='phase-deliver'))
        self.close(tp,tester)
        closure={'summary':'Isolated closure','review':'Matrix reviewed','historical_failures':[]}
        self.assertEqual('REQUIREMENT_PER_AC_SEMANTIC_REVIEW_REQUIRED',complete(self.ledger,self.coord,closure,command_id='missing-review')['code'])
        state=facts(self.ledger.snapshot(),'run-one')
        closure['coverage_review']=[{'obligation_id':o['id'],'validation_event_id':state['report']['event_id'],
            'result':'PASS','reason':'Isolated runtime semantic attestation fixture only'} for o in spec['obligations'] if o['phase']=='local_closure']
        self.assertCommitted(complete(self.ledger,self.coord,closure,command_id='phase-close'))
        final=next(e for e in self.ledger.snapshot()['events'] if e['kind']=='REQUIREMENT_COMPLETED')
        self.assertEqual(20,len(final['payload']['deferred_obligations']))
        self.assertFalse(final['payload']['production_complete'])

    def test_nonblocking_environment_issue_requires_disposition_without_erasing_raw_failure(self):
        self.queue();tp,tester,eid=self.run_final()
        issue={'id':'ENV-OUT','kind':'environment','test_event_ids':[],'summary':'Isolated unrelated environment issue',
            'blocking':False,'confidence':'confirmed','accepted_scope':'out_of_scope','ac_ids':[],
            'owner':'COORDINATOR','disposition':'excluded_with_review','rationale':'Outside isolated accepted contract',
            'review_required':True}
        self.assertCommitted(deliver(self.ledger,tester,self.payload(eid,issues=[issue]),command_id='out-scope'))
        self.close(tp,tester)
        closure={'summary':'Isolated local result','review':'Reviewed','historical_failures':[]}
        self.assertEqual('REQUIREMENT_NONBLOCKING_ISSUE_DISPOSITION_REQUIRED',complete(self.ledger,self.coord,closure,command_id='no-issue-review')['code'])
        evidence=self.root/'scope-review.txt';evidence.write_text('Isolated explicit scope review; no raw record modified')
        closure['issue_dispositions']=[{'issue_id':'ENV-OUT','reason':'Outside accepted local contract','evidence':[{'path':str(evidence)}]}]
        self.assertCommitted(complete(self.ledger,self.coord,closure,command_id='scope-reviewed'))
        report=facts(self.ledger.snapshot(),'run-one')['report']
        self.assertEqual(issue,report['payload']['issues'][0])

    def test_invalid_integration_parameters_never_start_mysql(self):
        from workflow_completion import verify
        self.queue();tp,tester=self.bind('TESTER')
        with mock.patch('workflow_supplemental.mysql_session',side_effect=AssertionError('No service on invalid request')):
            result=verify(self.ledger,tester,{'argv':['echo'],'profile':'mysql'},command_id='invalid-mysql-request')
        self.assertEqual('INVALID_NOT_STARTED',result['phase'])
        self.assertFalse((self.path.parent/'mysql-sessions').exists())

    def test_environment_exhaustion_can_resume_only_with_new_audited_recovery_evidence(self):
        self.queue()
        for n in range(3):
            tp,tester=self.bind('TESTER')
            self.assertCommitted(deliver(self.ledger,tester,self.payload(None,result='UNVERIFIED',issues=[{
                'id':'ENV','kind':'environment','test_event_ids':[],'summary':'Isolated service unavailable'}]),command_id='exhaust-'+str(n)))
            self.close(tp,tester);self.now+=301;reconcile(self.ledger)
        self.assertFalse(reconcile(self.ledger))
        from workflow_issue_policy import environment_recovered
        state=facts(self.ledger.snapshot(),'run-one');proof=self.root/'recovered.txt';proof.write_text('Isolated restored service probe')
        payload={'validation_event_id':state['report']['event_id'],'summary':'Isolated restored','evidence':[{'path':str(proof)}]}
        self.assertCommitted(environment_recovered(self.ledger,self.coord,payload,command_id='recovered'))
        self.now+=301
        self.assertEqual('ENVIRONMENT_RECOVERY_NEW_EVIDENCE_REQUIRED',environment_recovered(self.ledger,self.coord,payload,command_id='same-proof')['code'])
        self.assertTrue(reconcile(self.ledger))


class NotificationAndObserverTests(LedgerFixture):
    def test_final_event_contracts_deliver_fresh_suppress_old_and_never_retry_unknown(self):
        from workflow_notification_registry import REGISTRY,diagnostics
        from workflow_notifications import drain_business_notifications
        pool=self.root/'isolated.json';pool.write_text(json.dumps({'roles':{'TESTER':{'chat_id':'isolated-test-chat'},'COORDINATOR':{'chat_id':'isolated-coordinator'}}}))
        with self.ledger._connection() as db:
            old=self.ledger._event(db,'INTEGRATION_VERIFICATION_QUEUED',{'work_id':'isolated-old'},run_id='run-one',role='TESTER',notify=True)
        self.now+=1801
        with self.ledger._connection() as db:
            fresh=self.ledger._event(db,'INTEGRATION_VERIFICATION_REPORTED',{'summary':'isolated final event'},run_id='run-one',role='TESTER',notify=True)
            unknown=self.ledger._event(db,'REQUIREMENT_COMPLETED',{'summary':'isolated unknown delivery'},run_id='run-one',role='COORDINATOR',notify=True)
            db.execute("UPDATE outbox SET state='UNKNOWN' WHERE event_id=?",(unknown,))
        self.assertTrue(diagnostics(self.ledger))
        sent=[]
        with mock.patch('workflow_runtime.open_ledger',return_value=self.ledger):
            drain_business_notifications(pool,transport=lambda row:sent.append(row) or {'delivered':True,'message_id':'isolated-confirmed'})
            drain_business_notifications(pool,transport=lambda row:self.fail('Do not resend'))
        rows=self.ledger.outbox_items(include_terminal=True)
        self.assertEqual(('SUPPRESSED','SENT','UNKNOWN'),tuple(r['state'] for r in rows))
        self.assertEqual(1,len(sent));self.assertEqual('STALE_BUSINESS_NOTICE_NOT_REPLAYED',rows[0]['receipt']['reason'])
        self.assertTrue({'INTEGRATION_VERIFICATION_QUEUED','INTEGRATION_VERIFICATION_REPORTED','FINAL_ACCEPTANCE_REWORK_QUEUED','REQUIREMENT_COMPLETED'}<=set(REGISTRY))
        self.assertEqual(unknown,self.ledger.snapshot()['event_high_water'])

    def terminal_fixture(self):
        dp,dev=self.bind('A');cid,spec=self.candidate(dev);self.close(dp,dev)
        candidate=next(c for c in self.ledger.snapshot()['candidates'] if c['candidate_id']==cid)
        proof={k:candidate[k] for k in ('candidate_id','run_id','task_id','attempt','base','head','ac_version')}
        proof.update(action_id='isolated-terminal',service_id='isolated-service',service_stopped=True,sources=[])
        path=self.root/'terminal.json';path.write_text(json.dumps(proof))
        action={'action_id':'isolated-terminal','candidate_id':cid,'state':'ENDED','kind':'ocr',
            'authorization':{'service_id':'isolated-service'},'evidence':{'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}}
        return candidate,action,path

    def test_terminal_receipt_uses_durable_proof_and_preserves_incomplete_result(self):
        from workflow_terminal_observation import terminal
        candidate,action,path=self.terminal_fixture()
        observed=terminal(action,candidate,self.ledger.snapshot())
        self.assertEqual('terminal_receipt_verified',observed['observation'])
        self.assertFalse(observed['writer_liveness_probe_required'])
        action['state']='INCOMPLETE'
        self.assertEqual('INCOMPLETE',terminal(action,candidate,self.ledger.snapshot())['result_completeness'])
        path.write_text('{}')
        with self.assertRaisesRegex(ValueError,'RECEIPT_CHANGED'):terminal(action,candidate,self.ledger.snapshot())
        action['state']='UNKNOWN';self.assertIsNone(terminal(action,candidate,self.ledger.snapshot()))

    def test_verified_terminal_unit_incident_is_archived_but_unknown_writer_is_retained(self):
        from workflow_observation import step
        prior={'active':{'service':{'identity':'service','type':'observer_independent_service_unverified',
            'action_id':'isolated-terminal','source':'isolated-gone.service','first_seen':self.now-90000,
            'last_seen':self.now-90000,'episode':'isolated-episode','severity':1},
            'writer':{'identity':'writer','type':'observer_frozen_execution_unverified','source':'isolated-writer',
            'first_seen':self.now-90000,'last_seen':self.now-90000,'episode':'isolated-writer-episode','severity':1}}}
        observation={'services':[{'action_id':'isolated-terminal','observation':'terminal_receipt_verified'}],
            'faults':[{'type':'observer_frozen_execution_unverified','source':'isolated-writer','identity':'writer'}]}
        state,notices=step(prior,observation,self.now)
        self.assertNotIn('service',state['active']);self.assertIn('service',state['terminal_service_closures'])
        self.assertIn('writer',state['active']);self.assertFalse(notices)

    def test_hot_activation_replaces_observer_persistence_boundary(self):
        import mechanism_activation
        import role_observer
        old=role_observer.observe_pool_leases
        try:
            role_observer.observe_pool_leases=lambda *a,**kw:None
            mechanism_activation.activate()
            result=role_observer.observe_pool_leases(self.root/'missing-isolated-pool.json',home=self.root/'isolated-home')
            self.assertIn('health_layers',result)
        finally:role_observer.observe_pool_leases=old

    def test_ended_service_never_queries_transient_unit(self):
        from role_observer import _workflow_observation
        candidate,action,path=self.terminal_fixture()
        from business_ledger import _json
        with self.ledger._connection() as db:
            db.execute('INSERT INTO external_actions VALUES(?,?,?,?,?,?,?)',
                (action['action_id'],action['candidate_id'],'ocr',_json(action['authorization']),'ENDED',self.now,_json(action['evidence'])))
        home=self.root/'home';home.mkdir()
        # No service/systemctl should be invoked by terminal observation. The
        # isolated observer may still expose unrelated missing collector facts.
        with mock.patch('subprocess.run',side_effect=AssertionError('Terminal service must not query unit')):
            result=_workflow_observation(self.path,self.root/'pool.json',home,self.now)
        service=next(s for s in result['services'] if s['action_id']==action['action_id'])
        self.assertEqual('terminal_receipt_verified',service['observation'])
        self.assertFalse(any(f['type']=='observer_independent_service_unverified' for f in result['faults']))
