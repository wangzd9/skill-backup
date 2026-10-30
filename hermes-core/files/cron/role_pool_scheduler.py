"""Admission and immutable per-execution scope for explicitly configured role pools.

Pool configuration and reconciliation live in the installed role runtime. This
bridge supplies the scheduler facts that runtime cannot safely infer from leases.
It never edits jobs, starts a model, dispatches a task, or stops an owner.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import threading

from hermes_constants import get_hermes_home


SCOPE_ENV = {
    'pool_path': 'HERMES_ROLE_POOL_PATH',
    'generation': 'HERMES_ROLE_POOL_GENERATION',
    'requirement_id': 'HERMES_ROLE_REQUIREMENT_ID',
    'requirement_root': 'HERMES_ROLE_REQUIREMENT_ROOT',
    'project_root': 'HERMES_ROLE_PROJECT_ROOT',
    'role': 'HERMES_ROLE_NAME',
    'drain_only': 'HERMES_ROLE_POOL_DRAIN_ONLY',
    'run_id': 'HERMES_ROLE_RUN_ID',
    'work_id': 'HERMES_ROLE_WORK_ID',
    'execution_generation': 'HERMES_ROLE_EXECUTION_GENERATION',
}
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()
_HELD = threading.local()


class RolePoolUnavailable(ValueError):
    pass


def current_pool_routing(job, home=None):
    """Adopt explicit routing when a queued payload predates pool deployment.

    Existing workers never call this again. Only routing fields are refreshed;
    delivery, ownership, schedule and pause state retain scheduler semantics.
    """
    if job.get('role_pool') is not None:
        return job
    path = Path(home or get_hermes_home())/'cron/jobs.json'
    try:
        rows = json.loads(path.read_text()).get('jobs', [])
    except (OSError, ValueError, TypeError):
        return job
    for current in rows:
        if current.get('id') == job.get('id') and current.get('role_pool') is not None:
            routed = dict(job)
            for key in ('role_pool', 'prompt', 'script', 'workdir'):
                routed[key] = current.get(key)
            return routed
    return job


@contextmanager
def admission_guard(pool_path):
    """Same stable inode across freeze, reconciliation and crash recovery."""
    import fcntl

    path = Path(pool_path).resolve().with_suffix('.admission.lock')
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(str(path), threading.RLock())
    with lock:
        held = getattr(_HELD, 'paths', None)
        if held is None:
            held = _HELD.paths = set()
        if str(path) in held:
            yield
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a+') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            held.add(str(path))
            try:
                yield
            finally:
                held.remove(str(path))
                fcntl.flock(stream, fcntl.LOCK_UN)


def _process(pid, expected_start):
    try:
        fields = (Path('/proc')/str(int(pid))/'stat').read_text().rsplit(')', 1)[1].split()
        if fields[0] in ('Z', 'X'):
            return 'dead', False
        if expected_start is None:
            return 'unknown', False
        return ('alive', True) if str(expected_start) == fields[19] else ('dead', False)
    except (FileNotFoundError, ProcessLookupError):
        return 'dead', False
    except (OSError, ValueError, IndexError, TypeError):
        return 'unknown', False


def _runtime(home):
    source = (Path(home)/'plugins/role-workflow-runtime/requirement_runtime.py').resolve()
    if not source.is_file():
        raise RolePoolUnavailable('ROLE_POOL_RUNTIME_UNAVAILABLE')
    name = '_cron_role_requirement_'+hashlib.sha256(str(source).encode()).hexdigest()[:20]
    module = sys.modules.get(name)
    if module is None:
        # The plugin's sibling imports use the same canonical source directory.
        directory = str(source.parent)
        if directory not in sys.path:
            sys.path.append(directory)
        spec = importlib.util.spec_from_file_location(name, source)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    return module


class CronRolePoolAdapter:
    def __init__(self, pool_path, safe_execution_id=None, home=None):
        self.pool_path = Path(pool_path).resolve()
        self.home = Path(home or get_hermes_home()).resolve()
        self.safe_execution_id = safe_execution_id

    @contextmanager
    def switch_guard(self, job_ids, transition_id):
        # job_ids and transition_id belong to the runtime's durable transaction;
        # the inode is pool-wide so every new worker shares the same gate.
        with admission_guard(self.pool_path):
            yield

    def pause_business_roles(self, roles):
        """Apply explicit queued control without stopping an existing owner."""
        from cron.jobs import pause_job
        from cron.scheduler_provider import _profile_cron_scope
        pool = json.loads(self.pool_path.read_text())
        if not roles or any(role not in pool['roles'] for role in roles):
            raise RolePoolUnavailable('CONTROL_ROLE_UNVERIFIED')
        receipts = []
        with _profile_cron_scope(self.home):
            for role in dict.fromkeys(roles):
                identifier = pool['roles'][role]['poll_job_id']
                result = pause_job(identifier, reason='explicit ordered role control')
                if result is None:
                    raise RolePoolUnavailable('CONTROL_BUSINESS_JOB_UNAVAILABLE')
                receipts.append({'role': role, 'job_id': identifier, 'state': 'paused'})
        return receipts

    def inspect(self, job_ids):
        wanted = set(job_ids)
        pool = json.loads(self.pool_path.read_text())
        business_ids = {row['poll_job_id'] for row in pool['roles'].values()}
        value = json.loads((self.home/'cron/jobs.json').read_text())
        jobs = {row['id']: row for row in value['jobs'] if row.get('id') in wanted}
        path = self.home/'cron/executions.db'
        if not path.is_file():
            raise RolePoolUnavailable('ROLE_POOL_EXECUTIONS_UNAVAILABLE')
        executions = []
        seen = set()
        with sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=1) as db:
            db.row_factory = sqlite3.Row
            for row in db.execute('SELECT * FROM executions'):
                row = dict(row)
                if row['job_id'] not in wanted:
                    continue
                if (row['job_id'] not in business_ids
                        and row['id'] != self.safe_execution_id):
                    continue  # independent read-only reports do not own a business slot
                state, verified = _process(row.get('pid'), row.get('process_started_at'))
                terminal = row['status'] in ('completed', 'failed', 'unknown')
                # A completed ledger row with a living worker is still unsafe.
                # A proven-dead historical terminal owner is not a blocker.
                if terminal and state == 'dead':
                    continue
                execution_id = row['id']
                safe = (execution_id == self.safe_execution_id
                        and os.environ.get('_HERMES_CRON_EXTERNAL_WORKER') == execution_id
                        and row.get('pid') == os.getpid() and verified)
                executions.append({
                    'job_id': row['job_id'], 'execution_id': execution_id,
                    'state': ('terminal_owner_still_alive' if terminal and state == 'alive'
                              else 'unknown' if terminal else row['status']),
                    'pid': row.get('pid'), 'process_start': row.get('process_started_at'),
                    'process_state': state, 'identity_verified': verified,
                    'safe_checkpoint': safe,
                })
                seen.add(execution_id)
        # Account for live worker scopes even if the ledger row was pruned or
        # prematurely terminal. Read only cgroup identities, never command text.
        for proc in Path('/proc').iterdir():
            if not proc.name.isdigit():
                continue
            try:
                cgroup = (proc/'cgroup').read_text()
            except (OSError, ProcessLookupError):
                continue
            match = re.search(r'hermes-worker-cron-([a-zA-Z0-9_-]+)-exec-([a-f0-9]+)\.scope', cgroup)
            if (not match or match[1] not in wanted or match[1] not in business_ids
                    or match[2] in seen):
                continue
            # Multiple descendants are one scope blocker. This does not exclude
            # our caller: only a matching execution ledger can prove preflight.
            seen.add(match[2])
            executions.append({'job_id': match[1], 'execution_id': match[2],
                               'state': 'untracked_live_worker_scope', 'pid': int(proc.name),
                               'process_start': None, 'identity_verified': False,
                               'safe_checkpoint': False})
        return {'jobs': jobs, 'executions': executions, 'blockers': []}


def _save_scope(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True).encode()+b'\n'
    if path.exists():
        if path.read_bytes() != raw:
            raise RolePoolUnavailable('ROLE_POOL_EXECUTION_SCOPE_CONFLICT')
        return
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.scope-')
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        # Admission lock serializes writers for the same pool. Do not overwrite
        # an existing immutable scope on retry or session-ID collision.
        if path.exists():
            if path.read_bytes() != raw:
                raise RolePoolUnavailable('ROLE_POOL_EXECUTION_SCOPE_CONFLICT')
        else:
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def scope_for_session(session_id, home=None):
    """Read frozen attribution, never resolve a historical session as current."""
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    path = (Path(home or get_hermes_home())/'runtime/role-pool-sessions'/
            (hashlib.sha256(session_id.encode()).hexdigest()+'.json'))
    if not path.is_file():
        return None
    value = json.loads(path.read_text())
    if value.get('session_id') != session_id or value.get('schema_version') not in (1, 2):
        raise RolePoolUnavailable('ROLE_POOL_SESSION_SCOPE_INVALID')
    return value


@contextmanager
def prepare_role_pool_run(job, *, execution_id=None, session_id=None, home=None):
    """Freeze one external worker before any script, cwd, prompt or model load.

    A blocked/idle pool yields None. Jobs without role_pool retain the existing
    path. Internal environment metadata is only set in an adopted external
    worker, never in a shared gateway thread.
    """
    config = job.get('role_pool')
    if config is None:
        yield job
        return
    if not isinstance(config, dict) or not config.get('path') or not config.get('role'):
        raise RolePoolUnavailable('ROLE_POOL_JOB_CONFIG_INVALID')
    execution_id = execution_id or job.get('execution_id')
    if (not execution_id or os.environ.get('_HERMES_CRON_EXTERNAL_WORKER') != execution_id
            or not session_id):
        raise RolePoolUnavailable('ROLE_POOL_REQUIRES_EXTERNAL_WORKER')
    if not isinstance(execution_id, str) or not re.fullmatch(r'[a-f0-9]{32}', execution_id):
        raise RolePoolUnavailable('ROLE_POOL_EXECUTION_ID_INVALID')
    home = Path(home or get_hermes_home()).resolve()
    pool_path = Path(config['path']).resolve()
    adapter = CronRolePoolAdapter(pool_path, safe_execution_id=execution_id, home=home)
    runtime = _runtime(home)
    with admission_guard(pool_path):
        own = adapter.inspect([job['id']])['executions']
        if not any(row['execution_id'] == execution_id and row['safe_checkpoint']
                   and row['identity_verified'] for row in own):
            raise RolePoolUnavailable('ROLE_POOL_EXTERNAL_WORKER_IDENTITY_UNVERIFIED')
        workflow_enabled = getattr(runtime, 'workflow_enabled', lambda path: False)(pool_path)
        if workflow_enabled:
            own_execution = next(row for row in own if row['execution_id'] == execution_id)
            scoped = runtime.prepare_workflow_run(job, pool_path=pool_path, execution=own_execution,
                             home=home, session_id=session_id)
            if scoped is not None:
                scope = scoped['_role_pool_scope']
                _save_scope(home/'runtime/role-pool-executions'/(execution_id+'.json'), scope)
                _save_scope(home/'runtime/role-pool-sessions'/
                            (hashlib.sha256(session_id.encode()).hexdigest()+'.json'), scope)
        else:
            # Reconcile may keep an accepted request queued while any previous owner
            # is active. It never upgrades a group grant or invents a new request.
            kind = config.get('kind', 'business')
            # Report sessions are observers of the current accepted scope. They do
            # not advance control transactions or mutate business bindings.
            reconciliation = (runtime.reconcile(pool_path, scheduler_adapter=adapter, apply=True)
                              if kind == 'business' else {'state': 'NO_PENDING_REQUEST'})
            resolved = runtime.resolve_role(pool_path, config['role'])
            drain_codes = {'INDEPENDENT_OPERATION_ACTIVE_OR_UNKNOWN',
                           'OCR_ACTIVE_OR_UNRESOLVED', 'STRONG_REVIEW_ACTIVE_OR_UNKNOWN'}
            blockers = reconciliation.get('blockers') or []
            waiting = reconciliation.get('state') not in ('NO_PENDING_REQUEST', 'APPLIED')
            if (kind == 'business' and reconciliation.get('state') == 'APPLIED'
                    and runtime.status(pool_path).get('pending')):
                # Recovering a PREPARED transaction can apply an intermediate
                # selection while a newer accepted request is still queued. Let
                # the next preflight reconcile it before starting another owner.
                waiting = True
            drain = (kind == 'business' and config['role'] == 'COORDINATOR'
                     and reconciliation.get('state') == 'WAITING_SAFE_POINT' and bool(blockers)
                     and all(isinstance(row, dict) and row.get('code') in drain_codes for row in blockers))
            if resolved.get('state') != 'ACTIVE' or (kind == 'business' and waiting and not drain):
                scoped = None
            else:
                if kind not in ('business', 'report'):
                    raise RolePoolUnavailable('ROLE_POOL_JOB_KIND_INVALID')
                if kind == 'business' and resolved.get('job_id') != job.get('id'):
                    raise RolePoolUnavailable('ROLE_POOL_JOB_ID_MISMATCH')
                root = Path(resolved['project_root']).resolve()
                if not root.is_dir() or type(resolved.get('generation')) is not int:
                    raise RolePoolUnavailable('ROLE_POOL_CURRENT_SCOPE_INVALID')
                scope = {key: resolved.get(key) for key in
                         ('generation', 'requirement_id', 'requirement_root', 'project_root', 'role', 'legacy_assignment')}
                scope.update(schema_version=1, pool_path=str(pool_path), project_root=str(root),
                             job_id=job['id'], execution_id=execution_id, session_id=session_id,
                             kind=kind, drain_only=drain)
                _save_scope(home/'runtime/role-pool-executions'/(execution_id+'.json'), scope)
                _save_scope(home/'runtime/role-pool-sessions'/
                            (hashlib.sha256(session_id.encode()).hexdigest()+'.json'), scope)
                scoped = dict(job, workdir=str(root), _role_pool_scope=scope,
                              _role_pool_session_id=session_id)
                if kind == 'business' and scope.get('legacy_assignment'):
                    scoped['prompt'] = (str(job.get('prompt') or '')+
                        '\n本轮保留既有分散绑定，尚未显式统一选择需求；'
                        '仅沿本角色原绑定和已授权范围恢复、到达安全检查点，不把旧授权扩展到其他需求。')
                elif (kind == 'business' and scope.get('requirement_root')
                      and Path(scope['requirement_root']).resolve() != root):
                    scoped['prompt'] = (str(job.get('prompt') or '')+
                        '\n本轮接收需求包：'+str(scope['requirement_root'])+'；业务运行根：'+str(root)+
                        '。兼容运行根内的旧TODO、派单和检查点不自动属于本需求；'
                        '按所选需求包TODO中的“本包任务编号定位”精确确定任务范围。')
                if drain:
                    scoped['prompt'] = (str(scoped.get('prompt') or '')+
                        '\n本轮仅收口原需求：核对既有 run 终态、回执及裁决，保存检查点并释放；'
                        '不得新派单、新开发、新提交独立操作或审查。切换请求仍在等待安全点。')
    if scoped is None:
        yield None
        return
    previous = {name: os.environ.get(name) for name in SCOPE_ENV.values()}
    try:
        for key, name in SCOPE_ENV.items():
            value = scope.get(key)
            os.environ[name] = ('1' if value else '0') if key == 'drain_only' else (
                str(value) if value is not None else '')
        if (scope.get('kind') == 'business' and not scope.get('workflow_version')
                and hasattr(runtime, 'legacy_lease_maintenance')):
            with runtime.legacy_lease_maintenance(scope, home=home):
                yield scoped
        else:
            yield scoped
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
