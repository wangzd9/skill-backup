"""Switch coordination at a real writer-safe boundary, preserving old work."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
from unittest.mock import patch

from tests.test_business_ledger import LedgerFixture
from workflow_runtime import current_input


class RunSwitchHandoffTests(LedgerFixture):
    def new_run(self, name='run-two'):
        self.assertCommitted(self.ledger.create_run(name, self.root, self.repo, ['NEW'],
            run_id=name, command_id='create-'+name))
        return name

    def test_idle_coordinator_released_in_select_transaction_without_touching_developer(self):
        worker, old = self.bind('COORDINATOR')
        self.assertCommitted(self.ledger.checkpoint(old, {'evidence': 'retained history'}, command_id='cp'))
        self.close(worker, old)
        dev, developer = self.bind('A')
        self.new_run()
        result = self.assertCommitted(self.ledger.select_run('run-two', command_id='switch'))
        self.assertEqual(old['work_id'], result['released_coordination'][0]['work_id'])
        self.assertIsNone(self.ledger.current_work('COORDINATOR'))
        self.assertEqual(developer['work_id'], self.ledger.current_work('A')['work_id'])
        self.assertTrue(self.ledger.validate_identity(developer, allow_drain=True)['valid'])
        _, new = self.bind('COORDINATOR')
        self.assertEqual('run-two', new['run_id'])
        self.assertCommitted(self.ledger.publish_plan('run-two', 1, [self.task('NEW', 'B')],
            identity=new, command_id='new-plan'))
        ended = next(w for w in self.ledger.snapshot()['works'] if w['work_id'] == old['work_id'])
        self.assertEqual('ENDED', ended['state'])
        self.assertEqual('retained history', ended['checkpoint']['evidence'])

    def test_active_writer_gets_safe_release_guidance_then_new_work_can_plan(self):
        worker, old = self.bind('COORDINATOR')
        self.new_run(); self.assertCommitted(self.ledger.select_run('run-two', command_id='switch'))
        self.assertEqual(old['work_id'], self.ledger.current_work('COORDINATOR')['work_id'])
        self.assertEqual('ACTIVE', next(e for e in self.ledger.snapshot()['executions']
                         if e['execution_id'] == old['execution_id'])['state'])
        packet = current_input(self.root/'isolated.json', 'COORDINATOR')
        self.assertEqual('run-two', packet['selected_run_id'])
        self.assertEqual('run-one', packet['run']['run_id'])
        self.assertEqual(['safe_release'], [a['action'] for a in packet['next_actions']])
        from workflow_query import live_progress
        view = live_progress(self.root/'isolated.json', 'COORDINATOR')
        self.assertIn('RUN_SWITCH_HANDOFF_REQUIRED', [g['code'] for g in view['open_gates']])
        self.assertCommitted(self.ledger.release_work(old, reason='RUN_SWITCHED', command_id='release'))
        contender, spec = self.worker()
        self.assertEqual('OLD_WRITER_NOT_CLEARED',
            self.ledger.bind_execution('COORDINATOR', spec, command_id='too-soon')['code'])
        self.close(worker, old)
        result = self.assertCommitted(self.ledger.bind_execution('COORDINATOR', spec, command_id='after-exit'))
        self.assertFalse(result['drain_only'])
        self.assertEqual('run-two', result['identity']['run_id'])
        self.assertCommitted(self.ledger.publish_plan('run-two', 1, [self.task('NEW','B')],
            identity=result['identity'], command_id='new-plan'))

    def test_active_old_coordinator_auto_retires_after_verified_native_exit(self):
        worker, old = self.bind('COORDINATOR')
        self.new_run(); self.assertCommitted(self.ledger.select_run('run-two', command_id='switch'))
        denied = self.ledger.end_execution(old, command_id='alive')
        self.assertEqual('WRITER_STILL_ALIVE', denied['code'])
        self.assertIsNotNone(self.ledger.current_work('COORDINATOR'))
        result = self.close(worker, old)
        self.assertEqual(old['work_id'], result['released_coordination'][0]['work_id'])
        self.assertIsNone(self.ledger.current_work('COORDINATOR'))
        _, new = self.bind('COORDINATOR')
        self.assertEqual('run-two', new['run_id'])

    def test_preexisting_idle_stale_work_reconciles_on_bind_and_preserves_old_pause(self):
        worker, old = self.bind('COORDINATOR'); self.close(worker, old)
        self.new_run()
        # Reproduce the exact pre-fix selection state, without running new _select.
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE authority SET current_run_id='run-two'")
            db.execute("UPDATE runs SET control='PAUSED' WHERE run_id='run-one'")
        _, spec = self.worker()
        result = self.assertCommitted(self.ledger.bind_execution('COORDINATOR', spec, command_id='repair-bind'))
        self.assertFalse(result['drain_only'])
        self.assertEqual('run-two', result['work']['run_id'])
        self.assertEqual('PAUSED', next(r for r in self.ledger.snapshot()['runs'] if r['run_id']=='run-one')['control'])

    def test_unknown_writer_and_child_keep_fence_until_verified_ended(self):
        worker, old = self.bind('COORDINATOR')
        self.new_run(); self.assertCommitted(self.ledger.select_run('run-two', command_id='switch'))
        from business_ledger import _deny
        with patch.object(self.ledger, '_writer_clear', side_effect=lambda e: _deny('CHILD_SCOPE_UNVERIFIED', unknown=True)):
            result = self.ledger.end_execution(old, command_id='unknown-child')
        self.assertEqual('UNVERIFIED', result['status'])
        self.assertEqual(old['work_id'], self.ledger.current_work('COORDINATOR')['work_id'])
        self.assertEqual([], [e for e in self.ledger.events() if e['kind']=='WORK_RELEASED'])

    def test_paused_destination_does_not_start_model_and_repeat_switch_is_idempotent(self):
        worker, old = self.bind('COORDINATOR'); self.close(worker, old)
        self.new_run()
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE runs SET control='PAUSED' WHERE run_id='run-two'")
        self.assertCommitted(self.ledger.select_run('run-two', command_id='switch'))
        self.assertFalse(self.ledger.admission_hint('COORDINATOR')['can_start_model'])
        self.assertCommitted(self.ledger.select_run('run-two', command_id='switch-repeat'))
        self.assertEqual(1, len([e for e in self.ledger.events() if e['kind']=='WORK_RELEASED']))

    def test_missing_pointer_does_not_hide_an_unfinished_execution(self):
        worker, old = self.bind('COORDINATOR')
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE role_current SET writer_execution_id=NULL WHERE role='COORDINATOR'")
            db.execute('UPDATE works SET active_execution_id=NULL WHERE work_id=?', (old['work_id'],))
        self.new_run()
        result = self.assertCommitted(self.ledger.select_run('run-two', command_id='switch'))
        self.assertEqual([], result['released_coordination'])
        _, spec = self.worker()
        result = self.ledger.bind_execution('COORDINATOR', spec, command_id='no-takeover')
        self.assertEqual('UNVERIFIED', result['status'])
        self.assertEqual('PREVIOUS_WORK_WRITER_UNVERIFIED', result['code'])

    def test_two_competing_new_coordinator_binds_have_one_writer(self):
        worker, old = self.bind('COORDINATOR'); self.close(worker, old)
        self.new_run(); self.assertCommitted(self.ledger.select_run('run-two', command_id='switch'))
        specs = [self.worker()[1], self.worker()[1]]
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda spec: self.ledger.bind_execution('COORDINATOR', spec,
                command_id='race-'+spec['execution_id']), specs))
        self.assertEqual(1, sum(r['status']=='COMMITTED' for r in results))
        current = self.ledger.current_work('COORDINATOR')
        self.assertEqual('run-two', current['run_id'])
        self.assertEqual(1, len([e for e in self.ledger.snapshot()['executions'] if e['state']=='ACTIVE']))
