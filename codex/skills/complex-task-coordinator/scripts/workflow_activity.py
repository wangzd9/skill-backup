"""Exact-execution tool boundaries, collected by the existing native hooks.

This is a disposable observation cache. It stores no commands, arguments,
results, prompts, authority or business events. Incomplete hook coverage never
becomes evidence of inactivity, and reading this cache invokes no operation.
"""
from contextlib import contextmanager
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import sqlite3
import time
import uuid

VERSION = 'workflow-tool-hooks/1'
MODEL_VERSION = 'workflow-model-hooks/1'
FIELDS = ('pool', 'generation', 'run_id', 'role', 'work_id', 'task_id', 'attempt',
          'execution_id', 'execution_generation')
LOGGER = logging.getLogger(__name__)
TOOL_KINDS = {
    'terminal': 'shell', 'execute_code': 'code', 'python': 'code',
    'read_file': 'file_read', 'read_files': 'file_read', 'search_files': 'file_read',
    'search': 'search', 'web_search': 'search', 'web_extract': 'read',
    'write_file': 'file_write', 'patch': 'file_write', 'apply_patch': 'file_write',
    'role_development': 'business_tool', 'role_work': 'business_tool',
    'browser': 'browser', 'delegate_task': 'delegation', 'todo': 'planning',
}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _time(value):
    value = time.time() if value is None else value
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('FINITE_ACTIVITY_TIME_REQUIRED')
    return float(value)


def _identity(scope):
    if not isinstance(scope, dict):
        raise ValueError('EXACT_EXECUTION_IDENTITY_REQUIRED')
    identity = {key: scope.get(key) for key in FIELDS}
    identity['pool'] = scope.get('pool') or scope.get('pool_path')
    for key in ('pool', 'run_id', 'role', 'work_id', 'execution_id'):
        if not isinstance(identity[key], str) or not identity[key].strip():
            raise ValueError('EXACT_EXECUTION_IDENTITY_REQUIRED')
    for key in ('generation', 'execution_generation'):
        if type(identity[key]) is not int or identity[key] < (0 if key == 'generation' else 1):
            raise ValueError('EXACT_EXECUTION_IDENTITY_REQUIRED')
    if identity['role'] not in {'COORDINATOR', 'A', 'B', 'C', 'TESTER'}:
        raise ValueError('EXACT_EXECUTION_IDENTITY_REQUIRED')
    coordination = identity['role'] == 'COORDINATOR' and identity['task_id'] is None and identity['attempt'] is None
    if not coordination and (not isinstance(identity['task_id'], str) or not identity['task_id'].strip()
            or type(identity['attempt']) is not int or identity['attempt'] < 1):
        raise ValueError('EXACT_EXECUTION_IDENTITY_REQUIRED')
    identity['pool'] = str(Path(identity['pool']).resolve())
    return identity


def _location(home, identity):
    root = Path(home)/'runtime/workflow-activity'
    key = hashlib.sha256(_json(identity).encode()).hexdigest()
    return root, root/'tools.sqlite3', key


@contextmanager
def _tool_database(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(path, timeout=2)
    try:
        os.chmod(path, 0o600)
        db.execute('PRAGMA foreign_keys=ON')
        # Hook calls can run concurrently. Reserve the writer before reading
        # coverage so two deferred transactions cannot deadlock on upgrade.
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE IF NOT EXISTS coverage (identity_key TEXT PRIMARY KEY, '
                   'identity TEXT NOT NULL, epoch TEXT NOT NULL, started_at REAL NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS tools (identity_key TEXT NOT NULL, '
                   'tool_id TEXT NOT NULL, epoch TEXT NOT NULL, kind TEXT NOT NULL, '
                   'started_at REAL NOT NULL, ended_at REAL, wait_until REAL, outcome TEXT, '
                   'observation_grace_seconds REAL NOT NULL DEFAULT 180, '
                   'PRIMARY KEY(identity_key,tool_id), '
                   'FOREIGN KEY(identity_key) REFERENCES coverage(identity_key))')
        columns = {row[1] for row in db.execute('PRAGMA table_info(tools)')}
        if 'observation_grace_seconds' not in columns:
            db.execute('ALTER TABLE tools ADD COLUMN observation_grace_seconds REAL NOT NULL DEFAULT 180')
        db.execute('CREATE TABLE IF NOT EXISTS terminal_reports (identity_key TEXT NOT NULL, '
                   'tool_id TEXT NOT NULL, reported_at REAL NOT NULL, outcome TEXT NOT NULL, '
                   'end_verified INTEGER NOT NULL, '
                   'FOREIGN KEY(identity_key) REFERENCES coverage(identity_key))')
        with db:
            yield db
    finally:
        db.close()


@contextmanager
def _receipt_lock(path):
    """Serialize native hook writers across threads/processes, bounded to 2 s."""
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(str(path)+'.writer.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.monotonic()+2
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('ACTIVITY_RECEIPT_WRITER_BUSY')
                time.sleep(.005)
        yield
    finally:
        os.close(descriptor)


@contextmanager
def _database(path):
    with _receipt_lock(path), _tool_database(path) as db:
        yield db


def _fault(root, key, reason, at, error=None):
    """Retain a failed receipt even if SQLite is temporarily unavailable.

    These gaps cannot be erased by a later successful boundary. A fresh exact
    execution gets a different identity and starts its own coverage epoch.
    """
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = root/('gap-'+key+'.json')
        # One durable gap suffices to invalidate this epoch; do not overwrite it.
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            details = ({'error_type': type(error).__name__,
                        'sqlite_errorname': getattr(error, 'sqlite_errorname', None)} if error else {})
            stream.write(_json({'rule_version': VERSION, 'reason_code': reason, 'at': at, **details}))
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        pass
    except OSError as exc:
        LOGGER.warning('Tool activity gap receipt unavailable (%s)', type(exc).__name__)


def _record(home, scope, action, *, at=None, tool_call_id=None, tool_name=None, wait_until=None, outcome=None,
            observation_grace_seconds=180, end_verified=True):
    instant = _time(at)
    try:
        identity = _identity(scope)
    except (ValueError, TypeError):
        return {'state': 'INCOMPLETE', 'reason_codes': ['EXACT_EXECUTION_IDENTITY_REQUIRED']}
    root, path, key = _location(home, identity)
    try:
        with _database(path) as db:
            row = db.execute('SELECT identity,epoch,started_at FROM coverage WHERE identity_key=?', (key,)).fetchone()
            if action == 'coverage':
                if row:
                    if json.loads(row[0]) != identity:
                        raise ValueError('COLLECTOR_IDENTITY_MISMATCH')
                    return {'state': 'COVERED', 'collector_epoch': row[1], 'coverage_started_at': row[2]}
                epoch = uuid.uuid4().hex
                db.execute('INSERT INTO coverage VALUES (?,?,?,?)', (key, _json(identity), epoch, instant))
                return {'state': 'COVERED', 'collector_epoch': epoch, 'coverage_started_at': instant}
            if not row:
                raise ValueError('TOOL_COLLECTOR_COVERAGE_NOT_STARTED')
            if json.loads(row[0]) != identity or instant < row[2]:
                raise ValueError('COLLECTOR_IDENTITY_OR_TIME_MISMATCH')
            if not isinstance(tool_call_id, str) or not tool_call_id.strip():
                raise ValueError('TOOL_CALL_ID_REQUIRED')
            old = db.execute('SELECT epoch,kind,started_at,ended_at FROM tools '
                             'WHERE identity_key=? AND tool_id=?', (key, tool_call_id)).fetchone()
            if action == 'start':
                kind = TOOL_KINDS.get(str(tool_name), 'other_tool')
                wait = _time(wait_until) if wait_until is not None else None
                grace = observation_grace_seconds
                if (isinstance(grace, bool) or not isinstance(grace, (int, float))
                        or not math.isfinite(grace) or grace < 1 or grace > 86400):
                    raise ValueError('TOOL_OBSERVATION_GRACE_INVALID')
                if wait is not None and wait < instant:
                    raise ValueError('TOOL_WAIT_CONTRACT_INVALID')
                if old:
                    if old[0] != row[1] or old[1] != kind:
                        raise ValueError('TOOL_CALL_ID_CONFLICT')
                    return {'state': 'DUPLICATE', 'collector_epoch': row[1]}
                db.execute('INSERT INTO tools VALUES (?,?,?,?,?,?,?,?,?)',
                           (key, tool_call_id, row[1], kind, instant, None, wait, None, grace))
            else:
                if not old and str(outcome).upper() == 'BLOCKED' and end_verified is True:
                    # A pre-execution policy refusal has a native terminal but
                    # no executed tool/start. Retain that report without making
                    # up a lifecycle or marking the collector blind.
                    db.execute('INSERT INTO terminal_reports VALUES (?,?,?,?,?)',
                               (key, tool_call_id, instant, 'BLOCKED', 1))
                    return {'state': 'RECORDED', 'disposition': 'BLOCKED_BEFORE_EXECUTION',
                            'collector_epoch': row[1]}
                if not old or old[0] != row[1]:
                    raise ValueError('TOOL_START_RECEIPT_MISSING')
                if instant < old[2]:
                    raise ValueError('TOOL_END_PRECEDES_START')
                label = str(outcome).upper()
                label = label if label in {'SUCCESS', 'ERROR', 'CANCELLED', 'UNKNOWN', 'BLOCKED'} else 'UNKNOWN'
                db.execute('INSERT INTO terminal_reports VALUES (?,?,?,?,?)',
                           (key, tool_call_id, instant, label, int(end_verified is True)))
                if end_verified is not True:
                    # Native timeout/abandonment can publish a terminal while
                    # the underlying worker is still running. Retain the
                    # report separately; never turn it into an actual end.
                    _fault(root, key, 'TOOL_TERMINAL_WITHOUT_VERIFIED_END', instant)
                    return {'state': 'INCOMPLETE', 'collector_epoch': row[1],
                            'reason_codes': ['TOOL_TERMINAL_WITHOUT_VERIFIED_END']}
                if old[3] is not None:
                    return {'state': 'DUPLICATE', 'collector_epoch': row[1]}
                db.execute('UPDATE tools SET ended_at=?,outcome=? WHERE identity_key=? AND tool_id=?',
                           (instant, label, key, tool_call_id))
        return {'state': 'RECORDED', 'collector_epoch': row[1]}
    except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
        reason = str(exc) if isinstance(exc, ValueError) else 'TOOL_ACTIVITY_RECEIPT_UNAVAILABLE'
        _fault(root, key, reason, instant, error=exc)
        return {'state': 'INCOMPLETE', 'reason_codes': [reason], 'error_type': type(exc).__name__}


def start_coverage(home, scope, at=None):
    """Called by a real native on_llm hook for the current execution only."""
    return _record(home, scope, 'coverage', at=at)


def observation_grace_for_tool(tool_name, args=None):
    """Use the enforced test deadline to avoid a shorter observation alarm."""
    if (str(tool_name)=='role_coordination_command' and isinstance(args,dict)
            and args.get('action')=='finalize_delivery'):
        return 3*3600+4*120+180
    if str(tool_name) != 'role_test_command' or not isinstance(args, dict):
        return 180
    payload = args.get('payload') if isinstance(args.get('payload'), dict) else {}
    timeout = payload.get('timeout_seconds')
    if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and math.isfinite(timeout) and timeout > 0:
        # Keep a small collection margin after the enforced deadline. This is
        # an observation threshold, never a timeout or retry authorization.
        return min(86400, max(180, float(timeout) + 180))
    return 180


def record_tool_start(home, scope, tool_call_id, tool_name, at=None, wait_until=None,
                      observation_grace_seconds=180):
    """wait_until is accepted only from an actually enforced tool contract."""
    return _record(home, scope, 'start', at=at, tool_call_id=tool_call_id,
                   tool_name=tool_name, wait_until=wait_until,
                   observation_grace_seconds=observation_grace_seconds)


def record_tool_end(home, scope, tool_call_id, at=None, outcome='UNKNOWN', *, end_verified=True):
    """A native terminal report is an end only when the operation really ended.

    Timeout/abandonment callers must use end_verified=False even if their own
    result wrapper has reached a terminal state. A later report cannot erase
    the resulting historical collector gap.
    """
    return _record(home, scope, 'end', at=at, tool_call_id=tool_call_id, outcome=outcome,
                   end_verified=end_verified)


def read_activity(home, scope, now=None):
    """Read hook coverage and real boundaries without touching the collector.

    Completeness describes receipt coverage, not whether all work has ended.
    Observation grace is separate from an enforced execution deadline.
    """
    instant = _time(now)
    source = {'source_id': 'tools', 'source_version': VERSION, 'check_status': 'UNVERIFIED',
              'complete': False, 'last_verified_at': None, 'max_gap_seconds': 120,
              'coverage_started_at': None, 'reason_codes': [], 'evidence_ref': None}
    result = {'sources': [source], 'lifecycles': [], 'terminal_reports': []}
    try:
        identity = _identity(scope)
        root, path, key = _location(home, identity)
        source['evidence_ref'] = str(path)+'#'+key
        unverified_terminal = False
        with sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=.05) as db:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            row = db.execute('SELECT identity,epoch,started_at FROM coverage WHERE identity_key=?', (key,)).fetchone()
            if not row:
                raise ValueError('TOOL_COLLECTOR_COVERAGE_NOT_STARTED')
            if json.loads(row[0]) != identity or row[2] > instant:
                raise ValueError('COLLECTOR_IDENTITY_OR_TIME_MISMATCH')
            source.update(collector_epoch=row[1], coverage_started_at=row[2])
            operations = db.execute('SELECT tool_id,epoch,kind,started_at,ended_at,wait_until,outcome,observation_grace_seconds '
                'FROM tools WHERE identity_key=? AND ended_at IS NULL ORDER BY started_at LIMIT 513', (key,)).fetchall()
            if len(operations) > 512:
                raise ValueError('OPEN_TOOL_OBSERVATION_LIMIT_REACHED')
            operations += db.execute('SELECT tool_id,epoch,kind,started_at,ended_at,wait_until,outcome,observation_grace_seconds '
                'FROM tools WHERE identity_key=? AND ended_at IS NOT NULL '
                'ORDER BY ended_at DESC LIMIT 512', (key,)).fetchall()
            # Older observation caches predate separate native report facts.
            # An absent table is not an invocation or a reconstructed report.
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='terminal_reports'").fetchone():
                unverified_terminal = bool(db.execute('SELECT 1 FROM terminal_reports '
                    'WHERE identity_key=? AND end_verified=0 LIMIT 1', (key,)).fetchone())
                result['terminal_reports'] = [{'operation_id': tool_id, 'reported_at': reported,
                    'outcome': outcome, 'end_verified': bool(verified)}
                    for tool_id, reported, outcome, verified in db.execute(
                        'SELECT tool_id,reported_at,outcome,end_verified FROM terminal_reports '
                        'WHERE identity_key=? ORDER BY rowid DESC LIMIT 512', (key,))]
        reasons = []
        if unverified_terminal:
            reasons.append('TOOL_TERMINAL_WITHOUT_VERIFIED_END')
        gap = root/('gap-'+key+'.json')
        if gap.exists():
            saved = json.loads(gap.read_text())
            reasons.append(saved['reason_code'])
        for tool_id, epoch, kind, started, ended, wait, outcome, grace in operations:
            if epoch != row[1] or not math.isfinite(started) or started < row[2] or started > instant:
                raise ValueError('TOOL_BOUNDARY_IDENTITY_OR_TIME_INVALID')
            if ended is not None and (not math.isfinite(ended) or not started <= ended <= instant):
                raise ValueError('TOOL_BOUNDARY_IDENTITY_OR_TIME_INVALID')
            if wait is not None and (not math.isfinite(wait) or wait < started):
                raise ValueError('TOOL_WAIT_CONTRACT_INVALID')
            result['lifecycles'].append({'operation_id': tool_id, 'kind': kind,
                'source_id': 'tools', 'verified': True, 'started_at': started,
                'ended_at': ended, 'wait_until': wait, 'outcome': outcome,
                'observation_grace_seconds': grace, 'grace_policy': 'open-observation/1',
                'evidence_ref': str(path)+'#'+key+':'+hashlib.sha256(tool_id.encode()).hexdigest()})
        source.update(check_status='VERIFIED', complete=not reasons,
                      last_verified_at=instant, reason_codes=sorted(set(reasons)))
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        source['reason_codes'] = [str(exc) if isinstance(exc, ValueError)
                                  else 'TOOL_ACTIVITY_SOURCE_UNAVAILABLE']
        source['error_type'] = type(exc).__name__
    return result


@contextmanager
def _model_database_unlocked(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(path, timeout=2)
    try:
        os.chmod(path, 0o600)
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE IF NOT EXISTS coverage (identity_key TEXT PRIMARY KEY, '
                   'identity TEXT NOT NULL, epoch TEXT NOT NULL, started_at REAL NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS attempts (identity_key TEXT NOT NULL, '
                   'physical_id TEXT NOT NULL, request_id TEXT NOT NULL, epoch TEXT NOT NULL, '
                   'kind TEXT NOT NULL, started_at REAL NOT NULL, ended_at REAL, outcome TEXT, '
                   'PRIMARY KEY(identity_key,physical_id), '
                   'FOREIGN KEY(identity_key) REFERENCES coverage(identity_key))')
        db.execute('CREATE TABLE IF NOT EXISTS terminal_reports (identity_key TEXT NOT NULL, '
                   'physical_id TEXT NOT NULL, reported_at REAL NOT NULL, outcome TEXT NOT NULL, '
                   'end_verified INTEGER NOT NULL, '
                   'FOREIGN KEY(identity_key) REFERENCES coverage(identity_key))')
        with db:
            yield db
    finally:
        db.close()


@contextmanager
def _model_database(path):
    with _receipt_lock(path), _model_database_unlocked(path) as db:
        yield db


def _model_location(home, identity):
    root, _, key = _location(home, identity)
    root = root/'model_requests'
    return root, root/'attempts.sqlite3', key


def _record_model(home, scope, action, *, at=None, physical_attempt_id=None,
                  api_request_id=None, kind='main_request', outcome='UNKNOWN', end_verified=True):
    try:
        identity = _identity(scope)
    except (ValueError, TypeError):
        return {'state': 'INCOMPLETE', 'reason_codes': ['EXACT_EXECUTION_IDENTITY_REQUIRED']}
    root, path, key = _model_location(home, identity)
    instant = _time(None)
    try:
        instant = _time(at)
        with _model_database(path) as db:
            coverage = db.execute('SELECT identity,epoch,started_at FROM coverage WHERE identity_key=?', (key,)).fetchone()
            if action == 'coverage':
                if coverage:
                    if json.loads(coverage[0]) != identity:
                        raise ValueError('MODEL_COLLECTOR_IDENTITY_MISMATCH')
                    return {'state': 'COVERED', 'collector_epoch': coverage[1], 'coverage_started_at': coverage[2]}
                epoch = uuid.uuid4().hex
                db.execute('INSERT INTO coverage VALUES (?,?,?,?)', (key, _json(identity), epoch, instant))
                return {'state': 'COVERED', 'collector_epoch': epoch, 'coverage_started_at': instant}
            if not coverage:
                raise ValueError('MODEL_COLLECTOR_COVERAGE_NOT_STARTED')
            if json.loads(coverage[0]) != identity or instant < coverage[2]:
                raise ValueError('MODEL_COLLECTOR_IDENTITY_OR_TIME_MISMATCH')
            if not all(isinstance(value, str) and value.strip() for value in (physical_attempt_id, api_request_id)):
                raise ValueError('MODEL_PHYSICAL_AND_LOGICAL_IDS_REQUIRED')
            old = db.execute('SELECT request_id,epoch,kind,started_at,ended_at FROM attempts '
                'WHERE identity_key=? AND physical_id=?', (key, physical_attempt_id)).fetchone()
            if old and (old[0] != api_request_id or old[1] != coverage[1]):
                raise ValueError('MODEL_PHYSICAL_ID_CONFLICT')
            if action == 'start':
                category = kind if kind in {'main_request', 'compaction', 'auxiliary_request'} else 'auxiliary_request'
                if old:
                    if old[2] != category:
                        raise ValueError('MODEL_PHYSICAL_ID_CONFLICT')
                    return {'state': 'DUPLICATE', 'collector_epoch': coverage[1]}
                db.execute('INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?)',
                    (key, physical_attempt_id, api_request_id, coverage[1], category, instant, None, None))
            else:
                if not old:
                    raise ValueError('MODEL_START_RECEIPT_MISSING')
                if instant < old[3]:
                    raise ValueError('MODEL_END_PRECEDES_START')
                label = str(outcome).upper()
                label = label if label in {'SUCCESS', 'ERROR', 'CANCELLED', 'UNKNOWN'} else 'UNKNOWN'
                db.execute('INSERT INTO terminal_reports VALUES (?,?,?,?,?)',
                    (key, physical_attempt_id, instant, label, int(end_verified is True)))
                if end_verified is not True:
                    _fault(root, key, 'MODEL_TERMINAL_WITHOUT_VERIFIED_END', instant)
                    return {'state': 'INCOMPLETE', 'collector_epoch': coverage[1],
                            'reason_codes': ['MODEL_TERMINAL_WITHOUT_VERIFIED_END']}
                if old[4] is not None:
                    return {'state': 'DUPLICATE', 'collector_epoch': coverage[1]}
                db.execute('UPDATE attempts SET ended_at=?,outcome=? WHERE identity_key=? AND physical_id=?',
                    (instant, label, key, physical_attempt_id))
        return {'state': 'RECORDED', 'collector_epoch': coverage[1]}
    except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
        reason = str(exc) if isinstance(exc, ValueError) else 'MODEL_ACTIVITY_RECEIPT_UNAVAILABLE'
        _fault(root, key, reason, instant, error=exc)
        return {'state': 'INCOMPLETE', 'reason_codes': [reason], 'error_type': type(exc).__name__}


def start_model_coverage(home, scope, at=None):
    """Separate main/aux hook coverage; tool coverage is never substituted."""
    return _record_model(home, scope, 'coverage', at=at)


def record_model_start(home, scope, physical_attempt_id, api_request_id, at=None, kind='main_request'):
    return _record_model(home, scope, 'start', at=at, physical_attempt_id=physical_attempt_id,
                         api_request_id=api_request_id, kind=kind)


def record_model_end(home, scope, physical_attempt_id, api_request_id, at=None,
                     outcome='SUCCESS', *, end_verified=True):
    return _record_model(home, scope, 'end', at=at, physical_attempt_id=physical_attempt_id,
                         api_request_id=api_request_id, outcome=outcome, end_verified=end_verified)


def read_model_activity(home, scope, now=None):
    """Read physical requests; observation grace is never a timeout contract."""
    instant = _time(now)
    source = {'source_id': 'model_requests', 'source_version': MODEL_VERSION,
        'check_status': 'UNVERIFIED', 'complete': False, 'last_verified_at': None,
        'max_gap_seconds': 120, 'coverage_started_at': None, 'reason_codes': [], 'evidence_ref': None}
    result = {'sources': [source], 'lifecycles': [], 'terminal_reports': []}
    try:
        identity = _identity(scope)
        root, path, key = _model_location(home, identity)
        source['evidence_ref'] = str(path)+'#'+key
        with sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=.05) as db:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            coverage = db.execute('SELECT identity,epoch,started_at FROM coverage WHERE identity_key=?', (key,)).fetchone()
            if not coverage:
                raise ValueError('MODEL_COLLECTOR_COVERAGE_NOT_STARTED')
            if json.loads(coverage[0]) != identity or coverage[2] > instant:
                raise ValueError('MODEL_COLLECTOR_IDENTITY_OR_TIME_MISMATCH')
            source.update(collector_epoch=coverage[1], coverage_started_at=coverage[2])
            operations = db.execute('SELECT physical_id,request_id,epoch,kind,started_at,ended_at,outcome '
                'FROM attempts WHERE identity_key=? AND ended_at IS NULL ORDER BY started_at LIMIT 513', (key,)).fetchall()
            if len(operations) > 512:
                raise ValueError('OPEN_MODEL_OBSERVATION_LIMIT_REACHED')
            operations += db.execute('SELECT physical_id,request_id,epoch,kind,started_at,ended_at,outcome '
                'FROM attempts WHERE identity_key=? AND ended_at IS NOT NULL ORDER BY ended_at DESC LIMIT 512', (key,)).fetchall()
            unverified_terminal = bool(db.execute('SELECT 1 FROM terminal_reports '
                'WHERE identity_key=? AND end_verified=0 LIMIT 1', (key,)).fetchone())
            result['terminal_reports'] = [{'operation_id': physical, 'reported_at': reported,
                'outcome': outcome, 'end_verified': bool(verified)}
                for physical, reported, outcome, verified in db.execute(
                    'SELECT physical_id,reported_at,outcome,end_verified FROM terminal_reports '
                    'WHERE identity_key=? ORDER BY rowid DESC LIMIT 512', (key,))]
        reasons = ['MODEL_TERMINAL_WITHOUT_VERIFIED_END'] if unverified_terminal else []
        gap = root/('gap-'+key+'.json')
        if gap.exists():
            reasons.append(json.loads(gap.read_text())['reason_code'])
        for physical, logical, epoch, kind, started, ended, outcome in operations:
            if (epoch != coverage[1] or not math.isfinite(started) or not coverage[2] <= started <= instant
                    or ended is not None and (not math.isfinite(ended) or not started <= ended <= instant)):
                raise ValueError('MODEL_BOUNDARY_IDENTITY_OR_TIME_INVALID')
            result['lifecycles'].append({'operation_id': physical, 'logical_request_id': logical,
                'kind': kind, 'source_id': 'model_requests', 'verified': True,
                'started_at': started, 'ended_at': ended, 'wait_until': None, 'outcome': outcome,
                # Long model/compaction calls can legitimately take minutes.
                # This is a fixed alert threshold, not evidence of an enforced
                # timeout, continued progress, completion or permission to retry.
                'observation_grace_seconds': 900, 'grace_policy': 'open-observation/1',
                'evidence_ref': str(path)+'#'+key+':'+hashlib.sha256(physical.encode()).hexdigest()})
        source.update(check_status='VERIFIED', complete=not reasons,
                      last_verified_at=instant, reason_codes=sorted(set(reasons)))
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        source['reason_codes'] = [str(exc) if isinstance(exc, ValueError) else 'MODEL_ACTIVITY_SOURCE_UNAVAILABLE']
        source['error_type'] = type(exc).__name__
    return result
