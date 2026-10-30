"""Native cron output adapters retain one saver/channel and profile-scoped receipts."""
from pathlib import Path

import pytest


@pytest.mark.parametrize('delivery', ['verified', 'unverified', 'queued', 'suppressed'])
def test_real_run_and_save_emit_transformed_result_once_per_profile(tmp_path, monkeypatch, delivery):
    from cron import executions, jobs, scheduler
    from hermes_cli import plugins

    class OfflineAgent:
        model = 'offline-fixture'
        provider = 'fixture'
        _current_api_request_id = 'actual-offline-request'
        def __init__(self, session_id):
            self.session_id = session_id
        def run_conversation(self, prompt, **kwargs):
            assert 'read-only native fixture' in prompt
            return {'completed': True, 'final_response': '{"analysis":"原始模型JSON"}'}
        def close(self):
            pass

    monkeypatch.setattr(scheduler, '_launch_external_cron_worker', lambda _job: False)
    monkeypatch.setattr(scheduler, '_resolve_cron_agent_setup', lambda *_args:
        scheduler._CronAgentSetup(model='offline-fixture', runtime={}))
    monkeypatch.setattr(scheduler, '_construct_cron_agent', lambda *args, **kwargs:
        OfflineAgent(kwargs['session_id']))
    monkeypatch.setattr(scheduler, '_open_cron_session_db', lambda _job: None)
    delivered, outcomes, inputs = [], [], []
    managers = {}
    for profile in ('a', 'b'):
        home = tmp_path/profile
        home.mkdir()
        (home/'config.yaml').write_text('model: offline-fixture\n')
        manager = plugins.PluginManager(scope_key=str(home))
        def transform(*, job, execution_id, agent_identity, content, final_response, success, home=home):
            inputs.append((home, execution_id, agent_identity))
            assert list((home/'cron/output'/job['id']).glob('*.md'))
            assert content == final_response == '{"analysis":"原始模型JSON"}'
            return {'content': '程序排版的中文报告', 'suppress': delivery == 'suppressed',
                    'metadata': {'home': str(home), 'execution_id': execution_id}}
        manager._hooks['transform_cron_result'] = [transform]
        manager._hooks['post_cron_delivery'] = [lambda **kwargs: outcomes.append(kwargs)]
        managers[home] = manager

    def channel(job, content, **kwargs):
        delivered.append((Path(scheduler._get_hermes_home()), content))
        if delivery == 'unverified':
            job['last_delivery_unverified'] = ['fixture:chat']
        if delivery == 'queued':
            job['last_delivery_queued'] = ['fixture:chat']
        return None
    monkeypatch.setattr(scheduler, '_deliver_result', channel)
    for profile in ('a', 'b', 'a'):
        home = tmp_path/profile
        monkeypatch.setenv('HERMES_HOME', str(home))
        monkeypatch.setattr(executions, 'EXECUTIONS_FILE', home/'cron/executions.db')
        monkeypatch.setattr(plugins, '_delivery_manager', lambda home=home: managers[home])
        with jobs.use_cron_store(home):
            job = jobs.create_job(prompt='read-only native fixture', schedule='every 1h', deliver='telegram:fixture-chat')
            assert scheduler.run_one_job(job)
            row = executions.get_execution(job['execution_id'])
            assert row['status'] == 'completed'
            assert outcomes[-1]['execution_id'] == row['id']
            assert outcomes[-1]['metadata']['home'] == str(home)
            assert inputs[-1][2]['model_request_id'] == 'actual-offline-request'
            assert inputs[-1][2]['session_id'].startswith('cron_')
    assert len(inputs) == len(outcomes) == 3
    assert len(delivered) == (0 if delivery == 'suppressed' else 3)
    assert all(content == '程序排版的中文报告' for _, content in delivered)
    assert all(row['outcome'] == {'queued': 'queued', 'suppressed': 'suppressed'}.get(delivery, 'delivered')
               for row in outcomes)
    assert all(bool(row['unverified_targets']) == (delivery == 'unverified') for row in outcomes)
