"""Latest role context and explicit, lease-guarded checkpoint continuations."""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from runtime_common import locked, now, read_json, save_json
from role_records import canonical, role_guard

SPEC_FIELDS = {'continuation_id', 'task', 'attempt', 'checkpoint', 'next', 'runnable'}
CONTEXT_LIMIT = 32*1024


def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _fingerprint(row):
    frozen = {key: row[key] for key in SPEC_FIELDS | {'checkpoint_sha256'}}
    return hashlib.sha256(json.dumps(frozen, sort_keys=True).encode()).hexdigest()


def _snapshot_source(sha256):
    return 'inbox/control/runtime/continuation-inputs/'+sha256+'.snapshot'


def _freeze_input(root, raw, expected_sha256):
    """Persist the exact input before publishing its continuation, never a live view."""
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError('CONTINUATION_CONFLICT: recovery input is not the original frozen bytes')
    source = _snapshot_source(expected_sha256)
    path = canonical(root, source)
    with locked(path.with_suffix('.lock')):
        if path.exists():
            if path.read_bytes() != raw:
                raise ValueError('CONTINUATION_CONFLICT: immutable input snapshot changed')
            return source
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix='.continuation-input-')
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(directory)
            finally: os.close(directory)
        finally:
            if os.path.exists(temporary): os.unlink(temporary)
    return source


def _frozen_input(root, row):
    source = row.get('checkpoint_snapshot') or row['checkpoint']
    path = canonical(root, source)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError('CONTINUATION_CONFLICT: original frozen checkpoint changed or missing; recover exact input bytes') from exc
    if hashlib.sha256(raw).hexdigest() != row['checkpoint_sha256']:
        raise ValueError('CONTINUATION_CONFLICT: original frozen checkpoint changed or missing; recover exact input bytes')
    return path, raw


def _read_current_input(path):
    before = path.stat()
    raw = path.read_bytes()
    after = path.stat()
    if ((before.st_ino, before.st_size, before.st_mtime_ns) !=
            (after.st_ino, after.st_size, after.st_mtime_ns)):
        raise ValueError('CONTINUATION_CONFLICT: checkpoint changed during freeze; retry at a safe checkpoint')
    return raw


def _validate_continuation(row, role, path):
    if (not isinstance(row, dict) or row.get('schema_version') != 1 or row.get('role') != role
            or row.get('status') not in {'PENDING', 'HANDLED'} or not SPEC_FIELDS <= set(row)
            or row.get('runnable') is not True
            or any(not isinstance(row.get(key), str) or not row[key].strip()
                   for key in SPEC_FIELDS-{'runnable'} | {'checkpoint_sha256', 'input_sha256'})):
        raise ValueError('CONTINUATION_CONFLICT: invalid stored continuation')
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}', row['continuation_id'])
            or path.name != hashlib.sha256(row['continuation_id'].encode()).hexdigest()+'.json'
            or not re.fullmatch(r'[a-f0-9]{64}', row['checkpoint_sha256'])
            or row['input_sha256'] != _fingerprint(row)):
        raise ValueError('CONTINUATION_CONFLICT: stored continuation fingerprint changed')
    if ('checkpoint_snapshot' in row
            and row['checkpoint_snapshot'] != _snapshot_source(row['checkpoint_sha256'])):
        raise ValueError('CONTINUATION_CONFLICT: stored snapshot identity changed')
    return row


def _current_checkpoint(root, role, task, attempt):
    current = read_json(root/f'inbox/control/runtime/checkpoints/{role}.json', {}) or {}
    from requirement_scope import task_scope,task_allowed,restore_scope_checkpoint
    scope=task_scope(root)
    if scope and not task_allowed(scope,current.get('task')):
        current,_=restore_scope_checkpoint(root,role,task,attempt)
    if (current.get('schema_version') != 1 or current.get('role') != role
            or current.get('task') != task or current.get('attempt') != attempt):
        raise ValueError('CONTINUATION_CONFLICT: current role task or attempt changed')
    return current


def pending_continuations(project, role):
    root = Path(project).resolve()
    if role not in {'COORDINATOR', 'A', 'B', 'C', 'TESTER'}:
        raise ValueError('invalid continuation role')
    result = []
    for path in sorted((root/f'inbox/control/runtime/continuations/{role}').glob('*.json')):
        row = _validate_continuation(read_json(path), role, path)
        if row.get('status') == 'PENDING':
            # Keep an unresolved legacy input visible without launching the same
            # impossible action every five minutes. This is a projection only:
            # the original intent/fingerprint/PENDING row is never consumed.
            try:
                _frozen_input(root, row)
                availability = {'runnable_now': True, 'resume_state': 'READY'}
            except ValueError:
                availability = {'runnable_now': False, 'resume_state': 'INPUT_RECOVERY_REQUIRED',
                                'resume_error': '原冻结输入缺失或变化；须以精确原文哈希恢复，不能重跑或自动确认完成'}
            result.append({**row, **availability, 'source': str(path.relative_to(root))})
    return result


def publish_continuation(project, role, spec, token_file):
    allowed = SPEC_FIELDS
    if not isinstance(spec, dict) or set(spec) != allowed or spec.get('runnable') is not True:
        raise ValueError('continuation requires explicit runnable next action, not polling/waiting')
    if any(not isinstance(spec[k], str) or not spec[k].strip() for k in allowed-{'runnable'}):
        raise ValueError('continuation identity and action are required')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}', spec['continuation_id']):
        raise ValueError('invalid continuation identity')
    if len(json.dumps(spec).encode()) > 16*1024:
        raise ValueError('continuation too large; reference checkpoint evidence')
    with role_guard(project, role, token_file) as (root, _):
        from requirement_scope import require_task_scope
        require_task_scope(root,spec['task'])
        key = hashlib.sha256(spec['continuation_id'].encode()).hexdigest()
        path = root/f'inbox/control/runtime/continuations/{role}/{key}.json'
        old = read_json(path)
        if old:
            _validate_continuation(old, role, path)
            if any(old.get(field) != spec[field] for field in SPEC_FIELDS):
                raise ValueError('CONTINUATION_CONFLICT: frozen checkpoint or next action changed')
            if old['status'] != 'HANDLED':
                _current_checkpoint(root, role, spec['task'], spec['attempt'])
                _frozen_input(root, old)
            return old
        _current_checkpoint(root, role, spec['task'], spec['attempt'])
        evidence = canonical(root, spec['checkpoint'])
        if not evidence.is_file(): raise ValueError('checkpoint evidence absent')
        raw = _read_current_input(evidence)
        frozen = {**spec, 'checkpoint_sha256': hashlib.sha256(raw).hexdigest()}
        snapshot = _freeze_input(root, raw, frozen['checkpoint_sha256'])
        fingerprint = _fingerprint(frozen)
        row = {**frozen, 'schema_version': 1, 'role': role, 'input_sha256': fingerprint,
               'checkpoint_snapshot': snapshot, 'status': 'PENDING', 'recorded_at': now()}
        save_json(path, row)
        return row


def recover_continuation_input(project, role, identifier, evidence, token_file, *, expected_sha256):
    """Repair legacy input storage only with a byte-exact original; no task ACK."""
    with role_guard(project, role, token_file) as (root, _):
        key = hashlib.sha256(identifier.encode()).hexdigest()
        path = root/f'inbox/control/runtime/continuations/{role}/{key}.json'
        row = read_json(path)
        if not row or row.get('continuation_id') != identifier:
            raise ValueError('unknown continuation')
        _validate_continuation(row, role, path)
        from requirement_scope import require_task_scope
        require_task_scope(root,row['task'])
        if row['status'] != 'PENDING' or expected_sha256 != row['input_sha256']:
            raise ValueError('CONTINUATION_CONFLICT: recover requires exact pending input fingerprint')
        original = _read_current_input(canonical(root, evidence))
        source = _freeze_input(root, original, row['checkpoint_sha256'])
        row.update(checkpoint_snapshot=source, input_recovered_at=now(),
                   input_recovered_from=str(canonical(root, evidence).relative_to(root)))
        save_json(path, row)
        return row


def acknowledge_continuation(project, role, identifier, evidence, token_file, *, expected_sha256):
    with role_guard(project, role, token_file) as (root, _):
        key = hashlib.sha256(identifier.encode()).hexdigest()
        path = root/f'inbox/control/runtime/continuations/{role}/{key}.json'
        row = read_json(path)
        if not row or row.get('continuation_id') != identifier:
            raise ValueError('unknown continuation')
        _validate_continuation(row, role, path)
        from requirement_scope import require_task_scope
        require_task_scope(root,row['task'])
        if expected_sha256 != row['input_sha256']:
            raise ValueError('CONTINUATION_CONFLICT: acknowledge requires exact frozen input fingerprint')
        frozen_checkpoint, _ = _frozen_input(root, row)
        proof = canonical(root, evidence)
        if not proof.is_file(): raise ValueError('completion evidence absent')
        proof_hash = _hash(proof)
        if row['status'] == 'HANDLED':
            if str(proof.relative_to(root)) != row['evidence'] or proof_hash != row['evidence_sha256']:
                raise ValueError('CONTINUATION_CONFLICT: handled evidence changed')
            return row
        _current_checkpoint(root, role, row['task'], row['attempt'])
        if proof == frozen_checkpoint or proof == canonical(root, row['checkpoint']) or proof_hash == row['checkpoint_sha256']:
            raise ValueError('completion proof must describe the completed action separately from its input checkpoint')
        if proof.stat().st_size > 1024*1024:
            raise ValueError('completion proof too large; supply a concise task receipt')
        raw = proof.read_bytes()
        content = raw.decode()
        if hashlib.sha256(raw).hexdigest() != proof_hash:
            raise ValueError('CONTINUATION_CONFLICT: completion proof changed during read')
        if any(not re.search(r'(?<![A-Za-z0-9_.-])'+re.escape(row[key])+r'(?![A-Za-z0-9_.-])', content)
               for key in ('task', 'attempt')):
            raise ValueError('completion proof must identify the exact task and attempt')
        row.update(status='HANDLED', handled_at=now(), evidence=str(proof.relative_to(root)), evidence_sha256=proof_hash)
        save_json(path, row)
        return row


def _brief(value, limit=600, items=12, depth=0):
    if depth > 3: return '（按 source 读取嵌套原件）'
    if isinstance(value, str):
        raw = value.encode()
        return value if len(raw) <= limit else raw[:limit].decode(errors='ignore')+'…（按 source 读取完整原件）'
    if isinstance(value, dict): return {str(k)[:100]: _brief(v, limit, items, depth+1) for k, v in list(value.items())[:items]}
    if isinstance(value, list): return [_brief(v, limit, items, depth+1) for v in value[:items]]
    return value


def _selected(value, keys):
    return {key: value[key] for key in keys if key in value}


def _recovery_instructions(root, role, checkpoint, continuations, control):
    """Give concrete recovery argv, never ownership or a completed-action ACK.

    Only the deterministic, project-confined hash-named candidate is considered.
    A descriptive legacy filename or arbitrary receipt cannot become an input
    recovery candidate through a model guess or an unrestricted directory scan.
    """
    blocked = [item for item in continuations if item.get('resume_state') == 'INPUT_RECOVERY_REQUIRED']
    if not blocked:
        return {'state': 'NOT_REQUIRED'}
    matching = lambda item: (checkpoint.get('schema_version') == 1 and checkpoint.get('role') == role
                            and all(checkpoint.get(key) == item.get(key) for key in ('task', 'attempt')))
    blocked.sort(key=lambda item: (not matching(item), item['continuation_id']))
    items = []
    scripts = Path(__file__).resolve().parent
    token_file = '<PRIVATE_CLAIM_JSON_0600>'
    for item in blocked[:4]:
        row = {**_selected(item, ('continuation_id', 'source', 'task', 'attempt', 'input_sha256',
                                  'checkpoint_sha256')), 'state': 'CANDIDATE_REQUIRED'}
        if not matching(item):
            row['state'] = 'CURRENT_TASK_ATTEMPT_CONFLICT'
            items.append(row)
            continue
        if control != 'ACTIVE':
            row['state'] = 'PROJECT_NOT_ACTIVE'
            items.append(row)
            continue
        source = 'inbox/control/recovery-candidates/'+item['checkpoint_sha256']+'.snapshot'
        try:
            candidate = canonical(root, source)
            if (root/source).is_symlink():
                raise ValueError('恢复候选不得为符号链接')
            raw = _read_current_input(candidate)
            digest = hashlib.sha256(raw).hexdigest()
            if digest != item['checkpoint_sha256']:
                row.update(state='CANDIDATE_HASH_MISMATCH', candidate_source=source,
                           candidate_sha256=digest)
                items.append(row)
                continue
            # The recovery tool deliberately refuses to overwrite a corrupted
            # immutable snapshot. Do not advertise an argv it cannot perform.
            snapshot = canonical(root, _snapshot_source(item['checkpoint_sha256']))
            if snapshot.exists() and snapshot.read_bytes() != raw:
                row.update(state='IMMUTABLE_SNAPSHOT_CONFLICT', candidate_source=source)
                items.append(row)
                continue
        except (OSError, ValueError) as exc:
            row.update(state='CANDIDATE_UNAVAILABLE', candidate_source=source,
                       reason=type(exc).__name__)
            items.append(row)
            continue
        row.update(state='READY_FOR_OWNER_INPUT_RECOVERY', candidate_source=source,
                   candidate_sha256=digest, candidate_bytes=len(raw),
                   recovery_argv_template=['python3', str(scripts/'runtime_tools.py'), 'continuation-recover-input',
                       '--project', str(root), '--role', role, '--id', item['continuation_id'],
                       '--evidence', source, '--token-file', token_file, '--fingerprint', item['input_sha256']])
        items.append(row)
    result = {'state': 'OWNER_RECOVERY_GUIDANCE', 'input_recovery_count': len(blocked), 'items': items,
              'authority': '只读生成，不授予执行权。仅原授权内当前cron worker按既有角色租约执行；只读群会话不得执行。',
              'boundary': '恢复只补原输入快照，PENDING不变；不重跑已交付动作，不自动ACK。完成证明仍按原规则单独核对。'}
    if any(item['state'] == 'READY_FOR_OWNER_INPUT_RECOVERY' for item in items):
        result.update(
            worker_identity_argv=['python3', str(scripts/'role_runtime.py'), 'worker-identity'],
            lease={
                'claim_argv_template': ['python3', str(scripts/'role_runtime.py'), 'claim', '--project', str(root),
                    '--role', role, '--owner', '<CURRENT_CRON_TURN_OWNER>', '--pid', '<VERIFIED_WORKER_PID>', '--ttl', '300'],
                'renew_argv_template': ['python3', str(scripts/'role_runtime.py'), 'renew', '--project', str(root),
                    '--role', role, '--pid', '<VERIFIED_WORKER_PID>', '--token', '<OWN_TOKEN_FROM_PRIVATE_CLAIM_JSON>', '--ttl', '300'],
                'token_file_template': token_file,
                'steps': '先worker-identity核verified=true，PID只用该结果。未持有租约用claim；仅当前owner/PID/start一致且已有自己私存token时renew。claimed=false或身份不明即停，不借旧token、不省略PID。',
                'private_storage': 'claim成功且claimed=true后，完整JSON以0600保存在本worker私有scratch并将路径填入PRIVATE_CLAIM_JSON_0600；不打印token、不写群或共享文档。renew仅读自己的私存token且不打印；其简短结果不能覆盖原claim JSON。'},
            execution_order=['worker_identity_argv', 'lease.claim_argv_template 或同owner的lease.renew_argv_template',
                             '新claim成功时私存JSON为0600 token-file；同owner续期沿用自己的私存claim JSON',
                             'items[*].recovery_argv_template'])
    return result


def build_context(project, role):
    import os
    from workflow_runtime import active, current_input
    pool_path=os.environ.get('HERMES_ROLE_POOL_PATH')
    if pool_path and active(pool_path):
        return current_input(pool_path,role)
    root = Path(project).resolve()
    binding = read_json(root/'ROLE-BINDINGS.json')
    if not binding or Path(binding['project_root']).resolve() != root or role not in binding['roles']:
        raise ValueError('invalid role binding')
    base = root/'inbox/control/runtime'
    observer = read_json(base/f'observer/{role}.json', {}) or {}
    checkpoint = read_json(base/f'checkpoints/{role}.json', {}) or {}
    dispatches = read_json(base/f'dispatch-index/{role}.json', {}) or {}
    from requirement_scope import task_scope,task_allowed,checkpoint_for_scope
    scope=task_scope(root)
    parked_checkpoint=checkpoint if checkpoint and not task_allowed(scope,checkpoint.get('task')) else None
    checkpoint,checkpoint_source=checkpoint_for_scope(root,role,checkpoint,scope)
    paths = ['ROLE-BINDINGS.json', 'inbox/PROTOCOL.md', 'TODO.md', 'HANDOFF.md', binding['roles'][role]['session_file']]
    versions = {}
    for relative in paths:
        path = canonical(root, relative)
        versions[relative] = {'exists': path.is_file(), **({'sha256': _hash(path), 'bytes': path.stat().st_size} if path.is_file() else {})}
    references = Path.home()/'.codex/skills/complex-task-coordinator/references'
    for name in ('runtime-helpers.md', 'reporting-policy.md', 'script-observer.md'):
        path = references/name
        if path.is_file(): versions[str(path)] = {'sha256': _hash(path), 'bytes': path.stat().st_size}
    pending = observer.get('pending') or {}
    sources = pending.get('changed_sources', [])
    continuations = pending_continuations(root, role)
    excluded_continuations=sum(not task_allowed(scope,row.get('task')) for row in continuations)
    continuations=[row for row in continuations if task_allowed(scope,row.get('task'))]
    changed = {item.get('source') if isinstance(item, dict) else str(item) for item in sources}
    entries = sorted(dispatches.get('entries', []), key=lambda row: (
        row.get('task') != checkpoint.get('task'), row.get('attempt') != checkpoint.get('attempt'),
        row.get('source') not in changed, str(row.get('source', ''))))
    excluded_dispatches=sum(not task_allowed(scope,row.get('task')) for row in entries)
    entries=[row for row in entries if task_allowed(scope,row.get('task'))]
    related_tasks = {row.get('task') for row in entries if row.get('task')}
    if checkpoint.get('task'): related_tasks.add(checkpoint['task'])
    task_index = []
    if (root/'TODO.md').is_file():
        from progress_projection import parse_task_rows, TERMINAL
        tasks = parse_task_rows((root/'TODO.md').read_text())
        if role == 'COORDINATOR':
            related_tasks.update(task for task, row in tasks.items() if row['state'] not in TERMINAL and task_allowed(scope,task))
        for task in sorted(related_tasks, key=lambda task: (task != checkpoint.get('task'), task)):
            row = tasks.get(task)
            if row:
                task_index.append(_brief(_selected(row, ('task', 'title', 'state', 'owner', 'dependencies', 'line'))))
    result = {'schema_version': 1, 'kind': 'role-current-context', 'project': str(root), 'role': role,
            'control': binding.get('control'), 'observed_at': now(),
            'binding': _brief(_selected(binding['roles'][role], ('skill', 'session_file', 'worktree', 'chat_id', 'report_policy'))),
            'checkpoint': _brief(_selected(checkpoint, ('schema_version', 'role', 'task', 'attempt', 'status', 'base',
                'head', 'worktree', 'dispatch', 'event_id', 'evidence', 'next', 'pending_actions', 'open_gates',
                'references', 'recorded_at', 'slot_released', 'slot_release')), items=20),
            'checkpoint_source': checkpoint_source or str(base/f'checkpoints/{role}.json'),
            'related_tasks': task_index[:20], 'related_task_count': len(task_index),
            'task_source': 'TODO.md（按line定位当前任务，历史状态叙事不作为本轮行动依据）',
            'dispatch_index': {**_brief(_selected(dispatches, ('schema_version', 'role', 'observed_at', 'errors', 'active_operations', 'verified_no_task'))),
                'entries': [_brief(_selected(row, ('source', 'task', 'attempt', 'status', 'base', 'head', 'sha256'))) for row in entries[:12]],
                'entry_count': len(entries), 'full_source_index': str(base/f'dispatch-index/{role}.json')},
            'pending': {**_brief(_selected(pending, ('fingerprint', 'observed_at', 'requested_at', 'status', 'reason', 'event_id'))),
                        'changed_sources': _brief(sources[:12]), 'changed_source_count': len(sources),
                        'full_source_index': str(base/f'observer/{role}.json')},
            'continuations': _brief([_selected(item, ('continuation_id', 'source', 'input_sha256',
                'runnable_now', 'resume_state', 'resume_error', 'task', 'attempt', 'checkpoint_snapshot',
                'checkpoint', 'checkpoint_sha256', 'status', 'next')) for item in continuations[:12]], items=16),
            'continuation_count': len(continuations),
            'recovery_instructions': _recovery_instructions(root, role, checkpoint, continuations, binding.get('control')),
            'versions': versions,
            'instructions': '日常只读最新活动文档和当前有效派单/回执，不加载归档全文。同会话版本未变不重读参考全文；压缩/新会话重新加载。处理完业务写入后重新poll，核对新增输入，再ack准确指纹。未完成的可执行动作在安全检查点通过唯一continuation接续；纯等待不建续跑。'}
    if scope:
        result['requirement_scope']={**scope,'excluded_dispatches':excluded_dispatches,
            'excluded_continuations':excluded_continuations,
            'parked_checkpoint':({'task':parked_checkpoint.get('task'),'source':str(base/f'checkpoints/{role}.json')} if parked_checkpoint else None)}
    if role == 'COORDINATOR' and binding['roles'][role].get('coordinated_projects'):
        from coordination_inputs import collect_coordinated_inputs, context_view
        import role_observer
        coordinated = collect_coordinated_inputs(root, binding, role_observer.collect)
        if coordinated['projects']:
            result['coordinated_inputs'] = context_view(coordinated)
    if len(json.dumps(result, ensure_ascii=False).encode()) > CONTEXT_LIMIT:
        for key in ('checkpoint', 'binding', 'continuations'):
            result[key] = _brief(result[key], limit=180, items=8)
        result['dispatch_index']['entries'] = _brief(result['dispatch_index']['entries'][:4], limit=180, items=8)
        result['pending']['changed_sources'] = _brief(sources[:4], limit=180, items=8)
        result['related_tasks'] = _brief(task_index[:8], limit=180, items=8)
    if len(json.dumps(result, ensure_ascii=False).encode()) > CONTEXT_LIMIT:
        raise ValueError('current context exceeds 32KiB; shorten current checkpoint and source paths')
    return result
