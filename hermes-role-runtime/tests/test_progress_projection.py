import json
import os
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
        for label in ('整体目标与进度', '执行者进度', '测试者进度', 'OCR情况和后续计划', '强审查情况', '风险', '下一步计划'):
            self.assertIn('**'+label+'**', answer)
        for role in ('A','B','C'):
            self.assertIn('**执行者 '+role+'（待核实）**',answer)
        self.assertNotIn('<details>',answer)
        self.assertIn('**费用：** 未知',answer)
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
        self.assertEqual(packet['roles']['A']['activity']['occurred_at'],'2020-01-01 00:00 +08:00')
        self.assertEqual(packet['roles']['A']['last_action']['value'],'进展'*10000)
        self.assertIn('未记录明确的当前派单',answer)
        self.assertIn('2020-01-01 00:00 +08:00',answer)
        self.assertIn('未执行当次运行核验',answer)
        self.assertLess(len(answer),3000)
        tester=render_progress({'role':'TESTER','projection':packet},at=AT)
        self.assertEqual(packet['roles']['TESTER']['dispatch']['value']['notification_kind'],'UNIDENTIFIED_PENDING')
        self.assertTrue(packet['roles']['TESTER']['dispatch']['value']['newer_than_activity'])
        self.assertNotIn('全部完成',tester)
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
        c_section = answer.split('**执行者 C（待核实）**',1)[1].split('**测试者进度**',1)[0]
        self.assertIn('**下一步：** 未记录新派单，等待协调者接续安排',c_section)
        self.assertNotIn('流水线详情，**返修已交付',answer)

    def test_recorded_independent_pass_is_not_reported_as_still_waiting_for_validation(self):
        self.event('A',task='TASK-16C-A',status='REVIEW_PENDING',occurred_at='2026-10-14T13:12:00+08:00')
        (self.root/'TODO.md').write_text(TODO.replace('DELIVERED_FROZEN / 待OCR与独立验证',
                      'DELIVERED_FROZEN / 待OCR；TESTER MySQL 关键行为已 PASS',1))
        packet=reporting_packet(build_projection(self.root,at=AT))
        self.assertEqual(packet['tasks']['TASK-16C-A']['testing']['value'],'RECORDED_PASSED')
        answer=render_progress({'role':'COORDINATOR','projection':packet},at=AT)
        self.assertIn('查询/API，**已交付，独立验证有通过记录，等待核对并接受**',answer)
        a_section = answer.split('**执行者 A（待核实）**',1)[1].split('**执行者 B（待核实）**',1)[0]
        self.assertIn('核对独立验证适用版本',a_section)
        self.assertIn('完成审查与接受',a_section)
        self.assertNotIn('等待协调者审查和测试者独立验证',a_section)

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
        self.assertEqual(packet['current_conflicts'],[])
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
        self.assertIn('不能据此放行对应功能',answer)
        self.assertIn('核对上述未完成审查',answer)
        self.assertIn('再决定返修或补审',answer)
        self.assertNotIn('已接受并合入主分支',answer)
        self.assertEqual(packet['tasks']['TASK-16C-A']['state']['value'],'DELIVERED_FROZEN')

    def ack(self, role, task, dispatch, *, attempt='attempt-1', filename=None, status='ACKED'):
        folder = {'A':'1号','B':'2号','C':'3号','TESTER':'TESTER'}[role]
        path = self.root/'inbox'/folder/task/(filename or (attempt+'-ack.md'))
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text('# '+task+' '+attempt+' ACK\nstatus: '+status+'\n'
                        '- task/attempt: '+task+' / '+attempt+'\n'
                        '- 派单路径: `'+dispatch+'`\n')
        return path

    def test_updated_acknowledged_dispatch_is_not_new_work(self):
        task = 'TASK-16C-A'
        source = 'inbox/1号/'+task+'/attempt-1-dispatch.md'
        self.ack('A',task,source)
        self.event('A',task=task,status='DELIVERED_FROZEN',occurred_at='2026-10-14T13:30:00+08:00')
        with (self.root/source).open('a') as out:
            out.write('\n协调者补充：机械核验通过，仍等OCR。\n')
        os.utime(self.root/source,(AT.timestamp()+3600,AT.timestamp()+3600))
        packet = build_projection(self.root,at=AT)
        dispatch = packet['roles']['A']['dispatch']['value']
        self.assertTrue(dispatch['acknowledged'])
        self.assertEqual(dispatch['notification_kind'],'ACKNOWLEDGED')
        self.assertFalse(dispatch['newer_than_activity'])
        self.assertEqual(dispatch['attempt'],'attempt-1')
        ack_source = dispatch['ack_sources'][0]
        self.assertIn(ack_source,packet['sources'])
        self.assertEqual(dispatch['ack_evidence'][0]['source'],ack_source)
        self.assertEqual(packet['roles']['A']['primary_task']['value'],task)
        self.assertEqual(packet['roles']['A']['related_tasks']['value'],['TASK-11S-BATCH-1012'])
        self.assertEqual(set(packet['roles']['A']['tasks']['value']),{task,'TASK-11S-BATCH-1012'})
        compact = reporting_packet(packet)
        self.assertIn(compact['roles']['A']['primary_task']['value'],compact['tasks'])
        self.assertEqual(compact['tasks']['TASK-11S-BATCH-1012']['state']['value'],'RUNNING')

    def test_ack_must_match_attempt_and_explicit_dispatch_path(self):
        task = 'TASK-16C-A'
        source = 'inbox/1号/'+task+'/attempt-1-dispatch.md'
        self.ack('A',task,source,attempt='attempt-2')
        self.ack('A',task,'inbox/1号/'+task+'/other-correction.md',filename='other-ack.md')
        self.event('A',task=task,status='DELIVERED_FROZEN',occurred_at='2026-10-14T13:30:00+08:00')
        dispatch = build_projection(self.root,at=AT)['roles']['A']['dispatch']['value']
        self.assertFalse(dispatch['acknowledged'])
        self.assertFalse(dispatch['newer_than_activity'])
        self.assertEqual(dispatch['notification_kind'],'EXISTING_ASSIGNMENT')

    def test_explicit_new_rework_alert_ends_after_exact_ack_without_mtime_guess(self):
        task = 'TASK-16C-A'
        original = 'inbox/1号/'+task+'/attempt-1-dispatch.md'
        self.ack('A',task,original)
        self.event('A',task=task,status='DELIVERED_FROZEN',occurred_at='2026-10-14T13:30:00+08:00')
        source = 'inbox/1号/'+task+'/correction-rework.md'
        (self.root/source).write_text('# '+task+' attempt-1 返修派单 2026-10-14 13:40 +08:00\nstatus: REWORK_DISPATCHED\n')
        dispatch_mtime = (self.root/original).stat().st_mtime+10
        os.utime(self.root/source,(dispatch_mtime,dispatch_mtime))
        before = build_projection(self.root,at=AT)['roles']['A']['dispatch']['value']
        self.assertFalse(before['acknowledged'])
        self.assertTrue(before['newer_than_activity'])
        self.assertEqual(before['notification_kind'],'REWORK_PENDING')
        self.ack('A',task,source,filename='correction-ack.md')
        after = build_projection(self.root,at=AT)['roles']['A']['dispatch']['value']
        self.assertTrue(after['acknowledged'])
        self.assertFalse(after['newer_than_activity'])

    def test_new_attempt_is_kept_pending_and_cancelled_ack_does_not_confirm_it(self):
        task = 'TASK-16C-A'
        original = 'inbox/1号/'+task+'/attempt-1-dispatch.md'
        self.ack('A',task,original)
        source = 'inbox/1号/'+task+'/attempt-2-rework.md'
        (self.root/source).write_text('# '+task+' attempt-2 返修派单\nstatus: REWORK_DISPATCHED\n')
        dispatch_mtime = (self.root/original).stat().st_mtime+10
        os.utime(self.root/source,(dispatch_mtime,dispatch_mtime))
        self.ack('A',task,source,attempt='attempt-2',status='CANCELLED')
        dispatch = build_projection(self.root,at=AT)['roles']['A']['dispatch']['value']
        self.assertFalse(dispatch['acknowledged'])
        self.assertTrue(dispatch['newer_than_activity'])
        self.assertEqual(dispatch['notification_kind'],'NEW_ATTEMPT')

    def test_already_handled_same_attempt_correction_is_not_reopened_by_file_edit(self):
        task = 'TASK-16C-A'
        self.event('A',task=task,status='DELIVERED_FROZEN',attempt='attempt-1',occurred_at='2026-10-14T13:50:00+08:00')
        source = 'inbox/1号/'+task+'/correction-rework.md'
        (self.root/source).write_text('# '+task+' attempt-1 返修派单 2026-10-14 13:40 +08:00\nstatus: REWORK_DISPATCHED\n')
        future = (self.root/'inbox/1号'/task/'attempt-1-dispatch.md').stat().st_mtime+3600
        os.utime(self.root/source,(future,future))
        dispatch = build_projection(self.root,at=AT)['roles']['A']['dispatch']['value']
        self.assertFalse(dispatch['newer_than_activity'])
        self.assertEqual(dispatch['notification_kind'],'EXISTING_ASSIGNMENT')

    def test_accepted_result_remains_visible_without_assigning_it_as_current_development(self):
        self.event('C',task='TASK-16C-C',status='DELIVERED_REVIEW_PENDING',occurred_at='2026-10-14T13:00:00+08:00')
        (self.root/'TODO.md').write_text(TODO.replace('DELIVERED_REVIEW_PENDING / 返修已交付，待独立验证',
                                                    'DONE / C；未合入主分支'))
        full = build_projection(self.root,at=AT)
        self.assertIsNone(full['roles']['C']['primary_task']['value'])
        self.assertEqual(full['roles']['C']['recent_completed']['value'],['TASK-16C-C'])
        compact = reporting_packet(full)
        self.assertIn('TASK-16C-C',compact['tasks'])
        self.assertEqual(compact['tasks']['TASK-16C-C']['merge']['value'],'NOT_MERGED')

    def test_current_phase_accepted_result_survives_role_switching_to_background_observation(self):
        path = self.event('C',task='TASK-16C-C',status='DELIVERED_REVIEW_PENDING',occurred_at='2026-10-14T13:00:00+08:00')
        delivered_event = path.with_name('delivered.json')
        delivered_event.write_bytes(path.read_bytes())
        self.event('C',event_id='background-observation',task='TASK-11S-BATCH-1012',status='OBSERVING',occurred_at='2026-10-14T13:55:00+08:00')
        (self.root/'TODO.md').write_text(TODO.replace('DELIVERED_REVIEW_PENDING / 返修已交付，待独立验证',
                                                    'DONE / C；未合入主分支'))
        full = build_projection(self.root,at=AT)
        self.assertEqual(full['roles']['C']['activity_task']['value'],'TASK-11S-BATCH-1012')
        self.assertEqual(full['roles']['C']['recent_completed']['value'],['TASK-16C-C'])
        self.assertEqual(full['roles']['C']['completed_evidence']['TASK-16C-C']['source'],str(delivered_event.relative_to(self.root)))
        compact = reporting_packet(full)
        self.assertIn('TASK-16C-C',compact['tasks'])
        self.assertEqual(compact['tasks']['TASK-16C-C']['state']['value'],'DONE')
        self.assertEqual(compact['tasks']['TASK-16C-C']['merge']['value'],'NOT_MERGED')
        answer = render_progress({'role':'COORDINATOR','projection':compact},at=AT)
        c_section = answer.split('**执行者 C（待核实）**',1)[1].split('**测试者进度**',1)[0]
        self.assertIn('流水线详情',c_section)
        self.assertIn('开发交付已接受，尚未合入主分支',c_section)

    def test_recent_results_do_not_pull_old_phase_done_or_promote_event_only_acceptance(self):
        path = self.event('C',task='TASK-16C-C',status='ACCEPTED',occurred_at='2026-10-14T10:00:00+08:00')
        old = path.with_name('old-phase.json')
        old.write_bytes(path.read_bytes())
        self.event('C',event_id='background-observation',task='TASK-11S-BATCH-1012',status='OBSERVING',occurred_at='2026-10-14T13:55:00+08:00')
        (self.root/'TODO.md').write_text(TODO.replace('DELIVERED_REVIEW_PENDING / 返修已交付，待独立验证',
                                                    'DONE / C；未合入主分支'))
        full = build_projection(self.root,at=AT)
        self.assertEqual(full['roles']['C']['recent_completed']['value'],[])
        path.write_text(json.dumps({'role':'C','event_id':'new-accepted','task':'TASK-16C-C','status':'ACCEPTED',
                                    'occurred_at':'2026-10-14T13:50:00+08:00'}))
        (self.root/'TODO.md').write_text(TODO)
        full = build_projection(self.root,at=AT)
        self.assertEqual(full['roles']['C']['recent_completed']['value'],[])
        self.assertEqual(full['tasks']['TASK-16C-C']['state']['value'],'DELIVERED_REVIEW_PENDING')

    def test_resolved_and_nonblocking_ocr_statements_do_not_create_current_blockers(self):
        todo = TODO.replace('DELIVERED_FROZEN / 待OCR与独立验证',
                            'DELIVERED_FROZEN / OCR 排队（版本门禁已解除，入口验证已完成）',1)
        todo += '| TASK-16R 历史审查盘点 | 16 / 16 | 无 | 历史范围保留 | RUNNING / 协调者；未覆盖低优先补审候选，不阻塞父16收口 |\n'
        (self.root/'TODO.md').write_text(todo)
        directory = self.root/'reviews/TASK-16C-A/ocr-run'
        directory.mkdir(parents=True)
        (directory/'spec.json').write_text(json.dumps({'task':'TASK-16C-A','head':'522bb3f','run_id':'ocr-a'}))
        (directory/'state.json').write_text(json.dumps({'state':'RUNNING','updated_at':'2026-10-14T13:46:00+08:00'}))
        index = self.root/'inbox/control/runtime/ocr/current.json'
        index.parent.mkdir(parents=True)
        index.write_text(json.dumps({'run':str(directory)}))
        packet = build_projection(self.root,at=AT)
        declared = next(row for row in packet['ocr']['declared'] if row['task']['value']=='TASK-16C-A')
        self.assertEqual(declared['category'],'resolved')
        self.assertFalse(declared['is_blocking'])
        self.assertEqual(declared['queue_state'],'QUEUED')
        self.assertIn('版本门禁已解除',declared['detail']['value'])
        self.assertEqual(packet['tasks']['TASK-16C-A']['state']['value'],'DELIVERED_FROZEN')
        self.assertFalse(packet['current_conflicts'])
        self.assertFalse(packet['risks'])
        nonblocking = next(row for row in packet['conditions'] if row['task']=='TASK-16R')
        self.assertEqual(nonblocking['category'],'nonblocking')
        self.assertFalse(nonblocking['is_blocking'])

    def test_acceptance_gate_waiting_for_running_ocr_is_not_an_entry_conflict(self):
        (self.root/'TODO.md').write_text(TODO.replace('DELIVERED_FROZEN / 待OCR与独立验证',
            'DELIVERED_FROZEN / OCR补审 RUNNING；接受门禁维持关闭至补审收口',1))
        directory = self.root/'reviews/TASK-16C-A/ocr-run'
        directory.mkdir(parents=True)
        (directory/'spec.json').write_text(json.dumps({'task':'TASK-16C-A','head':'522bb3f','run_id':'ocr-a'}))
        (directory/'state.json').write_text(json.dumps({'state':'RUNNING','updated_at':'2026-10-14T13:46:00+08:00'}))
        index = self.root/'inbox/control/runtime/ocr/current.json'
        index.parent.mkdir(parents=True)
        index.write_text(json.dumps({'run':str(directory)}))
        packet = build_projection(self.root,at=AT)
        self.assertFalse(packet['current_conflicts'])
        gate = next(row for row in packet['conditions'] if row['gate_kind']=='acceptance')
        self.assertEqual(gate['category'],'blocked')
        self.assertTrue(gate['is_blocking'])
        self.assertIn(gate,packet['risks'])

    def test_real_entry_blocker_still_conflicts_with_active_run_and_mixed_claim_is_unknown(self):
        (self.root/'TODO.md').write_text(TODO.replace('DELIVERED_FROZEN / 待OCR与独立验证',
            'DELIVERED_FROZEN / OCR 被工具链版本门禁阻塞；未覆盖时间映射；版本门禁已解除但参数错误仍阻塞审查',1))
        directory = self.root/'reviews/TASK-16C-A/ocr-run'
        directory.mkdir(parents=True)
        (directory/'spec.json').write_text(json.dumps({'task':'TASK-16C-A','head':'522bb3f','run_id':'ocr-a'}))
        (directory/'state.json').write_text(json.dumps({'state':'RUNNING','updated_at':'2026-10-14T13:46:00+08:00'}))
        index = self.root/'inbox/control/runtime/ocr/current.json'
        index.parent.mkdir(parents=True)
        index.write_text(json.dumps({'run':str(directory)}))
        packet = build_projection(self.root,at=AT)
        self.assertTrue(any('OCR 受阻' in row['value'] for row in packet['current_conflicts']))
        unknown = [row for row in packet['conditions'] if row['category']=='unknown']
        self.assertEqual(len(unknown),2)
        self.assertTrue(all(row['is_blocking'] is None for row in unknown))
        self.assertIn('未覆盖时间映射',[row['value'] for row in unknown])

    def test_explicit_absence_of_blockers_is_not_inferred_as_a_blocker(self):
        for phrase in ('无阻塞','没有阻塞','已无阻塞','不再阻塞'):
            with self.subTest(phrase=phrase):
                (self.root/'TODO.md').write_text(TODO.replace('DELIVERED_FROZEN / 待OCR与独立验证',
                                                          'DELIVERED_FROZEN / OCR '+phrase,1))
                packet = build_projection(self.root,at=AT)
                declaration = next(row for row in packet['ocr']['declared'] if row['task']['value']=='TASK-16C-A')
                self.assertEqual(declaration['category'],'nonblocking')
                self.assertFalse(declaration['is_blocking'])
                self.assertFalse(packet['risks'])
                self.assertEqual(packet['tasks']['TASK-16C-A']['state']['value'],'DELIVERED_FROZEN')

    def test_mixed_entry_resolution_and_acceptance_closure_stays_unresolved_risk(self):
        phrase = 'OCR版本门禁已解除，但接受门禁仍关闭'
        (self.root/'TODO.md').write_text(TODO.replace('DELIVERED_FROZEN / 待OCR与独立验证',
                                                  'DELIVERED_FROZEN / '+phrase,1))
        packet = build_projection(self.root,at=AT)
        declaration = next(row for row in packet['ocr']['declared'] if row['task']['value']=='TASK-16C-A')
        self.assertEqual(declaration['category'],'unknown')
        self.assertIsNone(declaration['is_blocking'])
        self.assertTrue(declaration['unresolved_current_claim'])
        risk = next(row for row in packet['risks'] if row.get('task')=='TASK-16C-A')
        self.assertIn(phrase,risk['value'])
        self.assertEqual(risk['category'],'unknown')
        self.assertIsNone(risk['is_blocking'])
        self.assertEqual(packet['tasks']['TASK-16C-A']['delivery']['value'],'DELIVERED')

    def ocr_run(self, task, identity, head, *, folder, state='AWAITING_DECISION'):
        directory = self.root/'reviews'/task/folder/'run'
        directory.mkdir(parents=True)
        (directory/'spec.json').write_text(json.dumps({'task':task,'run_id':identity,'head':head}))
        (directory/'state.json').write_text(json.dumps({'run_id':identity,'state':state,'updated_at':'2026-10-14T13:30:00+08:00'}))
        index = self.root/'inbox/control/runtime/ocr'/(identity+'.json')
        index.parent.mkdir(parents=True,exist_ok=True)
        index.write_text(json.dumps({'run':str(directory)}))
        return directory

    def test_final_ruling_keeps_old_review_scope_without_covering_new_delivery(self):
        old = self.ocr_run('TASK-16C-A','0aba25ee'+'1'*24,'ff2a005',folder='ocr-retry1')
        (old.parent/'ocr-verdict.md').write_text('# OCR 裁决 ocr-retry1\nhead ff2a005\nF2 REJECTED。')
        ruling = self.root/'inbox/coordinator/TASK-16C-A/ocr-merge-ruling.md'
        ruling.parent.mkdir(parents=True)
        ruling.write_text('# TASK-16C-A 合并裁决\n补审 run `0aba25ee`，head ff2a005。\n'
                          '最小返修=删除死函数，接受门禁维持关闭。\n'
                          '本 ruling（16:42，独立核代码）为最终合并裁决，中间 verdict 被覆盖。')
        self.ocr_run('TASK-16C-A','66ca40d3'+'2'*24,'1489bc6',folder='ocr-r12',state='RUNNING')
        self.event('A',task='TASK-16C-A',status='REVIEW_PENDING',occurred_at='2026-10-14T13:15:00+08:00',
                   evidence='commit 1489bc6，R1-2交付')
        packet = build_projection(self.root,at=AT)
        previous = next(run for run in packet['ocr']['runs'] if run['head']['value']=='ff2a005')
        current = next(run for run in packet['ocr']['runs'] if run['head']['value']=='1489bc6')
        self.assertEqual(previous['adjudication']['source'],str(ruling.relative_to(self.root)))
        self.assertEqual(previous['decision_selection'],'FINAL_RECORDED')
        self.assertEqual(previous['current_snapshot']['value'],'DIFFERENT')
        self.assertEqual(previous['state']['value'],'AWAITING_DECISION')
        self.assertEqual(len(previous['adjudication_records']),2)
        self.assertEqual(current['state']['value'],'RUNNING')
        self.assertEqual(current['current_snapshot']['value'],'MATCH')
        self.assertNotIn('adjudication',current)
        self.assertEqual(packet['tasks']['TASK-16C-A']['state']['value'],'DELIVERED_FROZEN')
        self.assertEqual(packet['current_conflicts'],[])

    def test_run_local_verdict_and_business_acceptance_are_different_evidence(self):
        directory = self.ocr_run('TASK-16C-B','513ab61d'+'1'*24,'e399daf',folder='ocr-run-20261014')
        verdict = directory.parent/'ocr-verdict.md'
        verdict.write_text('# TASK-16C-B OCR 裁决\n记录：2026-10-14 13:20 +08:00；run ocr-run-20261014 head e399daf。\n'
                           'B 叶 OCR 门禁满足（finding 已转 A 清理），B自身无待修。')
        acceptance = self.root/'inbox/coordinator/TASK-16C-B/leaf-acceptance-decision.md'
        acceptance.parent.mkdir(parents=True)
        acceptance.write_text('# TASK-16C-B 接受决定\nrun 513ab61d，head e399daf。\n开发接受，尚未合入。')
        self.event('B',task='TASK-16C-B',status='REVIEW_PENDING',occurred_at='2026-10-14T13:15:00+08:00',
                   evidence='commit e399daf')
        packet = build_projection(self.root,at=AT)
        run = packet['ocr']['runs'][0]
        self.assertEqual(run['adjudication']['source'],str(verdict.relative_to(self.root)))
        self.assertEqual(len(run['adjudication_records']),1)
        self.assertEqual(run['adjudication_records'][0]['value']['followup_kind'],'NO_BLOCKING_RECORDED')
        self.assertEqual(run['adjudication']['recorded_at'],'2026-10-14 13:20 +08:00')
        self.assertEqual(packet['tasks']['TASK-16C-B']['state']['value'],'DELIVERED_FROZEN')
        self.assertEqual(packet['current_conflicts'],[])

    def test_independent_receipts_bind_exact_versions_without_claiming_joint_acceptance(self):
        self.event('A',task='TASK-16C-A',status='REVIEW_PENDING',occurred_at='2026-10-14T13:15:00+08:00',evidence='commit 1489bc6')
        self.event('B',task='TASK-16C-B',status='REVIEW_PENDING',occurred_at='2026-10-14T13:10:00+08:00',evidence='commit e399daf')
        receipt = self.root/'test-receipts/TEST-TASK16D/attempt-1.md'
        receipt.parent.mkdir(parents=True)
        receipt.write_text('# TASK-16D PREP，非联合验收结论\n'
                           '## A 增量独立验证（2026-10-14 13:3x 实测）\n'
                           '被测：TASK-16C-A `563a91a`\n结论（仅 A 增量面）：PASS，非联合验收。\n'
                           '## B 增量独立验证（2026-10-14 13:10 +08:00）\n'
                           '被测：TASK-16C-B `e399daf`\n结论（仅 B 增量面）：PASS。\n'
                           '## 尚未完成的后续检查\n被测：TASK-16C-A `1489bc6`\n等待验证，未得结论。\n')
        old = self.root/'test-receipts/TEST-TASK99/attempt-1.md'
        old.parent.mkdir(parents=True)
        old.write_text('DO_NOT_LOAD_OLD_RECEIPT')
        packet = build_projection(self.root,at=AT)
        a = packet['tasks']['TASK-16C-A']['validation_records']
        b = packet['tasks']['TASK-16C-B']['validation_records']
        self.assertEqual(a[0]['value']['commit'],'563a91a')
        self.assertEqual(a[0]['value']['current_snapshot'],'DIFFERENT')
        self.assertIsNone(a[0]['occurred_at'])
        self.assertEqual(a[1]['value']['conclusion'],'NOT_CONFIRMED')
        self.assertEqual(b[0]['value']['current_snapshot'],'MATCH')
        self.assertEqual(b[0]['value']['conclusion'],'PASS_RECORDED')
        self.assertEqual(packet['tasks']['TASK-16D']['state']['value'],'ACKED')
        self.assertEqual(packet['tasks']['TASK-16C-A']['testing']['value'],'NOT_CONFIRMED')
        self.assertNotIn(str(old.relative_to(self.root)),packet['sources'])
        self.assertEqual(reporting_packet(packet)['tasks']['TASK-16C-A']['validation_records'],a)

    def test_background_observation_preserves_pending_delivery_and_accepted_result(self):
        self.event('A',task='TASK-16C-A',status='REVIEW_PENDING',occurred_at='2026-10-14T12:30:00+08:00',evidence='commit 1489bc6')
        a_delivery = self.root/'events/A/delivery.json'
        a_delivery.write_text((self.root/'events/A/latest.json').read_text())
        self.event('A',event_id='a-observe',task='TASK-11S-BATCH-1012',status='IN_PROGRESS',
                   occurred_at='2026-10-14T13:30:00+08:00',summary='大包只读核实，无生产写入。')
        self.event('C',task='TASK-16C-C',status='LEAF_ACCEPTED',occurred_at='2026-10-14T12:40:00+08:00',evidence='commit ce5921d')
        c_accept = self.root/'events/C/accepted.json'
        c_accept.write_text((self.root/'events/C/latest.json').read_text())
        self.event('C',event_id='c-observe',task='TASK-11S-BATCH-1012',status='OBSERVING',
                   occurred_at='2026-10-14T13:35:00+08:00',summary='大包取消收敛只读观察。')
        (self.root/'TODO.md').write_text(TODO.replace('DELIVERED_REVIEW_PENDING / 返修已交付，待独立验证',
                                                   '✅ 接受（未合入主分支）'))
        packet = build_projection(self.root,at=AT)
        self.assertEqual(packet['roles']['A']['primary_task']['value'],'TASK-16C-A')
        self.assertEqual(packet['roles']['A']['observed_tasks']['value'],['TASK-11S-BATCH-1012'])
        self.assertEqual(packet['roles']['C']['observed_tasks']['value'],['TASK-11S-BATCH-1012'])
        self.assertEqual(packet['roles']['C']['recent_completed']['value'],['TASK-16C-C'])
        self.assertEqual(packet['roles']['C']['tasks']['value'],[])
        self.assertEqual(packet['tasks']['TASK-11S-BATCH-1012']['owner']['value'],'A')
        self.assertEqual(packet['tasks']['TASK-16C-C']['state']['value'],'DONE')
        self.assertEqual(packet['tasks']['TASK-16C-C']['merge']['value'],'NOT_MERGED')

    def test_ambiguous_short_run_identity_does_not_bind_a_ruling(self):
        self.ocr_run('TASK-16C-A','12345678'+'1'*24,'ff2a005',folder='ocr-first')
        self.ocr_run('TASK-16C-A','12345678'+'2'*24,'1489bc6',folder='ocr-second')
        ruling = self.root/'inbox/coordinator/TASK-16C-A/ocr-ruling.md'
        ruling.parent.mkdir(parents=True)
        ruling.write_text('# OCR 裁决\nrun 12345678：已审查。')
        packet = build_projection(self.root,at=AT)
        self.assertTrue(all('adjudication' not in run for run in packet['ocr']['runs']))


if __name__ == '__main__':
    unittest.main()
