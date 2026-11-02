"""Report-only language changes against isolated facts; never business rulings."""
import copy
import importlib.util
import sys
from pathlib import Path
from unittest import mock

from test_business_ledger import LedgerFixture
from workflow_report_text import language_error, plain_text
from workflow_reports import (COORDINATOR_SECTIONS, _coordinator_progress_format_error,
    render_live_progress_fallback, transform_coordinator_notice)


class ReportLanguageTests(LedgerFixture):
    def packet(self):
        return {'checked_at': '2026-11-02T10:20:00+08:00', 'event_high_water': 10,
            'run': {'run_id': 'run-one', 'requirement_id': 'isolated-fixture'},
            'roles': [], 'tasks': [{'task_id': 'T1', 'role': 'B', 'attempt': 1,
                'phase': 'REVIEW', 'candidate_id': 'isolated-candidate',
                'recorded_checks': {'ocr': ['NOT_RUN'], 'independent_test': ['PASS']}}],
            'ocr_serial_gate': {'blocked': True, 'blocking_count': 1},
            'ocr_version_gate': {'state': 'OPEN'},
            'external_actions': [{'kind': 'ocr', 'run_id': 'run-one', 'task_id': 'T1',
                'state': 'SUBMITTED', 'current_candidate': True, 'action_id': 'isolated-review'}]}

    def report(self, text):
        return '**协调者｜当前进展核验**\n\n' + '\n\n'.join(
            '**' + section + '**\n\n- **状态：** ' + (text if i == 0 else '本区没有新增结果。')
            for i, section in enumerate(COORDINATOR_SECTIONS))

    def test_status_translation_keeps_untested_failed_and_unknown_distinct(self):
        output = plain_text('独立测试 PASS，3 passed；数据库补验 NOT_RUN；审查 INCOMPLETE；执行 FAILED；结果 UNKNOWN。')
        for value in ('独立测试 通过', '3 项通过', '尚未执行', '结果不完整', '执行失败', '结果尚未确认'):
            self.assertIn(value, output)
        self.assertEqual(1, output.count('3'))
        self.assertNotIn('全部通过', output)

    def test_commands_paths_protocols_and_identifiers_remain_exact(self):
        refs = ['`python /tmp/worker.py --scope REVIEW`', '`git show REVIEW`', '`门禁.md`', '/tmp/门禁/worker.py',
                'doc/门禁/worker.py', 'TASK-UNKNOWN-01', 'scene-result-batch/v2',
                'https://example.invalid/worker/REVIEW', 'b54b4e94']
        output = plain_text('REVIEW；' + '；'.join(refs))
        self.assertTrue(output.startswith('等待审查'))
        for reference in refs:
            self.assertIn(reference, output)
        self.assertIsNone(language_error('文件 `/tmp/UNKNOWN.py` 仍待检查。MySQL 8.4和NAS尚未测试。'))

    def test_format_guard_rejects_internal_dump_but_keeps_chinese_report(self):
        self.assertIsNotNone(_coordinator_progress_format_error(self.report(
            '当前 run requires_user_approval=false，OCR RESULT_READY，work已park。')))
        self.assertIsNone(_coordinator_progress_format_error(self.report(
            '前置功能还在审查，原任务和进度已保留，完成后自动继续。')))

    def test_fallback_keeps_true_checks_without_exposing_internal_states(self):
        packet = self.packet(); before = copy.deepcopy(packet)
        text = render_live_progress_fallback(packet, 'COORDINATOR')
        self.assertIsNone(_coordinator_progress_format_error(text), text)
        self.assertIn('代码审查', text)
        self.assertIn('等待审查', text)
        self.assertIn('独立测试已通过', text)
        self.assertIn('尚无 OCR 通过', text)
        self.assertNotIn('RESULT_READY', text)
        self.assertNotIn('SUBMITTED', text)
        self.assertNotIn('isolated-review', text)
        self.assertEqual(before, packet)
        packet['external_actions'][0]['state'] = 'RESULT_READY'
        ready = render_live_progress_fallback(packet, 'COORDINATOR')
        self.assertIn('审查报告已返回，由协调者核对', ready)
        self.assertNotIn('补齐OCR结果', ready)

    def test_manual_and_native_delivery_use_readable_facts_and_leave_business_unchanged(self):
        spec = importlib.util.spec_from_file_location('report_language_plugin_test',
            Path(__file__).resolve().parents[1] / '__init__.py')
        module = importlib.util.module_from_spec(spec); sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        pool = self.root / 'pool.json'; pool.write_text('{}')
        plugin = module.RuntimePlugin(registry=self.root / 'registry', home=self.root,
            metrics_url='unused', pool_path=pool)
        plugin.manual_progress_turns[('session', 'turn')] = 'COORDINATOR'
        plugin.manual_turns[('session', 'turn')] = {'session_kind': 'group_chat', 'role': 'COORDINATOR'}
        old = self.report('审查 RESULT_READY；requires_user_approval=false。')
        before = self.ledger.snapshot()
        with mock.patch.object(module.workflow, 'active', return_value=True), \
                mock.patch('workflow_query.live_progress', return_value=self.packet()), \
                mock.patch('workflow_runtime.open_ledger', return_value=self.ledger):
            manual = plugin.transform_manual_progress_output(old, session_id='session', turn_id='turn')
            result = transform_coordinator_notice({'kind': 'business', 'workflow_version': 1,
                'pool_path': str(pool), 'role': 'COORDINATOR', 'run_id': 'run-one',
                'execution_id': 'isolated-language'}, 'isolated-language', old, self.root,
                kind='COORDINATOR_PROGRESS')
        self.assertEqual('BUSINESS_PROGRESS_QUEUED', result['metadata']['reason'])
        queued = self.ledger.outbox_items(include_terminal=True)[0]['payload']['text']
        self.assertEqual(manual, queued)
        self.assertIsNone(_coordinator_progress_format_error(queued))
        after = self.ledger.snapshot()
        for key in ('tasks', 'candidates', 'verifications', 'decisions', 'merges', 'events', 'roles', 'executions'):
            self.assertEqual(before[key], after[key], key)

    def test_periodic_translation_leaves_evidence_references_and_input_unchanged(self):
        from report_analysis import normalize
        value = {'evidence_ids': ['event:88'], 'sections': {'风险': [
            {'label': '缺项', 'text': '独立测试 PASS；MySQL补验 NOT_RUN；审查结果 UNKNOWN。'}]}}
        before = copy.deepcopy(value)
        output = normalize(value, 'COORDINATOR')
        self.assertEqual(before, value)
        self.assertEqual(['event:88'], output['evidence_ids'])
        self.assertIn('尚未执行', output['sections']['风险'])
        self.assertIn('结果尚未确认', output['sections']['风险'])

    def test_attention_explains_ready_and_incomplete_reviews_without_rewriting_options(self):
        from workflow_reports import render_notification
        item = {'kind': 'COORDINATION_ATTENTION_REQUIRED', 'payload': {
            'user_questions': [{'question': '如何处理？', 'options': ['保持 MySQL 8.4', '另作安排'], 'reason': '范围待确认'}],
            'reviews': [{'task_id': 'T1', 'state': 'RESULT_READY'}, {'task_id': 'T2', 'state': 'INCOMPLETE'}]}}
        before = copy.deepcopy(item)
        text = render_notification(self.ledger, item)
        self.assertIn('审查报告已返回', text)
        self.assertIn('审查结果不完整', text)
        self.assertNotIn('RESULT_READY', text)
        self.assertIn('保持 MySQL 8.4', text)
        self.assertEqual(before, item)
