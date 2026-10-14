import argparse
import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path.home()/'.codex/skills/complex-task-coordinator/scripts'))
import role_runtime
from role_records import record_role


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.binding = {'schema_version': 1, 'project_root': str(self.root), 'control': 'ACTIVE',
                        'roles': {'A': {'session_file': 'sessions/A.md', 'skill': 'executor'}}}
        (self.root/'ROLE-BINDINGS.json').write_text(json.dumps(self.binding))
        self.session = self.root/'sessions/A.md'
        self.session.parent.mkdir()
        self.session.write_text('# 保留既有内容\n原来的一行\n')
        self.claim = role_runtime.execute(argparse.Namespace(action='claim', project=str(self.root), role='A',
                                                             owner='isolated-test', pid=None, ttl=300))
        self.token = self.root/'token.json'
        self.token.write_text(json.dumps({'token': self.claim['token']}))
        self.payload = {'event_id': 'A:TASK-1:attempt-1:delivery', 'kind': 'business', 'task': 'TASK-1',
                        'attempt': 'attempt-1', 'status': 'DELIVERED', 'summary': '保留 `$()` 和中文\n不执行',
                        'evidence': 'receipts/test.md', 'next': '等待独立测试'}

    def write(self, payload=None):
        return record_role(self.root, 'A', payload or self.payload, self.token)

    def test_idempotent_append_preserves_old_text_and_records_unknown_time(self):
        first = self.write()
        text = self.session.read_text()
        self.assertTrue(text.startswith('# 保留既有内容\n原来的一行\n'))
        self.assertIn('发生时间未知', text)
        self.assertIsNone(first['occurred_at'])
        self.assertRegex(first['recorded_at'], r'T\d\d:\d\d:\d\d[+-]\d\d:\d\d$')
        self.assertFalse(self.write()['created'])
        self.assertEqual(text, self.session.read_text())
        with self.assertRaisesRegex(ValueError, 'EVENT_ID_CONFLICT'):
            self.write({**self.payload, 'summary': '新含义'})

    def test_observation_does_not_modify_session_or_create_event(self):
        original = self.session.read_text()
        result = self.write({**self.payload, 'kind': 'observation', 'occurrence': {'kind': 'now'}})
        self.assertEqual(original, self.session.read_text())
        self.assertFalse((self.root/'events').exists())
        self.assertFalse(result['business_event'])
        self.assertEqual(result['occurred_at'], result['recorded_at'])
        self.assertIn('/runtime/observations/A/', result['path'])

    def test_crash_between_event_and_session_recovers_once(self):
        original = role_runtime.atomic
        with patch.object(role_runtime, 'atomic', side_effect=OSError('isolated crash')):
            with self.assertRaises(OSError):
                self.write()
        self.assertEqual(len(list((self.root/'events/A').glob('*.json'))), 1)
        self.write()
        self.write()
        self.assertEqual(self.session.read_text().count('role-record:'), 1)

    def test_time_is_read_from_evidence_not_invented_from_supplement_time(self):
        source = self.root/'result.json'
        source.write_text(json.dumps({'ended_at': '2026-10-14T10:22:17+08:00'}))
        result = self.write({**self.payload, 'occurrence': {'source': 'result.json', 'field': 'ended_at'}})
        self.assertEqual(result['occurred_at'], '2026-10-14T10:22:17+08:00')
        row = json.loads(Path(result['event']).read_text())
        self.assertEqual(row['time_source']['field'], 'ended_at')
        self.assertEqual(row['time_source']['source'], 'result.json')

    def test_minute_evidence_keeps_original_precision_and_timezone(self):
        for number, value in enumerate(('2026-10-14T10:22+08:00', '2026-10-14T02:22Z')):
            with self.subTest(value=value):
                (self.root/'result.json').write_text(json.dumps({'ended_at': value}))
                result = self.write({**self.payload, 'event_id': f'A:TASK-1:minute-{number}',
                                     'occurrence': {'source': 'result.json', 'field': 'ended_at'}})
                self.assertEqual(result['occurred_at'], value)
                self.assertEqual(json.loads(Path(result['event']).read_text())['occurred_at'], value)
                self.assertIn(value, self.session.read_text())

    def test_minute_evidence_still_requires_real_date_known_minutes_and_timezone(self):
        for value in ('10:22+08:00', '2026-10-14T10:22', '2026-10-14T10:2x+08:00',
                      '2026-02-30T10:22+08:00', '2026-10-14T25:22+08:00'):
            with self.subTest(value=value):
                (self.root/'result.json').write_text(json.dumps({'ended_at': value}))
                with self.assertRaises(ValueError):
                    self.write({**self.payload, 'occurrence': {'source': 'result.json', 'field': 'ended_at'}})

    def test_imprecise_source_and_manual_time_are_rejected(self):
        (self.root/'result.json').write_text(json.dumps({'ended_at': '2026-10-14 10:2x'}))
        with self.assertRaises(ValueError):
            self.write({**self.payload, 'occurrence': {'source': 'result.json', 'field': 'ended_at'}})
        with self.assertRaises(ValueError):
            self.write({**self.payload, 'occurred_at': '2026-10-14T10:20:00+08:00'})

    def test_stale_token_and_escape_cannot_write(self):
        self.token.write_text(json.dumps({'token': 'stale'}))
        with self.assertRaisesRegex(ValueError, 'token'):
            self.write()
        self.assertFalse((self.root/'events').exists())
        self.token.write_text(json.dumps({'token': self.claim['token']}))
        self.binding['roles']['A']['session_file'] = '../outside.md'
        (self.root/'ROLE-BINDINGS.json').write_text(json.dumps(self.binding))
        with self.assertRaisesRegex(ValueError, '项目内'):
            self.write()

    def test_modified_existing_session_block_is_not_silently_accepted(self):
        self.write()
        self.session.write_text(self.session.read_text().replace('等待独立测试', '未经验证通过'))
        with self.assertRaisesRegex(ValueError, '禁止静默覆盖'):
            self.write()

    def test_ambiguous_clock_in_text_is_rejected_but_unknown_is_allowed(self):
        for text in ('今晨10:2x 强模型补报', '10:1X完成', '处理时间 10:?5', '1x:22 完成', '10:22:?? 完成'):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, '模糊钟点'):
                self.write({**self.payload, 'summary': text})
        self.write({**self.payload, 'summary': '发生时间未知；仅确认目前完成'})

    def test_staged_timeline_handles_unknown_occurrence_without_backdating(self):
        staged = ROOT.parent/'skills/complex-task-coordinator/scripts/role_runtime.py'
        if not staged.is_file():
            staged = Path.home()/'.codex/skills/complex-task-coordinator/scripts/role_runtime.py'
        spec = importlib.util.spec_from_file_location('staged_role_runtime', staged)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.binding['roles']['COORDINATOR'] = {'session_file': 'sessions/COORDINATOR.md', 'skill': 'coordinator'}
        (self.root/'ROLE-BINDINGS.json').write_text(json.dumps(self.binding))
        self.write()
        self.write({**self.payload, 'event_id': 'A:TASK-1:known-time', 'occurrence': {'kind': 'now'}})
        claim = module.execute(argparse.Namespace(action='claim', project=str(self.root), role='COORDINATOR',
                                                 owner='isolated-timeline', pid=None, ttl=300))
        output = module.execute(argparse.Namespace(action='timeline', project=str(self.root), role='COORDINATOR',
                                                  token=claim['token']))
        timeline = Path(output['path']).read_text()
        self.assertIn('发生时间未知', timeline)
        self.assertIn('补录：', timeline)
        self.assertEqual(output['appended'], 2)

    def test_staged_legacy_event_preserves_minute_precision_and_rejects_ambiguous_input(self):
        staged = ROOT.parent/'skills/complex-task-coordinator/scripts/role_runtime.py'
        if not staged.is_file():
            staged = Path.home()/'.codex/skills/complex-task-coordinator/scripts/role_runtime.py'
        spec = importlib.util.spec_from_file_location('staged_role_runtime_minutes', staged)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        def event(value, identifier):
            return module.execute(argparse.Namespace(action='event', project=str(self.root), role='A',
                token=self.claim['token'], occurred_at=value, event_id=identifier, task='TASK-1',
                attempt='attempt-1', status='DELIVERED', summary='有来源的交付记录', evidence='result.json', next='待测试'))
        for number, value in enumerate(('2026-10-14T10:22+08:00', '2026-10-14T02:22Z')):
            with self.subTest(value=value):
                result = event(value, f'A:legacy-minute-{number}')
                self.assertEqual(json.loads(Path(result['path']).read_text())['occurred_at'], value)
        for value in ('10:22+08:00', '2026-10-14T10:22', '2026-10-14T10:2x+08:00'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                event(value, 'A:legacy-invalid')


if __name__ == '__main__':
    unittest.main()
