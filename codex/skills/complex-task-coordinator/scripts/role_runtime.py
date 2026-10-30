#!/usr/bin/env python3
"""Small file protocol helper; no scheduler, model calls, Git changes or messaging."""
import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import sqlite3
import threading
import time
import uuid

ROLES = ('COORDINATOR', 'A', 'B', 'C', 'TESTER')

_SHARED_GATEWAY_PID = (
    'Cannot bind the shared gateway PID to a role lease: the gateway outlives every '
    'session, so the lease can never be proven dead and the role stays queued instead '
    "of woken. Bind this session's own long-lived worker PID/start instead."
)

_NOT_A_WORKER = (
    'PID %(pid)s is not a cron worker: only this session\'s own `python -m cron.scheduler` '
    'process can own a cron lease. A shell, helper or unrelated daemon dies before the '
    'turn ends, which strands the lease and blocks the next dispatch.'
)

_FOREIGN_WORKER = (
    'PID %(pid)s is a cron worker but not this session\'s: it is not in this process\'s '
    'ancestor chain, so it belongs to another running turn. Bind your own worker PID '
    '(use role_runtime.py worker-identity) instead of borrowing a neighbour\'s.'
)

_UNBOUND_NOTE = (
    'claim accepted without --pid; the lease carries no process identity, so liveness '
    'can never be proven and another owner may take over once the token expires. Pass '
    'this session\'s own cron worker PID to keep the role properly held.'
)


def now():
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec='seconds')


def load(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def atomic(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def save(path, value):
    atomic(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


@contextlib.contextmanager
def locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def pid_identity(pid):
    try:
        fields = Path(f'/proc/{int(pid)}/stat').read_text().rsplit(') ', 1)[1].split()
        # Exited but unreaped workers cannot execute writes either. Treating a
        # zombie as alive would leave its role blocked until its parent reaps it.
        return None if fields[0] in ('Z', 'X') else fields[19]
    except (FileNotFoundError, ProcessLookupError):
        return None
    except (OSError, ValueError, IndexError):
        return 'UNVERIFIABLE'


def process_state(pid, start):
    """A deadline is not liveness; only a bound process identity proves exit."""
    if not pid or not start:
        return 'unverified'
    identity = pid_identity(pid)
    if identity == 'UNVERIFIABLE':
        return 'unverified'
    return 'alive' if identity == start else 'exited_or_reused'


def verified_pid_identity(pid):
    identity = pid_identity(pid)
    if not identity or identity == 'UNVERIFIABLE':
        raise ValueError("Owner PID is not verifiably alive; bind this session's own long-lived "
                         'worker PID (never a transient shell, never the shared gateway PID: '
                         'the gateway outlives every session, so a lease bound to it can never '
                         'be proven dead)')
    return identity


def gateway_pid():
    """Return the shared Hermes gateway PID, or None when it cannot be identified.

    Resolved at call time on purpose: the gateway restarts and takes a new PID,
    so a hardcoded number would both miss the live gateway and reject unrelated
    PIDs that reuse the old value.
    """
    try:
        value = subprocess.run(
            ['systemctl', '--user', 'show', 'hermes-gateway', '-p', 'MainPID', '--value'],
            capture_output=True, text=True, timeout=5).stdout.strip()
        if value.isdigit() and int(value) > 0:
            return int(value)
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def refuse_shared_gateway_pid(pid):
    """Reject a lease bound to the shared gateway.

    The group-session plugin legitimately binds the gateway because it runs
    inside that process. This CLI is invoked by cron turns, whose own worker is
    never the gateway; a cron lease on the gateway would outlive its turn and
    keep the role queued forever instead of waking it.
    """
    if not pid:
        return
    resolved = gateway_pid()
    if resolved is not None and int(pid) == resolved:
        raise ValueError(_SHARED_GATEWAY_PID)
    if resolved is None:
        # systemd lookup unavailable: fall back to the process command line so a
        # gateway PID is still refused rather than silently accepted.
        try:
            command = Path(f'/proc/{int(pid)}/cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')
        except (OSError, ValueError):
            return
        if 'gateway' in command:
            raise ValueError(_SHARED_GATEWAY_PID)


def command_line(pid):
    try:
        return Path(f'/proc/{int(pid)}/cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace').strip()
    except (OSError, ValueError):
        return ''


def parent_pid(pid):
    try:
        return int(Path(f'/proc/{int(pid)}/stat').read_text().rsplit(') ', 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def is_cron_worker(pid):
    """A cron worker runs the scheduler, never the gateway.

    Matching the scheduler command line (not the gateway's parentage) keeps this
    independent of how the gateway happens to spawn its children.
    """
    return 'cron.scheduler' in command_line(pid)


def ancestry(pid=None):
    """Yield this process's ancestors, nearest first.

    Used to prove that a claimed PID really belongs to the calling session
    instead of any other live process that merely looks long-lived.
    """
    current = os.getpid() if pid is None else int(pid)
    for _ in range(12):
        current = parent_pid(current)
        if not current or current <= 1:
            return
        yield current


def _worker_process_snapshot(pid):
    """Read one live PID/start/parent identity without exposing its arguments."""
    def stat_identity():
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()
        if fields[0] in ('Z', 'X'):
            raise ValueError('调用链进程已退出，无法证明当前 cron worker 身份')
        return fields[19], int(fields[1])

    try:
        before = stat_identity()
        argv = Path(f'/proc/{pid}/cmdline').read_bytes().rstrip(b'\0').split(b'\0')
        executable = Path(os.readlink(f'/proc/{pid}/exe')).name
        after = stat_identity()
    except (OSError, ValueError, IndexError) as error:
        raise ValueError('无法核实当前调用链的进程身份；不猜测或借用其他 worker') from error
    if before != after:
        raise ValueError('调用链 PID/start 或父进程发生变化；请重新查询 worker-identity')
    # A shell can contain the complete scheduler command in its inline script.
    # Only the Python executable's actual module argv identifies this worker.
    is_worker = (re.fullmatch(r'python(?:\d+(?:\.\d+)*)?', executable) is not None
                 and argv[1:3] == [b'-m', b'cron.scheduler'])
    return {'pid': pid, 'process_start': before[0], 'parent_pid': before[1],
            'is_worker': is_worker}


def _calling_worker_chain():
    current = os.getpid()
    chain = []
    seen = set()
    for _ in range(64):
        if current <= 1 or current in seen:
            break
        seen.add(current)
        row = _worker_process_snapshot(current)
        chain.append((row['pid'], row['process_start'], row['parent_pid']))
        if row['is_worker']:
            return row, chain
        current = row['parent_pid']
    raise ValueError('无法证明当前调用者属于 python -m cron.scheduler；不选取其他 worker 或 gateway')


def worker_identity():
    """Identify only this CLI's own scheduler ancestor, with a second proof."""
    worker, chain = _calling_worker_chain()
    refuse_shared_gateway_pid(worker['pid'])
    confirmed, confirmed_chain = _calling_worker_chain()
    if chain != confirmed_chain or worker != confirmed:
        raise ValueError('调用链 PID/start 或父进程发生变化；请重新查询 worker-identity')
    return {'pid': worker['pid'], 'process_start': worker['process_start'],
            'source': 'current_process_ancestor_chain', 'verified': True}


def verify_worker_pid(pid):
    """Reject a binding that is not this session's own cron worker.

    A denial is far cheaper than a poisoned lease: once an unrelated long-lived
    PID owns a lease, `process_state` reports it alive forever and the role stops
    being woken. Short shells are refused explicitly because they die long before
    the turn does.
    """
    if not pid:
        return
    refuse_shared_gateway_pid(pid)
    if not is_cron_worker(pid):
        raise ValueError(_NOT_A_WORKER % {'pid': int(pid)})
    if int(pid) not in set(ancestry()):
        raise ValueError(_FOREIGN_WORKER % {'pid': int(pid)})


def owner_state(lease):
    return process_state(lease.get('pid'), lease.get('process_start'))


def record_recovery(root, role, lease, **details):
    record = {'recovered_at': now(), 'role': role,
              'old_owner': lease.get('owner'),
              'lease': {k: v for k, v in lease.items() if k != 'token'}, **details}
    save(root / 'inbox/control/runtime/recoveries' / (uuid.uuid4().hex + '.json'), record)
    return record


def binding(project):
    root = Path(project).resolve()
    data = load(root / 'ROLE-BINDINGS.json')
    if not data or data.get('schema_version') != 1 or Path(data['project_root']).resolve() != root:
        raise ValueError('Invalid project binding; do not guess another project')
    return root, data


def requirement_module():
    """Use the same installed source as the observer and plugin."""
    import requirement_runtime
    return requirement_runtime


def pool_scope_guard(data, role):
    """An old worker cannot borrow a newly bound root or generation."""
    configured = data.get('role_pool') or {}
    environment = os.environ.get('HERMES_ROLE_POOL_PATH')
    if not configured and not environment:
        return None
    if configured and not isinstance(configured, dict):
        raise ValueError('ROLE_POOL_BINDING_INVALID')
    path = environment or configured.get('path')
    if not path:
        raise ValueError('ROLE_POOL_BINDING_INVALID')
    generation = (os.environ.get('HERMES_ROLE_POOL_GENERATION') if environment
                  else configured.get('generation'))
    try:
        generation = int(generation)
    except (TypeError, ValueError):
        raise ValueError('ROLE_POOL_GENERATION_UNVERIFIED') from None
    root = str(Path(data['project_root']).resolve())
    if environment:
        if (os.environ.get('HERMES_ROLE_NAME') != role
                or str(Path(os.environ.get('HERMES_ROLE_PROJECT_ROOT', '')).resolve()) != root):
            raise ValueError('ROLE_POOL_FROZEN_SCOPE_MISMATCH')
    if configured and (Path(configured.get('path', '')).resolve() != Path(path).resolve()
                       or configured.get('generation') != generation):
        raise ValueError('ROLE_POOL_BINDING_GENERATION_CHANGED')
    return requirement_module().validate_role_scope(path, role, root, generation)


def legacy_busy(data, role):
    busy = []
    for row in data.get('legacy_runs', []):
        if row['role'] != role:
            continue
        if not row.get('pid'):
            if not row.get('stopped_verified'):
                busy.append(row)
            continue
        identity = pid_identity(row['pid'])
        if identity is None:
            continue
        if identity == 'UNVERIFIABLE' or not row.get('process_start') or identity == row.get('process_start'):
            busy.append(row)
    return busy


def guard(data, role):
    # The monotonic business authority also fences old CLIs/tokens; switching
    # model tools alone would leave the historical shell writer path open.
    configured = data.get('role_pool') or {}
    path = os.environ.get('HERMES_ROLE_POOL_PATH') or (configured.get('path') if isinstance(configured, dict) else None)
    if path:
        ledger_path = Path(path).resolve().with_suffix('.runtime')/'business.sqlite3'
        if ledger_path.exists():
            from business_ledger import BusinessLedger
            if BusinessLedger(ledger_path, readonly=True).mode()['mode'] == 'ACTIVE':
                raise ValueError('LEGACY_WRITER_DISABLED: use the current work-bound runtime')
    pool_scope_guard(data, role)
    if data.get('control') != 'ACTIVE':
        raise ValueError('Project is not ACTIVE: no new business actions')
    if legacy_busy(data, role):
        raise ValueError('MIGRATION_WAIT: legacy owner still alive; do not take over')


def lease_path(root, role):
    return root / 'inbox/control/runtime/leases' / (role + '.json')


def lease_lock(root, role):
    # Keep the historical coordinator.lock inode for compatibility with in-flight owners.
    return root / 'inbox' / ('coordinator.lock' if role == 'COORDINATOR' else f'{role}.lock')


def valid_lease(root, role, token, allow_expired=False, verify_owner=True):
    lease = load(lease_path(root, role), {})
    if not token or lease.get('token') != token or (not allow_expired and lease.get('expires_at', 0) <= dt.datetime.now().timestamp()):
        raise ValueError('Missing, stale or expired writer token; recheck owner before continuing')
    # Preserve old unbound leases, but never let a dead/reused bound owner renew
    # itself or authorize new writes merely because its token was left on disk.
    if verify_owner and (lease.get('pid') or lease.get('process_start')) and owner_state(lease) != 'alive':
        raise ValueError('Owner process is gone or unverifiable; do not inherit its token, recheck owner before recovery')
    return lease


class LegacyLeaseHeartbeat:
    """Maintain only this adopted business worker's existing legacy lease.

    The native scheduler owns this context, including model, compaction and
    tool waits. It neither claims a lease nor recreates one after deletion.
    Every renewal rechecks immutable execution scope and live native ownership.
    """
    def __init__(self, scope, *, home=None, interval=60, ttl=300):
        self.scope = dict(scope)
        self.home = Path(home or os.environ.get('HERMES_HOME') or Path.home()/'.hermes').resolve()
        self.interval, self.ttl = interval, ttl
        if not 0 < interval < ttl or not 30 <= ttl <= 600:
            raise ValueError('Invalid legacy heartbeat interval/ttl')
        self.pid = os.getpid()
        self.start = verified_pid_identity(self.pid)
        self.token = None
        self.owner = None
        self.stopped = False
        self.result = {'state': 'waiting_for_owned_lease', 'renewed': False}
        self._stop = threading.Event()
        self._thread = None
        self.started_at = time.time()
        self.last_renewed_at = None

    def _record(self):
        execution = self.scope.get('execution_id')
        if not isinstance(execution, str) or not re.fullmatch(r'[a-f0-9]{32}', execution):
            return
        value = {'schema_version': 1, 'execution_id': execution,
                 'role': self.scope.get('role'), 'pool_path': self.scope.get('pool_path'),
                 'project_root': self.scope.get('project_root'), 'generation': self.scope.get('generation'),
                 'pid': self.pid, 'process_start': self.start, 'started_at': self.started_at,
                 'checked_at': time.time(), 'last_renewed_at': self.last_renewed_at,
                 'stopped': self.stopped, **self.result}
        try:
            save(self.home/'runtime/legacy-heartbeats'/(execution+'.json'), value)
        except OSError as exc:
            self.result['diagnostic_error'] = type(exc).__name__
            __import__('logging').getLogger(__name__).warning('Legacy heartbeat diagnostic unavailable: %s',type(exc).__name__)

    @contextlib.contextmanager
    def _lock(self, path):
        path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a+') as stream:
            deadline = time.monotonic()+2
            while True:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if self._stop.wait(0.05) or time.monotonic() >= deadline:
                        raise TimeoutError('LEGACY_HEARTBEAT_LOCK_BUSY')
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def _execution(self):
        scope = self.scope
        execution = scope.get('execution_id')
        if (scope.get('schema_version') != 1 or scope.get('kind') != 'business'
                or scope.get('role') not in ROLES or not isinstance(execution, str)
                or not re.fullmatch(r'[a-f0-9]{32}', execution)
                or os.environ.get('_HERMES_CRON_EXTERNAL_WORKER') != execution
                or not _worker_process_snapshot(self.pid)['is_worker']
                or process_state(self.pid, self.start) != 'alive'):
            raise ValueError('LEGACY_HEARTBEAT_WORKER_UNVERIFIED')
        frozen = load(self.home/'runtime/role-pool-executions'/(execution+'.json'))
        if frozen != scope:
            raise ValueError('LEGACY_HEARTBEAT_FROZEN_SCOPE_CHANGED')
        path = self.home/'cron/executions.db'
        with sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=1) as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT * FROM executions WHERE id=?', (execution,)).fetchone()
        if (row is None or row['job_id'] != scope.get('job_id')
                or row['status'] not in ('claimed', 'running')
                or row['pid'] != self.pid or str(row['process_started_at']) != self.start):
            raise ValueError('LEGACY_HEARTBEAT_EXECUTION_OWNER_CHANGED')

    def heartbeat_once(self):
        if self.stopped:
            return self.result
        # Same lock and order as pool switching: admission, then role writer.
        # A switch cannot commit between the generation check and renewal.
        try:
            self.result = {'state': 'checking', 'renewed': False}
            self._record()
            with self._lock(Path(self.scope['pool_path']).resolve().with_suffix('.admission.lock')):
                return self._heartbeat_once()
        except TimeoutError as exc:
            self.result = {'state': 'deferred_lock_busy', 'renewed': False, 'reason': str(exc)}
            return self.result
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.stopped = True
            self.result = {'state': 'stopped_unverified', 'renewed': False, 'reason': str(exc)}
            return self.result
        finally:
            self._record()

    def _heartbeat_once(self):
        if self.stopped:
            return self.result
        try:
            self._execution()
            root, data = binding(self.scope['project_root'])
            role = self.scope['role']
            gate = data.get('role_pool')
            if (gate != {'path': self.scope['pool_path'], 'generation': self.scope['generation']}
                    or role not in data['roles'] or data.get('control') != 'ACTIVE'):
                raise ValueError('LEGACY_HEARTBEAT_SCOPE_OR_CONTROL_CHANGED')
            # A frozen scope is a fence, never permission to borrow a new root.
            requirement_module().validate_role_scope(self.scope['pool_path'], role,
                                                     str(root), self.scope['generation'])
            path = lease_path(root, role)
            if not path.exists():
                if self.token is not None:
                    raise ValueError('LEGACY_HEARTBEAT_OWNED_LEASE_REMOVED')
                self.result = {'state': 'waiting_for_owned_lease', 'renewed': False}
                return self.result
            with self._lock(lease_lock(root, role)):
                lease = load(path, {})
                if not lease:
                    raise ValueError('LEGACY_HEARTBEAT_OWNED_LEASE_REMOVED')
                if (lease.get('pid') != self.pid or lease.get('process_start') != self.start
                        or not lease.get('token') or not lease.get('owner') or lease.get('manual_session_id')
                        or lease.get('manual_turn_id')
                        or (self.token is not None and self.token != lease.get('token'))
                        or (self.owner is not None and self.owner != lease.get('owner'))
                        or lease.get('execution_id', self.scope['execution_id']) != self.scope['execution_id']):
                    raise ValueError('LEGACY_HEARTBEAT_LEASE_OWNER_CHANGED')
                # Recheck under the writer lock, including generation/control.
                self._execution()
                _, latest = binding(root)
                if latest != data:
                    raise ValueError('LEGACY_HEARTBEAT_BINDING_CHANGED')
                requirement_module().validate_role_scope(self.scope['pool_path'], role,
                                                         str(root), self.scope['generation'])
                self.token = lease['token']
                self.owner = lease['owner']
                checked = time.time()
                lease.update(expires_at=checked+self.ttl, last_seen=checked,
                             renewal_source={'kind': 'verified_native_worker_heartbeat',
                                             'execution_id': self.scope['execution_id'],
                                             'generation': self.scope['generation'],
                                             'project_root': str(root), 'role': role,
                                             'pid': self.pid, 'process_start': self.start})
                save(path, lease)
                self.last_renewed_at = checked
                self.result = {'state': 'renewed', 'renewed': True, 'at': checked,
                               'expires_at': lease['expires_at'], 'source': lease['renewal_source']}
                return self.result
        except TimeoutError:
            raise
        except sqlite3.OperationalError as exc:
            if 'locked' in str(exc).lower() or 'busy' in str(exc).lower():
                self.result = {'state': 'deferred_source_busy', 'renewed': False, 'reason': 'LEGACY_EXECUTION_SOURCE_BUSY'}
                return self.result
            self.stopped = True
            self.result = {'state': 'stopped_unverified', 'renewed': False, 'reason': str(exc)}
            return self.result
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            self.stopped = True
            self.result = {'state': 'stopped_unverified', 'renewed': False, 'reason': str(exc)}
            return self.result

    def __enter__(self):
        self.heartbeat_once()
        def maintain():
            while not self._stop.wait(self.interval) and not self.stopped:
                self.heartbeat_once()
        self._thread = threading.Thread(target=maintain, name='legacy-business-lease-heartbeat', daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=min(self.interval, 2))
        self.stopped = True
        self.result = {**self.result, 'state': 'context_ended',
                       'previous_state': self.result.get('state')}
        self._record()


@contextlib.contextmanager
def legacy_admission(project):
    root, data = binding(project)
    configured = data.get('role_pool') or {}
    pool = os.environ.get('HERMES_ROLE_POOL_PATH') or (configured.get('path') if isinstance(configured, dict) else None)
    # Mode activation and legacy mutations share this outer lock, then the
    # historical role lock. Recheck binding and authority only after admission.
    admission = Path(pool).resolve().with_suffix('.admission.lock') if pool else root/'.legacy-admission.lock'
    with locked(admission):
        fresh_root, fresh = binding(project)
        current = fresh.get('role_pool') or {}
        current_pool = os.environ.get('HERMES_ROLE_POOL_PATH') or (current.get('path') if isinstance(current, dict) else None)
        if (str(Path(current_pool).resolve()) if current_pool else None) != (str(Path(pool).resolve()) if pool else None):
            raise ValueError('ROLE_POOL_ADMISSION_CHANGED')
        yield fresh_root, fresh


def execute(args):
    if args.action not in ('claim', 'recover', 'renew', 'release', 'run', 'event', 'timeline'):
        return _execute_unlocked(args)
    with legacy_admission(args.project):
        return _execute_unlocked(args)


def _execute_unlocked(args):
    if args.action == 'worker-identity':
        return worker_identity()
    if args.action == 'resolve':
        registry = load(Path(args.registry), {'projects': []})
        matches = []
        for entry in registry['projects']:
            root, data = binding(entry['project_root'])
            for role, item in data['roles'].items():
                if (not args.chat_id or item.get('chat_id') == args.chat_id) and (not args.role or role == args.role):
                    matches.append({'project_root': str(root), 'role': role, **item})
        if len(matches) != 1:
            raise ValueError(f'AMBIGUOUS_BINDING: {len(matches)} matches; ask for project/role')
        return matches[0]

    root, data = binding(args.project)
    role = args.role
    if role not in data['roles']:
        raise ValueError('Role not registered')
    if args.action == 'preflight':
        stamp = {'checked_at': now(), 'role': role, 'mode': args.mode,
                 'control': data.get('control'), 'legacy_busy': legacy_busy(data, role)}
        with locked(root / f'inbox/control/runtime/{role}-tick.lock'):
            save(root / f'inbox/control/runtime/ticks/{role}.json', stamp)
        stamp.update({'skill': data['roles'][role]['skill'], 'session_file': data['roles'][role]['session_file']})
        return stamp
    if args.action == 'status':
        lease = load(lease_path(root, role), {})
        return {'role': role, 'legacy_busy': legacy_busy(data, role),
                'lease': {k: v for k, v in lease.items() if k != 'token'},
                'owner_process_state': owner_state(lease) if lease else 'none',
                'last_tick': load(root / f'inbox/control/runtime/ticks/{role}.json', {})}
    if args.action == 'recover':
        guard(data, role)
        with locked(lease_lock(root, role)):
            old = load(lease_path(root, role), {})
            if not old or old.get('owner') != args.owner:
                raise ValueError('Recovery owner does not match; recheck status')
            # A verified exit needs no TTL sleep. Unbound legacy leases retain
            # their existing expiry + explicit verification recovery protocol.
            state = owner_state(old)
            if state != 'exited_or_reused' and old.get('expires_at', 0) > dt.datetime.now().timestamp():
                raise ValueError('Lease not expired; no forced takeover')
            identity = pid_identity(old['pid']) if old.get('pid') else None
            if old.get('pid') and identity is not None and (identity == 'UNVERIFIABLE' or not old.get('process_start') or identity == old['process_start']):
                raise ValueError('Owner process is still alive; no takeover')
            if not args.confirmed_stopped or not args.evidence or not Path(args.evidence).is_file():
                raise ValueError('Recovery needs confirmed stopped owner and an existing verification evidence file')
            record = record_recovery(root, role, old, reason='EXPLICIT_STOP_VERIFICATION',
                                     verification_evidence=str(Path(args.evidence).resolve()))
            lease_path(root, role).unlink()
            return {'recovered': True, 'evidence': record['verification_evidence']}
    if args.action == 'claim':
        guard(data, role)
        verify_worker_pid(args.pid)
        with locked(lease_lock(root, role)):
            # Capture once; never persist UNVERIFIABLE as if it identified a worker.
            process_start = verified_pid_identity(args.pid) if args.pid is not None else None
            old = load(lease_path(root, role), {})
            if old:
                # Expiration alone never proves that an owner has stopped writing.
                proven_dead = owner_state(old) == 'exited_or_reused'
                if not proven_dead:
                    return {'claimed': False, 'reason': 'OWNER_BUSY_OR_UNVERIFIED',
                            'owner': old.get('owner'), 'expires_at': old.get('expires_at')}
                record_recovery(root, role, old, reason='BOUND_OWNER_EXITED_OR_PID_REUSED',
                                verification_source='/proc/PID/stat start-time identity')
            lease = {'owner': args.owner, 'token': uuid.uuid4().hex, 'claimed_at': now(),
                     'expires_at': dt.datetime.now().timestamp() + args.ttl,
                     'pid': args.pid, 'process_start': process_start}
            if args.pid is None:
                # Keep the history honest: an unbound lease is a weaker guarantee,
                # not an equivalent one, so later readers can tell the two apart.
                lease['binding'] = 'unbound'
            save(lease_path(root, role), lease)
            result = {'claimed': True, **lease}
            if args.pid is None:
                result['warning'] = _UNBOUND_NOTE
            return result
    if args.action in ('renew', 'release', 'run', 'event', 'timeline'):
        with locked(lease_lock(root, role)):
            if args.action != 'release':
                guard(data, role)
            # The same owner may release or renew an expired token if no takeover replaced it.
            lease = valid_lease(root, role, args.token, allow_expired=args.action in ('release', 'renew'),
                                verify_owner=args.action != 'release')
            if args.action == 'renew':
                if args.pid is not None:
                    verify_worker_pid(args.pid)
                    process_start = verified_pid_identity(args.pid)
                    if lease.get('pid') and (lease['pid'] != args.pid or lease.get('process_start') != process_start):
                        raise ValueError('Cannot transfer lease to another worker; release and claim normally')
                    lease.update(pid=args.pid, process_start=process_start)
                lease['expires_at'] = dt.datetime.now().timestamp() + args.ttl
                save(lease_path(root, role), lease)
                return {'renewed': True, 'expires_at': lease['expires_at']}
            if args.action == 'release':
                lease_path(root, role).unlink()
                return {'released': True}
            if args.action == 'run':
                command = args.command[1:] if args.command[:1] == ['--'] else args.command
                if not command:
                    raise ValueError('Missing command')
                # Only short control mutations belong here. Long OCR/tests keep separate locks.
                return subprocess.run(command, cwd=root, timeout=min(args.timeout, 90)).returncode
            if args.action == 'event':
                if args.occurred_at:
                    if not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d(?:\.\d+)?)?(?:Z|[+-]\d\d:\d\d)', args.occurred_at):
                        raise ValueError('occurred_at needs date, known minutes and timezone; unknown history uses runtime_tools role-record')
                    dt.datetime.fromisoformat(args.occurred_at.replace('Z', '+00:00'))
                row = {'event_id': args.event_id, 'occurred_at': args.occurred_at or now(),
                       'recorded_at': now(), 'role': role, 'task': args.task, 'attempt': args.attempt,
                       'status': args.status, 'summary': args.summary, 'evidence': args.evidence, 'next': args.next}
                path = root / 'events' / role / (hashlib.sha256(args.event_id.encode()).hexdigest() + '.json')
                old = load(path)
                if old:
                    # Retrying an ID is safe, but may not silently replace another event.
                    fields = ('role', 'task', 'attempt', 'status', 'summary', 'evidence', 'next')
                    if any(old[k] != row[k] for k in fields):
                        raise ValueError('EVENT_ID_CONFLICT')
                    return {'created': False, 'path': str(path)}
                save(path, row)
                return {'created': True, 'path': str(path)}
            if role != 'COORDINATOR':
                raise ValueError('Only coordinator can write TIMELINE')
            from document_store import append_record, indexed_markers
            target = root / 'TIMELINE.md'
            markers = indexed_markers(root, target)
            rows = [_event_row(p) for p in (root / 'events').glob('*/*.json')]
            rows = [row for row in rows if row]
            added = 0
            for row in sorted(rows, key=lambda r: (r.get('occurred_at') or r.get('recorded_at') or '', r['event_id'])):
                marker = '<!-- role-event:' + hashlib.sha256(row['event_id'].encode()).hexdigest() + ' -->'
                if marker in markers:
                    continue
                clean = lambda value: str(value).replace('\n', ' ').replace('\r', ' ')
                text = '\n' + marker + '\n- ' + clean(row.get('occurred_at') or '发生时间未知') + ' ' + clean(row['role'])
                text += ' ' + clean(row['task']) + '/' + clean(row['attempt']) + ' ' + clean(row['status'])
                text += '：' + clean(row['summary']) + '；证据：' + clean(row['evidence'])
                text += '；下一步：' + clean(row['next']) + '；补录：' + clean(row['recorded_at']) + '\n'
                checkpoint = load(root/'inbox/control/runtime/checkpoints/COORDINATOR.json', {}) or {}
                checkpoint = {'role': 'COORDINATOR', 'next': '读取当前 TODO/HANDOFF 与有效派单接续', **checkpoint}
                append_record(root, target, marker, text, checkpoint=checkpoint, writer=atomic)
                markers.add(marker)
                added += 1
            return {'appended': added, 'path': str(target)}
    raise ValueError('Unknown action')


def _event_row(p):
    """Tolerant load: legacy events lack occurred_at/recorded_at; bad JSON is skipped."""
    try:
        row = load(p)
    except Exception:
        return None
    if not isinstance(row, dict) or 'event_id' not in row:
        return None
    row.setdefault('occurred_at', row.get('checked_at') or row.get('time') or row.get('ts') or '')
    row.setdefault('recorded_at', '')
    row.setdefault('role', '')
    row.setdefault('task', 'none')
    row.setdefault('attempt', 'none')
    row.setdefault('status', '')
    row.setdefault('summary', '')
    row.setdefault('evidence', '')
    row.setdefault('next', '')
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['worker-identity', 'resolve', 'preflight', 'status', 'claim', 'renew', 'release', 'recover', 'run', 'event', 'timeline'])
    parser.add_argument('--project')
    parser.add_argument('--role', choices=ROLES)
    parser.add_argument('--registry', default='/home/wang/.hermes/role-projects.json')
    parser.add_argument('--chat-id')
    parser.add_argument('--mode', choices=['poll', 'report'], default='poll')
    parser.add_argument('--owner')
    parser.add_argument('--pid', type=int)
    parser.add_argument('--token')
    parser.add_argument('--ttl', type=int, default=300)
    parser.add_argument('--timeout', type=int, default=60)
    parser.add_argument('--confirmed-stopped', action='store_true')
    for key in ['event-id', 'occurred-at', 'task', 'attempt', 'status', 'summary', 'evidence', 'next']:
        parser.add_argument('--' + key)
    raw = sys.argv[1:]
    split = raw.index('--') if '--' in raw else len(raw)
    args = parser.parse_args(raw[:split])
    args.command = raw[split + 1:] if split < len(raw) else []
    if args.command and args.action != 'run':
        parser.error('Only run accepts a command after --')
    if args.action == 'worker-identity' and args.pid is not None:
        parser.error('worker-identity only identifies its own caller; --pid is not accepted')
    if args.action not in ('resolve', 'worker-identity') and (not args.project or not args.role):
        parser.error('--project and --role are required')
    if args.action == 'claim' and (not args.owner or not 30 <= args.ttl <= 600):
        parser.error('claim needs --owner and ttl between 30 and 600 seconds')
    if args.action == 'renew' and not 30 <= args.ttl <= 600:
        parser.error('ttl between 30 and 600 seconds')
    if args.action == 'event' and not all(getattr(args, k) for k in ['event_id','task','attempt','status','summary','evidence','next']):
        parser.error('event identity, summary, evidence and next are required; use explicit none values')
    try:
        result = execute(args)
        if isinstance(result, int):
            return result
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
