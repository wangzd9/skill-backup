import hashlib
import json
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coordination_inputs
import role_observer
from context_input import build_context


class CoordinationInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.primary, self.target = base/'primary', base/'target'
        self.roles = {'A': {'session_file': 'sessions/A.md'}, 'TESTER': {'session_file': 'sessions/TESTER.md'}}
        self.target_binding = {'schema_version': 1, 'project_root': str(self.target), 'control': 'ACTIVE', 'roles': self.roles}
        self.primary_binding = {'schema_version': 1, 'project_root': str(self.primary), 'control': 'ACTIVE',
            'roles': {'COORDINATOR': {'session_file': 'sessions/COORDINATOR.md',
                'coordinated_projects': [{'project_root': str(self.target), 'roles': ['A', 'TESTER']}]}}}
        for root, binding in ((self.primary, self.primary_binding), (self.target, self.target_binding)):
            self.write(root, 'ROLE-BINDINGS.json', json.dumps(binding))
            self.write(root, 'TODO.md', '| TASK-01 | ✅ | work | AC | none | DONE | 1号 |\n')
            for role, item in binding['roles'].items():
                self.write(root, item['session_file'], '# 当前角色\n')
            for folder in ('inbox/1号', 'inbox/TESTER'):
                (root/folder).mkdir(parents=True, exist_ok=True)
        registry = base/'registry.json'
        registry.write_text(json.dumps({'projects': [{'project_root': str(self.primary)}, {'project_root': str(self.target)}]}))
        self.registry = registry
        self.patch_registry = patch.object(coordination_inputs, 'REGISTRY', registry)
        self.patch_registry.start(); self.addCleanup(self.patch_registry.stop)
        self.patch_ledger = patch('strong_review.Ledger.status', return_value={'authorization': 'MANUAL_ONLY'})
        self.patch_ledger.start(); self.addCleanup(self.patch_ledger.stop)

    def write(self, root, source, content):
        path = root/source; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(content); return path

    def ack(self):
        value = role_observer.observe(self.primary, 'COORDINATOR')
        role_observer.acknowledge(self.primary, 'COORDINATOR', value['fingerprint'])
        return value

    def failure(self, result='FAIL MySQL1553', identity='TESTER:failed'):
        row = {'schema_version': 1, 'kind': 'business', 'role': 'TESTER', 'event_id': identity,
               'task': 'TASK-01', 'attempt': 'attempt-1', 'status': 'TEST_REPORT_PENDING',
               'summary': result, 'evidence': 'test-receipts/TASK-01/attempt-1.md'}
        path = 'events/TESTER/'+hashlib.sha256(identity.encode()).hexdigest()+'.json'
        self.write(self.target, path, json.dumps(row))
        return row

    def ocr_run(self):
        directory = 'reviews/TASK-01/ocr-linked'
        spec = {'schema': 'coordinator.ocr-run/v1', 'project': str(self.target), 'run_id': 'linked-1',
                'review_identity': 'linked-identity', 'task': 'TASK-01', 'attempt': 'attempt-1',
                'base': 'a'*40, 'head': 'b'*40, 'unit': 'coordinator-ocr-linked.service',
                'batches': [{'id': 'b01', 'files': ['app.py']}], 'provider': 'vllm-local', 'model': 'fake'}
        self.write(self.target, directory+'/spec.json', json.dumps(spec))
        state = {'state': 'RUNNING', 'run_id': spec['run_id'], 'unit': spec['unit'],
                 'batches': [{'id': 'b01', 'state': 'RUNNING', 'files': ['app.py']}],
                 'gate_decision': 'NOT_EVALUATED', 'updated_at': '2026-10-19T08:00:00+08:00'}
        path = self.write(self.target, directory+'/state.json', json.dumps(state))
        return path, state

    def test_target_fail_and_receipt_changes_wake_primary_without_local_changes(self):
        self.ack()
        self.assertFalse(role_observer.observe(self.primary, 'COORDINATOR')['wakeAgent'])
        self.write(self.target, 'test-receipts/TASK-01/attempt-1.md', '# TASK-01\nFAIL MySQL1553\n')
        self.failure()
        value = role_observer.observe(self.primary, 'COORDINATOR')
        self.assertTrue(value['wakeAgent'])
        self.assertTrue(any(str(self.target) in source for source in value['changed_sources']))
        packet = json.loads(Path(value['snapshot']).read_text())['coordinated_inputs']
        self.assertIn('FAIL MySQL1553', packet['projects'][0]['roles_state']['TESTER']['latest_event']['summary'])
        self.assertEqual(packet['projects'][0]['test_receipts'][0]['project_root'], str(self.target))
        self.assertEqual(packet['gates'][0]['state'], 'REVIEW_REQUIRED')
        self.ack()
        self.write(self.target, 'test-receipts/TASK-01/attempt-1.md', '# TASK-01\nFAIL new constraint\n')
        self.assertTrue(role_observer.observe(self.primary, 'COORDINATOR')['wakeAgent'])

    def test_old_primary_ack_cannot_consume_concurrent_new_target_fail(self):
        first = self.ack()
        self.failure()
        with self.assertRaisesRegex(ValueError, 'STALE_OBSERVATION'):
            role_observer.acknowledge(self.primary, 'COORDINATOR', first['fingerprint'])
        self.assertTrue(role_observer.observe(self.primary, 'COORDINATOR')['wakeAgent'])

    def test_linked_ocr_terminal_wakes_without_target_coordinator_binding(self):
        path, state = self.ocr_run()
        self.assertNotIn('COORDINATOR', self.target_binding['roles'])
        self.ack()
        state.update(updated_at='2026-10-19T08:01:00+08:00', heartbeat=99)
        path.write_text(json.dumps(state))
        self.write(self.target, str(path.parent.relative_to(self.target))+'/worker.log', 'log progress\n'*100)
        self.assertFalse(role_observer.observe(self.primary, 'COORDINATOR')['wakeAgent'])
        state['state'] = 'AWAITING_DECISION'
        state['batches'][0].update(state='REVIEW_COMPLETE', returncode=0)
        path.write_text(json.dumps(state))
        value = role_observer.observe(self.primary, 'COORDINATOR')
        self.assertTrue(value['wakeAgent'])
        self.assertIn('coordinated:'+str(self.target)+':review:'+str(path.relative_to(self.target)), value['changed_sources'])
        packet = json.loads(Path(value['snapshot']).read_text())['coordinated_inputs']
        self.assertEqual('AWAITING_DECISION', packet['projects'][0]['reviews']['ocr_runs'][0]['state'])
        self.assertTrue(any(gate['code'] == 'LINKED_OCR_RESULT_MUST_BE_READ' for gate in packet['gates']))
        self.ack()
        self.assertFalse(role_observer.observe(self.primary, 'COORDINATOR')['wakeAgent'])

    def test_old_primary_ack_cannot_swallow_new_linked_ocr_result(self):
        path, state = self.ocr_run()
        before = self.ack()
        state.update(state='FAILED', error='independent worker failed')
        path.write_text(json.dumps(state))
        with self.assertRaisesRegex(ValueError, 'STALE_OBSERVATION'):
            role_observer.acknowledge(self.primary, 'COORDINATOR', before['fingerprint'])
        packet = build_context(self.primary, 'COORDINATOR')['coordinated_inputs']
        self.assertTrue(any(gate['code'] == 'LINKED_OCR_RESULT_MUST_BE_READ' and gate['state'] == 'BLOCKED'
                            for gate in packet['gates']))

    def test_linked_strong_terminal_scopes_global_queue_and_ignores_quota_churn(self):
        target_status = {'authorization': 'AUTHORIZED', 'grant_id': 'grant-linked', 'used_last_60_minutes': 2,
                         'limit': 5, 'active_calls': [], 'last_call': {'id': 'call-linked', 'review_id': 'review-1',
                         'status': 'RUNNING', 'result_path': str(self.target/'reviews/result.json')}}
        def status(project):
            return target_status if project == str(self.target) else {'authorization': 'MANUAL_ONLY'}
        self.patch_ledger.stop()
        with patch('strong_review.Ledger.status', side_effect=status):
            self.ack()
            target_status['used_last_60_minutes'] = 1
            target_status['active_calls'] = [{'id': 'foreign', 'project': str(self.primary/'other'), 'status': 'RUNNING'}]
            self.assertFalse(role_observer.observe(self.primary, 'COORDINATOR')['wakeAgent'])
            target_status['last_call']['status'] = 'SUCCEEDED'
            result = role_observer.observe(self.primary, 'COORDINATOR')
            self.assertTrue(result['wakeAgent'])
            packet = json.loads(Path(result['snapshot']).read_text())['coordinated_inputs']
            self.assertEqual('SUCCEEDED', packet['projects'][0]['reviews']['strong_review']['last_call']['status'])
            self.assertEqual([], packet['projects'][0]['reviews']['strong_review']['active_calls'])
            self.assertTrue(any(gate['code'] == 'LINKED_STRONG_REVIEW_RESULT_MUST_BE_READ' for gate in packet['gates']))

    def test_linked_review_collection_error_is_actionable_and_not_silently_empty(self):
        self.ack()
        self.write(self.target, 'inbox/control/runtime/ocr/broken.json', '{}')
        result = role_observer.observe(self.primary, 'COORDINATOR')
        self.assertTrue(result['wakeAgent'])
        packet = json.loads(Path(result['snapshot']).read_text())['coordinated_inputs']
        self.assertTrue(any(gate['code'] == 'COORDINATED_REVIEWS_UNVERIFIED' and gate['state'] == 'BLOCKED'
                            for gate in packet['gates']))

    def test_subscriber_does_not_write_target_indices_acks_leases_or_activity(self):
        before = {str(path.relative_to(self.target)): path.read_bytes() for path in self.target.rglob('*') if path.is_file()}
        with patch('role_status.refresh_dispatch_index') as publish:
            role_observer.collect(self.primary, 'COORDINATOR', publish_indexes=False)
            context = build_context(self.primary, 'COORDINATOR')
        after = {str(path.relative_to(self.target)): path.read_bytes() for path in self.target.rglob('*') if path.is_file()}
        self.assertEqual(before, after)
        publish.assert_not_called()
        self.assertEqual(context['coordinated_inputs']['projects'][0]['project_root'], str(self.target))

    def test_role_scoped_todo_hashes_do_not_overwrite_each_other(self):
        self.write(self.target, 'TODO.md', '| TASK-01 | ✅ | work | AC | none | DONE | 1号 |\n| TASK-02 | 🏃 | verify | AC | none | RUNNING | 测试者 |\n')
        _, watch = role_observer.collect(self.primary, 'COORDINATOR', publish_indexes=False)
        a = 'coordinated:'+str(self.target)+':A:TODO.md'
        tester = 'coordinated:'+str(self.target)+':TESTER:TODO.md'
        self.assertIn(a, watch); self.assertIn(tester, watch)
        self.assertNotEqual(watch[a], watch[tester])

    def test_unconfigured_project_and_roles_are_not_inferred_or_subscribed(self):
        self.primary_binding['roles']['COORDINATOR']['coordinated_projects'][0]['roles'] = ['A']
        self.write(self.primary, 'ROLE-BINDINGS.json', json.dumps(self.primary_binding))
        self.ack()
        self.failure()
        self.write(self.target, 'test-receipts/TASK-01/attempt-1.md', 'FAIL unrelated unsubscribed TESTER')
        self.assertFalse(role_observer.observe(self.primary, 'COORDINATOR')['wakeAgent'])

    def test_target_collection_failure_is_visible_gate_not_silent_missing_fact(self):
        self.ack()
        self.write(self.target, 'TODO.md', '# not a recognized task table\n')
        value = role_observer.observe(self.primary, 'COORDINATOR')
        self.assertTrue(value['wakeAgent'])
        packet = json.loads(Path(value['snapshot']).read_text())['coordinated_inputs']
        self.assertTrue(any(gate['state'] == 'BLOCKED' for gate in packet['gates']))
        self.assertTrue(packet['projects'][0]['errors'])

    def test_subscription_requires_registered_canonical_root_and_exact_existing_roles(self):
        for replacement in ([str(self.target)], [{'project_root': str(self.target), 'roles': ['COORDINATOR']}],
                            [{'project_root': str(self.target), 'roles': ['B']}],
                            [{'project_root': str(self.target/'..'/'target'), 'roles': ['A']}],
                            [{'project_root': str(self.primary), 'roles': ['A']}],
                            [{'project_root': str(self.target), 'roles': ['A', 'A']}],
                            [{'project_root': str(self.target), 'roles': [{}]}]):
            with self.subTest(replacement=replacement):
                binding = json.loads(json.dumps(self.primary_binding))
                binding['roles']['COORDINATOR']['coordinated_projects'] = replacement
                with self.assertRaises(ValueError): coordination_inputs.validate_subscriptions(self.primary, binding)
        coordination_inputs.REGISTRY.write_text(json.dumps({'projects': [{'project_root': str(self.primary)}]}))
        with self.assertRaises(ValueError): coordination_inputs.validate_subscriptions(self.primary, self.primary_binding)

    def test_context_is_bounded_and_does_not_load_receipt_body(self):
        self.write(self.target, 'test-receipts/TASK-01/attempt-1.md', 'PRIVATE_RAW_BODY'*10000)
        self.failure()
        packet = build_context(self.primary, 'COORDINATOR')
        rendered = json.dumps(packet, ensure_ascii=False)
        self.assertNotIn('PRIVATE_RAW_BODY', rendered)
        self.assertIn('LATEST_LINKED_TEST_EVIDENCE_MUST_BE_READ', rendered)
        self.assertLess(len(rendered.encode()), 32768)

    def test_fresh_isolated_cli_loads_real_linked_observer_from_symlink_and_canonical_entry(self):
        # Only the registry location is injected; no imports or collector are
        # mocked in the child. -I excludes cwd/PYTHONPATH and the test process's
        # already-loaded modules. This reproduces the real symlink CLI failure.
        launcher = '''
import runpy,sys
from pathlib import Path
entry,project,registry,scripts=sys.argv[1:]
assert scripts not in sys.path
sys.path.insert(0,str(Path(entry).resolve().parent))
import coordination_inputs
coordination_inputs.REGISTRY=Path(registry)
assert scripts not in sys.path
assert 'role_observer' not in sys.modules
sys.argv=[entry,'context-input','--project',project,'--role','COORDINATOR']
runpy.run_path(entry,run_name='__main__')
'''
        from scripts.runtime_layout import reconcile
        scripts = Path(self.temp.name)/'compat-skill/scripts'
        self.assertTrue(reconcile(scripts,apply=True)['valid'])
        canonical = Path(__file__).resolve().parents[1]/'runtime_tools.py'
        environment = {key: value for key, value in os.environ.items()
                       if key not in {'PYTHONPATH', 'PYTHONHOME'}}
        for entry in (scripts/'runtime_tools.py', canonical):
            with self.subTest(entry=str(entry)):
                result = subprocess.run([sys.executable, '-I', '-c', launcher,
                    str(entry), str(self.primary), str(self.registry), str(scripts)],
                    cwd=self.temp.name, env=environment, text=True, capture_output=True, timeout=30)
                self.assertEqual(0, result.returncode, result.stdout+'\n'+result.stderr)
                packet = json.loads(result.stdout)
                self.assertEqual('COORDINATOR', packet['role'])
                self.assertEqual(str(self.target), packet['coordinated_inputs']['projects'][0]['project_root'])
                self.assertIn('TESTER', packet['coordinated_inputs']['projects'][0]['roles_state'])


if __name__ == '__main__': unittest.main()
