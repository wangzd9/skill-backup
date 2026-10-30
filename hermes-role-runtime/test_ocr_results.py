#!/usr/bin/env python3
"""Zero-model evidence checks: complete coverage must not erase actual findings."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import ocr_results


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


class ResultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ocr-results-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def fixture(self, name='run', findings=None, files=None):
        run = self.root / name
        run.mkdir()
        files = files or ['app/a.py']
        batches = [{'id': f'b{i:02}', 'files': [path]} for i, path in enumerate(files, 1)]
        spec = {'schema': 'coordinator.ocr-run/v1', 'runtime_version': '1.5.0',
                'run_id': name, 'review_identity': 'frozen-review', 'repo': str(self.root / 'repo'),
                'base': 'a' * 40, 'head': 'b' * 40, 'task': 'TASK-fixture', 'attempt': 'attempt-1',
                'provider': 'vllm-local', 'model': 'fixture-model', 'ocr_version': 'OCR v1.13.0',
                'artifacts': {}, 'batches': batches}
        state = {'state': 'AWAITING_DECISION', 'run_id': name, 'batches': []}
        for batch in batches:
            sid = batch['id'] + '-session'
            session = run / (sid + '.jsonl')
            items = [{'item_id': path, 'path': path, 'fingerprint': 'fingerprint-' + path}
                     for path in batch['files']]
            comments = deepcopy(findings or [])
            for comment in comments:
                comment.setdefault('path', batch['files'][0])
            manifest = {'schema_version': 'ocr.run-manifest/v1', 'run_id': sid,
                        'operation': 'review', 'terminal_state': 'complete',
                        'input': {'resolved_base': spec['base'], 'resolved_head': spec['head']},
                        'execution': {'provider': spec['provider'], 'model': spec['model'],
                                      'ocr_version': 'v1.13.0'},
                        'coverage': {'selected': items, 'completed': items, 'reused': [],
                                     'failed': [], 'waived': []}}
            events = [{'type': 'session_start', 'sessionId': sid, 'cwd': spec['repo'],
                       'diffFrom': spec['base'], 'diffTo': spec['head']},
                      {'type': 'review_item_done', 'sessionId': sid, 'filePath': batch['files'][0],
                       'fingerprint': items[0]['fingerprint'], 'comments': comments},
                      {'type': 'session_end', 'sessionId': sid, 'run_manifest': manifest}]
            session.write_text(''.join(json.dumps(e) + '\n' for e in events))
            diagnosis = {'session_file': str(session), 'session_ids': [sid],
                         'coverage_complete': True, 'batch_identity_matches': True,
                         'manifest_schema': 'ocr.run-manifest/v1', 'terminal_state': 'complete',
                         'input': manifest['input'], 'parse_errors': [],
                         'coverage_counts': {key: len(value) for key, value in manifest['coverage'].items()}}
            write_json(run / (batch['id'] + '-diagnosis.json'), diagnosis)
            output = {'status': 'complete', 'session_id': sid, 'manifest': manifest,
                      'llm': {'provider': spec['provider'], 'model': spec['model']},
                      'summary': {'comments': len(comments), 'files_reviewed': len(items)},
                      'comments': comments}
            # Actual OCR writes stderr warning lines before a multiline JSON document.
            (run / (batch['id'] + '.log')).write_text(
                '[ocr] warning (continuing)\n[ocr] tool failed: invalid line range\n' +
                json.dumps(output, indent=2) + '\n')
            state['batches'].append({'id': batch['id'], 'files': batch['files'],
                                    'state': 'REVIEW_COMPLETE', 'returncode': 0,
                                    'session_file': str(session)})
        self.freeze(run, spec)
        write_json(run / 'state.json', state)
        return run

    def freeze(self, run, spec):
        write_json(run / 'spec.json', spec)
        (run / 'spec.sha256').write_text(hashlib.sha256((run / 'spec.json').read_bytes()).hexdigest())

    def log_output(self, run, batch='b01'):
        text = (run / (batch + '.log')).read_text()
        return json.loads(text[text.index('{'):])

    def change_output(self, run, mutate, batch='b01'):
        output = self.log_output(run, batch)
        mutate(output)
        write_json(run / (batch + '.log'), output)

    def test_complete_coverage_preserves_findings_and_never_accepts(self):
        run = self.fixture(findings=[{'content': 'Physical path duplicated', 'severity': 'high',
                                     'start_line': 12, 'end_line': 12}])
        value = ocr_results.inspect_results(run)
        self.assertEqual(value['evidence_state'], 'READY_FOR_ADJUDICATION')
        self.assertTrue(value['coverage_complete'])
        self.assertEqual(value['finding_counts']['total'], 1)
        self.assertEqual(value['finding_counts']['severity']['high'], 1)
        self.assertEqual(value['findings'][0]['content'], 'Physical path duplicated')
        self.assertEqual(value['gate_decision'], 'NOT_EVALUATED')
        self.assertTrue(value['requires_adjudication'])

    def test_explicit_zero_is_ready_but_not_pass(self):
        value = ocr_results.inspect_results(self.fixture())
        self.assertEqual(value['evidence_state'], 'READY_FOR_ADJUDICATION')
        self.assertEqual(value['finding_counts']['total'], 0)
        self.assertEqual(value['gate_decision'], 'NOT_EVALUATED')

    def test_dynamic_batch_route_matches_manifest_and_rejects_missing_or_unauthorized_route(self):
        run = self.fixture()
        spec = json.loads((run / 'spec.json').read_text())
        route = {'provider': spec['provider'], 'model': spec['model'], 'endpoint': 'https://fixture.invalid/v1'}
        spec['model_routing'] = {'allowed_routes': [route]}
        spec['provider'] = 'baseline-provider'
        spec['model'] = 'baseline-model'
        self.freeze(run, spec)
        state = json.loads((run / 'state.json').read_text())
        for entry in state['batches']:
            entry['execution_route'] = dict(route)
        write_json(run / 'state.json', state)
        self.assertEqual(ocr_results.inspect_results(run)['evidence_state'], 'READY_FOR_ADJUDICATION')
        state['batches'][0]['execution_route']['model'] = 'unauthorized-model'
        write_json(run / 'state.json', state)
        self.assert_insufficient(run)
        state['batches'][0].pop('execution_route')
        write_json(run / 'state.json', state)
        self.assert_insufficient(run)

    def test_missing_comments_field_does_not_default_zero(self):
        run = self.fixture()
        self.change_output(run, lambda output: output.pop('comments'))
        self.assert_insufficient(run)

    def test_zero_finding_session_omits_comments_key(self):
        # OCR v1.12.13..v1.13.1 omits `comments` on review_item_done when a
        # file yields zero findings; the authoritative log still carries the
        # explicit empty array. This is a genuine zero, not missing evidence.
        run = self.fixture()
        session = run / 'b01-session.jsonl'
        events = [json.loads(line) for line in session.read_text().splitlines()]
        for event in events:
            if event.get('type') == 'review_item_done':
                event.pop('comments')
        session.write_text(''.join(json.dumps(event) + '\n' for event in events))
        value = ocr_results.inspect_results(run)
        self.assertEqual(value['evidence_state'], 'READY_FOR_ADJUDICATION', value)
        self.assertTrue(value['coverage_complete'])
        self.assertEqual(value['finding_counts']['total'], 0)

    def test_zero_finding_session_absent_key_cannot_hide_log_finding(self):
        # Session omits comments (looks like zero) but the log carries a real
        # finding -> session/log count cross-check must fail closed.
        run = self.fixture(findings=[{'content': 'Hidden blocker', 'severity': 'high'}])
        session = run / 'b01-session.jsonl'
        events = [json.loads(line) for line in session.read_text().splitlines()]
        for event in events:
            if event.get('type') == 'review_item_done':
                event.pop('comments')
        session.write_text(''.join(json.dumps(event) + '\n' for event in events))
        value = self.assert_insufficient(run)
        self.assertTrue(any(f['content'] == 'Hidden blocker' for f in value['findings']))

    def assert_insufficient(self, run):
        result = ocr_results.inspect_results(run)
        self.assertEqual(result['evidence_state'], 'INSUFFICIENT_EVIDENCE', result)
        self.assertIsNone(result['finding_counts']['total'])
        self.assertEqual(result['gate_decision'], 'NOT_EVALUATED')
        return result

    def test_truncation_and_trailing_nonwarning_fail_closed(self):
        for suffix in ('{"status":', 'unexpected result trailer'):
            run = self.fixture(name='truncated-' + str(len(suffix)))
            with (run / 'b01.log').open('a') as stream:
                stream.write(suffix)
            self.assert_insufficient(run)

    def test_partial_and_missing_batch_preserve_observed_problem(self):
        run = self.fixture(files=['app/a.py', 'app/b.py'], findings=[
            {'content': 'Do not publish wrong path', 'severity': 'high'}])
        (run / 'b02.log').unlink()
        value = self.assert_insufficient(run)
        self.assertGreaterEqual(value['finding_counts']['observed_total'], 1)
        self.assertTrue(value['findings'])

    def test_log_manifest_and_summary_disagreements_rejected(self):
        changes = [lambda o: o['manifest']['input'].update(resolved_head='c' * 40),
                   lambda o: o['summary'].update(comments=99),
                   lambda o: o.update(session_id='other-session'),
                   lambda o: o['manifest'].update(schema_version='future'),
                   lambda o: o['manifest']['coverage'].update(completed=[]),
                   lambda o: o['llm'].update(model='other-model'),
                   lambda o: o['manifest']['execution'].update(ocr_version='v9.0.0')]
        for number, change in enumerate(changes):
            run = self.fixture(name='identity-' + str(number))
            self.change_output(run, change)
            self.assert_insufficient(run)

    def test_session_and_diagnosis_binding_are_checked(self):
        run = self.fixture()
        path = run / 'b01-diagnosis.json'
        data = json.loads(path.read_text());data['session_ids'] = ['stale-session']
        write_json(path, data)
        self.assert_insufficient(run)

    def test_changed_comment_cannot_hide_session_findings(self):
        run = self.fixture(findings=[{'content': 'Broken identity', 'severity': 'high'}])
        self.change_output(run, lambda o: (o.update(comments=[]), o['summary'].update(comments=0)))
        value = self.assert_insufficient(run)
        self.assertTrue(any(f['content'] == 'Broken identity' for f in value['findings']))

    def test_partial_session_retains_observed_blocker(self):
        run = self.fixture(findings=[{'content': 'Do not overwrite source', 'severity': 'high'}])
        session = run / 'b01-session.jsonl'
        session.write_text('\n'.join(session.read_text().splitlines()[:-1]) + '\n')
        value = self.assert_insufficient(run)
        self.assertEqual(value['finding_counts']['observed_severity']['high'], 1)

    def test_reused_successful_batch_reads_original_log(self):
        previous = self.fixture(name='previous', findings=[{'content': 'Need fix', 'severity': 'medium'}])
        run = self.fixture(name='retry')
        spec = json.loads((run / 'spec.json').read_text())
        old_state = json.loads((previous / 'state.json').read_text())
        spec['reuse_run'] = str(previous)
        spec['batches'][0]['reuse_session'] = old_state['batches'][0]['session_file']
        self.freeze(run, spec)
        state = json.loads((run / 'state.json').read_text())
        state['batches'][0].update(reused=True, session_file=spec['batches'][0]['reuse_session'])
        write_json(run / 'state.json', state)
        (run / 'b01.log').unlink()
        (run / 'b01-diagnosis.json').write_bytes((previous / 'b01-diagnosis.json').read_bytes())
        value = ocr_results.inspect_results(run)
        self.assertEqual(value['evidence_state'], 'READY_FOR_ADJUDICATION', value)
        self.assertEqual(value['finding_counts']['total'], 1)
        self.assertEqual(value['batches'][0]['source_run'], str(previous))
        # Same path alone is insufficient when the review contract changed.
        spec['review_identity'] = 'different-review'
        self.freeze(run, spec)
        self.assert_insufficient(run)

    def test_duplicate_result_documents_and_duplicate_batch_ids_rejected(self):
        run = self.fixture()
        with (run / 'b01.log').open('a') as stream:
            stream.write(json.dumps(self.log_output(run)))
        self.assert_insufficient(run)
        spec = json.loads((run / 'spec.json').read_text())
        spec['batches'].append(deepcopy(spec['batches'][0]))
        self.freeze(run, spec)
        self.assert_insufficient(run)

    def test_actual_overnight_shape_11_findings_three_batches(self):
        # Redacted regression retaining actual batch structure/severity/line data.
        records = [
            ('app/services/pending_binding.py',
             [('medium', 162), ('medium', 106), ('low', 99), ('low', 218),
              ('high', 216), ('medium', 215), ('medium', 212)]),
            ('app/services/scene_schedule_consumer.py', [('low', 383), ('low', 398)]),
            ('app/services/source_service.py', [('high', 207), ('medium', 199)]),
        ]
        run = self.fixture(files=[path for path, _ in records])
        for number, (path, issues) in enumerate(records, 1):
            batch = f'b{number:02}'
            output = self.log_output(run, batch)
            output['comments'] = [{'path': path, 'content': f'Actual issue at line {line}',
                                   'severity': severity, 'start_line': line, 'end_line': line}
                                  for severity, line in issues]
            output['summary']['comments'] = len(issues)
            write_json(run / (batch + '.log'), output)
            session = run / (batch + '-session.jsonl')
            events = [json.loads(line) for line in session.read_text().splitlines()]
            events[1]['comments'] = output['comments']
            session.write_text(''.join(json.dumps(event) + '\n' for event in events))
        value = ocr_results.inspect_results(run)
        self.assertEqual(value['evidence_state'], 'READY_FOR_ADJUDICATION', value)
        self.assertEqual(value['finding_counts']['total'], 11)
        self.assertEqual(value['finding_counts']['severity'],
                         {'critical': 0, 'high': 2, 'medium': 5, 'low': 4, 'info': 0})

    def test_state_and_frozen_artifacts_tamper_fail_closed(self):
        run = self.fixture()
        state = json.loads((run / 'state.json').read_text())
        state['run_id'] = 'wrong-run'
        write_json(run / 'state.json', state)
        self.assert_insufficient(run)
        state['run_id'] = 'run'
        write_json(run / 'state.json', state)
        spec = json.loads((run / 'spec.json').read_text())
        spec['artifacts']['background.md'] = hashlib.sha256(b'original').hexdigest()
        (run / 'background.md').write_text('tampered')
        self.freeze(run, spec)
        self.assert_insufficient(run)


if __name__ == '__main__':
    unittest.main()
