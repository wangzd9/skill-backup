"""Real temporary gate receipts, rebuild and denial/recovery sequences; no egress."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT/'tests', Path('/home/wang/.hermes/hermes-agent')):
    sys.path.insert(0, str(directory))
from test_business_ledger import LedgerFixture
import workflow_diagnostics as diagnostics
import workflow_runtime as workflow


class WriteDiagnosticsTests(LedgerFixture):
    def setUp(self):
        super().setUp()
        self.pool = self.root/'isolated.json'
        self.pool.write_text('{}')
        self.wakeup = mock.patch.object(workflow, 'request_business_wakeups', return_value=None)
        self.wakeup.start()

    def tearDown(self):
        self.wakeup.stop()
        super().tearDown()

    def scope(self, identity):
        return {**identity, 'pool_path':str(self.pool), 'kind':'business', 'workflow_version':1}

    def active_receipts(self, role='A'):
        return diagnostics.read_projection(self.pool, role=role, include_receipts=True)['receipts']

    def test_actual_transaction_denial_survives_rollback_replay_and_conflict(self):
        _, identity = self.bind('A')
        bad = {**identity, 'generation':identity['generation']-1}
        before = self.ledger.snapshot()['event_high_water']
        result = self.ledger.checkpoint(bad, {'next':'must not commit'}, command_id='stale-write')
        self.assertEqual('REJECTED', result['status'])
        receipt = result['decision']
        self.assertEqual('EXPECTED_GATE_DENIAL', receipt['classification'])
        self.assertEqual('NONE', receipt['side_effect_state'])
        self.assertEqual('NOT_CHECKED', receipt['capture']['facts']['work']['check_status'])
        self.assertEqual(before, self.ledger.snapshot()['event_high_water'])
        count = len(self.active_receipts())
        repeated = self.ledger.checkpoint(bad, {'next':'must not commit'}, command_id='stale-write')
        self.assertEqual('DUPLICATE', repeated['status'])
        self.assertEqual('REJECTED', repeated['original_status'])
        self.assertEqual(receipt['decision_id'], repeated['decision']['decision_id'])
        self.assertTrue(repeated['decision']['replayed'])
        self.assertEqual(count, len(self.active_receipts()))
        conflict = self.ledger.checkpoint(identity, {'next':'different'}, command_id='stale-write')
        self.assertEqual('COMMAND_ID_CONFLICT', conflict['code'])
        self.assertEqual('BUSINESS_CONTRACT_REJECTION', conflict['decision']['classification'])
        with sqlite3.connect(self.path) as db:
            original = json.loads(db.execute('SELECT result FROM commands WHERE command_id=?', ('stale-write',)).fetchone()[0])
        self.assertEqual(receipt['decision_id'], original['decision']['decision_id'])

    def test_readonly_validation_uses_one_read_transaction_and_never_records_a_rejection(self):
        _, identity = self.bind('A')
        before = len(self.active_receipts())
        invalid = {**identity, 'execution_generation':identity['execution_generation']+1}
        result = self.ledger.validate_identity(invalid)
        self.assertFalse(result['valid'])
        self.assertIn('capture', result)
        self.assertEqual('OBSERVED', result['capture']['facts']['execution']['check_status'])
        self.assertEqual(before, len(self.active_receipts()))
        with self.assertRaises(workflow.GuardRejected) as raised:
            workflow.guard(self.scope(invalid), operation='checkpoint', capability='checkpoint')
        self.assertEqual(result['code'], str(raised.exception))
        self.assertEqual(result['code'], raised.exception.result['code'])
        self.assertEqual('TOOL_PREFLIGHT', raised.exception.decision['decision_phase'])
        self.assertEqual(before+1, len(self.active_receipts()))

    def test_nonpermission_unknown_results_do_not_become_write_authority_diagnostics(self):
        _, identity = self.bind('A')
        candidate = {'repo':str(self.root/'missing-git-repository'), 'base':self.base,
            'head':self.base, 'ac_version':'ac-sha256-frozen-v1',
            'evidence':[{'path':str(self.root/'missing-evidence')}]}
        result = self.ledger.deliver(identity, candidate, 'isolated candidate', command_id='git-unreadable')
        self.assertEqual('UNVERIFIED', result['status'])
        self.assertEqual('GIT_CANDIDATE_UNVERIFIED', result['code'])
        self.assertEqual('BUSINESS_CONTRACT_REJECTION', result['decision']['classification'])
        candidate['repo'] = str(self.repo)
        (self.repo/'app.py').write_text('def value():\n    return 1\n')
        self.git('add','app.py');self.git('commit','-qm','actual development delta')
        candidate['head']=self.git('rev-parse','HEAD')
        result = self.ledger.deliver(identity, candidate, 'isolated candidate', command_id='evidence-unreadable')
        self.assertEqual('EVIDENCE_UNREADABLE', result['code'])
        self.assertEqual('BUSINESS_CONTRACT_REJECTION', result['decision']['classification'])
        self.assertFalse(diagnostics.read_projection(self.pool, role='A')['anomalies'])
        capture = self.ledger.validate_identity(identity)['capture']
        self.assertEqual('BUSINESS_CONTRACT_REJECTION', diagnostics.classify(
            {'status':'REJECTED','code':'UNKNOWN_CANDIDATES'}, capture)[0])
        self.assertEqual('WRITE_AUTHORITY_UNVERIFIED', diagnostics.classify(
            {'status':'UNVERIFIED','code':'BUSINESS_SOURCE_UNAVAILABLE'}, capture,
            {'input_validated':True, 'capability_allowed':True})[0])
        administrative = self.ledger.select_run('missing-run', command_id='invalid-selection-input')
        self.assertEqual('UNKNOWN_RUNS', administrative['code'])
        self.assertEqual('BUSINESS_CONTRACT_REJECTION', administrative['decision']['classification'])

    def test_schema_unavailable_keeps_original_code_and_unverified_authority(self):
        _, identity = self.bind('A')
        with mock.patch.object(workflow, 'open_ledger', side_effect=ValueError('UNSUPPORTED_BUSINESS_SCHEMA')):
            with self.assertRaises(workflow.GuardRejected) as raised:
                workflow.guard(self.scope(identity), operation='checkpoint', capability='checkpoint')
        self.assertEqual('UNSUPPORTED_BUSINESS_SCHEMA', str(raised.exception))
        self.assertEqual('UNVERIFIED', raised.exception.result['status'])
        self.assertEqual('WRITE_AUTHORITY_UNVERIFIED', raised.exception.decision['classification'])

    def test_role_state_preserves_independent_reasons_despite_status_precedence(self):
        _, identity = self.bind('A')
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE role_current SET writer_execution_id='unverified-writer' WHERE role='A'")
            db.execute("UPDATE runs SET control='PAUSED' WHERE run_id=?",(identity['run_id'],))
            db.execute("UPDATE tasks SET unresolved_reason='isolated reconciliation gate' WHERE task_id=?",
                       (identity['task_id'],))
        projected = workflow.role_state(self.pool, 'A')
        self.assertEqual('UNKNOWN', projected['status'])
        self.assertEqual({'ROLE_OWNER_MISMATCH','CONTROL_BLOCKS_NEW_ACTION',
            'TASK_CONTROL_OR_RECONCILIATION_GATE'},set(projected['reason_codes']))
        self.assertEqual('UNVERIFIED', projected['business_authority_state']['state'])
        self.assertEqual('NOT_APPLICABLE', projected['heartbeat_state']['state'])

    def test_current_contradiction_multi_phase_duplicate_expected_and_real_recovery(self):
        _, identity = self.bind('A')
        scope = self.scope(identity)
        capture = self.ledger.validate_identity(identity)['capture']
        with diagnostics.operation_context(scope, operation='checkpoint', capability='checkpoint',
                operation_id='one-operation', input_validated=True, capability_allowed=True):
            first = diagnostics.record_result(self.path,
                {'status':'REJECTED', 'code':'ROLE_OWNER_MISMATCH'}, capture,
                phase='TOOL_PREFLIGHT', side_effect_state='NONE', at=self.now)
            diagnostics.record_result(self.path, {'status':'REJECTED', 'code':'ROLE_OWNER_MISMATCH'},
                capture, phase='TRANSACTION_GATE', side_effect_state='NONE', at=self.now+1)
        projected = diagnostics.read_projection(self.pool, role='A')
        anomaly = next(row for row in projected['write_paths'] if row['capability']=='checkpoint')
        self.assertEqual('WRITE_AUTHORITY_ANOMALY', anomaly['write_path_state'])
        self.assertEqual(1, anomaly['operation_count'])
        duplicate = {**first, 'status':'DUPLICATE', 'original_status':'REJECTED'}
        self.assertEqual(anomaly, diagnostics.write_path_step(anomaly, duplicate))
        with diagnostics.operation_context(scope, operation='checkpoint', capability='checkpoint'):
            expected = diagnostics.build_receipt(self.path,
                {'status':'REJECTED', 'code':'CONTROL_BLOCKS_NEW_ACTION'}, capture, at=self.now+2)
        self.assertEqual(anomaly, diagnostics.write_path_step(anomaly, expected))
        self.now += 10
        # A real committed checkpoint with another capability cannot recover it.
        with diagnostics.operation_context(scope, operation='checkpoint', capability='other_capability'):
            self.assertCommitted(self.ledger.checkpoint(identity, {'next':'different capability'}, command_id='other-success'))
        self.assertTrue(diagnostics.read_projection(self.pool, role='A')['anomalies'])
        with diagnostics.operation_context(scope, operation='checkpoint', capability='checkpoint'):
            self.assertCommitted(self.ledger.checkpoint(identity, {'next':'actual business write'}, command_id='real-success'))
        recovered = next(row for row in diagnostics.read_projection(self.pool, role='A')['write_paths'] if row['capability']=='checkpoint')
        self.assertEqual('RECOVERED', recovered['write_path_state'])
        self.assertEqual('NEW_BUSINESS_WRITE_SUCCEEDED', recovered['closure'])
        self.assertEqual(recovered, diagnostics.write_path_step(recovered, first))

    def test_termination_closes_without_claiming_write_recovery(self):
        _, identity = self.bind('A')
        scope = self.scope(identity)
        with diagnostics.operation_context(scope, operation='checkpoint', capability='checkpoint'):
            diagnostics.record_result(self.path, {'status':'UNVERIFIED','code':'BUSINESS_SOURCE_UNAVAILABLE'})
        closed = diagnostics.close_terminated(self.pool, identity,
            evidence_ref={'terminal_verified':True, 'source':'isolated end event'}, at=self.now)
        self.assertEqual('CLOSED_WITH_TERMINATION', closed['state'])
        path = next(row for row in diagnostics.read_projection(self.pool,role='A')['write_paths'] if row['capability']=='checkpoint')
        self.assertEqual('CLOSED_WITH_TERMINATION', path['write_path_state'])
        self.assertIsNone(path['last_success_at'])
        self.assertTrue(self.ledger.validate_identity(identity)['valid'])
        projected = diagnostics.read_projection(self.pool, role='A')
        self.assertEqual('closed_with_execution', projected['closures'][0]['transition'])
        self.assertEqual(diagnostics.VERSION, projected['closures'][0]['rule_version'])
        with diagnostics.operation_context(scope, operation='checkpoint', capability='checkpoint'):
            delayed = diagnostics.record_result(self.path,
                {'status':'UNVERIFIED','code':'BUSINESS_SOURCE_UNAVAILABLE'}, at=self.now+1)
        self.assertEqual(path, diagnostics.write_path_step(path, delayed))
        with sqlite3.connect(diagnostics.store_path(self.pool)) as db:
            self.assertEqual(0,db.execute('SELECT COUNT(*) FROM pending').fetchone()[0])

    def test_termination_projection_failure_keeps_error_stage(self):
        _, identity = self.bind('A')
        with diagnostics.operation_context(self.scope(identity), operation='checkpoint', capability='checkpoint'):
            diagnostics.record_result(self.path, {'status':'UNVERIFIED','code':'BUSINESS_SOURCE_UNAVAILABLE'})
        with mock.patch.object(diagnostics.sqlite3, 'connect', side_effect=sqlite3.OperationalError('offline source')):
            result = diagnostics.close_terminated(self.pool, identity,
                evidence_ref={'terminal_verified':True}, at=self.now)
        self.assertEqual('DEGRADED', result['state'])
        self.assertEqual('OperationalError', result['error'])
        self.assertEqual('close_terminated', result['error_stage'])

    def test_receipt_pages_and_pending_lookup_do_not_read_entire_history(self):
        _, identity = self.bind('A')
        scope = self.scope(identity)
        with diagnostics.operation_context(scope, operation='checkpoint', capability='checkpoint'):
            for index in range(12):
                diagnostics.record_result(self.path, {'status':'REJECTED','code':'PARAMETER_CONFLICT'},
                    command_id='history-'+str(index))
            diagnostics.record_result(self.path, {'status':'UNVERIFIED','code':'BUSINESS_SOURCE_UNAVAILABLE'})
        page = diagnostics.query_receipts(self.pool,role='A',limit=3)
        self.assertEqual(3,len(page['receipts']))
        self.assertTrue(page['has_more'])
        self.assertEqual([],diagnostics.read_projection(self.pool)['receipts'])
        observed = diagnostics.observe(self.pool,budget_seconds=1)
        self.assertEqual(1,observed['pending_rows_read'])
        self.assertEqual(1,len(observed['verification']))

    def test_active_incident_closure_lookup_pages_beyond_recent_display_window(self):
        from workflow_observation import step
        _, identity = self.bind('A')
        for index in range(130):
            with diagnostics.operation_context(self.scope(identity),operation='checkpoint',
                    capability='capability-'+str(index)):
                diagnostics.record_result(self.path,
                    {'status':'UNVERIFIED','code':'BUSINESS_SOURCE_UNAVAILABLE'},at=self.now)
        active = diagnostics.read_projection(self.pool)['anomalies']
        prior, _ = step({}, {'anomalies':active},self.now)
        diagnostics.close_terminated(self.pool,identity,evidence_ref={'terminal_verified':True},at=self.now+1)
        display = diagnostics.read_projection(self.pool)
        self.assertEqual(64,len(display['closures']))
        first = diagnostics.query_closures(self.pool,active,limit=128)
        self.assertEqual(128,first['queried'])
        self.assertTrue(first['has_more'])
        second = diagnostics.query_closures(self.pool,active,after=first['next_after'],limit=128)
        self.assertEqual(2,second['queried'])
        self.assertFalse(second['has_more'])
        remaining, outputs = step(prior,{'observation_closures':first['closures']+second['closures']},self.now+1)
        self.assertEqual({},remaining['active'])
        self.assertEqual(130,len(outputs))
        self.assertTrue(all(row['transition']=='closed_with_execution' for row in outputs))

    def test_projection_failure_keeps_original_decision_and_rebuilds_from_commands(self):
        _, identity = self.bind('A')
        with mock.patch.object(diagnostics, 'publish_receipt', side_effect=lambda _path, receipt: receipt):
            result = self.ledger.checkpoint(identity, {'next':'real saved checkpoint'}, command_id='cache-was-down')
        self.assertCommitted(result)
        self.assertFalse(any(row.get('command_id')=='cache-was-down' for row in self.active_receipts()))
        rebuilt = diagnostics.rebuild_projection(self.pool, budget_seconds=2)
        self.assertEqual('OBSERVED', rebuilt['state'])
        self.assertTrue(any(row.get('command_id')=='cache-was-down' for row in self.active_receipts()))
        # A failed cache never changes a business denial into an allowed result.
        with mock.patch.object(diagnostics.sqlite3, 'connect', side_effect=sqlite3.OperationalError('offline source')):
            receipt = diagnostics.publish_receipt(self.path,
                diagnostics.build_receipt(self.path, {'status':'REJECTED','code':'ROLE_OWNER_MISMATCH'}))
        self.assertEqual('DEGRADED', receipt['projection_state'])
        self.assertEqual('REJECTED', receipt['status'])

    def test_pending_current_supplement_never_backfills_history_or_grants_authority(self):
        _, identity = self.bind('A')
        with diagnostics.operation_context(self.scope(identity), operation='terminal', capability='workspace_write'):
            receipt = diagnostics.record_result(self.path,
                {'status':'UNVERIFIED','code':'BUSINESS_SOURCE_UNAVAILABLE'}, at=self.now)
        self.assertEqual('WRITE_AUTHORITY_UNVERIFIED', receipt['classification'])
        observed = diagnostics.observe(self.pool, now=self.now+60, budget_seconds=1)
        self.assertEqual('HISTORICAL_UNRESOLVED', observed['verification'][0]['state'])
        self.assertTrue(observed['verification'][0]['current_valid'])
        self.assertEqual('WRITE_AUTHORITY_UNVERIFIED', next(row for row in observed['write_paths']
            if row['capability']=='workspace_write')['write_path_state'])
        with mock.patch('business_ledger.BusinessLedger.validate_identity') as validate:
            exhausted = diagnostics.observe(self.pool, now=self.now+120, budget_seconds=0)
        validate.assert_not_called()
        self.assertEqual('BUDGET_EXHAUSTED', exhausted['verification'][0]['state'])
        self.assertEqual('LOCAL_COLLECTION_BUDGET_EXHAUSTED', exhausted['verification'][0]['reason'])
        self.assertEqual(120, exhausted['verification'][0]['pending_age_seconds'])

    def test_no_payload_or_command_arguments_in_receipts_and_missing_store_is_not_created(self):
        missing = self.root/'absent.json'
        result = diagnostics.read_projection(missing)
        self.assertEqual('NOT_COLLECTED', result['source_state'])
        self.assertFalse(missing.with_suffix('.runtime').exists())
        _, identity = self.bind('A')
        secret = 'do-not-persist-prompt-or-shell-secret'
        self.ledger.checkpoint(identity, {'next':secret}, command_id='safe-correlation-id')
        self.assertNotIn(secret, json.dumps(self.active_receipts()))

    def test_sandbox_revocation_after_launch_records_possible_side_effects_and_one_operation(self):
        import workflow_sandbox
        _, identity = self.bind('A')
        scope = self.scope(identity)
        destination = self.path.parent/'workspaces'/identity['work_id']
        destination.parent.mkdir(parents=True)
        self.git('clone','-q','--shared',str(self.repo),str(destination))
        scope['worktree'] = str(destination)
        child = mock.Mock()
        child.poll.return_value = None
        child.wait.return_value = 0
        def launch(_command):
            with sqlite3.connect(self.path) as db:
                db.execute("UPDATE runs SET control='PAUSED' WHERE run_id=?", (identity['run_id'],))
            return child
        with mock.patch.object(workflow_sandbox.os,'getsid',return_value=os.getpid()), \
                mock.patch.object(workflow_sandbox.subprocess,'check_output',return_value=str(self.repo/'.git')), \
                mock.patch.object(workflow_sandbox.subprocess,'Popen',side_effect=launch):
            with self.assertRaises(workflow.GuardRejected) as raised:
                workflow_sandbox.run({'scope':scope,'command':'not actually executed', 'cwd':str(destination),
                    'diagnostic_operation':{'operation_id':'sandbox-operation'}})
        receipt = raised.exception.decision
        self.assertEqual('POSSIBLE', receipt['side_effect_state'])
        self.assertEqual('SANDBOX_REAUTHORIZATION', receipt['decision_phase'])
        self.assertEqual('EXPECTED_GATE_DENIAL', receipt['classification'])
        self.assertEqual('sandbox-operation', receipt['operation_id'])
        child.terminate.assert_called_once()


if __name__ == '__main__':
    import unittest
    unittest.main()
