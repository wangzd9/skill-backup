"""Native-bound access for manual role conversations; never a business lease.

Only the gateway may call ``record_request`` with an authenticated user's
original primary text. Rewritten prompts, quotes, attachments, tool arguments,
and model output are not authorization sources. A full grant is deliberately
in-memory and binds to one exact native session/turn, never the latest session
message. Restarting the gateway restores the default read-only policy.
"""
from dataclasses import dataclass
import hashlib
import re
import threading
import time
from typing import Callable


READONLY = 'readonly'
FULL = 'full'
READONLY_TOOLS = frozenset({
    'read_file', 'search_files', 'skill_view', 'skills_list', 'web_search',
    'session_search', 'role_live_progress', 'role_repo_state',
    # Progressive disclosure catalog reads; actual tool_call execution must
    # still be unwrapped and checked against the concrete target tool name.
    'tool_search', 'tool_describe',
})
UNRESTRICTED_KINDS = frozenset({'business_cron', 'summary'})
ROLE_NAMES = frozenset({'COORDINATOR', 'A', 'B', 'C', 'TESTER'})

# A current primary-text command, not a substring search or an LLM judgement.
# Punctuation is optional for an affirmative grant. A bare "全部权限" is also
# an explicit grant for this turn; it never invents a business task or scope.
_FULL_COMMAND = re.compile(
    r'^(?:/role-full(?=\s|$)|'
    r'(?:全部|所有)权限(?=$|[\s，,:：。；;！!])|'
    r'(?:本次(?:请求)?|本轮|这次)?(?:我)?(?:请)?'
    r'(?:授予(?:你)?|给你|给予你|开启|开放|授权(?:给)?你?)'
    r'(?:全部|所有)权限|'
    r'(?:本次|本轮)(?:使用|以)(?:全部|所有)权限(?:执行|处理|开展)'
    r'(?=$|[\s，,:：。；;！!]))')
_QUOTED_OR_CONDITIONAL = re.compile(
    r'^(?:[>\x60\"\'“”‘’「『（(\[]|如果|假如|比如|例如|举例|引用|附件|转述|他说|她说|'
    r'是否|能否|可否|不要|无需|不需要|禁止|不授予|未授予)')

def full_permission_action(primary_text):
    """Return the current grant's action, or None when there is no grant.

    Empty text after a valid grant is allowed and returns ''. Callers still
    need an actual authorized business request before taking action. Only the
    trusted native primary text may be passed here: rendered gateway origin
    headers, history, quotes and attachment text never recover a grant.
    """
    if not isinstance(primary_text, str):
        return None
    text = primary_text.strip()
    if not text or len(text) > 128 * 1024 or _QUOTED_OR_CONDITIONAL.match(text):
        return None
    first_clause = re.split(r'[，,:：。；;！!\n]', text, maxsplit=1)[0]
    if any(mark in first_clause for mark in ('?', '？', '吗', '么', '是否', '不要', '不需要', '无需', '禁止')):
        return None
    match = _FULL_COMMAND.match(text)
    if match is None:
        return None
    return text[match.end():].lstrip(' \t\r\n，,:：。；;！!')


def explicit_full_permission(primary_text):
    """Recognize an affirmative full-permission grant in native primary text."""
    return full_permission_action(primary_text) is not None


def _block(reason):
    return {'action': 'block', 'message': reason}


def readonly_tool_decision(tool_name, *, session_kind='group_chat', full=False,
                           lease_check: Callable[[], bool] | None = None):
    """Return a native pre_tool_call veto, or None when this policy allows it.

    Outside role manual sessions the caller should not invoke this policy.
    Business cron and report sessions explicitly retain their existing scope.
    ``full`` alone never allows an effect: exact owner verification is required
    on every effectful tool call and this module never acquires/takes over a
    lease. Unknown tool names and missing scope fail closed.
    """
    if session_kind in UNRESTRICTED_KINDS:
        return None
    if session_kind != 'group_chat':
        return _block('角色会话来源未核实，本次不执行工具；请核对原生会话身份。')
    if tool_name in READONLY_TOOLS:
        return None
    if not full:
        return _block('角色群聊本轮为只读，仅可查询资料与当前进度。执行操作需当前用户原文明确授予本轮全部权限；后台工作继续按原 owner 执行。')
    try:
        owns_lease = lease_check is not None and lease_check() is True
    except Exception:
        owns_lease = False
    if not owns_lease:
        return _block('本轮已有全部权限授权，但尚未核实持有本角色业务租约；不得抢占或重复执行。请先由原生入口核对 owner。')
    return None


@dataclass
class _Request:
    key: str
    digest: str
    full: bool
    created: float
    bound_turn: tuple[str, str] | None = None
    conflict: bool = False


class ManualAccess:
    """Thread-safe one-request/one-turn grants from trusted native intake.

    The caller must correlate native message identity to the *starting* native
    turn before ``bind_turn``. A session's latest message, task IDs from model
    arguments, and prompt text are insufficient. No binding means read-only,
    including pre-existing/old role group conversations.
    """
    def __init__(self, *, clock=time.monotonic, max_age_seconds=7200, max_requests=2048, observer=None):
        if max_age_seconds <= 0 or max_requests <= 0:
            raise ValueError('manual access limits must be positive')
        self._clock = clock
        self._max_age = max_age_seconds
        self._max_requests = max_requests
        self._lock = threading.RLock()
        self._requests = {}
        self._turns = {}
        self._observer = observer

    def _observed(self, kind, **metadata):
        # Evidence is metadata only and can never alter the authorization result.
        if self._observer is not None:
            try:
                self._observer({'kind': kind, **metadata})
            except Exception:
                pass

    def _prune(self, now, *, reserve=False):
        # A grant belongs to its exact native turn, whose development work may
        # legitimately last longer than the intake queue TTL. Pending-message
        # pressure cannot revoke an unrelated running turn either. End/restart
        # and identity conflicts remain the explicit revocation boundaries.
        active = {key for key, request in self._requests.items()
                  if request.bound_turn is not None and self._turns.get(request.bound_turn) == key}
        expired = {key for key, request in self._requests.items()
                   if key not in active and (now - request.created >= self._max_age or now < request.created)}
        pending = set(self._requests) - active - expired
        if reserve and len(pending) >= self._max_requests:
            ordered = sorted((self._requests[key].created, key) for key in pending)
            expired.update(key for _, key in ordered[:len(ordered) - self._max_requests + 1])
        for key in expired:
            request = self._requests.pop(key, None)
            if request and request.bound_turn:
                self._turns.pop(request.bound_turn, None)

    def record_request(self, *, project, role, chat_id, user_id, message_id,
                       primary_text, authorized=False, native_primary_text=False):
        """Record authenticated primary text before the gateway rewrites it.

        ``native_primary_text=True`` asserts that text came from the adapter's
        primary user-content field, excluding attachments/reply expansion. It
        must be false when this provenance cannot be proven. Missing identity
        is rejected; a conflicting redelivery never upgrades an existing ID.
        """
        identity = (project, role, chat_id, user_id, message_id)
        if role not in ROLE_NAMES or any(not isinstance(value, str) or not value for value in identity):
            raise ValueError('manual request requires native project/role/chat/user/message identity')
        if not isinstance(primary_text, str):
            raise ValueError('manual primary text must be a string')
        key = hashlib.sha256('\0'.join(identity).encode()).hexdigest()
        digest = hashlib.sha256(primary_text.encode()).hexdigest()
        full = (authorized is True and native_primary_text is True and explicit_full_permission(primary_text))
        with self._lock:
            now = self._clock(); self._prune(now)
            previous = self._requests.get(key)
            if previous:
                if previous.digest != digest:
                    previous.conflict = True
                    previous.full = False
                # A redelivery lacking provenance cannot create or upgrade a grant.
                return key
            self._prune(now, reserve=True)
            self._requests[key] = _Request(key, digest, full, now)
            self._observed('NATIVE_REQUEST', request_key=key, primary_sha256=digest,
                           authenticated=authorized is True, native_primary=native_primary_text is True,
                           full=full, message_id=message_id)
        return key

    def bind_turn(self, *, session_id, turn_id, request_key):
        """Bind a known native message exactly once; return full or readonly."""
        if not all(isinstance(value, str) and value for value in (session_id, turn_id, request_key)):
            return READONLY
        turn = (session_id, turn_id)
        with self._lock:
            self._prune(self._clock())
            request = self._requests.get(request_key)
            existing = self._turns.get(turn)
            if existing is not None and existing != request_key:
                previous = self._requests.get(existing)
                if previous:
                    previous.conflict = True
                    previous.full = False
                return READONLY
            if not request:
                return READONLY
            if request.bound_turn not in (None, turn):
                return READONLY
            request.bound_turn = turn
            self._turns[turn] = request_key
            self._observed('TURN_BOUND', request_key=request_key, session_id=session_id,
                           turn_id=turn_id, full=request.full and not request.conflict)
            return FULL if request.full and not request.conflict else READONLY

    def access(self, *, session_id, turn_id):
        with self._lock:
            self._prune(self._clock())
            request = self._requests.get(self._turns.get((session_id, turn_id)))
            return FULL if request and request.full and not request.conflict else READONLY

    def end_turn(self, *, session_id, turn_id):
        """Forget this exact turn's grant; never cancel work or release a lease."""
        with self._lock:
            key = self._turns.pop((session_id, turn_id), None)
            request = self._requests.get(key)
            if request:
                request.full = False
                self._observed('TURN_ENDED', request_key=key, session_id=session_id,
                               turn_id=turn_id, full=False)

    def tool_decision(self, *, tool_name, session_kind, session_id='', turn_id='', lease_check=None):
        try:
            full = self.access(session_id=session_id, turn_id=turn_id) == FULL
            return readonly_tool_decision(tool_name, session_kind=session_kind, full=full, lease_check=lease_check)
        except Exception:
            return _block('角色群聊权限核验失败，本次工具已阻断；不会转为执行权限。')
