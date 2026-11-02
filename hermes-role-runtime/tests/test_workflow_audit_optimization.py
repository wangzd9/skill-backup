"""Isolated synthetic workflow regressions; no real acceptance or model calls."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests.test_business_ledger import LedgerFixture
from workflow_capacity import observe
from workflow_input_pages import compress_legacy_result,expand_result
from workflow_provider_health import record,cooldown


class WaitAndMemoryTests(LedgerFixture):
    def test_precise_wait_ignores_other_task_and_resumes_target_without_ack(self):
        proc,identity=self.bind('COORDINATOR')
        page=self.ledger.work_inputs(identity,command_id='initial')
        self.assertCommitted(self.ledger.checkpoint(identity,{'waiting_external':['isolated T1 dependency'],
            'wait_conditions':{'version':1,'objects':[{'kind':'task','id':'T1'}]},
            'next':'app.py:value'},processed_through=page['through'],yield_execution=True,command_id='wait'))
        self.close(proc,identity)
        with self.ledger._connection() as db:
            self.ledger._event(db,'ISOLATED_SYNTHETIC_TASK_NOTE',{'task_id':'T2'},run_id='run-one')
        before=self.ledger.snapshot()
        self.assertFalse(self.ledger.admission_hint('COORDINATOR')['can_start_model'])
        self.assertEqual(before,self.ledger.snapshot())
        with self.ledger._connection() as db:
            self.ledger._event(db,'ISOLATED_SYNTHETIC_DEPENDENCY_RESULT',{'task_id':'T1'},run_id='run-one')
        self.assertEqual('WAIT_TARGET_CHANGED',self.ledger.admission_hint('COORDINATOR')['reason'])
        self.assertEqual(page['through'],self.ledger.current_work('COORDINATOR')['processed_through'])
        self.now+=901
        self.assertTrue(self.ledger.admission_hint('COORDINATOR')['can_start_model'])

    def test_default_memory_is_locator_and_keeps_semantic_memory_across_checkpoints(self):
        _,identity=self.bind('A')
        memory={'version':1,'entries':[{'id':'read-locator','conclusion':'isolated historical observation',
            'dependencies':{'task_id':'T1'},'source_event_ids':[],'next':'app.py:value'}]}
        self.assertCommitted(self.ledger.checkpoint(identity,{'work_memory':memory},command_id='semantic'))
        self.assertCommitted(self.ledger.checkpoint(identity,{'next':'app.py:value:continue'},command_id='automatic'))
        work=self.ledger.current_work('A')
        from workflow_work_memory import inspect
        rows=inspect(self.ledger.snapshot(),work)['entries']
        self.assertEqual({'read-locator','runtime-continuation'},{r['id'] for r in rows})
        self.assertTrue(all(not r['current_verified'] for r in rows))
        self.assertEqual('app.py:value:continue',rows[-1]['next'])


class CapacityTests(unittest.TestCase):
    def test_tail_directories_and_large_root_finish_across_bounded_ticks(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);ledger=mock.Mock(path=root/'business.sqlite3')
            (root/'input-pages').mkdir();(root/'mysql-sessions').mkdir()
            for index in range(37):(root/'input-pages'/str(index)).write_bytes(b'x'*3)
            (root/'mysql-sessions'/'tail').write_bytes(b'tail')
            seen_tail=False;result=None
            for tick in range(80):
                result=observe(ledger,at=tick*301,ttl=0,max_entries=4)
                self.assertLessEqual(result['scan_entries_processed'],4)
                seen_tail|=result['attachments']['mysql-sessions']['files']==1
                if result['attachments_complete']:break
            self.assertTrue(seen_tail);self.assertTrue(result['attachments_complete'])
            self.assertEqual((111,37),(result['attachments']['input-pages']['bytes'],result['attachments']['input-pages']['files']))
            self.assertEqual(4,result['attachments']['mysql-sessions']['bytes'])


class LegacyStorageTests(LedgerFixture):
    def test_compressed_history_replays_original_command_without_operation(self):
        _,identity=self.bind('COORDINATOR')
        # Explicit isolated synthetic old-format result, not a real business record.
        original={'status':'COMMITTED','events':[{'event_id':1,'kind':'ISOLATED_SYNTHETIC',
            'payload':{'text':'重复正文'*100000}}],'through':1,'decision':{'synthetic_fixture':True}}
        compressed=compress_legacy_result(original)
        self.assertLess(len(json.dumps(compressed)),len(json.dumps(original))//20)
        self.assertEqual(original,expand_result(self.ledger,compressed))
        from business_ledger import _hash
        with self.ledger._connection() as db:
            db.execute('INSERT INTO commands VALUES(?,?,?,?,?)',('legacy-input','work_inputs',_hash([identity]),
                json.dumps(compressed),self.now))
        def never(db):self.fail('replay executed an operation')
        replay=self.ledger._command('work_inputs','legacy-input',[identity],never)
        self.assertEqual('DUPLICATE',replay['status']);self.assertEqual(original['events'],replay['events'])
        self.assertEqual('COMMAND_ID_CONFLICT',self.ledger._command('work_inputs','legacy-input',[identity,'changed'],never)['code'])

    def test_offline_compaction_keeps_all_hidden_rowids_including_gaps(self):
        import sqlite3
        from scripts.compact_ledger import compact
        copy=self.root/'offline.sqlite3'
        with self.ledger._connection() as source,sqlite3.connect(copy) as target:source.backup(target)
        value={'status':'COMMITTED','events':[{'isolated_synthetic':True,'text':'字'*400000}]}
        with sqlite3.connect(copy) as db:
            db.execute('INSERT INTO commands(rowid,command_id,verb,input_sha256,result,created_at) VALUES(?,?,?,?,?,?)',
                (999,'synthetic-gap','work_inputs','isolated',json.dumps(value,ensure_ascii=False),self.now))
        result=compact(copy)
        self.assertEqual(1,result['roundtrip_verified'])
        with sqlite3.connect(copy) as db:
            self.assertEqual(999,db.execute("SELECT rowid FROM commands WHERE command_id='synthetic-gap'").fetchone()[0])
            self.assertEqual(value,expand_result(None,json.loads(db.execute("SELECT result FROM commands WHERE command_id='synthetic-gap'").fetchone()[0])))


class MySQLPreparationCoverage(unittest.TestCase):
    def test_integration_preparation_failure_has_owner_and_no_test_publication(self):
        import tests.test_workflow_completion as fixtures
        from workflow_completion import verify
        from workflow_verification_exceptions import facts
        fixture=fixtures.CompletionTests('test_native_queue_is_automatic_idempotent_and_planner_stays_manual')
        fixture.setUp();self.addCleanup(fixture.tearDown)
        fixture.queue();_,tester=fixture.bind('TESTER')
        from tests.test_workflow_merge import CHECK
        import sys
        with mock.patch('workflow_supplemental.mysql_session',side_effect=ValueError('ISOLATED_PREPARATION_FAILURE')):
            result=verify(fixture.ledger,tester,{'profile':'mysql','report_format':'junit',
                'argv':[sys.executable,'-c',CHECK,'{junit_xml}','2']},command_id='integration-prepare-fails')
        self.assertEqual('UNVERIFIED',result['status'])
        item=facts(fixture.ledger.snapshot(),'run-one')[-1]
        self.assertEqual('integration_mysql',item['profile']);self.assertEqual('COORDINATOR',item['responsible_role'])
        self.assertFalse(any(e['kind']=='INTEGRATION_TEST_RECORDED' and e['work_id']==tester['work_id'] for e in fixture.ledger.snapshot()['events']))

    def test_supplemental_preparation_failure_keeps_existing_retirement_handoff(self):
        import tests.test_supplemental_verification as fixtures
        from workflow_supplemental import request,verify
        from workflow_verification_exceptions import facts
        fixture=fixtures.SupplementalVerificationTests('test_result_is_a_separate_check_and_does_not_reopen_completed_task')
        fixture.setUp();self.addCleanup(fixture.tearDown)
        fixture.assertCommitted(request(fixture.ledger,fixture.target,fixture.source,command_id='supplement-request'))
        _,tester=fixture.bind('TESTER')
        with mock.patch('workflow_supplemental.mysql_session',side_effect=ValueError('ISOLATED_PREPARATION_FAILURE')):
            result=verify(fixture.ledger,tester,command_id='supplement-prepare-fails')
        self.assertEqual('UNVERIFIED',result['status'])
        self.assertIsNotNone(result['exception_handoff']['retirement_event_id'])
        self.assertEqual('mysql_migration',facts(fixture.ledger.snapshot(),'mysql-run')[-1]['profile'])


class ProcessReceiptTests(unittest.TestCase):
    def test_short_process_does_not_replace_gateway_receipt_and_adoption_is_per_execution(self):
        from workflow_runtime_identity import record_activation,adopt
        with tempfile.TemporaryDirectory() as directory:
            home=Path(directory);legacy=home/'runtime/role-observation/mechanism-activation.json'
            legacy.parent.mkdir(parents=True);legacy.write_text('{"gateway_fixture":true}')
            with mock.patch('workflow_migration.code_version',return_value={'digest':'isolated-version','files':[]}):
                identity=record_activation(home,{'state':'ACTIVATED','source_sha256':{'synthetic.py':'isolated'}})
            self.assertEqual({'gateway_fixture':True},json.loads(legacy.read_text()))
            scope={'execution_id':'isolated-execution','work_id':'isolated-work','role':'A'}
            adopt(home,identity,scope,'request-1');adopt(home,identity,scope,'request-2')
            files=list((legacy.parent/'adoptions').glob('*.json'));self.assertEqual(1,len(files))
            row=json.loads(files[0].read_text())
            self.assertEqual(('request-1','request-2',2),(row['first_request_id'],row['latest_api_request_id'],row['request_hook_calls']))


class ProviderHealthTests(unittest.TestCase):
    def test_quota_cools_original_route_and_success_clears_without_business_verdict(self):
        with tempfile.TemporaryDirectory() as directory:
            result=record(directory,success=False,provider='isolated',model='fake',at=100,
                api_request_id='request-1',status_code=402,error_message='insufficient balance',scope={'work_id':'isolated'})
            self.assertTrue(result['business_result_unchanged'])
            self.assertIsNotNone(cooldown(directory,'isolated','fake',at=101))
            self.assertIsNone(cooldown(directory,'other','fake',at=101))
            self.assertIsNone(cooldown(directory,'isolated','fake',at=1001))
            record(directory,success=True,provider='isolated',model='fake',at=1002,api_request_id='request-2')
            self.assertIsNone(cooldown(directory,'isolated','fake',at=1003))
            self.assertIsNone(record(directory,success=False,retry_pending=True,api_request_id='retry'))
