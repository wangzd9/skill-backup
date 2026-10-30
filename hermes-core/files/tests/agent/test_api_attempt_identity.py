"""Physical request identity survives paired hooks without changing cached messages."""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.api_request_hooks import ApiRequestHooksMixin
from agent.auxiliary_client import reset_runtime_main, set_runtime_main
from agent.auxiliary_hooks import run_with_aux_hooks
from agent.turn_api_request import build_api_request
from agent.turn_response_intake import _fire_post_api_request_hook
from hermes_cli import plugins as plugins_mod
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest


def _observer(monkeypatch):
    manager = PluginManager()
    monkeypatch.setattr(plugins_mod, '_plugin_manager', manager)
    monkeypatch.setattr(plugins_mod, '_plugin_managers_by_home', {})
    context = PluginContext(PluginManifest(name='physical-observer', source='user'), manager)
    fired = []
    for name in ('pre_api_request', 'post_api_request', 'api_request_error', 'pre_auxiliary_call', 'post_auxiliary_call'):
        context.register_hook(name, lambda _name=name, **metadata: fired.append((_name, metadata)))
    return fired


class Agent(ApiRequestHooksMixin, SimpleNamespace):
    pass


def test_retry_attempts_have_unique_paired_ids_and_leave_prefix_byte_stable(monkeypatch):
    fired = _observer(monkeypatch)
    messages = [{'role': 'system', 'content': 'stable prefix'}, {'role': 'user', 'content': 'task'}]
    original = json.dumps(messages)
    agent = Agent(session_id='role-session', platform='cron', model='model', provider='custom',
        base_url='https://provider/v1', api_mode='chat_completions', tools=[], max_tokens=128,
        _empty_content_retries=0, _is_user_initiated_turn=False, _cached_system_prompt='stable prefix',
        _image_rejecting_models=set(), _force_ascii_payload=False,
        _reset_stream_delivery_tracking=lambda: None, _reapply_reasoning_echo_for_provider=lambda _messages: None,
        _build_api_kwargs=lambda _messages: {'model': 'model', 'messages': _messages})
    monkeypatch.setattr('agent.request_routing.apply_model_route', lambda _agent: None)
    monkeypatch.setattr('agent.conversation_loop._redecorate_prompt_cache_for_provider',
        lambda _agent, api_messages, **_kw: (api_messages, None, []))
    for number in range(2):
        build_api_request(agent, api_messages=messages, _moa_prepared_request=None, tools_for_api=[],
            system_message=messages[0], messages=messages, original_user_message='task', approx_tokens=10,
            total_chars=20, retry_count=number, api_call_count=1, api_request_id='logical',
            api_start_time=100, effective_task_id='task', turn_id='turn')
        if number == 0:
            agent._invoke_api_request_error_hook(task_id='task', turn_id='turn', api_request_id='logical',
                api_call_count=1, api_start_time=100, api_kwargs={}, error_type='Transient', error_message='retry',
                retry_count=0, max_retries=2, retryable=True)
        else:
            response = SimpleNamespace(model='model', usage=SimpleNamespace(prompt_tokens=10, completion_tokens=3))
            _fire_post_api_request_hook(agent, response, SimpleNamespace(content='done', tool_calls=None), 'stop',
                api_messages=messages, api_call_count=1, api_duration=1, api_start_time=agent._current_api_attempt_started_at,
                api_request_id='logical', effective_task_id='task', turn_id='turn')
    assert [name for name, _ in fired] == ['pre_api_request', 'api_request_error', 'pre_api_request', 'post_api_request']
    first, error, second, completed = [row for _, row in fired]
    assert first['physical_attempt_id'] == error['physical_attempt_id']
    assert second['physical_attempt_id'] == completed['physical_attempt_id']
    assert first['physical_attempt_id'] != second['physical_attempt_id']
    assert all(row['api_request_id'] == 'logical' for _, row in fired)
    assert completed['usage']['total_tokens'] == 13
    assert json.dumps(messages) == original and agent._cached_system_prompt == 'stable prefix'


def test_auxiliary_attempts_follow_explicit_parent_context_without_global_fallback(monkeypatch):
    fired = _observer(monkeypatch)
    monkeypatch.setattr('agent.relay_runtime.current_turn', lambda: None)
    client = SimpleNamespace(base_url='https://provider/v1')
    response = SimpleNamespace(model='model', choices=[], usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1))
    for session in ('role-A', 'role-B', 'role-A'):
        token = set_runtime_main('custom', 'model', session_id=session)
        try:
            run_with_aux_hooks(lambda: response, aux_task='compression', metadata={'api_request_id': 'aux-logical'},
                client=client, kwargs={'messages': []}, provider='custom', model='model', api_mode='chat_completions')
        finally:
            reset_runtime_main(token)
    pre = [row for name, row in fired if name == 'pre_auxiliary_call']
    post = [row for name, row in fired if name == 'post_auxiliary_call']
    assert [row['session_id'] for row in pre] == ['role-A', 'role-B', 'role-A']
    assert len({row['physical_attempt_id'] for row in pre}) == 3
    assert [row['physical_attempt_id'] for row in pre] == [row['physical_attempt_id'] for row in post]
    from agent.auxiliary_hooks import _parent_turn_identity
    assert _parent_turn_identity()['session_id'] == ''  # process compatibility mirrors are deliberately ignored


def test_routing_failures_do_not_reuse_previous_physical_attempt(monkeypatch):
    fired = _observer(monkeypatch)
    agent = Agent(session_id='role-session', platform='cron', model='model', provider='custom',
        base_url='https://provider/v1', api_mode='chat_completions',
        _current_api_physical_attempt_id='previous-attempt', _current_api_attempt_started_at=1)

    def route_timeout(_agent):
        raise RuntimeError('Model routing policy timed out; original provider was not called.')

    monkeypatch.setattr('agent.request_routing.apply_model_route', route_timeout)
    for logical in ('next-request', 'another-request'):
        with pytest.raises(RuntimeError, match='routing policy timed out'):
            build_api_request(agent, api_messages=[], _moa_prepared_request=None, tools_for_api=[],
                system_message=None, messages=[], original_user_message='task', approx_tokens=10,
                total_chars=20, retry_count=0, api_call_count=1, api_request_id=logical,
                api_start_time=100, effective_task_id='task', turn_id='turn')
        agent._invoke_api_request_error_hook(task_id='task', turn_id='turn', api_request_id=logical,
            api_call_count=1, api_start_time=100, api_kwargs=None, error_type='RuntimeError',
            error_message='routing policy timed out', retry_count=0, max_retries=2, retryable=True)
    assert [name for name, _ in fired] == ['api_request_error', 'api_request_error']
    rows = [row for _, row in fired]
    assert [row['api_request_id'] for row in rows] == ['next-request', 'another-request']
    assert len({row['physical_attempt_id'] for row in rows} | {'previous-attempt'}) == 3
    assert all(row['attempt_started_at'] > 100 for row in rows)
    assert all(row['provider_dispatched'] is False for row in rows)


def test_main_and_aux_usage_preserve_missing_wire_fields_and_explicit_zero():
    from agent.auxiliary_hooks import _usage_summary
    from pydantic import BaseModel

    class Usage(BaseModel):
        prompt_tokens: int
        completion_tokens: int
        prompt_tokens_details: dict | None = None

    agent = Agent(provider='custom', api_mode='chat_completions')
    for wire in (Usage(prompt_tokens=50, completion_tokens=4),
                 {'prompt_tokens': 50, 'completion_tokens': 4,
                  'prompt_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 0}},
                 {'prompt_tokens': 50, 'completion_tokens': 4,
                  'prompt_cache_hit_tokens': 38}):
        response = SimpleNamespace(usage=wire)
        main = agent._usage_summary_for_api_request_hook(response)
        auxiliary = _usage_summary(response, provider='custom', api_mode='chat_completions')
        assert main == auxiliary
        assert main['prompt_tokens'] == 50 and main['total_tokens'] == 54
        assert main['normalization_version'] == 'hermes-canonical-v1'
        assert 'raw_usage' not in main
    missing = agent._usage_summary_for_api_request_hook(SimpleNamespace(usage=Usage(prompt_tokens=50, completion_tokens=4)))
    assert missing['field_presence']['cache_read_tokens'] is False
    assert missing['field_presence']['input_tokens'] is False
    assert missing['field_presence']['prompt_tokens'] is True
    explicit = _usage_summary({'usage': {'prompt_tokens': 50, 'completion_tokens': 4,
        'prompt_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 0}}},
        provider='custom', api_mode='chat_completions')
    assert explicit['field_presence']['cache_read_tokens'] is True
    assert explicit['cache_read_tokens'] == 0
