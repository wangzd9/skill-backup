from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path.home()/'.hermes/hermes-agent'))
import report_summary
import usage_accounting


class SummaryTests(unittest.TestCase):
    def _saved_progress(self, root):
        todo = root/'TODO.md'
        todo.write_text('current authoritative source\n')
        def fact(value):
            return {'value': value, 'source': 'TODO.md', 'observed_at': '2026-10-16 08:00 +08:00'}
        full = {'schema_version': 1, 'kind': 'current-progress', 'project': str(root),
            'observed_at': '2026-10-16 08:00 +08:00', 'goal': fact('current goal'), 'control': fact('ACTIVE'),
            'overall': {'current_tasks': fact(['TASK-01', 'TASK-02'])},
            'roles': {'A': {'tasks': fact(['TASK-01']), 'last_action': fact('A exact result'),
                            'next': fact('A exact next')},
                      'B': {'tasks': fact(['TASK-02']), 'last_action': fact('B private result')}},
            'tasks': {'TASK-01': {'details': fact('full detail '*100+'原始阻塞'),
                                 'current_action': fact('must wait for approval'),
                                 'variants': [fact('PASS'), fact('FAIL')]},
                      'TASK-02': {'details': fact('B private details')}},
            'current_conflicts': [{**fact('TASK-01 PASS/FAIL conflict'), 'task': 'TASK-01'},
                                  {**fact('TASK-02 conflict'), 'task': 'TASK-02'}],
            'conditions': [{**fact('TASK-01 needs user decision'), 'task': 'TASK-01'}],
            'risks': [{**fact('TASK-01 unresolved gate'), 'task': 'TASK-01'}],
            'current_handoff': [fact('TASK-01 handoff original')], 'ocr': {'runs': []},
            'strong_review': {'authorization': fact('AUTHORIZED'), 'tokens': fact({'total': 999})},
            'sources': {'TODO.md': {'sha256': hashlib.sha256(todo.read_bytes()).hexdigest()}}}
        digest = hashlib.sha256(json.dumps(full, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        full['fingerprint'] = digest
        folder = root/'inbox/control/runtime/progress'
        (folder/'evidence').mkdir(parents=True)
        evidence = folder/'evidence'/(digest+'.json')
        evidence.write_text(json.dumps(full, ensure_ascii=False))
        (folder/'facts.json').write_text(json.dumps({'project': str(root), 'evidence_fingerprint': digest,
            'evidence_reference': str(evidence), 'tasks': {'TASK-01': {}, 'TASK-02': {}}}))
        return evidence

    def test_prompt_embeds_role_sections_and_exact_cache_ratio_without_double_count(self):
        usage = {'calls': 1, 'usage_calls': 1, 'routes': {'local': {'known_tokens': {
            'input_tokens': 10, 'cache_read_tokens': 20, 'cache_write_tokens': 5,
            'prompt_tokens': 35, 'output_tokens': 3, 'total_tokens': 38}}}}
        packet = report_summary._usage_packet(usage)
        self.assertEqual(35, packet['cache']['known_prompt_tokens'])
        self.assertEqual(20/35, packet['cache']['known_cache_read_ratio'])
        self.assertIsNone(report_summary._usage_packet({})['cache']['known_cache_read_ratio'])
        for role, sections in [('COORDINATOR', ('整体目标与进度', '执行者进度', '测试者进度',
                'OCR情况和后续计划', '强审查情况', '风险', '下一步计划')),
                ('A', ('当前工作', '本次完成', '阻塞与依赖', '下一步计划'))]:
            prompt = report_summary.summary_prompt({'role': role, 'roles': []})
            for section in sections:
                self.assertIn('**'+section+'**', prompt)
            self.assertIn('缓存读token/已知prompt总token', prompt)
            self.assertIn('分母未知或为0时标未知', prompt)

    def test_current_evidence_expands_related_originals_and_keeps_conflicts_readonly(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._saved_progress(root)
            before = {path: path.read_bytes() for path in root.rglob('*') if path.is_file()}
            result = report_summary.expand_current_evidence(root, 'A',
                at=datetime.fromisoformat('2026-10-16T08:00:00+08:00'))
            self.assertEqual('VERIFIED', result['state'])
            self.assertIn('原始阻塞', result['facts']['tasks']['TASK-01']['details']['value'])
            self.assertEqual(2, len(result['facts']['tasks']['TASK-01']['variants']))
            self.assertEqual(1, len(result['facts']['current_conflicts']))
            self.assertNotIn('TASK-02', result['facts']['tasks'])
            self.assertNotIn('B private', json.dumps(result))
            self.assertEqual(before, {path: path.read_bytes() for path in root.rglob('*') if path.is_file()})
            coordinator = report_summary.expand_current_evidence(root, 'COORDINATOR',
                at=datetime.fromisoformat('2026-10-16T08:00:00+08:00'))
            self.assertEqual(2, len(coordinator['facts']['current_conflicts']))
            self.assertNotIn('tokens', coordinator['facts']['strong_review'])

    def test_actual_progress_publication_expands_current_conflicting_todo_variants(self):
        from runtime_tools import refresh_progress_facts
        at = datetime.fromisoformat('2026-10-16T08:00:00+08:00')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'ROLE-BINDINGS.json').write_text(json.dumps({'project_root': str(root),
                'control': 'ACTIVE', 'roles': {'A': {}}}))
            (root/'TODO.md').write_text('| TASK-01 功能 | REQ | 无 | 产物 | RUNNING / A |\n'
                                       '| TASK-01 功能 | REQ | 无 | 产物 | DONE / A |\n')
            with patch('progress_projection._strong_review', return_value={}):
                published = refresh_progress_facts(root, at=at)
            before = {path: path.read_bytes() for path in root.rglob('*') if path.is_file()}
            result = report_summary.expand_current_evidence(root, 'A', at=at)
            self.assertEqual('VERIFIED', result['state'])
            self.assertTrue(str(root/result['source']) == published['evidence_reference'])
            self.assertEqual('CONFLICT', result['facts']['tasks']['TASK-01']['state']['value'])
            self.assertEqual(1, len(result['facts']['tasks']['TASK-01']['variants']))
            original_and_variants = json.dumps(result['facts']['tasks']['TASK-01'], ensure_ascii=False)
            self.assertIn('RUNNING / A', original_and_variants)
            self.assertIn('DONE / A', original_and_variants)
            self.assertTrue(result['facts']['current_conflicts'])
            self.assertEqual(before, {path: path.read_bytes() for path in root.rglob('*') if path.is_file()})

    def test_changed_or_escaping_evidence_is_explicit_and_critical_facts_are_never_budget_dropped(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)/'project'
            root.mkdir()
            self._saved_progress(root)
            (root/'TODO.md').write_text('changed source')
            result = report_summary.expand_current_evidence(root, 'A',
                at=datetime.fromisoformat('2026-10-16T08:00:00+08:00'))
            self.assertEqual('UNVERIFIED', result['state'])
            self.assertTrue(any(row['reason'] == 'source_changed_since_evidence' for row in result['gaps']))
            self.assertEqual(1, len(result['facts']['current_conflicts']))
            folder = root/'inbox/control/runtime/progress'
            (folder/'facts.json').write_text(json.dumps({'project': str(root),
                'evidence_reference': str(Path(temp)/'outside.json'), 'evidence_fingerprint': 'f'*64}))
            refused = report_summary.expand_current_evidence(root, 'A',
                at=datetime.fromisoformat('2026-10-16T08:00:00+08:00'))
            self.assertEqual('UNAVAILABLE', refused['state'])
            self.assertIn('evidence_reference_outside_bound_directory', refused['gaps'][0]['reason'])
        with self.assertRaisesRegex(ValueError, '关键证据.*预算'):
            report_summary.summary_prompt({'role': 'A', 'roles': [], 'current_evidence': {
                'facts': {'current_conflicts': [{'value': '不能隐藏的冲突'*10000}]}, 'gaps': []}})

    def test_summary_uses_role_window_without_observer_refresh_or_business_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)/'project'
            home = Path(temp)/'home'
            root.mkdir()
            (root/'ROLE-BINDINGS.json').write_text(json.dumps({'project_root': str(root), 'control': 'ACTIVE',
                'roles': {'A': {'poll_job_id': 'a'}, 'B': {'poll_job_id': 'b'}}}))
            (root/'TODO.md').write_text('| TASK-01 | RUNNING | feature | | | detail | A |\n')
            for role in ('A', 'B'):
                source = root/f'events/{role}/work.json'
                source.parent.mkdir(parents=True)
                source.write_text(json.dumps({'role': role, 'event_id': role+':done', 'task': 'TASK-01',
                    'attempt': '1', 'status': 'DELIVERED', 'summary': role+' result',
                    'occurred_at': '2026-10-16T10:00:00+08:00', 'recorded_at': '2026-10-16T10:00:00+08:00'}))
                usage_accounting.record(home/'runtime/role-usage.db', 'end', session_id=role, api_request_id=role,
                    physical_attempt_id=role, role=role, project=root, route='cloud', model='cloud', provider='custom',
                    started_at='2026-10-16T10:00:00+08:00', ended_at='2026-10-16T10:00:01+08:00',
                    usage={'input_tokens': 10, 'output_tokens': 2})
            before = (root/'TODO.md').read_bytes()
            activity = report_summary.activity_module()
            import role_observer
            with patch.object(report_summary, 'activity_module', return_value=activity), \
                 patch.object(role_observer, 'refresh', side_effect=AssertionError('summary must not scan/refresh business inputs')):
                result = report_summary.build_summary_input(root, 'A', 'phase',
                    at=datetime.fromisoformat('2026-10-16T11:00:00+08:00'), home=home)
            self.assertEqual(before, (root/'TODO.md').read_bytes())
            self.assertEqual(['A'], [row['role'] for row in result['facts']['roles']])
            self.assertEqual(['A'], list(result['facts']['role_states']))
            self.assertEqual('待核实', result['facts']['role_states']['A']['label'])
            self.assertEqual(1, result['facts']['roles'][0]['model_usage']['calls'])
            self.assertIn('A result', result['prompt'])
            self.assertNotIn('B result', result['prompt'])
            self.assertFalse((root/'inbox/control/runtime/leases').exists())
            self.assertFalse((root/'inbox/control/runtime/activity').exists())
            self.assertTrue(Path(result['input_artifact']).is_file())

    def test_summary_bounds_details_and_preserves_exact_aggregate_coverage(self):
        packet = {'role': 'COORDINATOR', 'roles': []}
        for role in ('COORDINATOR', 'A', 'B', 'C', 'TESTER'):
            packet['roles'].append({'role': role, 'omitted_tasks': 0,
                'task_results': [{'summary': '很长的任务内容'*1000} for _ in range(10)],
                'recent_intervals': [], 'model_usage': {'calls': 80, 'usage_calls': 79,
                    'omitted_model_groups': 0, 'models': [{'description': 'model'*1000}]}})
        prompt = report_summary.summary_prompt(packet)
        self.assertLess(len(prompt.encode()), report_summary.MAX_INPUT_BYTES + 3000)
        parsed = json.loads(prompt.split('时段事实JSON：\n', 1)[1])
        self.assertEqual(80, parsed['roles'][0]['model_usage']['calls'])
        self.assertEqual(79, parsed['roles'][0]['model_usage']['usage_calls'])
        self.assertEqual(10, parsed['roles'][0]['omitted_tasks'])

    def test_morning_keeps_team_results_but_only_coordinator_usage_and_night_claims(self):
        with tempfile.TemporaryDirectory() as temp:
            root, home = Path(temp)/'project', Path(temp)/'home'
            root.mkdir()
            roles = ('COORDINATOR', 'A', 'B', 'C', 'TESTER')
            (root/'ROLE-BINDINGS.json').write_text(json.dumps({'schema_version': 1,
                'project_root': str(root), 'control': 'ACTIVE', 'roles': {role: {} for role in roles}}))
            indexes = root/'inbox/control/runtime/dispatch-index'
            indexes.mkdir(parents=True)
            (indexes/'A.json').write_text(json.dumps({'schema_version': 1, 'role': 'A',
                'observed_at': '2026-10-16T08:00:00+08:00', 'verified_no_task': True, 'entries': []}))
            for role in ('COORDINATOR', 'A'):
                event = root/f'events/{role}/night.json'
                event.parent.mkdir(parents=True)
                event.write_text(json.dumps({'role': role, 'event_id': role+':night', 'task': 'TASK-01',
                    'attempt': '1', 'status': 'DELIVERED', 'summary': role+' night result',
                    'occurred_at': '2026-10-15T22:00:00+08:00', 'recorded_at': '2026-10-15T22:00:00+08:00'}))
                usage_accounting.record(home/'runtime/role-usage.db', 'end', session_id=role, api_request_id=role,
                    physical_attempt_id=role, role=role, project=root, route='cloud', model=role+'-private-model',
                    started_at='2026-10-15T22:00:00+08:00', ended_at='2026-10-15T22:00:01+08:00',
                    usage={'input_tokens': 10, 'output_tokens': 2})
            notices = root/'inbox/control/runtime/claim-outbox/A'
            notices.mkdir(parents=True)
            for name, at in [('night', '2026-10-15T23:00:00+08:00'), ('old', '2026-10-14T23:00:00+08:00')]:
                (notices/(name+'.json')).write_text(json.dumps({'schema_version': 1, 'role': 'A',
                    'task': name, 'attempt': '1', 'occurred_at': at, 'status': 'SUPPRESSED', 'text': name+' claim'}))
            before = {path: path.read_bytes() for path in notices.glob('*.json')}
            result = report_summary.build_summary_input(root, 'COORDINATOR', 'morning',
                at=datetime.fromisoformat('2026-10-16T08:00:00+08:00'), home=home)
            facts = result['facts']
            self.assertEqual(list(roles), [item['role'] for item in facts['roles']])
            self.assertEqual(1, facts['roles'][0]['model_usage']['calls'])
            self.assertEqual('空闲', facts['role_states']['A']['label'])
            self.assertEqual('待核实', facts['role_states']['B']['label'])
            self.assertIn('执行者 A（空闲）', result['prompt'])
            self.assertTrue(all('model_usage' not in item for item in facts['roles'][1:]))
            self.assertTrue(all('model_usage' not in item for item in facts['evening_supplement'][1:]))
            self.assertIn('A night result', result['prompt'])
            self.assertNotIn('A-private-model', result['prompt'])
            self.assertEqual(['night'], [row['task'] for row in facts['night_claim_notifications']])
            self.assertEqual(before, {path: path.read_bytes() for path in notices.glob('*.json')})

    def test_morning_after_migration_does_not_collect_unbound_roles_or_claims(self):
        with tempfile.TemporaryDirectory() as temp:
            root, home = Path(temp)/'project', Path(temp)/'home'
            root.mkdir()
            (root/'ROLE-BINDINGS.json').write_text(json.dumps({'schema_version': 1,
                'project_root': str(root), 'control': 'ACTIVE', 'roles': {'COORDINATOR': {}}}))
            (root/'TODO.md').write_text('# Current project\n')
            old = root/'events/A/old.json'; old.parent.mkdir(parents=True)
            old.write_text(json.dumps({'role': 'A', 'event_id': 'old', 'task': 'TASK-01',
                'attempt': '1', 'status': 'DELIVERED', 'summary': 'MIGRATED_PRIVATE_RESULT',
                'occurred_at': '2026-10-18T23:00:00+08:00', 'recorded_at': '2026-10-18T23:00:00+08:00'}))
            with patch('task_claim.pending_claim_notifications', side_effect=AssertionError('no bound executor')):
                result = report_summary.build_summary_input(root, 'COORDINATOR', 'morning',
                    at=datetime.fromisoformat('2026-10-19T08:00:00+08:00'), home=home)
            packet = result['facts']
            self.assertEqual(['COORDINATOR'], [row['role'] for row in packet['roles']])
            self.assertEqual(['COORDINATOR'], [row['role'] for row in packet['evening_supplement']])
            self.assertEqual({}, packet['role_states'])
            self.assertEqual([], packet['claim_collection_errors'])
            self.assertNotIn('MIGRATED_PRIVATE_RESULT', result['prompt'])

    def test_generation_state_tracks_exact_native_execution_failure_and_delivery(self):
        from cron import executions
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            token = set_hermes_home_override(home)
            try:
                with patch.object(executions, 'EXECUTIONS_FILE', home/'cron/executions.db'):
                    failed = executions.create_execution('report-job', source='test')
                    artifact = home/'facts.input.json'
                    artifact.write_text(json.dumps({'generation': {'state': 'INPUT_READY', 'execution': {
                        'database': str(home/'cron/executions.db'), 'job_id': 'report-job', 'execution_id': failed['id']}}}))
                    self.assertEqual('INPUT_READY', report_summary.summary_status(artifact)['state'])
                    executions.finish_execution(failed['id'], success=False, error='model unavailable', delivery_outcome='delivered')
                    state = report_summary.summary_status(artifact)
                    self.assertEqual('FAILED', state['state'])
                    self.assertEqual('ERROR_NOTICE',state['delivery_kind'])
                    self.assertFalse(state['summary_delivered'])
                    self.assertIn('本次总结未生成', state['message'])
                    self.assertEqual('delivered', state['delivery_outcome'])  # Existing error notice, not a second send.
                    succeeded = executions.create_execution('report-job', source='test')
                    executions.finish_execution(succeeded['id'], success=True, delivery_outcome='delivered')
                    self.assertEqual('FAILED', report_summary.summary_status(artifact)['state'])
                    data = json.loads(artifact.read_text())
                    data['generation']['execution']['execution_id'] = succeeded['id']
                    artifact.write_text(json.dumps(data))
                    self.assertEqual('NATIVE_COMPLETED', report_summary.summary_status(artifact)['state'])
                    silent = executions.create_execution('report-job', source='test')
                    from cron.scheduler import _classify_delivery_outcome
                    suppressed = _classify_delivery_outcome(delivery_error=None, should_deliver=False,
                        unresolved_origin=False, normalized_deliver='feishu:test', incident_acked=False, success=True)
                    self.assertEqual('suppressed', suppressed)
                    executions.finish_execution(silent['id'], success=True, delivery_outcome=suppressed)
                    data['generation']['execution']['execution_id'] = silent['id']
                    artifact.write_text(json.dumps(data))
                    state = report_summary.summary_status(artifact)
                    self.assertEqual('NATIVE_COMPLETED', state['state'])
                    self.assertIn('正文尚未核实', state['message'])
            finally:
                reset_hermes_home_override(token)


if __name__ == '__main__':
    unittest.main()
