"""Lease-checked task ACKs and durable, separately delivered claim notices."""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from zoneinfo import ZoneInfo

from role_records import role_guard, record_role
from role_status import ROLES, NAMES, DELIVERED, TERMINAL, confined, has_released_slot, read_dispatch, runtime_module
from runtime_common import locked, now, read_json, save_json

TZ = ZoneInfo('Asia/Shanghai')


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _instant(at):
    at = at or datetime.now(TZ)
    if at.tzinfo is None:
        raise ValueError('领取/通知时间必须含时区')
    return at.astimezone(TZ)


def _quiet(at):
    return at.hour >= 21 or at.hour < 8


def _git(worktree, *args):
    try:
        return subprocess.check_output(['git', '-C', str(worktree), *args], text=True,
                                       stderr=subprocess.DEVNULL, timeout=10).strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        raise ValueError('无法核实实际 Git 工作区/分支/基线') from exc


def _restore_claim_checkpoint(root,role,task,attempt):
    """A repeated ACK may restore parked progress, never its old claim event."""
    from requirement_scope import task_scope,task_allowed,park_checkpoint,restore_scope_checkpoint
    path=root/f'inbox/control/runtime/checkpoints/{role}.json'
    current=read_json(path,{}) or {}
    scope=task_scope(root)
    # An ACK retry for a previous task cannot displace newer selected work.
    if (current and task_allowed(scope,current.get('task'))
            and any(current.get(key)!=value for key,value in (('task',task),('attempt',attempt)))):
        return
    restored,_=restore_scope_checkpoint(root,role,task,attempt)
    if restored and restored!=current:
        if current:park_checkpoint(root,role,current)
        save_json(path,restored)


def _validate_spec(root, role, spec, *, verify_worktree=True):
    required = {'dispatch', 'task', 'attempt', 'base', 'branch', 'worktree'}
    if not isinstance(spec, dict) or set(spec) - required - {'ack', 'head'} or not required <= set(spec):
        raise ValueError('领取必须提供派单、task/attempt、base、实际 branch/worktree')
    for key in required:
        if not isinstance(spec[key], str) or not spec[key].strip() or any(ord(c) < 32 for c in spec[key]):
            raise ValueError('领取字段必须为明确的非空字符串：'+key)
    dispatch = read_dispatch(root, role, spec['dispatch'])
    if dispatch['status'] in TERMINAL | DELIVERED | {'PAUSED', 'BLOCKED', 'WAITING'}:
        raise ValueError('派单已关闭、暂停、交付或存在未解除前置条件，不能重新领取')
    if dispatch['task'] != spec['task'] or dispatch['attempt'] != spec['attempt']:
        raise ValueError('派单 task/attempt 与领取身份不匹配，需要协调者明确当前任务卡')
    if not dispatch['base'] or not (spec['base'].startswith(dispatch['base']) or dispatch['base'].startswith(spec['base'])):
        raise ValueError('领取 base 与派单固定 base 不匹配')
    if not re.fullmatch(r'[a-f0-9]{7,40}', spec['base']):
        raise ValueError('base 必须为真实固定 commit')
    path = confined(root, spec['dispatch'])
    ack_name = re.sub(r'dispatch', 'ack', path.stem, flags=re.I)
    if ack_name == path.stem:
        ack_name += '-ack'
    ack = confined(root, spec.get('ack') or str(path.with_name(ack_name+'.md').relative_to(root)))
    if not ack.is_relative_to((root/'inbox'/ROLES[role]).resolve()) or ack.suffix != '.md' or ack == path:
        raise ValueError('ACK 必须为本角色独立文档')
    worktree = Path(spec['worktree']).resolve()
    if not verify_worktree:
        return dispatch, ack, None, None, str(worktree)
    if not worktree.is_dir() or Path(_git(worktree, 'rev-parse', '--show-toplevel')).resolve() != worktree:
        raise ValueError('worktree 必须是实际 Git 工作区根')
    commit = _git(worktree, 'rev-parse', '--verify', spec['base']+'^{commit}')
    branch = _git(worktree, 'rev-parse', '--abbrev-ref', 'HEAD')
    expected_branch = 'HEAD' if spec['branch'] == 'DETACHED' else spec['branch']
    if branch != expected_branch:
        raise ValueError('领取分支与实际 checkout 不一致')
    if role != 'TESTER' and branch in {'HEAD', 'main', 'master'}:
        raise ValueError('执行者必须使用任务专属分支')
    target = spec.get('head') or (dispatch.get('head') if role == 'TESTER' else None) or commit
    if spec.get('head') and (not dispatch.get('head') or not spec['head'].startswith(dispatch['head'])):
        raise ValueError('被测 head 与派单固定快照不一致')
    actual = _git(worktree, 'rev-parse', 'HEAD')
    expected = _git(worktree, 'rev-parse', '--verify', target+'^{commit}')
    if actual != expected:
        raise ValueError('实际 checkout 与本次领取固定基线/被测快照不一致')
    return dispatch, ack, commit, actual, str(worktree)


def claim_task(project, role, spec, token_file, *, at=None):
    if os.environ.get('HERMES_ROLE_POOL_DRAIN_ONLY')=='1':
        raise ValueError('需求切换收口回合仅处理既有回执，不领取新任务')
    at = _instant(at)
    runtime = runtime_module(); root, binding = runtime.binding(project)
    if role not in ROLES or role not in binding['roles']:
        raise ValueError('领取角色未绑定')
    if not isinstance(spec, dict):
        raise ValueError('领取输入必须是 JSON 对象')
    from requirement_scope import require_task_scope
    require_task_scope(root,spec.get('task'))
    # The project is the enclosing directory; dispatch mirrors are not new work.
    identifier = _digest({'role': role, 'task': spec.get('task'), 'attempt': spec.get('attempt')})
    folder = root/'inbox/control/runtime/task-claims'/role
    journal = folder/(identifier+'.json')
    with locked(folder/'claims.lock'):
        with role_guard(root, role, token_file) as (_, current):
            dispatch, ack, _, _, _ = _validate_spec(root, role, spec, verify_worktree=False)
            frozen_input = {**spec, 'dispatch_sha256': dispatch['sha256']}
            saved = read_json(journal)
            if saved is None:
                # Older v3 journals used the dispatch path in their key. Keep
                # their original outbox identity rather than charge/notify again.
                legacy_matches = []
                for path in folder.glob('*.json'):
                    legacy = read_json(path)
                    if not isinstance(legacy, dict) or not isinstance(legacy.get('record'), dict):
                        raise ValueError('历史领取记录不可核实')
                    record = legacy['record']
                    if (legacy.get('role') == role and record.get('task') == spec['task']
                            and record.get('attempt') == spec['attempt']):
                        if (not re.fullmatch(r'[a-f0-9]{64}', str(legacy.get('claim_id', '')))
                                or path.stem != legacy['claim_id']):
                            raise ValueError('历史领取身份不可核实')
                        legacy_matches.append((path, legacy))
                if len(legacy_matches) > 1:
                    raise ValueError('相同 task/attempt 已存在多个领取身份，需核对历史记录')
                if legacy_matches:
                    journal, saved = legacy_matches[0]
                    identifier = saved['claim_id']
            if saved and saved.get('input_sha256') != _digest(frozen_input):
                raise ValueError('相同领取身份的派单或输入发生变化，禁止覆盖旧 ACK')
            if saved and saved.get('complete'):
                if not ack.exists() or ack.read_text() != saved['ack_text']:
                    raise ValueError('已领取 ACK 缺失或变化，需要核对')
                _restore_claim_checkpoint(root,role,spec['task'],spec['attempt'])
                return {'claimed': False, 'already_acknowledged': True, 'ack': str(ack),
                        'claim_id': identifier,
                        **({'notification': str(root/f'inbox/control/runtime/claim-outbox/{role}/{identifier}.json')}
                           if not saved.get('legacy_ack') else {})}
            if saved is None:
                if ack.exists():
                    old = ack.read_text()
                    if all(value in old for value in (dispatch['source'], spec['task'], spec['attempt'])):
                        # Restoring an old ACK is silent, but still freezes the
                        # task/attempt so a renamed dispatch cannot restart it.
                        save_json(journal, {'schema_version': 1, 'role': role, 'claim_id': identifier,
                            'input_sha256': _digest(frozen_input), 'ack': str(ack.relative_to(root)),
                            'ack_text': old, 'record': {'task': spec['task'], 'attempt': spec['attempt']},
                            'complete': True, 'legacy_ack': True})
                        _restore_claim_checkpoint(root,role,spec['task'],spec['attempt'])
                        return {'claimed': False, 'already_acknowledged': True, 'ack': str(ack),
                                'claim_id': identifier}
                    raise ValueError('已有 ACK 与本次派单身份冲突，禁止覆盖')
                from requirement_scope import checkpoint_for_scope,park_checkpoint
                active = read_json(root/f'inbox/control/runtime/checkpoints/{role}.json', {}) or {}
                previous,_ = checkpoint_for_scope(root,role,current=active)
                if previous and not has_released_slot(previous, root):
                    raise ValueError('本角色尚有未释放的任务槽，不能领取第二项任务')
                dispatch, ack, commit, head, worktree = _validate_spec(root, role, spec)
                if active:park_checkpoint(root,role,active)
                event_id = role+':claim:'+identifier
                stamp = at.isoformat(timespec='seconds')
                text = (f'# ACK：{spec["task"]} / {spec["attempt"]}\n\n- status: ACKED_RUNNING\n'
                        f'- role: {role}\n- dispatch: {dispatch["source"]}\n- task: {spec["task"]}\n'
                        f'- attempt: {spec["attempt"]}\n- owner: {runtime.load(runtime.lease_path(root, role))["owner"]}\n'
                        f'- base: {commit}\n- head: {head}\n- branch: {spec["branch"]}\n'
                        f'- worktree: {worktree}\n- occurred_at: {stamp}\n- dispatch_sha256: {dispatch["sha256"]}\n')
                payload = {'event_id': event_id, 'kind': 'business', 'task': spec['task'], 'attempt': spec['attempt'],
                           'status': 'ACKED_RUNNING', 'summary': '已领取派单，开始本任务工作',
                           'evidence': str(ack.relative_to(root)), 'next': '按有效派单开展开发或独立验证',
                           'occurrence': {'source': str(journal.relative_to(root)), 'field': 'occurred_at'}}
                saved = {'schema_version': 1, 'role': role, 'claim_id': identifier, 'input_sha256': _digest(frozen_input),
                         'dispatch_sha256': dispatch['sha256'], 'ack': str(ack.relative_to(root)), 'ack_text': text,
                         'record': payload, 'occurred_at': stamp, 'complete': False,
                         'chat_id': current['roles'][role].get('chat_id')}
                save_json(journal, saved)
            if ack.exists():
                if ack.read_text() != saved['ack_text']:
                    raise ValueError('领取 ACK 被改变，保留冲突待核对')
            else:
                runtime.atomic(ack, saved['ack_text'])
        # record_role takes the same role lease lock itself; never nest that flock.
        record = record_role(root, role, saved['record'], token_file)
        with role_guard(root, role, token_file):
            checkpoint_path = root/f'inbox/control/runtime/checkpoints/{role}.json'
            checkpoint = read_json(checkpoint_path, {}) or {}
            if checkpoint.get('event_id') == saved['record']['event_id']:
                checkpoint.update(dispatch=spec['dispatch'], dispatch_sha256=saved['dispatch_sha256'], slot_released=False)
                checkpoint.pop('slot_release', None)
                save_json(checkpoint_path, checkpoint)
        outbox_path = root/f'inbox/control/runtime/claim-outbox/{role}/{identifier}.json'
        if not outbox_path.exists():
            happened = datetime.fromisoformat(saved['occurred_at']).astimezone(TZ)
            row = {'schema_version': 1, 'kind': 'task-claimed', 'notification_id': identifier, 'role': role,
                   'task': spec['task'], 'attempt': spec['attempt'], 'chat_id': saved['chat_id'],
                   'occurred_at': saved['occurred_at'], 'created_at': now(),
                   'status': 'SUPPRESSED' if _quiet(happened) else 'PENDING',
                   'text': NAMES[role]+'已领取 '+spec['task']+'（'+spec['attempt']+'），开始工作。',
                   'ack': saved['ack'], 'event_id': saved['record']['event_id']}
            if not row['chat_id']:
                row.update(status='UNKNOWN', error='领取通知缺少绑定群，禁止猜测投递目标')
            save_json(outbox_path, row)
        claimed = not saved['complete']
        saved['complete'] = True; save_json(journal, saved)
        return {'claimed': claimed, 'ack': str(ack), 'event': record.get('event'),
                'claim_id': identifier, 'notification': str(outbox_path)}


def release_slot(project, role, spec, coordinator_token_file, *, at=None):
    at = _instant(at)
    if role not in ROLES or not isinstance(spec, dict) or set(spec) != {'task', 'attempt', 'evidence'}:
        raise ValueError('释放槽必须明确角色、task/attempt与交付证据')
    runtime = runtime_module()
    with role_guard(project, 'COORDINATOR', coordinator_token_file) as (root, binding):
        if role not in binding['roles']:
            raise ValueError('角色未绑定')
        with runtime.locked(runtime.lease_lock(root, role)):
            checkpoint_path = root/f'inbox/control/runtime/checkpoints/{role}.json'
            checkpoint = read_json(checkpoint_path, {}) or {}
            if checkpoint.get('task') != spec['task'] or checkpoint.get('attempt') != spec['attempt']:
                raise ValueError('当前 task/attempt 与释放对象不匹配')
            event_id = checkpoint.get('event_id')
            if not event_id:
                raise ValueError('缺少当前不可变交付事件')
            event_path = root/'events'/role/(hashlib.sha256(event_id.encode()).hexdigest()+'.json')
            event = read_json(event_path, {}) or {}
            if any(event.get(k) != checkpoint.get(k) for k in ('event_id', 'role', 'task', 'attempt', 'status')) or event.get('status') not in DELIVERED:
                raise ValueError('只有真实已交付事件可由协调者释放槽')
            evidence = confined(root, spec['evidence'])
            if not evidence.is_file() or evidence.stat().st_size > 1024*1024:
                raise ValueError('交付证据缺失或过大')
            evidence_raw = evidence.read_bytes()
            body = evidence_raw.decode('utf-8')
            if not body.strip() or not all(re.search(r'(?<![A-Za-z0-9_-])'+re.escape(value)+r'(?![A-Za-z0-9_-])', body)
                                           for value in (spec['task'], spec['attempt'])):
                raise ValueError('交付证据 task/attempt 不匹配')
            if str(evidence.relative_to(root)) not in event.get('evidence', ''):
                raise ValueError('释放证据不是当前交付事件登记的材料')
            old_release = checkpoint.get('slot_release') or {}
            if checkpoint.get('slot_released') and old_release.get('task') == spec['task'] and old_release.get('attempt') == spec['attempt']:
                return {'released': False, 'role': role, 'task': spec['task'], 'attempt': spec['attempt']}
            checkpoint.update(slot_released=True, slot_release={'task': spec['task'], 'attempt': spec['attempt'],
                'delivery_event_id': event_id, 'delivery_event_sha256': hashlib.sha256(event_path.read_bytes()).hexdigest(),
                'evidence': str(evidence.relative_to(root)), 'evidence_sha256': hashlib.sha256(evidence_raw).hexdigest(),
                'released_at': at.isoformat(timespec='seconds'),
                'dispatches': {row['source']: row['sha256'] for row in
                    (read_json(root/f'inbox/control/runtime/dispatch-index/{role}.json', {}) or {}).get('entries', [])
                    if row.get('task') == spec['task'] and row.get('attempt') == spec['attempt']}})
            save_json(checkpoint_path, checkpoint)
            return {'released': True, 'role': role, 'task': spec['task'], 'attempt': spec['attempt']}


def pending_claim_notifications(project, role, *, at=None, include_suppressed=False):
    if at is not None:
        _instant(at)
    runtime = runtime_module(); root, binding = runtime.binding(project)
    if role not in ROLES or role not in binding['roles']:
        raise ValueError('领取通知角色未绑定')
    rows = []
    for path in sorted((root/f'inbox/control/runtime/claim-outbox/{role}').glob('*.json')):
        row = read_json(path)
        if not isinstance(row, dict) or row.get('role') != role or row.get('schema_version') != 1:
            raise ValueError('领取通知身份或版本无效')
        if row.get('status') == 'PENDING' or include_suppressed and row.get('status') == 'SUPPRESSED':
            rows.append({**row, 'source': str(path)})
    return rows


def drain_claim_notifications(project, role, send, *, at=None):
    """send(row) must confirm delivery; ambiguous outcomes require reconciliation."""
    fixed_at = _instant(at) if at is not None else None
    runtime = runtime_module(); root, binding = runtime.binding(project)
    results = []
    folder = root/f'inbox/control/runtime/claim-outbox/{role}'
    with locked(folder/'delivery.lock'):
        for row in pending_claim_notifications(root, role):
            path = Path(row.pop('source'))
            delivery_at = fixed_at if fixed_at is not None else _instant(None)
            if _quiet(delivery_at):
                row.update(status='SUPPRESSED', suppressed_at=delivery_at.isoformat(timespec='seconds'))
                save_json(path, row); results.append(row); continue
            if row.get('chat_id') != binding['roles'][role].get('chat_id'):
                row.update(status='UNKNOWN', error='角色绑定群已变化，禁止投递到旧目标')
                save_json(path, row); results.append(row); continue
            row.update(status='SENDING', sending_at=now()); save_json(path, row)
            try:
                outcome = send(dict(row))
                confirmed = outcome is True or isinstance(outcome, dict) and outcome.get('delivered') is True
                row.update(status='SENT' if confirmed else 'UNKNOWN', delivery_result=outcome,
                           finished_at=now())
            except Exception as exc:
                row.update(status='UNKNOWN', error=type(exc).__name__+': '+str(exc)[:180], finished_at=now())
            save_json(path, row); results.append(row)
    return results
