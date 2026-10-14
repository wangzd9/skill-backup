import unittest
from datetime import datetime
from pathlib import Path
import tempfile
from unittest.mock import patch

from progress_query import progress_answer
from progress_display import render_progress, task_records, clock_label, clean

AT = datetime.fromisoformat('2026-10-14T10:00:00+08:00')

class ProgressTests(unittest.TestCase):
    def snapshot(self, root):
        return {'project': str(root), 'role': 'COORDINATOR', 'observed_at': AT.isoformat(),
                'control': 'ACTIVE', 'tasks': [{'task': 'TASK-11S-BATCH-1012', 'state': 'RUNNING'}],
                'events': {'A': {'task': 'TASK-11S-BATCH-1012', 'status': 'RUNNING',
                          'occurred_at': '2026-10-14T09:59:00+08:00',
                          'summary': 'PRIVATE_RAW_LOG /very/long/internal/path ' * 80}},
                'dispatches': {}, 'strong_review': {}}

    def test_query_reads_current_documents_instead_of_saved_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            old=root/'inbox/control/runtime/snapshots/COORDINATOR.json'
            old.parent.mkdir(parents=True); old.write_text('{"old": true}')
            live=self.snapshot(root)
            with patch('progress_query.collect_live', return_value=live) as collect:
                answer=progress_answer(root, {'enabled': True}, at=AT)
            collect.assert_called_once_with(root)
            self.assertIn('TASK-11S-BATCH-1012', answer)
            self.assertIn('09:59', answer)
            self.assertEqual(old.read_text(), '{"old": true}')
            self.assertNotIn('PRIVATE_RAW_LOG', answer)
            self.assertLess(len(answer), 1200)

    def test_collection_failure_does_not_present_old_data_as_current(self):
        with patch('progress_query.collect_live', side_effect=ValueError('inconsistent file')):
            answer=progress_answer('/tmp/example', {'enabled': False}, at=AT)
        self.assertIn('无法核实', answer)
        self.assertNotIn('正在运行', answer)
        self.assertIn('暂停', answer)

    def test_old_event_is_explicitly_historical_and_new_dispatch_invalidates_it(self):
        row=self.snapshot(Path('/tmp/example'))
        row['events']['A']['occurred_at']='2026-10-14T05:00:00+08:00'
        answer=render_progress(row, at=AT)
        self.assertIn('当前待核实', answer)
        row['dispatches']['A']={'mtime': AT.timestamp(), 'source': 'inbox/1号/TASK-NEW/dispatch.md'}
        answer=render_progress(row, at=AT)
        self.assertIn('新派单', answer)
        self.assertNotIn('记录为处理中（05:00', answer)

    def test_no_action_or_released_lease_does_not_imply_idle_or_done(self):
        row=self.snapshot(Path('/tmp/example'))
        row['events']['A']['status']='NO_ACTION'
        row['processes']={'A': {'state': 'none'}}
        answer=render_progress(row, at=AT)
        self.assertIn('巡检无新动作', answer)
        self.assertNotIn('确认空闲', answer)
        self.assertNotIn('全部完成', answer)

    def test_conflicting_mirrors_remain_visible_in_user_summary(self):
        row=self.snapshot(Path('/tmp/example'))
        row['event_conflicts']=[{'event_id':'A:same','sources':['events/A/a.json','inbox/events/A/a.json']}]
        answer=render_progress(row,at=AT)
        self.assertIn('相同事件 ID 的内容不一致，待核对',answer)
        self.assertIn('不据此批准或判定完成',answer)

    def test_unknown_status_and_invalid_timestamp_are_not_invented_success(self):
        row=self.snapshot(Path('/tmp/example'))
        row['events']['A'].update(status='CUSTOM_SUCCESS_WORD', occurred_at='invalid')
        answer=render_progress(row, at=AT)
        self.assertIn('状态待核对', answer)
        self.assertIn('时间待核对', answer)

    def test_file_time_and_fresh_read_do_not_become_occurrence_time(self):
        row=self.snapshot(Path('/tmp/example'))
        row['events']['A'].update(occurred_at='10:2x', source_mtime=AT.isoformat())
        answer=render_progress(row, at=AT)
        self.assertIn('发生时间未知', answer)
        self.assertIn('当前待核实', answer)
        self.assertNotIn('10:2x', answer)
        self.assertNotIn('发生 '+AT.isoformat(), answer)

    def test_observation_and_backfill_are_labeled_separately(self):
        row=self.snapshot(Path('/tmp/example'))
        event=row['events']['A']
        event.update(occurred_at=None, observed_at='2026-10-14T09:59:43+08:00',
                     recorded_at='2026-10-14T10:00:00+08:00')
        answer=render_progress(row, at=AT)
        self.assertIn('发生时间未知；观测 2026-10-14 09:59 +08:00', answer)
        del event['observed_at']
        answer=render_progress(row, at=AT)
        self.assertIn('发生时间未知；补录 2026-10-14 10:00 +08:00', answer)
        self.assertIn('当前待核实', answer)

    def test_time_display_uses_minutes_and_retains_offset_and_calendar_date(self):
        self.assertEqual(clock_label(datetime.fromisoformat('2026-10-13T18:00:17+00:00'), AT),
                         '2026-10-14 02:00 +08:00')
        self.assertEqual(clean('10:2x 补报完成'), '时间未精确记录 补报完成')
        row=self.snapshot(Path('/tmp/example'))
        row['strong_review']={'authorization':'ACTIVE','expires_at':'2026-10-14T05:27:15+00:00'}
        self.assertIn('期限 2026-10-14 13:27 +08:00',render_progress(row,at=AT))

    def test_minute_precision_legacy_event_does_not_invent_seconds(self):
        row=self.snapshot(Path('/tmp/example'))
        row['events']['A']['occurred_at']='2026-10-14T09:59+08:00'
        answer=render_progress(row,at=AT)
        self.assertIn('发生 2026-10-14 09:59 +08:00',answer)
        self.assertNotIn('发生时间未知',answer.split('- **执行者 A：**')[1].split('\n')[0])
        self.assertNotIn('09:59:00',answer)
        self.assertEqual(row['events']['A']['occurred_at'],'2026-10-14T09:59+08:00')

    def test_batch_task_is_not_hidden_by_short_task_id_parser(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'TODO.md').write_text('| TASK-01 | ✅ | desc | req | none | DONE | A |\n'
                                      '| TASK-11S-BATCH-1012 | RUNNING | input | criteria | output | owner |\n')
            row=self.snapshot(root)
            row['tasks']=[]
            with patch('progress_query.collect_live', return_value=task_records(root,row)):
                answer=progress_answer(root, {'enabled': True}, at=AT)
            self.assertIn('TASK-11S-BATCH-1012', answer)
            self.assertIn('尚未收口', answer)

if __name__=='__main__': unittest.main()
