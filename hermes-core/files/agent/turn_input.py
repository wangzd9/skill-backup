"""Native turn provenance for policy hooks, never inferred from rendered messages."""
from types import MappingProxyType


def capture_turn_input(agent, platform_message_id):
    from gateway.session_context import get_bound_session_env
    from gateway.native_turn_input import current_native_primary_input

    platform = str(getattr(agent, "platform", "") or "")
    bound_platform = get_bound_session_env("HERMES_SESSION_PLATFORM")
    message_id = str(platform_message_id or "")
    chat_id = get_bound_session_env("HERMES_SESSION_CHAT_ID")
    user_id = get_bound_session_env("HERMES_SESSION_USER_ID")
    cron = get_bound_session_env("HERMES_CRON_SESSION") == "1"
    primary = current_native_primary_input()
    trusted = bool(not cron and message_id and chat_id and user_id
                   and primary is not None and primary.platform == platform
                   and primary.chat_id == chat_id and primary.user_id == user_id
                   and primary.message_id == message_id
                   and bound_platform == platform
                   and get_bound_session_env("HERMES_SESSION_MESSAGE_ID") == message_id)
    return MappingProxyType({"schema_version": 1, "platform": platform,
                             "chat_id": chat_id, "user_id": user_id, "message_id": message_id,
                             "session_kind": "cron" if cron else "gateway" if trusted else "unknown",
                             "trusted_gateway": trusted})


def turn_input_for_hooks(agent):
    value = getattr(agent, "_current_trusted_turn_input", None)
    return dict(value) if value is not None else {}
