"""Evidence links must resolve against the file that actually stores each fact."""
import json
from pathlib import Path
import tempfile
import unittest

from report_contract import _reference, build_report
from runtime_common import save_json
from tests.test_report_contract import fixture


def resolve_reference(reference):
    filename, fragment = reference.split('#', 1)
    value = json.loads(Path(filename).read_text())
    if fragment:
        if not fragment.startswith('/'):
            raise AssertionError('not a JSON Pointer')
        for segment in fragment[1:].split('/'):
            key = segment.replace('~1', '/').replace('~0', '~')
            value = value[int(key)] if isinstance(value, list) else value[key]
    return value


class ReportReferenceTests(unittest.TestCase):
    def test_document_root_and_escaped_segments_follow_json_pointer(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'evidence.json'
            value = {'a/b': {'~name': 7}}
            save_json(path, value)
            packet = {'evidence_reference': str(path)}
            self.assertEqual(resolve_reference(_reference(packet, ())), value)
            self.assertEqual(resolve_reference(_reference(packet, ('a/b', '~name'))), 7)
            packet['runtime_observation'] = {'evidence_reference': str(path)}
            self.assertEqual(resolve_reference(_reference(packet, ('runtime_observation',))), value)

    def test_runtime_and_review_facts_resolve_in_independent_observation_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packet = fixture()
            packet['evidence_reference'] = str(root / 'projection.json')
            packet['role_statuses'] = {}
            save_json(packet['evidence_reference'], packet)
            observation = {
                'kind': 'live-runtime-observation', 'checked_at': '2026-10-16T18:00:00+08:00',
                'state': 'VERIFIED', 'errors': [], 'ocr': [],
                'roles': {'A': {'state': 'VERIFIED', 'owner': {'state': 'alive'},
                    'checkpoint': {'source': 'A.json', 'value': {'task': 'TASK-16C-A',
                        'attempt': 'attempt-1', 'next': '继续验证固定快照'}},
                    'operations': [], 'errors': []}},
                'strong_review': {'state': 'VERIFIED', 'source': 'strong-review-state.json',
                    'checked_at': '2026-10-16T18:00:00+08:00',
                    'value': {'authorization': 'REVOKED', 'active_calls': [],
                        'used_last_60_minutes': 1, 'limit': 5, 'cost': None,
                        'last_call': {'status': 'SUCCEEDED'}}}}
            runtime_path = root / 'runtime.json'
            save_json(runtime_path, observation)
            packet['runtime_observation'] = {**observation, 'evidence_reference': str(runtime_path)}
            report = build_report(packet)
            references = [source['evidence_reference']
                for section in report['sections'] for item in section['items']
                for source in item['sources']]
            runtime_references = [reference for reference in references if reference.startswith(str(runtime_path) + '#')]
            self.assertTrue(runtime_references)
            for reference in runtime_references:
                resolve_reference(reference)
            self.assertIn(str(runtime_path) + '#/strong_review/value/authorization', runtime_references)
            self.assertNotIn('runtime_observation', json.loads(Path(packet['evidence_reference']).read_text()))
            for reference in references:
                if reference == str(root / 'projection.json') + '#':
                    self.assertEqual(resolve_reference(reference)['fingerprint'], packet['fingerprint'])


if __name__ == '__main__':
    unittest.main()
