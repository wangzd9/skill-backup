"""Generic cron observers remain visible while admission/dispatch is paused."""
from contextlib import ExitStack
from unittest.mock import patch

import pytest


@pytest.mark.parametrize('gate', ['dispatch', 'estop'])
def test_tick_invokes_real_lifecycle_manager_before_pause_gate(tmp_path, gate):
    from cron import scheduler, scheduler_tick
    from hermes_cli import plugins
    calls = []
    manager = plugins.PluginManager(scope_key=str(tmp_path))
    def broken(**kwargs):
        calls.append(('broken', kwargs))
        raise RuntimeError('isolated observer fault')
    manager._hooks['on_scheduler_tick'] = [broken,
        lambda **kwargs: calls.append(('healthy', kwargs)) or {'action': 'dispatch'}]
    assert 'on_scheduler_tick' in plugins.VALID_HOOKS
    with ExitStack() as stack:
        stack.enter_context(patch.object(scheduler, '_should_yield_tick_to_fresh_gateway', return_value=None))
        stack.enter_context(patch.object(scheduler, '_get_lock_paths', return_value=(tmp_path, tmp_path/'tick.lock')))
        stack.enter_context(patch.object(scheduler, '_ensure_cron_dir'))
        stack.enter_context(patch.object(scheduler, '_acquire_tick_lock', return_value=17))
        released = stack.enter_context(patch.object(scheduler, '_release_tick_lock'))
        stack.enter_context(patch.object(scheduler, '_get_hermes_home', return_value=tmp_path))
        stack.enter_context(patch.object(plugins, '_delivery_manager', return_value=manager))
        stack.enter_context(patch('agent.estop.check_paused', return_value=gate == 'estop'))
        dispatch = stack.enter_context(patch.object(scheduler, 'get_due_jobs', side_effect=AssertionError('observer cannot grant dispatch')))
        assert scheduler_tick._tick_admitted(can_dispatch=lambda: False) == 0
    assert [name for name, _ in calls] == ['broken', 'healthy']
    assert all(kwargs['home'] == tmp_path and set(kwargs) <= {'home', 'telemetry_schema_version', 'hook_contract_version'}
               for _, kwargs in calls)
    released.assert_called_once_with(17)
    dispatch.assert_not_called()


def test_tick_lock_contender_does_not_duplicate_observer(tmp_path):
    from cron import scheduler, scheduler_tick
    with patch.object(scheduler, '_should_yield_tick_to_fresh_gateway', return_value=None), \
         patch.object(scheduler, '_get_lock_paths', return_value=(tmp_path, tmp_path/'tick.lock')), \
         patch.object(scheduler, '_ensure_cron_dir'), \
         patch.object(scheduler, '_acquire_tick_lock', return_value=None), \
         patch('hermes_cli.lifecycle.invoke_hook') as hook:
        assert scheduler_tick._tick_admitted() == 0
    hook.assert_not_called()


def test_slow_tick_callback_is_bounded_and_profile_isolated_even_if_timeout_disabled(tmp_path):
    import threading
    import time
    from hermes_cli import plugins
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override, get_hermes_home
    manager = plugins.PluginManager(scope_key=str(tmp_path))
    release = threading.Event()
    entered = threading.Event()
    homes = []
    a, b = tmp_path/'a', tmp_path/'b'
    def observer(home, **kwargs):
        homes.append((home, get_hermes_home()))
        if home == a:
            entered.set()
            release.wait(10)
    manager._hooks['on_scheduler_tick'] = [observer]
    try:
        with patch.object(plugins, '_resolve_hook_callback_timeout', return_value=0):
            token = set_hermes_home_override(a)
            try:
                started = time.monotonic()
                manager.invoke_hook('on_scheduler_tick',home=a,hook_contract_version=1)
                assert time.monotonic()-started < 3
                assert entered.is_set()
                manager.invoke_hook('on_scheduler_tick',home=a,hook_contract_version=1)
            finally:
                reset_hermes_home_override(token)
            token = set_hermes_home_override(b)
            try:
                manager.invoke_hook('on_scheduler_tick',home=b,hook_contract_version=1)
            finally:
                reset_hermes_home_override(token)
            assert homes == [(a,a),(b,b)]
    finally:
        release.set()
