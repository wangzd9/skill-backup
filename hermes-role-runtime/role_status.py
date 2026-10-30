"""Small, read-only role availability view; task acceptance remains separate."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo

from runtime_common import locked, now, read_json, save_json

ROLES = {'A': '1号', 'B': '2号', 'C': '3号', 'TESTER': 'TESTER'}
NAMES = {'A': '执行者 A', 'B': '执行者 B', 'C': '执行者 C', 'TESTER': '测试者'}
LABELS = {'IDLE': '空闲', 'WORKING': '工作中', 'WAITING': '等待',
          'BLOCKED': '阻塞', 'PAUSED': '暂停', 'UNKNOWN': '待核实'}
DELIVERED = {'DELIVERED', 'DELIVERED_FROZEN', 'DELIVERED_REVIEW_PENDING',
             'REVIEW_PENDING', 'TEST_REPORT_PENDING'}
TERMINAL = {'DONE', 'ACCEPTED', 'ACCEPT_MERGED', 'ACCEPTED_MERGED', 'CLOSED',
            'CANCELLED', 'CANCELED', 'SUPERSEDED', 'REJECTED'}
TASK = r'(?:TASK|NC|MN)-[A-Z]{0,4}\d+[A-Z]?(?:-[A-Z0-9]+)*'
TZ = ZoneInfo('Asia/Shanghai')


def runtime_module():
    import role_runtime
    return role_runtime


def confined(root, value):
    path = (root/value).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError('角色材料必须位于绑定项目内')
    return path


def file_signature(path):
    stat = path.stat()
    return [stat.st_ino, stat.st_size, stat.st_mtime_ns]


def read_dispatch(root, role, source):
    """Read the actual role-owned dispatch, without trusting a supplied status."""
    if role not in ROLES:
        raise ValueError('领取只适用于执行者或测试者')
    path = confined(root, source)
    folder = (root/'inbox'/ROLES[role]).resolve()
    if not path.is_relative_to(folder) or path.suffix != '.md':
        raise ValueError('派单必须位于本角色 inbox')
    before = file_signature(path)
    if before[1] > 256*1024:
        raise ValueError('派单过大，需要协调者提供明确当前任务卡')
    body = path.read_text()
    signature = file_signature(path)
    if signature != before:
        raise ValueError('派单读取期间变化')
    if not (re.search(r'dispatch|rework', path.stem, re.I) or re.search(r'^#.*(?:派单|返修)', body[:1800], re.M)):
        raise ValueError('材料不是明确派单')
    plain = body.replace('**', '').replace('`', '')
    status = re.search(r'^\s*(?:-\s*)?status\s*[:：=]\s*([A-Z_]+)', plain, re.M | re.I)
    task_field = re.search(r'^\s*(?:-\s*)?(?:task|任务)\s*[:：=]\s*('+TASK+r')(?![A-Z0-9-])', plain, re.M | re.I)
    task = task_field[1].upper() if task_field else None
    if not task and re.fullmatch(TASK, path.parent.name):
        task = path.parent.name
    if not task:
        heading = re.search(r'^#.*?('+TASK+r')(?![A-Z0-9-])', plain, re.M)
        task = heading[1] if heading else None
    attempt_field = re.search(r'(?:^|[｜|;；\s])(?:attempt|轮次)\s*[:：=]\s*([A-Za-z0-9_.-]+)', plain, re.M | re.I)
    attempt = attempt_field[1] if attempt_field else None
    if not attempt:
        match = re.match(r'(attempt-\d+(?:-r\d+)?)', path.stem)
        attempt = match[1] if match else None
    base = re.search(r'(?:固定\s*)?base\s*[:：=]\s*([a-f0-9]{7,40})(?![a-f0-9])', plain, re.I)
    head = re.search(r'\bhead\s*[:：=]\s*([a-f0-9]{7,40})(?![a-f0-9])', plain, re.I)
    declared_role = re.search(r'^\s*(?:-\s*)?(?:role|角色(?:/owner)?|owner)\s*[:：=]\s*(?:执行者\s*)?(A|B|C|TESTER)\b', plain, re.M | re.I)
    if declared_role and declared_role[1].upper() != role:
        raise ValueError('派单声明角色与本角色冲突')
    return {'source': str(path.relative_to(root)), 'task': task, 'attempt': attempt,
            'status': status[1].upper() if status else 'UNKNOWN', 'base': base[1] if base else None,
            'head': head[1] if head else None, 'sha256': hashlib.sha256(body.encode()).hexdigest(),
            'signature': signature, 'body': body}


def refresh_dispatch_index(project, role, dispatches, *, operations=None):
    """Called by the existing scan with its already discovered dispatch locations."""
    runtime = runtime_module(); root, binding = runtime.binding(project)
    if role not in ROLES or role not in binding['roles']:
        raise ValueError('角色未绑定')
    if len(dispatches) > 1024:
        raise ValueError('派单索引过大，需归档已关闭派单')
    entries, errors, seen = [], [], set()
    for item in dispatches:
        source = item['source'] if isinstance(item, dict) else str(item)
        if source in seen:
            continue
        seen.add(source)
        try:
            row = read_dispatch(root, role, source)
            row.pop('body')
            if row['status'] not in TERMINAL:
                entries.append(row)
        except (OSError, ValueError, KeyError) as exc:
            errors.append({'source': source, 'error': str(exc)[:160]})
    if operations is None:
        from operation_jobs import operation_states
        operations = operation_states(root, role)
    active = [{key: item.get(key) for key in ('operation_id', 'status', 'source')}
              for item in operations if item.get('status') in {'PENDING', 'RUNNING', 'UNKNOWN'}]
    value = {'schema_version': 1, 'role': role, 'observed_at': now(), 'entries': entries, 'errors': errors,
             'active_operations': active,
             'verified_no_task': (root/'inbox'/ROLES[role]).is_dir() and not entries and not errors and not active}
    target = root/f'inbox/control/runtime/dispatch-index/{role}.json'
    with locked(target.with_suffix('.lock')):
        save_json(target, value)
    return value


def role_status(project, role, *, at=None):
    """Availability means a new authorized task can be claimed, not business DONE."""
    import os
    from workflow_runtime import active, role_state
    pool_path=os.environ.get('HERMES_ROLE_POOL_PATH')
    if pool_path and active(pool_path):
        return role_state(pool_path,role,at=at)
    if role not in ROLES:
        raise ValueError('unsupported availability role')
    at = at or datetime.now(TZ)
    result = {'role': role, 'status': 'UNKNOWN', 'label': LABELS['UNKNOWN'], 'available': False,
              'task': None, 'attempt': None, 'reasons': [], 'sources': [], 'observed_at': at.isoformat()}
    def finish(status, reason):
        result.update(status=status, label=LABELS[status], available=status == 'IDLE')
        result['reasons'].append(reason)
        return result
    try:
        runtime = runtime_module(); root, binding = runtime.binding(project)
        if role not in binding['roles']:
            return finish('UNKNOWN', '角色未绑定')
        if binding.get('control') != 'ACTIVE':
            return finish('PAUSED', '项目控制状态暂停新领取')
        if runtime.legacy_busy(binding, role):
            return finish('UNKNOWN', '旧 owner 尚未确认停止')
        base = root/'inbox/control/runtime'
        checkpoint_path = base/f'checkpoints/{role}.json'
        checkpoint = read_json(checkpoint_path, {}) or {}
        from requirement_scope import task_scope,task_allowed,checkpoint_for_scope
        scope=task_scope(root)
        if scope:
            checkpoint,_=checkpoint_for_scope(root,role,checkpoint,scope)
        index_path = base/f'dispatch-index/{role}.json'
        index = read_json(index_path, {}) or {}
        if scope:
            index={**index,'entries':[row for row in index.get('entries',[]) if task_allowed(scope,row.get('task'))],
                   'active_operations':[row for row in index.get('active_operations',[]) if task_allowed(scope,row.get('task'))]}
        result['sources'] = [str(p.relative_to(root)) for p in (checkpoint_path, index_path) if p.exists()]
        if checkpoint and (checkpoint.get('schema_version') != 1 or checkpoint.get('role') != role):
            return finish('UNKNOWN', '当前检查点身份或版本不匹配')
        result.update(task=checkpoint.get('task'), attempt=checkpoint.get('attempt'))
        lease = read_json(runtime.lease_path(root, role), {}) or {}
        health = runtime.owner_state(lease) if lease.get('owner') else 'none'
        result['process_state'] = health
        if lease.get('owner') and health != 'alive':
            return finish('UNKNOWN', '角色租约 owner 的进程身份待核实')
        state = str(checkpoint.get('status', '')).upper()
        if state in {'PAUSED', 'STOPPED'}:
            return finish('PAUSED', '当前检查点暂停新领取')
        if state in {'BLOCKED', 'FAILED'}:
            return finish('BLOCKED', '当前检查点有未解除阻塞')
        if state in {'WAITING', 'WAITING_DEPENDENCY', 'MIGRATION_WAIT'}:
            return finish('WAITING', '等待派单列明的前置条件')
        if health == 'alive':
            return finish('WORKING', '角色业务 owner 正在执行')
        if not index or index.get('schema_version') != 1 or index.get('role') != role or index.get('errors'):
            return finish('UNKNOWN', '当前派单索引缺失或待核对')
        observed = datetime.fromisoformat(str(index.get('observed_at')).replace('Z', '+00:00'))
        if observed.tzinfo is None or not -30 <= (at-observed).total_seconds() <= 600:
            return finish('UNKNOWN', '当前派单索引已超过十分钟或时钟不可核实')
        operations = index.get('active_operations', [])
        if operations:
            return finish('UNKNOWN' if any(x.get('status') == 'UNKNOWN' for x in operations) else 'WORKING',
                          '本角色仍有在途或待对账工程操作')
        for dispatch in index.get('entries', []):
            path = confined(root, dispatch['source'])
            if file_signature(path) != dispatch.get('signature'):
                return finish('UNKNOWN', '派单已变化，等待脚本刷新索引')
        release = checkpoint.get('slot_release') or {}
        released = has_released_slot(checkpoint, root)
        pending = [row for row in index.get('entries', [])
                   if not (released and row.get('task') == checkpoint.get('task')
                           and row.get('attempt') == checkpoint.get('attempt')
                           and (release.get('dispatches', {}).get(row['source']) == row.get('sha256')
                                or (row['source'] == checkpoint.get('dispatch')
                                    and row.get('sha256') == checkpoint.get('dispatch_sha256'))))]
        if pending:
            return finish('WAITING', '有当前派单或返修待领取/接续')
        if released:
            return finish('IDLE', '协调者已释放本次交付的执行槽，可领取新派单')
        if not checkpoint and index.get('verified_no_task') is True:
            return finish('IDLE', '最新完整扫描确认没有当前派单、owner 或在途操作，可领取新派单')
        return finish('UNKNOWN' if not checkpoint or state in {'IDLE', 'IDLE_WAITING'} else 'WAITING',
                      '没有当前可接单的释放证据' if checkpoint else '当前检查点缺失，不从历史 session 猜测空闲')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return finish('UNKNOWN', '状态来源不可核实：'+str(exc)[:140])


def has_released_slot(checkpoint, project=None):
    """Only immutable, still-verifiable delivery material can free a business slot."""
    release = checkpoint.get('slot_release') or {}
    released = (checkpoint.get('slot_released') is True and bool(release.get('delivery_event_id'))
            and release.get('task') == checkpoint.get('task') and release.get('attempt') == checkpoint.get('attempt')
            and str(checkpoint.get('status', '')).upper() in DELIVERED | TERMINAL | {'IDLE', 'IDLE_WAITING'})
    if not released:
        return False
    if project is None:
        raise ValueError('释放槽证据需要绑定项目才能核实')
    root = Path(project).resolve()
    role = checkpoint.get('role')
    if role not in ROLES:
        raise ValueError('释放槽角色身份不可核实')
    event_id = release['delivery_event_id']
    if not isinstance(event_id, str) or not event_id.startswith(role+':'):
        raise ValueError('释放槽交付事件身份不可核实')
    event_path = confined(root, 'events/'+role+'/'+hashlib.sha256(event_id.encode()).hexdigest()+'.json')
    evidence = confined(root, release.get('evidence', ''))
    for path, hash_field in ((event_path, 'delivery_event_sha256'), (evidence, 'evidence_sha256')):
        expected = release.get(hash_field)
        if not isinstance(expected, str) or not re.fullmatch(r'[a-f0-9]{64}', expected):
            raise ValueError('释放槽缺少冻结证据哈希')
        if not path.is_file() or path.stat().st_size > 1024*1024:
            raise ValueError('释放槽交付证据缺失或过大')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError('释放槽交付证据发生变化')
        if path == event_path:
            event = json.loads(raw)
    if (not isinstance(event, dict) or event.get('event_id') != event_id
            or any(event.get(key) != checkpoint.get(key) for key in ('role', 'task', 'attempt'))
            or event.get('status') not in DELIVERED
            or str(evidence.relative_to(root)) not in event.get('evidence', '')):
        raise ValueError('释放槽交付事件与当前任务或证据不匹配')
    return True


def collect_role_statuses(project, *, at=None):
    scope = report_role_scope(project)
    rows = {}
    for role in ROLES:
        origins = scope['roles'].get(role, [])
        if len(origins) == 1 and not scope['errors']:
            source = origins[0]['project_root']
            rows[role] = {**role_status(source, role, at=at), 'project_root': source}
        elif not origins and not scope['errors']:
            rows[role] = role_status(project, role, at=at)
        else:
            rows[role] = {'role': role, 'status': 'UNKNOWN', 'label': LABELS['UNKNOWN'],
                'available': False, 'task': None, 'attempt': None,
                'observed_at': (at or datetime.now(TZ)).isoformat(), 'sources': origins,
                'reasons': ['角色报告来源冲突或显式订阅不可核实'], 'scope_errors': scope['errors']}
    return rows


def report_role_scope(project):
    """Resolve only this project's explicit, one-level read subscriptions."""
    try:
        runtime = runtime_module(); root, binding = runtime.binding(project)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {'roles': {}, 'errors': [{'code': 'ROLE_REPORT_BINDING_UNVERIFIED',
            'error': type(exc).__name__+': '+str(exc)[:240]}]}
    roles = {role: [{'project_root': str(root), 'role': role}] for role in ROLES if role in binding['roles']}
    errors = []
    try:
        from coordination_inputs import validate_subscriptions
        for item in validate_subscriptions(root, binding):
            for role in item['roles']:
                roles.setdefault(role, []).append({'project_root': item['project_root'], 'role': role})
    except (OSError, ValueError, KeyError, TypeError) as exc:
        errors.append({'code': 'COORDINATED_SCOPE_UNVERIFIED', 'error': type(exc).__name__+': '+str(exc)[:240]})
    for role, origins in roles.items():
        if len(origins) > 1:
            errors.append({'code': 'ROLE_REPORT_SOURCE_CONFLICT', 'role': role, 'sources': origins})
    return {'roles': roles, 'errors': errors}


def render_status_line(rows):
    return '；'.join(NAMES[role]+'（'+str(rows.get(role, {}).get('label', '待核实'))+'）' for role in ROLES)
