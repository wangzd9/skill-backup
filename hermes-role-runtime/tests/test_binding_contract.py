import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path.home()/'.hermes/hermes-agent'))
from binding_contract import registry_bindings, validate_candidate


class BindingContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.projects = [self.home/name for name in ('first', 'second')]
        for root in self.projects:
            root.mkdir()
            self.write(root, {'A': {'chat_id': root.name, 'poll_job_id': root.name+'-poll'}})
        self.registry = self.home/'registry.json'
        self.registry.write_text(json.dumps({'projects': [{'project_root': str(p)} for p in self.projects]}))

    def write(self, root, roles):
        (root/'ROLE-BINDINGS.json').write_text(json.dumps({'schema_version': 1,
            'project_root': str(root), 'control': 'ACTIVE', 'roles': roles}))

    def plugin(self):
        spec = importlib.util.spec_from_file_location('binding_contract_plugin', ROOT/'__init__.py')
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module.RuntimePlugin(registry=self.registry, home=self.home, metrics_url='unused')

    def test_audit_record_does_not_hide_healthy_roles_or_become_authority(self):
        first = self.projects[0]
        self.write(first, {'A': {'chat_id': 'first', 'poll_job_id': 'first-poll'},
                           'REBIND-20261018': {'summary': 'audit, not role'}})
        plugin = self.plugin()
        self.assertEqual('A', plugin.api_identity('cron_first-poll_20261019_090000')['role'])
        self.assertEqual(2, len(plugin.bindings()))
        self.assertEqual('UNKNOWN_ROLE_OR_AUDIT_RECORD', plugin.binding_issues[0]['code'])
        result = validate_candidate(first, first/'ROLE-BINDINGS.json', self.registry)
        self.assertEqual('INVALID', result['state'])

    def test_bad_project_is_quarantined_without_losing_healthy_project(self):
        plugin = self.plugin()
        plugin.bindings()
        (self.projects[0]/'ROLE-BINDINGS.json').write_text('{bad')
        self.assertEqual(str(self.projects[1]), plugin.api_identity('cron_second-poll_20261019_090000')['project'])
        with self.assertRaisesRegex(ValueError, 'QUARANTINED'):
            plugin.api_identity('cron_first-poll_20261019_090000')
        self.assertEqual('block', plugin.on_tool(tool_name='terminal', session_id='cron_first-poll_20261019_090000', turn_id='t')['action'])

    def test_cold_start_cannot_treat_missing_identity_as_unrestricted(self):
        (self.projects[0]/'ROLE-BINDINGS.json').write_text('{bad')
        plugin = self.plugin()
        with self.assertRaisesRegex(ValueError, 'UNVERIFIABLE'):
            plugin.api_identity('unknown-group-session')
        self.assertEqual('A', plugin.api_identity('cron_second-poll_20261019_090000')['role'])

    def test_missing_chat_id_retains_only_quarantine_locator(self):
        plugin = self.plugin()
        plugin.bindings()
        self.write(self.projects[0], {'A': {'poll_job_id': 'first-poll'}})
        with sqlite3.connect(self.home/'state.db') as db:
            db.execute('CREATE TABLE sessions(id TEXT,chat_id TEXT,parent_session_id TEXT)')
            db.executemany('INSERT INTO sessions VALUES(?,?,?)', [('broken', 'first', None), ('other', 'unrelated', None)])
        with self.assertRaisesRegex(ValueError, 'QUARANTINED'):
            plugin.api_identity('broken')
        self.assertIsNone(plugin.api_identity('other'))
        self.assertEqual('A', plugin.api_identity('cron_second-poll_20261019_090000')['role'])

    def test_identity_override_and_duplicate_binding_fail_closed(self):
        first, second = self.projects
        self.write(first, {'A': {'chat_id': 'first', 'root': str(second)}})
        with self.assertRaisesRegex(ValueError, 'QUARANTINED'):
            plugin = self.plugin()
            with sqlite3.connect(self.home/'state.db') as db:
                db.execute('CREATE TABLE sessions(id TEXT,chat_id TEXT,parent_session_id TEXT)')
                db.execute('INSERT INTO sessions VALUES(?,?,?)', ('s', 'first', None))
            plugin.api_identity('s')
        self.write(first, {'A': {'chat_id': 'second'}})
        with self.assertRaisesRegex(ValueError, 'AMBIGUOUS'):
            with sqlite3.connect(self.home/'state.db') as db:
                db.execute('UPDATE sessions SET chat_id=? WHERE id=?', ('second', 's'))
            plugin.api_identity('s')
        self.assertTrue(any(x['code'] == 'DUPLICATE_OR_INVALID_BINDING_ID' for x in
                            validate_candidate(first, first/'ROLE-BINDINGS.json', self.registry)['issues']))

    def test_usage_end_keeps_begin_attribution_after_rebind_and_route_change(self):
        from usage_accounting import summarize
        plugin = self.plugin()
        plugin.usage_routes = lambda: [('local', 'http://local/v1', 'm')]
        fields = dict(session_id='cron_first-poll_20261019_090000', model='m', api_request_id='r',
            physical_attempt_id='physical', started_at=100, base_url='http://local/v1')
        plugin.record_usage('start', **fields)
        self.write(self.projects[0], {})
        self.write(self.projects[1], {'A': {'chat_id': 'second', 'poll_job_id': 'first-poll'}})
        plugin.usage_routes = lambda: [('cloud', 'http://local/v1', 'm')]
        plugin.record_usage('end', **fields, ended_at=120, usage={'input_tokens': 10, 'output_tokens': 3})
        report = summarize(self.home/'runtime/role-usage.db', 'A', 90, 130, project=self.projects[0])
        self.assertEqual(1, report['calls'])
        self.assertEqual(13, report['routes']['local']['known_tokens']['total_tokens'])
        self.assertEqual(0, summarize(self.home/'runtime/role-usage.db', 'A', 90, 130, project=self.projects[1])['calls'])

    def test_usage_end_closes_original_attempt_even_with_no_current_binding(self):
        from usage_accounting import summarize
        plugin = self.plugin()
        fields = dict(session_id='cron_first-poll_20261019_090000', model='m', api_request_id='r',
            physical_attempt_id='physical', started_at=100)
        plugin.record_usage('start', **fields)
        for project in self.projects:
            self.write(project, {})
        plugin.record_usage('end', **fields, ended_at=120, usage={'input_tokens': 10, 'output_tokens': 3})
        report = summarize(self.home/'runtime/role-usage.db', 'A', 90, 130, project=self.projects[0])
        self.assertEqual(1, report['usage_calls'])

    def test_cached_old_activation_manifest_cannot_hide_new_core_seams(self):
        import hashlib
        from unittest.mock import patch
        from agent import native_policy_activation as old
        from cron import scheduler, executions
        import agent.turn_api_request as requests
        self.plugin()
        module = sys.modules['binding_contract_plugin']
        legacy_lock = old._LOCK
        with patch.object(old, '_TARGETS', {}), patch.object(old, 'activate_native_turn_policy',
                side_effect=AssertionError('must not execute cached helper')):
            result = module.activate_current_native_policy()
        self.assertEqual('ACTIVATED', result['state'])
        self.assertIn('cron/executions.py', result['source_sha256'])
        self.assertIn('agent/turn_api_request.py', result['source_sha256'])
        self.assertIs(scheduler.recover_interrupted_executions, executions.recover_interrupted_executions)
        name = 'agent._role_native_activation_'+result['activation_helper_sha256']
        self.assertIs(legacy_lock, sys.modules[name]._LOCK)
        self.assertEqual(hashlib.sha256(Path(old.__file__).read_bytes()).hexdigest(), result['activation_helper_sha256'])

    def test_native_manual_turn_overrides_a_restored_historical_cron_id_for_usage(self):
        plugin = self.plugin()
        session = 'cron_first-poll_20261019_090000'
        plugin.bind_manual_turn(session, 'native-turn', {'trusted_gateway': True, 'platform': 'feishu',
            'chat_id': 'second', 'user_id': 'u', 'message_id': 'm'})
        plugin.record_usage('end', session_id=session, turn_id='native-turn', model='m', api_request_id='r',
            physical_attempt_id='physical', started_at=100, ended_at=120, usage={'input_tokens': 10, 'output_tokens': 3})
        with sqlite3.connect(self.home/'runtime/role-usage.db') as db:
            self.assertEqual((str(self.projects[1]), 'group_chat'), db.execute('SELECT project,session_kind FROM role_usage').fetchone())


if __name__ == '__main__':
    unittest.main()
