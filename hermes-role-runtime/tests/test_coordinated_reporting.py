from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coordination_inputs
import progress_query
import report_summary
import role_runtime
import role_status
import usage_accounting


class CoordinatedReportingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.primary, self.target, self.home = self.base/'primary', self.base/'target', self.base/'home'
        self.at = datetime.now(ZoneInfo('Asia/Shanghai'))
        self.workers = ['A', 'B', 'C', 'TESTER']
        self.primary_binding = {'schema_version': 1, 'project_root': str(self.primary), 'control': 'ACTIVE',
            'roles': {'COORDINATOR': {'session_file': 'sessions/COORDINATOR.md',
                'coordinated_projects': [{'project_root': str(self.target), 'roles': self.workers}]}}}
        self.target_binding = {'schema_version': 1, 'project_root': str(self.target), 'control': 'ACTIVE',
            'roles': {role: {'session_file': 'sessions/'+role+'.md'} for role in self.workers}}
        for root, binding in ((self.primary, self.primary_binding), (self.target, self.target_binding)):
            self.write(root, 'ROLE-BINDINGS.json', binding)
            self.write(root, 'TODO.md', '| TASK-01 | 🏃 | feature | AC | none | RUNNING | 1号 |\n')
            for role in binding['roles']:
                self.write(root, 'sessions/'+role+'.md', '# 当前角色\n')
            for folder in ('inbox/1号', 'inbox/2号', 'inbox/3号', 'inbox/TESTER'):
                (root/folder).mkdir(parents=True, exist_ok=True)
        registry = self.base/'registry.json'
        self.write(self.base, 'registry.json', {'projects': [{'project_root': str(self.primary)},
                                                            {'project_root': str(self.target)}]})
        subscription_registry = patch.object(coordination_inputs, 'REGISTRY', registry)
        subscription_registry.start(); self.addCleanup(subscription_registry.stop)
        ledger = SimpleNamespace(database=self.base/'missing-review.db',
            status=lambda _project: {'authorization': 'MANUAL_ONLY', 'active_calls': []})
        review = patch('strong_review.Ledger', return_value=ledger)
        review.start(); self.addCleanup(review.stop)
        for role in self.workers:
            self.write(self.target, 'inbox/control/runtime/dispatch-index/'+role+'.json',
                {'schema_version': 1, 'role': role, 'observed_at': self.at.isoformat(),
                 'verified_no_task': True, 'entries': [], 'active_operations': []})
        for role, state in [('A', 'RUNNING'), ('B', 'WAITING'), ('C', 'BLOCKED')]:
            self.write(self.target, 'inbox/control/runtime/checkpoints/'+role+'.json',
                {'schema_version': 1, 'role': role, 'task': 'TASK-01', 'attempt': 'attempt-1',
                 'status': state, 'recorded_at': self.at.isoformat(), 'next': '按目标项目派单接续'})
        self.write(self.target, 'inbox/control/runtime/leases/A.json', {'owner': 'target-worker',
            'pid': os.getpid(), 'process_start': role_runtime.pid_identity(os.getpid())})
        self.event(self.target, 'A', 'TARGET_WORKER_PROGRESS', self.at)
        self.event(self.primary, 'A', 'OLD_LOCAL_MIGRATED_RESULT', self.at)

    def write(self, root, source, value):
        path = root/source; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value)
        return path

    def event(self, root, role, summary, at):
        identity = role+':progress'
        return self.write(root, 'events/'+role+'/'+hashlib.sha256(identity.encode()).hexdigest()+'.json',
            {'schema_version': 1, 'kind': 'business', 'role': role, 'event_id': identity,
             'task': 'TASK-01', 'attempt': 'attempt-1', 'status': 'RUNNING', 'summary': summary,
             'occurred_at': at.isoformat(), 'recorded_at': at.isoformat()})

    def target_files(self):
        return {str(path.relative_to(self.target)): path.read_bytes()
                for path in self.target.rglob('*') if path.is_file()}

    def test_status_uses_declared_worker_project_without_target_writes(self):
        before = self.target_files()
        states = role_status.collect_role_statuses(self.primary, at=self.at)
        self.assertEqual(['WORKING', 'WAITING', 'BLOCKED', 'IDLE'], [states[role]['status'] for role in self.workers])
        self.assertTrue(all(states[role]['project_root'] == str(self.target) for role in self.workers))
        self.assertIn('执行者 A（工作中）', role_status.render_status_line(states))
        self.assertEqual(before, self.target_files())

    def test_live_query_keeps_remote_runtime_and_business_origin_without_target_writes(self):
        before = self.target_files()
        reviews = {'ocr_runs': [{'run_id': 'linked-ocr', 'task': 'TASK-01', 'state': 'AWAITING_DECISION',
                                'source': 'reviews/linked-ocr/state.json'}],
            'strong_review': {'source': str(self.base/'review.db'), 'active_calls': [],
                             'last_call': {'id': 'linked-strong', 'status': 'SUCCEEDED'}}, 'watch': {}}
        with patch.object(coordination_inputs, '_review_inputs', return_value=reviews):
            result = progress_query.live_progress_report(self.primary)
        item = result['runtime_observation']['roles']['A']
        self.assertEqual(str(self.target), item['project_root'])
        self.assertEqual('alive', item['owner']['state'])
        self.assertIn('TARGET_WORKER_PROGRESS', result['markdown'])
        self.assertIn('执行者 A（工作中）', result['markdown'])
        self.assertIn('linked-ocr', result['markdown'])
        self.assertIn('linked-strong', result['markdown'])
        self.assertTrue(Path(item['checkpoint']['source']).is_relative_to(self.target))
        self.assertNotIn('OLD_LOCAL_MIGRATED_RESULT', result['markdown'])
        self.assertEqual(before, self.target_files())

    def test_morning_collects_subscribed_interval_and_claims_but_only_own_usage(self):
        at = self.at.replace(hour=8, minute=0, second=0, microsecond=0)
        occurred = at-timedelta(hours=9)
        self.event(self.target, 'A', 'TARGET_NIGHT_RESULT', occurred)
        self.event(self.primary, 'A', 'OLD_LOCAL_MIGRATED_RESULT', occurred)
        for root, role, model in [(self.primary, 'COORDINATOR', 'OWN_MODEL'),
                                  (self.target, 'A', 'REMOTE_PRIVATE_MODEL')]:
            usage_accounting.record(self.home/'runtime/role-usage.db', 'end', session_id=role, api_request_id=role,
                physical_attempt_id=role, role=role, project=root, route='local', model=model,
                started_at=occurred, ended_at=occurred+timedelta(seconds=1),
                usage={'input_tokens': 10, 'output_tokens': 2})
        self.write(self.target, 'inbox/control/runtime/claim-outbox/A/night.json',
            {'schema_version': 1, 'role': 'A', 'task': 'TASK-01', 'attempt': 'attempt-1',
             'occurred_at': occurred.isoformat(), 'status': 'SUPPRESSED', 'text': '领取目标项目任务'})
        before = self.target_files()
        result = report_summary.build_summary_input(self.primary, 'COORDINATOR', 'morning', at=at, home=self.home)
        packet = result['facts']
        self.assertEqual(['COORDINATOR', *self.workers], [item['role'] for item in packet['roles']])
        self.assertTrue(all(item['project_root'] == str(self.target) for item in packet['roles'][1:]))
        self.assertTrue(all('model_usage' not in item for item in packet['roles'][1:]))
        self.assertEqual(1, packet['roles'][0]['model_usage']['calls'])
        self.assertIn('TARGET_NIGHT_RESULT', result['prompt'])
        self.assertNotIn('REMOTE_PRIVATE_MODEL', result['prompt'])
        self.assertNotIn('OLD_LOCAL_MIGRATED_RESULT', result['prompt'])
        self.assertEqual(str(self.target), packet['night_claim_notifications'][0]['project_root'])
        self.assertEqual(before, self.target_files())

    def test_conflicting_role_sources_are_unverified_and_not_selected(self):
        self.primary_binding['roles']['A'] = {'session_file': 'sessions/A.md'}
        self.write(self.primary, 'ROLE-BINDINGS.json', self.primary_binding)
        states = role_status.collect_role_statuses(self.primary, at=self.at)
        self.assertEqual('UNKNOWN', states['A']['status'])
        self.assertFalse(states['A']['available'])
        before = self.target_files()
        result = progress_query.live_progress_report(self.primary)
        self.assertEqual('UNVERIFIED', result['runtime_observation']['roles']['A']['state'])
        self.assertIn('订阅冲突', result['markdown'])
        self.assertNotIn('OLD_LOCAL_MIGRATED_RESULT', result['markdown'])
        morning = self.at.replace(hour=8, minute=0, second=0, microsecond=0)
        self.event(self.primary, 'A', 'DISPUTED_LOCAL_RESULT', morning-timedelta(hours=1))
        summary = report_summary.build_summary_input(self.primary, 'COORDINATOR', 'morning',
            at=morning, home=self.home)
        self.assertNotIn('A', [item['role'] for item in summary['facts']['roles']])
        self.assertEqual('UNKNOWN', summary['facts']['role_states']['A']['status'])
        self.assertNotIn('DISPUTED_LOCAL_RESULT', summary['prompt'])
        self.assertEqual(before, self.target_files())

    def test_unsubscribed_roles_and_invalid_target_are_not_silently_inferred(self):
        self.primary_binding['roles']['COORDINATOR']['coordinated_projects'][0]['roles'] = ['A']
        self.write(self.primary, 'ROLE-BINDINGS.json', self.primary_binding)
        states = role_status.collect_role_statuses(self.primary, at=self.at)
        self.assertEqual('WORKING', states['A']['status'])
        self.assertEqual('UNKNOWN', states['TESTER']['status'])
        self.target_binding['roles'].pop('A')
        self.write(self.target, 'ROLE-BINDINGS.json', self.target_binding)
        self.assertEqual('UNKNOWN', role_status.collect_role_statuses(self.primary, at=self.at)['A']['status'])


if __name__ == '__main__': unittest.main()
