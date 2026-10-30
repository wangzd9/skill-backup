"""Bounded wakeup intents around the original synchronous Hermes event entry.

The cache and receipts grant no business authority. Durable events, native claims
and writer fences remain authoritative; the existing tick recovers lost intents.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from contextlib import closing, suppress
import hashlib
import importlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from agent.memory_provider import spawn_context_thread

logger = logging.getLogger(__name__)
REQUIRED_STAGES = frozenset({'request_entered', 'claim_decided', 'run_registration', 'native_return'})
MAX_CALLS = 5
OBSERVATION_WRITE_TIMEOUT = .25
# Preserve active reservations if this module is explicitly reloaded.
if '_STATE' not in globals():
    _STATE = {'lock': threading.Lock(), 'slots': threading.BoundedSemaphore(MAX_CALLS),
              'inflight': {}, 'closed': False, 'gaps': deque(maxlen=64), 'rotation': 0}
_STATE.setdefault('receipt_lock', threading.Lock())


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def _source_version(path):
    """Read local provenance without a subprocess, network or executing a job."""
    if not path:
        return {'path': None, 'commit': 'UNKNOWN', 'sha256': None}
    path = Path(path).resolve()
    result = {'path': str(path), 'commit': 'UNKNOWN', 'sha256': None}
    try:
        result['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        for parent in path.parents:
            git = parent/'.git'
            if git.is_file():
                git = (parent/git.read_text().strip().removeprefix('gitdir: ')).resolve()
            if not git.is_dir():
                continue
            head = (git/'HEAD').read_text().strip()
            if head.startswith('ref: '):
                ref = head[5:]
                if (git/ref).is_file():
                    head = (git/ref).read_text().strip()
                else:
                    head = next((line.split()[0] for line in (git/'packed-refs').read_text().splitlines()
                                 if line.endswith(' '+ref)), 'UNKNOWN')
            result['commit'] = head
            break
    except (OSError, ValueError):
        pass
    return result


def observer_binding(dispatcher=None):
    """Negotiate declared protocol, never infer it from a signature or retry."""
    if dispatcher is None:
        module = importlib.import_module('tools.cronjob_tools')
        dispatcher = module.execute_job_for_event
        declaration = getattr(module, 'EVENT_OBSERVER_CAPABILITIES', None)
    else:
        module = importlib.import_module(dispatcher.__module__) if getattr(dispatcher, '__module__', None) else None
        # Injected adapters must opt in on their callable, not borrow module support.
        declaration = getattr(dispatcher, 'EVENT_OBSERVER_CAPABILITIES', None)
    capability = {}
    supported = False
    if isinstance(declaration, Mapping):
        try:
            capability = {key: declaration.get(key) for key in
                          ('api_major', 'api_minor', 'keyword', 'stage_schema_major')}
            capability['stages'] = sorted(declaration.get('stages') or ())
            supported = (type(capability['api_major']) is int and capability['api_major'] == 1
                         and type(capability['stage_schema_major']) is int and capability['stage_schema_major'] == 1
                         and type(capability['api_minor']) is int and capability['api_minor'] >= 0
                         and capability['keyword'] == 'observer'
                         and REQUIRED_STAGES.issubset(capability['stages']))
        except (TypeError, ValueError):
            capability = {'invalid_declaration': True}
    manifest = {'negotiation': 'SUPPORTED' if supported else 'UNSUPPORTED',
                'measurement': 'READY' if supported else 'UNKNOWN',
                'capabilities': capability,
                'capability_sha256': hashlib.sha256(_json(capability).encode()).hexdigest(),
                'native': _source_version(getattr(module, '__file__', None)),
                'runtime': _source_version(__file__),
                'capacity_domain': 'this_process_role_adapter_calls_only',
                'max_calls': MAX_CALLS, 'per_stable_role': 1, 'queue_capacity': 0}
    return dispatcher, manifest


def receipt_path(home, pool_path):
    # Same namespace as existing role-lease-monitor observation/delivery receipts.
    digest = hashlib.sha256(json.dumps(str(Path(pool_path).resolve()),
                                      sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return Path(home).resolve()/'cache/role-lease-monitor'/digest/'dispatch-observations.sqlite3'


def _persist(request, stage, at, outcome, payload):
    path = receipt_path(request['home'], request['pool_path'])
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Set identity-receipt privacy before SQLite opens a newly created file.
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    row = {key: request.get(key) for key in ('request_id', 'dispatch_attempt_id', 'pool_path',
           'generation', 'run_id', 'role', 'work_id', 'job_id', 'source_event_high_water', 'source')}
    from runtime_common import process_identity
    process = process_identity(os.getpid()) or {'pid':os.getpid(),'process_start':None,'boot_id':None}
    origin=payload.pop('origin_process',None)
    if stage=='commit' and isinstance(origin,dict):
        process={key:origin.get(key) for key in ('pid','process_start','boot_id')}
    row.update(schema_version=1, stage=stage, at=at, recorded_at=time.time(), outcome=outcome,
               evidence_ref=str(request['ledger_path']), clock='unix_wall_and_process_monotonic',
               **process, **payload)
    # A separate short, bounded observation lock prevents this process's rapid
    # commit/join writer from repeatedly overtaking its native phase writer in
    # SQLite's busy retry loop. It never guards claims, capacity or a native call.
    deadline=time.monotonic()+OBSERVATION_WRITE_TIMEOUT
    if not _STATE['receipt_lock'].acquire(timeout=OBSERVATION_WRITE_TIMEOUT):
        raise TimeoutError('observation writer busy')
    try:
        with closing(sqlite3.connect(path, timeout=max(0,deadline-time.monotonic()))) as db:
            if path.stat().st_size==0:
                db.execute('PRAGMA journal_mode=WAL')
            with db:
                db.execute('CREATE TABLE IF NOT EXISTS observations '
                           '(receipt_id TEXT PRIMARY KEY,request_id TEXT NOT NULL,stage TEXT NOT NULL,payload TEXT NOT NULL)')
                row['recorded_at']=time.time()
                db.execute('INSERT INTO observations VALUES(?,?,?,?)',
                           (uuid.uuid4().hex, request['request_id'], stage, _json(row)))
    finally:
        _STATE['receipt_lock'].release()


def _record(request, stage, *, at=None, outcome='OBSERVED', **payload):
    at = time.time() if at is None else at
    payload.setdefault('monotonic_ns', time.monotonic_ns())
    try:
        _persist(request, stage, at, outcome, payload)
        return True
    except Exception as exc:
        gap = {'request_id': request['request_id'], 'stage': stage,
               'at': at, 'error': type(exc).__name__, 'measurement': 'UNKNOWN'}
        with _STATE['lock']:
            _STATE['gaps'].append(gap)
        with suppress(Exception):
            logger.warning('Role dispatch observation missing: %s', gap)
        return False


def receipts(home, pool_path, request_id=None):
    """Read-only diagnostics; receipt presence is never an admission condition."""
    path = receipt_path(home, pool_path)
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=.05)) as db:
        sql = 'SELECT payload FROM observations'
        args = ()
        if request_id:
            sql += ' WHERE request_id=?'
            args = (request_id,)
        return [json.loads(row[0]) for row in db.execute(sql+' ORDER BY rowid', args)]


def state():
    with _STATE['lock']:
        return {'active': len(_STATE['inflight']), 'capacity': MAX_CALLS,
                'queue_capacity': 0, 'closed': _STATE['closed'],
                'measurement_gaps': list(_STATE['gaps'])}


def close():
    """Stop new submissions without terminating jobs or clearing persistent input."""
    with _STATE['lock']:
        _STATE['closed'] = True


def rotate_roles(roles):
    roles = list(roles)
    if not roles:
        return roles
    with _STATE['lock']:
        offset = _STATE['rotation'] % len(roles)
        _STATE['rotation'] += 1
    return roles[offset:]+roles[:offset]


def _job(pool_path, role, home):
    pool = json.loads(Path(pool_path).read_text())
    job_id = pool['roles'][role]['poll_job_id']
    jobs = json.loads((Path(home)/'cron/jobs.json').read_text()).get('jobs', [])
    job = next((row for row in jobs if row['id'] == job_id), None)
    runnable = bool(job and not (job.get('paused') or job.get('enabled') is False
                                or job.get('state') == 'paused'))
    return job_id, runnable


def _intent(pool_path, role, home):
    import workflow_runtime as workflow
    from hermes_constants import hermes_home_key
    if role not in workflow.ROLES:
        raise ValueError('ROLE_NOT_IN_RUNTIME')
    pool_path, home = Path(pool_path).resolve(), Path(home).resolve()
    job_id, runnable = _job(pool_path, role, home)
    ledger = workflow.open_ledger(pool_path)
    snapshot = ledger.snapshot()
    work = next((item for item in snapshot['works']
                 if item['work_id'] == next((r['current_work_id'] for r in snapshot['roles']
                                            if r['role'] == role), None)), None)
    run_id = (work or {}).get('run_id') or snapshot['current_run_id']
    previous = [v for v in snapshot['works'] if v['role'] == role and v['run_id'] == run_id]
    processed = (work or (previous[-1] if previous else {})).get('processed_through', 0)
    mechanical = {'EXECUTION_BOUND', 'EXECUTION_ENDED', 'EXECUTION_OUTCOME_OBSERVED', 'TOOL_EXECUTION_REGISTERED',
                  'CHECKPOINT_SAVED', 'WORK_CLAIMED'}
    high = max((event['event_id'] for event in snapshot['events']
                if event.get('run_id') in (None, run_id) and event['kind'] not in mechanical
                and not (work and event.get('work_id') == work['work_id']
                         and event['kind'] in {'PLAN_PUBLISHED', 'CANDIDATE_DECIDED'})), default=0)
    hint=ledger.admission_hint(role)
    # Admission may return every pending event ID. Retain only the bounded hint;
    # durable event membership is represented here by its original high water.
    hint={key:hint[key] for key in ('can_start_model','reason','work_id','drain_only') if key in hint}
    return {'home': str(home), 'pool_path': str(pool_path), 'role': role, 'job_id': job_id,
            'generation': snapshot['generation'], 'run_id': run_id,
            'work_id': (work or {}).get('work_id'), 'processed_through': processed,
            'source_event_high_water': high, 'ledger_path': str(workflow.ledger_path(pool_path)),
            'hint': hint, 'runnable': runnable,
            'mode': snapshot['mode'], 'key': (hermes_home_key(home), str(pool_path), role)}


def _compatible(before, after):
    return (all(before[key] == after[key] for key in ('generation', 'run_id', 'job_id'))
            and (before.get('work_id') is None or before['work_id'] == after.get('work_id')))


def dispatch_event(pool_path, role, *, home, dispatcher=None, observer=None, expected_scope=None):
    """Synchronous primitive, always called outside adapter/observation locks."""
    job_id, runnable = _job(pool_path, role, home)
    if not runnable:
        return {'state': 'DEFERRED', 'reason': 'JOB_PAUSED_OR_UNAVAILABLE', 'event_preserved': True}
    if expected_scope is not None:
        current = _intent(pool_path, role, home)
        if not _compatible(expected_scope, current):
            return {'state': 'DEFERRED_SCOPE_CHANGE', 'event_preserved': True}
        if not current['hint']['can_start_model']:
            return {'state': 'DEFERRED', 'reason': current['hint']['reason'], 'event_preserved': True}
    try:
        call, manifest = observer_binding(dispatcher)
        kwargs = {'observer': observer} if observer is not None and manifest['negotiation'] == 'SUPPORTED' else {}
        from cron.scheduler_provider import _profile_cron_scope
        with _profile_cron_scope(home):
            # Exactly one call even if a declared interface raises TypeError after claim.
            result = call(job_id, **kwargs)
        return {'state': 'DISPATCH_REQUESTED', 'result': result, 'observer_manifest': manifest}
    except Exception as exc:
        return {'state': 'DEFERRED', 'reason': type(exc).__name__, 'event_preserved': True,
                'interface_defect': isinstance(exc, TypeError)}


def _release(key, token):
    with _STATE['lock']:
        current = _STATE['inflight'].get(key)
        if current is None or current['token'] != token:
            return None
        del _STATE['inflight'][key]
        _STATE['slots'].release()
        return current


def _run(request, dispatcher):
    import workflow_runtime as workflow
    result = None
    try:
        _, manifest = observer_binding(dispatcher)
        _record(request, 'observer_negotiation', outcome=manifest['negotiation'], manifest=manifest)
        def observe(*, stage, at, snapshot):
            _record(request, stage, at=at, **snapshot)
        _record(request, 'call', outcome='CALLING')
        result = workflow.dispatch_event(request['pool_path'], request['role'], home=request['home'],
                                         dispatcher=dispatcher, observer=observe, expected_scope=request)
    except Exception as exc:
        result = {'state': 'DEFERRED', 'reason': type(exc).__name__, 'event_preserved': True}
    finally:
        try:
            native = (result or {}).get('result')
            _record(request, 'finish', outcome=(result or {}).get('state', 'ERROR_OR_UNKNOWN'),
                    result={key: native.get(key) for key in ('claimed', 'success')} if isinstance(native, dict) else None,
                    reason=(result or {}).get('reason'), interface_defect=(result or {}).get('interface_defect', False))
        finally:
            finished = _release(request['key'], request['token'])
        # No spin after busy/error/no-progress. One continuation only; later work is
        # rediscovered by the original tick. Observation failure cannot leak a slot.
        try:
            if finished and not request.get('continuation') and isinstance(native, dict) and native.get('success') is True:
                current = _intent(request['pool_path'], request['role'], request['home'])
                advanced = current['processed_through'] > request['processed_through']
                new_input = current['source_event_high_water'] > request['source_event_high_water']
                if (_compatible(request, current) and current['hint']['can_start_model']
                        and (advanced or new_input) and current['source_event_high_water'] > current['processed_through']):
                    request_role_wakeup(request['pool_path'], request['role'], home=request['home'],
                                        dispatcher=dispatcher, source='completion_recheck', _continuation=True)
        except Exception:
            logger.warning('Role dispatch completion recheck deferred to original tick', exc_info=True)


def request_role_wakeup(pool_path, role, *, home, dispatcher=None, source='event_tick',
                        committed_at=None, _continuation=False):
    """Submit or coalesce an intent; never wait for claim or the synchronous job."""
    from workflow_gateway_wakeup import submit, pending, acknowledge
    # A worker must not claim another role then try to run its model in-process.
    # The gateway's existing event tick rechecks fresh admission and owns launch.
    if dispatcher is None and os.environ.get('_HERMES_CRON_EXTERNAL_WORKER'):
        return submit(home,pool_path,role,committed_at)
    forwarded=pending(home,pool_path,role) if dispatcher is None else None
    if forwarded:
        source='business_commit_forwarded'
        committed_at=[value for value in (forwarded.get('first_commit'),forwarded.get('latest_commit')) if value]
    request = {'request_id': uuid.uuid4().hex, 'dispatch_attempt_id': uuid.uuid4().hex,
               'token': uuid.uuid4().hex, 'source': source, 'dirty': False,
               'continuation': _continuation, 'home': str(Path(home).resolve()),
               'pool_path': str(Path(pool_path).resolve()), 'role': role,
               'ledger_path': str(Path(pool_path).resolve().with_suffix('.runtime')/'business.sqlite3')}
    deferred = None
    try:
        intent = _intent(pool_path, role, home)
    except Exception as exc:
        intent = None
        deferred = {'state': 'DEFERRED', 'reason': type(exc).__name__}
    joined = False
    joined_at = None
    if intent:
        request.update(intent, joined_high_water=intent['source_event_high_water'])
        if not intent['runnable']:
            deferred = {'state': 'DEFERRED', 'reason': 'JOB_PAUSED_OR_UNAVAILABLE'}
        elif intent['mode'] != 'ACTIVE':
            deferred = {'state': 'DEFERRED', 'reason': 'WORKFLOW_NOT_ACTIVE'}
        else:
            with _STATE['lock']:
                current = _STATE['inflight'].get(intent['key'])
                if _STATE['closed']:
                    deferred = {'state': 'DEFERRED', 'reason': 'ADAPTER_CLOSED'}
                elif current:
                    if not _compatible(current, intent):
                        deferred = {'state': 'DEFERRED_SCOPE_CHANGE', 'existing_request_id': current['request_id']}
                    elif intent['hint']['reason'] in {'PAUSED_OR_CANCELLED', 'MODE_OR_ADMISSION_BLOCKED'}:
                        deferred = {'state': 'DEFERRED', 'reason': intent['hint']['reason']}
                    else:
                        current['dirty'] = current['dirty'] or intent['source_event_high_water'] > current['source_event_high_water']
                        current['joined_high_water'] = max(current['joined_high_water'], intent['source_event_high_water'])
                        request = {**current, 'source_event_high_water': intent['source_event_high_water'], 'source': source}
                        joined = True
                        joined_at = {'at':time.time(),'monotonic_ns':time.monotonic_ns()}
                elif not intent['hint']['can_start_model']:
                    deferred = {'state': 'DEFERRED', 'reason': intent['hint']['reason']}
                elif not _STATE['slots'].acquire(blocking=False):
                    deferred = {'state': 'DEFERRED', 'reason': 'CAPACITY_FULL'}
                else:
                    _STATE['inflight'][intent['key']] = request
    markers = ([committed_at] if isinstance(committed_at, dict) else committed_at) or ()
    for marker in markers:
        _record(request, 'commit', at=marker['at'], outcome='COMMITTED',
                monotonic_ns=marker['monotonic_ns'],
                origin_process=marker.get('origin_process'),
                source_event_ids=[marker['event_id']] if marker.get('event_id') else None)
    if deferred:
        _record(request, 'deferred', outcome=deferred['state'], reason=deferred.get('reason'))
        return {**deferred, 'request_id': request['request_id'], 'event_preserved': True}
    if joined:
        _record(request, 'join', outcome='COALESCED', **joined_at)
        if forwarded:
            acknowledge(home,pool_path,role,forwarded['token'])
        return {'state': 'COALESCED', 'request_id': request['request_id'],
                'source_event_high_water': intent['source_event_high_water'], 'event_preserved': True}
    _record(request, 'prepared', outcome='PREPARED')
    entered=threading.Event()
    thread=None
    def run():
        entered.set()
        _run(request,dispatcher)
    try:
        thread = spawn_context_thread(run, name='role-event-'+role)
        thread.start()
    except Exception as exc:
        # A custom start boundary may raise after it actually launched. A live
        # worker alone owns its token until its original synchronous call exits.
        if entered.is_set() or type(getattr(thread,'ident',None)) is int:
            _record(request,'start_boundary_error',outcome='SUBMITTED',exception_type=type(exc).__name__)
            return {'state':'SUBMITTED','request_id':request['request_id'],
                    'dispatch_attempt_id':request['dispatch_attempt_id'],'event_preserved':True,
                    'measurement':'START_RETURN_UNKNOWN'}
        _release(request['key'], request['token'])
        _record(request, 'start_failed', outcome='DEFERRED', exception_type=type(exc).__name__)
        return {'state': 'DEFERRED', 'reason': 'THREAD_START_FAILED',
                'request_id': request['request_id'], 'event_preserved': True}
    # A worker may already have finished. Receipt order never defines stage order.
    _record(request, 'enqueued', outcome='SUBMITTED')
    if forwarded:
        acknowledge(home,pool_path,role,forwarded['token'])
    return {'state': 'SUBMITTED', 'request_id': request['request_id'],
            'dispatch_attempt_id': request['dispatch_attempt_id'], 'event_preserved': True}
