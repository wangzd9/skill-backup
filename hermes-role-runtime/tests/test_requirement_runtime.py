"""Isolated catalog, authorization and crash recovery tests; no real scheduling."""
import concurrent.futures
from contextlib import contextmanager
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requirement_runtime as runtime

INDEPENDENT_PROBE = runtime._independent_blockers


class FakeScheduler:
    def __init__(self, pool):
        self.jobs = {identifier: {'id': identifier, 'enabled': False if identifier.endswith('-report') else True,
            'state': 'paused' if identifier.endswith('-report') else 'active',
            'schedule': {'type': 'cron', 'expression': '*/5 * * * *'},
            'origin': {'platform': 'feishu', 'chat_id': role['chat_id']},
            'deliver': {'platform': 'feishu', 'chat_id': role['chat_id']}}
            for role in pool['roles'].values() for field, identifier in role.items() if field.endswith('job_id')}
        self.executions = []
        self.blockers = []
        self.lock = threading.RLock()
        self.guards = 0

    @contextmanager
    def switch_guard(self, job_ids, transition_id):
        with self.lock:
            self.guards += 1
            try:
                yield
            finally:
                self.guards -= 1

    def inspect(self, job_ids):
        return copy.deepcopy({'jobs': {key: self.jobs[key] for key in job_ids},
                              'executions': self.executions, 'blockers': self.blockers})


def fixture(directory):
    root = Path(directory)
    catalog = root/'README.md'
    catalog.write_text('# 需求索引\n| 需求包 | 目标 | 任务入口 |\n|---|---|---|\n'
        '| [旧需求](old/README.md) | old | [TODO](old/TODO.md) |\n'
        '| [新需求](new/README.md) | new | [TODO](new/TODO.md) |\n'
        '| [导航需求](navigation/README.md) | alias | [TODO](navigation/TODO.md) |\n'
        '导航需求兼容运行根：[旧运行根](legacy/README.md)。\n')
    for name in ('old', 'new', 'navigation', 'legacy'):
        project = root/name
        project.mkdir()
        (project/'README.md').write_text('# '+name+'\n')
        (project/'TODO.md').write_text('# unfinished TASK-'+name+'\n')
    roles = {role: {'chat_id': 'chat-'+role, 'poll_job_id': role+'-poll', 'report_job_id': role+'-report',
        'skill': 'coordinator' if role == 'COORDINATOR' else 'tester' if role == 'TESTER' else 'executor',
        'session_file': 'sessions/'+role+'.md'} for role in runtime.ROLES}
    defaults = {'schema_version': 1, 'control': 'ACTIVE', 'runtime_script': '/tmp/role_runtime.py',
                'observer_runtime': '/tmp/role_observer.py'}
    for name in ('old', 'new', 'legacy'):
        runtime.save_json(root/name/'ROLE-BINDINGS.json', {**defaults, 'project_root': str(root/name),
            'roles': copy.deepcopy(roles) if name == 'old' else {}, 'retained_decision': name})
    registry = root/'role-projects.json'
    runtime.save_json(registry, {'schema_version': 1, 'projects': [
        {'project_root': str(root/'old'), 'bindings': str(root/'old'/'ROLE-BINDINGS.json')}]})
    pool = {'schema_version': 1, 'pool_id': 'isolated-main', 'catalog_path': str(catalog),
        'registry_path': str(registry), 'generation': 0, 'control': 'ACTIVE', 'roles': roles,
        'requirements': {'new': {'aliases': ['新项目']}, 'navigation': {'runtime_root': str(root/'legacy'), 'aliases': []}},
        'binding_defaults': defaults,
        'current': {'requirement_id': 'old', 'runtime_root': str(root/'old'),
                    'role_roots': {role: str(root/'old') for role in runtime.ROLES}}}
    pool_path = root/'role-pool.json'
    runtime.save_json(pool_path, pool)
    return root, pool_path, pool, FakeScheduler(pool)


class RequirementRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root, self.path, self.pool, self.scheduler = fixture(self.temp.name)
        patch = mock.patch.object(runtime, '_independent_blockers', return_value=[])
        patch.start(); self.addCleanup(patch.stop)

    def provenance(self, target='new', message='message-1', action=None):
        return {'platform': 'feishu', 'chat_id': self.pool['roles']['COORDINATOR']['chat_id'],
            'user_id': 'private-user-identity', 'message_id': message,
            'primary_text': action or '全部权限：切换需求 '+target,
            'authenticated': True, 'current_full': True, 'role': 'COORDINATOR'}

    def select(self, target='new', **changes):
        provenance = self.provenance(target, **changes)
        return runtime.request(self.path, target, provenance, trusted_ingress=True)

    def reconcile(self, apply=True):
        return runtime.reconcile(self.path, self.scheduler, apply=apply)

    def test_exact_catalog_and_explicit_alias_runtime_mapping(self):
        catalog = runtime.list_requirements(self.path)
        entries = {entry['requirement_id']: entry for entry in catalog['requirements']}
        self.assertEqual(set(entries), {'old', 'new', 'navigation'})
        self.assertEqual(entries['navigation']['runtime_root'], str(self.root/'legacy'))
        request = self.select('新项目')
        self.assertEqual(request['requirement_id'], 'new')
        with self.assertRaisesRegex(ValueError, 'UNKNOWN_EXACT'):
            self.select('新')

    def test_ordinary_full_flag_is_not_native_authorization(self):
        provenance = self.provenance()
        with self.assertRaisesRegex(ValueError, 'CURRENT_AUTHENTICATED'):
            runtime.request(self.path, 'new', provenance)
        for field, value in (('authenticated', False), ('current_full', False), ('role', 'A'), ('chat_id', 'other')):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'CURRENT_AUTHENTICATED'):
                runtime.request(self.path, 'new', {**provenance, field: value}, trusted_ingress=True)

    def test_affirmative_natural_actions_are_accepted_as_whole_commands(self):
        for index, action in enumerate(('现在做需求 new', '开始做新项目', '开始开发 新需求',
                                       '开发需求new', '推进 new 并自动推进', '执行需求新项目，进行开发')):
            with self.subTest(action=action):
                self.select('new', message='natural-'+str(index), action='本轮授予全部权限：'+action)

    def test_quotes_negation_questions_and_side_mentions_do_not_select(self):
        for action in ('全部权限：不要切换需求 new', '全部权限：解释“切换需求 new”',
                       '全部权限：切换需求 new 吗？', '“全部权限：切换需求 new”',
                       '全部权限：如果可以就执行需求new', '全部权限：检查旧需求，提到new'):
            with self.subTest(action=action), self.assertRaises(ValueError):
                self.select(action=action)
        self.assertEqual(runtime.status(self.path)['pending'], [])

    def test_ordered_stop_then_exact_switch_retains_explicit_precontrol_receipt(self):
        result=self.select('new',action='全部权限：停止 A，转 new')
        saved=runtime.read_json(runtime._state_dir(self.path)/'requests'/(result['request_id']+'.json'))
        self.assertEqual(saved['pre_controls'],[{'action':'pause','role':'A'}])
        again=self.select('new',action='全部权限：停止 A，转 new')
        self.assertFalse(again['created'])
        self.assertEqual(len(runtime.status(self.path)['pending']),1)

    def test_message_retries_and_concurrent_intake_create_one_receipt(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.select(), range(6)))
        self.assertEqual(sum(result['created'] for result in results), 1)
        self.assertEqual(len(runtime.status(self.path)['pending']), 1)
        with self.assertRaisesRegex(ValueError, 'MESSAGE_ID_CONFLICT'):
            self.select('old')

    def test_public_status_and_receipt_do_not_expose_authorization_text(self):
        receipt = self.select()
        public = json.dumps({'receipt': receipt, 'status': runtime.status(self.path)}, ensure_ascii=False)
        self.assertNotIn('private-user-identity', public)
        self.assertNotIn('primary_text', public)
        stored = runtime._requests(self.path)[0]
        self.assertEqual(stored['source']['primary_text'], self.provenance()['primary_text'])
        self.assertFalse(stored['authorization']['production_or_strong_review_granted'])
        self.assertFalse(stored['authorization']['manual_grant_inherited'])

    def test_expired_but_live_owner_defers_without_business_mutation(self):
        fields = Path('/proc/self/stat').read_text().rsplit(') ', 1)[1].split()
        lease_path = self.root/'old/inbox/control/runtime/leases/A.json'
        lease = {'pid': os.getpid(), 'process_start': fields[19], 'expires_at': 0, 'token': 'unchanged'}
        runtime.save_json(lease_path, lease)
        before = (self.root/'old/ROLE-BINDINGS.json').read_bytes()
        self.select()
        result = self.reconcile()
        self.assertEqual(result['state'], 'WAITING_SAFE_POINT')
        self.assertIn('ROLE_OWNER_ACTIVE_OR_UNKNOWN', [row['code'] for row in result['blockers']])
        self.assertEqual((self.root/'old/ROLE-BINDINGS.json').read_bytes(), before)
        self.assertEqual(runtime.read_json(lease_path), lease)
        self.assertEqual(runtime.resolve_role(self.path, 'A')['project_root'], str(self.root/'old'))

    def test_unclaimed_cron_worker_also_defers_switch(self):
        self.scheduler.executions = [{'job_id': 'A-poll', 'execution_id': 'pre-agent', 'state': 'claimed'}]
        self.select()
        self.assertEqual(self.reconcile()['state'], 'WAITING_SAFE_POINT')
        self.assertEqual(runtime.read_json(self.path)['generation'], 0)

    def test_only_verified_safe_preflight_is_exempt(self):
        self.scheduler.executions = [{'job_id': 'A-poll', 'execution_id': 'preflight',
                                     'state': 'running', 'safe_checkpoint': True}]
        self.select()
        self.assertEqual(self.reconcile()['state'], 'WAITING_SAFE_POINT')
        self.scheduler.executions[0]['identity_verified'] = True
        self.assertEqual(self.reconcile()['state'], 'APPLIED')

    def test_readonly_report_execution_does_not_block_business_switch(self):
        self.scheduler.executions = [{'job_id': 'A-report', 'execution_id': 'report-model', 'state': 'running'}]
        self.select()
        self.assertEqual(self.reconcile()['state'], 'APPLIED')

    def test_dryrun_does_not_change_pool_binding_or_receipt(self):
        self.select()
        files = [self.path, self.root/'role-projects.json', self.root/'old/ROLE-BINDINGS.json',
                 self.root/'new/ROLE-BINDINGS.json', *runtime._state_dir(self.path).glob('requests/*.json')]
        before = {str(path): path.read_bytes() for path in files}
        self.assertEqual(self.reconcile(False)['state'], 'READY')
        self.assertEqual(before, {str(path): path.read_bytes() for path in files})
        self.assertFalse(runtime._journal_path(self.path).exists())

    def test_apply_preserves_jobs_business_material_and_does_not_copy_old_worktree(self):
        old_path = self.root/'old/ROLE-BINDINGS.json'
        old = runtime.read_json(old_path); old['roles']['A']['worktree'] = '/old/business-worktree'
        runtime.save_json(old_path, old)
        evidence = self.root/'old/inbox/A/ACK.json'
        runtime.save_json(evidence, {'task': 'already-claimed', 'evidence': 'preserve'})
        todo = (self.root/'old/TODO.md').read_bytes(); jobs = copy.deepcopy(self.scheduler.jobs)
        self.select()
        self.assertEqual(self.reconcile()['state'], 'APPLIED')
        self.assertEqual(runtime.read_json(old_path)['roles'], {})
        self.assertEqual((self.root/'old/TODO.md').read_bytes(), todo)
        self.assertEqual(runtime.read_json(evidence)['task'], 'already-claimed')
        self.assertEqual(self.scheduler.jobs, jobs)
        target = runtime.read_json(self.root/'new/ROLE-BINDINGS.json')
        self.assertIsNone(target['roles']['A']['worktree'])
        self.assertEqual(target['role_pool'], {'path': str(self.path), 'generation': 1})
        self.assertEqual(runtime.resolve_role(self.path, 'TESTER')['project_root'], str(self.root/'new'))
        self.assertEqual(runtime.status(self.path)['pending'], [])
        with self.assertRaisesRegex(ValueError, 'STALE_OR_INACTIVE'):
            runtime.validate_role_scope(self.path, 'A', str(self.root/'old'), 0)

    def test_mixed_legacy_role_roots_can_be_bootstrapped_without_losing_evidence(self):
        pool = runtime.read_json(self.path)
        old = runtime.read_json(self.root/'old/ROLE-BINDINGS.json')
        legacy = runtime.read_json(self.root/'legacy/ROLE-BINDINGS.json')
        for role in ('A', 'B', 'C', 'TESTER'):
            legacy['roles'][role] = old['roles'].pop(role)
            pool['current']['role_roots'][role] = str(self.root/'legacy')
        runtime.save_json(self.path, pool)
        runtime.save_json(self.root/'old/ROLE-BINDINGS.json', old)
        runtime.save_json(self.root/'legacy/ROLE-BINDINGS.json', legacy)
        self.select(); self.assertEqual(self.reconcile()['state'], 'APPLIED')
        self.assertEqual(runtime.read_json(self.root/'legacy/ROLE-BINDINGS.json')['roles'], {})
        self.assertTrue((self.root/'legacy/TODO.md').is_file())

    def test_unselected_mixed_bootstrap_resolves_each_roles_actual_package(self):
        pool=runtime.read_json(self.path)
        old=runtime.read_json(self.root/'old/ROLE-BINDINGS.json')
        legacy=runtime.read_json(self.root/'legacy/ROLE-BINDINGS.json')
        for role in ('A','B','C','TESTER'):
            legacy['roles'][role]=old['roles'].pop(role)
            pool['current']['role_roots'][role]=str(self.root/'legacy')
        runtime.save_json(self.path,pool)
        runtime.save_json(self.root/'old/ROLE-BINDINGS.json',old)
        runtime.save_json(self.root/'legacy/ROLE-BINDINGS.json',legacy)
        for role,root in (('COORDINATOR',self.root/'old'),('A',self.root/'legacy')):
            scope=runtime.resolve_role(self.path,role)
            self.assertTrue(scope['legacy_assignment'])
            self.assertEqual(scope['requirement_root'],str(root))
            self.assertEqual(scope['project_root'],str(root))
        self.select();self.assertEqual(self.reconcile()['state'],'APPLIED')
        scope=runtime.resolve_role(self.path,'COORDINATOR')
        self.assertFalse(scope['legacy_assignment'])
        self.assertEqual(scope['requirement_root'],str(self.root/'new'))

    def test_target_pause_and_disabled_report_jobs_are_preserved(self):
        path = self.root/'new/ROLE-BINDINGS.json'; binding = runtime.read_json(path)
        binding['control'] = 'PAUSED'; runtime.save_json(path, binding)
        self.select(); self.assertEqual(self.reconcile()['state'], 'APPLIED')
        self.assertEqual(runtime.resolve_role(self.path, 'A')['state'], 'PAUSED')
        self.assertFalse(self.scheduler.jobs['A-report']['enabled'])
        self.assertEqual(self.scheduler.jobs['A-report']['state'], 'paused')

    def test_indexed_requirement_without_todo_can_prepare_under_accepted_scope(self):
        (self.root/'new/TODO.md').unlink()
        self.select()
        result = self.reconcile()
        self.assertEqual(result['state'], 'APPLIED')
        scope = runtime.resolve_role(self.path, 'COORDINATOR')
        self.assertTrue(scope['preparation_required'])
        self.assertIn('TODO.md', scope['missing_documents'])
        self.assertFalse((self.root/'new/TODO.md').exists())
        self.assertEqual(list((self.root/'new/inbox').glob('**/*dispatch*')), [])

    def test_existing_inbox_protocol_satisfies_preparation_check(self):
        (self.root/'new/PRD.md').write_text('# accepted requirement\n')
        (self.root/'new/inbox').mkdir()
        (self.root/'new/inbox/PROTOCOL.md').write_text('# current protocol\n')
        self.select(); self.assertEqual(self.reconcile()['state'], 'APPLIED')
        scope = runtime.resolve_role(self.path, 'COORDINATOR')
        self.assertFalse(scope['preparation_required'])
        self.assertEqual(scope['missing_documents'], [])

    def test_compatible_runtime_keeps_selected_requirement_identity(self):
        self.select('navigation'); self.assertEqual(self.reconcile()['state'], 'APPLIED')
        scope = runtime.resolve_role(self.path, 'COORDINATOR')
        self.assertEqual(scope['requirement_root'], str(self.root/'navigation'))
        self.assertEqual(scope['project_root'], str(self.root/'legacy'))
        binding = runtime.read_json(self.root/'legacy/ROLE-BINDINGS.json')
        self.assertEqual(binding['requirement_authorization']['requirement_root'], str(self.root/'navigation'))

    def test_catalog_changes_require_new_confirmation_instead_of_silent_retarget(self):
        self.select()
        (self.root/'README.md').write_text((self.root/'README.md').read_text()+'\nnew catalog content\n')
        self.assertEqual(self.reconcile()['state'], 'BLOCKED')
        self.assertEqual(runtime.read_json(self.path)['generation'], 0)

    def test_new_authenticated_message_supersedes_blocked_selection_in_one_durable_write(self):
        old = self.select()
        (self.root/'README.md').write_text((self.root/'README.md').read_text()+'\nchanged index\n')
        self.assertEqual(self.reconcile()['state'], 'BLOCKED')
        original = runtime.save_json

        def interrupt_after_new_receipt(path, value):
            original(path, value)
            if Path(path).parent.name == 'requests':
                raise OSError('exit after accepting replacement')

        with mock.patch.object(runtime, 'save_json', side_effect=interrupt_after_new_receipt), self.assertRaises(OSError):
            self.select(message='replacement-message')
        pending = runtime.status(self.path)['pending']
        self.assertEqual(len(pending), 1)
        self.assertNotEqual(pending[0]['request_id'], old['request_id'])
        self.assertEqual(self.reconcile()['state'], 'APPLIED')
        history = runtime._requests(self.path)
        self.assertEqual(len(history), 2)
        replacement = next(row for row in history if row['status'] == 'APPLIED')
        self.assertEqual(replacement['supersedes'], [old['request_id']])
        replay = self.select()
        self.assertFalse(replay['created'])
        self.assertEqual(replay['status'], 'SUPERSEDED')

    def test_new_selection_does_not_supersede_prepared_transition(self):
        old = self.select()
        original = runtime.save_json

        def fail_after_journal(path, value):
            original(path, value)
            if Path(path) == runtime._journal_path(self.path):
                raise OSError('exit with PREPARED transition')

        with mock.patch.object(runtime, 'save_json', side_effect=fail_after_journal), self.assertRaises(OSError):
            self.reconcile()
        new = self.select('navigation', message='new-requirement-message')
        self.assertNotIn(old['request_id'], new['supersedes'])
        self.assertEqual(self.reconcile()['current']['requirement_id'], 'new')
        self.assertEqual(self.reconcile()['current']['requirement_id'], 'navigation')
        self.assertEqual(runtime.read_json(self.path)['generation'], 2)

    def test_explicit_runtime_root_must_be_index_referenced_and_canonical(self):
        pool = runtime.read_json(self.path)
        for root in (str(self.root.parent), str(self.root/'new/../old'), str(self.root/'not-listed')):
            pool['requirements']['new']['runtime_root'] = root; runtime.save_json(self.path, pool)
            with self.subTest(root=root), self.assertRaises(ValueError):
                runtime.list_requirements(self.path)

    def test_new_requirement_uses_only_binding_defaults_and_keeps_business_unmodified(self):
        (self.root/'new/ROLE-BINDINGS.json').unlink()
        before = (self.root/'new/TODO.md').read_bytes()
        self.select(); self.assertEqual(self.reconcile()['state'], 'APPLIED')
        self.assertEqual((self.root/'new/TODO.md').read_bytes(), before)
        self.assertEqual(runtime.read_json(self.root/'new/ROLE-BINDINGS.json')['runtime_script'], '/tmp/role_runtime.py')

    def test_binding_and_role_transport_templates_resolve_to_target_root(self):
        (self.root/'new/ROLE-BINDINGS.json').unlink()
        pool = runtime.read_json(self.path)
        pool['binding_defaults']['report_policy'] = {'command': 'read {project_root}/TODO.md'}
        pool['roles']['A']['report_policy'] = {'inputs': ['{root}/sessions/A.md']}
        runtime.save_json(self.path, pool)
        self.select(); self.assertEqual(self.reconcile()['state'], 'APPLIED')
        target = runtime.read_json(self.root/'new/ROLE-BINDINGS.json')
        self.assertEqual(target['report_policy']['command'], 'read '+str(self.root/'new/TODO.md'))
        self.assertEqual(target['roles']['A']['report_policy']['inputs'], [str(self.root/'new/sessions/A.md')])

    def test_concurrent_reconcile_only_commits_once(self):
        self.select()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.reconcile(), range(2)))
        self.assertEqual(sum(row['state'] == 'APPLIED' for row in results), 1)
        self.assertEqual(runtime.read_json(self.path)['generation'], 1)
        self.assertEqual(len(list((runtime._state_dir(self.path)/'transitions').glob('*.json'))), 1)

    def test_live_or_unknown_independent_operation_defers_switch(self):
        runtime.save_json(self.root/'old/inbox/control/runtime/operations/A/x/result.json', {'state': 'RUNNING'})
        with mock.patch.object(runtime.Path, 'home', return_value=self.root):
            rows = INDEPENDENT_PROBE([str(self.root/'old')])
        self.assertEqual(rows[0]['code'], 'INDEPENDENT_OPERATION_ACTIVE_OR_UNKNOWN')

    def test_ocr_service_is_checked_even_after_terminal_document_state(self):
        directory = self.root/'old/reviews/ocr-x'
        runtime.save_json(directory/'spec.json', {'schema': 'coordinator.ocr-run/v1', 'unit': 'ocr-test.service'})
        runtime.save_json(directory/'state.json', {'state': 'REVIEW_COMPLETE'})
        with mock.patch.object(runtime, '_service_active_or_unknown', return_value=True), \
             mock.patch.object(runtime.Path, 'home', return_value=self.root):
            rows = INDEPENDENT_PROBE([str(self.root/'old')])
        self.assertEqual(rows[0]['code'], 'OCR_ACTIVE_OR_UNRESOLVED')

    def test_stopped_ocr_awaiting_decision_is_frozen_without_changing_gate(self):
        directory = self.root/'old/reviews/ocr-x'
        runtime.save_json(directory/'spec.json', {'schema': 'coordinator.ocr-run/v1', 'unit': 'ocr-test.service'})
        state = {'state': 'AWAITING_DECISION', 'gate_decision': 'NOT_EVALUATED'}
        runtime.save_json(directory/'state.json', state)
        with mock.patch.object(runtime, '_service_active_or_unknown', return_value=False), \
             mock.patch.object(runtime.Path, 'home', return_value=self.root):
            self.assertEqual(INDEPENDENT_PROBE([str(self.root/'old')]), [])
        self.assertEqual(runtime.read_json(directory/'state.json'), state)

    def test_ocr_live_process_conflicts_with_stopped_service(self):
        directory = self.root/'old/reviews/ocr-x'
        runtime.save_json(directory/'spec.json', {'schema': 'coordinator.ocr-run/v1', 'unit': 'ocr-test.service'})
        fields = Path('/proc/self/stat').read_text().rsplit(') ', 1)[1].split()
        runtime.save_json(directory/'state.json', {'state': 'AWAITING_DECISION', 'pid': os.getpid(),
                                                  'process_start_ticks': fields[19]})
        with mock.patch.object(runtime, '_service_active_or_unknown', return_value=False), \
             mock.patch.object(runtime.Path, 'home', return_value=self.root):
            rows = INDEPENDENT_PROBE([str(self.root/'old')])
        self.assertEqual(rows[0]['code'], 'OCR_ACTIVE_OR_UNRESOLVED')

    def test_absent_legacy_pid_proves_exit_without_start_ticks(self):
        self.assertEqual(runtime._process_state(2147483647, None), 'dead')
        self.assertEqual(runtime._process_state(os.getpid(), None), 'unverified')

    def test_legacy_ocr_dedicated_cgroup_identifies_pid_reuse(self):
        row = {'pid': os.getpid(), 'cgroup': '0::/user.slice/ocr-old.service'}
        with mock.patch.object(runtime.Path, 'read_text', return_value='0::/user.slice/current.service'), \
             mock.patch.object(runtime, '_process_state', return_value='unverified'):
            self.assertEqual(runtime._record_process_state(row, 'ocr-old.service'), 'dead')
        with mock.patch.object(runtime.Path, 'read_text', return_value=row['cgroup']), \
             mock.patch.object(runtime, '_process_state', return_value='unverified'):
            self.assertEqual(runtime._record_process_state(row, 'ocr-old.service'), 'alive')

    def test_unstarted_prepared_and_stopped_legacy_ocr_do_not_block_or_change_gates(self):
        for index, state in enumerate(('PREPARED', 'AWAITING_DECISION', 'HARD_DEADLINE', 'BLOCKED', 'RUNNING')):
            directory = self.root/'old/reviews'/('ocr-'+str(index))
            runtime.save_json(directory/'spec.json', {'schema': 'coordinator.ocr-run/v1', 'unit': 'ocr-test.service'})
            body = {'state': state, 'gate_decision': 'NOT_EVALUATED'}
            if state != 'PREPARED':
                body['pid'] = 2147483647  # Old workers predate stored start ticks.
            runtime.save_json(directory/'state.json', body)
        before = {str(p): p.read_bytes() for p in (self.root/'old/reviews').rglob('*.json')}
        with mock.patch.object(runtime, '_service_active_or_unknown', return_value=False), \
             mock.patch.object(runtime.Path, 'home', return_value=self.root):
            self.assertEqual(INDEPENDENT_PROBE([str(self.root/'old')]), [])
        self.assertEqual(before, {str(p): p.read_bytes() for p in (self.root/'old/reviews').rglob('*.json')})

    def test_stopped_ocr_with_unknown_present_pid_is_still_blocked(self):
        directory = self.root/'old/reviews/legacy-ocr'
        runtime.save_json(directory/'spec.json', {'schema': 'coordinator.ocr-run/v1', 'unit': 'ocr-test.service'})
        runtime.save_json(directory/'state.json', {'state': 'AWAITING_DECISION', 'pid': os.getpid()})
        with mock.patch.object(runtime, '_service_active_or_unknown', return_value=False), \
             mock.patch.object(runtime.Path, 'home', return_value=self.root):
            self.assertEqual(INDEPENDENT_PROBE([str(self.root/'old')])[0]['code'], 'OCR_ACTIVE_OR_UNRESOLVED')

    def test_operation_submission_and_reconciled_history_are_inspected_readonly(self):
        base = self.root/'old/inbox/control/runtime/operations/TESTER'
        first = base/'never-started/result.json'
        runtime.save_json(first, {'status': 'PENDING', 'unit': 'role-operation-old'})
        runtime.save_json(first.with_suffix('.submission.json'), {'status': 'FAILED', 'error': 'worker failed before start'})
        runtime.save_json(first.parent/'spec.json', {'operation_id': 'never-started'})
        second = base/'reconciled/result.json'
        runtime.save_json(second, {'status': 'RECONCILED', 'pid': 2147483647, 'process_start': '123', 'ended_at': '2026-10-13'})
        runtime.save_json(second.parent/'spec.json', {'operation_id': 'reconciled'})
        before = {str(p): p.read_bytes() for p in base.rglob('*.json')}
        with mock.patch.object(runtime, '_service_active_or_unknown', return_value=False) as service, \
             mock.patch.object(runtime.Path, 'home', return_value=self.root):
            self.assertEqual(INDEPENDENT_PROBE([str(self.root/'old')]), [])
        observed = [call.args[0] for call in service.call_args_list]
        self.assertIn('role-operation-old', observed)
        self.assertIn('role-operation-'+runtime._sha(str(second.parent).encode())[:24], observed)
        self.assertEqual(before, {str(p): p.read_bytes() for p in base.rglob('*.json')})

    def test_reconciled_document_cannot_hide_live_operation_process(self):
        result = self.root/'old/inbox/control/runtime/operations/A/x/result.json'
        fields = Path('/proc/self/stat').read_text().rsplit(') ', 1)[1].split()
        runtime.save_json(result, {'status': 'RECONCILED', 'pid': os.getpid(), 'process_start': fields[19],
                                  'unit': 'role-operation-test'})
        with mock.patch.object(runtime, '_service_active_or_unknown', return_value=False), \
             mock.patch.object(runtime.Path, 'home', return_value=self.root):
            self.assertEqual(INDEPENDENT_PROBE([str(self.root/'old')])[0]['code'],
                             'INDEPENDENT_OPERATION_ACTIVE_OR_UNKNOWN')

    def test_service_name_without_suffix_resolves_as_service(self):
        stdout = 'LoadState=not-found\nActiveState=inactive\nMainPID=0\nControlPID=0\nControlGroup=\n'
        with mock.patch.object(runtime.subprocess, 'run', return_value=mock.Mock(returncode=1, stdout=stdout)) as run:
            self.assertFalse(runtime._service_active_or_unknown('role-operation-old'))
        self.assertIn('role-operation-old.service', run.call_args.args[0])

    def test_collected_service_absence_is_proven_but_bus_errors_are_unknown(self):
        for code, stdout, expected in ((1, 'LoadState=not-found\nActiveState=inactive\nMainPID=0\nControlPID=0\nControlGroup=\n', False),
                                      (0, 'LoadState=loaded\nActiveState=failed\nMainPID=0\nControlPID=0\nControlGroup=\n', False),
                                      (0, 'LoadState=loaded\nActiveState=active\nMainPID=12\nControlPID=0\nControlGroup=/abc\n', True),
                                      (1, '', True),
                                      (0, 'LoadState=loaded\nActiveState=inactive\nMainPID=0\n', True)):
            with self.subTest(stdout=stdout), mock.patch.object(runtime.subprocess, 'run',
                    return_value=mock.Mock(returncode=code, stdout=stdout)):
                self.assertEqual(runtime._service_active_or_unknown('ocr-test.service'), expected)

    def test_inactive_service_with_populated_cgroup_is_still_blocked(self):
        stdout = 'LoadState=loaded\nActiveState=inactive\nMainPID=0\nControlPID=0\nControlGroup=/fake-role-test\n'
        with mock.patch.object(runtime.subprocess, 'run', return_value=mock.Mock(returncode=0, stdout=stdout)), \
             mock.patch.object(runtime.Path, 'read_text', return_value='populated 1\nfrozen 0\n'):
            self.assertTrue(runtime._service_active_or_unknown('ocr-test.service'))

    def test_explicitly_recovered_lost_strong_review_is_terminal(self):
        database = self.root/'.local/state/complex-task-reviews/ledger.sqlite3'
        database.parent.mkdir(parents=True)
        with runtime.sqlite3.connect(database) as con:
            con.execute('CREATE TABLE calls(project TEXT, status TEXT, unit TEXT)')
            con.execute('INSERT INTO calls VALUES(?,?,?)', (str(self.root/'old'), 'LOST', 'review-old.service'))
        with mock.patch.object(runtime, '_service_active_or_unknown', return_value=False), \
             mock.patch.object(runtime.Path, 'home', return_value=self.root):
            self.assertEqual(INDEPENDENT_PROBE([str(self.root/'old')]), [])

    def test_probe_failures_become_visible_blockers(self):
        self.select()
        with mock.patch.object(runtime, '_independent_blockers', side_effect=ValueError('corrupt OCR metadata')):
            result = self.reconcile()
        self.assertEqual(result['state'], 'BLOCKED')
        self.assertEqual(result['blockers'][0]['code'], 'SWITCH_RUNTIME_UNVERIFIED')
        self.assertIn('corrupt OCR metadata', runtime.status(self.path)['pending'][0]['last_blockers'][0]['reason'])

    def test_every_commit_boundary_recovers_without_double_assignment(self):
        original = runtime.save_json
        for boundary in range(1, 10):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as temporary:
                root, path, pool, scheduler = fixture(temporary)
                runtime.request(path, 'new', self.provenance(), trusted_ingress=True)
                calls = 0

                def interrupted(dest, value):
                    nonlocal calls
                    original(dest, value)
                    calls += 1
                    if calls == boundary:
                        raise OSError('simulated process exit after durable write')

                with mock.patch.object(runtime, 'save_json', side_effect=interrupted):
                    with self.assertRaises(OSError):
                        runtime.reconcile(path, scheduler, apply=True)
                scope = runtime.resolve_role(path, 'A')
                self.assertIn(scope['state'], ('SWITCHING', 'ACTIVE'))
                result = runtime.reconcile(path, scheduler, apply=True)
                self.assertIn(result['state'], ('APPLIED', 'NO_PENDING_REQUEST'))
                self.assertEqual(runtime.read_json(path)['generation'], 1)
                self.assertEqual(runtime.read_json(root/'old/ROLE-BINDINGS.json')['roles'], {})
                self.assertEqual(set(runtime.read_json(root/'new/ROLE-BINDINGS.json')['roles']), set(runtime.ROLES))
                self.assertEqual(runtime.resolve_role(path, 'A')['project_root'], str(root/'new'))

    def test_external_changes_during_interrupted_transition_are_not_overwritten(self):
        self.select()
        original = runtime.save_json

        def fail_after_journal(dest, value):
            original(dest, value)
            if Path(dest) == runtime._journal_path(self.path):
                raise OSError('exit')

        with mock.patch.object(runtime, 'save_json', side_effect=fail_after_journal), self.assertRaises(OSError):
            self.reconcile()
        target = self.root/'new/ROLE-BINDINGS.json'
        changed = runtime.read_json(target); changed['new_valid_decision'] = 'retain'; original(target, changed)
        self.assertEqual(self.reconcile()['state'], 'BLOCKED')
        self.assertEqual(runtime.read_json(target)['new_valid_decision'], 'retain')
        self.assertEqual(runtime.resolve_role(self.path, 'A')['state'], 'SWITCHING')

    def test_mixed_legacy_root_recovers_after_pool_pointer_has_moved(self):
        pool = runtime.read_json(self.path)
        old = runtime.read_json(self.root/'old/ROLE-BINDINGS.json')
        legacy = runtime.read_json(self.root/'legacy/ROLE-BINDINGS.json')
        for role in ('A', 'B', 'C', 'TESTER'):
            legacy['roles'][role] = old['roles'].pop(role)
            pool['current']['role_roots'][role] = str(self.root/'legacy')
        runtime.save_json(self.path, pool)
        runtime.save_json(self.root/'old/ROLE-BINDINGS.json', old)
        runtime.save_json(self.root/'legacy/ROLE-BINDINGS.json', legacy)
        self.select(); original = runtime.save_json

        def interrupt_after_pool_pointer(path, value):
            original(path, value)
            if Path(path) == self.path:
                raise OSError('exit after pointer moved')

        with mock.patch.object(runtime, 'save_json', side_effect=interrupt_after_pool_pointer), self.assertRaises(OSError):
            self.reconcile()
        self.assertEqual(runtime.read_json(self.path)['generation'], 1)
        self.assertEqual(self.reconcile()['state'], 'APPLIED')
        self.assertEqual(runtime.read_json(self.path)['generation'], 1)

    def test_duplicate_transition_file_record_is_rejected(self):
        self.select(); original = runtime.save_json

        def stop_after_journal(path, value):
            original(path, value)
            if Path(path) == runtime._journal_path(self.path):
                raise OSError('exit')

        with mock.patch.object(runtime, 'save_json', side_effect=stop_after_journal), self.assertRaises(OSError):
            self.reconcile()
        journal = runtime.read_json(runtime._journal_path(self.path))
        journal['files'].append(copy.deepcopy(journal['files'][0]))
        original(runtime._journal_path(self.path), journal)
        result = self.reconcile()
        self.assertEqual(result['state'], 'BLOCKED')
        self.assertIn('INVALID_TRANSITION_FILE_SET', result['blockers'][0]['reason'])


if __name__ == '__main__':
    unittest.main()
