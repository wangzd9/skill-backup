"""Process-local authored text, kept separate from quotes and attachment bodies."""
from dataclasses import dataclass
from contextvars import ContextVar

_CURRENT_NATIVE_INPUT = ContextVar("hermes_native_input", default=None)


@dataclass(frozen=True)
class NativePrimaryInput:
    platform: str
    chat_id: str
    user_id: str
    message_id: str
    primary_text: str
    is_bot: bool = False


def bind_native_primary_input(event):
    envelope = native_primary_input(event)
    _CURRENT_NATIVE_INPUT.set(getattr(event, "_native_primary_input", None) if envelope else None)
    return envelope


def current_native_primary_input():
    return _CURRENT_NATIVE_INPUT.get()


def clear_native_primary_input():
    _CURRENT_NATIVE_INPUT.set(None)


def native_primary_input(event):
    """Return a fresh hook envelope only for the exact, unbatched platform event.

    This receipt is never reconstructed from wire metadata or prompt text. A text
    rewrite retains only the adapter's original authored text; batching, internal
    events and mismatched routing identities cannot grant authority.
    """
    value = getattr(event, "_native_primary_input", None)
    source = getattr(event, "source", None)
    if not isinstance(value, NativePrimaryInput) or source is None or getattr(event, "internal", False):
        return None
    platform = getattr(getattr(source, "platform", None), "value", None)
    if (not all((value.platform, value.chat_id, value.user_id, value.message_id))
            or value.platform != platform or value.chat_id != str(getattr(source, "chat_id", "") or "")
            or value.user_id != str(getattr(source, "user_id", "") or "")
            or value.message_id != str(getattr(event, "message_id", None) or "")
            or value.message_id != str(getattr(source, "message_id", None) or "")
            or value.is_bot != bool(getattr(source, "is_bot", False))):
        return None
    return {"schema_version": 1, "platform": value.platform, "chat_id": value.chat_id,
            "user_id": value.user_id, "message_id": value.message_id,
            "message_ids": [value.message_id], "primary_text": value.primary_text,
            "is_bot": value.is_bot}
