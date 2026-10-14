import json
from datetime import datetime
from pathlib import Path
import tempfile
import unittest

from progress_projection import build_projection, parse_task_rows, reporting_packet
from progress_display import render_progress

AT = datetime.fromisoformat('2026-10-14T14:00:00+08:00')
TODO = '''> **当前执行决定（2026-10-14T11:25:19+08:00）：恢复最小界面查询、追溯和获取闭环。**
| 任务 | REQ / AC | 硬依赖 | 可验收产物 | 状态 / owner |
|---|---|---|---|---|
| TASK-16C 界面最小补齐 | 16 / 16 | TASK-16C-A、TASK-16C-B、TASK-16C-C | 三视图与详情获取 | DISPATCHED / 协调者 |
| TASK-16C-A 查询/API | 16 / 16 | 固定seed | 分类、日期、分页与集合获取 | DELIVERED_FROZEN / 待OCR与独立验证 |
| TASK-16C-B Bag三视图 | 16 / 16 | 固定seed | 三视图与条件保留 | DELIVERED_FROZEN / 待OCR与独立验证 |
| TASK-16C-C 流水线详情 | 16 / 16 | 固定seed | 八步详情与操作隔离 | DELIVERED_REVIEW_PENDING / 返修已交付，待独立验证 |
| TASK-16D 独立联合验收 | 16 / 16 | 三功能接受后固定集成 | 浏览器、API和MySQL | ACKED / TESTER |
| TASK-11S-BATCH-1012 批次收尾 | 19 / 19 | 原包 | 大包终态与复用验收 | RUNNING / A |
'''


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root/'TODO.md').write_text(TODO)
        (self.root/'HANDOFF.md').write_text('> **2026-10-14 13:36 协调者检查点**：开发已交付，独立验证进行中。\n\n# 历史\nUI暂停。')
        (self.root/'ROLE-BINDINGS.json').write_text(json.dumps({'project_root':str(self.root), 'control':'ACTIVE', 'roles':{}}))
        for role, folder in (('A','1号'), ('B','2号'), ('C','3号')):
            path = self.root/'inbox'/folder/('TASK-16C-'+role)/'attempt-1-dispatch.md'
            path.parent.mkdir(parents=True)
            path.write_text('task: TASK-16C-'+role+'\nstatus: DISPATCHED')

    def event(self, role, **values):
        path = self.root/'events'/role/'latest.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'role':role, 'event_id':'latest-'+role, **values}))
        return path

    def test_complete_id_and_function_survive_both_table_formats(self):
        rows = parse_task_rows(TODO)
        self.assertIn('TASK-11S-BATCH-1012', rows)
        self.assertEqual(rows['TASK-16C-A']['title'], '查询/API')
        self.assertEqual(rows['TASK-16C-A']['state'], 'DELIVERED_FROZEN')
        self.assertEqual(rows['TASK-16C-A']['line'], 5)
        old = '| TASK-02 | ✅ | 数据迁移 | REQ-02 | TASK-01 | DONE（测试已完成） | 1号 / attempt-1 |'
        self.assertEqual(parse_task_rows(old)['TASK-02']['title'], '数据迁移')

    def test_report_publication_preserves_exact_evidence_across_refreshes(self):
        from runtime_tools import refresh_progress_facts
        first=refresh_progress_facts(self.root,at=AT)
        evidence=Path(first['evidence_reference'])
        original=evidence.read_bytes()
        self.assertEqual(json.loads(original)['fingerprint'],first['evidence_fingerprint'])
        (self.root/'TODO.md').write_text(TODO.replace('DELIVERED_FROZEN','REWORK'))
        second=refresh_progress_facts(self.root,at=AT)
        self.assertNotEqual(first['evidence_reference'],second['evidence_reference'])
        self.assertEqual(evidence.read_bytes(),original)
        self.assertEqual(json.loads((self.root/'inbox/control/runtime/progress/facts.json').read_text()),second)
        self.assertEqual(second['tasks']['TASK-16C-A']['state']['value'],'REWORK')

    def test_delivery_idle_and_completed_ocr_never_become_testing_or_merge(self):
        self.event('B', task='TASK-16C-B', status='IDLE', occurred_at='2026-10-14T13:12:02+08:00',
                   summary='PRIVATE_LOG frozen head rc=0 叶')
        packet = build_projection(self.root, at=AT)
        task = packet['tasks']['TASK-16C-B']
        self.assertEqual(task['state']['value'], 'DELIVERED_FROZEN')
        self.assertEqual(task['delivery']['value'], 'DELIVERED')
        self.assertEqual(task['testing']['value'], 'NOT_CONFIRMED')
        self.assertEqual(task['merge']['value'], 'NOT_CONFIRMED')
        self.assertEqual(packet['roles']['B']['activity']['value'], 'IDLE')
        self.assertIn('TASK-16C-B', packet['roles']['B']['tasks']['value'])

    def test_every_fact_carries_source_and_time_and_no_file_time_is_occurrence(self):
        packet = build_projection(self.root, at=AT)
        fact = packet['tasks']['TASK-16C-A']['state']
        self.assertEqual(fact['source'], 'TODO.md')
        self.assertEqual(fact['observed_at'], '2026-10-14 14:00 +08:00')
        self.assertIsNone(fact['occurred_at'])
        self.assertTrue(fact['source_updated_at'].endswith('+08:00'))
        self.assertEqual(packet['goal']['occurred_at'], '2026-10-14 11:25 +08:00')
        self.assertEqual(len(packet['sources']['TODO.md']['sha256']), 64)

    def test_conflicting_same_id_and_conflicting_todo_rows_are_retained(self):
        self.event('A', task='TASK-16C-A', status='DONE', occurred_at='2026-10-14T13:50:00+08:00')
        mirror = self.root/'inbox/events/A/mirror.json'
        mirror.parent.mkdir(parents=True)
        mirror.write_text(json.dumps({'role':'A', 'event_id':'latest-A', 'task':'TASK-16C-A',
                                     'status':'BLOCKED', 'occurred_at':'2026-10-14T13:50:00+08:00'}))
        with (self.root/'TODO.md').open('a') as out:
            out.write('| TASK-16C-A 查询/API | 16 / 16 | 固定seed | 分类查询 | DONE / A |\n')
        packet = build_projection(self.root, at=AT)
        self.assertEqual(packet['tasks']['TASK-16C-A']['state']['value'], 'CONFLICT')
        self.assertGreaterEqual(len(packet['conflicts']), 2)

    def test_indexed_ocr_and_declared_queue_stay_separate(self):
        directory = self.root/'reviews/TASK-16C-C/ocr-run'
        directory.mkdir(parents=True)
        (directory/'spec.json').write_text(json.dumps({'task':'TASK-16C-C','head':'522bb3f','run_id':'ocr-c'}))
        (directory/'state.json').write_text(json.dumps({'state':'AWAITING_DECISION', 'updated_at':'2026-10-14T04:46:09+00:00',
                                                     'batches':[{'state':'REVIEW_COMPLETE','returncode':0}]}))
        index = self.root/'inbox/control/runtime/ocr/current.json'
        index.parent.mkdir(parents=True)
        index.write_text(json.dumps({'run':str(directory), 'updated_at':'2026-10-14T04:24:00+00:00'}))
        packet = build_projection(self.root, at=AT)
        run = packet['ocr']['runs'][0]
        self.assertEqual(run['state']['value'], 'AWAITING_DECISION')
        self.assertEqual(run['state']['occurred_at'], '2026-10-14 12:46 +08:00')
        self.assertEqual(packet['tasks']['TASK-16C-C']['state']['value'], 'DELIVERED_REVIEW_PENDING')
        self.assertEqual(packet['tasks']['TASK-16C-C']['testing']['value'], 'NOT_CONFIRMED')

    def test_report_has_required_sections_and_uses_functions_without_raw_tech_summary(self):
        self.event('B', task='TASK-16C-B', status='IDLE', occurred_at='2026-10-14T13:12:00+08:00', summary='PRIVATE_LOG 冻结head rc=0 叶')
        packet = build_projection(self.root, at=AT)
        answer = render_progress({'role':'COORDINATOR','projection':packet}, at=AT)
        for label in ('整体目标与进度', '执行者功能进度', '测试者', 'OCR 当前与后续', '强审', '风险', '下一步'):
            self.assertIn('**'+label+'**', answer)
        for role in ('A','B','C'):
            self.assertIn('**执行者 '+role+' 当前**',answer)
        self.assertNotIn('<details>',answer)
        self.assertIn('费用：未知',answer)
        body = answer.split('<details>')[0]
        for value in ('PRIVATE_LOG','冻结head','rc=0','叶','TASK-16C-A'):
            self.assertNotIn(value, body)
        self.assertIn('查询/API', body)
        self.assertIn('Bag三视图', body)
        self.assertIn('流水线详情', body)
        self.assertNotIn('%', body)

    def test_coordinator_review_actor_does_not_take_executor_ownership(self):
        (self.root/'TODO.md').write_text(TODO.replace('DELIVERED_FROZEN / 待OCR与独立验证','DELIVERED_FROZEN / 协调者机械核验全绿'))
        packet = build_projection(self.root,at=AT)
        self.assertEqual(packet['tasks']['TASK-16C-A']['owner']['value'],'A')
        self.assertEqual(packet['tasks']['TASK-16C-B']['owner']['value'],'B')
        self.assertIn('TASK-16C-A',packet['roles']['A']['tasks']['value'])

    def test_dispatch_outside_todo_and_old_undocumented_status_keep_their_evidence(self):
        self.event('A',occurred_at='2020-01-01T00:00:00+08:00',summary='进展'*10000)
        self.event('TESTER',status='IDLE',summary='曾无任务')
        path=self.root/'inbox/TESTER/TASK-NEW/anything.md'
        path.parent.mkdir(parents=True)
        path.write_text('# 派单\nstatus: PENDING\n')
        packet=build_projection(self.root,at=AT)
        answer=render_progress({'role':'A','projection':packet},at=AT)
        self.assertIn('2020-01-01',answer)
        self.assertIn('历史动作时间',answer)
        self.assertIn('非生产实时探测',answer)
        self.assertLess(len(answer),3000)
        tester=render_progress({'role':'TESTER','projection':packet},at=AT)
        self.assertIn('新派单',tester)
        self.assertNotIn('TASK-NEW',packet['tasks'])

    def test_projection_read_has_no_business_or_runtime_writes(self):
        before = {str(p.relative_to(self.root)):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        build_projection(self.root, at=AT)
        after = {str(p.relative_to(self.root)):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_reporting_packet_retains_every_current_task_stage_and_exact_gate_evidence(self):
        self.event('A', task='TASK-16C-A',status='REVIEW_PENDING',occurred_at='2026-10-14T13:12:00+08:00',summary='HEAD技术证据 '*300)
        with (self.root/'TODO.md').open('a') as out:
            out.write('| TASK-12S-E 后续功能 | 20 / 20 | 当前界面接受 | 幂等绑定 | DEFERRED_AFTER_V04；未派单 |\n')
            out.write('| TASK-99 已接受 | 20 / 20 | 无 | 已验收 | DONE / A |\n')
        full=build_projection(self.root,at=AT)
        compact=reporting_packet(full,evidence_reference='run/evidence.json')
        self.assertEqual(set(compact['tasks']),set(full['overall']['current_tasks']['value']))
        self.assertNotIn('TASK-99',compact['tasks'])
        self.assertNotIn('TASK-12S-E',compact['tasks'])
        for task in compact['tasks']:
            for field in ('state','delivery','testing','merge'):
                self.assertEqual(compact['tasks'][task][field]['value'],full['tasks'][task][field]['value'])
        self.assertEqual(compact['risks'],full['risks'])
        self.assertEqual(compact['ocr'],full['ocr'])
        self.assertEqual(compact['strong_review'],full['strong_review'])
        fact=compact['roles']['A']['last_action']
        self.assertIn('run/evidence.json#/roles/A/last_action',fact['evidence_reference'])
        self.assertEqual(fact['source'],full['roles']['A']['last_action']['source'])
        self.assertEqual(fact['occurred_at'],full['roles']['A']['last_action']['occurred_at'])
        self.assertLess(len(json.dumps(compact,ensure_ascii=False)),len(json.dumps(full,ensure_ascii=False)))

    def test_reporting_packet_keeps_current_conflicts_and_indexes_historical_variants(self):
        self.event('B',task='TASK-16C-B',status='IDLE',occurred_at='2020-01-01T00:00:00+08:00')
        mirror=self.root/'inbox/events/B/mirror.json'
        mirror.parent.mkdir(parents=True)
        mirror.write_text(json.dumps({'role':'B','event_id':'latest-B','task':'TASK-16C-B','status':'BLOCKED',
                                     'occurred_at':'2020-01-01T00:00:00+08:00','summary':'HISTORICAL_VARIANT '*1000}))
        (self.root/'events/B/history.json').write_text((self.root/'events/B/latest.json').read_text())
        self.event('B',event_id='newer-B',task='TASK-16C-B',status='REVIEW_PENDING',occurred_at='2026-10-14T13:12:00+08:00')
        full=build_projection(self.root,at=AT)
        compact=reporting_packet(full)
        self.assertEqual(compact['current_conflicts'],full['current_conflicts'])
        historical=compact['historical_conflicts']
        self.assertGreater(historical['count']['value'],0)
        self.assertTrue(historical['sources'][0]['evidence_reference'].startswith('evidence.json#/conflicts/'))
        self.assertNotIn('HISTORICAL_VARIANT',json.dumps(compact,ensure_ascii=False))
        self.assertEqual(render_progress({'role':'COORDINATOR','projection':full},at=AT),
                         render_progress({'role':'COORDINATOR','projection':compact},at=AT))

    def test_batch_state_column_and_deferred_after_current_phase_are_recognized(self):
        text = '''## 10. 整批水雾处理验收（2026-10-13）
| 任务 | 状态 | 输入/依赖 | 完成条件 | 当前动作 |
|---|---|---|---|---|
| TASK-11S-BATCH-1012 | RUNNING（已领取） | 原包 | 独立验收 | A 唯一上传提交 owner |
| 任务 | REQ / AC | 硬依赖 | 可验收产物 | 状态 / owner |
|---|---|---|---|---|
| TASK-12S-E 自动绑定 | 20 / 20 | 当前界面接受 | 幂等绑定 | DEFERRED_AFTER_V04；未派单 |
'''
        (self.root/'TODO.md').write_text(text)
        packet = build_projection(self.root, at=AT)
        batch = packet['tasks']['TASK-11S-BATCH-1012']
        self.assertEqual(batch['state']['value'],'RUNNING')
        self.assertEqual(batch['title']['value'],'整批水雾处理验收')
        self.assertEqual(batch['owner']['value'],'A')
        self.assertNotIn('TASK-12S-E',packet['overall']['current_tasks']['value'])

    def test_role_done_does_not_claim_aggregate_business_acceptance(self):
        self.event('B', task='TASK-11S-BATCH-1012', status='DONE', occurred_at='2026-10-14T13:12:00+08:00')
        packet = build_projection(self.root, at=AT)
        self.assertEqual(packet['tasks']['TASK-11S-BATCH-1012']['state']['value'],'RUNNING')
        self.assertEqual(packet['roles']['B']['activity']['value'],'DONE')
        self.assertEqual(packet['conflicts'],[])
        self.event('B', task='TASK-11S-BATCH-1012', status='DONE', business_state='DONE', occurred_at='2026-10-14T13:12:00+08:00')
        packet = build_projection(self.root, at=AT)
        self.assertEqual(packet['conflicts'][0]['todo_source']['value'],'RUNNING')

    def test_todo_acceptance_keeps_role_result_and_does_not_claim_merge(self):
        self.event('C',task='TASK-16C-C',status='DELIVERED_REVIEW_PENDING',occurred_at='2026-10-14T13:11:00+08:00')
        before=reporting_packet(build_projection(self.root,at=AT))
        self.assertEqual(before['tasks']['TASK-16C-C']['state']['value'],'DELIVERED_REVIEW_PENDING')
        (self.root/'TODO.md').write_text(TODO.replace('DELIVERED_REVIEW_PENDING / 返修已交付，待独立验证',
                           '✅ 接受（开发收口，未合入主分支）；TESTER 独立验证 PASS'))
        full=build_projection(self.root,at=AT)
        compact=reporting_packet(full)
        self.assertNotIn('TASK-16C-C',compact['overall']['current_tasks']['value'])
        self.assertIn('TASK-16C-C',compact['tasks'])
        self.assertEqual(compact['tasks']['TASK-16C-C']['state']['value'],'DONE')
        self.assertEqual(compact['tasks']['TASK-16C-C']['merge']['value'],'NOT_MERGED')
        self.assertEqual(compact['roles']['C']['activity']['value'],'DELIVERED_REVIEW_PENDING')
        answer=render_progress({'role':'COORDINATOR','projection':compact},at=AT)
        self.assertIn('流水线详情，**开发交付已接受，尚未合入主分支**',answer)
        self.assertIn('**执行者 C 下一步**：未记录新派单，等待协调者接续安排',answer)
        self.assertNotIn('流水线详情，**返修已交付',answer)

    def test_recorded_independent_pass_is_not_reported_as_still_waiting_for_validation(self):
        (self.root/'TODO.md').write_text(TODO.replace('DELIVERED_FROZEN / 待OCR与独立验证',
                      'DELIVERED_FROZEN / 待OCR；TESTER MySQL 关键行为已 PASS',1))
        packet=reporting_packet(build_projection(self.root,at=AT))
        self.assertEqual(packet['tasks']['TASK-16C-A']['testing']['value'],'RECORDED_PASSED')
        answer=render_progress({'role':'COORDINATOR','projection':packet},at=AT)
        self.assertIn('查询/API，**已交付，独立验证已通过，等待审查接受**',answer)
        self.assertIn('**执行者 A 下一步**：等待协调者审查并接受交付',answer)

    def test_current_decision_is_shown_without_hiding_stale_ocr_runtime_state(self):
        directory = self.root/'reviews/TASK-16C-C/ocr-run'
        directory.mkdir(parents=True)
        (directory/'spec.json').write_text(json.dumps({'task':'TASK-16C-C','head':'522bb3f','run_id':'ocr-c'}))
        (directory/'state.json').write_text(json.dumps({'state':'AWAITING_DECISION','updated_at':'2026-10-14T12:46:00+08:00'}))
        index = self.root/'inbox/control/runtime/ocr/current.json'
        index.parent.mkdir(parents=True)
        index.write_text(json.dumps({'run':str(directory)}))
        decision = self.root/'inbox/coordinator/TASK-16C-C/ocr-adjudication.md'
        decision.parent.mkdir(parents=True)
        decision.write_text('# OCR 裁决 run ocr-c\n裁决时间：2026-10-14 13:00 +08:00\n最小返修已派。')
        packet = build_projection(self.root, at=AT)
        self.assertEqual(packet['ocr']['runs'][0]['state']['value'],'AWAITING_DECISION')
        self.assertEqual(packet['ocr']['runs'][0]['adjudication']['occurred_at'],'2026-10-14 13:00 +08:00')
        self.assertIn('已有对应裁决文档',packet['current_conflicts'][0]['value'])
        answer = render_progress({'role':'COORDINATOR','projection':packet},at=AT).split('<details>')[0]
        self.assertIn('已有审查裁决',answer)
        self.assertNotIn('审查已结束，等待裁决',answer)

    def test_runtime_timeout_is_reported_as_incomplete_and_blocks_release(self):
        directory = self.root/'reviews/TASK-16C-A/ocr-run'
        directory.mkdir(parents=True)
        (directory/'spec.json').write_text(json.dumps({'task':'TASK-16C-A','head':'522bb3f','run_id':'ocr-a'}))
        (directory/'state.json').write_text(json.dumps({'state':'HARD_DEADLINE','updated_at':'2026-10-14T13:46:00+08:00'}))
        index = self.root/'inbox/control/runtime/ocr/current.json'
        index.parent.mkdir(parents=True)
        index.write_text(json.dumps({'run':str(directory)}))
        packet = build_projection(self.root, at=AT)
        answer = render_progress({'role':'COORDINATOR','projection':packet},at=AT)
        self.assertIn('达到运行时限，审查未完成',answer)
        self.assertIn('不能据此放行合入',answer)
        self.assertIn('协调者核查未完成审查的终因和覆盖范围',answer)
        self.assertEqual(packet['tasks']['TASK-16C-A']['state']['value'],'DELIVERED_FROZEN')


if __name__ == '__main__':
    unittest.main()
