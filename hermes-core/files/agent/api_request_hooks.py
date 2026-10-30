"""Lifecycle-hook payloads for ``AIAgent`` API requests.

JSON-safe coercion, secret-key redaction, size caps, and the ``api_request_error`` hook dispatch.
Extracted from ``run_agent.py``; every method resolves through ``AIAgent``'s MRO unchanged.
"""
import json
import os
import time
from contextlib import suppress
from types import SimpleNamespace
from typing import Any, Dict, Optional

from agent.usage_pricing import normalize_usage

_SENSITIVE_HOOK_KEYS = {"api_key", "authorization", "proxy_authorization", "cookie", "set_cookie"}
USAGE_NORMALIZATION_VERSION = "hermes-canonical-v1"


def usage_summary_for_hook(raw_usage: Any, *, provider: str, api_mode: str) -> Optional[Dict[str, Any]]:
    """Canonical buckets plus wire-field presence; absent cache is not a cache miss.

    Only booleans and normalized counts leave this function. Pydantic SDK defaults
    are excluded so an absent optional field is distinguishable from an explicit 0.
    """
    if raw_usage is None:
        return None
    from dataclasses import asdict
    from agent.usage_pricing import _ANTHROPIC_USAGE_SHAPE, _CODEX_USAGE_SHAPE, _CHAT_USAGE_SHAPE

    if hasattr(raw_usage, "model_dump"):
        try:
            reported = raw_usage.model_dump(exclude_unset=True)
        except TypeError:
            reported = _model_dump(raw_usage)
    else:
        reported = raw_usage

    def path_present(path):
        value = reported
        for name in path:
            value = value.get(name) if isinstance(value, dict) else getattr(value, name, None)
            if value is None:
                return False
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    anthropic = api_mode == "anthropic_messages" or provider == "anthropic"
    shape = _ANTHROPIC_USAGE_SHAPE if anthropic else _CODEX_USAGE_SHAPE if api_mode == "codex_responses" else _CHAT_USAGE_SHAPE
    prompt, output, read, write = [any(path_present(path) for path in paths) for paths in shape]
    cu = normalize_usage(raw_usage, provider=provider, api_mode=api_mode)
    summary = asdict(cu)
    summary.pop("raw_usage", None)
    summary.update(prompt_tokens=cu.prompt_tokens, total_tokens=cu.total_tokens,
                   normalization_version=USAGE_NORMALIZATION_VERSION,
                   field_presence={"input_tokens": prompt if anthropic else prompt and read and write,
                       "output_tokens": output, "cache_read_tokens": read, "cache_write_tokens": write,
                       "reasoning_tokens": any(path_present(path) for path in (
                           ("output_tokens_details", "reasoning_tokens"), ("completion_tokens_details", "reasoning_tokens"))),
                       "prompt_tokens": prompt and read and write if anthropic else prompt,
                       "total_tokens": prompt and output and (read and write if anthropic else True)})
    return summary


def _model_dump(value: Any) -> Any:
    """``value.model_dump(mode="json")`` with graceful degradation for older pydantic signatures.

    warnings=False: pydantic UserWarnings on generic-union SDK models would leak to the terminal.
    """
    try:
        return value.model_dump(mode="json", warnings=False)
    except TypeError:
        try:
            return value.model_dump(mode="json")
        except TypeError:
            return value.model_dump()


class ApiRequestHooksMixin:
    """Hook payload sanitising + ``api_request_error`` dispatch (see module docstring)."""

    def _usage_summary_for_api_request_hook(self, response: Any) -> Optional[Dict[str, Any]]:
        """Token buckets for ``post_api_request`` plugins (no raw ``response`` object)."""
        if response is None:
            return None
        raw_usage = response.get("usage") if isinstance(response, dict) else getattr(response, "usage", None)
        return usage_summary_for_hook(raw_usage, provider=self.provider, api_mode=self.api_mode)

    @staticmethod
    def _hook_payload_max_chars() -> int:
        raw = os.getenv("HERMES_PLUGIN_PAYLOAD_MAX_CHARS", "50000")
        try:
            return max(1000, int(raw))
        except (TypeError, ValueError):
            return 50000

    @staticmethod
    def _is_sensitive_hook_key(key: Any) -> bool:
        if not isinstance(key, str):
            return False
        lowered = key.lower().replace("-", "_")
        return lowered in _SENSITIVE_HOOK_KEYS or lowered.endswith("_api_key")

    @classmethod
    def _hook_jsonable(
        cls, value: Any, *, depth: int = 0, max_depth: int = 8, max_string: int = 8000,
        max_sequence: int = 200,
    ) -> Any:
        if depth > max_depth:
            return f"<{type(value).__name__} depth limit>"
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            if len(value) > max_string:
                return value[:max_string] + f"...[truncated {len(value) - max_string} chars]"
            return value
        if isinstance(value, (bytes, bytearray)):
            return f"<{len(value)} bytes>"

        def recurse(item):
            return cls._hook_jsonable(
                item, depth=depth + 1, max_depth=max_depth, max_string=max_string,
                max_sequence=max_sequence,
            )

        if isinstance(value, dict):
            out: Dict[str, Any] = {}
            for idx, (key, item) in enumerate(value.items()):
                if idx >= max_sequence:
                    out["_truncated_items"] = len(value) - max_sequence
                    break
                str_key = str(key)
                out[str_key] = "<redacted>" if cls._is_sensitive_hook_key(str_key) else recurse(item)
            return out
        if isinstance(value, (list, tuple, set)):
            seq = list(value)
            out = [recurse(item) for item in seq[:max_sequence]]
            if len(seq) > max_sequence:
                out.append({"_truncated_items": len(seq) - max_sequence})
            return out
        with suppress(Exception):
            if hasattr(value, "model_dump"):
                return recurse(_model_dump(value))
        with suppress(Exception):
            from dataclasses import asdict, is_dataclass
            if is_dataclass(value):
                return recurse(asdict(value))
        if isinstance(value, SimpleNamespace):
            return recurse(vars(value))
        if hasattr(value, "__dict__"):
            with suppress(Exception):
                return recurse({k: v for k, v in vars(value).items() if not str(k).startswith("_")})
        return str(value)[:max_string]

    @classmethod
    def _sanitize_hook_payload(cls, value: Any) -> Any:
        """JSON-able payload under the size cap: full → reduced caps → truncated preview."""
        limit = cls._hook_payload_max_chars()
        encoded = ""
        for caps in ({}, {"max_string": 1000, "max_sequence": 50}):
            payload = cls._hook_jsonable(value, **caps)
            try:
                encoded = json.dumps(payload, ensure_ascii=False, default=str)
            except Exception:
                return str(payload)[:limit]
            if len(encoded) <= limit:
                return payload
        return {
            "_truncated": True, "original_type": type(value).__name__, "preview": encoded[:limit]
        }

    def _api_request_payload_for_hook(self, api_kwargs: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        body = {
            key: value
            for key, value in (api_kwargs or {}).items()
            if key not in {"timeout", "http_client"}
        }
        return self._sanitize_hook_payload({"method": "POST", "body": body})

    def _api_response_payload_for_hook(
        self, response: Any, assistant_message: Any, *, finish_reason: Optional[str]
    ) -> Dict[str, Any]:
        # Raw provider SDK tool_call objects are handed to the sanitizer on purpose; `_hook_jsonable` must
        # keep normalising them (model_dump / __dict__ / dataclass) or subscribers get str() blobs.
        tool_calls = getattr(assistant_message, "tool_calls", None) or []
        return self._sanitize_hook_payload(
            {
                "model": getattr(response, "model", None),
                "finish_reason": finish_reason,
                "assistant_message": {
                    "role": getattr(assistant_message, "role", "assistant"),
                    "content": getattr(assistant_message, "content", None),
                    "tool_calls": tool_calls,
                },
                "usage": self._usage_summary_for_api_request_hook(response),
            }
        )

    def _invoke_api_request_error_hook(
        self, *, task_id: str, turn_id: str, api_request_id: str, api_call_count: int,
        api_start_time: float, api_kwargs: Optional[Dict[str, Any]], error_type: str,
        error_message: str, status_code: Optional[int] = None, retry_count: Optional[int] = None,
        max_retries: Optional[int] = None, retryable: Optional[bool] = None,
        reason: Optional[str] = None,
    ) -> None:
        # Lazy module import (not from-import) so tests can replace lifecycle dispatch at this call site.
        with suppress(Exception):
            from hermes_cli import lifecycle as _lifecycle
            if not _lifecycle.has_hook("api_request_error"):
                return
            ended_at = time.time()
            _lifecycle.invoke_hook(
                "api_request_error",
                task_id=task_id,
                turn_id=turn_id,
                api_request_id=api_request_id,
                physical_attempt_id=getattr(self, '_current_api_physical_attempt_id', ''),
                attempt_started_at=getattr(self, '_current_api_attempt_started_at', api_start_time),
                session_id=self.session_id or "",
                platform=self.platform or "",
                model=self.model,
                provider=self.provider,
                base_url=self.base_url,
                api_mode=self.api_mode,
                api_call_count=api_call_count,
                api_duration=ended_at - api_start_time,
                started_at=api_start_time,
                ended_at=ended_at,
                provider_dispatched=False if api_kwargs is None else None,
                status_code=status_code,
                retry_count=retry_count,
                max_retries=max_retries,
                retryable=retryable,
                reason=reason,
                error={"type": error_type, "message": error_message},
                request=self._api_request_payload_for_hook(api_kwargs),
            )
