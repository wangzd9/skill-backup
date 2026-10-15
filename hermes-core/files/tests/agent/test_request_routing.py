from types import SimpleNamespace
from unittest.mock import Mock, patch

import unittest

from agent.request_routing import apply_model_route


def agent():
    a = SimpleNamespace(session_id='role', platform='feishu', model='local', provider='custom:local',
                        base_url='http://localhost/v1', api_mode='chat_completions',
                        api_key='local-key', _cached_system_prompt='stable prefix', _fallback_chain=[])
    def switch_model(**kwargs):
        a.model=kwargs['new_model']; a.provider=kwargs['new_provider']
        a.base_url=kwargs['base_url'];a.api_mode=kwargs['api_mode'];a.api_key=kwargs['api_key']
        a._cached_system_prompt=None
    a.switch_model=Mock(side_effect=switch_model)
    return a


def test_route_switch_preserves_prompt_and_rechecks_next_request():
    a=agent()
    runtime={'model':'cloud','provider':'custom:cloud','base_url':'https://cloud/v1',
             'api_mode':'codex_responses','api_key':'cloud-key','request_overrides':{'extra_body':{'store':False}}}
    with patch('hermes_cli.plugins.has_hook',return_value=True), \
         patch('hermes_cli.plugins.invoke_hook',return_value=[{'runtime':runtime}]):
        apply_model_route(a)
        apply_model_route(a)
    a.switch_model.assert_called_once()
    assert a.model=='cloud'
    assert a._cached_system_prompt=='stable prefix'
    assert a.request_overrides=={'extra_body':{'store':False}}


def test_block_does_not_call_original_provider():
    a=agent()
    with patch('hermes_cli.plugins.has_hook',return_value=True), \
         patch('hermes_cli.plugins.invoke_hook',return_value=[{'error':'protected'}]):
        with unittest.TestCase().assertRaisesRegex(RuntimeError,'protected'): apply_model_route(a)
    a.switch_model.assert_not_called()


def test_no_policy_leaves_original_route_untouched():
    a=agent()
    with patch('hermes_cli.plugins.has_hook',return_value=False): apply_model_route(a)
    a.switch_model.assert_not_called()


def test_later_policy_veto_wins_over_a_valid_route():
    a = agent()
    runtime = {'model': 'cloud', 'provider': 'custom:cloud', 'base_url': 'https://cloud/v1',
               'api_mode': 'chat_completions', 'api_key': 'cloud-key'}
    with patch('hermes_cli.plugins.has_hook', return_value=True), \
         patch('hermes_cli.plugins.invoke_hook', return_value=[{'runtime': runtime}, {'action': 'block'}]):
        with unittest.TestCase().assertRaises(RuntimeError):
            apply_model_route(a)
    a.switch_model.assert_not_called()


def test_malformed_route_never_falls_back_to_original_provider():
    for result in ({'runtime': None}, {'runtime': {'model': 'cloud'}}):
        a = agent()
        with patch('hermes_cli.plugins.has_hook', return_value=True), \
             patch('hermes_cli.plugins.invoke_hook', return_value=[result]):
            with unittest.TestCase().assertRaises(RuntimeError):
                apply_model_route(a)
        a.switch_model.assert_not_called()


def test_policy_callback_exception_fails_closed():
    from hermes_cli.plugins import PluginManager
    manager=PluginManager()
    def broken(**kwargs): raise ValueError('broken policy')
    manager._hooks['resolve_model_route']=[broken]
    with patch('hermes_cli.plugins._resolve_hook_callback_timeout',return_value=0):
        results=manager.invoke_hook('resolve_model_route',session_id='role')
    assert results[0]['action']=='block'
    a=agent()
    with patch('hermes_cli.plugins.has_hook',return_value=True), \
         patch('hermes_cli.plugins.invoke_hook',return_value=results):
        with unittest.TestCase().assertRaises(RuntimeError): apply_model_route(a)
    a.switch_model.assert_not_called()


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(unittest.FunctionTestCase(fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn))
