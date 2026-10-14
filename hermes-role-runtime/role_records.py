"""Lease-guarded JSON records. Observations never become business events."""
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sys

from runtime_common import now, read_json, save_json


def canonical(root, value):
    path = (root / value).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError('记录路径必须在当前绑定项目内')
    return path


@contextmanager
def role_guard(project, role, token_file):
    """Use the existing ACTIVE gate and lease; never claim or renew ownership."""
    sys.path.insert(0, str(Path.home()/'.codex/skills/complex-task-coordinator/scripts'))
    import role_runtime
    root, data = role_runtime.binding(project)
    if role not in data['roles']:
        raise ValueError('角色未绑定')
    token = read_json(Path(token_file)).get('token')
    with role_runtime.locked(role_runtime.lease_lock(root, role)):
        root, data = role_runtime.binding(project)
        if role not in data['roles']:
            raise ValueError('角色绑定已改变')
        role_runtime.guard(data, role)
        role_runtime.valid_lease(root, role, token)
        yield root, data


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _exact_time(value):
    # Preserve source precision. Known minutes are valid; placeholders, relative
    # expressions and ambiguous local times cannot become dated evidence.
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d(?:\.\d+)?)?(?:Z|[+-]\d\d:\d\d)', value):
        raise ValueError('发生时间证据须含完整日期、明确分钟和时区；无法核实请省略 occurrence')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.utcoffset() is None:
        raise ValueError('时间缺少时区')
    return value


def _occurrence(root, occurrence, recorded_at):
    if occurrence is None:
        return None, {'kind': 'unknown', 'reason': '历史发生时间未核实；recorded_at 只表示补录时间'}
    if not isinstance(occurrence, dict):
        raise ValueError('occurrence 必须是 JSON 对象')
    if occurrence == {'kind': 'now'}:
        return recorded_at, {'kind': 'script_now'}
    if set(occurrence) != {'source', 'field'}:
        raise ValueError('历史时间仅从 source JSON 的 field 读取，不接受手填发生时间')
    source = canonical(root, occurrence['source'])
    raw = source.read_bytes()
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError('时间证据文件过大；请引用结构化结果文件')
    value = json.loads(raw)
    for part in occurrence['field'].split('.'):
        value = value[part]
    return _exact_time(value), {'kind': 'json_evidence', 'source': str(source.relative_to(root)),
                               'field': occurrence['field'], 'sha256': hashlib.sha256(raw).hexdigest()}


def _validate(role, payload):
    allowed = {'event_id', 'kind', 'task', 'attempt', 'status', 'summary', 'evidence', 'next', 'occurrence'}
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise ValueError('记录仅接受约定的 JSON 字段')
    if payload.get('kind') not in ('business', 'observation'):
        raise ValueError('kind 必须明确为 business 或 observation')
    identifier = payload.get('event_id')
    if not isinstance(identifier, str) or not identifier.startswith(role + ':') or len(identifier) > 200 or any(ord(c) < 32 for c in identifier):
        raise ValueError('event_id 必须是以角色加冒号开头的稳定 ID，且不含控制字符')
    for key in ('task', 'attempt', 'status', 'summary', 'evidence', 'next'):
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            raise ValueError('缺少非空字符串字段：' + key)
        clocks = re.findall(r'(?<![0-9A-Za-z])[\dXx?？]{1,2}[:：][\dXx?？]{1,2}(?:[:：][\dXx?？]{1,2})?(?![\dXx?？])', payload[key])
        if any(re.search(r'[xX?？]', clock) for clock in clocks):
            raise ValueError('禁止模糊钟点（如 10:2x）；请写发生时间未知，或给出有来源的可信时间区间')
    if len(json.dumps(payload, ensure_ascii=False).encode()) > 128 * 1024:
        raise ValueError('记录过大；详细输出应作为独立证据引用')


def record_role(project, role, payload, token_file):
    """Write a durable intent first, then recover idempotently across event/session files."""
    _validate(role, payload)
    with role_guard(project, role, token_file) as (root, binding):
        key = hashlib.sha256(payload['event_id'].encode()).hexdigest()
        quiet = payload['kind'] == 'observation'
        folder = 'observations' if quiet else 'records'
        journal = canonical(root, f'inbox/control/runtime/{folder}/{role}/{key}.json')
        old = read_json(journal)
        created = old is None
        fingerprint = _digest(payload)
        if old is not None and old.get('input_sha256') != fingerprint:
            raise ValueError('EVENT_ID_CONFLICT：相同 ID 的输入改变，禁止覆盖')
        if old is None:
            recorded_at = now()
            occurred_at, time_source = _occurrence(root, payload.get('occurrence'), recorded_at)
            row = {**payload, 'schema_version': 1, 'role': role, 'recorded_at': recorded_at,
                   'occurred_at': occurred_at, 'time_source': time_source}
            old = {'schema_version': 1, 'input_sha256': fingerprint, 'record': row, 'complete': False}
            if not quiet:
                session = canonical(root, binding['roles'][role]['session_file'])
                old['session'] = str(session.relative_to(root))
            save_json(journal, old)
        row = old['record']
        if quiet:
            if not old['complete']:
                old['complete'] = True
                save_json(journal, old)
            return {'created': created, 'kind': 'observation', 'recorded_at': row['recorded_at'],
                    'occurred_at': row['occurred_at'], 'path': str(journal), 'business_event': False}
        # A changed binding cannot redirect a previously frozen write to another session.
        session = canonical(root, binding['roles'][role]['session_file'])
        if str(session.relative_to(root)) != old['session']:
            raise ValueError('会话绑定已变化，须人工核对未完成记录')
        event = canonical(root, f'events/{role}/{key}.json')
        existing = read_json(event)
        if existing is not None and existing != row:
            raise ValueError('EVENT_ID_CONFLICT：已有事件与冻结记录不符')
        if existing is None:
            save_json(event, row)
        marker = '<!-- role-record:' + key + ' -->'
        text = session.read_text() if session.exists() else '# ' + role + '\n'
        clean = lambda value: str(value).replace('\r', ' ').replace('\n', ' ')
        when = row['occurred_at'] or '发生时间未知'
        block = ('\n' + marker + '\n- **' + clean(when) + '｜' + clean(row['status']) + '** ' +
                 clean(row['task']) + '/' + clean(row['attempt']) + '：' + clean(row['summary']) +
                 '；证据：' + clean(row['evidence']) + '；下一步：' + clean(row['next']) +
                 '；记录时间：' + row['recorded_at'] + '\n')
        if marker in text:
            if block not in text:
                raise ValueError('已有会话记录与冻结内容不符，禁止静默覆盖')
        else:
            # Atomic replace preserves all old text; the role lease serializes cooperating writers.
            import role_runtime
            role_runtime.atomic(session, text + block)
        was_complete = old['complete']
        if not was_complete:
            old['complete'] = True
            save_json(journal, old)
        return {'created': not was_complete, 'kind': 'business', 'recorded_at': row['recorded_at'],
                'occurred_at': row['occurred_at'], 'path': str(journal), 'event': str(event), 'session': str(session)}
