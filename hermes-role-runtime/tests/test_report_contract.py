from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from report_contract import build_report, render_report, COORDINATOR_SECTIONS


def fact(value, source='TODO.md', **metadata):
    return {'value': value, 'source': source, 'line': 8, 'observed_at': '2026-10-14 16:00 +08:00',
            'occurred_at': None, 'recorded_at': None, **metadata}


def fixture():
    def task(title, state, testing='NOT_CONFIRMED', merge='NOT_CONFIRMED'):
        return {key: fact(val) for key, val in {'title': title, 'state': state, 'testing': testing,
                'merge': merge, 'delivery': 'DELIVERED', 'dependencies': '固定代码版本',
                'details': 'PRIVATE_RAW A叶 seed ff2a005 rc=1'}.items()}
    def role(task, related=(), completed=()):
        return {'primary_task': fact(task), 'tasks': fact(([task] if task else []) + list(related)),
                'related_tasks': fact(list(related)), 'recent_completed': fact(list(completed)),
                'activity_task': fact(task), 'activity': fact('IDLE'), 'next': fact('PRIVATE_NEXT')}
    return {'project': '/project', 'fingerprint': 'snapshot123', 'evidence_reference': '/evidence/snapshot123.json',
            'observed_at': '2026-10-14 16:00 +08:00', 'goal': fact('完成查询与获取闭环'),
            'overall': {'current_tasks': fact(['TASK-16C-A', 'TASK-16C-B', 'TASK-16D', 'TASK-11S'])},
            'tasks': {'TASK-16C-A': task('查询/API', 'DELIVERED_FROZEN', 'RECORDED_PASSED'),
                      'TASK-16C-B': task('Bag三视图', 'DELIVERED_FROZEN'),
                      'TASK-16C-C': task('流水线详情', 'DONE', 'RECORDED_PASSED', 'NOT_MERGED'),
                      'TASK-16D': task('独立联合验收', 'ACKED'),
                      'TASK-11S': task('整批水雾处理', 'RUNNING')},
            'roles': {'A': role('TASK-16C-A', ['TASK-11S']), 'B': role('TASK-16C-B'),
                      'C': role(None, completed=['TASK-16C-C']), 'TESTER': role('TASK-16D')},
            'ocr': {'runs': [], 'declared': []}, 'strong_review': {
                'authorization': fact('REVOKED'), 'used_last_60_minutes': fact(0), 'limit': fact(5),
                'active_calls': fact([]), 'cost': fact(None), 'last_call': fact({'status': 'SUCCEEDED'})},
            'risks': [], 'current_conflicts': [], 'conflicts': []}


class ReportContractTests(unittest.TestCase):
    def setUp(self):
        self.packet = fixture()

    def test_coordinator_contract_has_all_seven_ordered_sections_and_one_group_per_executor(self):
        report = build_report(self.packet)
        self.assertEqual([(s['key'], s['title']) for s in report['sections']], list(COORDINATOR_SECTIONS))
        groups = [i.get('group') for i in report['sections'][1]['items']]
        compressed = [g for n, g in enumerate(groups) if n == 0 or groups[n - 1] != g]
        self.assertEqual(compressed, ['执行者 A', '执行者 B', '执行者 C'])
        text = render_report(report)
        self.assertEqual(text.count('**执行者 A**'), 1)
        self.assertEqual(text.count('**当前任务：** 查询/API'), 1)
        self.assertIn('**关联事项：** 整批水雾处理', text)
        self.assertNotIn('**当前任务：** 整批水雾处理', text)

    def test_subroles_include_only_their_own_work_and_necessary_dependencies(self):
        text = render_report(build_report(self.packet, 'A'))
        for unrelated in ('Bag三视图', '流水线详情', '独立联合验收', '强审查情况', '执行者 B'):
            self.assertNotIn(unrelated, text)
        self.assertIn('查询/API', text)
        self.assertEqual([s['title'] for s in build_report(self.packet, 'A')['sections']],
                         ['当前工作', '本次完成', '阻塞与依赖', '下一步计划'])
        tester = render_report(build_report(self.packet, 'TESTER'))
        self.assertNotIn('整批水雾处理', tester)
        self.assertNotIn('执行者进度', tester)
        self.assertNotIn('强审查情况', tester)
        self.assertIn('已有验证记录', tester)
        self.assertNotIn('查询/API', tester)  # No demonstrated link to this validation assignment.

    def test_source_evidence_is_preserved_without_raw_technical_narrative_in_model_input(self):
        before = deepcopy(self.packet)
        report = build_report(self.packet)
        self.assertEqual(self.packet, before)
        serialized = json.dumps(report, ensure_ascii=False)
        self.assertNotIn('PRIVATE_RAW', serialized)
        self.assertNotIn('PRIVATE_NEXT', serialized)
        self.assertNotIn('ff2a005', serialized)
        for section in report['sections']:
            for item in section['items']:
                for source in item['sources']:
                    self.assertTrue(source['evidence_reference'].startswith('/evidence/snapshot123.json#/'))
                    self.assertIn('source', source)

    def test_delivery_testing_acceptance_and_merge_never_collapse(self):
        text = render_report(build_report(self.packet))
        self.assertIn('查询/API，**已交付，独立验证有通过记录，等待核对并接受**', text)
        self.assertIn('Bag三视图，**已交付，等待审查和独立验证**', text)
        self.assertIn('流水线详情，**开发交付已接受，尚未合入主分支**', text)
        self.assertNotIn('全部完成', text)

    def test_mtime_only_dispatch_never_becomes_an_unacknowledged_new_task(self):
        self.packet['roles']['A']['dispatch'] = fact({'task': 'TASK-16C-A', 'newer_than_activity': True,
                                                      'source_mtime': 99999999999, 'status': 'UNKNOWN'})
        text = render_report(build_report(self.packet))
        self.assertNotIn('尚未确认领取', text)
        self.packet['roles']['A']['dispatch']['value'].update(acknowledged=False, notification_kind='new_task')
        self.assertIn('新派单尚未确认领取', render_report(build_report(self.packet)))

    def test_resolved_and_nonblocking_ocr_do_not_create_false_blockers(self):
        self.packet['ocr']['declared'] = [
            {'task': fact('TASK-16C-B'), 'detail': fact('版本门禁已解除，OCR排队'),
             'category': 'resolved', 'is_blocking': False, 'queue_state': 'QUEUED'},
            {'task': fact('TASK-11S'), 'detail': fact('未覆盖低优先补审候选，不阻塞'),
             'category': 'nonblocking', 'is_blocking': False}]
        report = build_report(self.packet)
        risks = report['sections'][5]['items']
        self.assertEqual(len(risks), 1)
        self.assertIn('未记录新的明确阻塞', risks[0]['text'])
        ocr = '\n'.join(i['text'] for i in report['sections'][3]['items'])
        self.assertIn('已解除', ocr)
        self.assertIn('等待串行审查', ocr)
        self.assertNotIn('前置条件仍未满足', ocr)

    def test_unknown_ocr_is_neither_a_pass_nor_an_invented_blocker(self):
        self.packet['ocr']['declared'] = [{'task': fact('TASK-16C-A'), 'detail': fact('现状待核对'),
            'category': 'unknown', 'is_blocking': None}]
        risks = build_report(self.packet)['sections'][5]['items']
        self.assertIn('查询/API', risks[0]['text'])
        self.assertIn('不能据此认定通过或阻塞', risks[0]['text'])
        self.assertNotIn('前置条件尚未满足', risks[0]['text'])

    def test_failed_ocr_never_releases_gate_and_unrelated_failure_stays_out_of_executor_report(self):
        self.packet['ocr']['runs'] = [{'task': fact('TASK-16C-B'), 'state': fact('HARD_DEADLINE'),
                                      'gate_decision': fact('NOT_EVALUATED')}]
        coordinator = render_report(build_report(self.packet))
        self.assertIn('达到运行时限，审查未完成', coordinator)
        self.assertIn('不能据此放行对应功能', coordinator)
        self.assertNotIn('Bag三视图', render_report(build_report(self.packet, 'A')))

    def test_newer_retry_replaces_old_failure_in_current_report_without_deleting_evidence(self):
        self.packet['ocr']['runs'] = [
            {'task': fact('TASK-16C-A'), 'state': fact('HARD_DEADLINE', occurred_at='2026-10-14T15:14:47+08:00')},
            {'task': fact('TASK-16C-A'), 'state': fact('RUNNING', occurred_at='2026-10-14T16:10:00+08:00')}]
        before = deepcopy(self.packet)
        text = render_report(build_report(self.packet))
        self.assertIn('正在审查', text)
        self.assertNotIn('达到运行时限', text)
        self.assertEqual(self.packet, before)

    def test_blocked_executor_next_action_does_not_tell_it_to_continue_development(self):
        self.packet['tasks']['TASK-16C-A']['state']['value'] = 'BLOCKED'
        next_section = build_report(self.packet, 'A')['sections'][-1]
        self.assertIn('等待派单列明的前置条件', next_section['items'][0]['text'])
        self.assertNotIn('继续开发', next_section['items'][0]['text'])

    def test_goal_selects_named_business_milestone_not_historical_instruction(self):
        self.packet['goal']['value'] = '用户恢复 v0.4 最小界面闭环，并要求原 OCR 未修项进入任务。'
        goal = build_report(self.packet)['sections'][0]['items'][0]['text']
        self.assertEqual(goal, '**目标：** v0.4 最小界面闭环')

    def test_tester_results_are_limited_to_explicit_assignment_dependencies(self):
        self.packet['tasks']['TASK-16D']['dependencies']['value'] = 'TASK-16C-A'
        text = render_report(build_report(self.packet, 'TESTER'))
        self.assertIn('已记录通过', text)
        self.assertIn('查询/API', text)
        self.assertNotIn('流水线详情', text)

    def test_old_independent_pass_cannot_validate_a_new_delivery_snapshot(self):
        row=self.packet['tasks']['TASK-16C-A']
        row['delivery_snapshot']=fact({'commit':'new1234','status':'DELIVERED','role':'A'})
        row['validation_records']=[fact({'commit':'old1234','conclusion':'PASS_RECORDED',
            'scope':'INCREMENTAL','current_snapshot':'DIFFERENT'},source='test-receipts/TEST-TASK16D/attempt-1.md')]
        text=render_report(build_report(self.packet,'A'))
        self.assertIn('此前版本验证通过，当前版本需增量验证',text)
        self.assertIn('等待测试者完成当前返修的增量验证、协调者安排适用审查',text)
        self.assertNotIn('当前版本增量验证有通过记录',text)

    def test_tester_reports_actual_scoped_receipts_without_claiming_the_new_snapshot_passed(self):
        row=self.packet['tasks']['TASK-16C-A']
        row['validation_records']=[fact({'commit':'old1234','conclusion':'PASS_RECORDED',
            'scope':'REWORK','current_snapshot':'DIFFERENT'},source='test-receipts/TEST-TASK16D/attempt-1.md')]
        self.packet['tasks']['TASK-16C-C']['validation_records']=[fact({'commit':'other1234','conclusion':'PASS_RECORDED',
            'scope':'INCREMENTAL','current_snapshot':'MATCH'},source='test-receipts/TEST-TASK99/attempt-1.md')]
        text=render_report(build_report(self.packet,'TESTER'))
        self.assertIn('查询/API',text)
        self.assertIn('返修验证有通过记录',text)
        self.assertIn('对应此前版本，当前返修需补增量验证',text)
        self.assertNotIn('流水线详情',text)
        self.assertNotIn('范围、版本对应关系仍需核对',text)

    def test_completed_developer_observation_responsibility_does_not_become_idle(self):
        self.packet['roles']['C'].update(observed_tasks=fact(['TASK-11S']),
            activity_focus=fact({'task':'TASK-11S','kind':'OBSERVATION'}))
        text=render_report(build_report(self.packet,'C'))
        self.assertIn('最近完成',text)
        self.assertIn('持续观察',text)
        self.assertIn('继续观察 整批水雾处理 的后台结果',text)
        self.assertNotIn('等待协调者接续安排',text)

    def test_final_adjudication_is_reported_without_auto_accepting_the_task(self):
        self.packet['ocr']['runs']=[{'task':fact('TASK-16C-B'),'state':fact('AWAITING_DECISION'),
            'adjudication':fact('RECORDED'),'current_snapshot':fact('MATCH'),
            'adjudication_records':[fact({'final_record':True,'followup_kind':'NO_BLOCKING_RECORDED'})]}]
        text=render_report(build_report(self.packet))
        self.assertIn('审查记录已明确无阻塞，接受和合入分别确认',text)
        self.assertNotIn('审查已结束，等待协调者裁决',text)
        self.assertIn('Bag三视图，**已交付，OCR已记录当前版本无阻塞，等待核对独立验证并接受**',text)

    def test_global_next_actions_reuse_current_adjudication_instead_of_requesting_another_review(self):
        self.packet['ocr']['runs']=[{'task':fact('TASK-16C-B'),'state':fact('AWAITING_DECISION'),
            'current_snapshot':fact('MATCH'), 'adjudication_records':[
                fact({'final_record':True,'followup_kind':'NO_BLOCKING_RECORDED'})]}]
        actions=build_report(self.packet)['sections'][-1]['items']
        pending=[item['text'] for item in actions if '审查与验证' in item['text']]
        ready=[item['text'] for item in actions if '接受核对' in item['text']]
        self.assertTrue(any('查询/API' in text for text in pending))
        self.assertFalse(any('Bag三视图' in text for text in pending))
        self.assertTrue(any('Bag三视图' in text and '已有审查裁决' in text for text in ready))
        self.assertEqual(self.packet['tasks']['TASK-16C-B']['state']['value'],'DELIVERED_FROZEN')

    def test_running_incremental_review_takes_precedence_over_older_run_adjudication(self):
        self.packet['ocr']['runs']=[
            {'task':fact('TASK-16C-A'),'state':fact('AWAITING_DECISION',occurred_at='2026-10-14T16:00:00+08:00'),
             'adjudication':fact('RECORDED'),'current_snapshot':fact('DIFFERENT'),
             'adjudication_records':[fact({'final_record':True,'followup_kind':'REWORK'})]},
            {'task':fact('TASK-16C-A'),'state':fact('RUNNING',occurred_at='2026-10-14T17:47:00+08:00')}
        ]
        text=render_report(build_report(self.packet))
        self.assertIn('查询/API，**正在审查**',text)
        self.assertNotIn('查询/API，**已裁决，需要返修**',text)

    def test_executor_next_actions_never_grant_it_acceptance_ocr_or_independent_verification(self):
        for role in ('A','B','C'):
            with self.subTest(role=role):
                task='TASK-16C-'+role
                self.packet['tasks'][task]['state']['value']='DELIVERED'
                self.packet['roles'][role]['primary_task']['value']=task
                self.packet['tasks'][task]['validation_records']=[fact({'conclusion':'PASS_RECORDED',
                    'scope':'INCREMENTAL','current_snapshot':'MATCH'})]
                self.packet['ocr']['runs']=[{'task':fact(task),'state':fact('AWAITING_DECISION'),
                    'current_snapshot':fact('MATCH'),'adjudication_records':[
                        fact({'final_record':True,'followup_kind':'NO_BLOCKING_RECORDED'})]}]
                action=build_report(self.packet,role)['sections'][-1]['items'][0]['text']
                self.assertTrue(action.startswith('等待协调者'),action)
                self.assertNotIn('作出接受决定',action)
                self.assertNotIn('触发OCR',action)
                self.assertNotIn('执行独立验证',action)
        self.packet['tasks']['TASK-16D']['state']['value']='DELIVERED'
        action=build_report(self.packet,'TESTER')['sections'][-1]['items'][0]['text']
        self.assertTrue(action.startswith('等待协调者裁决验证报告'),action)

    def test_current_matching_verification_hides_superseded_gap_claim_from_brief_report(self):
        self.packet['tasks']['TASK-16C-C']['validation_records']=[
            fact({'conclusion':'PASS_RECORDED','scope':'INCREMENTAL','current_snapshot':'DIFFERENT'},
                 source='test-receipts/TEST-TASK16D/attempt-1.md'),
            fact({'conclusion':'PASS_RECORDED','scope':'REWORK','current_snapshot':'MATCH'},
                 source='test-receipts/TEST-TASK16D/attempt-1.md')]
        report=build_report(self.packet,'TESTER')
        text=render_report(report)
        self.assertIn('流水线详情：** 返修验证有通过记录；对应当前交付版本',text)
        self.assertNotIn('当前返修需补增量验证',text)
        self.assertEqual(len(self.packet['tasks']['TASK-16C-C']['validation_records']),2)

    def test_ocr_section_contains_a_plan_for_the_actual_run_state(self):
        self.packet['ocr']['runs']=[{'task':fact('TASK-16C-A'),'state':fact('RUNNING')}]
        section=build_report(self.packet)['sections'][3]
        self.assertIn('后续计划',section['items'][-1]['text'])
        self.assertIn('核对覆盖和结果',section['items'][-1]['text'])
        self.packet['ocr']['runs'][0]['state']['value']='HARD_DEADLINE'
        section=build_report(self.packet)['sections'][3]
        self.assertIn('不自动增加重跑',section['items'][-1]['text'])

    def test_successful_strong_review_process_does_not_imply_review_pass(self):
        text = render_report(build_report(self.packet))
        self.assertIn('已撤销', text)
        self.assertIn('近 60 分钟 0/5', text)
        self.assertIn('调用完成，审查结论以结果文档为准', text)
        self.assertNotIn('强审查通过', text)

    def test_unknown_action_keeps_evidence_time_without_inventing_status(self):
        activity = self.packet['roles']['A']['activity']
        activity.update(value='NOT_RECORDED', occurred_at='2020-01-01T00:00:00+08:00')
        report = build_report(self.packet, 'A')
        text = render_report(report)
        self.assertIn('动作阶段未明确登记（2020-01-01 00:00 +08:00）', text)
        self.assertNotIn('已确认空闲', text)
        items = [item for section in report['sections'] for item in section['items']
                 if '动作阶段未明确登记' in item['text']]
        self.assertEqual(items[0]['sources'][0]['occurred_at'], '2020-01-01T00:00:00+08:00')
        activity.update(occurred_at=None, recorded_at='2026-10-14T15:12:00+08:00')
        self.assertIn('发生时间未记录，补录于 2026-10-14 15:12 +08:00',
                      render_report(build_report(self.packet, 'A')))
        activity.update(recorded_at=None)
        self.assertNotIn('最近记录', render_report(build_report(self.packet, 'A')))

    def test_recorded_or_observation_time_never_becomes_action_occurrence(self):
        activity = self.packet['roles']['A']['activity']
        activity.update(recorded_at='2026-10-14 15:12 +08:00', occurred_at=None)
        text = render_report(build_report(self.packet, 'A'))
        self.assertIn('发生时间未记录，补录于 2026-10-14 15:12 +08:00', text)
        self.assertNotIn('15:12:00', text)
        activity.update(recorded_at=None)
        text = render_report(build_report(self.packet, 'A'))
        self.assertNotIn('最近记录', text)

    def test_report_input_cli_scopes_content_and_does_not_write_business_documents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            todo = '# 查询闭环\n| 任务 | REQ / AC | 硬依赖 | 可验收产物 | 状态 / owner |\n|---|---|---|---|---|\n| TASK-01 查询功能 | 1 | 无 | 查询结果 | RUNNING / A |\n| TASK-02 其他角色功能 | 2 | 无 | 结果 | RUNNING / B |\n'
            (root / 'TODO.md').write_text(todo)
            binding = json.dumps({'project_root': str(root), 'control': 'ACTIVE', 'roles': {}})
            (root / 'ROLE-BINDINGS.json').write_text(binding)
            result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / 'runtime_tools.py'),
                                     'report-input', '--project', str(root), '--role', 'A'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            report = json.loads(result.stdout)
            self.assertEqual(report['role'], 'A')
            self.assertEqual(report['markdown'], render_report(report))
            self.assertNotIn('其他角色功能', result.stdout)
            self.assertTrue(Path(report['evidence_reference']).exists())
            self.assertEqual((root / 'TODO.md').read_text(), todo)
            self.assertEqual((root / 'ROLE-BINDINGS.json').read_text(), binding)


if __name__ == '__main__':
    unittest.main()
