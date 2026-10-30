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
import usage_accounting as usage


class UsageTests(unittest.TestCase):
    def test_exact_request_lookup_freezes_attribution_and_rejects_logical_collision(self):
        with tempfile.TemporaryDirectory() as temp:
            index = Path(temp)/'usage.db'
            self.assertIsNone(usage.request_identity(index, session_id='s', api_request_id='r'))
            self.assertFalse(index.exists())
            fields = dict(session_id='s', api_request_id='r', physical_attempt_id='attempt',
                role='A', project=temp+'/old', model='m', provider='custom', route='local',
                endpoint_alias='frozen', parent_session_id='parent', session_kind='business_cron',
                aux_task='compression', started_at=100)
            usage.record(index, 'start', **fields)
            original = index.read_bytes()
            identity = usage.request_identity(index, session_id='s', api_request_id='r', physical_attempt_id='attempt')
            self.assertEqual(temp+'/old', identity['project'])
            self.assertEqual(('A', 'local', 'frozen', 'parent', 'business_cron', 'compression'),
                tuple(identity[k] for k in ('role', 'route', 'endpoint_alias', 'parent_session_id', 'session_kind', 'aux_task')))
            self.assertEqual(original, index.read_bytes())
            self.assertIsNone(usage.request_identity(index, session_id='s', api_request_id='r', physical_attempt_id='another'))
            with self.assertRaisesRegex(ValueError, 'logical_request_id'):
                usage.request_identity(index, session_id='s', api_request_id='other', physical_attempt_id='attempt')

    def test_existing_ledger_migrates_idempotently_and_finishes_pre_upgrade_attempt(self):
        with tempfile.TemporaryDirectory() as temp:
            index = Path(temp)/'usage.db'
            with sqlite3.connect(index) as db:
                db.execute('''CREATE TABLE role_usage (
                    session_id TEXT NOT NULL, physical_attempt_id TEXT NOT NULL,
                    logical_request_id TEXT NOT NULL, role TEXT NOT NULL, project TEXT NOT NULL,
                    task_kind TEXT NOT NULL, model TEXT NOT NULL, provider TEXT NOT NULL,
                    endpoint_alias TEXT NOT NULL, route TEXT NOT NULL, started_at REAL NOT NULL,
                    ended_at REAL, status TEXT NOT NULL, usage_present INTEGER NOT NULL,
                    usage_json TEXT, identity_basis TEXT NOT NULL,
                    PRIMARY KEY(session_id,physical_attempt_id))''')
                measured = json.dumps({'input_tokens': 10, 'output_tokens': 2, 'cache_read_tokens': 0,
                    'cache_write_tokens': 0, 'reasoning_tokens': 0, 'prompt_tokens': 10, 'total_tokens': 12})
                for physical, ended, status, present, tokens in [('done', 110, 'completed', 1, measured),
                                                               ('open', None, 'open', 0, None)]:
                    db.execute('INSERT INTO role_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        ('s', physical, physical, 'A', temp, 'main', 'model', 'custom', '', 'local',
                         100, ended, status, present, tokens, 'physical_hook'))
            fields = dict(session_id='s', api_request_id='open', physical_attempt_id='open', role='A',
                project=temp, model='model', provider='custom', route='local', started_at=100, ended_at=120,
                parent_session_id='parent', session_kind='group_chat', usage={'input_tokens': 20, 'output_tokens': 3,
                                                                            'prompt_tokens': 20})
            usage.record(index, 'end', **fields)
            usage.record(index, 'end', **fields)
            preserved = usage.record(index, 'end', **dict(fields, api_request_id='done', physical_attempt_id='done'))
            self.assertEqual('unknown', preserved['session_kind'])
            self.assertEqual(measured, preserved['usage_json'])
            with sqlite3.connect(index) as db:
                db.row_factory = sqlite3.Row
                columns = [row['name'] for row in db.execute('PRAGMA table_info(role_usage)')]
                self.assertEqual(1, columns.count('parent_session_id'))
                self.assertEqual(1, columns.count('session_kind'))
                opened = db.execute("SELECT * FROM role_usage WHERE physical_attempt_id='open'").fetchone()
                self.assertEqual(('parent', 'group_chat', 'group_chat'),
                                 (opened['parent_session_id'], opened['session_kind'], opened['task_kind']))
            report = usage.summarize(index, 'A', 90, 130, project=temp)
            self.assertEqual(2, report['calls'])
            self.assertEqual(35, report['routes']['local']['known_tokens']['total_tokens'])

    def test_attempt_dedup_boundaries_and_missing_usage_preserve_real_cost(self):
        with tempfile.TemporaryDirectory() as temp:
            index = Path(temp)/'usage.db'
            common = dict(session_id='s', api_request_id='logical', role='A', project=temp,
                          model='same-name', provider='custom', endpoint_alias='server')
            for physical, start, end, route, measured in (
                    ('one', 100, 110, 'local', {'input_tokens': 10, 'cache_read_tokens': 20, 'cache_write_tokens': 0, 'output_tokens': 5, 'reasoning_tokens': 3}),
                    ('two', 111, 120, 'cloud', {'input_tokens': 30, 'output_tokens': 5, 'prompt_tokens': 30}),
                    ('failed', 121, 130, 'cloud', None),
                    ('edge', 130, 140, 'local', {'input_tokens': 99, 'output_tokens': 1})):
                fields = dict(common, physical_attempt_id=physical, attempt_started_at=start, route=route)
                usage.record(index, 'start', **fields, request='SECRET', api_key='SECRET')
                usage.record(index, 'error' if measured is None else 'end', **fields, ended_at=end, usage=measured)
                usage.record(index, 'end', **fields, ended_at=end, usage=measured)
            report = usage.summarize(index, 'A', 100, 140, project=temp)
            self.assertEqual((3, 2, 1), (report['calls'], report['usage_calls'], report['missing_usage_calls']))
            self.assertEqual(35, report['routes']['local']['known_tokens']['total_tokens'])
            self.assertEqual(35, report['routes']['cloud']['known_tokens']['total_tokens'])
            self.assertEqual(1/3, report['routes']['local']['call_ratio'])
            self.assertEqual(.5, report['routes']['local']['known_token_ratio'])
            self.assertEqual(0, usage.summarize(index, 'B', 100, 140, project=temp)['calls'])
            self.assertEqual(0, usage.summarize(index, 'A', 100, 140, project=temp+'/other')['calls'])
            self.assertNotIn(b'SECRET', index.read_bytes())

    def test_wire_presence_dispatch_business_identity_and_completion_windows(self):
        from agent.api_request_hooks import usage_summary_for_hook
        with tempfile.TemporaryDirectory() as temp:
            index = Path(temp)/'usage.db'
            common = dict(session_id='s', api_request_id='logical', role='A', project=temp,
                run_id='run-old', work_id='work-old', task_id='task-old', business_attempt=2,
                execution_id='exec-old', session_kind='business_cron', model='m', provider='custom')
            unknown = usage_summary_for_hook({'prompt_tokens': 40, 'completion_tokens': 3},
                                            provider='custom', api_mode='chat_completions')
            miss = usage_summary_for_hook({'prompt_tokens': 20, 'completion_tokens': 2,
                'prompt_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 0}},
                provider='custom', api_mode='chat_completions')
            for attempt, finish, route, measured in [('retry', 110, 'unknown', None),
                    ('success', 120, 'cloud', unknown), ('edge', 130, 'local', miss)]:
                fields = dict(common, physical_attempt_id=attempt, started_at=100, route=route)
                usage.record(index, 'start', **fields)
                usage.record(index, 'error' if measured is None else 'end', **fields, ended_at=finish,
                    usage=measured, provider_dispatched=False if measured is None else True)
                usage.record(index, 'end', **fields, ended_at=finish, usage=measured)
            identity = usage.request_identity(index, session_id='s', api_request_id='logical', physical_attempt_id='success')
            self.assertEqual('work-old', identity['work_id'])
            self.assertEqual(2, identity['business_attempt'])
            with self.assertRaisesRegex(ValueError, 'run_id'):
                usage.record(index, 'end', **dict(common, run_id='run-new'), physical_attempt_id='success',
                             started_at=100, ended_at=120, route='cloud', usage=unknown)
            before = usage.summarize(index, 'A', 100, 130, project=temp)
            self.assertEqual((2, 1, 1), (before['calls'], before['usage_calls'], before['error_calls']))
            self.assertEqual((1, 1), (before['logical_requests'], before['extra_physical_attempts']))
            self.assertEqual((1, 1, 0), (before['provider_dispatched_calls'], before['provider_not_dispatched_calls'], before['provider_dispatch_unknown_calls']))
            self.assertEqual(2, before['business_attributed_calls'])
            self.assertIsNone(before['routes']['cloud']['known_tokens']['cache_read_tokens'])
            self.assertEqual(43, before['routes']['cloud']['known_tokens']['total_tokens'])
            self.assertEqual(0, before['cache_coverage_ratio'])
            self.assertIsNone(before['known_cache_read_ratio'])
            after = usage.summarize(index, 'A', 130, 140, project=temp)
            self.assertEqual((1, 1), (after['calls'], after['cache_complete_calls']))
            self.assertEqual(0, after['known_cache_read_ratio'])
            self.assertEqual(0, after['routes']['local']['known_tokens']['cache_read_tokens'])

    def test_historical_zero_cache_is_unknown_without_rewriting_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            index = Path(temp)/'usage.db'
            usage.record(index, 'end', session_id='s', api_request_id='r', physical_attempt_id='p',
                role='A', project=temp, started_at=100, ended_at=110,
                usage={'input_tokens': 5, 'output_tokens': 2, 'cache_read_tokens': 0, 'cache_write_tokens': 0})
            with sqlite3.connect(index) as db:
                db.execute("UPDATE role_usage SET field_presence_json=NULL, normalization_version='legacy-presence-unknown'")
            original = index.read_bytes()
            report = usage.summarize(index, 'A', 100, 120, project=temp)
            self.assertIsNone(report['routes']['unknown']['known_tokens']['cache_read_tokens'])
            self.assertIsNone(report['known_cache_read_ratio'])
            self.assertEqual(0, report['cache_read_calls'])
            self.assertEqual(1, report['business_attribution_missing_calls'])
            self.assertEqual(original, index.read_bytes())

    def test_plugin_uses_actual_routes_and_only_bound_role_sessions_across_homes(self):
        spec = importlib.util.spec_from_file_location('usage_test_plugin', ROOT/'__init__.py')
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp:
            plugins = []
            for name in ('first', 'second'):
                home = Path(temp)/name
                project = home/'project'
                project.mkdir(parents=True)
                (project/'ROLE-BINDINGS.json').write_text(json.dumps({'schema_version': 1, 'project_root': str(project), 'control': 'ACTIVE',
                    'roles': {'A': {'chat_id': name, 'poll_job_id': name+'-poll', 'report_job_id': name+'-report'},
                              'COORDINATOR': {'chat_id': name+'-coord', 'morning_report_job_id': name+'-morning'}}}))
                (home/'registry.json').write_text(json.dumps({'projects': [{'project_root': str(project)}]}))
                (home/'config.yaml').write_text('''providers:
  local: {base_url: "http://local/v1"}
  cloud: {base_url: "https://cloud/v1"}
plugins:
  entries:
    role-workflow-runtime:
      settings:
        model_routing:
          local_provider: local
          local_model: same-model
          cloud_provider: custom:cloud
          cloud_model: same-model
''')
                with sqlite3.connect(home/'state.db') as db:
                    db.execute('CREATE TABLE sessions(id TEXT,chat_id TEXT,parent_session_id TEXT)')
                    db.executemany('INSERT INTO sessions VALUES(?,?,?)', [('human', name, None), ('compressed', None, 'human'),
                                                                         ('unrelated', 'other-chat', None)])
                plugins.append(module.RuntimePlugin(registry=home/'registry.json', home=home, metrics_url='unused'))
            for plugin in (plugins[0], plugins[1], plugins[0]):
                name = plugin.home.name
                for number, session in enumerate(('human', 'compressed', f'cron_{name}-poll_20261016_110000',
                                                  f'cron_{name}-report_20261016_110000')):
                    fields = dict(session_id=session, model='same-model', provider='custom', api_request_id='r'+str(number),
                                  physical_attempt_id='p'+str(number), started_at=100, ended_at=110,
                                  base_url='http://local/v1', usage={'input_tokens': 10, 'output_tokens': 2})
                    plugin.on_api_end(**fields)
                plugin.on_aux_end(session_id='compressed', model='same-model', provider='custom', api_request_id='aux',
                    physical_attempt_id='aux', started_at=100, ended_at=110, base_url='http://local/v1',
                    usage={'input_tokens': 7, 'output_tokens': 1}, aux_task='compression')
                plugin.on_api_end(session_id=f'cron_{name}-morning_20261016_080000', model='same-model', provider='custom',
                                  api_request_id='morning', physical_attempt_id='morning', started_at=100, ended_at=110,
                                  base_url='https://cloud/v1', usage={'input_tokens': 20, 'output_tokens': 3})
                plugin.on_api_end(session_id='unrelated', model='same-model', api_request_id='ignore',
                                  physical_attempt_id='ignore', started_at=100, ended_at=110)
            for plugin in plugins:
                index = plugin.home/'runtime/role-usage.db'
                report = usage.summarize(index, 'A', 90, 120, project=plugin.home/'project')
                self.assertEqual(5, report['routes']['local']['calls'])
                self.assertEqual(0, report['routes']['cloud']['calls'])
                coordinator = usage.summarize(index, 'COORDINATOR', 90, 120, project=plugin.home/'project')
                self.assertEqual(1, coordinator['routes']['cloud']['calls'])
                self.assertEqual({'role': 'A', 'project': str(plugin.home/'project')}, plugin.api_identity('compressed'))
                with sqlite3.connect(index) as db:
                    rows = {row[0]: row[1:] for row in db.execute(
                        'SELECT physical_attempt_id,parent_session_id,session_kind,task_kind FROM role_usage')}
                self.assertEqual(('', 'group_chat', 'group_chat'), rows['p0'])
                self.assertEqual(('human', 'group_chat', 'group_chat'), rows['p1'])
                self.assertEqual(('', 'business_cron', 'business_cron'), rows['p2'])
                self.assertEqual(('', 'summary', 'summary'), rows['p3'])
                self.assertEqual(('compressed', 'group_chat', 'aux:compression'), rows['aux'])
                self.assertEqual(('', 'summary', 'summary'), rows['morning'])


if __name__ == '__main__':
    unittest.main()
