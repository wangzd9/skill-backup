"""Original queue terminal receipts retain profile scope and a body digest only."""
import hashlib
from pathlib import Path
from types import SimpleNamespace


def test_original_gateway_queue_emits_terminal_hook_once_after_privacy_prune_per_profile(tmp_path, monkeypatch):
    from cron import delivery_queue as queue, scheduler
    from hermes_cli import plugins
    managers, sends, receipts = {}, [], []
    monkeypatch.setattr(queue, 'DELIVERY_DB', None)
    for profile in ('a', 'b'):
        home = tmp_path/profile
        home.mkdir()
        manager = plugins.PluginManager(scope_key=str(home))

        def receipt(*, job, execution_id, metadata, content_sha256, stage, home=home, **kwargs):
            status = queue.get_status(execution_id)
            assert status['status'] == 'delivered'
            assert status['job_json'] == '{}' and status['content'] == ''
            assert metadata['home'] == str(home)
            assert stage == 'native_queue_terminal'
            receipts.append((home, execution_id, content_sha256))

        manager._hooks['post_cron_delivery'] = [receipt]
        managers[home] = manager
    monkeypatch.setattr(plugins, '_delivery_manager', lambda: managers[Path(scheduler._get_hermes_home())])

    def send(job, content, **kwargs):
        sends.append((Path(scheduler._get_hermes_home()), job['execution_id']))
        job['_cron_delivery_content_sha256'] = hashlib.sha256(content.encode()).hexdigest()
        return None

    monkeypatch.setattr(scheduler, '_deliver_result', send)
    for profile in ('a', 'b', 'a'):
        home = tmp_path/profile
        monkeypatch.setenv('HERMES_HOME', str(home))
        execution = 'execution-'+profile
        queue.enqueue(execution, {'id': 'report', 'execution_id': execution,
            '_cron_result_hook_metadata': {'home': str(home)}}, '中文排版报告')
        scheduler.drain_delivery_queue({}, None)
    assert len(sends) == len(receipts) == 2
    assert all(row[2] == hashlib.sha256('中文排版报告'.encode()).hexdigest() for row in receipts)


def test_terminal_hook_is_not_called_when_original_queue_commit_is_not_owned(tmp_path, monkeypatch):
    from cron import delivery_queue as queue
    monkeypatch.setattr(queue, 'DELIVERY_DB', tmp_path/'queue.db')
    queue.enqueue('execution', {'id': 'report'}, 'body')
    monkeypatch.setattr(queue, '_finish', lambda *args, **kwargs: False)
    receipts = []
    assert queue.drain(lambda *args: None, on_terminal=lambda *args: receipts.append(args)) == 1
    assert receipts == []


def test_actual_delivery_hash_is_of_cleaned_send_text(tmp_path, monkeypatch):
    from cron import scheduler, scheduler_delivery as delivery, role_reporting_policy
    from gateway import config, media_policy
    from gateway.platforms.base import BasePlatformAdapter
    monkeypatch.delenv('_HERMES_CRON_EXTERNAL_WORKER', raising=False)
    monkeypatch.setattr(role_reporting_policy, 'suppress_role_delivery', lambda job: False)
    monkeypatch.setattr(role_reporting_policy, 'is_role_job', lambda job: True)
    monkeypatch.setattr(delivery, '_resolve_delivery_targets', lambda *args, **kwargs: [{'platform': 'feishu', 'chat_id': 'fixture'}])
    monkeypatch.setattr(delivery, '_redact_cron_payload', lambda text, what: text.replace('private-value', '[redacted]'))
    monkeypatch.setattr(delivery, '_prepare_target_delivery', lambda *args, **kwargs: SimpleNamespace(live_adapter_ready=True))
    monkeypatch.setattr(delivery, '_record_delivery_verification', lambda *args: None)
    monkeypatch.setattr(config, 'load_gateway_config', lambda: {})
    monkeypatch.setattr(scheduler, 'load_config', lambda: {})
    monkeypatch.setattr(media_policy, 'apply_media_policy_env', lambda config: None)
    monkeypatch.setattr(BasePlatformAdapter, 'extract_media', staticmethod(lambda text: ([], text)))
    monkeypatch.setattr(BasePlatformAdapter, 'filter_media_delivery_paths', staticmethod(lambda paths: paths))
    sent = []
    monkeypatch.setattr(delivery, '_deliver_via_live_adapter', lambda target, text, media, **kwargs: sent.append(text) or True)
    job = {'id': 'report'}
    assert delivery._deliver_result(job, '中文 private-value 报告', adapters={}, loop=None) is None
    assert sent == ['中文 [redacted] 报告']
    assert job['_cron_delivery_content_sha256'] == hashlib.sha256(sent[0].encode()).hexdigest()
    assert job['_cron_delivery_content_sha256'] != hashlib.sha256('中文 private-value 报告'.encode()).hexdigest()
