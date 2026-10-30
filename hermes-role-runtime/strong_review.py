#!/usr/bin/env python3
"""Grant, meter and launch read-only Codex reviews; no task or approval decisions."""
import argparse
import contextlib
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from zoneinfo import ZoneInfo

LIMIT = 5
WINDOW = 3600
STATE = Path.home() / '.local/state/complex-task-reviews'
TZ = ZoneInfo('Asia/Shanghai')


class ServiceNotSubmitted(ValueError):
    """The launcher executable was never entered; systemd could not accept a unit."""


def model_settings(path):
    """Parse TOML locally; only model/profile fields may leave the parser."""
    reader = '''import json,sys,tomllib
d=tomllib.loads(sys.stdin.read())
print(json.dumps({**{k:d.get(k) for k in ('model','profile','config_profile_v2')},
 'profiles':{k:{'model':v.get('model')} for k,v in d.get('profiles',{}).items() if isinstance(v,dict)}}))
'''
    try:
        raw = Path(path).read_bytes()
        if len(raw) > 1024*1024:
            raise ValueError('oversized config')
        try:
            import tomllib
        except ModuleNotFoundError:
            interpreter = next((value for name in ('python3.11', 'python3.12', 'python3.13')
                                if (value := shutil.which(name))), None)
            if not interpreter:
                raise ValueError('local TOML parser unavailable')
            result = subprocess.run([interpreter, '-c', reader], input=raw.decode(), capture_output=True,
                                    text=True, timeout=5)
            if result.returncode:
                raise ValueError('invalid config')
            return json.loads(result.stdout)
        data = tomllib.loads(raw.decode())
        return {**{key: data.get(key) for key in ('model', 'profile', 'config_profile_v2')},
                'profiles': {key: {'model': value.get('model')} for key, value in data.get('profiles', {}).items()
                             if isinstance(value, dict)}}
    except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError) as exc:
        # Never include config text, parser diagnostics, credential fields or subprocess stderr.
        raise ValueError('CODEX_DEFAULT_MODEL_CONFIG_UNREADABLE') from exc


def checked_model(model):
    if not isinstance(model, str) or not model or any(ord(char) < 33 for char in model):
        raise ValueError('CODEX_DEFAULT_MODEL_UNDETERMINED: explicit configured or authorized model required')
    return model


def resolve_default_model(repo, target, project=None):
    """Resolve known user/profile settings; reject ambiguous project precedence."""
    codex_home = Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex').expanduser().resolve()
    config = codex_home/'config.toml'
    data = model_settings(config)
    selected = [data.get(key) for key in ('profile', 'config_profile_v2') if data.get(key) is not None]
    if len(selected) > 1:
        raise ValueError('CODEX_DEFAULT_MODEL_PROFILE_AMBIGUOUS')
    model = data.get('model')
    if selected:
        profile = selected[0]
        if not isinstance(profile, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', profile):
            raise ValueError('CODEX_DEFAULT_MODEL_PROFILE_UNDETERMINED')
        legacy = data.get('profiles', {}).get(profile)
        overlay = codex_home/(profile+'.config.toml')
        if legacy is not None and overlay.exists():
            raise ValueError('CODEX_DEFAULT_MODEL_PROFILE_AMBIGUOUS')
        if overlay.exists():
            settings = model_settings(overlay)
            if settings.get('profile') is not None or settings.get('config_profile_v2') is not None:
                raise ValueError('CODEX_DEFAULT_MODEL_PROFILE_AMBIGUOUS')
            model = settings.get('model') if settings.get('model') is not None else model
        elif legacy is not None:
            model = legacy.get('model') if legacy.get('model') is not None else model
        else:
            raise ValueError('CODEX_DEFAULT_MODEL_PROFILE_UNDETERMINED')
    # Trust and project-layer precedence vary by CLI version. Without an explicit
    # authorized model, never guess when a project can override the default.
    directories = set()
    for source in (Path(repo).resolve(), Path(project or repo).resolve()/'reviews/strongmodel/runs'):
        directories.update((source, *source.parents))
    for directory in directories:
        candidate = directory/'.codex/config.toml'
        if candidate.resolve() == config or not candidate.exists():
            continue
        settings = model_settings(candidate)
        if (any(settings.get(key) is not None for key in ('model', 'profile', 'config_profile_v2'))
                or any(value.get('model') is not None for value in settings.get('profiles', {}).values())):
            raise ValueError('CODEX_DEFAULT_MODEL_SCOPE_AMBIGUOUS: explicit model authorization required')
    if git(repo, 'ls-tree', '--name-only', target, '--', '.codex/config.toml').strip():
        raise ValueError('CODEX_DEFAULT_MODEL_SCOPE_AMBIGUOUS: target contains project model configuration')
    return checked_model(model)


def atomic_bytes(path, content, mode=0o600):
    path = Path(path)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def own_cgroup():
    return Path('/proc/self/cgroup').read_text()


def process_cgroup(pid):
    try:
        return Path(f'/proc/{int(pid)}/cgroup').read_text().strip()
    except (OSError, TypeError, ValueError):
        return None


def require_service(unit):
    cgroup = own_cgroup()
    paths = [line.split(':', 2)[-1].split('/') for line in cgroup.splitlines()]
    if not unit or not any(unit in path for path in paths) or 'hermes-worker-cron-' in cgroup:
        raise ValueError('NOT_IN_DEDICATED_SERVICE')
    return cgroup.strip()


def service_info(unit):
    if not unit:
        return {}
    try:
        result = subprocess.run(['systemctl', '--user', 'show', unit,
                                 '--property=LoadState,ActiveState,SubState,MainPID,ControlGroup,Result,ExecMainCode,ExecMainStatus'],
                                capture_output=True, text=True, timeout=5)
        fields = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        if result.returncode and fields.get('LoadState') != 'not-found':
            return {'error': 'SERVICE_STATUS_UNAVAILABLE'}
        return fields if 'ActiveState' in fields else {'error': 'SERVICE_STATUS_UNAVAILABLE'}
    except (OSError, subprocess.TimeoutExpired):
        return {'error': 'SERVICE_STATUS_UNAVAILABLE'}


def check_user_manager():
    """Fail before binding/submission if the required independent manager is unavailable."""
    try:
        result = subprocess.run(['systemctl', '--user', 'show', '--property=Version'],
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError('USER_SYSTEMD_UNAVAILABLE_BEFORE_SUBMISSION') from exc
    if result.returncode:
        # Manager diagnostics may contain environment details; never include them.
        raise ValueError('USER_SYSTEMD_UNAVAILABLE_BEFORE_SUBMISSION')


def start_service(spec):
    directory = Path(spec['run_directory'])
    command = ['systemd-run', '--user', '--no-ask-password', '--unit=' + spec['unit'],
               '--service-type=exec', '--property=Restart=no', '--property=KillMode=control-group',
               '--property=TimeoutStopSec=20', '--property=RuntimeMaxSec=' + str(spec['timeout_seconds'] + 120),
               '--property=StandardOutput=append:' + str(directory / 'service.log'),
               '--property=StandardError=append:' + str(directory / 'service.log'),
               '--working-directory=' + spec['repo'], '--setenv=PATH=' + spec['path'],
               '/usr/bin/python3', str(directory / 'worker.py'), '_worker', '--run', str(directory)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
    except (FileNotFoundError, PermissionError) as exc:
        raise ServiceNotSubmitted('SERVICE_NOT_SUBMITTED: systemd-run could not execute') from exc
    if result.returncode:
        # Do not log manager environment or executable output containing configuration.
        raise ValueError('SERVICE_SUBMISSION_FAILED: independent user systemd service required')


def stamp(value):
    return dt.datetime.fromtimestamp(value, TZ).isoformat(timespec='seconds')


def parse_time(value):
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed.timestamp()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def pid_start(pid):
    try:
        return Path(f'/proc/{int(pid)}/stat').read_text().rsplit(') ', 1)[1].split()[19]
    except (FileNotFoundError, ProcessLookupError):
        return None
    except (OSError, ValueError, IndexError):
        return 'UNKNOWN'


def alive(pid, identity):
    current = pid_start(pid) if pid else None
    return current is not None and (current == 'UNKNOWN' or not identity or current == identity)


def project_binding(project, for_run=False):
    root = Path(project).resolve()
    data = json.loads((root / 'ROLE-BINDINGS.json').read_text())
    if Path(data['project_root']).resolve() != root or 'COORDINATOR' not in data['roles']:
        raise ValueError('INVALID_PROJECT_BINDING')
    if for_run and (data.get('control') != 'ACTIVE' or data.get('migration', {}).get('status') == 'UNBOUND'):
        raise ValueError('PROJECT_NOT_RUNNING: review permission does not start development or bind roles')
    return root, data


def workflow_action(path, scope):
    runtime_plugin=Path(os.environ.get('HERMES_HOME') or Path.home()/'.hermes')/'plugins/role-workflow-runtime'
    if str(runtime_plugin) not in sys.path:
        sys.path.append(str(runtime_plugin))
    from workflow_services import validate_native_action
    return validate_native_action(path,'strong_review',scope)


class Ledger:
    # Tests inject an isolated directory/clock. Production CLI has no state/quota override.
    def __init__(self, directory=STATE, clock=time.time):
        self.directory = Path(directory)
        self.database = self.directory / 'ledger.sqlite3'
        self.clock = clock

    @contextlib.contextmanager
    def transaction(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        con = sqlite3.connect(self.database, timeout=15)
        con.row_factory = sqlite3.Row
        try:
            con.executescript('''
                CREATE TABLE IF NOT EXISTS grants (
                    id TEXT PRIMARY KEY, project TEXT NOT NULL, source_id TEXT NOT NULL,
                    source_file TEXT NOT NULL, source_hash TEXT NOT NULL, user_quote TEXT NOT NULL,
                    starts REAL NOT NULL, ends REAL, tasks TEXT NOT NULL, model TEXT, repo TEXT NOT NULL,
                    revoked REAL, revoke_reason TEXT, UNIQUE(project, source_id));
                CREATE TABLE IF NOT EXISTS calls (
                    id TEXT PRIMARY KEY, project TEXT NOT NULL, grant_id TEXT NOT NULL,
                    review_id TEXT NOT NULL, task_id TEXT NOT NULL, target TEXT NOT NULL,
                    package_hash TEXT NOT NULL, created REAL NOT NULL, status TEXT NOT NULL,
                    wrapper_pid INTEGER, wrapper_start TEXT, child_pid INTEGER, child_start TEXT,
                    result_path TEXT NOT NULL, log_path TEXT NOT NULL,
                    finished REAL, detail TEXT, usage TEXT, result_hash TEXT);
                CREATE INDEX IF NOT EXISTS calls_created ON calls(created);
                CREATE INDEX IF NOT EXISTS calls_identity ON calls(project, review_id, target, package_hash);
            ''')
            os.chmod(self.database, 0o600)
            con.execute('BEGIN IMMEDIATE')
            columns = {row['name'] for row in con.execute('PRAGMA table_info(calls)')}
            for name in ('unit', 'spec_path', 'spec_hash', 'wrapper_cgroup', 'request_hash'):
                if name not in columns:
                    con.execute('ALTER TABLE calls ADD COLUMN ' + name + ' TEXT')
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def grant(self, project, source_id, source_file, quote, starts, ends, tasks=(), model=None, repo=None):
        source_file = Path(source_file).resolve()
        if not source_id.strip() or not quote.strip() or not source_file.is_file():
            raise ValueError('EXPLICIT_USER_EVIDENCE_REQUIRED')
        if not math.isfinite(starts) or (ends is not None and (not math.isfinite(ends) or ends <= max(starts, self.clock()))):
            raise ValueError('INVALID_AUTHORIZATION_INTERVAL')
        with self.transaction() as con:
            old = con.execute('SELECT * FROM grants WHERE project=? AND source_id=?', (project, source_id)).fetchone()
            if old:
                return dict(old)  # Re-reading one user message never renews authorization.
            con.execute('UPDATE grants SET revoked=?, revoke_reason=? WHERE project=? AND revoked IS NULL',
                        (self.clock(), 'replaced by new explicit user authorization', project))
            key = uuid.uuid4().hex
            con.execute('INSERT INTO grants VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,NULL)',
                        (key, project, source_id, str(source_file), digest(source_file), quote,
                         starts, ends, json.dumps(list(tasks)), model, str(Path(repo or project).resolve())))
            return dict(con.execute('SELECT * FROM grants WHERE id=?', (key,)).fetchone())

    def revoke(self, project, reason):
        with self.transaction() as con:
            n = con.execute('UPDATE grants SET revoked=?, revoke_reason=? WHERE project=? AND revoked IS NULL',
                            (self.clock(), reason, project)).rowcount
        return {'revoked': n, 'reason': reason}

    def _authorization(self, con, project, task_id=None):
        row = con.execute('SELECT * FROM grants WHERE project=? ORDER BY rowid DESC LIMIT 1', (project,)).fetchone()
        now = self.clock()
        if not row or row['revoked'] is not None:
            raise ValueError('NO_ACTIVE_AUTHORIZATION')
        if now < row['starts']:
            raise ValueError('AUTHORIZATION_NOT_STARTED')
        if row['ends'] is not None and now >= row['ends']:
            raise ValueError('AUTHORIZATION_EXPIRED')
        tasks = json.loads(row['tasks'])
        if task_id and tasks and task_id not in tasks:
            raise ValueError('TASK_OUTSIDE_AUTHORIZATION')
        return row

    def reserve(self, project, review_id, task_id, target, package_hash, retry_of=None, repo=None, *,
                base=None, timeout_seconds=None, risk=None, question=None, ac=None, gate=None,
                workflow_authorization=None):
        with self.transaction() as con:
            if workflow_authorization:
                grant={'id':'workflow:'+workflow_authorization['authorization_id'],
                       'repo':workflow_authorization['repo'],'model':None}
                if (workflow_authorization['project']!=project or workflow_authorization['task_id']!=task_id
                    or workflow_authorization['action_id']!=review_id):
                    raise ValueError('WORKFLOW_REVIEW_AUTHORIZATION_SCOPE_MISMATCH')
            else:
                grant = self._authorization(con, project, task_id)
            if repo is not None and grant['repo'] != str(Path(repo).resolve()):
                raise ValueError('REPOSITORY_OUTSIDE_AUTHORIZATION')
            model = checked_model(grant['model']) if grant['model'] is not None else resolve_default_model(repo or grant['repo'], target, project)
            identity = {'schema_version': 1, 'project': project, 'review_id': review_id, 'task_id': task_id,
                        'repo': str(Path(repo or grant['repo']).resolve()), 'base': base, 'target': target,
                        'package_hash': package_hash, 'model': model, 'timeout_seconds': timeout_seconds,
                        'risk': risk, 'question': question, 'ac': ac, 'gate': gate, 'decision': 'REVIEW_NOW'}
            request_hash = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True,
                                                    separators=(',', ':')).encode()).hexdigest()
            prior = con.execute('SELECT * FROM calls WHERE project=? AND review_id=? ORDER BY rowid DESC LIMIT 1',
                                (project, review_id)).fetchone()
            if prior:
                if not prior['request_hash']:
                    raise ValueError('LEGACY_REQUEST_IDENTITY_UNVERIFIABLE: inspect original evidence; do not silently reuse or retry')
                if prior['request_hash'] != request_hash:
                    raise ValueError('REVIEW_INPUT_IDENTITY_CONFLICT: frozen request changed; use a new review_id')
                if prior['status'] == 'SUCCEEDED':
                    if not Path(prior['result_path']).is_file() or digest(prior['result_path']) != prior['result_hash']:
                        raise ValueError('RESULT_EVIDENCE_CHANGED: inspect instead of automatically recalling')
                    return {**dict(prior), 'reused': True}
                if prior['status'] in ('RESERVED', 'RUNNING'):
                    if prior['unit']:
                        return {**dict(prior), 'reused': True}
                    raise ValueError('REVIEW_ALREADY_IN_FLIGHT')
                if retry_of != prior['id']:
                    raise ValueError('FAILED_REVIEW_REQUIRES_EXPLICIT_RETRY_OF: no automatic retry loop')
            elif retry_of:
                raise ValueError('RETRY_ID_DOES_NOT_MATCH_REVIEW')
            if con.execute("SELECT 1 FROM calls WHERE status IN ('RESERVED','RUNNING') LIMIT 1").fetchone():
                raise ValueError('GLOBAL_REVIEW_BUSY: check the existing process, do not launch another')
            count = con.execute('SELECT count(*) FROM calls WHERE created>?', (self.clock() - WINDOW,)).fetchone()[0]
            if count >= LIMIT:
                raise ValueError('ROLLING_HOUR_LIMIT: maximum five reserved review starts across projects')
            key = uuid.uuid4().hex
            output_dir = Path(project) / 'reviews/strongmodel/runs' / key
            con.execute('''INSERT INTO calls
                (id,project,grant_id,review_id,task_id,target,package_hash,created,status,
                 wrapper_pid,wrapper_start,result_path,log_path,request_hash)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (key, project, grant['id'], review_id, task_id, target, package_hash, self.clock(), 'RESERVED',
                 os.getpid(), pid_start(os.getpid()), str(output_dir / 'result.md'), str(output_dir / 'codex.jsonl'), request_hash))
            return {**dict(con.execute('SELECT * FROM calls WHERE id=?', (key,)).fetchone()), 'model': model, 'reused': False}

    def bind_service(self, call_id, unit, spec_path, spec_hash):
        with self.transaction() as con:
            call = con.execute('SELECT * FROM calls WHERE id=?', (call_id,)).fetchone()
            if not call or call['status'] != 'RESERVED' or call['unit']:
                raise ValueError('CALL_NOT_RESERVED_OR_ALREADY_BOUND')
            con.execute('UPDATE calls SET unit=?,spec_path=?,spec_hash=? WHERE id=?',
                        (unit, str(spec_path), spec_hash, call_id))

    def call(self, call_id):
        with self.transaction() as con:
            call = con.execute('SELECT * FROM calls WHERE id=?', (call_id,)).fetchone()
            if not call:
                raise ValueError('UNKNOWN_CALL')
            return dict(call)

    def adopt_worker(self, call_id, spec_hash):
        with self.transaction() as con:
            call = con.execute('SELECT * FROM calls WHERE id=?', (call_id,)).fetchone()
            if not call or call['status'] != 'RESERVED' or call['spec_hash'] != spec_hash:
                raise ValueError('INVALID_WORKER_CLAIM')
            cgroup = require_service(call['unit'])
            if call['wrapper_cgroup']:
                raise ValueError('WORKER_ALREADY_ADOPTED')
            con.execute('UPDATE calls SET wrapper_pid=?,wrapper_start=?,wrapper_cgroup=? WHERE id=?',
                        (os.getpid(), pid_start(os.getpid()), cgroup, call_id))
            return dict(con.execute('SELECT * FROM calls WHERE id=?', (call_id,)).fetchone())

    def launch(self, call_id, launch_process):
        # Revoke and actual subprocess start serialize under the same global transaction.
        with self.transaction() as con:
            call = con.execute('SELECT * FROM calls WHERE id=?', (call_id,)).fetchone()
            if not call or call['status'] != 'RESERVED':
                raise ValueError('CALL_NOT_RESERVED')
            if call['grant_id'].startswith('workflow:'):
                raw=Path(call['spec_path']).read_bytes()
                if hashlib.sha256(raw).hexdigest()!=call['spec_hash']:
                    raise ValueError('WORKFLOW_SERVICE_SPEC_CHANGED')
                frozen=json.loads(raw)
                grant=workflow_action(frozen['workflow_action'],{'project':call['project'],'repo':frozen['repo'],
                    'base':frozen['base'],'head':call['target'],'task':call['task_id'],'review_id':call['review_id']})
                if 'workflow:'+grant['authorization_id']!=call['grant_id']:
                    raise ValueError('WORKFLOW_AUTHORIZATION_REPLACED_BEFORE_LAUNCH')
            else:
                grant = self._authorization(con, call['project'], call['task_id'])
                if grant['id'] != call['grant_id']:
                    raise ValueError('AUTHORIZATION_REPLACED_BEFORE_LAUNCH')
                project_binding(call['project'], for_run=True)
            if call['unit']:
                require_service(call['unit'])
            if call['grant_id'].startswith('workflow:'):
                from workflow_services import native_dispatch_guard
                with native_dispatch_guard(frozen['workflow_action'],'strong_review',{'project':call['project'],
                    'repo':frozen['repo'],'base':frozen['base'],'head':call['target'],
                    'task':call['task_id'],'review_id':call['review_id']}):
                    child = launch_process()
            else:
                child = launch_process()
            con.execute('UPDATE calls SET status=?,child_pid=?,child_start=? WHERE id=?',
                        ('RUNNING', child.pid, pid_start(child.pid), call_id))
            return child

    def finish(self, call_id, status, detail, usage=None):
        with self.transaction() as con:
            call = con.execute('SELECT * FROM calls WHERE id=?', (call_id,)).fetchone()
            if not call:
                raise ValueError('UNKNOWN_CALL')
            result_hash = digest(call['result_path']) if status == 'SUCCEEDED' else None
            con.execute('UPDATE calls SET status=?,finished=?,detail=?,usage=?,result_hash=? WHERE id=?',
                        (status, self.clock(), detail, json.dumps(usage), result_hash, call_id))

    def fail_unsubmitted(self, call_id, detail):
        # An externally adopted worker must never be overwritten by a launcher failure.
        with self.transaction() as con:
            con.execute("""UPDATE calls SET status='FAILED',finished=?,detail=?
                           WHERE id=? AND status='RESERVED'
                           AND wrapper_cgroup IS NULL AND child_pid IS NULL""",
                        (self.clock(), detail, call_id))

    def recover(self, call_id, evidence):
        evidence = Path(evidence).resolve()
        if not evidence.is_file():
            raise ValueError('STOPPED_PROCESS_EVIDENCE_REQUIRED')
        with self.transaction() as con:
            call = con.execute('SELECT * FROM calls WHERE id=?', (call_id,)).fetchone()
            if not call or call['status'] not in ('RESERVED', 'RUNNING'):
                raise ValueError('NO_UNRESOLVED_CALL')
            if call['unit']:
                service = service_info(call['unit'])
                if service.get('ActiveState') not in ('inactive', 'failed'):
                    raise ValueError('SERVICE_STILL_ALIVE_OR_UNVERIFIABLE')
                if service.get('MainPID') not in (None, '', '0') and pid_start(service['MainPID']) is not None:
                    raise ValueError('SERVICE_STILL_ALIVE_OR_UNVERIFIABLE')
                group = service.get('ControlGroup', '')
                if group:
                    try:
                        members = (Path('/sys/fs/cgroup') / group.lstrip('/') / 'cgroup.procs').read_text().strip()
                        if members:
                            raise ValueError('SERVICE_STILL_ALIVE_OR_UNVERIFIABLE')
                    except FileNotFoundError:
                        pass  # Inactive units may already have removed their empty cgroup.
                    except OSError as exc:
                        raise ValueError('SERVICE_STILL_ALIVE_OR_UNVERIFIABLE') from exc
            if alive(call['wrapper_pid'], call['wrapper_start']) or alive(call['child_pid'], call['child_start']):
                raise ValueError('REVIEW_PROCESS_STILL_ALIVE_OR_UNVERIFIABLE')
            con.execute('UPDATE calls SET status=?,finished=?,detail=? WHERE id=?',
                        ('LOST', self.clock(), 'verified stopped; inspect partial result: ' + str(evidence), call_id))
        return {'recovered': call_id, 'quota_refunded': False}

    def status(self, project):
        empty = {'project': project, 'authorization': 'MANUAL_ONLY', 'used_last_60_minutes': 0,
                 'limit': LIMIT, 'active_calls': [], 'last_call': None, 'tokens': None, 'cost': None}
        if not self.database.exists():
            return empty
        con = sqlite3.connect(self.database.as_uri() + '?mode=ro', uri=True, timeout=15)
        con.row_factory = sqlite3.Row
        try:
            grant = con.execute('SELECT * FROM grants WHERE project=? ORDER BY rowid DESC LIMIT 1', (project,)).fetchone()
            calls = con.execute('SELECT created FROM calls WHERE created>? ORDER BY created DESC', (self.clock() - WINDOW,)).fetchall()
            columns = {row['name'] for row in con.execute('PRAGMA table_info(calls)')}
            service_fields = ',unit,spec_path,wrapper_pid,wrapper_start,child_pid,child_start,wrapper_cgroup' if 'unit' in columns else ''
            active = [dict(row) for row in con.execute("SELECT id,project,review_id,status,created,result_path,log_path" + service_fields + " FROM calls WHERE status IN ('RESERVED','RUNNING')")]
            last = con.execute('SELECT id,review_id,status,created,finished,result_path,log_path,usage' + service_fields + ' FROM calls WHERE project=? ORDER BY rowid DESC LIMIT 1', (project,)).fetchone()
            def observed(row):
                row = dict(row)
                info = service_info(row.get('unit'))
                row['service'] = info
                row['service_state'] = ('LEGACY' if not row.get('unit') else 'UNKNOWN' if 'error' in info else
                                        'ACTIVE' if info.get('ActiveState') == 'active' else
                                        'SUBMITTED' if info.get('ActiveState') == 'activating' else 'EXITED')
                row['wrapper_alive'] = alive(row.get('wrapper_pid'), row.get('wrapper_start'))
                row['child_alive'] = alive(row.get('child_pid'), row.get('child_start'))
                row['actual_wrapper_cgroup'] = process_cgroup(row.get('wrapper_pid')) if row['wrapper_alive'] else None
                row['service_identity'] = ('LEGACY' if not row.get('unit') else
                    'VERIFIED' if row['wrapper_alive'] and row.get('wrapper_cgroup') == row['actual_wrapper_cgroup']
                    and str(row.get('wrapper_pid')) == info.get('MainPID')
                    and info.get('ControlGroup', '').endswith('/' + row['unit']) else
                    'STOPPED' if row['service_state'] == 'EXITED' and not row['wrapper_alive'] and not row['child_alive'] else 'UNKNOWN')
                return row
            active = [observed(row) for row in active]
            result = {**empty, 'used_last_60_minutes': len(calls), 'active_calls': active,
                      'next_slot_at': stamp(calls[LIMIT - 1]['created'] + WINDOW) if len(calls) >= LIMIT else None,
                      'last_call': observed(last) if last else None}
            if grant:
                state = 'REVOKED' if grant['revoked'] is not None else 'SCHEDULED' if self.clock() < grant['starts'] else 'EXPIRED' if grant['ends'] is not None and self.clock() >= grant['ends'] else 'AUTHORIZED'
                _, binding = project_binding(project)
                if binding.get('control') == 'COMPLETE':
                    state = 'SCOPE_COMPLETE'
                elif binding.get('control') != 'ACTIVE':
                    state = 'PROJECT_' + str(binding.get('control'))
                result.update({'authorization': state, 'grant_id': grant['id'], 'starts_at': stamp(grant['starts']),
                               'expires_at': stamp(grant['ends']) if grant['ends'] is not None else 'until authorized task scope completes',
                               'task_scope': json.loads(grant['tasks']), 'source_id': grant['source_id'],
                               'repository': grant['repo'],
                               'calls_under_authorization': con.execute('SELECT count(*) FROM calls WHERE grant_id=?', (grant['id'],)).fetchone()[0]})
            if last and last['usage']:
                result['tokens'] = json.loads(last['usage'])
            return result
        finally:
            con.close()


def git(repo, *args):
    return subprocess.check_output(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(repo), *args], text=True, stderr=subprocess.PIPE).strip()


def run_review(args, ledger):
    """Freeze a request and submit a short-lived launcher, never wait for Codex."""
    if os.environ.get('HERMES_ROLE_POOL_DRAIN_ONLY')=='1':
        raise ValueError('需求切换收口回合不能提交新强审')
    action_path=getattr(args,'workflow_action',None)
    workflow_authorization=None
    if action_path:
        workflow_authorization=workflow_action(action_path,{'project':str(Path(args.project).resolve()),
            'repo':str(Path(args.repo).resolve()),'base':args.base,'head':args.target,
            'task':args.task_id,'review_id':args.review_id})
        project=Path(workflow_authorization['project'])
    else:
        project, _ = project_binding(args.project, for_run=True)
    repo = Path(args.repo).resolve()
    package = Path(args.package).resolve()
    if not package.is_relative_to(project) or not package.is_file():
        raise ValueError('REVIEW_PACKAGE_MUST_BE_IN_BOUND_PROJECT')
    for text in (args.risk, args.question, args.ac, args.gate):
        if not text.strip():
            raise ValueError('SPECIFIC_RISK_QUESTION_AC_AND_GATE_REQUIRED')
    if args.timeout_seconds <= 0:
        raise ValueError('TIMEOUT_MUST_BE_POSITIVE')
    base, target = [git(repo, 'rev-parse', '--verify', '--end-of-options', value + '^{commit}') for value in (args.base, args.target)]
    package_bytes = package.read_bytes()
    package_text = package_bytes.decode()
    call = ledger.reserve(str(project), args.review_id, args.task_id, target,
                          hashlib.sha256(package_bytes).hexdigest(), args.retry_of, str(repo),
                          base=base, timeout_seconds=args.timeout_seconds, risk=args.risk,
                          question=args.question, ac=args.ac, gate=args.gate,
                          workflow_authorization=workflow_authorization)
    if call['reused']:
        return {**call, 'call_id': call['id'], 'approval': 'NOT_DECIDED'}
    folder = Path(call['result_path']).parent
    bound = False
    try:
        folder.mkdir(parents=True, exist_ok=False, mode=0o700)
        reviewer = Path.home() / '.codex/skills/complex-task-reviewer/SKILL.md'
        prompt = '\n\n'.join([
            reviewer.read_text(),
            'You are a read-only reviewer authorized by the user. No code edits, model delegation, merges, deployments or extra review calls.',
            json.dumps({'review_id': args.review_id, 'grant_id': call['grant_id'], 'call_id': call['id'],
                        'base': base, 'target': target, 'task': args.task_id, 'gate': args.gate,
                        'risk': args.risk, 'question': args.question, 'AC': args.ac}, ensure_ascii=False),
            'Return the complete independent result as your final message; the wrapper writes it. Do not try to write a result file in the read-only sandbox. Freeze existing requirements; OUT_OF_SCOPE suggestions do not block.',
            'The following is the frozen review package. Read actual code in this detached snapshot and the cited verification evidence; do not merely trust the summary.',
            package_text])
        codex = shutil.which('codex')
        if not codex:
            raise ValueError('CODEX_EXECUTABLE_NOT_FOUND')
        atomic_bytes(folder / 'input.md', prompt.encode())
        atomic_bytes(folder / 'package.md', package_bytes)
        atomic_bytes(folder / 'worker.py', Path(__file__).read_bytes(), mode=0o700)
        runtime_plugin = Path(os.environ.get('HERMES_HOME') or Path.home()/'.hermes')/'plugins/role-workflow-runtime'
        if str(runtime_plugin) not in sys.path:
            sys.path.append(str(runtime_plugin))
        from workflow_migration import code_version
        spec = {'schema_version': 1, 'runtime_version_digest': code_version()['digest'],
                'call_id': call['id'], 'project': str(project), 'repo': str(repo),
                'base': base, 'target': target, 'review_id': args.review_id, 'task_id': args.task_id,
                'grant_id': call['grant_id'], 'package_hash': call['package_hash'],
                'request_hash': call['request_hash'],
                'model': call.get('model'), 'timeout_seconds': args.timeout_seconds,
                'run_directory': str(folder), 'unit': 'complex-task-strong-review-' + call['id'] + '.service',
                'input_hash': digest(folder / 'input.md'), 'worker_hash': digest(folder / 'worker.py'),
                'codex': str(Path(codex).absolute()), 'path': os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin')}
        if action_path:
            spec['workflow_action']=str(Path(action_path).resolve())
        atomic_bytes(folder / 'spec.json', json.dumps(spec, ensure_ascii=False, sort_keys=True).encode())
        check_user_manager()
        ledger.bind_service(call['id'], spec['unit'], folder / 'spec.json', digest(folder / 'spec.json'))
        bound = True
        start_service(spec)
        current = ledger.call(call['id'])
        phase = 'SUBMITTED' if current['status'] == 'RESERVED' else 'ACTIVE' if current['status'] == 'RUNNING' else 'EXITED'
        return {**current, 'call_id': call['id'], 'service_state': phase,
                'reused': False, 'approval': 'NOT_DECIDED'}
    except Exception as exc:
        if not bound:
            ledger.finish(call['id'], 'FAILED', str(exc))
        elif isinstance(exc, ServiceNotSubmitted):
            ledger.fail_unsubmitted(call['id'], str(exc))
        else:
            # A timeout or caller death may happen after systemd accepted the unit.
            # Retain an uncertain reservation; never declare it safe to retry.
            info = service_info(spec['unit'])
            current = ledger.call(call['id'])
            if info.get('ActiveState') in ('inactive', 'failed') and info.get('MainPID', '0') == '0' and current['status'] == 'RESERVED' and not current['wrapper_cgroup']:
                ledger.finish(call['id'], 'FAILED', str(exc))
        raise


def stop_child(child):
    if child is None or child.poll() is not None:
        return
    try:
        os.killpg(child.pid, signal.SIGTERM)
        child.wait(timeout=10)
    except ProcessLookupError:
        pass
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait()


def worker(directory, ledger=None):
    """Execute only an already reserved, hash-bound request inside its own service."""
    ledger = ledger or Ledger()
    folder = Path(directory).resolve()
    spec_path = folder / 'spec.json'
    raw = spec_path.read_bytes()
    spec = json.loads(raw)
    call = ledger.call(spec['call_id'])
    if (call.get('spec_path') != str(spec_path) or call.get('spec_hash') != hashlib.sha256(raw).hexdigest()
            or spec['run_directory'] != str(folder) or spec['unit'] != call.get('unit')
            or spec['project'] != call['project'] or spec['target'] != call['target']
            or spec['package_hash'] != call['package_hash'] or spec['grant_id'] != call['grant_id']
            or spec.get('request_hash') != call.get('request_hash')):
        raise ValueError('FROZEN_SPEC_CHANGED')
    require_service(spec['unit'])
    if spec.get('workflow_action'):
        workflow_action(spec['workflow_action'],{'project':spec['project'],'repo':spec['repo'],
            'base':spec['base'],'head':spec['target'],'task':spec['task_id'],'review_id':spec['review_id']})
    call = ledger.adopt_worker(call['id'], call['spec_hash'])
    repo = Path(spec['repo'])
    snapshot = folder / 'snapshot'
    temporary_result = folder / 'result.tmp'
    child = None
    def interrupted(*_):
        raise ValueError('REVIEW_INTERRUPTED: independent service stopped')
    previous_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    for sig in previous_handlers:
        signal.signal(sig, interrupted)
    try:
        if (digest(folder / 'input.md') != spec['input_hash'] or digest(folder / 'worker.py') != spec['worker_hash']
                or digest(folder / 'package.md') != spec['package_hash']):
            raise ValueError('FROZEN_INPUT_CHANGED')
        if not spec.get('workflow_action'):
            project_binding(call['project'], for_run=True)
        git(repo, 'worktree', 'add', '--detach', str(snapshot), spec['target'])
        argv = [spec['codex'], 'exec', '--sandbox', 'read-only', '-c', 'approval_policy="never"',
                '-C', str(snapshot), '--json', '--output-last-message', str(temporary_result)]
        argv += ['--model', checked_model(spec.get('model'))]
        argv += ['-']
        with Path(call['log_path']).open('wb') as log:
            child = ledger.launch(call['id'], lambda: subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=log,
                                                                       stderr=subprocess.STDOUT, start_new_session=True))
            try:
                child.communicate((folder / 'input.md').read_bytes(), timeout=spec['timeout_seconds'])
            except subprocess.TimeoutExpired:
                stop_child(child)
                raise ValueError('REVIEW_TIMEOUT: failed start remains in hourly count')
        if child.returncode != 0 or not temporary_result.is_file() or not temporary_result.read_text().strip():
            raise ValueError('REVIEW_FAILED_OR_EMPTY_RESULT: inspect log, never assume PASS')
        if git(snapshot, 'rev-parse', 'HEAD') != spec['target'] or git(snapshot, 'status', '--porcelain'):
            raise ValueError('REVIEW_SNAPSHOT_CHANGED')
        os.replace(temporary_result, call['result_path'])
        usage = []
        for line in Path(call['log_path']).read_text(errors='replace').splitlines():
            try:
                event = json.loads(line)
                if event.get('type') == 'turn.completed' and isinstance(event.get('usage'), dict):
                    usage.append(event['usage'])
            except (ValueError, AttributeError):
                continue
        ledger.finish(call['id'], 'SUCCEEDED', 'CLI result received; coordinator must still verify snapshot and findings', usage or None)
        return {**call, 'status': 'SUCCEEDED', 'usage': usage or None, 'cost': None, 'approval': 'NOT_DECIDED'}
    except Exception as exc:
        stop_child(child)
        ledger.finish(call['id'], 'FAILED', str(exc))
        raise
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
        if snapshot.exists():
            # Only remove the snapshot created by this call; never reset the source worktree.
            try:
                git(repo, 'worktree', 'remove', str(snapshot))
            except subprocess.CalledProcessError:
                pass  # Retain unexpected files for inspection; do not force-remove them.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    grant = commands.add_parser('grant')
    grant.add_argument('--project', required=True)
    grant.add_argument('--repo', required=True)
    grant.add_argument('--source-id', required=True)
    grant.add_argument('--source-file', required=True)
    grant.add_argument('--user-quote', help='Optional exact excerpt; by default read the original message from source-file')
    grant.add_argument('--starts-at')
    interval = grant.add_mutually_exclusive_group(required=True)
    interval.add_argument('--duration-minutes', type=float)
    interval.add_argument('--until')
    interval.add_argument('--until-project-complete', action='store_true')
    grant.add_argument('--task-ids', nargs='*', default=[])
    grant.add_argument('--model')
    for name in ['status', 'revoke']:
        part = commands.add_parser(name)
        part.add_argument('--project', required=True)
        if name == 'revoke':
            part.add_argument('--reason', required=True)
    run = commands.add_parser('run')
    for key in ['project', 'repo', 'review-id', 'task-id', 'base', 'target', 'package', 'risk', 'question', 'ac', 'gate']:
        run.add_argument('--' + key, required=True)
    run.add_argument('--decision', choices=['REVIEW_NOW'], required=True)
    run.add_argument('--retry-of')
    run.add_argument('--timeout-seconds', type=int, default=1800)
    run.add_argument('--workflow-action', help=argparse.SUPPRESS)
    recover = commands.add_parser('recover')
    recover.add_argument('--call-id', required=True)
    recover.add_argument('--evidence', required=True)
    worker_parser = commands.add_parser('_worker', help=argparse.SUPPRESS)
    worker_parser.add_argument('--run', required=True)
    args = parser.parse_args()
    try:
        ledger = Ledger()
        if args.action == 'grant':
            project, _ = project_binding(args.project)
            starts = parse_time(args.starts_at) if args.starts_at else ledger.clock()
            if args.duration_minutes is not None and (not math.isfinite(args.duration_minutes) or args.duration_minutes <= 0):
                raise ValueError('DURATION_MUST_BE_FINITE_AND_POSITIVE')
            ends = starts + args.duration_minutes * 60 if args.duration_minutes is not None else parse_time(args.until) if args.until else None
            repo = Path(args.repo).resolve()
            git(repo, 'rev-parse', '--show-toplevel')
            quote = args.user_quote if args.user_quote is not None else Path(args.source_file).read_text()
            result = ledger.grant(str(project), args.source_id, args.source_file, quote, starts, ends, args.task_ids, args.model, str(repo))
        elif args.action == 'run':
            if args.timeout_seconds <= 0:
                raise ValueError('TIMEOUT_MUST_BE_POSITIVE')
            result = run_review(args, ledger)
        elif args.action == 'recover':
            result = ledger.recover(args.call_id, args.evidence)
        elif args.action == '_worker':
            result = worker(args.run, ledger)
        else:
            project, _ = project_binding(args.project)
            result = ledger.status(str(project)) if args.action == 'status' else ledger.revoke(str(project), args.reason)
        print(json.dumps({'success': True, **result}, ensure_ascii=False))
    except (ValueError, OSError, KeyError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(json.dumps({'success': False, 'error': str(exc)}, ensure_ascii=False))
        sys.exit(1)


if __name__ == '__main__':
    main()
