import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import time_accounting as accounting


class TimeAccountingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.index = self.base / 'timing.db'

    def test_hook_allowlist_open_requests_and_retry(self):
        accounting.record_api_start(self.index, session_id='cron_job_20261014_080000',
            api_request_id='r1', role='B', started_at=100, model='qwen',
            request={'body': 'secret prompt'}, authorization='secret-token')
        accounting.record_api_end(self.index, session_id='cron_job_20261014_080000',
            api_request_id='r1', ended_at=120, status='error', retry_pending=True)
        report = accounting.summarize(self.index, 'B', 90, 150)
        model = report['dimensions']['client_model_wait']
        self.assertEqual(0, model['observed_seconds'])
        self.assertIn('request_still_open', [g['reason'] for g in model['gaps']])
        self.assertEqual(1, len(model['open_requests']))
        accounting.record_api_end(self.index, session_id='cron_job_20261014_080000',
            api_request_id='r1', ended_at=140, first_chunk_at=125)
        report = accounting.summarize(self.index, 'B', 90, 150)
        model = report['dimensions']['client_model_wait']
        self.assertEqual(40, model['observed_seconds'])
        self.assertEqual(25, model['requests'][0]['ttfb_seconds'])
        self.assertNotIn('secret', self.index.read_bytes().decode('latin1'))

    def test_post_hook_can_supply_start_and_duplicate_is_idempotent(self):
        kwargs = dict(session_id='s', api_request_id='r', role='A',
                      started_at=100, ended_at=140)
        accounting.record_api_end(self.index, **kwargs)
        accounting.record_api_start(self.index, session_id='s', api_request_id='r', started_at=100)
        accounting.record_api_start(self.index, session_id='s', api_request_id='r', started_at=110)
        accounting.record_api_error(self.index, session_id='s', api_request_id='r',
                                    ended_at=135, retryable=True)
        accounting.record_api_end(self.index, **kwargs)
        report = accounting.summarize(self.index, 'A', 110, 130)
        self.assertEqual(20, report['dimensions']['client_model_wait']['observed_seconds'])
        self.assertEqual(1, len(report['dimensions']['client_model_wait']['requests']))
        self.assertEqual(100, report['dimensions']['client_model_wait']['requests'][0]['started_at'])

    def test_project_filter_and_retryable_error(self):
        accounting.record_api_start(self.index, session_id='s', api_request_id='r',
                                    role='A', project='/p1', started_at=100)
        accounting.record_api_error(self.index, session_id='s', api_request_id='r',
                                    ended_at=110, retryable=True)
        accounting.record_api_end(self.index, session_id='s', api_request_id='r', ended_at=120)
        one = accounting.summarize(self.index, 'A', 100, 150, project='/p1')
        two = accounting.summarize(self.index, 'A', 100, 150, project='/p2')
        self.assertEqual(20, one['dimensions']['client_model_wait']['observed_seconds'])
        self.assertEqual(0, two['dimensions']['client_model_wait']['observed_seconds'])
        self.assertEqual(30, sum(g['seconds'] for g in one['dimensions']['client_model_wait']['gaps']))

    def test_dimensions_overlap_and_never_become_development_by_subtraction(self):
        for rid, start, end in (('r1', 100, 140), ('r2', 130, 170)):
            accounting.record_api_end(self.index, session_id='s', api_request_id=rid,
                role='C', started_at=start, ended_at=end)
        business = [{'start': 90, 'end': 180, 'category': 'working', 'task': 'TASK-1',
                     'summary': 'developing', 'sources': ['event.json']}]
        cron = [{'role': 'C', 'execution_id': 'e', 'claimed_at': 95, 'started_at': 110,
                 'finished_at': 175, 'status': 'completed', 'source': 'test.db#e'}]
        report = accounting.summarize(self.index, 'C', 90, 180,
                                      business_segments=business, cron_records=cron)
        dims = report['dimensions']
        self.assertEqual(90, dims['business_phase']['observed_seconds'])
        self.assertEqual(70, dims['client_model_wait']['observed_seconds'])
        self.assertEqual(15, dims['role_task_dispatch']['dispatch_wait_seconds'])
        self.assertEqual(80, dims['client_model_wait']['request_sum_seconds'])
        self.assertFalse(report['dimensions_are_additive'])
        self.assertNotIn('development_seconds', report)
        self.assertNotIn('idle_seconds', dims['role_task_dispatch'])

    def test_unknown_business_has_reason_and_recovery_sources(self):
        segments = [{'start': 100, 'end': 150, 'category': 'unknown',
                     'summary': 'OBSERVING source not recognized', 'sources': ['events/C/x.json']}]
        report = accounting.summarize(self.index, 'C', 100, 150, business_segments=segments)
        gap = report['dimensions']['business_phase']['gaps'][0]
        self.assertEqual(50, gap['seconds'])
        self.assertEqual('unclassified_business_evidence', gap['reason'])
        self.assertIn('events/C/x.json', gap['recovery_sources'])

    def test_incremental_legacy_import_partial_lines_rotation_and_dedup(self):
        log = self.base / 'agent.log'
        row = ('2026-10-14 08:01:00,000 INFO [cron_job_20261014_080000] '
               'agent.conversation_loop: API call #1: model=qwen provider=custom '
               'in=4 out=2 total=6 latency=30.0s id=chatcmpl-abc\n')
        log.write_text(row + row[:40])
        result = accounting.import_agent_logs(self.index, [log], {'job': 'A'})
        self.assertEqual(1, result['inserted'])
        self.assertEqual(0, accounting.import_agent_logs(self.index, [log], {'job': 'A'})['inserted'])
        with log.open('a') as out: out.write(row[40:])
        self.assertEqual(0, accounting.import_agent_logs(self.index, [log], {'job': 'A'})['inserted'])
        rotated = self.base / 'agent.log.1'
        log.rename(rotated)
        log.write_text(row.replace('#1', '#2').replace('08:01:00,000', '08:02:00,000'))
        result = accounting.import_agent_logs(self.index, [rotated, log], {'job': 'A'})
        self.assertEqual(1, result['inserted'])
        report = accounting.summarize(self.index, 'A', '2026-10-14T08:00:00+08:00',
                                      '2026-10-14T08:03:00+08:00')
        requests = report['dimensions']['client_model_wait']['requests']
        self.assertEqual(2, len(requests))
        self.assertTrue(all(r['timing_basis'] == 'log_completion_backfill' for r in requests))
        self.assertTrue(all(r['timing_uncertainty_seconds'] >= .05 for r in requests))

    def test_cron_reader_is_read_only_and_distinguishes_delay_from_dispatch(self):
        path = self.base / 'executions.db'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE executions (id,job_id,claimed_at,started_at,finished_at,status,scheduled_instant)')
            db.execute('INSERT INTO executions VALUES (?,?,?,?,?,?,?)',
                ('e','job','2026-10-14T08:00:10+08:00','2026-10-14T08:00:20+08:00',
                 '2026-10-14T08:01:00+08:00','completed','2026-10-14T08:00:00+08:00'))
            db.execute('INSERT INTO executions VALUES (?,?,?,?,?,?,?)',
                ('lost','job','2026-10-14T08:02:00+08:00',None,
                 '2026-10-14T08:02:10+08:00','failed',None))
        before = path.read_bytes()
        rows = accounting.read_cron_records(path, {'job':'A'},
            '2026-10-14T08:00:00+08:00', '2026-10-14T08:03:00+08:00')
        self.assertEqual(before, path.read_bytes())
        report = accounting.summarize(self.index, 'A', '2026-10-14T08:00:00+08:00',
            '2026-10-14T08:03:00+08:00', cron_records=rows)
        dims = report['dimensions']['role_task_dispatch']
        self.assertEqual(10, dims['schedule_delay_seconds'])
        self.assertEqual(10, dims['dispatch_wait_seconds'])
        self.assertEqual(40, dims['execution_seconds'])
        self.assertEqual('execution_never_started', dims['gaps'][0]['reason'])

    def test_log_cutover_excludes_native_request_duplication(self):
        log = self.base/'agent.log'
        log.write_text('2026-10-14 08:01:00,000 INFO [cron_job_20261014_080000] '
                       'agent.conversation_loop: API call #1: model=qwen provider=custom '
                       'in=4 out=2 total=6 latency=30.0s\n')
        accounting.record_api_end(self.index, session_id='cron_job_20261014_080000',
            api_request_id='turn:api:1', role='A', started_at='2026-10-14T08:00:30+08:00',
            ended_at='2026-10-14T08:01:00+08:00')
        result = accounting.import_agent_logs(self.index, [log], {'job':'A'})
        self.assertEqual(0,result['inserted'])
        self.assertEqual(1,result['skipped_after_cutover'])

    def test_reject_naive_or_reversed_time_and_invalid_roles(self):
        with self.assertRaises(ValueError): accounting.summarize(self.index, 'A', '2026-10-14T08:00:00', 200)
        with self.assertRaises(ValueError): accounting.summarize(self.index, 'A', 200, 100)
        with self.assertRaises(ValueError):
            accounting.record_api_end(self.index, session_id='s', api_request_id='r',
                role='A', started_at=140, ended_at=100)


if __name__ == '__main__':
    unittest.main()
