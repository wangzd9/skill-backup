"""Rebuildable write-path observations. Nothing here grants business authority.

Receipts contain allowlisted facts from the original gate, not prompts, shell
arguments or a fresh full-pool snapshot. The commands table retains transaction
receipts; this cache also covers preflight, conflicts and unavailable sources.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import time
import uuid

VERSION = 'write-diagnostics-v1'
IDENTITY_FIELDS = ('generation', 'run_id', 'role', 'work_id', 'task_id', 'attempt',
                   'execution_id', 'execution_generation')
FACT_FIELDS = {
    'authority': ('mode', 'generation', 'current_run_id', 'admission_open', 'freeze_reason'),
    'execution': ('execution_id', 'work_id', 'role', 'generation', 'execution_generation',
                  'pid', 'process_start', 'state'),
    'work': ('work_id', 'run_id', 'task_id', 'attempt', 'role', 'state',
             'active_execution_id', 'execution_generation'),
    'role_current': ('role', 'current_work_id', 'writer_execution_id', 'control'),
    'run': ('run_id', 'control', 'plan_version'),
    'task': ('run_id', 'task_id', 'role', 'phase', 'attempt', 'plan_version', 'unresolved_reason'),
}
TABLE_FACTS = {'executions': 'execution', 'works': 'work', 'runs': 'run',
               'tasks': 'task', 'role_current': 'role_current'}
EXPECTED = frozenset({'BUSINESS_MODE_INACTIVE', 'WORKFLOW_NOT_ACTIVE', 'STALE_MODE_GENERATION',
    'STALE_EXECUTION_IDENTITY', 'WORK_SCOPE_MISMATCH', 'EXECUTION_NO_LONGER_WRITABLE',
    'WORK_NOT_RUNNING', 'MIGRATION_ADMISSION_FROZEN', 'CONTROL_BLOCKS_NEW_ACTION',
    'RUN_NOT_CURRENT', 'TASK_CONTROL_OR_RECONCILIATION_GATE', 'ROLE_PAUSED', 'RUN_NOT_ACTIVE',
    'COMMAND_NOT_ALLOWED_FOR_ROLE', 'ROLE_CAPABILITY_NOT_ALLOWED', 'REPORT_READ_ONLY',
    'DRAIN_CAPABILITY_NOT_ALLOWED', 'COORDINATOR_REQUIRED', 'DEVELOPMENT_WORK_REQUIRED',
    'COORDINATOR_SERVICE_ACTION_REQUIRED', 'INDEPENDENT_TESTER_WORK_REQUIRED',
    'PREVIOUS_WORK_REQUIRES_SAFE_RELEASE', 'EXECUTION_ALREADY_BOUND_USE_CURRENT_WORK'})
AUTHORITY = frozenset({'ROLE_OWNER_MISMATCH', 'EXECUTION_IDENTITY_REQUIRED',
    'WORK_IDENTITY_UNVERIFIED', 'BUSINESS_WORK_REQUIRED', 'EXECUTION_SCOPE_CHANGED',
    'NATIVE_EXECUTION_NOT_LIVE', 'NATIVE_EXECUTION_UNVERIFIED', 'NATIVE_SCOPE_UNVERIFIED',
    'OLD_WRITER_NOT_CLEARED', 'OLD_RUN_CONTROL_UNVERIFIED', 'PID_START_MISMATCH',
    'ROLE_JOB_ID_MISMATCH', 'ROLE_BINDING_UNVERIFIED', 'PRIVATE_WORKSPACE_REQUIRED',
    'WORKSPACE_SANDBOX_UNAVAILABLE', 'TOOL_SCOPE_REGISTRATION_FAILED',
    'EXACT_TOOL_PROCESS_IDENTITY_REQUIRED', 'TOOL_PROCESS_UNVERIFIED',
    'TOOL_PROCESS_IDENTITY_MISMATCH', 'BUSINESS_SOURCE_UNAVAILABLE', 'SANDBOX_SOURCE_UNAVAILABLE',
    'UNSUPPORTED_BUSINESS_SCHEMA', 'UNKNOWN_EXECUTIONS', 'UNKNOWN_WORKS',
    'UNKNOWN_ROLE_CURRENT', 'UNKNOWN_RUNS', 'UNKNOWN_TASKS',
    'WRITER_OBSERVATION_UNAVAILABLE', 'CHILD_SCOPE_UNVERIFIED',
    'WRITER_SCOPE_NOT_ISOLATED'})
UNCERTAIN_AUTHORITY = frozenset(code for code in AUTHORITY if
    code.endswith('_UNVERIFIED') or code.startswith('UNKNOWN_')) | frozenset({
    'BUSINESS_SOURCE_UNAVAILABLE', 'SANDBOX_SOURCE_UNAVAILABLE', 'UNSUPPORTED_BUSINESS_SCHEMA',
    'WRITER_OBSERVATION_UNAVAILABLE', 'WORKSPACE_SANDBOX_UNAVAILABLE',
    'TOOL_SCOPE_REGISTRATION_FAILED', 'WRITER_SCOPE_NOT_ISOLATED'})
CAPABILITIES = {'checkpoint': 'checkpoint', 'safe_release': 'safe_release',
    'release_work': 'safe_release', 'publish_plan': 'publish_plan', 'deliver': 'deliver',
    'decide': 'decide', 'verify': 'verify', 'publish_verification': 'verify',
    'register_tool_execution': 'workspace_write', 'terminal': 'workspace_write',
    'bind_execution': 'execution_admission'}
_operation = ContextVar('workflow_diagnostic_operation', default={})
_capture = ContextVar('workflow_diagnostic_capture', default=None)
logger = logging.getLogger(__name__)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def ledger_file(pool_path):
    path = Path(pool_path).resolve()
    return path if path.name == 'business.sqlite3' else path.with_suffix('.runtime')/'business.sqlite3'


def store_path(pool_path):
    return ledger_file(pool_path).parent/'observations'/'write-diagnostics.sqlite3'


@contextmanager
def operation_context(scope=None, *, operation=None, capability=None, operation_id=None,
                      command_id=None, tool_call_id=None, phase=None, side_effect_state=None,
                      input_validated=None, capability_allowed=None):
    old = _operation.get()
    value = dict(old)
    if isinstance(scope, dict):
        value.update(identity={key: scope[key] for key in IDENTITY_FIELDS if key in scope},
                     pool=str(Path(scope['pool_path']).resolve()) if scope.get('pool_path') else None)
    for key, item in dict(operation=operation, capability=capability, operation_id=operation_id,
                          command_id=command_id, tool_call_id=tool_call_id, phase=phase,
                          side_effect_state=side_effect_state, input_validated=input_validated,
                          capability_allowed=capability_allowed).items():
        if item is not None:
            value[key] = item
    token = _operation.set(value)
    try:
        yield value
    finally:
        _operation.reset(token)


def new_capture(identity=None, *, at=None, allow_drain=False):
    return {'capture_id': uuid.uuid4().hex, 'rule_version': VERSION,
            'db_checked_at': time.time() if at is None else at,
            'allow_drain': allow_drain, 'identity_verdict': 'NOT_CHECKED',
            'requested_identity': {key: identity[key] for key in IDENTITY_FIELDS
                                   if isinstance(identity, dict) and key in identity},
            'facts': {key: {'check_status': 'NOT_CHECKED'} for key in FACT_FIELDS},
            'process': {'check_status': 'NOT_CHECKED'}, 'branches': []}


@contextmanager
def capturing(capture):
    token = _capture.set(capture)
    try:
        yield capture
    finally:
        _capture.reset(token)


def capture_fact(name, value):
    capture = _capture.get()
    if capture is not None and name in FACT_FIELDS:
        selected = {key: value[key] for key in FACT_FIELDS[name] if value is not None and key in value}
        for key in ('freeze_reason', 'unresolved_reason'):
            if selected.get(key) is not None:
                selected[key+'_sha256'] = _hash(selected.pop(key))
        fact = {'check_status': 'OBSERVED' if value is not None else 'MISSING', 'value': selected}
        # A command may reread a row after changing it. Preserve that ordering;
        # the first gate evidence must not silently become a post-write row.
        if capture['facts'][name]['check_status'] == 'NOT_CHECKED':
            capture['facts'][name] = fact
        elif capture['facts'][name] != fact:
            capture.setdefault('subsequent_reads', []).append({'fact':name, **fact})


def capture_process(value, *, at=None):
    capture = _capture.get()
    if capture is not None:
        capture['process'] = {'checked_at': time.time() if at is None else at,
            'check_status': 'MISSING' if value is None else 'UNVERIFIED' if value.get('state') == 'unknown' else 'OBSERVED',
            'value': {key: value[key] for key in ('state', 'process_start', 'pgrp')
                      if value is not None and key in value}}


def identity_allowed():
    capture = _capture.get()
    if capture is not None:
        capture['identity_verdict'] = 'ALLOWED'


def identity_check(*, allow_drain):
    capture = _capture.get()
    if capture is not None:
        capture['allow_drain'] = allow_drain
        capture['branches'].append('identity_allow_drain' if allow_drain else 'identity_normal_action')


def denied_branch(code):
    capture = _capture.get()
    if capture is not None:
        capture['branches'].append(code)


def current_operation():
    """Only correlation metadata may cross the sandbox boundary."""
    return {key: value for key, value in _operation.get().items()
            if key in ('operation_id', 'tool_call_id', 'command_id', 'operation', 'capability')}


def classify(result, capture, context=None):
    """Classify original evidence; later healthy state cannot rewrite this result."""
    context = context or {}
    status = result.get('original_status', result.get('status'))
    code = result.get('code', '')
    if status == 'COMMITTED':
        return 'WRITE_SUCCEEDED', 'success', []
    proven_allowed = (capture.get('identity_verdict') == 'ALLOWED'
                      and context.get('input_validated') is True
                      and context.get('capability_allowed') is True)
    if proven_allowed and code in (EXPECTED | AUTHORITY)-UNCERTAIN_AUTHORITY:
        return 'WRITE_AUTHORITY_ANOMALY', 'write_authority', []
    if code in EXPECTED:
        return 'EXPECTED_GATE_DENIAL', 'expected_gate', []
    if code.startswith('UNKNOWN_') and not capture.get('requested_identity') and not any(
            branch.startswith('identity_') for branch in capture.get('branches', [])):
        return 'BUSINESS_CONTRACT_REJECTION', 'business_contract', []
    # UNVERIFIED also describes unreadable delivery evidence and Git candidates.
    # Their uncertainty does not establish a missing permission basis.
    if code in AUTHORITY:
        missing = [key for key, fact in capture.get('facts', {}).items()
                   if fact.get('check_status') != 'OBSERVED']
        return 'WRITE_AUTHORITY_UNVERIFIED', 'write_authority', missing or ['permission_basis']
    return 'BUSINESS_CONTRACT_REJECTION', 'business_contract', []


def build_receipt(database, result, capture=None, *, operation=None, command_id=None,
                  phase=None, side_effect_state=None, at=None):
    context = _operation.get()
    capture = capture or new_capture(context.get('identity'), at=at)
    now = time.time() if at is None else at
    operation = operation or context.get('operation') or 'UNKNOWN'
    command_id = command_id or context.get('command_id')
    identity = dict(capture.get('requested_identity') or context.get('identity') or {})
    if result.get('status') == 'COMMITTED' and isinstance(result.get('identity'),dict):
        identity.update({key:result['identity'][key] for key in IDENTITY_FIELDS if key in result['identity']})
    classification, family, missing = classify(result, capture, context)
    capability = context.get('capability') or CAPABILITIES.get(operation, operation)
    operation_id = context.get('operation_id') or ('command:'+command_id if command_id else 'operation:'+uuid.uuid4().hex)
    phase = phase or context.get('phase') or 'TRANSACTION_GATE'
    authority = capture.get('facts', {}).get('authority', {}).get('value', {})
    for key in IDENTITY_FIELDS:
        identity.setdefault(key, None)
    evidence_digest = _hash(capture)
    return {'schema_version': 1, 'rule_version': VERSION,
        'decision_id': _hash([operation_id, phase, capture['capture_id']]),
        'operation_id': operation_id, 'command_id': command_id,
        'tool_call_id': context.get('tool_call_id'), 'decision_phase': phase,
        'at': now, 'decision_at': now, 'recorded_at': None, 'receipt_recorded_at': None,
        'classified_at': now,
        'pool': context.get('pool') or str(ledger_file(database)), **identity,
        'identity_source': 'runtime_scope' if context.get('identity') else 'ledger_input_unverified',
        'unknown_identity_fields': [key for key, value in identity.items() if value is None],
        'operation': operation, 'capability': capability,
        'status': result.get('status', 'UNVERIFIED'), 'original_status': result.get('original_status'),
        'code': result.get('code'), 'reason_codes': list(dict.fromkeys(list(result.get('reason_codes') or []) +
            ([result['code']] if result.get('code') else []))),
        'classification': classification, 'reason_family': family,
        'authority_snapshot_ref': capture['capture_id'],
        'authority_version': _hash(authority) if authority else None,
        'side_effect_state': side_effect_state or context.get('side_effect_state') or 'UNKNOWN',
        'evidence_ref': {'capture_id': capture['capture_id'], 'sha256': evidence_digest},
        'capture': capture, 'verification_pending': classification == 'WRITE_AUTHORITY_UNVERIFIED',
        'missing_evidence': missing, 'projection_state': 'NOT_RECORDED'}


_SCHEMA = '''
CREATE TABLE IF NOT EXISTS receipts(decision_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL,
 at REAL NOT NULL, receipt TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS receipts_operations ON receipts(operation_id);
CREATE INDEX IF NOT EXISTS receipts_roles ON receipts(json_extract(receipt,'$.role'));
CREATE TABLE IF NOT EXISTS write_paths(identity TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS verifications(capture_key TEXT PRIMARY KEY, checked_at REAL NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pending(identity TEXT PRIMARY KEY, owner_key TEXT NOT NULL,
 capability TEXT NOT NULL, authority_version TEXT, verification_key TEXT NOT NULL, first_seen REAL NOT NULL,
 last_seen REAL NOT NULL, receipt TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS pending_owners ON pending(owner_key);
CREATE INDEX IF NOT EXISTS pending_verifications ON pending(verification_key);
CREATE INDEX IF NOT EXISTS path_states ON write_paths(json_extract(payload,'$.write_path_state'));
CREATE INDEX IF NOT EXISTS path_roles ON write_paths(json_extract(payload,'$.role'));
CREATE INDEX IF NOT EXISTS path_works ON write_paths(json_extract(payload,'$.work_id'));
'''


def _key(receipt):
    return _hash([receipt.get('pool'), *[receipt.get(key) for key in IDENTITY_FIELDS
                                      if key not in ('task_id', 'attempt')], receipt['capability']])


def write_path_step(prior, receipt):
    """The production and replay transition; activity is deliberately no input."""
    old = json.loads(_json(prior)) if prior else None
    kind = receipt['classification']
    if kind not in ('WRITE_AUTHORITY_ANOMALY', 'WRITE_AUTHORITY_UNVERIFIED', 'WRITE_SUCCEEDED'):
        return old
    if receipt['status'] == 'DUPLICATE':
        return old
    # A proved terminal execution cannot regain its identity. Delayed cache
    # reconstruction retains its receipts without reopening this closed path.
    if old and old.get('write_path_state') == 'CLOSED_WITH_TERMINATION':
        return old
    identity = {field: receipt.get(field) for field in IDENTITY_FIELDS}
    value = old or {'identity': _key(receipt), **identity, 'pool': receipt['pool'],
        'capability': receipt['capability'], 'first_seen': receipt['at'], 'operation_ids': [],
        'operation_count': 0, 'reason_codes': [], 'evidence_refs': [], 'last_success_at': None}
    if kind == 'WRITE_SUCCEEDED':
        # Preflight/registration/ACKs are authorization checks, not recovery probes.
        if receipt['decision_phase'] != 'TRANSACTION_GATE' or receipt['operation'] in {
                'bind_execution', 'register_tool_execution', 'work_inputs', 'ack_events'}:
            return old
        if old:
            earlier = receipt['at'] < old['last_seen']
            if receipt.get('command_rowid') and old.get('last_command_rowid'):
                earlier = receipt['command_rowid'] < old['last_command_rowid']
            if earlier:
                return old
        value['last_success_at'] = receipt['at']
        value['last_success_decision_id'] = receipt['decision_id']
        value['last_success_operation_id'] = receipt['operation_id']
        value['last_success_command_rowid'] = receipt.get('command_rowid')
        if old and old.get('write_path_state') in {'WRITE_AUTHORITY_ANOMALY', 'WRITE_AUTHORITY_UNVERIFIED'}:
            value.update(write_path_state='RECOVERED', closed_at=receipt['at'], closure='NEW_BUSINESS_WRITE_SUCCEEDED')
        else:
            value['write_path_state'] = 'WRITABLE_OBSERVED'
    else:
        # Cache recovery can discover an older durable command after a newer
        # success. Preserve the historical receipt without resurrecting it.
        if old and old.get('last_success_at') is not None:
            if receipt['operation_id'] == old.get('last_success_operation_id'):
                return old
            earlier = receipt['at'] < old['last_success_at']
            if receipt.get('command_rowid') and old.get('last_success_command_rowid'):
                earlier = receipt['command_rowid'] < old['last_success_command_rowid']
            if earlier:
                return old
        if not old or old.get('write_path_state') in {'RECOVERED', 'WRITABLE_OBSERVED', 'CLOSED_WITH_TERMINATION'}:
            value.update(first_seen=receipt['at'], operation_ids=[], operation_count=0,
                         reason_codes=[], evidence_refs=[])
            value.pop('closed_at', None)
            value.pop('closure', None)
        # Source uncertainty may not downgrade a previously proved contradiction.
        value['write_path_state'] = ('WRITE_AUTHORITY_ANOMALY' if old and
            old.get('write_path_state') == 'WRITE_AUTHORITY_ANOMALY' else kind)
        if receipt['operation_id'] not in value['operation_ids']:
            value['operation_ids'].append(receipt['operation_id'])
            value['operation_count'] += 1
        value['reason_codes'] = list(dict.fromkeys(value['reason_codes'] + receipt['reason_codes']))
    value.update(last_seen=receipt['at'], last_decision_id=receipt['decision_id'])
    value['last_operation_id'] = receipt['operation_id']
    if receipt.get('command_rowid'):
        value['last_command_rowid'] = receipt['command_rowid']
    if receipt['evidence_ref'] not in value['evidence_refs']:
        value['evidence_refs'].append(receipt['evidence_ref'])
    return value


def _project(db, receipt):
    key = _key(receipt)
    row = db.execute('SELECT payload FROM write_paths WHERE identity=?', (key,)).fetchone()
    value = write_path_step(json.loads(row[0]) if row else None, receipt)
    if value is not None:
        db.execute('INSERT OR REPLACE INTO write_paths VALUES(?,?)', (key, _json(value)))
    if receipt['status'] == 'DUPLICATE':
        return
    if receipt['classification'] == 'WRITE_AUTHORITY_UNVERIFIED' and value and value.get(
            'write_path_state') in {'WRITE_AUTHORITY_ANOMALY','WRITE_AUTHORITY_UNVERIFIED'}:
        owner_key = _hash([receipt.get('pool'), *[receipt.get(field) for field in IDENTITY_FIELDS]])
        pending_key = _hash([owner_key, receipt['capability'], receipt['authority_version']])
        verification_key = _hash([owner_key, receipt['authority_version']])
        db.execute('INSERT INTO pending VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(identity) DO UPDATE SET '
            'last_seen=excluded.last_seen,receipt=excluded.receipt',
            (pending_key, owner_key, receipt['capability'], receipt['authority_version'], verification_key,
             receipt['at'], receipt['at'], _json(receipt)))
    elif value and value.get('write_path_state') == 'RECOVERED':
        owner_key = _hash([receipt.get('pool'), *[receipt.get(field) for field in IDENTITY_FIELDS]])
        db.execute('DELETE FROM pending WHERE owner_key=? AND capability=?', (owner_key, receipt['capability']))


def publish_receipt(database, receipt):
    """Best effort, bounded local cache write; never replace the original decision."""
    if receipt.get('replayed'):
        return receipt
    try:
        path = store_path(database)
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path, timeout=0.05) as db:
            db.executescript(_SCHEMA)
            recorded = receipt.get('receipt_recorded_at') or time.time()
            value = {**receipt, 'recorded_at': recorded, 'receipt_recorded_at': recorded,
                     'projection_recorded_at':time.time(), 'projection_state': 'RECORDED'}
            inserted = db.execute('INSERT OR IGNORE INTO receipts VALUES(?,?,?,?)',
                (value['decision_id'], value['operation_id'], value['at'], _json(value))).rowcount
            if inserted:
                _project(db, value)
            receipt.update(recorded_at=value['recorded_at'], receipt_recorded_at=recorded,
                           projection_state='RECORDED')
        path.chmod(0o600)
    except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
        receipt.update(projection_state='DEGRADED', recording_error=type(exc).__name__)
        logger.warning('Write diagnostic receipt unavailable: %s (%s)', receipt.get('decision_id'), type(exc).__name__)
    return receipt


def record_result(database, result, capture=None, **metadata):
    receipt = build_receipt(database, result, capture, **metadata)
    return publish_receipt(database, receipt)


def projection_anomalies(paths, source):
    return [{'type': row['write_path_state'].lower(),
        'rule_version': VERSION,
        'identity': _hash([row['identity'], 'write_authority']), 'severity': 1,
        'source': str(source), 'pool':row['pool'], 'write_path_id':row['identity'],
        **{key: row.get(key) for key in IDENTITY_FIELDS},
        'capability': row['capability'], 'reason_codes': row['reason_codes'],
        'first_seen': row['first_seen'], 'last_seen': row['last_seen'],
        'decision_ref': row['last_decision_id'], 'operation_id':row.get('last_operation_id'),
        'operation_count': row['operation_count']}
        for row in paths if row.get('write_path_state') in {
            'WRITE_AUTHORITY_ANOMALY', 'WRITE_AUTHORITY_UNVERIFIED'}]


def projection_closures(paths):
    """Explicit proofs shared by the cache adapter and pure temporal replay."""
    result = []
    for row in paths:
        state = row.get('write_path_state')
        if state not in {'RECOVERED','WRITABLE_OBSERVED','CLOSED_WITH_TERMINATION'}:
            continue
        proof = {'identity':_hash([row['identity'],'write_authority']),
            'write_path_id':row['identity'], 'pool':row['pool'], 'capability':row['capability'],
            **{key:row.get(key) for key in IDENTITY_FIELDS}, 'rule_version':VERSION,
            'closed_at':row.get('closed_at',row.get('last_success_at'))}
        if state in {'RECOVERED','WRITABLE_OBSERVED'}:
            proof.update(transition='recovered', recovery_basis='NEW_BUSINESS_WRITE_SUCCEEDED',
                closed_at=row.get('last_success_at') if row.get('last_success_at') is not None else row.get('closed_at'),
                status='COMMITTED', decision_phase='TRANSACTION_GATE',
                decision_id=row.get('last_success_decision_id'),
                operation_id=row.get('last_success_operation_id'),
                evidence_ref={'decision_id':row.get('last_success_decision_id')})
        else:
            proof.update(transition='closed_with_execution',
                evidence_ref=row.get('termination_evidence_ref'))
        result.append(proof)
    return result


def query_closures(pool_path, incidents, *, after='', limit=128):
    """Bounded primary-key lookups for already-active monitor incidents.

    The recent display window is deliberately not a recovery certificate. A
    stable incident-ID cursor eventually checks every active incident without
    querying the pool's full command/receipt/path history.
    """
    if type(limit) is not int or not 1 <= limit <= 128 or not isinstance(after,str):
        raise ValueError('BOUNDED_CLOSURE_PAGE_REQUIRED')
    candidates = sorted((row for row in incidents if
        row.get('type','').startswith('write_authority_') and row.get('identity','') > after),
        key=lambda row:row['identity'])
    selected = candidates[:limit]
    result = {'source_state':'NOT_COLLECTED','closures':[], 'queried':len(selected),
        'next_after':selected[-1]['identity'] if len(candidates)>limit else '',
        'has_more':len(candidates)>limit}
    path = store_path(pool_path)
    if not path.exists():
        return result
    try:
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=0.01) as db:
            db.execute('PRAGMA query_only=ON')
            keys = [row.get('write_path_id') or _key({**row,'pool':str(Path(pool_path).resolve())})
                    for row in selected]
            paths = []
            if keys:
                paths = [json.loads(row[0]) for row in db.execute(
                    'SELECT payload FROM write_paths WHERE identity IN ('+','.join('?' for _ in keys)+')',keys)]
        result.update(source_state='OBSERVED',closures=projection_closures(paths))
    except (OSError,ValueError,TypeError,KeyError,sqlite3.Error) as exc:
        result.update(source_state='DEGRADED', error=type(exc).__name__,
                     error_stage='query_closures')
    return result


def close_terminated(pool_path, identity, *, evidence_ref, at=None):
    """A trusted observer supplies already-verified terminal facts, never a probe.

    This only closes an observation and cannot clear a writer, alter a control,
    or manufacture successful write-path recovery.
    """
    if not isinstance(evidence_ref, dict) or evidence_ref.get('terminal_verified') is not True:
        raise ValueError('VERIFIED_TERMINATION_EVIDENCE_REQUIRED')
    evidence_ref = {**evidence_ref,'terminal_kind':'EXECUTION'}
    if not all(identity.get(key) is not None for key in ('generation','run_id','role','work_id',
                                                        'execution_id','execution_generation')):
        raise ValueError('EXACT_TERMINATED_IDENTITY_REQUIRED')
    path = store_path(pool_path)
    if not path.exists():
        return {'state':'NOT_COLLECTED'}
    count = 0
    try:
        with sqlite3.connect(path, timeout=0.01) as db:
            rows = db.execute("SELECT identity,payload FROM write_paths WHERE json_extract(payload,'$.work_id')=?",
                              (identity['work_id'],)).fetchall()
            for key, raw in rows:
                value = json.loads(raw)
                if any(value.get(field) != identity.get(field) for field in
                       ('generation','run_id','role','work_id','execution_id','execution_generation')):
                    continue
                if value.get('write_path_state') not in {'WRITE_AUTHORITY_ANOMALY','WRITE_AUTHORITY_UNVERIFIED'}:
                    continue
                value.update(write_path_state='CLOSED_WITH_TERMINATION', closure='CLOSED_WITH_TERMINATION',
                             closed_at=time.time() if at is None else at, termination_evidence_ref=evidence_ref)
                db.execute('UPDATE write_paths SET payload=? WHERE identity=?', (_json(value),key))
                count += 1
            owner_key = _hash([str(Path(pool_path).resolve()), *[identity.get(field) for field in IDENTITY_FIELDS]])
            db.execute('DELETE FROM pending WHERE owner_key=?', (owner_key,))
        return {'state':'CLOSED_WITH_TERMINATION','closed_paths':count}
    except (OSError, ValueError, sqlite3.Error) as exc:
        return {'state':'DEGRADED', 'error':type(exc).__name__,
                'error_stage':'close_terminated'}


def query_receipts(pool_path, *, role=None, after=0, limit=100):
    if type(limit) is not int or not 1 <= limit <= 1000 or type(after) is not int or after < 0:
        raise ValueError('BOUNDED_RECEIPT_PAGE_REQUIRED')
    path = store_path(pool_path)
    if not path.exists():
        return {'source_state':'NOT_COLLECTED', 'receipts':[], 'next_after':after}
    with sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=0.01) as db:
        db.execute('PRAGMA query_only=ON')
        where = 'rowid>?' + (' AND json_extract(receipt,\'$.role\')=?' if role else '')
        values = (after,role,limit+1) if role else (after,limit+1)
        rows = db.execute('SELECT rowid,receipt FROM receipts WHERE '+where+' ORDER BY rowid LIMIT ?', values).fetchall()
    return {'source_state':'OBSERVED', 'receipts':[json.loads(row[1]) for row in rows[:limit]],
            'next_after':rows[min(len(rows),limit)-1][0] if rows else after, 'has_more':len(rows)>limit}


def read_projection(pool_path, role=None, *, include_receipts=False, limit=100):
    path = store_path(pool_path)
    base = {'schema_version': 1, 'rule_version': VERSION, 'source': str(path),
            'source_state': 'NOT_COLLECTED', 'receipts': [], 'write_paths': [], 'anomalies': [], 'closures': []}
    if not path.exists():
        return base
    try:
        with sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=0.05) as db:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            where = "json_extract(payload,'$.write_path_state') IN ('WRITE_AUTHORITY_ANOMALY','WRITE_AUTHORITY_UNVERIFIED')"
            if role:
                where += " AND json_extract(payload,'$.role')=?"
            values = (role,) if role else ()
            active = [json.loads(row[0]) for row in db.execute(
                'SELECT payload FROM write_paths WHERE '+where+' ORDER BY identity LIMIT 1001', values)]
            closed_where = "json_extract(payload,'$.write_path_state') NOT IN ('WRITE_AUTHORITY_ANOMALY','WRITE_AUTHORITY_UNVERIFIED')"
            if role:
                closed_where += " AND json_extract(payload,'$.role')=?"
            closed = [json.loads(row[0]) for row in db.execute('SELECT payload FROM write_paths WHERE '+
                closed_where+' ORDER BY rowid DESC LIMIT 64', values)]
        paths = active[:1000] + closed
        base.update(source_state='OBSERVED', write_paths=paths)
        if len(active)>1000:
            base.update(source_state='DEGRADED', error='ACTIVE_DIAGNOSTIC_QUERY_LIMIT_REACHED',
                        error_stage='read_projection.active_query')
        if include_receipts:
            page = query_receipts(pool_path,role=role,limit=limit)
            base.update({key:page[key] for key in ('receipts','next_after','has_more') if key in page})
        base['anomalies'] = projection_anomalies(paths, path)
        base['closures'] = projection_closures(paths)
    except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
        base.update(source_state='DEGRADED', error=type(exc).__name__,
                    error_stage='read_projection')
        base['anomalies'] = [{'type': 'write_diagnostics_source_unverified',
            'identity': _hash([str(path), 'source']), 'severity': 1, 'source': str(path),
            'reason_codes': ['DIAGNOSTIC_SOURCE_UNAVAILABLE']}]
    return base


def rebuild_projection(pool_path, *, budget_seconds=0.05, limit=100):
    """Copy original command receipts, never re-execute a command or business ACK."""
    database = ledger_file(pool_path)
    if not database.exists():
        return {'state': 'NOT_COLLECTED', 'copied': 0}
    started = time.monotonic()
    copied = 0
    try:
        high = 0
        path = store_path(pool_path)
        if path.exists():
            with sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=0.01) as db:
                row = db.execute("SELECT value FROM metadata WHERE key='commands_rowid'").fetchone()
                high = int(row[0]) if row else 0
        with sqlite3.connect(database.as_uri()+'?mode=ro', uri=True, timeout=0.01) as db:
            db.execute('PRAGMA query_only=ON')
            rows = db.execute('SELECT rowid,result FROM commands WHERE rowid>? ORDER BY rowid LIMIT ?', (high, limit)).fetchall()
        for rowid, raw in rows:
            if time.monotonic()-started >= budget_seconds:
                break
            receipt = json.loads(raw).get('decision')
            if receipt:
                persisted = publish_receipt(database, receipt)
                if persisted['projection_state'] != 'RECORDED':
                    return {'state': 'DEGRADED', 'copied': copied}
                copied += 1
            path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(path, timeout=0.01) as cache:
                cache.executescript(_SCHEMA)
                cache.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)', ('commands_rowid', str(rowid)))
        return {'state': 'OBSERVED', 'copied': copied, 'budget_exhausted': time.monotonic()-started >= budget_seconds}
    except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
        return {'state': 'DEGRADED', 'copied': copied, 'error': type(exc).__name__}


def observe(pool_path, *, now=None, budget_seconds=0.05):
    """Existing tick's targeted supplement. Current facts never backfill history."""
    started = time.monotonic()
    def finished(value):
        elapsed = time.monotonic()-started
        value.update(collection_budget_seconds=budget_seconds, collection_elapsed_seconds=elapsed,
                     collection_budget_exhausted=elapsed >= budget_seconds)
        return value
    rebuild = rebuild_projection(pool_path, budget_seconds=budget_seconds/2)
    result = read_projection(pool_path)
    result['rebuild'] = rebuild
    result['verification'] = []
    if rebuild['state'] == 'DEGRADED':
        result.update(source_state='DEGRADED', verification_source_state='DEGRADED',
                      error=rebuild.get('error') or result.get('error'),
                      error_stage='rebuild_projection')
    if result['source_state'] != 'OBSERVED':
        return finished(result)
    checked = set()
    try:
        with sqlite3.connect(store_path(pool_path).as_uri()+'?mode=ro', uri=True, timeout=0.01) as db:
            db.execute('PRAGMA query_only=ON')
            pending = db.execute('SELECT p.verification_key,p.receipt FROM pending p LEFT JOIN verifications v '
                'ON v.capture_key=p.verification_key GROUP BY p.verification_key '
                'ORDER BY COALESCE(v.checked_at,0),p.first_seen LIMIT 32').fetchall()
    except (OSError, sqlite3.Error) as exc:
        result.update(source_state='DEGRADED', verification_source_state='DEGRADED',
                      error=type(exc).__name__, error_stage='pending_verification_query')
        return finished(result)
    result['pending_rows_read'] = len(pending)
    for key, raw in pending:
        receipt = json.loads(raw)
        if key in checked:
            continue
        checked.add(key)
        if time.monotonic()-started >= budget_seconds:
            result['verification'].append({'decision_id': receipt['decision_id'], 'state': 'BUDGET_EXHAUSTED',
                'verification_pending': True, 'reason':'LOCAL_COLLECTION_BUDGET_EXHAUSTED',
                'pending_age_seconds':max(0,(time.time() if now is None else now)-receipt['at'])})
            continue
        try:
            from business_ledger import BusinessLedger
            identity = {field: receipt[field] for field in IDENTITY_FIELDS if receipt.get(field) is not None}
            current = BusinessLedger(ledger_file(pool_path), readonly=True, timeout=0.01).validate_identity(identity)
            item = {'decision_id': receipt['decision_id'], 'verification_at': time.time() if now is None else now,
                'current_valid': current.get('valid'), 'current_code': current.get('code'),
                'current_evidence_ref': {'capture_id':current.get('capture', {}).get('capture_id'),
                                         'sha256':_hash(current.get('capture', {}))},
                'current_capture':current.get('capture'),
                'state': 'HISTORICAL_UNRESOLVED', 'historical_classification': receipt['classification'],
                'verification_pending': True, 'reason': 'CURRENT_FACTS_CANNOT_PROVE_ORIGINAL_DECISION'}
            item['pending_age_seconds'] = max(0, (time.time() if now is None else now)-receipt['at'])
        except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
            item = {'decision_id': receipt['decision_id'], 'state': 'SOURCE_UNAVAILABLE',
                    'verification_pending': True, 'error': type(exc).__name__,
                    'reason':'CURRENT_AUTHORITY_SOURCE_UNAVAILABLE',
                    'pending_age_seconds':max(0,(time.time() if now is None else now)-receipt['at'])}
        result['verification'].append(item)
        try:
            with sqlite3.connect(store_path(pool_path), timeout=0.01) as db:
                db.execute('INSERT OR REPLACE INTO verifications VALUES(?,?,?)',
                    (key, time.time() if now is None else now, _json(item)))
        except (OSError, sqlite3.Error):
            result['verification_source_state'] = 'DEGRADED'
            result.setdefault('error', 'VERIFICATION_PROJECTION_WRITE_FAILED')
            result.setdefault('error_stage', 'pending_verification_write')
    return finished(result)
