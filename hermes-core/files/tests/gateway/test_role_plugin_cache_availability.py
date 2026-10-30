"""The existing next-turn path discovers newly registered role tools without reset."""

from copy import deepcopy
import json
import threading
from unittest.mock import Mock

from gateway.config import Platform
from gateway.session import SessionSource
from gateway.turn_context import TurnContext


def test_default_feishu_next_turn_gets_role_tools_and_preserves_history(monkeypatch):
    from gateway.run import GatewayRunner
    from gateway.run_turn_runner import TurnRunner
    from hermes_cli import plugins
    from model_tools import _dispatch_bridge_tool, get_tool_definitions

    manager = plugins.PluginManager()
    manager._discovered = True
    monkeypatch.setattr(plugins, '_plugin_manager', manager)
    monkeypatch.setattr(plugins, '_plugin_managers_by_home', {})
    monkeypatch.setattr(plugins, '_background_discovery_thread', None)
    plugin = plugins.PluginContext(plugins.PluginManifest(name='role-workflow-test', source='user'), manager)

    class SchemaOnlyAgent:
        """Exercise real schema assembly without constructing or calling a model client."""

        def __init__(self, **kwargs):
            self.session_id = kwargs['session_id']
            self.tools = get_tool_definitions(kwargs['enabled_toolsets'], kwargs['disabled_toolsets'], quiet_mode=True)
            self.max_iterations = kwargs['max_iterations']
            self.interrupt = Mock()

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    runner._session_db = None
    runner._prefill_messages = None
    runner._service_tier = None
    runner._delivery_adapter_for = lambda source: None
    runner._refresh_fallback_model = lambda: None
    runner._apply_fallback_chain_to_agent = lambda *args: None
    runner._init_cached_agent_for_turn = lambda *args: None
    runner._enforce_agent_cache_cap = lambda: None

    # The deployed Feishu config has neither a saved platform selection nor
    # known_plugin_toolsets. Use exactly those default-selection semantics.
    config = {}
    source = SessionSource(platform=Platform.FEISHU, chat_id='isolated-role-chat', user_id='isolated-user')
    before, disabled = runner._resolve_turn_toolsets(config, source, 'feishu')
    history = [{'role': 'user', 'content': 'existing task'},
               {'role': 'assistant', 'content': 'existing checkpoint'}]
    original_history = deepcopy(history)
    ctx = TurnContext(source=source, session_id='existing-session', session_key='existing-role-key',
                      user_config=config, enabled_toolsets=before, disabled_toolsets=disabled,
                      AIAgent=SchemaOnlyAgent, history=history)
    turn = TurnRunner(runner, ctx)
    route = {'model': 'offline-no-client', 'runtime': {}}
    first, reused = turn._resolve_turn_agent(route, 'feishu', '', 5, None, {})
    assert not reused
    again, reused = turn._resolve_turn_agent(route, 'feishu', '', 5, None, {})
    assert reused and again is first
    assert 'role_workflow' not in before
    assert not {'role_live_progress', 'role_manual_lease'} & {t['function']['name'] for t in first.tools}
    before_keys = runner._extract_cache_busting_config(config)
    unrelated = object()
    runner._agent_cache['unrelated-session'] = unrelated

    def never_execute(*args, **kwargs):
        raise AssertionError('The availability check must not execute a role tool')

    registrations = []
    try:
        for name in ('role_live_progress', 'role_manual_lease'):
            registrations.append(plugin.register_tool(name, 'role_workflow', {
                'name': name, 'description': 'Isolated availability check',
                'parameters': {'type': 'object', 'properties': {}}}, never_execute))
        after, disabled = runner._resolve_turn_toolsets(config, source, 'feishu')
        assert set(after) - set(before) == {'role_workflow'}

        # New toolset membership alone changes the existing signature, even on
        # an older loaded runtime that did not include registry generation.
        stable_keys = {k: v for k, v in before_keys.items() if k != 'tools.registry_generation'}
        signature = runner._agent_config_signature
        assert signature(route['model'], {}, before, '', cache_keys=stable_keys) != signature(
            route['model'], {}, after, '', cache_keys=stable_keys)

        ctx.enabled_toolsets, ctx.disabled_toolsets = after, disabled
        refreshed, reused = turn._resolve_turn_agent(route, 'feishu', '', 5, None, {})
        assert not reused and refreshed is not first
        role_names = {'role_live_progress', 'role_manual_lease'}
        raw_defs = get_tool_definitions(after, disabled, quiet_mode=True, skip_tool_search_assembly=True)
        assert role_names <= {t['function']['name'] for t in raw_defs}
        visible_names = {t['function']['name'] for t in refreshed.tools}
        if not role_names <= visible_names:
            # Default progressive disclosure can expose plugin tools through
            # the native search bridge instead of two eager schemas.
            assert {'tool_search', 'tool_describe', 'tool_call'} <= visible_names
            described, pending = _dispatch_bridge_tool('tool_describe', {'names': sorted(role_names)}, after, disabled)
            assert pending is None
            assert role_names <= set(json.loads(described)['tools'])
            assert _dispatch_bridge_tool('tool_call', {'name': 'role_live_progress', 'arguments': {}},
                                         after, disabled) == (None, ('role_live_progress', {}))
        recovered, _, _ = turn._load_turn_history(refreshed, reused)
        assert [(row['role'], row['content']) for row in recovered] == [
            (row['role'], row['content']) for row in original_history]
        assert history == original_history
        assert refreshed.session_id == first.session_id == ctx.session_id == 'existing-session'
        assert runner._agent_cache['unrelated-session'] is unrelated
        first.interrupt.assert_not_called()
    finally:
        for registration in registrations:
            if registration is not None:
                registration.dispose()
