import json
import os
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import activity_report as report


def at(text):
    return datetime.fromisoformat('2026-10-13T' + text + '+08:00')


class ActivityTests(unittest.TestCase):
    def test_acked_running_is_working_not_unknown_or_waiting(self):
        self.assertEqual('working', report.event_state('ACKED_RUNNING'))
        with tempfile.TemporaryDirectory() as temp:
            snapshot={'project':temp,'role':'TESTER','observed_at':at('08:05:00').isoformat(),
                      'events':{'TESTER':{'status':'ACKED_RUNNING','task':'TASK-01',
                          'attempt':'attempt-1','occurred_at':at('08:00:00').isoformat()}},
                      'processes':{'TESTER':{'state':'alive'}}}
            self.assertEqual('working',report.sample(snapshot)['category'])

    def test_closed_dispatch_edit_does_not_turn_working_phase_unknown(self):
        for status in ('ACCEPTED','DONE','CANCELLED','REPLACED'):
            with self.subTest(status=status),tempfile.TemporaryDirectory() as temp:
                snapshot={'project':temp,'role':'TESTER','observed_at':at('08:05:00').isoformat(),
                          'events':{'TESTER':{'status':'TESTING','task':'TASK-02',
                              'attempt':'attempt-1','occurred_at':at('08:00:00').isoformat()}},
                          'dispatches':{'TESTER':{'status':status,'source':'inbox/TESTER/old.md',
                              'mtime':at('08:04:00').timestamp()}},'processes':{}}
                self.assertEqual('working',report.sample(snapshot)['category'])

    def test_new_unacknowledged_dispatch_still_requires_matching_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            snapshot={'project':temp,'role':'TESTER','observed_at':at('08:05:00').isoformat(),
                      'events':{'TESTER':{'status':'TESTING','task':'TASK-02',
                          'occurred_at':at('08:00:00').isoformat()}},
                      'dispatches':{'TESTER':{'status':'PENDING','source':'inbox/TESTER/new.md',
                          'mtime':at('08:04:00').timestamp()}},'processes':{}}
            self.assertEqual('unknown',report.sample(snapshot)['category'])

    def legacy_fixture(self, root, *, role='B', status='IDLE', task='TASK-01', summary='等待独立审核'):
        source=root/f'events/{role}/legacy.json';source.parent.mkdir(parents=True)
        event={'role':role,'event_id':role+':legacy','status':status,'task':task,'attempt':'1',
               'summary':summary,'occurred_at':at('07:55:00').isoformat(),'recorded_at':at('07:56:00').isoformat()}
        source.write_text(json.dumps(event));os.utime(source,(at('07:56:00').timestamp(),)*2)
        sample={'at':at('08:00:00').isoformat(),'role':role,'category':'unknown','task':task,
                'attempt':'1','summary':summary,'source':str(source.relative_to(root)),'basis':'script_observation'}
        activity=root/f'inbox/control/runtime/activity/2026-10-13/{role}.jsonl'
        activity.parent.mkdir(parents=True);activity.write_text(json.dumps(sample)+'\n')
        return source,event,activity,sample

    def test_legacy_acked_running_recovers_only_from_matching_original_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);self.legacy_fixture(root,status='ACKED_RUNNING')
            rows,_,_=report.points(root,'B',at('08:00:00'),at('08:05:00'))
            self.assertEqual('working',rows[0]['category'])
            self.assertIn('legacy_status_mapping_recovered',rows[0]['classification_reason'])
            self.assertIn('legacy_status_evidence',rows[0])

    def test_legacy_source_identity_recovers_waiting_never_confirmed_idle(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);self.legacy_fixture(root)
            rows,_,_=report.points(root,'B',at('08:00:00'),at('08:05:00'))
            self.assertEqual(rows[0]['category'],'waiting')
            self.assertEqual(rows[0]['raw_status'],'IDLE')
            self.assertIn('reported_idle',rows[0]['classification_reason'])
            self.assertTrue(rows[0]['legacy_status_evidence']['sha256'])

    def test_legacy_recovery_rejects_wrong_summary_role_task_future_source_and_snapshot(self):
        for change in ('summary','role','task','future','mtime','snapshot'):
            with self.subTest(change=change),tempfile.TemporaryDirectory() as temp:
                root=Path(temp);source,event,activity,sample=self.legacy_fixture(root,status='OBSERVING')
                if change in {'summary','role','task'}:event[change]='different'
                if change=='future':event['recorded_at']=at('09:00:00').isoformat()
                source.write_text(json.dumps(event));os.utime(source,(at('07:56:00').timestamp(),)*2)
                if change=='mtime':os.utime(source,(at('09:00:00').timestamp(),)*2)
                if change=='snapshot':
                    sample['source']='inbox/control/runtime/snapshots/B.json'
                    snapshot=root/sample['source'];snapshot.parent.mkdir(parents=True);snapshot.write_text(json.dumps(event))
                    activity.write_text(json.dumps(sample)+'\n')
                rows,_,_=report.points(root,'B',at('08:00:00'),at('08:05:00'))
                self.assertEqual(rows[0]['category'],'unknown')
                self.assertIn('legacy_status_recovery_refused',rows[0]['classification_reason'])

    def test_gap_records_exact_reason_and_recovery_source(self):
        rows=[{'_at':at('08:00:00'),'category':'working','task':'TASK-01','source':'events/A/start.json'}]
        segments,_=report.intervals(rows,at('08:00:00'),at('08:20:00'))
        gap=segments[-1]
        self.assertEqual(gap['reason'],'sample_expired_after_600_seconds')
        self.assertEqual(gap['start'],at('08:10:00').isoformat())
        self.assertIn('events/A/start.json',gap['recovery_sources'])

    def test_summary_integrates_parallel_model_and_cron_evidence_without_log_scan(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);self.legacy_fixture(root)
            (root/'ROLE-BINDINGS.json').write_text(json.dumps({'schema_version':1,'project_root':str(root),
                'roles':{'B':{'poll_job_id':'job'}}}))
            timing=report.time_accounting_module()
            index=root/'time.db'
            timing.record_api_end(index,session_id='s',api_request_id='r',role='B',project=root,
                started_at=at('08:00:30'),ended_at=at('08:01:30'))
            with patch.object(timing,'read_cron_records',return_value=[{'role':'B','claimed_at':at('08:00:00').isoformat(),
                'started_at':at('08:00:10').isoformat(),'finished_at':at('08:02:00').isoformat(),'status':'completed','source':'cron.db#e'}]), \
                 patch.object(timing,'import_agent_logs',side_effect=AssertionError('reports must not scan logs')):
                item=report.summarize(root,'B',at('08:00:00'),at('08:05:00'),timing_index=index,executions_db=root/'cron.db')
            dims=item['time_accounting']['dimensions']
            self.assertEqual(300,item['totals_seconds']['waiting'])
            self.assertEqual(60,dims['client_model_wait']['observed_seconds'])
            self.assertEqual(10,dims['role_task_dispatch']['dispatch_wait_seconds'])
            self.assertFalse(item['time_accounting']['dimensions_are_additive'])
            data={'generated_at':at('11:00:00').isoformat(),'kind':'phase','role':'B',
                  'start':item['start'],'end':item['end'],'roles':[item],'current':{},'artifact':'digest.json'}
            text=report.render_digest(data)
            self.assertIn('模型响应',text)
            self.assertIn('不可相加',text)
            self.assertIn('派发等待',text)
            self.assertIn('未观测到 API 响应占用',text)
            self.assertNotIn('未记录起止',text)

    def test_compact_render_merges_summary_changes_and_keeps_evidence_segments(self):
        rows=[{'_at':at('08:00:00'),'category':'working','task':'TASK-01','summary':'fingerprint secret-1'},
              {'_at':at('08:05:00'),'category':'working','task':'TASK-01','summary':'fingerprint secret-2'}]
        segments,totals=report.intervals(rows,at('08:00:00'),at('08:10:00'))
        item={'role':'A','segments':segments,'totals_seconds':totals,'delivery_records':[],
              'unverified_sources':[],'task_functions':{'TASK-01':'接收配置验证'}}
        data={'generated_at':at('11:00:00').isoformat(),'kind':'phase','role':'A',
              'start':at('08:00:00').isoformat(),'end':at('08:10:00').isoformat(),
              'roles':[item],'current':{},'artifact':'digest.json'}
        text=report.render_digest(data)
        self.assertEqual(2,len(item['segments']))
        self.assertEqual(1,len(report.display_segments(item)))
        self.assertIn('接收配置验证',text)
        self.assertNotIn('fingerprint',text)
        self.assertIn('**当前工作**',text)
        self.assertEqual(text.count('**当前工作**'),1)
        self.assertLessEqual(max(map(len,text.splitlines())),180)

    def test_subminute_display_never_prints_identical_minute_range(self):
        segment={'start':at('10:25:10').isoformat(),'end':at('10:25:30').isoformat()}
        label=report.interval_label(segment)
        self.assertIn('该分钟内',label)
        self.assertIn('不足1分钟',label)
        self.assertNotIn('10:25–10:25',label)

    def test_visible_function_label_never_repeats_fuzzy_clock(self):
        label=report.task_function({'task_functions':{'TASK-01':'12:3x 恢复场景获取'}},'TASK-01')
        self.assertIn('时间未精确记录',label)
        self.assertNotIn('12:3x',label)

    def display_fixture(self, snapshot=None):
        item={'role':'A','segments':[],'totals_seconds':{},'delivery_records':[],
              'unverified_sources':[],'task_functions':{}}
        data={'generated_at':at('11:00:00').isoformat(),'kind':'phase','role':'A',
              'start':at('08:00:00').isoformat(),'end':at('11:00:00').isoformat(),
              'roles':[item],'current':{} if snapshot is None else {'A':snapshot},'artifact':'digest.json'}
        return item,data

    def test_digest_names_role_deliveries_and_current_functions_without_acceptance_inference(self):
        self.assertEqual('', report.task_identity('TASK-11S-BATCH-1012_OLD'))
        snapshot={'observed_at':at('12:40:00').isoformat(),'confirmed_idle':False,
                  'tasks':[{'task':'`TASK-16D` 独立联合验收','state':'**ACKED / TESTER（12:3x，raw-state-secret）'}]}
        item,data=self.display_fixture(snapshot)
        item['task_functions']={'TASK-11S':'水雾生产首包','TASK-11S-BATCH-1012':'1012整批生产水雾测试',
                                'TASK-16D':'独立联合验收'}
        item['delivery_records']=[{'task':'TASK-11S-BATCH-1012','status':'DONE','summary':'raw-delivery-secret'}]
        text=report.render_digest(data)
        self.assertIn('本期角色交付/接受记录',text)
        self.assertIn('1012整批生产水雾测试',text)
        self.assertNotIn('水雾生产首包',text)
        self.assertIn('任务最终完成仍以验收裁决为准',text)
        self.assertIn('独立联合验收，**已领取**',text)
        self.assertIn('2026-10-13 12:40',text)
        for hidden in ('12:3x','raw-state-secret','raw-delivery-secret','业务已完成'):
            self.assertNotIn(hidden,text)

    def test_current_task_title_is_usable_but_narrative_done_is_not_structural_state(self):
        item,data=self.display_fixture({'tasks':[{'task':'TASK-99','title':'场景获取','state':'已交付，正文提到DONE'}]})
        text=report.render_digest(data)
        self.assertIn('场景获取，**业务状态未明确记录**',text)
        self.assertNotIn('已交付，正文提到DONE',text)

    def test_empty_current_tasks_only_confirm_idle_with_explicit_confirmation(self):
        for snapshot in (None,{'tasks':[],'confirmed_idle':False},{'tasks':[],'confirmed_idle':True}):
            with self.subTest(snapshot=snapshot):
                _,data=self.display_fixture(snapshot)
                text=report.render_digest(data)
                if snapshot and snapshot.get('confirmed_idle'):
                    self.assertIn('已确认无派单',text)
                else:
                    self.assertIn('当前派单/状态待核',text)
                    self.assertNotIn('已确认无派单',text)

    def test_current_task_display_is_bounded_and_preserves_complete_snapshot(self):
        tasks=[{'task':f'TASK-{n:02d}','title':f'功能{n}','state':'RUNNING'} for n in range(1,5)]
        tasks.insert(0,{'task':'TASK-00','title':'旧完成项','state':'DONE'})
        item,data=self.display_fixture({'tasks':tasks,'confirmed_idle':False})
        item['delivery_records']=[{'task':f'TASK-{n:02d}','status':'DONE'} for n in range(1,5)]
        item['task_functions']={f'TASK-{n:02d}':f'功能{n}' for n in range(1,5)}
        original=json.dumps(data,sort_keys=True)
        text=report.render_digest(data)
        current=[line for line in text.splitlines() if line.startswith('- **当前任务：**')]
        delivered=[line for line in text.splitlines() if line.startswith('- **本期角色记录：**')]
        self.assertEqual(0,len(current))
        self.assertEqual(4,len([line for line in text.splitlines() if line.startswith('- **关联事项：**')]))
        self.assertEqual(1,len([line for line in text.splitlines() if line.startswith('- **最近完成：**')]))
        self.assertEqual(3,len(delivered))
        self.assertTrue(all('旧完成项' not in line for line in current))
        self.assertIn('完整集合见证据',text)
        self.assertEqual(original,json.dumps(data,sort_keys=True))

    def test_digest_uses_role_phase_names_and_plain_generic_task_title(self):
        self.assertEqual('整体任务跟进',report.task_function({'task_functions':{'TASK-16':'子聚合'}},'TASK-16'))
        for role,label in (('COORDINATOR','协调处理中'),('A','开发/自测阶段'),
                           ('B','开发/自测阶段'),('C','开发/自测阶段'),('TESTER','独立测试阶段')):
            with self.subTest(role=role):
                snapshot={'tasks':[{'task':'TASK-16','state':'REOPENED'}]}
                item,data=self.display_fixture()
                item.update(role=role,task_functions={'TASK-16':'聚合'},totals_seconds={'working':60},
                            segments=[{'start':at('08:00:00').isoformat(),'end':at('08:01:00').isoformat(),
                                       'category':'working','task':'TASK-16','reason':''}])
                data.update(role=role,current={role:snapshot})
                text=report.render_digest(data)
                self.assertIn(f'**{label}：** 1分钟',text)
                self.assertIn(f'{label} · 整体任务跟进',text)
                if role != 'COORDINATOR':
                    self.assertIn('整体任务跟进，**已恢复推进**',text)
                self.assertNotIn('聚合',text)
                self.assertNotIn('开发/测试/协调阶段',text)
                self.assertEqual('聚合',item['task_functions']['TASK-16'])
                self.assertEqual(60,item['totals_seconds']['working'])

    def test_shared_facts_override_empty_snapshots_and_preserve_accepted_unmerged_result(self):
        fact=lambda value:{'value':value,'source':'TODO.md'}
        item,data=self.display_fixture({'tasks':[],'confirmed_idle':False})
        c_item={**item,'role':'C'}
        data.update(role='COORDINATOR',roles=[item,c_item],current={
            'A':{'tasks':[],'confirmed_idle':False},'C':{'tasks':[],'confirmed_idle':False}},
            progress_facts={'observed_at':at('12:40:00').isoformat(),'evidence_reference':'same-read-evidence.json',
                'roles':{'A':{'tasks':fact(['TASK-16C-A']),'recent_completed':fact([])},
                         'C':{'tasks':fact([]),'recent_completed':fact(['TASK-16C-C'])}},
                'tasks':{'TASK-16C-A':{'title':fact('查询/API'),'state':fact('DELIVERED_FROZEN'),
                                      'testing':fact('RECORDED_PASSED'),'merge':fact('NOT_CONFIRMED')},
                         'TASK-16C-C':{'title':fact('流水线详情'),'state':fact('DONE'),
                                      'delivery':fact('ACCEPTED'),'testing':fact('RECORDED_PASSED'),
                                      'merge':fact('NOT_MERGED')}}})
        original=json.dumps(data,sort_keys=True)
        text=report.render_digest(data)
        self.assertIn('共同进度事实',text)
        self.assertIn('查询/API，**已交付，独立验证有通过记录，等待核对并接受**',text)
        self.assertIn('流水线详情，**开发交付已接受，尚未合入主分支**',text)
        self.assertIn('same-read-evidence.json',text)
        self.assertNotIn('当前派单/状态待核',text)
        self.assertNotIn('快照回退',text)
        self.assertNotIn('等待审查和独立验证',text)
        self.assertEqual(original,json.dumps(data,sort_keys=True))

    def test_shared_facts_empty_role_still_requires_idle_confirmation_and_checks_new_dispatch(self):
        fact=lambda value:{'value':value}
        for confirmed,new_dispatch in ((False,False),(True,False),(True,True)):
            with self.subTest(confirmed=confirmed,new_dispatch=new_dispatch):
                _,data=self.display_fixture({'tasks':[],'confirmed_idle':confirmed})
                data['progress_facts']={'roles':{'A':{'tasks':fact([]),'recent_completed':fact([]),
                    'dispatch':fact({'newer_than_activity':new_dispatch})}},'tasks':{}}
                text=report.render_digest(data)
                if confirmed and not new_dispatch:
                    self.assertIn('已确认无派单',text)
                else:
                    self.assertNotIn('已确认无派单',text)
                    self.assertIn('待核',text)
        _,data=self.display_fixture({'tasks':[],'confirmed_idle':False})
        self.assertIn('未取得共同进度事实，以下为保存快照回退',report.render_digest(data))

    def test_build_collects_one_shared_fact_packet_for_all_roles(self):
        import role_observer,runtime_tools
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            snapshots={role:{'role':role,'project':str(root),'tasks':[],'confirmed_idle':False,
                             'observed_at':at('08:00:00').isoformat()} for role in report.runtime.ROLES}
            packet={'roles':{},'tasks':{},'evidence_reference':'current-evidence.json'}
            with patch.object(role_observer,'refresh',side_effect=lambda project,role:snapshots[role]), \
                 patch.object(runtime_tools,'refresh_progress_facts',return_value=packet) as refresh:
                data=report.build(root,'COORDINATOR','morning',at('08:00:00'))
            refresh.assert_called_once_with(root,at=at('08:00:00'),snapshot=snapshots['COORDINATOR'])
            self.assertIs(packet,data['progress_facts'])
            self.assertTrue(all('progress_facts' not in role for role in data['roles']))
            stored=json.loads((root/data['artifact']).read_text())
            self.assertEqual(packet,stored['progress_facts'])

    def test_quiet_boundaries(self):
        for value in ('00:00:00', '07:59:59', '21:00:00', '23:59:59'):
            self.assertTrue(report.quiet(at(value)))
        for value in ('08:00:00', '11:00:00', '20:59:59'):
            self.assertFalse(report.quiet(at(value)))

    def test_windows_cover_daytime_and_cross_date_night(self):
        for slot, start in [(11, 8), (15, 11), (18, 15)]:
            a, b = report.window('phase', at(f'{slot}:02:00'))
            self.assertEqual((a.hour, b.hour), (start, slot))
        a, b = report.window('morning', at('08:00:00'))
        self.assertEqual((a.day, a.hour, b.day, b.hour), (12, 21, 13, 8))
        self.assertEqual((b - a).total_seconds(), 11 * 3600)

    def test_long_observation_gap_is_unknown_not_idle(self):
        rows = [{'_at': at('08:00:00'), 'category': 'idle', 'task': '', 'source': 'sample'}]
        segments, totals = report.intervals(rows, at('08:00:00'), at('11:00:00'))
        self.assertEqual(totals['idle'], 600)
        self.assertEqual(totals['unknown'], 3 * 3600 - 600)
        self.assertEqual(sum(totals.values()), 3 * 3600)

    def test_waiting_and_blocked_are_not_idle(self):
        rows = [{'_at': at('08:00:00'), 'category': 'working', 'task': 'TASK-01'},
                {'_at': at('08:05:00'), 'category': 'waiting', 'task': 'TASK-01'},
                {'_at': at('08:10:00'), 'category': 'blocked', 'task': 'TASK-01'}]
        _, totals = report.intervals(rows, at('08:00:00'), at('08:15:00'))
        self.assertEqual([totals[k] for k in ('working', 'waiting', 'blocked', 'idle')], [300, 300, 300, 0])

    def test_same_task_activity_change_retains_separate_time_segments(self):
        rows = [{'_at': at('08:00:00'), 'category': 'working', 'task': 'TASK-01', 'summary': '实现接收配置'},
                {'_at': at('08:05:00'), 'category': 'working', 'task': 'TASK-01', 'summary': '运行自测'}]
        segments, totals = report.intervals(rows, at('08:00:00'), at('08:10:00'))
        self.assertEqual(len(segments), 2)
        self.assertEqual(totals['working'], 600)
        text = report.timeline({'segments': segments})
        self.assertIn('实现接收配置', text)
        self.assertIn('运行自测', text)

    def test_user_timeline_uses_reliable_minutes_without_changing_internal_seconds(self):
        rows=[{'_at':at('09:01:42'),'category':'working','task':'TASK-01','summary':'运行自测'}]
        segments,_=report.intervals(rows,at('09:01:42'),at('09:05:57'))
        text=report.timeline({'segments':segments})
        self.assertIn('2026-10-13 09:01 +08:00',text)
        self.assertIn('2026-10-13 09:05 +08:00',text)
        self.assertEqual(segments[0]['start'],at('09:01:42').isoformat())
        self.assertEqual(segments[0]['end'],at('09:05:57').isoformat())
        self.assertEqual(report.parse_at('2026-10-13T09:01+08:00').minute,1)

    def test_end_boundary_event_belongs_to_next_window(self):
        rows = [{'_at': at('11:00:00'), 'category': 'working', 'task': 'TASK-02'}]
        _, totals = report.intervals(rows, at('08:00:00'), at('11:00:00'))
        self.assertEqual(totals['unknown'], 3 * 3600)

    def test_sampling_does_not_consider_assigned_task_a_classification_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            snapshot = {'project': temp, 'role': 'A', 'observed_at': at('09:00:00').isoformat(),
                        'classification_reasons': ['assigned nonterminal task: TASK-01'],
                        'events': {'A': {'task': 'TASK-01', 'status': 'RUNNING'}}}
            row = report.sample(snapshot)
            self.assertEqual(row['category'], 'working')
            snapshot['events']['A']['status'] = 'REVIEW_PENDING'
            self.assertEqual(report.sample(snapshot)['category'], 'waiting')

    def test_new_dispatch_cannot_reuse_old_idle_or_running_event(self):
        with tempfile.TemporaryDirectory() as temp:
            snapshot = {'project': temp, 'role': 'A', 'observed_at': at('09:00:00').isoformat(),
                        'events': {'A': {'task': 'TASK-01', 'status': 'RUNNING', 'occurred_at': at('08:00:00').isoformat()}},
                        'dispatches': {'A': {'mtime': at('08:30:00').timestamp()}}}
            self.assertEqual(report.sample(snapshot)['category'], 'unknown')

    def test_unknown_event_time_is_not_replaced_by_file_mtime(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / 'events/A'
            folder.mkdir(parents=True)
            (folder / 'done.json').write_text(json.dumps({'event_id': 'x', 'status': 'DONE', 'task': 'TASK-01'}))
            rows, events, errors = report.points(root, 'A', at('08:00:00'), at('11:00:00'))
            self.assertEqual((rows, events), ([], []))
            self.assertTrue(errors)

    def test_unknown_occurrence_uses_explicit_observation_without_faking_occurrence(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            folder=root/'events/A'; folder.mkdir(parents=True)
            (folder/'state.json').write_text(json.dumps({'event_id':'x','status':'RUNNING','task':'TASK-01',
                'occurred_at':None,'observed_at':at('09:01:42').isoformat(),
                'recorded_at':at('09:03:01').isoformat(),'summary':'10:2x 继续推理'}))
            rows,events,errors=report.points(root,'A',at('08:00:00'),at('11:00:00'))
            self.assertFalse(errors)
            self.assertEqual(rows[0]['_at'],at('09:01:42'))
            self.assertEqual(rows[0]['basis'],'event_observation')
            self.assertIsNone(rows[0]['occurred_at'])
            self.assertIn('时间未精确记录',rows[0]['summary'])

    def test_recent_file_mtime_does_not_supersede_dispatch_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            snapshot={'project':temp,'role':'A','observed_at':at('10:00:00').isoformat(),
                'events':{'A':{'status':'RUNNING','occurred_at':None,
                    'source_mtime':at('09:59:00').isoformat()}},
                'dispatches':{'A':{'mtime':at('09:00:00').timestamp()}}}
            self.assertEqual(report.sample(snapshot)['category'],'unknown')

    def test_morning_reports_all_roles_and_preserves_evidence_gaps(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            import role_observer
            def snapshot(project, role):
                return {'role': role, 'project': str(project), 'observed_at': at('08:00:00').isoformat(),
                        'confirmed_idle': True, 'tasks': [], 'events': {},
                        'strong_review': {'authorization': '未授权', 'used_last_60_minutes': 0}}
            with patch.object(role_observer, 'refresh', side_effect=snapshot):
                data = report.build(root, 'COORDINATOR', 'morning', at('08:00:00'))
            self.assertEqual({r['role'] for r in data['roles']}, set(report.runtime.ROLES))
            self.assertEqual(len(data['evening_supplement']), 5)
            for item in data['roles']:
                self.assertEqual(item['totals_seconds']['idle'], 0)
                self.assertEqual(item['totals_seconds']['unknown'], 11 * 3600)
            text = report.render_digest(data)
            self.assertIn('近 60 分钟 0/5', text)
            self.assertTrue((root / data['artifact']).is_file())

    def test_summary_uses_real_completed_events_and_deduplicates_ids(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for directory in ('events/A', 'inbox/events/A'):
                folder = root / directory
                folder.mkdir(parents=True)
                (folder / 'accepted.json').write_text(json.dumps({'event_id': 'one', 'status': 'ACCEPTED',
                    'task': 'TASK-01', 'occurred_at': at('09:00:00').isoformat()}))
            result = report.summarize(root, 'A', at('08:00:00'), at('11:00:00'))
            self.assertEqual(len(result['events']), 1)
            self.assertEqual(len(result['delivery_records']), 1)
            self.assertEqual(result['totals_seconds']['idle'], 0)

    def test_refresh_failure_is_visible_not_confirmed_idle(self):
        with tempfile.TemporaryDirectory() as temp:
            import role_observer
            with patch.object(role_observer, 'refresh', side_effect=OSError('fixture unavailable')):
                data = report.build(Path(temp), 'A', 'phase', at('11:00:00'))
            self.assertIn('fixture unavailable', report.render_digest(data))
            self.assertEqual(data['roles'][0]['totals_seconds']['idle'], 0)


if __name__ == '__main__':
    unittest.main()


class PrimaryWorkDisplayTests(unittest.TestCase):
    def test_background_responsibility_is_not_a_second_current_development_task(self):
        def fact(value): return {'value':value}
        data={'observed_at':'2026-10-14 16:00 +08:00','roles':{'A':{
            'tasks':fact(['TASK-16C-A','TASK-11S-BATCH-1012']),
            'primary_task':fact('TASK-16C-A'),'related_tasks':fact(['TASK-11S-BATCH-1012']),
            'recent_completed':fact([])}},'tasks':{
            'TASK-16C-A':{'title':fact('查询/API'),'state':fact('DELIVERED_FROZEN'),'testing':fact('RECORDED_PASSED')},
            'TASK-11S-BATCH-1012':{'title':fact('生产批次观察'),'state':fact('RUNNING')}}}
        item={'role':'A','segments':[],'totals_seconds':{},'delivery_records':[],
              'unverified_sources':[],'task_functions':{}}
        text=report.render_digest({'role':'A','kind':'phase','generated_at':'2026-10-14T16:00:00+08:00',
            'start':'2026-10-14T15:00:00+08:00','end':'2026-10-14T16:00:00+08:00',
            'roles':[item],'current':{},'progress_facts':data,'artifact':'digest.json'})
        self.assertEqual(text.count('**当前任务：**'),1)
        self.assertIn('**关联事项：** 生产批次观察',text)
        self.assertIn('查询/API',text)
        self.assertNotIn('执行者 B',text)


class UnifiedDigestContractTests(unittest.TestCase):
    def fixture(self, role='COORDINATOR'):
        fact=lambda value:{'value':value,'source':'TODO.md'}
        identities={'COORDINATOR':'TASK-16','A':'TASK-16C-A','B':'TASK-16C-B',
                    'C':'TASK-16C-C','TESTER':'TASK-16D'}
        names={'COORDINATOR':'整体任务','A':'查询服务','B':'Bag视图','C':'详情页面','TESTER':'独立验收'}
        packet={'goal':fact('完成界面最小闭环'),'observed_at':'2026-10-13 11:00 +08:00',
                'roles':{},'tasks':{},'ocr':{'runs':[],'declared':[]},'strong_review':{}}
        items=[]
        for index,who in enumerate(identities):
            task=identities[who]
            packet['roles'][who]={'tasks':fact([task]),'primary_task':fact(task),'related_tasks':fact([]),
                                  'recent_completed':fact([])}
            packet['tasks'][task]={'title':fact(names[who]),'state':fact('RUNNING'),'testing':fact('NOT_CONFIRMED')}
            items.append({'role':who,'segments':[{'start':at('08:00:00').isoformat(),
                'end':at('08:01:00').isoformat(),'category':'working','task':task,'reason':''}],
                'totals_seconds':{'working':60,'waiting':120,'blocked':180,'idle':240,'unknown':300},
                'delivery_records':[],'unverified_sources':[],'task_functions':{task:names[who]},
                'time_accounting':{'dimensions':{
                    'client_model_wait':{'observed_seconds':360,'requests':[{}],
                        'not_observed_api_occupancy_seconds':420,'open_requests':[{}]},
                    'role_task_dispatch':{'records':[{}],'schedule_delay_seconds':480,
                        'dispatch_wait_seconds':540,'execution_seconds':600}}}})
        return {'generated_at':at('11:00:00').isoformat(),'kind':'phase','role':role,
                'start':at('08:00:00').isoformat(),'end':at('11:00:00').isoformat(),
                'roles':items,'progress_facts':packet,'current':{},'artifact':'digest.json'}

    def test_coordinator_digest_keeps_seven_sections_and_each_executor_in_one_group(self):
        from report_contract import COORDINATOR_SECTIONS
        data=self.fixture()
        original=json.dumps(data,sort_keys=True)
        text=report.render_digest(data)
        headings=[title for _,title in COORDINATOR_SECTIONS]
        self.assertEqual([text.index('**'+title+'**') for title in headings],
                         sorted(text.index('**'+title+'**') for title in headings))
        for who in ('A','B','C'):
            self.assertEqual(text.count('**执行者 '+who+'**'),1)
        self.assertNotIn('**证据与接续**',text)
        self.assertEqual(original,json.dumps(data,sort_keys=True))

    def test_all_timing_dimensions_and_unknown_evidence_remain_visible_without_combining_them(self):
        data=self.fixture()
        text=report.render_digest(data)
        for label,count in (('确认空闲',4),('等待任务/外部处理/审核',2),('阻塞',3),
                            ('待补证据',5),('模型响应',6),('调度滞后',8),('派发等待',9),('执行区间',10)):
            self.assertEqual(text.count('**'+label+'：** '+str(count)+'分钟'),5)
        self.assertEqual(text.count('**未结束请求：** 1条'),5)
        self.assertEqual(text.count('**未观测到 API 响应占用：** 7分钟'),5)
        self.assertIn('不可相加',text)
        self.assertEqual(data['roles'][0]['totals_seconds']['unknown'],300)

    def test_worker_digest_excludes_other_roles_even_when_artifact_contains_every_role(self):
        for role in ('A','B','C','TESTER'):
            with self.subTest(role=role):
                data=self.fixture(role)
                text=report.render_digest(data)
                for title in ('当前工作','本次完成','阻塞与依赖','下一步计划'):
                    self.assertIn('**'+title+'**',text)
                self.assertNotIn('**执行者进度**',text)
                self.assertNotIn('**强审查情况**',text)
                self.assertEqual(text.count('**模型响应：** 6分钟'),1)
                others={'A':'查询服务','B':'Bag视图','C':'详情页面','TESTER':'独立验收'}
                for who,feature in others.items():
                    if who!=role:
                        self.assertNotIn(feature,text)

    def test_morning_evening_supplement_stays_with_its_role_without_duplicate_groups(self):
        data=self.fixture()
        data['kind']='morning'
        data['evening_supplement']=data['roles']
        text=report.render_digest(data)
        self.assertEqual(text.count('**晚间补充（前日18:00–21:00）：**'),5)
        for who in ('A','B','C'):
            self.assertEqual(text.count('**执行者 '+who+'**'),1)
