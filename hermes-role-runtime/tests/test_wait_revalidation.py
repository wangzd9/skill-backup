"""Real ledger/Git regressions for stale waits and restricted coordinator writes."""
import json
import sys
from unittest import mock

from test_business_ledger import LedgerFixture
import workflow_runtime as workflow
from workflow_coordination_log import append


class WaitRevalidationTests(LedgerFixture):
    def wait(self, checkpoint=None):
        process, coord = self.bind('COORDINATOR')
        page = self.ledger.work_inputs(coord, command_id='wait-input')
        self.assertCommitted(self.ledger.checkpoint(coord,
            checkpoint or {'waiting_external': ['主仓工作区/外部授权待复查']},
            processed_through=page['through'], yield_execution=True, command_id='wait'))
        self.close(process, coord)
        return coord

    def test_wait_expires_readonly_without_event_or_control_and_real_gates_remain(self):
        self.wait()
        before = self.ledger.snapshot()
        self.assertFalse(self.ledger.admission_hint('COORDINATOR')['can_start_model'])
        self.now += 900
        hint = self.ledger.admission_hint('COORDINATOR')
        self.assertTrue(hint['can_start_model'])
        self.assertEqual('WAIT_RECHECK_DUE', hint['reason'])
        self.assertEqual(before, self.ledger.snapshot())
        # Admission is a recheck opportunity, never permission to skip a gate.
        process, coord = self.bind('COORDINATOR')
        self.assertEqual('WRITER_FENCE_PRESENT', self.ledger.admission_hint('COORDINATOR')['reason'])
        self.assertEqual('UNKNOWN_CANDIDATES', self.ledger.merge_readiness(coord,'missing','missing')['code'])
        self.close(process, coord)

    def test_dirty_to_clean_and_branch_change_wake_before_deadline(self):
        (self.repo/'external.txt').write_text('preserve other session work')
        self.wait()
        self.assertFalse(self.ledger.admission_hint('COORDINATOR')['can_start_model'])
        (self.repo/'external.txt').write_text('more in-flight work')
        self.assertFalse(self.ledger.admission_hint('COORDINATOR')['can_start_model'])
        (self.repo/'external.txt').unlink()
        hint = self.ledger.admission_hint('COORDINATOR')
        self.assertEqual('WAIT_REPOSITORY_CHANGED', hint['reason'])

    def test_legacy_wait_without_observation_rechecks_and_pause_still_wins(self):
        coord=self.wait()
        with self.ledger._connection() as db:
            row=db.execute("SELECT event_id,payload FROM events WHERE kind='CHECKPOINT_SAVED' ORDER BY event_id DESC LIMIT 1").fetchone()
            payload=json.loads(row['payload']);payload.pop('wait_observation')
            db.execute('UPDATE events SET payload=? WHERE event_id=?',(json.dumps(payload),row['event_id']))
            db.commit()
        self.now += 901
        self.assertTrue(self.ledger.admission_hint('COORDINATOR')['can_start_model'])
        with self.ledger._connection() as db:
            db.execute("UPDATE runs SET control='PAUSED' WHERE run_id='run-one'"); db.commit()
        self.assertEqual('PAUSED_OR_CANCELLED',self.ledger.admission_hint('COORDINATOR')['reason'])

    def test_new_wait_checkpoint_resets_deadline_without_clearing_dependency(self):
        self.wait();self.now += 901
        self.assertTrue(self.ledger.admission_hint('COORDINATOR')['can_start_model'])
        process,coord=self.bind('COORDINATOR')
        self.assertCommitted(self.ledger.checkpoint(coord,{'waiting_external':['仍缺真实外部授权']},
            yield_execution=True,command_id='checked-again'))
        self.close(process,coord)
        self.assertFalse(self.ledger.admission_hint('COORDINATOR')['can_start_model'])

    def test_capabilities_and_admission_visible_to_existing_readonly_tool(self):
        from workflow_query import live_progress
        self.wait()
        with mock.patch('workflow_runtime.open_ledger',return_value=self.ledger):
            result=live_progress(self.root/'isolated.json','COORDINATOR')
        self.assertFalse(result['runtime_capabilities']['ordinary_terminal'])
        self.assertEqual('WAITING_EXTERNAL_WITHOUT_NEW_BUSINESS_EVENT',result['model_admission']['COORDINATOR']['reason'])

    def test_large_wait_contract_does_not_break_live_progress_byte_budget(self):
        from workflow_query import live_progress
        self.wait({'waiting_external': ['外部依赖'+str(i)+'x'*450 for i in range(32)]})
        with mock.patch('workflow_runtime.open_ledger',return_value=self.ledger):
            result=live_progress(self.root/'isolated.json','COORDINATOR')
        self.assertLess(len(json.dumps(result,ensure_ascii=False).encode()),8192)
        manifest=json.loads(__import__('pathlib').Path(result['artifacts']['manifest']['path']).read_text())
        detail=json.loads(__import__('pathlib').Path(manifest['model_admission']['path']).read_text())
        self.assertEqual(32,len(detail['COORDINATOR']['waiting_external']))


class CoordinatorLogTests(LedgerFixture):
    def test_native_command_appends_once_and_rejects_arbitrary_path(self):
        _,coord=self.bind('COORDINATOR')
        scope={'pool_path':str(self.root/'isolated.json'),'kind':'business','role':'COORDINATOR','run_id':'run-one',
               'work_id':coord['work_id'],**coord}
        with mock.patch('workflow_runtime.open_ledger',return_value=self.ledger):
            result=workflow.command(scope,'append_development_log',{'content':'已核对真实批准与合入门禁'},command_id='milestone')
            self.assertCommitted(result)
            replay=workflow.command(scope,'append_development_log',{'content':'已核对真实批准与合入门禁'},command_id='milestone')
            self.assertEqual('DUPLICATE',replay['status'])
            with self.assertRaisesRegex(ValueError,'INVALID_COMMAND_PAYLOAD'):
                workflow.command(scope,'append_development_log',{'content':'bad','path':'/tmp/bad'},command_id='bad')
        self.assertEqual(1,(self.root/'DEVELOPMENT-LOG.md').read_text().count('已核对真实批准与合入门禁'))

    def test_no_developer_or_symlink_write_and_crash_replay_is_idempotent(self):
        _,dev=self.bind('A')
        self.assertEqual('COORDINATOR_REQUIRED',append(self.ledger,dev,'bad',command_id='dev')['code'])
        _,coord=self.bind('COORDINATOR')
        outside=self.root/'external.md';outside.write_text('preserve')
        path=self.root/'DEVELOPMENT-LOG.md';path.symlink_to(outside)
        self.assertEqual('DEVELOPMENT_LOG_UNAVAILABLE',append(self.ledger,coord,'bad',command_id='link')['code'])
        self.assertEqual('preserve',outside.read_text());path.unlink()
        self.assertCommitted(append(self.ledger,coord,'milestone',command_id='crash'))
        with self.ledger._connection() as db:
            db.execute("DELETE FROM commands WHERE command_id='crash'");db.commit()
        self.assertCommitted(append(self.ledger,coord,'milestone',command_id='crash'))
        self.assertEqual(1,path.read_text().count('milestone'))
