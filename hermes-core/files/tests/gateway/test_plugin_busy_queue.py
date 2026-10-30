"""Independent-turn plugin requests keep native identity through the busy FIFO."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.native_turn_input import NativePrimaryInput, native_primary_input
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.session_context import reset_session_vars


def event(message_id, text, *, kind=MessageType.TEXT):
    source = SessionSource(platform=Platform.FEISHU, chat_id="bound-role", user_id="user",
                           chat_type="group", message_id=message_id)
    value = MessageEvent(text=text, source=source, message_id=message_id, message_type=kind)
    value._native_primary_input = NativePrimaryInput("feishu", "bound-role", "user", message_id, text)
    return value


def runner_fixture(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    runner = GatewayRunner(config=GatewayConfig())
    adapter = SimpleNamespace(_pending_messages={})
    runner._delivery_adapter_for = lambda source: adapter
    runner._is_user_authorized_for_source = lambda source: True
    runner._admit_bot_message_for_source = lambda source: True
    runner._send_busy_ack_reply = AsyncMock()
    runner._resolve_busy_steer_or_redirect = AsyncMock()
    running = Mock()
    runner._session_state("role-key").turn.agent = running
    return runner, adapter, running


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["steer", "interrupt"])
@pytest.mark.parametrize("entry", ["adapter_busy", "admitted_priority"])
async def test_queue_directive_preserves_each_native_event_and_never_injects(mode, entry, tmp_path, monkeypatch):
    runner, adapter, running = runner_fixture(tmp_path, monkeypatch)
    runner._busy_input_mode = mode
    runner._hm_busy_slash_or_photo = AsyncMock(return_value=(False, None))
    seen = []

    async def hook(name, **kw):
        seen.append((name, kw["event"]))
        return [{"action": "queue", "message": "已排队，下回合独立处理。"}]

    monkeypatch.setattr("hermes_cli.lifecycle.ainvoke_hook", hook)
    # A preceding photo must not merge with a grant or revoke its native receipt.
    photo = event("photo", "attachment", kind=MessageType.PHOTO)
    adapter._pending_messages["role-key"] = photo
    first = event("grant", "全部权限，执行当前任务")
    second = event("question", "当前进展如何？")
    for value in (first, second):
        if entry == "adapter_busy":
            assert await runner._handle_active_session_busy_message(value, "role-key")
        else:
            assert await runner._hm_handle_running_session_message(value, value.source, "role-key") is None
        assert value._gateway_accepted
        assert native_primary_input(value)["primary_text"] == value.text
    assert [name for name, _ in seen] == ["pre_gateway_busy_dispatch"] * 2
    assert adapter._pending_messages["role-key"] is photo
    assert runner._overflow_queue("role-key") == [first, second]
    assert runner._overflow_queue("role-key")[0] is first
    assert runner._promote_queued_event("role-key", adapter, None) is first
    assert runner._promote_queued_event("role-key", adapter, None) is second
    runner._resolve_busy_steer_or_redirect.assert_not_awaited()
    running.steer.assert_not_called()
    running.interrupt.assert_not_called()


@pytest.mark.asyncio
async def test_full_queue_does_not_fall_back_to_steering_or_claim_admission(tmp_path, monkeypatch):
    runner, adapter, running = runner_fixture(tmp_path, monkeypatch)
    runner._BUSY_QUEUE_MAX_PENDING = 1
    previous = event("previous", "old message")
    adapter._pending_messages["role-key"] = previous
    monkeypatch.setattr("hermes_cli.lifecycle.ainvoke_hook", AsyncMock(return_value=[{
        "action": "queue", "queue_full_message": "待处理消息已满，本条尚未入队。"}]))
    value = event("grant", "全部权限：执行当前任务")
    assert await runner._handle_active_session_busy_message(value, "role-key")
    assert not value._gateway_accepted
    assert adapter._pending_messages["role-key"] is previous
    assert not runner._overflow_queue("role-key")
    runner._resolve_busy_steer_or_redirect.assert_not_awaited()
    running.interrupt.assert_not_called()
    assert "尚未入队" in runner._send_busy_ack_reply.await_args.args[2]


@pytest.mark.asyncio
async def test_native_authorization_precedes_plugin_queue_directive(tmp_path, monkeypatch):
    runner, adapter, running = runner_fixture(tmp_path, monkeypatch)
    runner._is_user_authorized_for_source = lambda source: False
    hook = AsyncMock(return_value=[{"action": "queue"}])
    monkeypatch.setattr("hermes_cli.lifecycle.ainvoke_hook", hook)
    value = event("untrusted", "全部权限：执行当前任务")
    assert await runner._handle_active_session_busy_message(value, "role-key")
    hook.assert_not_awaited()
    assert not adapter._pending_messages
    assert not value._gateway_accepted


@pytest.mark.asyncio
async def test_control_skip_takes_precedence_over_queue(tmp_path, monkeypatch):
    from gateway.plugin_busy_control import handle_plugin_busy_control
    runner, adapter, _ = runner_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr("hermes_cli.lifecycle.ainvoke_hook", AsyncMock(return_value=[
        {"action": "queue"}, {"action": "skip"}]))
    value = event("stop", "/stop")
    assert await handle_plugin_busy_control(runner, value, session_key="role-key")
    assert not adapter._pending_messages
    assert not value._gateway_accepted


@pytest.mark.asyncio
async def test_no_queue_directive_leaves_existing_busy_routing_unchanged(tmp_path, monkeypatch):
    from gateway.plugin_busy_control import handle_plugin_busy_control
    runner, adapter, _ = runner_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr("hermes_cli.lifecycle.ainvoke_hook", AsyncMock(return_value=[None, {"action": "unknown"}]))
    assert not await handle_plugin_busy_control(runner, event("plain", "hello"), session_key="role-key")
    assert not adapter._pending_messages


@pytest.mark.asyncio
@pytest.mark.parametrize('adapter_available',[False,True])
async def test_missing_fifo_fails_closed_without_steering_or_false_acceptance(tmp_path,monkeypatch,adapter_available):
    from gateway.plugin_busy_control import handle_plugin_busy_control
    runner,adapter,running=runner_fixture(tmp_path,monkeypatch)
    if adapter_available:
        del adapter._pending_messages
    else:
        runner._delivery_adapter_for=lambda source:None
    monkeypatch.setattr('hermes_cli.lifecycle.ainvoke_hook',AsyncMock(return_value=[{
        'action':'queue','message':'已排队','queue_unavailable_message':'投递通道不可用，本次未执行。'}]))
    value=event('grant','全部权限：执行当前任务')
    assert await handle_plugin_busy_control(runner,value,session_key='role-key')
    assert not value._gateway_accepted
    runner._resolve_busy_steer_or_redirect.assert_not_awaited()
    running.steer.assert_not_called()
    running.interrupt.assert_not_called()
    if adapter_available:
        assert '未执行' in runner._send_busy_ack_reply.await_args.args[2]
    else:
        runner._send_busy_ack_reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_busy_grant_reaches_its_own_native_turn_without_rebinding_old_turn(tmp_path, monkeypatch):
    from agent.turn_input import capture_turn_input
    runner, adapter, running = runner_fixture(tmp_path, monkeypatch)
    reset_session_vars()
    ingress = []

    async def hook(name, **kw):
        if name == "pre_gateway_busy_dispatch":
            return [{"action": "queue"}]
        ingress.append(kw["native_primary_input"])
        return []

    monkeypatch.setattr("hermes_cli.lifecycle.ainvoke_hook", hook)
    agent = SimpleNamespace(platform="feishu")
    old = event("old-query", "当前进展如何？")
    await runner._hm_pre_gateway_dispatch_hook(old, old.source)
    tokens = runner._set_session_env(SimpleNamespace(source=old.source, session_key="role-key"))
    try:
        old_turn = capture_turn_input(agent, old.message_id)
        assert old_turn["trusted_gateway"]
        grant = event("new-grant", "全部权限：执行当前任务")
        assert await runner._handle_active_session_busy_message(grant, "role-key")
        assert old_turn["message_id"] == "old-query"
        assert not capture_turn_input(agent, grant.message_id)["trusted_gateway"]
    finally:
        runner._clear_session_env(tokens)
    queued = adapter._pending_messages.pop("role-key")
    assert queued is grant
    await runner._hm_pre_gateway_dispatch_hook(queued, queued.source)
    tokens = runner._set_session_env(SimpleNamespace(source=queued.source, session_key="role-key"))
    try:
        new_turn = capture_turn_input(agent, queued.message_id)
        assert new_turn["trusted_gateway"]
        assert new_turn["message_id"] == "new-grant"
        assert ingress[0]["primary_text"] == old.text
        assert ingress[1]["primary_text"] == grant.text
        assert ingress[0]["message_id"] != ingress[1]["message_id"]
        assert old_turn["message_id"] == "old-query"
    finally:
        runner._clear_session_env(tokens)
        reset_session_vars()
    running.steer.assert_not_called()
    running.interrupt.assert_not_called()
