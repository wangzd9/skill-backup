"""A document registration must not silently authorize, restart or duplicate work."""
import hashlib
import json
from pathlib import Path

from test_business_ledger import LedgerFixture
from workflow_intake import accept
from workflow_registration import packages, register_packages


class RegistrationTests(LedgerFixture):
    def setUp(self):
        super().setUp()
        self.package = self.root / 'new-package'
        self.package.mkdir()
        (self.package / 'TODO.md').write_text('NEW-01: fixed local AC; NEW-02: optional next stage')
        self.entry = {'requirement_id': 'new-package', 'requirement_root': str(self.package),
            'repo': str(self.repo), 'kind': 'development', 'task_ids': ['NEW-01', 'NEW-02'],
            'contract_files': ['TODO.md'], 'requires_explicit_task_ids': False}
        self.source = {'purpose': 'registration_only', 'channel': 'IDE', 'primary_text': '整理需求文档并完成注册'}

    def register(self, *entries, command_id='register-packages'):
        return register_packages(self.ledger, list(entries or [self.entry]), self.source, command_id=command_id)

    def target(self, tasks=None):
        return {k: self.entry[k] for k in ('requirement_id', 'requirement_root', 'repo', 'contract_files')} | {
            'task_ids': tasks or ['NEW-01']}

    def native(self, text=None, message='native-develop'):
        return {'authenticated': True, 'current_full': False, 'message_id': message, 'user_id': 'user',
                'primary_text': text or '开发 new-package，自动推进；不要部署生产。'}

    def test_registration_is_paused_atomic_audited_and_idempotent_without_grants(self):
        before = self.ledger.snapshot()
        result = self.assertCommitted(self.register())
        self.assertEqual('DUPLICATE', self.register()['status'])
        snap = self.ledger.snapshot()
        self.assertEqual(before['current_run_id'], snap['current_run_id'])
        self.assertEqual(before['roles'], snap['roles'])
        self.assertEqual(before['tasks'], snap['tasks'])
        self.assertEqual(before['authorizations'], snap['authorizations'])
        item = packages(self.ledger)['new-package']
        self.assertEqual('PAUSED', item['run_control'])
        self.assertEqual(2, len(snap['runs']))
        self.assertEqual(1, len(result['packages']))
        raw = (self.package / 'TODO.md').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), item['sources'][0]['sha256'])
        self.assertEqual(raw, Path(item['sources'][0]['frozen_path']).read_bytes())

    def test_real_native_intake_reuses_and_narrows_prepared_run_without_duplicate(self):
        self.assertCommitted(self.register())
        result = self.assertCommitted(accept(self.ledger, self.target(), self.native(), command_id='start-new'))
        self.assertEqual('prepared-new-package', result['run_id'])
        self.assertEqual('DUPLICATE', accept(self.ledger, self.target(), self.native(), command_id='start-new')['status'])
        snapshot = self.ledger.snapshot()
        self.assertEqual(2, len(snapshot['runs']))
        self.assertEqual('prepared-new-package', snapshot['current_run_id'])
        registered = next(r for r in snapshot['runs'] if r['run_id'] == 'prepared-new-package')
        self.assertEqual(['NEW-01'], registered['task_ids'])
        self.assertEqual('ACTIVE', registered['control'])
        self.assertEqual(['NEW-01'], snapshot['authorizations'][0]['spec']['task_ids'])
        denied = accept(self.ledger, self.target(), self.native(message='duplicate-new'), command_id='start-again')
        self.assertEqual('REGISTERED_RUN_ALREADY_ACCEPTED_USE_EXISTING_RUN_CONTROL', denied['code'])

    def test_reference_package_registers_without_fake_run_and_cannot_autodevelop(self):
        reference = dict(self.entry, kind='reference', task_ids=[])
        self.assertCommitted(self.register(reference))
        self.assertEqual(1, len(self.ledger.snapshot()['runs']))
        self.assertIsNone(packages(self.ledger)['new-package']['run_id'])
        with self.assertRaisesRegex(ValueError, 'REFERENCE_REQUIREMENT_NEEDS_EXPLICIT_DELIVERY_CONTRACT'):
            accept(self.ledger, self.target(), self.native(), command_id='start-reference')

    def test_selection_or_resuming_placeholder_cannot_bypass_native_acceptance(self):
        self.assertCommitted(self.register())
        result=self.ledger.select_run('prepared-new-package',command_id='select-unaccepted')
        self.assertEqual('REGISTERED_REQUIREMENT_NOT_ACCEPTED',result['code'])
        self.assertEqual('run-one',self.ledger.mode()['current_run_id'])
        # Even an inconsistent persisted control pointer must not admit a model/writer.
        with self.ledger._connection() as db:
            db.execute("UPDATE authority SET current_run_id='prepared-new-package'")
            db.execute("UPDATE runs SET control='ACTIVE' WHERE run_id='prepared-new-package'")
            db.commit()
        self.assertEqual('REGISTERED_REQUIREMENT_NOT_ACCEPTED',self.ledger.admission_hint('COORDINATOR')['reason'])
        _,spec=self.worker()
        denied=self.ledger.bind_execution('COORDINATOR',spec,command_id='bind-unaccepted')
        self.assertEqual('REGISTERED_REQUIREMENT_NOT_ACCEPTED',denied['code'])

    def test_historical_tasks_require_current_user_scope_not_all_old_done_tasks(self):
        self.assertCommitted(self.register(dict(self.entry, requires_explicit_task_ids=True)))
        with self.assertRaisesRegex(ValueError, 'HISTORICAL_REQUIREMENT_NEEDS_EXPLICIT_CURRENT_TASK_SCOPE'):
            accept(self.ledger, self.target(), self.native(), command_id='implicit-all')
        self.assertCommitted(accept(self.ledger, self.target(),
            self.native('开发 new-package，仅推进 NEW-01，自动推进。'), command_id='explicit-one'))

    def test_changed_or_omitted_contract_and_expanded_tasks_do_not_unpause(self):
        self.assertCommitted(self.register())
        with self.assertRaisesRegex(ValueError, 'REGISTERED_RUN_ID_MISMATCH'):
            accept(self.ledger, dict(self.target(),run_id='new-duplicate-bypass'),
                   self.native(), command_id='duplicate-bypass')
        with self.assertRaisesRegex(ValueError, 'REGISTERED_CONTRACT_FILES_REQUIRED'):
            accept(self.ledger, dict(self.target(), contract_files=[], use_native_requirement=True),
                   self.native(), command_id='omit-contract')
        (self.package / 'OTHER.md').write_text('OUTSIDE: not in this registration')
        with self.assertRaisesRegex(ValueError, 'TASK_OUTSIDE_REGISTERED_REQUIREMENT'):
            accept(self.ledger, dict(self.target(['OUTSIDE']), contract_files=['TODO.md', 'OTHER.md']),
                   self.native(), command_id='expand-contract')
        (self.package / 'TODO.md').write_text('NEW-01: changed criteria')
        with self.assertRaisesRegex(ValueError, 'REGISTERED_CONTRACT_CHANGED_RE_REGISTER_REQUIRED'):
            accept(self.ledger, self.target(), self.native(), command_id='changed-contract')
        self.assertEqual('PAUSED', packages(self.ledger)['new-package']['run_control'])

    def test_existing_run_and_writers_are_linked_not_recreated_or_reset(self):
        process, dev = self.bind('A')
        old = self.ledger.current_work('A')
        existing = dict(self.entry, requirement_id='requirement', requirement_root=str(self.root),
                        contract_files=['contract.md'], task_ids=['T1'])
        (self.root / 'contract.md').write_text('T1: historical task')
        self.assertCommitted(self.register(existing))
        item = packages(self.ledger)['requirement']
        self.assertEqual('run-one', item['run_id'])
        self.assertFalse(item['prepared_run'])
        self.assertEqual(old, self.ledger.current_work('A'))
        self.assertTrue(self.ledger.validate_identity(dev)['valid'])
        self.assertEqual(1, len(self.ledger.snapshot()['runs']))

    def test_bad_second_package_rolls_back_first_registration(self):
        conflicting = dict(self.entry, requirement_id='requirement')
        result = self.register(self.entry, conflicting)
        self.assertEqual('EXISTING_REQUIREMENT_SCOPE_MISMATCH', result['code'])
        self.assertEqual(1, len(self.ledger.snapshot()['runs']))
        self.assertEqual({}, packages(self.ledger))

    def test_catalog_exposes_registration_to_role_readers_without_mutating_state(self):
        from requirement_runtime import list_requirements
        from business_ledger import ROLES
        (self.package/'README.md').write_text('registered requirement')
        (self.root/'README.md').write_text('| [New package](new-package/README.md) | goal |')
        pool=self.root/'isolated.json'
        pool.write_text(json.dumps({'schema_version':1,'pool_id':'test','generation':1,'control':'ACTIVE',
            'catalog_path':str(self.root/'README.md'),'registry_path':str(self.root/'registry.json'),
            'roles':{r:{'chat_id':'chat-'+r,'poll_job_id':'poll-'+r,'skill':'test'} for r in ROLES}}))
        self.assertCommitted(self.register())
        before=self.ledger.snapshot()
        row=list_requirements(pool)['requirements'][0]
        self.assertEqual('prepared-new-package',row['registration']['run_id'])
        self.assertEqual('PAUSED',row['registration']['run_control'])
        self.assertEqual('business.sqlite3',row['registration']['authority'])
        self.assertEqual(before,self.ledger.snapshot())
