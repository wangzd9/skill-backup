from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from progress_query import collect_live, live_progress_report, live_progress_tool_packet
from tests.test_report_contract import fixture


class LiveQueryTests(unittest.TestCase):
    def test_bad_observer_evidence_does_not_erase_independent_runtime_or_documents(self):
        import json
        from types import SimpleNamespace
        import role_status
        role_status.runtime_module()
        import role_observer
        import runtime_tools
        import live_progress
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packet = fixture(); packet['project'] = str(root)
            observation = {'kind': 'live-runtime-observation', 'checked_at': '2026-10-19T08:00:00+08:00',
                'state': 'VERIFIED', 'roles': {'A': {'state': 'VERIFIED', 'owner': {'state': 'alive'},
                    'checkpoint': {}, 'errors': []}}, 'ocr': [], 'strong_review': {}, 'errors': []}
            states = {'A': {'status': 'IDLE', 'label': '空闲'}}
            runtime = SimpleNamespace(binding=lambda _: (root, {'roles': {'COORDINATOR': {}, 'A': {}}}))
            with patch.object(role_status, 'runtime_module', return_value=runtime), \
                 patch.object(role_observer, 'collect', side_effect=ValueError('OCR conflicting runtime index: bad.json')), \
                 patch.object(runtime_tools, 'refresh_progress_facts', return_value=packet), \
                 patch.object(live_progress, 'collect_live_runtime', return_value=observation) as live, \
                 patch.object(role_status, 'collect_role_statuses', return_value=states):
                snapshot = collect_live(root)
            live.assert_called_once_with(root, ocr_candidates=None)
            projection = snapshot['projection']
            self.assertEqual(packet['tasks'], projection['tasks'])
            observed = projection['runtime_observation']
            self.assertEqual('UNVERIFIED', observed['state'])
            self.assertEqual('VERIFIED', observed['roles']['A']['state'])
            self.assertIn('OCR conflicting', observed['collection_gaps'][0])
            self.assertEqual(observed['collection_gaps'], json.loads(Path(observed['evidence_reference']).read_text())['collection_gaps'])
            from report_contract import build_report, render_report
            report = render_report(build_report(projection))
            self.assertIn('执行者 A（空闲）', report)
            self.assertIn('OCR conflicting', report)
            self.assertIn('进程存活', report)

    def test_failed_projection_returns_fresh_runtime_and_explicit_gap_without_cached_prose(self):
        from types import SimpleNamespace
        import role_status
        role_status.runtime_module()
        import role_observer
        import runtime_tools
        import live_progress
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root/'inbox/control/runtime/progress'; folder.mkdir(parents=True)
            (folder/'facts.json').write_text('{"old_cached_marker":"DO NOT REPLAY"}')
            runtime = SimpleNamespace(binding=lambda _: (root, {'roles': {'COORDINATOR': {}}}))
            observation = {'state': 'VERIFIED', 'roles': {}, 'ocr': [], 'strong_review': {}, 'errors': []}
            with patch.object(role_status, 'runtime_module', return_value=runtime), \
                 patch.object(role_observer, 'collect', return_value=({'role': 'COORDINATOR', 'project': str(root), 'observed_at': 'now'}, {})), \
                 patch.object(runtime_tools, 'refresh_progress_facts', side_effect=ValueError('TODO unreadable')), \
                 patch.object(live_progress, 'collect_live_runtime', return_value=observation), \
                 patch.object(role_status, 'collect_role_statuses', return_value={}):
                result = collect_live(root)['projection']
            self.assertEqual({}, result['tasks'])
            self.assertTrue(Path(result['evidence_reference']).is_file())
            self.assertIn('TODO unreadable', result['runtime_observation']['collection_gaps'][0])
            self.assertNotIn('DO NOT REPLAY', str(result))

    def test_queries_observe_changed_runtime_without_consuming_business_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pending = root/'pending.json'
            pending.write_text('{"fingerprint":"new-input","status":"PENDING"}')
            def snapshot(state):
                packet = fixture()
                packet['project'] = str(root)
                packet['runtime_observation'] = {'kind': 'live-runtime-observation',
                    'checked_at': '2026-10-16T18:00:00+08:00', 'state': 'VERIFIED',
                    'roles': {'A': {'state': 'VERIFIED', 'owner': {'state': state},
                        'checkpoint': {'source': 'A.json', 'value': {'task': 'TASK-16C-A',
                            'attempt': 'attempt-1', 'recorded_at': '2026-10-16T17:20:00+08:00'}},
                        'errors': [], 'operations': []}}, 'ocr': [], 'strong_review': {}, 'errors': []}
                return {'role': 'COORDINATOR', 'project': str(root), 'projection': packet}
            with patch('progress_query.collect_live', side_effect=[snapshot('alive'), snapshot('exited_or_reused')]) as collect:
                first = live_progress_report(root)
                second = live_progress_report(root)
            self.assertEqual(collect.call_count, 2)
            self.assertEqual(['整体目标与进度','执行者进度','测试者进度','OCR情况和后续计划',
                              '强审查情况','风险','下一步计划'],
                             first['report_presentation']['sections'])
            self.assertIn('进程存活', first['markdown'])
            self.assertIn('已退出', second['markdown'])
            self.assertIn('17:20', second['markdown'])
            self.assertEqual(pending.read_text(), '{"fingerprint":"new-input","status":"PENDING"}')

    def test_failed_runtime_collection_never_returns_an_old_report(self):
        with patch('progress_query.collect_live', side_effect=ValueError('service identity unavailable')):
            with self.assertRaisesRegex(ValueError, 'service identity'):
                live_progress_report('/tmp/project')

    def test_tool_packet_preserves_facts_and_points_to_complete_observation_without_replay(self):
        import hashlib
        import json
        with tempfile.TemporaryDirectory() as temporary:
            report={'kind':'role-progress-report','role':'A','project':temporary,
                'report_basis':'live_runtime_with_latest_records','checked_at':'2026-10-16T18:00:00+08:00',
                'markdown':'执行者 A（待核实）：owner身份未核实；下一步核实当前派单。',
                'analysis_instructions':'根据当次核验分析，不执行任务。',
                'sections':[{'raw':'x'*200000}],
                'runtime_observation':{'state':'UNVERIFIED','errors':['identity_missing']}}
            with patch('progress_query.live_progress_report',return_value=report):
                packet=live_progress_tool_packet(temporary,'A')
            self.assertEqual(packet['markdown'],report['markdown'])
            self.assertEqual(packet['runtime_errors'],['identity_missing'])
            raw=Path(packet['artifact']['path']).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(),packet['artifact']['sha256'])
            self.assertEqual(json.loads(raw),report)
            self.assertNotIn('sections',packet)
            self.assertLess(len(json.dumps(packet)),2000)
