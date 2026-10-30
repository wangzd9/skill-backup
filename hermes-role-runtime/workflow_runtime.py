"""Work-bound adapters for the redesign; activation stays behind migration gates.

The ledger is the only business state. This module joins its transactions to
native execution identity, existing observations and the existing operation runner.
An absent ledger is a read-only LEGACY decision, never an implicit migration.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import tempfile
import uuid
import workflow_diagnostics as diagnostics

ROLES = ('COORDINATOR', 'A', 'B', 'C', 'TESTER')
READ_TOOLS = frozenset({'read_file', 'search_files', 'skill_view', 'skills_list',
                        'role_live_progress', 'role_repo_state', 'role_requirement_catalog', 'role_work_input', 'role_decision_context',
                        'tool_search', 'tool_describe'})
COMMAND_TOOLS = {'COORDINATOR': 'role_coordination_command', 'TESTER': 'role_test_command',
                 'A': 'role_development_command', 'B': 'role_development_command',
                 'C': 'role_development_command'}
COMMANDS = {'COORDINATOR': frozenset({'publish_plan', 'decide', 'checkpoint', 'safe_release',
                                    'service_submit', 'service_collect', 'service_decide', 'merge'}),
            'TESTER': frozenset({'verify', 'close_verification', 'checkpoint', 'safe_release'}),
            'A': frozenset({'deliver', 'checkpoint', 'safe_release'}),
            'B': frozenset({'deliver', 'checkpoint', 'safe_release'}),
            'C': frozenset({'deliver', 'checkpoint', 'safe_release'})}
LABELS = {'UNKNOWN': '待核实', 'PAUSED': '暂停', 'BLOCKED': '阻塞',
          'WORKING': '工作中', 'WAITING': '等待', 'IDLE': '空闲'}


def ledger_path(pool_path):
    return Path(pool_path).resolve().with_suffix('.runtime') / 'business.sqlite3'


def open_ledger(pool_path, *, readonly=True):
    from business_ledger import BusinessLedger
    return BusinessLedger(ledger_path(pool_path), readonly=readonly)


def mode(pool_path):
    path = ledger_path(pool_path)
    if not path.exists():
        return {'mode': 'LEGACY', 'generation': 0, 'current_run_id': None}
    # Corrupt/inaccessible sources must block admission, not silently enable legacy.
    return open_ledger(pool_path).mode()


def active(pool_path):
    return mode(pool_path)['mode'] == 'ACTIVE'


def identity_from_scope(scope):
    required = ('role', 'run_id', 'work_id', 'execution_id', 'execution_generation', 'generation')
    if not isinstance(scope, dict) or any(key not in scope for key in required):
        raise ValueError('WORK_IDENTITY_UNVERIFIED')
    if scope['role'] not in ROLES or scope.get('kind') != 'business':
        raise ValueError('BUSINESS_WORK_REQUIRED')
    return {key: scope[key] for key in (*required, 'task_id', 'attempt') if key in scope}


class GuardRejected(ValueError):
    """Compatibility code string plus the unabridged original decision receipt."""
    def __init__(self, result):
        self.result = result
        self.decision = result.get('decision')
        super().__init__(result.get('code', 'WORK_IDENTITY_UNVERIFIED'))


def rejected(scope, code, *, operation=None, capability=None, phase='TOOL_PREFLIGHT',
             side_effect_state='NONE', result=None, capture=None, command_id=None):
    result = dict(result or {'status': 'REJECTED', 'code': code})
    with diagnostics.operation_context(scope, operation=operation, capability=capability):
        if not result.get('decision'):
            result['decision'] = diagnostics.record_result(ledger_path(scope['pool_path']), result,
                capture or result.get('capture'), operation=operation, command_id=command_id,
                phase=phase, side_effect_state=side_effect_state)
    return GuardRejected(result)


def guard(scope, *, allow_drain=False, operation=None, capability=None, operation_id=None,
          tool_call_id=None, phase='TOOL_PREFLIGHT', side_effect_state='NONE'):
    with diagnostics.operation_context(scope, operation=operation, capability=capability,
            operation_id=operation_id, tool_call_id=tool_call_id, phase=phase,
            side_effect_state=side_effect_state):
        try:
            ledger = open_ledger(scope['pool_path'])
            result = ledger.validate_identity(identity_from_scope(scope), allow_drain=allow_drain)
        except (OSError, sqlite3.Error, TypeError, KeyError) as exc:
            raise rejected(scope, 'BUSINESS_SOURCE_UNAVAILABLE', operation=operation,
                capability=capability, phase=phase, side_effect_state=side_effect_state,
                result={'status': 'UNVERIFIED', 'code': 'BUSINESS_SOURCE_UNAVAILABLE',
                        'source_error': type(exc).__name__}) from exc
        except ValueError as exc:
            result = {'status': 'UNVERIFIED', 'code': str(exc)} if str(exc) == 'UNSUPPORTED_BUSINESS_SCHEMA' else None
            raise rejected(scope, str(exc), operation=operation, capability=capability,
                phase=phase, side_effect_state=side_effect_state, result=result) from exc
        if result.get('valid') is not True:
            raise rejected(scope, result.get('code', 'WORK_IDENTITY_UNVERIFIED'), result=result,
                operation=operation, capability=capability, phase=phase,
                side_effect_state=side_effect_state)
    return result


def prepare(job, *, pool_path, execution, home, session_id):
    """Called under native admission lock after PID/start and job are verified."""
    role = job['role_pool']['role']
    from workflow_sessions import policy as session_policy
    limits=session_policy(home)
    if role not in ROLES:
        raise ValueError('ROLE_NOT_IN_RUNTIME')
    pool = json.loads(Path(pool_path).read_text())
    binding = pool.get('roles', {}).get(role, {})
    if binding.get('poll_job_id') != job['id']:
        raise ValueError('ROLE_JOB_ID_MISMATCH')
    ledger = open_ledger(pool_path, readonly=False)
    state = ledger.mode()
    if state['mode'] != 'ACTIVE':
        raise ValueError('WORKFLOW_NOT_ACTIVE')
    recover_executions(pool_path)
    for request in ledger.snapshot()['controls']:
        if request['state']=='ACCEPTED':
            ledger.apply_control(request['request_id'],command_id='control-apply:'+request['request_id'])
    if not ledger.admission_hint(role)['can_start_model']:
        return None
    identifier = execution['execution_id']
    pid = execution['pid']
    cgroup = (Path('/proc') / str(pid) / 'cgroup').read_text()
    result = ledger.bind_execution(role, {
        'execution_id': identifier, 'pid': pid,
        'process_start': execution['process_start'], 'scope': cgroup,
        'generation': state['generation'],
    }, command_id='native-bind:' + identifier)
    if result.get('original_status', result.get('status')) != 'COMMITTED':
        raise GuardRejected(result)
    work = ledger.current_work(role)
    if not work or work.get('active_execution_id') != identifier:
        raise rejected({'pool_path':str(pool_path),'role':role,'generation':state['generation'],
                        'execution_id':identifier}, 'ROLE_BINDING_UNVERIFIED',
                       operation='bind_execution', capability='execution_admission')
    snapshot = ledger.snapshot(work['run_id'])
    run = next(row for row in snapshot['runs'] if row['run_id'] == work['run_id'])
    from workflow_migration import code_version
    scope = {'schema_version': 2, 'workflow_version': 1, 'pool_path': str(Path(pool_path).resolve()),
             'generation': state['generation'], 'requirement_id': run['requirement_id'],
             'requirement_root': run['requirement_root'], 'project_root': run['requirement_root'],
             'role': role, 'run_id': work['run_id'], 'work_id': work['work_id'],
             'task_id': work.get('task_id'), 'attempt': work.get('attempt'),
             'execution_id': identifier, 'execution_generation': work['execution_generation'],
             'job_id': job['id'], 'session_id': session_id, 'kind': 'business',
             'home':str(Path(home).resolve()),
             'runtime_version_digest': code_version()['digest'],
             'drain_only': result.get('drain_only', False)}
    scope['session_policy']=limits
    issued = ledger.work_inputs(result['identity'], limit=100, command_id='native-input:' + identifier)
    packet = current_input(pool_path, role, scope=scope, event_page=issued)
    from workflow_toolsets import configure
    toolsets = configure(role)
    # The supplied worktree is chosen by the accepted specification, never cwd history.
    task = packet.get('task') or {}
    workdir = task.get('contract', {}).get('worktree') or run['repo']
    if role in {'A','B','C'}:
        import subprocess
        destination=ledger.path.parent/'workspaces'/work['work_id']
        if not destination.exists():
            destination.parent.mkdir(parents=True,exist_ok=True)
            subprocess.run(['git','clone','--shared','--no-checkout','--no-hardlinks',
                            run['repo'],str(destination)],check=True,capture_output=True,timeout=30)
            subprocess.run(['git','-C',str(destination),'checkout','--detach',task['base']],
                           check=True,capture_output=True,timeout=30)
        workdir=str(destination)
        packet['worktree']=workdir
        scope['worktree']=workdir
        scope['write_scope']=task['scope']
    if not Path(workdir).is_dir():
        raise ValueError('WORKTREE_UNAVAILABLE')
    scoped = dict(job, script=None, workdir=str(workdir), _role_pool_scope=scope,
                  _role_pool_session_id=session_id,
                  enabled_toolsets=toolsets)
    seal_input(packet)
    from workflow_tool_bridge import usage
    scoped['prompt'] = (
        '使用简体中文处理当前工作单。身份、事件与领取已由程序维护。'
        '只在指定范围内处理本单；用本角色专用工具提交计划、检查点、交付或验证。'
        '不得手写旧租约、ACK、slot-release或continuation。'
        '工具和子进程尚未结束时不得声称写入已结束。未知外部副作用先核查。'
        '冻结AC、独立测试、OCR、适用强审、批准及生产授权继续生效。'
        '技术会话有安全交接阈值：到阈值时保存真实 checkpoint 并设置 yield_execution=true，然后结束回合；'
        '下一回合保留同一 work/attempt 接续，不因会话轮换放宽验收或重放未知副作用。'
        '开发接续先核当前HEAD/dirty和上次checkpoint的具体next，复用已定位的范围与结论，只展开当前实现所需代码及变化材料。'
        '不要每轮从头通读全部合同和全部scope；未变化的材料按需回查。完成一个可验证的最小实现后及时保存真实改动与定位。'
        '交接checkpoint记录已读材料的路径/哈希、已确认接口、代码进度、未决点和下一处文件/函数；不把重复核验说成开发成果。'
        'candidate_approvals 表示已记录批准，CURRENT 不等于尚未批准；按 next_actions 当前阶段推进，不重复 APPROVE。'
        '检查点正文只放业务事实；processed_through/yield_execution 必须放 payload 外层，检查回执中的实际水位和yielded。'
        '先处理已发行事件，再用 ack_events(through) 或外层 processed_through 确认；不能跳过未发行前缀。'
        '未确认时续读下一页用 paging.next_after；历史回读用 role_work_input history=true，不能用于 ACK。'
        'requires_paging 仅为体积提示；事件续读看 paging.events_may_have_more，反复翻旧页不能缩减非事件字段。'
        'detail_artifacts 为未展开的完整证据；waiting_external 只在仅余外部授权或结果时按建议保存。'
        '事件续读用role_work_input events_only=true；首次或状态变化需要完整快照时省略该参数。'
        '待决目录和按对象材料用role_decision_context；contract按action取完整schema，prepare只预填ID不填裁决结论。'
        'same_as 是本包内相同原文的JSON Pointer，递归展开即可读取，不依赖上轮记忆；未展开归档不能声称已读。'
        '独立材料查询尽量在同轮并行；按路径/范围/哈希复用本人已核实结论，版本或环境改变即重新读取。'
        + usage(role,include_background=role=='COORDINATOR')+'\n'
        + json.dumps(packet, ensure_ascii=False, sort_keys=True))
    return scoped


def prepare_report(job, *, pool_path, execution, home, session_id):
    """Freeze attribution for a read-only report; it never acquires a work."""
    role=job['role_pool']['role']
    pool=json.loads(Path(pool_path).read_text())
    if role not in ROLES or job['id'] not in {pool['roles'][role].get('report_job_id'),
                                            pool['roles'][role].get('morning_report_job_id')}:
        raise ValueError('REPORT_ROLE_JOB_MISMATCH')
    ledger=open_ledger(pool_path)
    snapshot=ledger.snapshot()
    run=next((row for row in snapshot['runs'] if row['run_id']==snapshot['current_run_id']),None)
    if not run:
        return None
    from workflow_migration import code_version
    scope={'schema_version':2,'workflow_version':1,'pool_path':str(Path(pool_path).resolve()),
           'generation':snapshot['generation'],'run_id':run['run_id'],
           'requirement_id':run['requirement_id'],'requirement_root':run['requirement_root'],
           'project_root':run['requirement_root'],'role':role,'kind':'report',
           'execution_id':execution['execution_id'],'job_id':job['id'],'session_id':session_id,
           'home':str(Path(home).resolve()),
           'runtime_version_digest':code_version()['digest'],
           'drain_only':False}
    from workflow_toolsets import configure
    return dict(job,workdir=run['requirement_root'],_role_pool_scope=scope,
                _role_pool_session_id=session_id,enabled_toolsets=configure(role,readonly=True))


def recover_executions(pool_path):
    """Only verified ended scopes are cleared; unknown owners stay fenced."""
    ledger=open_ledger(pool_path,readonly=False)
    snapshot=ledger.snapshot()
    results=[]
    for execution in snapshot['executions']:
        if execution['state']=='ENDED':
            continue
        from cron.role_pool_scheduler import _process
        state,_=_process(execution['pid'],execution['process_start'])
        if state!='dead':
            continue
        identity={key:execution[key] for key in
                  ('role','work_id','execution_id','execution_generation','generation')}
        # A refused clearance is retried only with fresh observed facts. Reusing
        # its command id would cache the first UNKNOWN forever.
        observation=datetime.now(timezone.utc).isoformat()
        results.append(ledger.end_execution(identity,command_id='recover:'+execution['execution_id']+':'+observation))
    return results


def recover_execution_outcomes(pool_path,home):
    ledger=open_ledger(pool_path,readonly=False)
    source=Path(home)/'cron/executions.db'
    if not source.is_file():
        return []
    with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True,timeout=1) as db:
        terminal={row[0] for row in db.execute("SELECT id FROM executions WHERE status IN ('completed','failed','unknown')")}
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='execution_history'").fetchone():
            terminal.update(row[0] for row in db.execute('SELECT id FROM execution_history'))
    results=[]
    for execution in ledger.snapshot()['executions']:
        if (execution['state']!='ENDED' or execution['native_outcome']!='UNOBSERVED'
                or execution['execution_id'] not in terminal):
            continue
        # Rejected observations can be retried after native finalization. They
        # neither release writers nor invent a successful business outcome.
        results.append(ledger.observe_execution_outcome(execution['execution_id'],source,
            command_id='native-outcome:'+execution['execution_id']+':'+str(time.time_ns())))
    return results


def _now(at=None):
    value = at or datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise ValueError('TIMEZONE_REQUIRED')
    return value


def _process_observation(execution, at):
    if not execution:
        return {'state': 'NONE', 'checked_at': at.isoformat()}
    from cron.role_pool_scheduler import _process
    state, verified = _process(execution.get('pid'), execution.get('process_start'))
    return {'state': state.upper(), 'identity_verified': verified,
            'pid': execution.get('pid'), 'process_start': execution.get('process_start'),
            'checked_at': at.isoformat(), 'execution_id': execution.get('execution_id')}


def role_state(pool_path, role, *, at=None):
    """Six-state projection of current facts; query never updates business clocks."""
    if role not in ROLES:
        raise ValueError('ROLE_NOT_IN_RUNTIME')
    at = _now(at)
    ledger = open_ledger(pool_path)
    snapshot = ledger.snapshot()
    roles = snapshot.get('roles', [])
    row = (roles.get(role, {}) if isinstance(roles, dict) else
           next((item for item in roles if item.get('role') == role), {}))
    work = next((item for item in snapshot['works'] if item['work_id'] == row.get('current_work_id')), None)
    execution = next((item for item in snapshot['executions']
                      if item['execution_id'] == row.get('writer_execution_id')), {})
    if work and work.get('active_execution_id'):
        current_identity = {'role': role, 'work_id': work['work_id'], 'run_id': work['run_id'],
                            'execution_id': work['active_execution_id'],
                            'execution_generation': work['execution_generation'],
                            'generation': snapshot['generation']}
        checked = ledger.validate_identity(current_identity, allow_drain=True)
        execution = checked.get('execution') or execution
        valid = checked.get('valid') is True
    else:
        valid = True
        checked = {'valid': True, 'capture': None}
    observed = _process_observation(execution, at)
    run_id = work['run_id'] if work else snapshot.get('current_run_id')
    run = next((item for item in snapshot['runs'] if item['run_id'] == run_id), {})
    reasons = []
    reason_codes = [checked['code']] if checked.get('code') else []
    writer_fence = execution and (not work or execution.get('state') == 'DRAINING')
    task = next((item for item in snapshot['tasks'] if work and
                 (item['run_id'], item['task_id']) == (work['run_id'], work['task_id'])), None)
    identity_unverified = (not valid or observed['state'] == 'UNKNOWN' or writer_fence or
            work and work.get('active_execution_id') and observed['state'] != 'ALIVE' or
            task and task.get('phase') is None)
    control_blocked = (not snapshot.get('admission_open', True) or
        run.get('control') in {'PAUSED', 'CANCELLED', 'DRAINING'} or row.get('control') != 'ACTIVE')
    task_blocked = work and task and task.get('unresolved_reason')
    # Status has precedence; independent, observed gate reasons do not.
    if identity_unverified:
        reasons.append('执行身份或旧writer尚未核实')
        if not reason_codes:
            reason_codes.append('EXECUTION_OR_WRITER_UNVERIFIED')
    if control_blocked:
        reasons.append('控制状态停止新领取')
        reason_codes.append('CONTROL_BLOCKS_NEW_ACTION')
    if task_blocked:
        reasons.append('当前工作有明确阻断门禁')
        reason_codes.append('TASK_CONTROL_OR_RECONCILIATION_GATE')
    if identity_unverified:
        status = 'UNKNOWN'
    elif control_blocked:
        status = 'PAUSED'
    elif task_blocked:
        status = 'BLOCKED'
    elif work and work.get('active_execution_id') and observed['state'] == 'ALIVE':
        status = 'WORKING'
    elif work:
        status = 'WAITING'; reasons.append('当前工作保留，等待接续或结果')
    else:
        status = 'IDLE'
    after = ledger.mode()
    if any(snapshot.get(key) != after.get(key) for key in ('mode', 'generation', 'current_run_id', 'admission_open')):
        status = 'UNKNOWN'; reasons.append('采集期间运行范围变化')
        reason_codes.append('AUTHORITY_CHANGED_DURING_COLLECTION')
    diagnostic = diagnostics.read_projection(pool_path, role=role)
    return {'role': role, 'status': status, 'label': LABELS[status], 'available': status == 'IDLE',
            'task': (work or {}).get('task_id'), 'attempt': (work or {}).get('attempt'),
            'run_id': run_id, 'work_id': (work or {}).get('work_id'),
            'project_root': run.get('requirement_root'), 'observed_at': at.isoformat(),
            'process': observed, 'reasons': reasons, 'sources': [str(ledger_path(pool_path))],
            'reason_codes': list(dict.fromkeys(reason_codes)),
            'owner_process_state': {'state':observed['state'], 'checked_at':at.isoformat(),
                'source':'/proc', 'evidence_ref':observed.get('execution_id')},
            'business_authority_state': {'state':'VERIFIED' if valid else 'UNVERIFIED',
                'checked_at':at.isoformat(), 'source':str(ledger_path(pool_path)),
                'reason_codes':reason_codes, 'evidence_ref':(checked.get('capture') or {}).get('capture_id')},
            'heartbeat_state': {'state':'NOT_APPLICABLE', 'source':'work_execution_runtime',
                'checked_at':at.isoformat(), 'reason_codes':['TTL_NOT_USED_FOR_BUSINESS_AUTHORITY']},
            'activity_source_state': {'state':'NOT_CHECKED', 'source':'activity_lifecycle',
                'checked_at':None},
            'write_diagnostics': {key:diagnostic[key] for key in
                ('source_state','source','write_paths','anomalies','closures')},
            'checkpoint': (work or {}).get('checkpoint')}


def verified_input_view(directory, identifier, value, *, pretty=False):
    """Persist an immutable, private JSON view and verify it before reuse."""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2 if pretty else None).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink():
        raise ValueError('AUDIT_EVENT_VIEW_DIRECTORY_UNVERIFIED')
    os.chmod(directory, 0o700)
    path = directory / (str(identifier) + '-' + digest + '.json')
    if path.is_symlink():
        raise ValueError('AUDIT_EVENT_VIEW_UNVERIFIED')
    if path.exists():
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('AUDIT_EVENT_VIEW_HASH_MISMATCH')
    else:
        fd, temporary = tempfile.mkstemp(prefix='.view-', dir=directory)
        try:
            with os.fdopen(fd, 'wb') as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return {'path': str(path), 'sha256': digest, 'bytes': len(encoded)}


def project_events(ledger, events):
    """Project large authority audit envelopes, retaining original event identities.

    Task contracts, controls, failures and checkpoints are never trimmed here.
    The ledger remains authoritative; the private, content-addressed JSON is a
    readable full-event view, verified before every reuse, not an input ACK.
    """
    projected = []
    for event in events:
        manifest = event.get('payload', {}).get('manifest')
        encoded = json.dumps(event, ensure_ascii=False, sort_keys=True).encode()
        if (event['kind'] not in {'MODE_CHANGED', 'ADMISSION_OPENED'} or
                not isinstance(manifest, dict) or len(encoded) <= 8192):
            projected.append(event)
            continue
        reference = {'database': str(ledger.path), 'event_id': event['event_id'],
            **verified_input_view(ledger.path.parent / 'event-views', event['event_id'], event),
            'read_hint': '完整审计事件按需使用 read_file/search_files 分段读取；事件未被确认或删除。'}
        # Only bounded authority metadata is inlined. Complete gate proofs and
        # source inventories remain available in the verified full-event view.
        summary = {key: manifest[key] for key in
            ('ready', 'validated', 'mode', 'generation', 'manifest_sha256')
            if key in manifest and isinstance(manifest[key], (str, int, float, bool, type(None)))
            and len(str(manifest[key])) <= 256}
        version = manifest.get('version')
        if isinstance(version, dict):
            summary['version_digest'] = version.get('digest')
        value = dict(event)
        value['payload'] = {**event['payload'], 'manifest': {
            'projection': 'authority-audit-reference-v1', 'summary': summary,
            'omitted_manifest_fields': sorted(manifest), 'full_event': reference}}
        projected.append(value)
    from workflow_progression import event_view
    return event_view(projected)


def current_input(pool_path, role, *, scope=None, at=None, event_page=None, snapshot=None, model_view=True):
    at = _now(at)
    ledger = open_ledger(pool_path)
    selected=(scope or {}).get('run_id') or (ledger.current_work(role) or {}).get('run_id') or ledger.mode()['current_run_id']
    snapshot = ledger.snapshot(selected) if snapshot is None else snapshot
    row = next((item for item in snapshot['roles'] if item['role'] == role), None)
    if row is None:
        raise ValueError('ROLE_NOT_IN_RUNTIME')
    work = next((item for item in snapshot['works'] if item['work_id'] == row['current_work_id']), None)
    from workflow_query import checkpoint_history
    if work:
        work = {'checkpoint_history': checkpoint_history(work, snapshot['events']), **work}
    if scope:
        guard(scope, allow_drain=True)
        if not work or (scope.get('run_id'), scope.get('work_id')) != (work['run_id'], work['work_id']):
            raise ValueError('WORK_SCOPE_CHANGED')
    task = next((row for row in snapshot['tasks'] if work and row.get('run_id') == work['run_id']
                 and row.get('task_id') == work.get('task_id')), None)
    run_id = (work or {}).get('run_id') or snapshot.get('current_run_id')
    tasks = [item for item in snapshot['tasks'] if item['run_id'] == run_id and
             (role == 'COORDINATOR' or item['task_id'] == (work or {}).get('task_id'))]
    task_ids = {item['task_id'] for item in tasks}
    candidates = [row for row in snapshot.get('candidates', []) if
                  row.get('task_id') in task_ids and row.get('run_id') == run_id]
    ids = {row['candidate_id'] for row in candidates}
    current_candidates = [row['candidate_id'] for row in candidates
                          if row['state'] == 'CURRENT' and
                          (role == 'COORDINATOR' or row['attempt'] == (work or {}).get('attempt'))]
    checks = [row for row in snapshot.get('verifications', []) if row.get('candidate_id') in ids]
    events = event_page['events'] if event_page else [e for e in snapshot['events']
        if e['event_id']>(work or {}).get('processed_through',0) and e['event_id']<=snapshot['event_high_water']
        and e.get('run_id') in (None,run_id)][:100]
    through = events[-1]['event_id'] if events else (work or {}).get('processed_through', 0)
    packet = {'schema_version': 1, 'kind': 'current-work-input', 'checked_at': at.isoformat(),
              'mode': snapshot['mode'], 'generation': snapshot['generation'], 'role': role,
              'selected_run_id': snapshot['current_run_id'],
              'work': work, 'task': task, 'tasks': tasks, 'candidates': candidates, 'verifications': checks,
              'plans': [item for item in snapshot.get('plans', []) if item['run_id'] == run_id] if role == 'COORDINATOR' else [],
              'controls': [item for item in snapshot.get('controls', []) if item['target'].get('run_id') == run_id],
              'external_actions': [item for item in snapshot.get('external_actions', []) if item.get('candidate_id') in ids],
              'execution_history':[{key:item[key] for key in ('execution_id','state','native_outcome','writer_cleared')}
                                   for item in snapshot['executions'] if work and item['work_id']==work['work_id']],
              'current_candidate_ids': current_candidates,
              'events': project_events(ledger, events), 'event_high_water': snapshot['event_high_water'],
              'checkpoint': (work or {}).get('checkpoint'),
              'scope': scope, 'source': str(ledger_path(pool_path)),
              'authority': '已发布任务规格与业务账本；Markdown仅为导出视图',
              'archives_loaded': False, 'paging': {'events_limit': 100,
                'event_through': event_page.get('frozen_through', snapshot['event_high_water']) if event_page else snapshot['event_high_water'],
                'displayed_through': through,
                'issued_through': event_page.get('through') if event_page else None,
                'events_may_have_more': event_page.get('has_more', False) if event_page else len(events) == 100,
                'next_after':event_page.get('next_after',through) if event_page else through,
                'can_ack':bool(event_page and event_page.get('can_ack')),
                'ack_action':event_page.get('ack_action') if event_page else None},
              'run': next((item for item in snapshot['runs'] if item['run_id'] ==
                          ((work or {}).get('run_id') or snapshot.get('current_run_id'))), None)}
    accepted=snapshot['events']
    packet['accepted_requirements']=[row['payload'] for row in accepted
                                    if row['run_id']==run_id and row['kind']=='REQUIREMENT_ACCEPTED'][-1:]
    # Keep complete gates/specifications. Oversized inputs require explicit paging,
    # never a silent truncation of AC, FAIL, controls or checkpoint obligations.
    from workflow_progression import next_actions
    from workflow_supplemental import summary
    packet['supplemental_verifications']=summary(snapshot,run_id)
    from workflow_development_policy import facts as automation_facts
    from workflow_verification_lifecycle import facts as retirement_facts
    packet['local_development_automation']=automation_facts(snapshot,run_id)
    packet['verification_retirements']=retirement_facts(snapshot,run_id)
    from workflow_verification_exceptions import facts as exception_facts, blockers as exception_blockers
    packet['verification_exceptions']=exception_facts(snapshot,run_id)
    packet['mechanism_blockers']=exception_blockers(snapshot,run_id)
    packet['verification_exception_issues']=[e['payload'] for e in snapshot['events']
        if task and e['kind']=='VERIFICATION_EXCEPTION_REPAIR_QUEUED' and e['run_id']==run_id
        and e['payload']['task_id']==task['task_id'] and e['payload']['attempt']==task['attempt']]
    from workflow_completion import facts as completion_facts, context as completion_context, historical_failures
    from workflow_completion_repair import task_issues
    packet['completion']=completion_facts(snapshot,run_id)
    packet['final_acceptance_issues']=task_issues(snapshot,run_id,task)
    packet['historical_failures']=historical_failures(snapshot,run_id) if role=='COORDINATOR' else []
    if (work or {}).get('kind')=='integration_verification':
        packet['integration_validation']=completion_context(snapshot,work)
    packet['next_actions']=next_actions(packet,ledger,snapshot=snapshot)
    misplaced=sorted(set((work or {}).get('checkpoint',{}))&{'processed_through','issued_through','yield_execution'})
    if misplaced:
        packet['checkpoint_contract_warnings']={'code':'LEGACY_CHECKPOINT_CONTROL_FIELDS_IGNORED',
            'fields':misplaced,'processed_through':work['processed_through'],'issued_through':work['issued_through'],
            'instruction':'历史正文中的控制字段未生效；按paging连续读取并显式ack_events确认，不据正文跳水位。新checkpoint控制字段放payload外层。'}
    from workflow_wait import capabilities
    packet['runtime_capabilities']=capabilities(role)
    if (work or {}).get('checkpoint', {}).get('waiting_external'):
        packet['next_actions'].insert(0, {'action':'recheck_waiting_external',
            'reason':'先逐项只读核验历史等待是否仍成立；内部工具拒绝不等于缺用户授权；仅更新真实等待，不跳过验收/授权/dirty门禁。',
            'repository_tool':'role_repo_state'})
    from workflow_progression import approval_state
    packet['candidate_approvals']={c['candidate_id']:approval_state(snapshot,c)
                                   for c in candidates if c['state']=='CURRENT'}
    from workflow_decision_context import sampling, startup_index
    packet['sampling']=sampling(snapshot,packet)
    from workflow_work_memory import inspect as memory_inspect
    memory=memory_inspect(snapshot,work)
    if memory is not None:packet['work_memory_validation']=memory
    if not model_view:
        return packet
    packet['decision_context']=startup_index(ledger,packet,snapshot)
    from workflow_views import work_packet
    work_packet(ledger,packet)
    return seal_input(packet)


def seal_input(packet):
    """Hash the final model-visible fields after runtime workspace injection."""
    for key in ('input_sha256', 'input_bytes', 'requires_paging'):
        packet.pop(key, None)
    encoded = json.dumps(packet, ensure_ascii=False, sort_keys=True).encode()
    packet['input_sha256'] = hashlib.sha256(encoded).hexdigest()
    packet['input_bytes'] = len(encoded)
    packet['requires_paging'] = len(encoded) > 32 * 1024
    return packet


def selected_target(pool_path, target):
    """Resolve an exact registered requirement/run without moving role bindings."""
    snapshot=open_ledger(pool_path).snapshot()
    value=str(target).strip()
    matches=[row for row in snapshot['runs'] if value in
             {row['run_id'],row['requirement_id'],row['requirement_root']}]
    if len(matches)!=1:
        raise ValueError('EXACT_REGISTERED_RUN_REQUIRED')
    return {'run_id':matches[0]['run_id']}


def tool_decision(scope, tool_name, args=None, *, tool_call_id=None, operation_id=None):
    command_id = (args or {}).get('command_id') if isinstance(args, dict) else None
    identifier = operation_id or ('command:'+command_id if isinstance(command_id,str) and command_id
        else 'tool:'+str(tool_call_id) if tool_call_id else 'tool:'+uuid.uuid4().hex)
    action = (args or {}).get('action') if command_id else None
    capability = diagnostics.CAPABILITIES.get(action,action) if action else 'workspace_write' if tool_name in {'terminal', 'write_file', 'patch'} else tool_name
    with diagnostics.operation_context(scope, operation=tool_name, capability=capability,
            operation_id=identifier, tool_call_id=tool_call_id, phase='TOOL_PREFLIGHT', side_effect_state='NONE'):
        return _tool_decision(scope, tool_name, args)


def _tool_decision(scope, tool_name, args=None):
    if scope.get('kind') == 'report':
        if tool_name in READ_TOOLS:
            return None
        error = rejected(scope, 'REPORT_READ_ONLY', operation=tool_name, capability=tool_name)
        return {'action': 'block', 'message': '报告会话只读，不开放shell、测试、派单或审查。',
                'decision': error.decision}
    checked = guard(scope, operation=tool_name,
        allow_drain=tool_name in READ_TOOLS or tool_name == COMMAND_TOOLS.get(scope['role']))
    if tool_name in READ_TOOLS or tool_name == COMMAND_TOOLS[scope['role']]:
        return None
    # Ordinary development tools are still checked against the same execution
    # on every call; a yielded or superseded worker cannot keep writing.
    allowed = {'terminal', 'write_file', 'patch'}
    if scope['role'] in {'A', 'B', 'C'} and tool_name in allowed and not scope.get('drain_only'):
        guard(scope, operation=tool_name, capability='workspace_write')
        from workflow_sandbox import tool_directive
        try:
            return tool_directive(scope, tool_name, args or {})
        except ValueError as exc:
            raise rejected(scope, str(exc), operation=tool_name, capability='workspace_write',
                           capture=checked.get('capture')) from exc
    error = rejected(scope, 'ROLE_CAPABILITY_NOT_ALLOWED', operation=tool_name, capability=tool_name,
                     capture=checked.get('capture'))
    from workflow_recovery import guidance
    return {'action':'block','message':'当前角色不开放该工具；使用本角色command或role_decision_context查询合同。',
            'decision':error.decision,
            'recovery':guidance(scope,error.result,not_started=True)}


def command(scope, action, payload, *, command_id):
    """Identity is from the frozen native scope, never a model-supplied token."""
    with diagnostics.operation_context(scope, operation=action,
            capability=diagnostics.CAPABILITIES.get(action, action), command_id=command_id,
            operation_id='command:'+str(command_id), phase='TOOL_PREFLIGHT', side_effect_state='NONE'):
        try:
            result = _execute_command(scope, action, payload, command_id=command_id)
        except GuardRejected:
            raise
        except ValueError as exc:
            raise rejected(scope, str(exc), operation=action, command_id=command_id) from exc
    if result.get('status') == 'COMMITTED':
        try:
            request_business_wakeups(scope, result)
        except Exception as exc:
            result['wakeup_observation'] = {'state': 'DEGRADED', 'error': type(exc).__name__}
    return result


def _execute_command(scope, action, payload, *, command_id):
    guard(scope, allow_drain=action in {'checkpoint', 'safe_release','ack_events'})
    identity = identity_from_scope(scope)
    extra=({'extend_task_scope','service_resume','service_reject','refresh_task_base','refresh_development_base','complete','complete_requirement','append_development_log','commit_documentation','resolve_user_decision','environment_recovered','resolve_verification_exception'}
           if scope['role']=='COORDINATOR' else {'verify_supplemental','verify_integration','deliver_integration'} if scope['role']=='TESTER' else set())
    if action not in COMMANDS[scope['role']] and action not in extra and action!='ack_events':
        raise ValueError('COMMAND_NOT_ALLOWED_FOR_ROLE')
    if not isinstance(payload, dict):
        raise ValueError('PAYLOAD_MUST_BE_OBJECT')
    if any(key in payload for key in ('role', 'run_id', 'work_id', 'execution_id', 'generation', 'token')):
        raise ValueError('IDENTITY_IS_RUNTIME_BOUND')
    from workflow_contracts import validate_payload
    try:validate_payload(action, payload)
    except ValueError as exc:
        raise rejected(scope,str(exc),operation=action,command_id=command_id,result={
            'status':'REJECTED','code':str(exc),'phase':'INVALID_NOT_STARTED','side_effect_state':'NONE',
            'retryable':True,'new_command_id_required':False,
            'next_action':'参数未通过预检，未启动动作；按工具 schema 修正后可沿用 command_id。'}) from exc
    with diagnostics.operation_context(scope, operation=action,
            capability=diagnostics.CAPABILITIES.get(action, action), command_id=command_id,
            input_validated=True, capability_allowed=True):
        return _validated_command(scope, action, payload, identity, command_id=command_id)


def _validated_command(scope, action, payload, identity, *, command_id):
    ledger = open_ledger(scope['pool_path'], readonly=False)
    if action=='resolve_verification_exception':
        from workflow_verification_exceptions import resolve
        return resolve(ledger,identity,payload,command_id=command_id)
    if action in ('verify_integration','deliver_integration','complete_requirement'):
        from workflow_completion import verify, deliver, complete
        function={'verify_integration':verify,'deliver_integration':deliver,'complete_requirement':complete}[action]
        return function(ledger,identity,payload,command_id=command_id)
    if action=='environment_recovered':
        from workflow_issue_policy import environment_recovered
        return environment_recovered(ledger,identity,payload,command_id=command_id)
    if action=='resolve_user_decision':
        from workflow_user_decisions import resolve
        return resolve(ledger,identity,payload,command_id=command_id)
    if action=='verify_supplemental':
        from workflow_supplemental import verify
        return verify(ledger,identity,command_id=command_id)
    if action=='commit_documentation':
        from workflow_documentation import commit
        return commit(ledger,identity,payload,command_id=command_id)
    if action=='append_development_log':
        from workflow_coordination_log import append
        return append(ledger,identity,payload['content'],command_id=command_id,
            source_event_ids=payload.get('source_event_ids'))
    if action=='refresh_development_base':
        from workflow_integration_recovery import refresh_undelivered
        return refresh_undelivered(ledger,identity,payload['task_id'],payload['reason'],command_id=command_id)
    if action=='refresh_task_base':
        from workflow_integration_recovery import refresh_base
        return refresh_base(ledger,identity,payload['candidate_id'],payload['reason'],command_id=command_id)
    if action=='complete':
        from workflow_delivery import complete
        return complete(ledger,identity,payload['candidate_id'],payload['summary'],command_id=command_id)
    if action=='extend_task_scope':
        from workflow_autonomy import extend_scope
        return extend_scope(ledger,identity,payload['task_id'],payload['additions'],payload['reason'],command_id=command_id)
    if action=='service_reject':
        from workflow_service_recovery import reject
        return reject(ledger,identity,payload['action_id'],payload['reason'],command_id=command_id)
    if action=='service_resume':
        from workflow_service_recovery import resume
        return resume(ledger,identity,payload['action_id'],payload['new_action_id'],payload['reason'],
                      authorization=payload.get('authorization'),command_id=command_id)
    if action=='ack_events':
        return ledger.ack_events(identity,payload['through'],command_id=command_id)
    if action == 'checkpoint':
        return ledger.checkpoint(identity, payload.get('checkpoint', {}),
                                 processed_through=payload.get('processed_through'),
                                 yield_execution=payload.get('yield_execution',False), command_id=command_id)
    if action == 'safe_release':
        return ledger.release_work(identity, reason=payload['reason'], command_id=command_id)
    if action == 'publish_plan':
        return ledger.publish_plan(scope['run_id'], payload['version'], payload['tasks'],
                                   identity=identity, command_id=command_id)
    if action == 'deliver':
        return ledger.deliver(identity, payload['candidate'], payload['summary'], command_id=command_id)
    if action == 'decide':
        return ledger.decide(identity, payload['candidate_id'], payload['decision'],
                             evidence=payload.get('evidence'), command_id=command_id)
    if action == 'verify':
        from delivery_evidence import execute_candidate_tests
        return execute_candidate_tests(ledger, identity, payload['candidate_id'], payload['argv'],
            command_id=command_id, timeout_seconds=payload.get('timeout_seconds', 300),
            supersedes=payload.get('supersedes'), evidence=payload.get('evidence'),
            report_format=payload.get('report_format', 'pytest'))
    if action == 'close_verification':
        return ledger.close_verification(identity, payload['verification_id'], payload['supersedes'], command_id=command_id)
    if action.startswith('service_'):
        from workflow_services import submit_service, collect_service_result, adjudicate_service
        if action == 'service_submit':
            return submit_service(ledger,identity,payload['candidate_id'],payload['kind'],payload['request'],
                action_id=payload['action_id'],authorization=payload.get('authorization'),command_id=command_id)
        if action == 'service_collect':
            return collect_service_result(ledger,payload['action_id'],command_id=command_id)
        if action == 'service_decide':
            return adjudicate_service(ledger,identity,payload['action_id'],payload['result'],payload['summary'],
                command_id=command_id,supersedes=payload.get('supersedes'))
    if action == 'merge':
        from workflow_merge import merge_candidate
        authorization_id=payload.get('authorization_id')
        if not authorization_id:
            from workflow_merge_policy import authorization
            permitted=authorization(ledger,identity,payload['candidate_id'])
            if permitted.get('status') not in ('COMMITTED','DUPLICATE'):
                return permitted
            authorization_id=permitted['authorization_id']
        return merge_candidate(ledger,identity,payload['candidate_id'],authorization_id,command_id=command_id)
    raise ValueError('COMMAND_UNSUPPORTED')


def _control_source(ledger, role, action, target, primary):
    """Bound the model's parsed command by the user's own affirmative words."""
    from control_intents import clauses, UNSAFE
    if re.search(r'[?？`"“”>]|如果|假如|引用|转述|他说|她说', primary):
        raise ValueError('AMBIGUOUS_CONTROL_SOURCE')
    matches = []
    for index, clause in enumerate(clauses(primary, allow_unsafe=True)):
        if UNSAFE.search(clause):
            continue
        try:
            _control_clause(ledger, role, action, target, clause)
            matches.append(index)
        except ValueError:
            continue
    if len(matches) != 1:
        raise ValueError('CONTROL_ACTION_NOT_IN_PRIMARY_SOURCE')
    return matches[0]


def _control_clause(ledger, role, action, target, primary):
    verbs = {'pause': r'暂停|停止', 'resume': r'恢复|解除暂停', 'continue': r'继续|接着|推进|接续|起跑|启动',
             'cancel': r'取消|撤销任务', 'rework': r'返修|重新开发',
             'select_run': r'切换(?:到|至)?|转(?:到|至)?|选择(?:需求)?'}
    pattern = verbs.get(action)
    if not pattern or not re.match(r'^(?:请|帮我|现在|立即|将|把|对|为|先|\s)*(?:' + pattern + r')', primary):
        raise ValueError('CONTROL_ACTION_NOT_IN_PRIMARY_SOURCE')
    snapshot = ledger.snapshot()
    run = next((item for item in snapshot['runs'] if item['run_id'] == target.get('run_id')), None)
    explicit_run = run and any(value and value in primary for value in
                              (run['run_id'], run['requirement_id'], run['requirement_root']))
    current_words = any(word in primary for word in ('当前需求', '本需求', '当前run', '当前工作', '本角色', '当前任务', '当前候选'))
    if target.get('role') and re.search(r'(?<![A-Za-z0-9_])'+re.escape(target['role'])+r'(?![A-Za-z0-9_])', primary):
        current_words = True
    if not explicit_run and not (run and run['run_id'] == snapshot['current_run_id'] and current_words and action != 'select_run'):
        raise ValueError('CONTROL_RUN_NOT_IN_PRIMARY_SOURCE')
    if target.get('task_id') and target['task_id'] not in primary:
        raise ValueError('CONTROL_TASK_NOT_IN_PRIMARY_SOURCE')
    if target.get('candidate_id') and target['candidate_id'] not in primary:
        current = [item for item in snapshot['candidates'] if item['run_id'] == run['run_id'] and item['state'] == 'CURRENT']
        if not ('当前候选' in primary and len(current) == 1 and current[0]['candidate_id'] == target['candidate_id']):
            raise ValueError('CONTROL_CANDIDATE_NOT_IN_PRIMARY_SOURCE')
    if target.get('role'):
        named = re.search(r'(?<![A-Za-z0-9_])' + re.escape(target['role']) + r'(?![A-Za-z0-9_])', primary)
        if not named and not (role != 'COORDINATOR' and target['role'] == role and current_words):
            raise ValueError('CONTROL_ROLE_NOT_IN_PRIMARY_SOURCE')
    return target


def _authorization_limits(ledger, spec, primary, *, external):
    """A parsed payload may narrow consent, never manufacture its call budget."""
    if external:
        matches = re.findall(r'(?:max_calls\s*=\s*|最多\s*|限额?\s*)(\d+)(?:\s*次)?', primary)
        limits = list(map(int, matches))
        written = re.search(r'([一二两三四五1-5])\s*次', primary)
        if written:
            limits.append({'一':1, '二':2, '两':2, '三':3, '四':4, '五':5}.get(written.group(1),
                int(written.group(1)) if written.group(1).isdigit() else 1))
        allowed = min(limits) if limits else 1
        if type(spec.get('max_calls')) is not int or not 1 <= spec['max_calls'] <= min(allowed,1000 if spec.get('run_id') else 5):
            raise ValueError('AUTHORIZATION_CALL_LIMIT_NOT_IN_PRIMARY_SOURCE')
    explicit = re.search(r'expires_at\s*=\s*(\d+(?:\.\d+)?)', primary)
    minutes = re.search(r'有效(?:期)?\s*(\d+)\s*(分钟|小时)', primary)
    duration=int(minutes.group(1))*(3600 if minutes.group(2)=='小时' else 60) if minutes else 3600
    until = float(explicit.group(1)) if explicit else ledger.clock()+min(duration,72*3600 if spec.get('run_id') else 3600)
    if not isinstance(spec.get('expires_at'), (int, float)) or not ledger.clock() < spec['expires_at'] <= until:
        raise ValueError('AUTHORIZATION_EXPIRY_EXCEEDS_NATIVE_LIMIT')
    if spec.get('postmerge_argv'):
        literal = json.dumps(spec['postmerge_argv'], ensure_ascii=False)
        if literal not in primary:
            raise ValueError('POSTMERGE_COMMAND_NOT_IN_PRIMARY_SOURCE')


def submit_control(pool_path, role, action, target, provenance):
    from manual_access import full_permission_action
    from workflow_intake import scoped_control
    narrow=scoped_control(provenance,action)
    if role not in ROLES or not provenance.get('authenticated') or not (provenance.get('current_full') or narrow):
        raise ValueError('CURRENT_NATIVE_FULL_GRANT_REQUIRED')
    if not narrow and not full_permission_action(provenance.get('primary_text', '')):
        raise ValueError('SPECIFIC_NATIVE_ACTION_REQUIRED')
    ledger = open_ledger(pool_path, readonly=False)
    if ledger.mode()['mode'] != 'ACTIVE':
        raise ValueError('WORKFLOW_NOT_ACTIVE')
    if action == 'select_run' and role != 'COORDINATOR':
        raise ValueError('COORDINATOR_REQUIRED')
    command_id='message-control:'+provenance['chat_id']+':'+provenance['message_id']
    if action=='record_user_decision':
        if role!='COORDINATOR':
            raise ValueError('COORDINATOR_REQUIRED')
        from workflow_user_decisions import record
        return record(ledger,target or {},provenance,command_id=command_id)
    if action=='request_supplemental_verification':
        if role!='COORDINATOR':
            raise ValueError('COORDINATOR_REQUIRED')
        from workflow_supplemental import request
        return request(ledger,target or {},provenance,command_id=command_id)
    if action=='accept_requirement':
        if role!='COORDINATOR':
            raise ValueError('COORDINATOR_REQUIRED')
        from workflow_intake import accept
        return accept(ledger,target,provenance,command_id=command_id)
    if action in {'authorize_service','authorize_merge','revoke_authorization'}:
        if role != 'COORDINATOR':
            raise ValueError('COORDINATOR_REQUIRED')
        spec=dict(target or {})
        primary=full_permission_action(provenance['primary_text'])
        subject = primary.strip()
        if action == 'revoke_authorization':
            if not re.match(r'^(?:请\s*)?(?:撤销授权|取消授权|revoke)',subject,re.I) or not spec.get('authorization_id') or spec['authorization_id'] not in subject:
                raise ValueError('SPECIFIC_REVOCATION_SOURCE_REQUIRED')
            return ledger.revoke_authorization(spec['authorization_id'],provenance,command_id=command_id)
        # Native provenance is necessary, and a grant must also be explicit in
        # the current user's own words. A model's payload cannot create consent.
        if not re.match(r'^(?:请\s*)?(?:授权|authorize\b)',subject,re.I):
            raise ValueError('EXPLICIT_AUTHORIZATION_SOURCE_REQUIRED')
        if re.search(r'不要|不得|不授权|禁止|暂不|未经|不允许|\b(?:do not|not authorized|except)\b',primary,re.I):
            raise ValueError('AMBIGUOUS_AUTHORIZATION_SOURCE')
        if action == 'authorize_service':
            snapshot=ledger.snapshot()
            if spec.get('run_id'):
                run=next((row for row in snapshot['runs'] if row['run_id']==spec['run_id']),None)
                if (not run or run['run_id']!=snapshot['current_run_id']
                        or not any(value in subject for value in (run['run_id'],run['requirement_id'],'当前需求','本需求'))
                        or not any(value in subject for value in ('OCR','ocr') if spec.get('service_kind')=='ocr')
                        or not isinstance(spec.get('task_ids'),list)
                        or not all(value in subject for value in spec['task_ids']) and not any(value in subject for value in ('全部任务','所有任务','三路'))):
                    raise ValueError('EXPLICIT_RUN_TASK_SERVICE_POLICY_REQUIRED')
                _authorization_limits(ledger,spec,primary,external=True)
                return ledger.grant_external_authorization(spec,provenance,command_id=command_id)
            candidate=next((row for row in snapshot['candidates'] if row['candidate_id']==spec.get('candidate_id')),None)
            names={'ocr':('OCR','ocr'), 'strong_review':('强审','Codex','codex','strong_review')}
            if (not candidate or candidate['run_id']!=snapshot['current_run_id'] or
                    not any(word in subject for word in names.get(spec.get('service_kind'),())) or
                    not (candidate['candidate_id'] in subject or
                         '当前候选' in subject and len([row for row in snapshot['candidates'] if
                            row['run_id']==snapshot['current_run_id'] and row['state']=='CURRENT'])==1)):
                raise ValueError('SPECIFIC_SERVICE_CANDIDATE_GRANT_REQUIRED')
            _authorization_limits(ledger,spec,primary,external=True)
            return ledger.grant_external_authorization(spec,provenance,command_id=command_id)
        if not any(word in subject for word in ('合入','合并','merge')) or not spec.get('task_id') or not spec.get('branch') or spec['task_id'] not in subject or spec['branch'] not in subject:
            raise ValueError('SPECIFIC_MERGE_TASK_AND_BRANCH_GRANT_REQUIRED')
        _authorization_limits(ledger,spec,primary,external=False)
        return ledger.grant_merge_authorization(spec,provenance,command_id=command_id)
    current = ledger.current_work(role)
    target = dict(target or {})
    target.setdefault('run_id', (current or {}).get('run_id') or ledger.mode()['current_run_id'])
    if role != 'COORDINATOR':
        if target['run_id'] != ((current or {}).get('run_id') or ledger.mode()['current_run_id']):
            raise ValueError('CONTROL_RUN_OUTSIDE_CURRENT_ROLE')
        if target.get('task_id') or target.get('candidate_id') or action in {'rework','cancel'}:
            raise ValueError('ROLE_CONTROL_ONLY_PAUSE_RESUME_CONTINUE')
        target['role'] = role
    primary=full_permission_action(provenance['primary_text'])
    index = _control_source(ledger,role,action,target,primary if primary is not None else provenance['primary_text'])
    if index:
        accepted = {row['provenance'].get('source_clause', 0) for row in ledger.snapshot()['controls']
                    if row['provenance'].get('message_id') == provenance['message_id']
                    and row['provenance'].get('chat_id') == provenance['chat_id']}
        if not set(range(index)) <= accepted:
            raise ValueError('PRECEDING_CONTROL_ACTION_REQUIRED')
    source = {**provenance, 'source_clause': index}
    return ledger.control_request(action, target, source, command_id=command_id+':clause:'+str(index))


def dispatch_event(pool_path, role, *, home, dispatcher=None, observer=None, expected_scope=None):
    """Request the existing job; native claim and pause checks remain authoritative.

    The business event is already durable. A failed dispatch never consumes it;
    the original periodic job can find it again. This does not call trigger_job,
    whose manual semantics may clear a pause.
    """
    from workflow_dispatch import dispatch_event as run
    return run(pool_path, role, home=home, dispatcher=dispatcher,
               observer=observer, expected_scope=expected_scope)


def request_role_wakeup(pool_path, role, *, home, dispatcher=None,
                        source='event_tick', committed_at=None):
    from workflow_dispatch import request_role_wakeup as submit
    return submit(pool_path, role, home=home, dispatcher=dispatcher,
                  source=source, committed_at=committed_at)


def request_business_wakeups(scope, result):
    """Post-transaction hook: capture completion now, never from event.created_at."""
    import time
    if result.get('status') != 'COMMITTED':
        return None
    committed_at = {'at': time.time(), 'monotonic_ns': time.monotonic_ns()}
    event_id=result.get('event_id')
    if not event_id:
        return {'state':'NOT_APPLICABLE','reason':'NO_BUSINESS_EVENT'}
    events=open_ledger(scope['pool_path']).events(after=event_id-1,through=event_id,limit=1)
    if not events or events[0]['kind'] in {'CHECKPOINT_SAVED','EXECUTION_BOUND','EXECUTION_ENDED','EXECUTION_OUTCOME_OBSERVED',
                                         'TOOL_EXECUTION_REGISTERED','WORK_CLAIMED'}:
        return {'state':'NOT_APPLICABLE','reason':'MECHANICAL_EVENT'}
    committed_at['event_id']=event_id
    home = scope.get('home')
    if not home:
        from hermes_constants import get_hermes_home
        home = get_hermes_home()
    return {role: request_role_wakeup(scope['pool_path'], role, home=home,
            source='business_commit', committed_at=committed_at) for role in ROLES}


def collect_event_tick(pool_path, *, home, service_adapter=None):
    """Collect/recover under the original admission guard, without dispatching.

    No dispatch cursor is ACKed here. If the process exits after a transaction,
    the next native tick can observe the same pending fact. Native claims and
    ledger writer fences serialize the actual execution.
    """
    if not active(pool_path):
        return None
    ledger=open_ledger(pool_path,readonly=False)
    result={'controls':[], 'recovery':[], 'services':[], 'dispatch':{}, 'errors':[]}
    commits=[]
    def capture_commit(value):
        if value.get('status')=='COMMITTED' and value.get('event_id'):
            import time
            commits.append({'at':time.time(),'monotonic_ns':time.monotonic_ns(),
                            'event_id':value['event_id']})
    from cron.role_pool_scheduler import admission_guard
    from workflow_services import collect_service_result
    with admission_guard(pool_path):
        snapshot=ledger.snapshot()
        for request in snapshot['controls']:
            if request['state']=='ACCEPTED':
                applied=ledger.apply_control(request['request_id'],
                    command_id='control-apply:'+request['request_id'])
                capture_commit(applied)
                result['controls'].append(applied)
        result['recovery']=recover_executions(pool_path)
        from workflow_autonomy import sweep
        result['repairs']=sweep(ledger)
        result['execution_outcomes']=recover_execution_outcomes(pool_path,home)
        for action in snapshot['external_actions']:
            if action['state'] not in {'SUBMITTED','UNKNOWN'}:
                continue
            try:
                collected=collect_service_result(ledger,action['action_id'],
                    command_id='service-terminal:'+action['action_id'],adapter=service_adapter)
                capture_commit(collected)
                result['services'].append({'action_id':action['action_id'],**collected})
            except (OSError,ValueError,KeyError) as exc:
                result['errors'].append({'source':'service','action_id':action['action_id'],
                                         'error':type(exc).__name__})
        # Busy roles still reach the adapter so new input can join their existing
        # wakeup intent. The hint/native gate remain authoritative at execution.
        eligible=list(ROLES)
    return {'result':result, 'roles':eligible, 'commits':commits}


def submit_event_tick(pool_path, collected, *, home, dispatcher=None, notification_transport=None,
                      drain_notifications=True):
    """Submit intents and drain original notifications after collection locks."""
    if collected is None:
        return None
    result=collected['result']
    # Original dispatcher may acquire the admission lock itself. Never hold it
    # over model/worker dispatch or a channel side effect.
    from workflow_dispatch import rotate_roles
    for role in rotate_roles(collected['roles']):
        result['dispatch'][role]=request_role_wakeup(pool_path,role,home=home,dispatcher=dispatcher,
                                                    committed_at=collected.get('commits'))
    if drain_notifications:
        from workflow_notifications import drain_business_notifications
        result['notifications']=drain_business_notifications(pool_path,transport=notification_transport)
    return result


def event_tick(pool_path, *, home, dispatcher=None, service_adapter=None, notification_transport=None):
    collected=collect_event_tick(pool_path,home=home,service_adapter=service_adapter)
    return submit_event_tick(pool_path,collected,home=home,dispatcher=dispatcher,
                             notification_transport=notification_transport)
