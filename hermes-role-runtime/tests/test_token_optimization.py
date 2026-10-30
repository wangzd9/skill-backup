"""Isolated synthetic input and real native-selection regressions; no model calls."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from workflow_model_input import deduplicate, project
from workflow_toolsets import configure
from workflow_views import size


_UNSET = object()


def expand(document, value=_UNSET, stack=()):
    value = document if value is _UNSET else value
    if isinstance(value, dict):
        if set(value) == {'same_as'}:
            pointer = value['same_as']
            if pointer in stack:
                raise AssertionError('cyclic reference: ' + pointer)
            target = document
            for part in pointer.lstrip('/').split('/'):
                part = part.replace('~1', '/').replace('~0', '~')
                target = target[int(part)] if isinstance(target, list) else target[part]
            return expand(document, target, (*stack, pointer))
        if {'detail_source', 'index', 'field', 'expanded'} <= set(value):
            ref = document['detail_artifacts'][value['detail_source']]
            raw = Path(ref['path']).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == ref['sha256']
            return expand(document, json.loads(raw)[value['index']][value['field']], stack)
        return {key: expand(document, child, stack) for key, child in value.items()}
    if isinstance(value, list):
        return [expand(document, child, stack) for child in value]
    return value


class ModelInputTests(unittest.TestCase):
    def fixture(self):
        scenario = '此为隔离合成验收场景，必须独立核验固定版本及未闭合问题。' * 30
        checkpoint = {'next': scenario, 'risk': 'UNKNOWN', 'failure': 'FAIL',
                      'special~/key': {'evidence': scenario}, 'nullable': None}
        return {'work': {'checkpoint': checkpoint}, 'tasks': [{'task_id': 'synthetic'}],
            'completion': {'plan': {'obligations': [
                {'id': 'synthetic:' + phase, 'phase': phase, 'owner': owner,
                 'scenario': scenario, 'applicable': applicable, 'predecessors': ['synthetic:local']}
                for phase, owner, applicable in [('local', 'TESTER', True),
                    ('closure', 'COORDINATOR', True), ('production', 'COORDINATOR', False)]]}},
            'events': [{'event_id': i, 'kind': 'CHECKPOINT_SAVED',
                        'payload': {'checkpoint': copy.deepcopy(checkpoint)}} for i in range(1, 4)],
            'verifications': [{'candidate_id': 'synthetic', 'result': r,
                               'evidence': {'reason': scenario}, 'supersedes': []}
                              for r in ('PASS', 'FAIL', 'UNKNOWN', 'UNVERIFIED')],
            'external_actions': [{'action_id': str(i), 'state': state,
                'authorization': {'grant': scenario, 'expires_at': 99}, 'evidence': {'reason': scenario}}
                for i, state in enumerate(('ENDED', 'REJECTED', 'UNKNOWN', 'INCOMPLETE', 'SUBMITTED', 'RESULT_READY'))]}

    def test_exact_recursive_reconstruction_and_all_gate_rows(self):
        original = self.fixture()
        projected = copy.deepcopy(original)
        references = deduplicate(projected)
        self.assertGreater(references, 0)
        self.assertEqual(original, expand(projected))
        for name in ('work', 'tasks', 'verifications', 'external_actions'):
            self.assertEqual(original[name], projected[name])
        self.assertEqual([1, 2, 3], [e['event_id'] for e in projected['events']])
        self.assertEqual(['local', 'closure', 'production'],
                         [o['phase'] for o in projected['completion']['plan']['obligations']])
        self.assertLess(size(projected), size(original))

    def test_only_terminal_authorization_archived_and_hash_recoverable(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = types.SimpleNamespace(path=Path(root) / 'synthetic.sqlite3')
            original = self.fixture()
            result = project(ledger, copy.deepcopy(original))
            restored = expand(result)
            for name, value in original.items():
                self.assertEqual(value, restored[name])
            for i in range(2, 6):
                self.assertEqual(original['external_actions'][i], result['external_actions'][i])
            self.assertEqual(2, result['input_projection']['terminal_authorizations_archived'])
            reference = result['detail_artifacts']['external_actions']
            Path(reference['path']).write_text('tampered isolated fixture')
            with self.assertRaisesRegex(ValueError, 'HASH_MISMATCH'):
                project(ledger, copy.deepcopy(original))

    def test_no_cross_session_memory_and_changed_text_is_not_shared(self):
        first = self.fixture()
        deduplicate(first)
        next_packet = self.fixture()
        next_packet['events'][0]['payload']['checkpoint']['next'] += '实际版本已变化'
        original = copy.deepcopy(next_packet)
        deduplicate(next_packet)
        self.assertEqual(original, expand(next_packet))
        self.assertIn('实际版本已变化', json.dumps(next_packet['events'][0], ensure_ascii=False))

    def test_pointer_escaping_and_no_cycle_when_container_has_shared_children(self):
        body = {'a~/b': '合成正文' * 200}
        packet = {'work': {'checkpoint': body}, 'completion': {'plan': {'text': body['a~/b']}},
                  'events': [{'payload': copy.deepcopy(body)}, {'payload': {'nested': copy.deepcopy(body)}}]}
        original = copy.deepcopy(packet)
        deduplicate(packet)
        self.assertEqual(original, expand(packet))
        self.assertEqual('/work/checkpoint/a~0~1b', packet['completion']['plan']['text']['same_as'])


class ToolDiscoveryTests(unittest.TestCase):
    def selected(self, role, readonly=False):
        from toolsets import resolve_multiple_toolsets, TOOLSETS
        from cron.scheduler import _merge_mcp_into_per_job_toolsets
        with patch('hermes_cli.tools_config.enabled_mcp_server_names', side_effect=AssertionError('must not enumerate MCP')):
            selected = _merge_mcp_into_per_job_toolsets(configure(role, readonly=readonly), {})
        groups = {name: {'tools': [tool], 'includes': []} for name, tool in (
            ('role_coordination', 'role_coordination_command'),
            ('role_testing', 'role_test_command'), ('role_development', 'role_development_command'))}
        with patch.dict(TOOLSETS, groups):
            return set(resolve_multiple_toolsets(selected))

    def test_coordinator_and_tester_keep_reads_without_denied_shell_mutation(self):
        for role in ('COORDINATOR', 'TESTER'):
            with self.subTest(role=role):
                selected = self.selected(role)
                self.assertTrue({'read_file', 'search_files', 'skill_view', 'role_work_input', 'role_repo_state'} <= selected)
                self.assertIn({'COORDINATOR': 'role_coordination_command', 'TESTER': 'role_test_command'}[role], selected)
                self.assertFalse({'terminal', 'process_manage', 'write_file', 'patch', 'skill_manage',
                    'role_manual_lease', 'role_requirement_select', 'role_control_request'} & selected)

    def test_developers_keep_scoped_edits_and_readonly_reports_hide_commands(self):
        for role in ('A', 'B', 'C'):
            self.assertTrue({'terminal', 'write_file', 'patch', 'read_file'} <= self.selected(role))
            self.assertFalse({'terminal', 'write_file', 'patch', 'role_development_command'} & self.selected(role, True))
        self.assertEqual(self.selected('COORDINATOR', True), self.selected('TESTER', True))

    def test_native_denylist_still_applies_to_custom_group(self):
        from model_tools import _select_tool_names
        enabled = [name for name in configure('A') if name != 'no_mcp']
        selected = _select_tool_names(enabled, ['terminal', 'file'], True)
        self.assertFalse({'terminal', 'read_file', 'search_files', 'write_file', 'patch'} & selected)
        self.assertIn('role_repo_state', selected)

    def test_activation_declares_every_new_function(self):
        import ast
        from mechanism_activation import TARGETS
        root = Path(__file__).resolve().parents[1]
        for name in ('workflow_model_input.py', 'workflow_toolsets.py'):
            functions = {node.name for node in ast.parse((root / name).read_text()).body
                         if isinstance(node, ast.FunctionDef)}
            self.assertEqual(functions, set(TARGETS[name]))
        self.assertIn('work_packet', TARGETS['workflow_views.py'])
        self.assertIn('prepare_report', TARGETS['workflow_runtime.py'])
        self.assertIn('PrefixAudit.record', TARGETS['prefix_audit.py'])


class PrefixMetadataTests(unittest.TestCase):
    def test_sizes_are_observed_bytes_and_never_claim_full_prompt_tokens(self):
        from prefix_audit import PrefixAudit
        with tempfile.TemporaryDirectory() as root:
            request = {'tools': [{'function': {'name': 'synthetic'}}]}
            original = copy.deepcopy(request)
            result = PrefixAudit(Path(root)).record(session_id='isolated', request=request, system_prompt='合成正文')
            self.assertEqual(original, request)
            self.assertEqual(len('合成正文'.encode()), result['system_observed_bytes'])
            self.assertEqual('sanitized_hook_payload_only', result['measurement_scope'])
            self.assertIsNone(result['measured_section_tokens'])
            self.assertNotIn('合成正文', ''.join(p.read_text() for p in Path(root).glob('*.json')))


if __name__ == '__main__':
    unittest.main()
