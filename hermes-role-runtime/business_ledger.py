"""Transactional business state for the fixed five-role Hermes pool.

The native scheduler owns executions. This ledger owns accepted work, never a
TTL lease. Callers inject a native execution identity; every business command
checks it again. Read-only construction never creates a directory or database.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import fnmatch
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
import uuid
from zoneinfo import ZoneInfo
import workflow_diagnostics as diagnostics

ROLES = ('COORDINATOR', 'A', 'B', 'C', 'TESTER')
PHASES = ('PENDING', 'DEVELOPING', 'REVIEW', 'COMPLETED', 'CANCELLED')
MODES = ('LEGACY', 'STAGED', 'ACTIVE')
CONTROL_ACTIONS = ('select_run', 'pause', 'resume', 'cancel', 'continue', 'rework')
SCHEMA_VERSION = 1
_FULL_COMMIT = re.compile(r'[0-9a-f]{40}|[0-9a-f]{64}')
_JSON_FIELDS = {'scope', 'task_ids', 'spec', 'dependencies', 'required_checks',
                'checkpoint', 'payload', 'result', 'target', 'provenance',
                'evidence', 'supersedes', 'receipt', 'writer', 'authorization', 'manifest', 'contract',
                'context'}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _id(prefix):
    return prefix + '-' + uuid.uuid4().hex


def _row(value):
    if value is None:
        return None
    result = dict(value)
    for key in _JSON_FIELDS & result.keys():
        if result[key] is not None:
            if key=='result' and result[key] in ('PASS','FAIL'):
                continue
            result[key] = json.loads(result[key])
    return result


def _process(pid):
    """Read Linux identity without treating an unreadable process as exited."""
    try:
        text = Path('/proc', str(int(pid)), 'stat').read_text()
        tail = text[text.rfind(')') + 2:].split()
        return {'state': tail[0], 'ppid': int(tail[1]), 'pgrp': int(tail[2]),
                'process_start': tail[19],
                'cgroup': Path('/proc', str(int(pid)), 'cgroup').read_text()}
    except FileNotFoundError:
        return None
    except (OSError, ValueError, IndexError):
        return {'state': 'unknown'}


class _Denied(Exception):
    def __init__(self, code, *, unknown=False, **details):
        self.code, self.unknown, self.details = code, unknown, details


def _deny(code, *, unknown=False, **details):
    diagnostics.denied_branch(code)
    raise _Denied(code, unknown=unknown, **details)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS authority (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), mode TEXT NOT NULL,
 generation INTEGER NOT NULL, current_run_id TEXT, migration_manifest TEXT,
 admission_open INTEGER NOT NULL DEFAULT 1, freeze_reason TEXT);
INSERT OR IGNORE INTO authority(singleton,mode,generation) VALUES(1,'STAGED',0);
CREATE TABLE IF NOT EXISTS commands (
 command_id TEXT PRIMARY KEY, verb TEXT NOT NULL, input_sha256 TEXT NOT NULL,
 result TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
 run_id TEXT PRIMARY KEY, requirement_id TEXT NOT NULL, requirement_root TEXT NOT NULL,
 repo TEXT NOT NULL, task_ids TEXT NOT NULL, control TEXT NOT NULL DEFAULT 'ACTIVE',
 plan_version INTEGER, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS plans (
 run_id TEXT NOT NULL REFERENCES runs, version INTEGER NOT NULL, spec TEXT NOT NULL,
 sha256 TEXT NOT NULL, accepted_at REAL NOT NULL, PRIMARY KEY(run_id,version));
CREATE TABLE IF NOT EXISTS tasks (
 run_id TEXT NOT NULL REFERENCES runs, task_id TEXT NOT NULL, role TEXT NOT NULL,
 dependencies TEXT NOT NULL, scope TEXT NOT NULL, base TEXT NOT NULL,
 ac_version TEXT NOT NULL, required_checks TEXT NOT NULL, priority INTEGER NOT NULL,
 phase TEXT, attempt INTEGER NOT NULL, plan_version INTEGER NOT NULL,
 unresolved_reason TEXT, contract TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(run_id,task_id));
CREATE TABLE IF NOT EXISTS attempts (
 run_id TEXT NOT NULL, task_id TEXT NOT NULL, attempt INTEGER NOT NULL,
 state TEXT NOT NULL, previous_candidate_id TEXT, reason TEXT,
 PRIMARY KEY(run_id,task_id,attempt),
 FOREIGN KEY(run_id,task_id) REFERENCES tasks(run_id,task_id));
CREATE TABLE IF NOT EXISTS works (
 work_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs, task_id TEXT,
 attempt INTEGER, role TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL,
 candidate_id TEXT, checkpoint TEXT NOT NULL DEFAULT '{}',
 processed_through INTEGER NOT NULL DEFAULT 0, issued_through INTEGER NOT NULL DEFAULT 0,
 active_execution_id TEXT, execution_generation INTEGER NOT NULL DEFAULT 0,
 created_at REAL NOT NULL, ended_at REAL);
CREATE TABLE IF NOT EXISTS role_current (
 role TEXT PRIMARY KEY, current_work_id TEXT REFERENCES works,
 writer_execution_id TEXT, control TEXT NOT NULL DEFAULT 'ACTIVE');
CREATE TABLE IF NOT EXISTS executions (
 execution_id TEXT PRIMARY KEY, work_id TEXT NOT NULL REFERENCES works,
 role TEXT NOT NULL, generation INTEGER NOT NULL, execution_generation INTEGER NOT NULL,
 pid INTEGER NOT NULL, process_start TEXT NOT NULL, scope TEXT NOT NULL,
 writer TEXT NOT NULL, state TEXT NOT NULL, bound_at REAL NOT NULL, ended_at REAL);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_execution_per_role
 ON executions(role) WHERE state IN ('ACTIVE','DRAINING');
CREATE TABLE IF NOT EXISTS execution_tools (
 execution_id TEXT NOT NULL REFERENCES executions, tool_id TEXT NOT NULL,
 pid INTEGER NOT NULL, process_start TEXT NOT NULL, writer TEXT NOT NULL,
 registered_at REAL NOT NULL, PRIMARY KEY(execution_id,tool_id));
CREATE TABLE IF NOT EXISTS candidates (
 candidate_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, task_id TEXT NOT NULL,
 attempt INTEGER NOT NULL, role TEXT NOT NULL, repo TEXT NOT NULL,
 base TEXT NOT NULL, head TEXT NOT NULL, ac_version TEXT NOT NULL,
 evidence TEXT NOT NULL, summary TEXT NOT NULL, state TEXT NOT NULL,
 created_at REAL NOT NULL,
 UNIQUE(run_id,task_id,attempt),
 FOREIGN KEY(run_id,task_id,attempt) REFERENCES attempts(run_id,task_id,attempt));
CREATE TABLE IF NOT EXISTS verifications (
 verification_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL REFERENCES candidates,
 check_name TEXT NOT NULL, role TEXT NOT NULL, result TEXT NOT NULL,
 evidence TEXT NOT NULL, supersedes TEXT NOT NULL, action_id TEXT,
 created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS external_actions (
 action_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL REFERENCES candidates,
 kind TEXT NOT NULL, authorization TEXT NOT NULL, state TEXT NOT NULL,
 created_at REAL NOT NULL, evidence TEXT);
CREATE TABLE IF NOT EXISTS decisions (
 decision_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL REFERENCES candidates,
 decision TEXT NOT NULL, evidence TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS authorizations (
 authorization_id TEXT PRIMARY KEY, kind TEXT NOT NULL, spec TEXT NOT NULL,
 provenance TEXT NOT NULL, created_at REAL NOT NULL, revoked_at REAL);
CREATE TABLE IF NOT EXISTS merges (
 merge_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL REFERENCES candidates,
 authorization_id TEXT NOT NULL REFERENCES authorizations,
 head TEXT NOT NULL, evidence TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS controls (
 request_id TEXT PRIMARY KEY, action TEXT NOT NULL, target TEXT NOT NULL,
 provenance TEXT NOT NULL, state TEXT NOT NULL, result TEXT, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events (
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, run_id TEXT,
 work_id TEXT, role TEXT, payload TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS work_contexts (
 work_id TEXT PRIMARY KEY REFERENCES works, context TEXT NOT NULL,
 context_sha256 TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TRIGGER IF NOT EXISTS work_contexts_no_update
 BEFORE UPDATE ON work_contexts BEGIN
 SELECT RAISE(ABORT,'WORK_CONTEXT_IMMUTABLE');
END;
CREATE TRIGGER IF NOT EXISTS work_contexts_no_delete
 BEFORE DELETE ON work_contexts BEGIN
 SELECT RAISE(ABORT,'WORK_CONTEXT_IMMUTABLE');
END;
CREATE TABLE IF NOT EXISTS outbox (
 notification_id TEXT PRIMARY KEY, event_id INTEGER NOT NULL REFERENCES events,
 kind TEXT NOT NULL, role TEXT, run_id TEXT, payload TEXT NOT NULL,
 state TEXT NOT NULL, receipt TEXT, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS imports (
 import_id TEXT PRIMARY KEY, manifest TEXT NOT NULL, created_at REAL NOT NULL);
"""


def _tester_candidate(db, run_id):
    # A user-directed retirement with an explicit no-rerun disposition must
    # remove that frozen candidate from both supplemental and regular tester
    # admission. Its evidence stays in the ledger; a later, different candidate
    # remains eligible for the next independent check.
    from workflow_verification_lifecycle import no_rerun_candidate_ids
    retired_candidates = no_rerun_candidate_ids(db, run_id)
    candidates = db.execute("SELECT c.* FROM candidates c JOIN tasks t USING(run_id,task_id) "
        "WHERE c.run_id=? AND c.state='CURRENT' AND t.phase='REVIEW' AND c.head!=c.base ORDER BY c.created_at,c.candidate_id", (run_id,)).fetchall()
    for candidate in candidates:
        if candidate['candidate_id'] in retired_candidates:
            continue
        rows = [_row(row) for row in db.execute('SELECT v.* FROM verifications v JOIN candidates c USING(candidate_id) '
            "WHERE c.run_id=? AND c.task_id=? AND c.attempt<=? AND v.check_name='independent_test'",
            (run_id, candidate['task_id'],candidate['attempt']))]
        current = [row for row in rows if row['candidate_id'] == candidate['candidate_id']]
        if not current:
            return candidate
        closed = {key for row in rows if row['result'] == 'PASS' for key in row['supersedes']}
        if any(row['result'] == 'PASS' for row in current) and any(
                row['result'] == 'FAIL' and row['verification_id'] not in closed for row in rows):
            return candidate
    return None


def _stage_dependencies(task, stage):
    contract=task.get('contract') or {}
    stages=contract.get('stage_dependencies')
    if not isinstance(stages,dict):
        return list(task.get('dependencies') or [])
    return list(stages.get(stage,[]))


def _dependency_gate(db, task, stage):
    """Evaluate an explicitly staged dependency gate; legacy edges stay hard."""
    if stage not in ('development_start','integration','acceptance'):
        raise ValueError('UNKNOWN_DEPENDENCY_STAGE')
    staged=isinstance((task.get('contract') or {}).get('stage_dependencies'),dict)
    dependencies=_stage_dependencies(task,stage)
    missing=[];contract_mismatches=[]
    for dependency in dependencies:
        source=_row(db.execute('SELECT * FROM tasks WHERE run_id=? AND task_id=?',
                               (task['run_id'],dependency)).fetchone())
        if not source:
            missing.append(dependency);continue
        if (staged and stage in ('integration','acceptance')
            and dependency not in _stage_dependencies(task,'development_start')):
            from_contract=(task.get('contract') or {}).get('dependency_contracts',[])
            to_contract=(source.get('contract') or {}).get('provided_contracts',[])
            reference=next((item for item in from_contract if item.get('task_id')==dependency),None)
            if not reference or not any(item.get('contract_id')==reference.get('contract_id')
                and item.get('version')==reference.get('version')
                and item.get('interface')==reference.get('interface') for item in to_contract):
                contract_mismatches.append(dependency);missing.append(dependency);continue
        # Plans without the new contract retain the original complete-before-
        # start behavior at every gate. In a staged plan, the integration gate
        # may open at a recorded merge; acceptance always needs completion.
        if staged and stage=='integration':
            merged=_row(db.execute(
                "SELECT m.head,c.repo FROM merges m JOIN candidates c USING(candidate_id) "
                "WHERE c.run_id=? AND c.task_id=? AND c.state='CURRENT' "
                "ORDER BY m.created_at DESC LIMIT 1",
                (task['run_id'],dependency)).fetchone())
            if merged:
                run=_row(db.execute('SELECT repo FROM runs WHERE run_id=?',(task['run_id'],)).fetchone())
                satisfied=subprocess.run(['git','-C',run['repo'],'merge-base','--is-ancestor',
                    merged['head'],task['base']],capture_output=True,timeout=15).returncode==0
            else:
                satisfied=False
        else:
            satisfied=source['phase']=='COMPLETED'
        if not satisfied:
            missing.append(dependency)
    return {'ready':not missing,'stage':stage,'dependencies':dependencies,'missing':missing,
            'contract_mismatches':contract_mismatches,
            'mode':'staged' if staged else 'legacy_complete'}


class BusinessLedger:
    def __init__(self, path, *, readonly=False, clock=None, timeout=10):
        self.path = Path(path).resolve()
        self.readonly = readonly
        self.clock = clock or time.time
        self.timeout = timeout
        if readonly:
            if not self.path.is_file():
                raise FileNotFoundError(self.path)
            with self._connection() as db:
                if db.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION:
                    raise ValueError('UNSUPPORTED_BUSINESS_SCHEMA')
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connection() as db:
                version = db.execute('PRAGMA user_version').fetchone()[0]
                if version not in (0, SCHEMA_VERSION):
                    raise ValueError('UNSUPPORTED_BUSINESS_SCHEMA')
                db.executescript(_SCHEMA)
                columns={r['name'] for r in db.execute('PRAGMA table_info(authority)')}
                if 'admission_open' not in columns:
                    db.execute('ALTER TABLE authority ADD COLUMN admission_open INTEGER NOT NULL DEFAULT 1')
                    db.execute('ALTER TABLE authority ADD COLUMN freeze_reason TEXT')
                if 'contract' not in {r['name'] for r in db.execute('PRAGMA table_info(tasks)')}:
                    db.execute("ALTER TABLE tasks ADD COLUMN contract TEXT NOT NULL DEFAULT '{}'")
                if 'evidence' not in {r['name'] for r in db.execute('PRAGMA table_info(external_actions)')}:
                    db.execute('ALTER TABLE external_actions ADD COLUMN evidence TEXT')
                db.executemany('INSERT OR IGNORE INTO role_current(role) VALUES(?)', [(r,) for r in ROLES])
                db.execute('PRAGMA user_version=1')

    @contextmanager
    def _connection(self):
        if self.readonly:
            db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=self.timeout)
        else:
            db = sqlite3.connect(self.path, timeout=self.timeout, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA busy_timeout='+str(max(0, int(self.timeout*1000))))
        if self.readonly:
            db.execute('PRAGMA query_only=ON')
        try:
            yield db
        finally:
            db.close()

    def _command(self, verb, command_id, payload, operation):
        if self.readonly:
            raise PermissionError('READ_ONLY_BUSINESS_LEDGER')
        if not isinstance(command_id, str) or not command_id or len(command_id) > 240:
            error = ValueError('command_id must be a stable nonempty string')
            error.decision = diagnostics.record_result(self.path,
                {'status': 'REJECTED', 'code': 'INVALID_COMMAND_ID'}, operation=verb,
                phase='TRANSACTION_GATE', side_effect_state='NONE', at=self.clock())
            raise error
        fingerprint = _hash(payload)
        identity = next((item for item in payload if isinstance(item, dict) and
                         {'role', 'execution_id', 'generation'} <= item.keys()), {}) if isinstance(payload, list) else {}
        if verb == 'bind_execution' and isinstance(payload, list) and len(payload)>1 and isinstance(payload[1],dict):
            identity = {'role':payload[0], **{key:payload[1][key] for key in ('generation','execution_id') if key in payload[1]}}
        capture = diagnostics.new_capture(identity, at=self.clock())
        result = None
        try:
            with diagnostics.capturing(capture), self._connection() as db:
                db.execute('BEGIN IMMEDIATE')
                try:
                    previous = db.execute('SELECT * FROM commands WHERE command_id=?', (command_id,)).fetchone()
                    if previous:
                        db.rollback()
                        if previous['verb'] != verb or previous['input_sha256'] != fingerprint:
                            result = {'status': 'REJECTED', 'code': 'COMMAND_ID_CONFLICT'}
                        else:
                            original = json.loads(previous['result'])
                            result = {**original, 'status': 'DUPLICATE', 'original_status': original['status']}
                            if original.get('decision'):
                                result['decision'] = {**original['decision'], 'replayed': True,
                                    'status': 'DUPLICATE', 'original_status': original['status']}
                            return result
                    else:
                        db.execute('SAVEPOINT business_command')
                        try:
                            result = {'status': 'COMMITTED', **(operation(db) or {})}
                        except _Denied as exc:
                            db.execute('ROLLBACK TO business_command')
                            result = {'status': 'UNVERIFIED' if exc.unknown else 'REJECTED',
                                      'code': exc.code, **exc.details}
                        except sqlite3.IntegrityError:
                            db.execute('ROLLBACK TO business_command')
                            result = {'status': 'REJECTED', 'code': 'STATE_CONSTRAINT_CONFLICT'}
                        db.execute('RELEASE business_command')
                        # Kept outside the rolled-back savepoint. The existing
                        # commands result is the rebuild source for this receipt.
                        result['decision'] = diagnostics.build_receipt(self.path, result, capture,
                            operation=verb, command_id=command_id, phase='TRANSACTION_GATE',
                            side_effect_state='POSSIBLE' if result['status']=='COMMITTED' else 'NONE', at=self.clock())
                        result['decision'].update(receipt_inserted_at=self.clock(),
                                                  receipt_storage='business_commands_result')
                        inserted = db.execute('INSERT INTO commands VALUES(?,?,?,?,?)',
                                   (command_id, verb, fingerprint, _json(result), self.clock()))
                        result['decision']['command_rowid'] = inserted.lastrowid
                        db.execute('UPDATE commands SET result=? WHERE command_id=?', (_json(result),command_id))
                        db.commit()
                        result['decision'].update(recorded_at=self.clock(), receipt_recorded_at=self.clock())
                except BaseException:
                    db.rollback()
                    raise
        except Exception as exc:
            receipt = diagnostics.record_result(self.path,
                {'status': 'UNVERIFIED', 'code': 'BUSINESS_SOURCE_UNAVAILABLE'}, capture,
                operation=verb, command_id=command_id, phase='TRANSACTION_GATE',
                side_effect_state='UNKNOWN', at=self.clock())
            try:
                exc.decision = receipt
            except (AttributeError, TypeError):
                pass
            raise
        if 'decision' not in result:
            result['decision'] = diagnostics.build_receipt(self.path, result, capture,
                operation=verb, command_id=command_id, phase='TRANSACTION_GATE',
                side_effect_state='NONE', at=self.clock())
        diagnostics.publish_receipt(self.path, result['decision'])
        return result

    def _one(self, db, table, key, value):
        row = _row(db.execute(f'SELECT * FROM {table} WHERE {key}=?', (value,)).fetchone())
        diagnostics.capture_fact(diagnostics.TABLE_FACTS.get(table), row)
        if row is None:
            _deny('UNKNOWN_' + table.upper(), identity=value)
        return row

    def _authority(self, db):
        value = dict(db.execute('SELECT mode,generation,current_run_id,admission_open,freeze_reason FROM authority WHERE singleton=1').fetchone())
        diagnostics.capture_fact('authority', value)
        return value

    def _event(self, db, kind, payload, *, run_id=None, work_id=None, role=None, notify=False):
        event_id = db.execute('INSERT INTO events(kind,run_id,work_id,role,payload,created_at) VALUES(?,?,?,?,?,?)',
                             (kind, run_id, work_id, role, _json(payload), self.clock())).lastrowid
        if notify:
            local = datetime.fromtimestamp(self.clock(), ZoneInfo('Asia/Shanghai'))
            quiet = local.hour >= 21 or local.hour < 8
            db.execute('INSERT INTO outbox VALUES(?,?,?,?,?,?,?,?,?)',
                       ('event-' + str(event_id), event_id, kind, role, run_id, _json(payload),
                        'SUPPRESSED' if quiet else 'PENDING', None, self.clock()))
        return event_id

    def mode(self):
        with self._connection() as db:
            return self._authority(db)

    def snapshot(self, run_id=None):
        with self._connection() as db:
            db.execute('BEGIN')
            result = {'schema_version': SCHEMA_VERSION, **self._authority(db),
                      'event_high_water': db.execute('SELECT COALESCE(MAX(event_id),0) FROM events').fetchone()[0]}
            for name in ('runs', 'plans', 'tasks', 'works', 'candidates', 'controls', 'outbox', 'imports'):
                if run_id and name not in ('controls','imports'):
                    rows = db.execute(f'SELECT * FROM {name} WHERE run_id=?', (run_id,)).fetchall()
                else:
                    rows = db.execute(f'SELECT * FROM {name}').fetchall()
                result[name] = [_row(r) for r in rows]
            result['roles'] = [_row(r) for r in db.execute('SELECT * FROM role_current')]
            try:
                if run_id:
                    result['work_contexts'] = [_row(r) for r in db.execute(
                        'SELECT c.* FROM work_contexts c JOIN works w USING(work_id) WHERE w.run_id=?', (run_id,))]
                else:
                    result['work_contexts'] = [_row(r) for r in db.execute('SELECT * FROM work_contexts')]
            except sqlite3.OperationalError as exc:
                # Read-only observers may encounter a ledger created before
                # this additive table existed. Supplemental event recovery is
                # the compatibility path for those records.
                if 'no such table' not in str(exc).lower():
                    raise
                result['work_contexts'] = []
            result['verifications'] = [_row(r) for r in db.execute(
                'SELECT v.* FROM verifications v JOIN candidates c USING(candidate_id)' +
                (' WHERE c.run_id=?' if run_id else ''), (run_id,) if run_id else ())]
            result['executions'] = [_row(r) for r in db.execute('SELECT * FROM executions')]
            result['execution_tools'] = [_row(r) for r in db.execute('SELECT * FROM execution_tools')]
            result['attempts'] = [_row(r) for r in db.execute('SELECT * FROM attempts')]
            result['decisions'] = [_row(r) for r in db.execute('SELECT * FROM decisions')]
            result['external_actions'] = [_row(r) for r in db.execute('SELECT * FROM external_actions')]
            result['events'] = [_row(r) for r in db.execute('SELECT * FROM events ORDER BY event_id')]
            outcomes = {event['payload']['execution_id']:event['payload'] for event in result['events']
                        if event['kind']=='EXECUTION_OUTCOME_OBSERVED'}
            for execution in result['executions']:
                execution['native_outcome'] = outcomes.get(execution['execution_id'],{}).get('native_outcome','UNOBSERVED')
                execution['writer_cleared'] = execution['state']=='ENDED'
            result['authorizations'] = [_row(r) for r in db.execute('SELECT * FROM authorizations')]
            result['merges'] = [_row(r) for r in db.execute('SELECT * FROM merges')]
            db.commit()
            return result

    def current_work(self, role):
        if role not in ROLES:
            raise ValueError('UNKNOWN_ROLE')
        with self._connection() as db:
            return _row(db.execute('SELECT w.* FROM works w JOIN role_current r ON r.current_work_id=w.work_id WHERE r.role=?',
                                   (role,)).fetchone())

    def admission_hint(self, role):
        """Read-only model-admission hint, never an event ACK or ownership grant.

        Only an explicit checkpoint waiting_external contract suppresses model
        resumption. A vague 'next' string cannot establish that work is waiting.
        Mechanical execution/checkpoint events never create business feedback.
        """
        if role not in ROLES:
            raise ValueError('UNKNOWN_ROLE')
        with self._connection() as db:
            authority=self._authority(db)
            current=self._one(db,'role_current','role',role)
            if authority['mode']!='ACTIVE' or not authority['admission_open']:
                return {'can_start_model':False,'reason':'MODE_OR_ADMISSION_BLOCKED'}
            if current['writer_execution_id']:
                return {'can_start_model':False,'reason':'WRITER_FENCE_PRESENT'}
            work=self._one(db,'works','work_id',current['current_work_id']) if current['current_work_id'] else None
            run_id=work['run_id'] if work else authority['current_run_id']
            if not run_id:
                return {'can_start_model':False,'reason':'NO_SELECTED_RUN'}
            run=self._one(db,'runs','run_id',run_id)
            if self._registered_pending(db,run_id):
                return {'can_start_model':False,'reason':'REGISTERED_REQUIREMENT_NOT_ACCEPTED'}
            if work and run_id!=authority['current_run_id']:
                return {'can_start_model':True,'reason':'OLD_WORK_SAFE_DRAIN_REQUIRED','work_id':work['work_id'],'drain_only':True}
            if run['control']!='ACTIVE' or current['control']!='ACTIVE':
                return {'can_start_model':False,'reason':'PAUSED_OR_CANCELLED'}
            mechanical={'EXECUTION_BOUND','EXECUTION_ENDED','EXECUTION_OUTCOME_OBSERVED','TOOL_EXECUTION_REGISTERED','CHECKPOINT_SAVED','WORK_CLAIMED'}
            events=[_row(r) for r in db.execute('SELECT * FROM events WHERE event_id>? AND (run_id=? OR run_id IS NULL) ORDER BY event_id',
                                              ((work or {}).get('processed_through',0),run_id))]
            relevant=[v for v in events if v['kind'] not in mechanical
                      and not (work and v['work_id']==work['work_id'] and v['kind'] in ('PLAN_PUBLISHED','CANDIDATE_DECIDED'))]
            if work:
                from workflow_verification_lifecycle import pending
                if work['kind'] in ('verification','supplemental_verification') and pending(db,work['work_id']):
                    return {'can_start_model':True,'reason':'VERIFICATION_RETIREMENT_SAFE_RELEASE_REQUIRED',
                            'work_id':work['work_id'],'drain_only':True,'requires_user_approval':False}
                waiting=work['checkpoint'].get('waiting_external')
                if waiting and not relevant:
                    from workflow_wait import review
                    recheck = review(self, db, work, run)
                    if recheck['due']:
                        return {'can_start_model':True,'reason':recheck['reason'],
                                'work_id':work['work_id'],'waiting_external':waiting,
                                'wait_recheck':recheck}
                    return {'can_start_model':False,'reason':'WAITING_EXTERNAL_WITHOUT_NEW_BUSINESS_EVENT',
                            'work_id':work['work_id'],'waiting_external':waiting,'wait_recheck':recheck}
                return {'can_start_model':True,'reason':'PENDING_BUSINESS_EVENT' if relevant else 'CHECKPOINT_RESUMPTION',
                        'work_id':work['work_id'],'event_ids':[v['event_id'] for v in relevant]}
            if role=='COORDINATOR':
                from workflow_completion import facts as completion_facts
                completion=completion_facts(self.snapshot(),run_id)
                if completion['state']=='READY_FOR_CLOSURE':
                    return {'can_start_model':True,'reason':'REQUIREMENT_FINAL_CLOSURE_READY'}
                tasks=[_row(r) for r in db.execute('SELECT * FROM tasks WHERE run_id=?',(run_id,))]
                if not run['plan_version'] or any(v['phase'] not in ('COMPLETED','CANCELLED') for v in tasks):
                    return {'can_start_model':True,'reason':'COORDINATION_WORK_REQUIRED'}
                last=_row(db.execute("SELECT * FROM works WHERE run_id=? AND role='COORDINATOR' ORDER BY rowid DESC LIMIT 1",(run_id,)).fetchone())
                cutoff=(last or {}).get('processed_through',0)
                pending=[_row(r) for r in db.execute('SELECT * FROM events WHERE event_id>? AND (run_id=? OR run_id IS NULL)',(cutoff,run_id))]
                pending=[v for v in pending if v['kind'] not in mechanical
                         and not (last and v['work_id']==last['work_id'] and v['kind'] in ('PLAN_PUBLISHED','CANDIDATE_DECIDED','WORK_RELEASED'))]
                services=db.execute("SELECT 1 FROM external_actions a JOIN candidates c USING(candidate_id) WHERE c.run_id=? AND a.state IN ('SUBMITTED','UNKNOWN','RESULT_READY') LIMIT 1",(run_id,)).fetchone()
                available=bool(pending or services)
                return {'can_start_model':available,'reason':'PENDING_TERMINAL_RUN_BUSINESS_EVENT' if available else 'RUN_TERMINAL_WITHOUT_NEW_BUSINESS_EVENT'}
            if role=='TESTER':
                pending=db.execute("SELECT work_id FROM works WHERE run_id=? AND role='TESTER' "
                    "AND kind IN ('supplemental_verification','integration_verification') AND state='QUEUED' "
                    "AND NOT EXISTS (SELECT 1 FROM events e WHERE e.work_id=works.work_id "
                    "AND e.kind='VERIFICATION_RETIREMENT_REQUESTED') ORDER BY rowid LIMIT 1",(run_id,)).fetchone()
                if pending:
                    queued=self._one(db,'works','work_id',pending['work_id'])
                    return {'can_start_model':True,'reason':'INTEGRATION_VERIFICATION_READY' if queued['kind']=='integration_verification' else 'SUPPLEMENTAL_VERIFICATION_READY','work_id':pending['work_id']}
                available=_tester_candidate(db,run_id)
                return {'can_start_model':bool(available),'reason':'CANDIDATE_VERIFICATION_READY' if available else 'NO_VERIFICATION_READY'}
            tasks=[_row(r) for r in db.execute("SELECT * FROM tasks WHERE run_id=? AND role=? AND phase='PENDING' AND unresolved_reason IS NULL",(run_id,role))]
            available=any(_dependency_gate(db,task,'development_start')['ready'] for task in tasks)
            return {'can_start_model':available,'reason':'TASK_READY' if available else 'NO_DEPENDENCY_READY_TASK'}

    def set_mode(self, mode, *, command_id, expected_generation=None, migration_manifest=None):
        def operation(db):
            authority = self._authority(db)
            if mode not in MODES:
                _deny('INVALID_MODE')
            if expected_generation is not None and authority['generation'] != expected_generation:
                _deny('STALE_MODE_GENERATION')
            if mode == authority['mode']:
                return authority
            if db.execute("SELECT 1 FROM executions WHERE state IN ('ACTIVE','DRAINING')").fetchone():
                _deny('WRITER_NOT_CLEARED', unknown=True)
            if authority['mode'] == 'ACTIVE' and not migration_manifest:
                _deny('FINAL_EXPORT_MANIFEST_REQUIRED')
            generation = authority['generation'] + 1
            db.execute('UPDATE authority SET mode=?,generation=?,migration_manifest=? WHERE singleton=1',
                       (mode, generation, _json(migration_manifest) if migration_manifest else None))
            event = self._event(db, 'MODE_CHANGED', {'mode': mode, 'generation': generation,
                                                    'manifest': migration_manifest})
            return {**self._authority(db), 'event_id': event}
        return self._command('set_mode', command_id, [mode, expected_generation, migration_manifest], operation)

    def create_run(self, requirement_id, requirement_root, repo, task_ids, *, command_id, run_id=None):
        def operation(db):
            if not requirement_id or not Path(requirement_root).is_dir() or not Path(repo).is_dir():
                _deny('INVALID_RUN_SCOPE')
            if (not isinstance(task_ids, list) or not task_ids or len(set(task_ids)) != len(task_ids)
                    or any(not isinstance(v, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}', v) for v in task_ids)):
                _deny('EXACT_TASK_SCOPE_REQUIRED')
            identifier = run_id or _id('run')
            if db.execute('SELECT 1 FROM runs WHERE run_id=?', (identifier,)).fetchone():
                _deny('RUN_ID_EXISTS')
            db.execute('INSERT INTO runs(run_id,requirement_id,requirement_root,repo,task_ids,created_at) VALUES(?,?,?,?,?,?)',
                       (identifier, requirement_id, str(Path(requirement_root).resolve()),
                        str(Path(repo).resolve()), _json(task_ids), self.clock()))
            event = self._event(db, 'RUN_REGISTERED', {'requirement_id': requirement_id, 'task_ids': task_ids}, run_id=identifier)
            return {'run_id': identifier, 'event_id': event}
        return self._command('create_run', command_id, [requirement_id, str(requirement_root), str(repo), task_ids, run_id], operation)

    def _registered_pending(self, db, run_id):
        event=_row(db.execute("SELECT * FROM events WHERE run_id=? AND kind='REQUIREMENT_PACKAGE_REGISTERED' ORDER BY event_id DESC LIMIT 1",
                              (run_id,)).fetchone())
        return bool(event and event['payload']['package'].get('prepared_run') and not db.execute(
            "SELECT 1 FROM events WHERE run_id=? AND kind='REQUIREMENT_ACCEPTED' LIMIT 1",(run_id,)).fetchone())

    def _release_idle_coordination(self, db, selected_run_id):
        """Retire old coordination only after every recorded writer is cleared.

        Never transfer an active execution, reopen a paused run, modify a task,
        or treat an unknown writer as ended. Caller holds the ledger transaction.
        """
        released = []
        rows = db.execute("SELECT w.* FROM works w JOIN role_current r "
            "ON r.current_work_id=w.work_id WHERE w.kind='coordination' "
            "AND w.role='COORDINATOR' AND w.state!='ENDED' AND w.run_id!=? "
            "AND r.writer_execution_id IS NULL AND w.active_execution_id IS NULL",
            (selected_run_id,)).fetchall()
        for row in rows:
            work = _row(row)
            old_run = self._one(db, 'runs', 'run_id', work['run_id'])
            if old_run['control'] not in ('DRAINING', 'PAUSED', 'CANCELLED'):
                continue
            if db.execute("SELECT 1 FROM executions WHERE work_id=? AND state!='ENDED'",
                          (work['work_id'],)).fetchone():
                continue
            db.execute("UPDATE works SET state='ENDED',ended_at=? WHERE work_id=?",
                       (self.clock(), work['work_id']))
            db.execute('UPDATE role_current SET current_work_id=NULL WHERE role=? AND current_work_id=?',
                       (work['role'], work['work_id']))
            event = self._event(db, 'WORK_RELEASED', {'reason': 'RUN_SWITCHED',
                'selected_run_id': selected_run_id, 'writer_clearance_required': False,
                'basis': 'NO_ACTIVE_EXECUTION_AND_ALL_RECORDED_WRITERS_ENDED', 'business_success': None},
                run_id=work['run_id'], work_id=work['work_id'], role=work['role'])
            released.append({'work_id': work['work_id'], 'event_id': event})
        return released

    def _select(self, db, run_id):
        selected = self._one(db, 'runs', 'run_id', run_id)
        if self._registered_pending(db,run_id):
            _deny('REGISTERED_REQUIREMENT_NOT_ACCEPTED')
        authority = self._authority(db)
        old = authority['current_run_id']
        if old == run_id:
            return {'run_id': run_id, 'unchanged': True,
                    'released_coordination': self._release_idle_coordination(db, run_id)}
        if old:
            db.execute("UPDATE runs SET control='DRAINING' WHERE run_id=? AND control='ACTIVE'", (old,))
        # Selecting a paused run never clears its pause.
        if selected['control'] == 'DRAINING':
            db.execute("UPDATE runs SET control='ACTIVE' WHERE run_id=?", (run_id,))
        db.execute('UPDATE authority SET current_run_id=? WHERE singleton=1', (run_id,))
        event = self._event(db, 'RUN_SELECTED', {'previous_run_id': old}, run_id=run_id)
        return {'run_id': run_id, 'event_id': event,
                'released_coordination': self._release_idle_coordination(db, run_id)}

    def select_run(self, run_id, *, command_id):
        return self._command('select_run', command_id, [run_id], lambda db: self._select(db, run_id))

    def _identity(self, db, identity, *, allow_drain=False):
        diagnostics.identity_check(allow_drain=allow_drain)
        if not isinstance(identity, dict) or not {'role','work_id','execution_id','execution_generation','generation'} <= identity.keys():
            _deny('EXECUTION_IDENTITY_REQUIRED')
        authority = self._authority(db)
        if authority['mode'] != 'ACTIVE':
            _deny('BUSINESS_MODE_INACTIVE')
        if identity['generation'] != authority['generation']:
            _deny('STALE_MODE_GENERATION')
        execution = self._one(db, 'executions', 'execution_id', identity['execution_id'])
        work = self._one(db, 'works', 'work_id', identity['work_id'])
        for key in ('role', 'work_id', 'execution_generation', 'generation'):
            if execution[key] != identity[key]:
                _deny('STALE_EXECUTION_IDENTITY')
        for key in ('run_id', 'task_id', 'attempt'):
            if key in identity and identity[key] != work[key]:
                _deny('WORK_SCOPE_MISMATCH')
        if execution['state'] != 'ACTIVE' or work['active_execution_id'] != execution['execution_id']:
            _deny('EXECUTION_NO_LONGER_WRITABLE')
        if work['state'] != 'RUNNING':
            _deny('WORK_NOT_RUNNING')
        current = self._one(db, 'role_current', 'role', work['role'])
        if current['current_work_id'] != work['work_id'] or current['writer_execution_id'] != execution['execution_id']:
            _deny('ROLE_OWNER_MISMATCH')
        actual = _process(execution['pid'])
        diagnostics.capture_process(actual)
        if actual is None or actual.get('process_start') != execution['process_start']:
            _deny('NATIVE_EXECUTION_NOT_LIVE', unknown=actual is not None and actual.get('state') == 'unknown')
        if actual.get('cgroup') != execution['writer']['cgroup']:
            _deny('EXECUTION_SCOPE_CHANGED', unknown=True)
        run = self._one(db, 'runs', 'run_id', work['run_id'])
        if not allow_drain:
            if work['kind'] in ('verification','supplemental_verification'):
                from workflow_verification_lifecycle import pending
                if pending(db,work['work_id']):
                    _deny('VERIFICATION_RETIREMENT_SAFE_RELEASE_ONLY')
            if not authority['admission_open']:
                _deny('MIGRATION_ADMISSION_FROZEN')
            if run['control'] != 'ACTIVE' or current['control'] != 'ACTIVE':
                _deny('CONTROL_BLOCKS_NEW_ACTION')
            if authority['current_run_id'] != work['run_id']:
                _deny('RUN_NOT_CURRENT')
            if work['kind']=='integration_verification':
                if not db.execute("SELECT 1 FROM events WHERE kind='INTEGRATION_VERIFICATION_QUEUED' AND work_id=? AND run_id=?",(work['work_id'],work['run_id'])).fetchone():
                    _deny('INTEGRATION_WORK_CONTEXT_REQUIRED')
            elif work['task_id']:
                task=_row(db.execute('SELECT * FROM tasks WHERE run_id=? AND task_id=?',(work['run_id'],work['task_id'])).fetchone())
                diagnostics.capture_fact('task', task)
                if task is None:
                    _deny('UNKNOWN_TASKS', unknown=True)
                if task['phase']=='CANCELLED' or task['unresolved_reason']:
                    _deny('TASK_CONTROL_OR_RECONCILIATION_GATE')
        diagnostics.identity_allowed()
        return work, execution

    def validate_identity(self, identity, *, allow_drain=False):
        capture = diagnostics.new_capture(identity, at=self.clock(), allow_drain=allow_drain)
        with diagnostics.capturing(capture), self._connection() as db:
            db.execute('BEGIN')
            try:
                work, execution = self._identity(db, identity, allow_drain=allow_drain)
                return {'valid': True, 'work': work, 'execution': execution, 'capture': capture}
            except _Denied as exc:
                return {'valid': False, 'status': 'UNVERIFIED' if exc.unknown else 'REJECTED',
                        'code': exc.code, **exc.details, 'capture': capture}

    def publish_plan(self, run_id, version, tasks, *, command_id, identity=None):
        def operation(db):
            run = self._one(db, 'runs', 'run_id', run_id)
            if self._authority(db)['mode'] == 'ACTIVE':
                work, _ = self._identity(db, identity)
                if work['role'] != 'COORDINATOR' or work['run_id'] != run_id:
                    _deny('COORDINATOR_REQUIRED')
            if type(version) is not int or version < 1 or version != (run['plan_version'] or 0) + 1:
                _deny('PLAN_VERSION_NOT_NEXT')
            if not isinstance(tasks, list) or not tasks:
                _deny('EMPTY_PLAN')
            task_map = {}
            for item in tasks:
                if not isinstance(item, dict) or set(item) - {'task_id','role','dependencies','stage_dependencies','dependency_contracts','provided_contracts',
                                                            'minimum_reading_set','risk_scenarios','test_environment','escalation_conditions',
                                                            'scope','base','ac_version','required_checks','priority',
                                                            'ac','acceptance','inputs','stop_conditions','worktree'}:
                    _deny('INVALID_TASK_SPEC')
                task = item.get('task_id')
                if task not in run['task_ids'] or task in task_map or item.get('role') not in ('A','B','C'):
                    _deny('TASK_OUTSIDE_ACCEPTED_SCOPE_OR_DUPLICATE')
                if not _FULL_COMMIT.fullmatch(str(item.get('base', ''))) or not item.get('ac_version'):
                    _deny('FROZEN_BASE_AND_AC_REQUIRED')
                scope = item.get('scope')
                if (not isinstance(scope, list) or not scope or any(not isinstance(v,str) or not v or Path(v).is_absolute()
                    or '..' in Path(v).parts for v in scope)):
                    _deny('INVALID_WRITE_SCOPE')
                dependencies = item.get('dependencies', [])
                stage_dependencies=item.get('stage_dependencies')
                if stage_dependencies is not None:
                    if (not isinstance(stage_dependencies,dict)
                        or set(stage_dependencies)!={'development_start','integration','acceptance'}):
                        _deny('INVALID_STAGED_DEPENDENCY_CONTRACT')
                    if (not isinstance(dependencies,list)
                        or any(not isinstance(value,str) or not value for value in dependencies)):
                        _deny('INVALID_DEPENDENCIES_OR_CHECKS')
                    for stage,values in stage_dependencies.items():
                        if (not isinstance(values,list) or len(values)!=len(set(values))
                            or any(not isinstance(value,str) or not value for value in values)):
                            _deny('INVALID_STAGED_DEPENDENCY_CONTRACT',stage=stage)
                    development_dependencies=set(stage_dependencies['development_start'])
                    integration_dependencies=set(stage_dependencies['integration'])
                    acceptance_dependencies=set(stage_dependencies['acceptance'])
                    if (not development_dependencies <= integration_dependencies
                        or not integration_dependencies <= acceptance_dependencies):
                        _deny('STAGED_DEPENDENCY_GATES_NOT_MONOTONIC',task_id=task,
                              development_start=sorted(development_dependencies),
                              integration=sorted(integration_dependencies),
                              acceptance=sorted(acceptance_dependencies))
                    stages=[*stage_dependencies['development_start'],*stage_dependencies['integration'],
                            *stage_dependencies['acceptance']]
                    if set(stages)!=set(dependencies):
                        _deny('STAGED_DEPENDENCIES_MUST_COVER_DEPENDENCIES')
                dependency_contracts=item.get('dependency_contracts',[])
                if not isinstance(dependency_contracts,list):
                    _deny('INVALID_DEPENDENCY_CONTRACTS')
                contract_ids=[]
                for reference in dependency_contracts:
                    if (not isinstance(reference,dict)
                        or set(reference)!={'task_id','contract_id','version','interface','simulation'}
                        or not isinstance(reference.get('task_id'),str) or not reference['task_id']
                        or not isinstance(reference.get('contract_id'),str) or not reference['contract_id'].strip()
                        or not isinstance(reference.get('version'),str) or not reference['version'].strip()
                        or not isinstance(reference.get('interface'),dict) or not reference['interface']
                        or not isinstance(reference.get('simulation'),dict) or not reference['simulation']):
                        _deny('INVALID_DEPENDENCY_CONTRACTS')
                    contract_ids.append(reference['task_id'])
                if len(contract_ids)!=len(set(contract_ids)):
                    _deny('DUPLICATE_DEPENDENCY_CONTRACT')
                provided_contracts=item.get('provided_contracts',[])
                if not isinstance(provided_contracts,list):
                    _deny('INVALID_PROVIDED_CONTRACTS')
                provided_ids=[]
                for provided in provided_contracts:
                    if (not isinstance(provided,dict)
                        or set(provided)!={'contract_id','version','interface'}
                        or not isinstance(provided.get('contract_id'),str) or not provided['contract_id'].strip()
                        or not isinstance(provided.get('version'),str) or not provided['version'].strip()
                        or not isinstance(provided.get('interface'),dict) or not provided['interface']):
                        _deny('INVALID_PROVIDED_CONTRACTS')
                    provided_ids.append(provided['contract_id'])
                if len(provided_ids)!=len(set(provided_ids)):
                    _deny('DUPLICATE_PROVIDED_CONTRACT')
                if 'minimum_reading_set' in item and (
                    not isinstance(item['minimum_reading_set'],list) or not item['minimum_reading_set']
                    or any(not isinstance(value,str) or not value.strip() for value in item['minimum_reading_set'])):
                    _deny('INVALID_MINIMUM_READING_SET')
                if 'risk_scenarios' in item and (
                    not isinstance(item['risk_scenarios'],list) or not item['risk_scenarios']
                    or any(not isinstance(value,dict) or set(value)!={'id','scenario','invariant','verification'}
                        or any(not isinstance(value.get(key),str) or not value[key].strip()
                               for key in ('id','scenario','invariant','verification'))
                        for value in item['risk_scenarios'])
                    or len({value['id'] for value in item['risk_scenarios']})!=len(item['risk_scenarios'])):
                    _deny('INVALID_RISK_SCENARIOS')
                if 'test_environment' in item and (
                    not isinstance(item['test_environment'],dict) or not item['test_environment']):
                    _deny('INVALID_TEST_ENVIRONMENT')
                if 'escalation_conditions' in item and (
                    not isinstance(item['escalation_conditions'],list) or not item['escalation_conditions']
                    or any(not isinstance(value,str) or not value.strip() for value in item['escalation_conditions'])):
                    _deny('INVALID_ESCALATION_CONDITIONS')
                if stage_dependencies is not None:
                    early_dependencies={dependency for dependency in dependencies
                                        if any(dependency in stage_dependencies[stage]
                                               for stage in ('integration','acceptance'))
                                        and dependency not in stage_dependencies['development_start']}
                    if set(contract_ids)!=early_dependencies:
                        _deny('PRESTART_DEPENDENCY_CONTRACT_REQUIRED',task_id=task,
                              dependency_task_ids=sorted(early_dependencies))
                    required_inputs=('ac','acceptance','inputs','stop_conditions','minimum_reading_set',
                                    'risk_scenarios','test_environment','escalation_conditions')
                    if any(key not in item or item[key] in (None,[],{}) for key in required_inputs):
                        _deny('STAGED_TASK_CONTRACT_INCOMPLETE',task_id=task,
                              missing=[key for key in required_inputs if key not in item or item[key] in (None,[],{})])
                    if (not isinstance(item['minimum_reading_set'],list)
                        or any(not isinstance(value,str) or not value.strip() for value in item['minimum_reading_set'])
                        or not isinstance(item['risk_scenarios'],list)
                        or any(not isinstance(value,dict) or not all(isinstance(value.get(key),str) and value[key].strip()
                            for key in ('id','scenario','invariant','verification')) for value in item['risk_scenarios'])
                        or not isinstance(item['test_environment'],dict)
                        or not isinstance(item['escalation_conditions'],list)
                        or any(not isinstance(value,str) or not value.strip() for value in item['escalation_conditions'])):
                        _deny('STAGED_TASK_CONTRACT_INVALID',task_id=task)
                elif dependency_contracts:
                    _deny('DEPENDENCY_CONTRACT_WITHOUT_STAGED_DEPENDENCIES')
                checks = item.get('required_checks', ['independent_test'])
                if (not isinstance(dependencies,list) or len(set(dependencies)) != len(dependencies)
                    or not isinstance(checks,list) or 'independent_test' not in checks
                    or any(not isinstance(c,str) or not c for c in checks) or len(set(checks)) != len(checks)
                    or type(item.get('priority',0)) is not int):
                    _deny('INVALID_DEPENDENCIES_OR_CHECKS')
                task_map[task] = {**item, 'dependencies': dependencies, 'required_checks': checks,
                                  'priority': item.get('priority',0),
                                  'contract':{k:item[k] for k in ('ac','acceptance','inputs','stop_conditions','worktree',
                                      'stage_dependencies','dependency_contracts','provided_contracts','minimum_reading_set','risk_scenarios',
                                      'test_environment','escalation_conditions') if k in item}}
                if len(_json(task_map[task]['contract']).encode())>1024*1024:
                    _deny('TASK_CONTRACT_TOO_LARGE')
                if 'worktree' in item and (not isinstance(item['worktree'],str) or not Path(item['worktree']).is_absolute()):
                    _deny('WORKTREE_MUST_BE_ABSOLUTE')
            visiting, visited = set(), set()
            def visit(task):
                if task in visiting:
                    _deny('PLAN_DEPENDENCY_CYCLE')
                if task not in task_map:
                    _deny('DEPENDENCY_NOT_IN_PLAN')
                if task in visited:
                    return
                visiting.add(task)
                contract=task_map[task]['contract']
                staged=contract.get('stage_dependencies')
                dependencies=task_map[task]['dependencies']
                if staged is not None:
                    dependencies=[*dependencies,*staged['development_start'],*staged['integration'],*staged['acceptance']]
                for dependency in set(dependencies):
                    visit(dependency)
                visiting.remove(task); visited.add(task)
            for task in task_map:
                visit(task)
            # Early consumer work is safe only against a contract the provider
            # has explicitly frozen in this same plan. The version and
            # interface are rechecked at integration/acceptance gates below.
            for task_id,spec in task_map.items():
                for reference in spec['contract'].get('dependency_contracts',[]):
                    provider=task_map.get(reference['task_id'])
                    provided=(provider or {}).get('contract',{}).get('provided_contracts',[])
                    if not provider or not any(item.get('contract_id')==reference['contract_id']
                        and item.get('version')==reference['version']
                        and item.get('interface')==reference['interface'] for item in provided):
                        _deny('DEPENDENCY_INTERFACE_CONTRACT_MISMATCH',task_id=task_id,
                              dependency_task_id=reference['task_id'],contract_id=reference['contract_id'])
            old = {r['task_id']: _row(r) for r in db.execute('SELECT * FROM tasks WHERE run_id=?',(run_id,))}
            if set(old) - set(task_map):
                _deny('PLAN_CANNOT_SILENTLY_DROP_TASKS')
            for task, item in task_map.items():
                if task in old and old[task]['phase'] != 'PENDING':
                    for key in ('role','dependencies','scope','base','ac_version','required_checks','priority','contract'):
                        if item.get(key) != old[task][key]:
                            _deny('IN_FLIGHT_TASK_SPEC_IMMUTABLE')
                if task in old:
                    db.execute('UPDATE tasks SET role=?,dependencies=?,scope=?,base=?,ac_version=?,required_checks=?,priority=?,plan_version=?,contract=? WHERE run_id=? AND task_id=?',
                               (item['role'],_json(item['dependencies']),_json(item['scope']),item['base'],item['ac_version'],
                                _json(item['required_checks']),item['priority'],version,_json(item['contract']),run_id,task))
                else:
                    db.execute('INSERT INTO tasks(run_id,task_id,role,dependencies,scope,base,ac_version,required_checks,priority,phase,attempt,plan_version,unresolved_reason,contract) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                               (run_id,task,item['role'],_json(item['dependencies']),_json(item['scope']),item['base'],item['ac_version'],
                                _json(item['required_checks']),item['priority'],'PENDING',1,version,None,_json(item['contract'])))
                    db.execute("INSERT INTO attempts VALUES(?,?,1,'CURRENT',NULL,NULL)",(run_id,task))
            db.execute('INSERT INTO plans VALUES(?,?,?,?,?)',(run_id,version,_json(tasks),_hash(tasks),self.clock()))
            db.execute('UPDATE runs SET plan_version=? WHERE run_id=?',(version,run_id))
            event = self._event(db,'PLAN_PUBLISHED',{'version':version,'task_ids':list(task_map)},run_id=run_id,
                                work_id=identity.get('work_id') if identity else None,
                                role=identity.get('role') if identity else None)
            return {'run_id':run_id,'version':version,'event_id':event}
        return self._command('publish_plan',command_id,[run_id,version,tasks,identity],operation)

    def _new_work(self, db, run_id, role, kind, task=None, candidate_id=None):
        identifier = _id('work')
        db.execute('INSERT INTO works(work_id,run_id,task_id,attempt,role,kind,state,candidate_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                   (identifier,run_id,task['task_id'] if task else None,task['attempt'] if task else None,
                    role,kind,'QUEUED',candidate_id,self.clock()))
        db.execute('UPDATE role_current SET current_work_id=? WHERE role=?',(identifier,role))
        if kind == 'development':
            db.execute("UPDATE tasks SET phase='DEVELOPING' WHERE run_id=? AND task_id=?",(run_id,task['task_id']))
            self._event(db,'WORK_CLAIMED',{'task_id':task['task_id'],'attempt':task['attempt'],'work_id':identifier},
                        run_id=run_id,work_id=identifier,role=role,notify=True)
        return self._one(db,'works','work_id',identifier)

    def bind_execution(self, role, execution, *, command_id):
        def operation(db):
            if role not in ROLES or not isinstance(execution,dict):
                _deny('INVALID_ROLE_OR_EXECUTION')
            authority = self._authority(db)
            if authority['mode'] != 'ACTIVE':
                _deny('BUSINESS_MODE_INACTIVE')
            if execution.get('generation') != authority['generation']:
                _deny('STALE_MODE_GENERATION')
            if not re.fullmatch(r'[0-9a-f]{32}',str(execution.get('execution_id',''))):
                _deny('INVALID_NATIVE_EXECUTION_ID')
            if type(execution.get('pid')) is not int or execution['pid'] <= 1:
                _deny('INVALID_EXECUTION_PID')
            actual = _process(execution['pid'])
            if actual is None or actual.get('state') == 'unknown':
                _deny('NATIVE_EXECUTION_UNVERIFIED',unknown=True)
            if str(execution.get('process_start')) != actual['process_start']:
                _deny('PID_START_MISMATCH')
            supplied_scope = execution.get('scope')
            supplied_cgroup = supplied_scope.get('cgroup') if isinstance(supplied_scope,dict) else supplied_scope
            if not supplied_cgroup or supplied_cgroup != actual['cgroup']:
                _deny('NATIVE_SCOPE_UNVERIFIED',unknown=True)
            current = self._one(db,'role_current','role',role)
            if current['writer_execution_id']:
                old = self._one(db,'executions','execution_id',current['writer_execution_id'])
                if old['execution_id'] == execution['execution_id']:
                    _deny('EXECUTION_ALREADY_BOUND_USE_CURRENT_WORK')
                _deny('OLD_WRITER_NOT_CLEARED',unknown=True,execution_id=old['execution_id'])
            if db.execute('SELECT 1 FROM executions WHERE execution_id=?',(execution['execution_id'],)).fetchone():
                _deny('NATIVE_EXECUTION_ID_REUSE')
            # Also reconcile an idle work left by an earlier implementation.
            # This is transactionally fenced; existing development drain stays.
            if authority['current_run_id']:
                self._release_idle_coordination(db, authority['current_run_id'])
            current = self._one(db,'role_current','role',role)
            work = self._one(db,'works','work_id',current['current_work_id']) if current['current_work_id'] else None
            if work and (work['active_execution_id'] or db.execute(
                    "SELECT 1 FROM executions WHERE work_id=? AND state!='ENDED'", (work['work_id'],)).fetchone()):
                _deny('PREVIOUS_WORK_WRITER_UNVERIFIED', unknown=True)
            run_id = authority['current_run_id']
            if not run_id:
                _deny('NO_SELECTED_RUN')
            drain_only = False
            if work and work['state']!='ENDED' and work['run_id']!=run_id:
                old_run=self._one(db,'runs','run_id',work['run_id'])
                if old_run['control'] not in ('DRAINING','PAUSED','CANCELLED'):
                    _deny('OLD_RUN_CONTROL_UNVERIFIED',unknown=True)
                run_id=work['run_id']; drain_only=True
            run = self._one(db,'runs','run_id',run_id)
            if self._registered_pending(db,run_id) and not drain_only:
                _deny('REGISTERED_REQUIREMENT_NOT_ACCEPTED')
            if current['control']!='ACTIVE' and not drain_only:
                _deny('ROLE_PAUSED')
            if not authority['admission_open'] and not drain_only:
                _deny('MIGRATION_ADMISSION_FROZEN')
            if run['control'] != 'ACTIVE' and not drain_only:
                _deny('RUN_NOT_ACTIVE')
            if work and work['state'] == 'ENDED':
                _deny('PREVIOUS_WORK_REQUIRES_SAFE_RELEASE')
            if work and work['kind'] in ('verification','supplemental_verification'):
                from workflow_verification_lifecycle import pending
                if pending(db,work['work_id']):
                    # Admit only the existing TESTER's safe-release drain path.
                    drain_only = True
            if not work:
                if role == 'COORDINATOR':
                    work = self._new_work(db,run_id,role,'coordination')
                elif role == 'TESTER':
                    work=_row(db.execute("SELECT * FROM works WHERE run_id=? AND role='TESTER' "
                        "AND kind IN ('supplemental_verification','integration_verification') AND state='QUEUED' "
                        "AND NOT EXISTS (SELECT 1 FROM events e WHERE e.work_id=works.work_id "
                        "AND e.kind='VERIFICATION_RETIREMENT_REQUESTED') ORDER BY rowid LIMIT 1",(run_id,)).fetchone())
                    if not work:
                        selected = _tester_candidate(db,run_id)
                        if not selected:
                            return {'code':'NO_ELIGIBLE_WORK','work':None}
                        task = _row(db.execute('SELECT * FROM tasks WHERE run_id=? AND task_id=?',(run_id,selected['task_id'])).fetchone())
                        work = self._new_work(db,run_id,role,'verification',task,selected['candidate_id'])
                else:
                    selected = None
                    for row in db.execute("SELECT * FROM tasks WHERE run_id=? AND role=? AND phase='PENDING' AND unresolved_reason IS NULL ORDER BY priority DESC,task_id",(run_id,role)):
                        task = _row(row)
                        if not _dependency_gate(db,task,'development_start')['ready']:
                            continue
                        # Shared write resources serialize across roles and runs.
                        occupied = [_row(v) for v in db.execute("SELECT DISTINCT t.* FROM tasks t JOIN works w ON t.run_id=w.run_id AND t.task_id=w.task_id LEFT JOIN executions e ON e.work_id=w.work_id WHERE w.kind='development' AND (w.state!='ENDED' OR e.state IN ('ACTIVE','DRAINING'))")]
                        if any(self._scope_overlap(task['scope'],v['scope']) and self._one(db,'runs','run_id',v['run_id'])['repo'] == run['repo'] for v in occupied):
                            continue
                        selected = task; break
                    if not selected:
                        return {'code':'NO_ELIGIBLE_WORK','work':None}
                    from workflow_integration_recovery import prepare_pending_base
                    selected=prepare_pending_base(self,db,selected)
                    work = self._new_work(db,run_id,role,'development',selected)
            generation = work['execution_generation'] + 1
            writer = {'cgroup':actual['cgroup'],'pgrp':actual['pgrp'],'dedicated_process_group':actual['pgrp']==execution['pid']}
            db.execute('INSERT INTO executions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       (execution['execution_id'],work['work_id'],role,authority['generation'],generation,
                        execution['pid'],actual['process_start'],_json(supplied_scope),_json(writer),'ACTIVE',self.clock(),None))
            db.execute("UPDATE works SET state='RUNNING',active_execution_id=?,execution_generation=? WHERE work_id=?",
                       (execution['execution_id'],generation,work['work_id']))
            db.execute('UPDATE role_current SET current_work_id=?,writer_execution_id=? WHERE role=?',
                       (work['work_id'],execution['execution_id'],role))
            event = self._event(db,'EXECUTION_BOUND',{'execution_id':execution['execution_id'],'execution_generation':generation},run_id=run_id,work_id=work['work_id'],role=role)
            work = self._one(db,'works','work_id',work['work_id'])
            identity = {'role':role,'work_id':work['work_id'],'execution_id':execution['execution_id'],
                        'execution_generation':generation,'generation':authority['generation'],
                        'run_id':run_id,'task_id':work['task_id'],'attempt':work['attempt']}
            return {'work':work,'identity':identity,'event_id':event,'drain_only':drain_only}
        return self._command('bind_execution',command_id,[role,execution],operation)

    @staticmethod
    def _scope_overlap(left, right):
        # Globs are conservative: their fixed directory prefixes reserve resources.
        def prefix(value):
            value = re.split(r'[*?\[]',value,1)[0].rstrip('/')
            return value or '.'
        return any(a=='.' or b=='.' or a==b or a.startswith(b+'/') or b.startswith(a+'/')
                   for a in map(prefix,left) for b in map(prefix,right))

    def events(self, *, after=0, through=None, run_id=None, limit=1000):
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError('INVALID_EVENT_LIMIT')
        with self._connection() as db:
            clauses, args = ['event_id>?'], [after]
            if through is not None:
                clauses.append('event_id<=?'); args.append(through)
            if run_id is not None:
                clauses.append('(run_id=? OR run_id IS NULL)'); args.append(run_id)
            return [_row(r) for r in db.execute('SELECT * FROM events WHERE '+' AND '.join(clauses)+' ORDER BY event_id LIMIT ?',(*args,limit))]

    def work_inputs(self, identity, *, limit=1000, command_id, after=None, through=None):
        def operation(db):
            if type(limit) is not int or not 1<=limit<=10000:
                _deny('INVALID_EVENT_LIMIT')
            work,_ = self._identity(db,identity,allow_drain=True)
            start=work['processed_through'] if after is None else after
            high=db.execute('SELECT COALESCE(MAX(event_id),0) FROM events').fetchone()[0]
            upper=high if through is None else through
            bounds={'processed_through':work['processed_through'],
                    'issued_through':work['issued_through'],'requested_after':start,
                    'hint':'续读省略 after；历史回读使用只读事件查询，不回退业务确认水位。'}
            if type(start) is not int:
                _deny('INVALID_PAGE_AFTER',**bounds)
            if start<work['processed_through']:
                _deny('PAGE_AFTER_ALREADY_PROCESSED',**bounds)
            if start>max(work['processed_through'],work['issued_through']):
                _deny('PAGE_AFTER_NOT_PREVIOUSLY_ISSUED',**bounds)
            if type(upper) is not int or not start<=upper<=high:
                _deny('PAGE_THROUGH_NOT_FROZEN_VALID_HIGH_WATER')
            rows = [_row(r) for r in db.execute('SELECT * FROM events WHERE event_id>? AND event_id<=? AND (run_id=? OR run_id IS NULL) ORDER BY event_id LIMIT ?',
                                               (start,upper,work['run_id'],limit))]
            issued = rows[-1]['event_id'] if rows else start
            db.execute('UPDATE works SET issued_through=MAX(issued_through,?) WHERE work_id=?',(issued,work['work_id']))
            more=bool(db.execute('SELECT 1 FROM events WHERE event_id>? AND event_id<=? AND (run_id=? OR run_id IS NULL) LIMIT 1',
                                 (issued,upper,work['run_id'])).fetchone())
            return {'events':rows,'through':issued,'frozen_through':upper,'has_more':more,'processed_through':work['processed_through']}
        return self._command('work_inputs',command_id,[identity,limit,after,through],operation)

    def _ack(self, db, work, through):
        if type(through) is not int or not work['processed_through'] <= through <= work['issued_through']:
            _deny('EVENT_PREFIX_NOT_ISSUED')
        db.execute('UPDATE works SET processed_through=? WHERE work_id=?',(through,work['work_id']))

    def ack_events(self, identity, through, *, command_id):
        def operation(db):
            work,_ = self._identity(db,identity,allow_drain=True)
            self._ack(db,work,through)
            return {'work_id':work['work_id'],'processed_through':through}
        return self._command('ack_events',command_id,[identity,through],operation)

    def checkpoint(self, identity, payload, *, command_id, processed_through=None, yield_execution=False):
        def operation(db):
            work,execution = self._identity(db,identity,allow_drain=True)
            if not isinstance(payload,dict) or len(_json(payload).encode()) > 128*1024:
                _deny('INVALID_CHECKPOINT')
            waiting = payload.get('waiting_external', [])
            if (not isinstance(waiting, list) or len(waiting)>32
                    or any(not isinstance(value,str) or not value.strip() or len(value)>512 for value in waiting)):
                _deny('INVALID_WAITING_EXTERNAL', expected='list[str]', recovery='列出真实外部依赖；内部能力故障交给协调者修复')
            if processed_through is not None:
                self._ack(db,work,processed_through)
            db.execute('UPDATE works SET checkpoint=? WHERE work_id=?',(_json(payload),work['work_id']))
            if yield_execution:
                db.execute("UPDATE executions SET state='DRAINING' WHERE execution_id=?",(execution['execution_id'],))
                db.execute("UPDATE works SET state='QUEUED',active_execution_id=NULL WHERE work_id=?",(work['work_id'],))
            event_payload = {'checkpoint':payload,'yield_execution':yield_execution}
            if waiting and work['role']=='COORDINATOR':
                from workflow_wait import repo_observation
                event_payload['wait_observation'] = repo_observation(self._one(db,'runs','run_id',work['run_id'])['repo'])
            event = self._event(db,'CHECKPOINT_SAVED',event_payload,run_id=work['run_id'],work_id=work['work_id'],role=work['role'])
            return {'event_id':event,'work_id':work['work_id'],'writer_clearance_required':yield_execution}
        return self._command('checkpoint',command_id,[identity,payload,processed_through,yield_execution],operation)

    def _writer_clear(self, execution):
        actual = _process(execution['pid'])
        if actual and actual.get('state') == 'unknown':
            _deny('WRITER_OBSERVATION_UNAVAILABLE',unknown=True)
        if actual and actual.get('process_start') == execution['process_start'] and actual.get('state') != 'Z':
            _deny('WRITER_STILL_ALIVE',unknown=True)
        writer = execution['writer']
        if writer.get('dedicated_process_group'):
            for path in Path('/proc').iterdir():
                if not path.name.isdigit():
                    continue
                observed = _process(int(path.name))
                if observed and observed.get('pgrp') == writer['pgrp'] and observed.get('state') != 'Z':
                    _deny('CHILD_WRITER_STILL_ALIVE',unknown=True,pid=int(path.name))
                if observed and observed.get('state') == 'unknown':
                    _deny('CHILD_SCOPE_UNVERIFIED',unknown=True)
            return {'kind':'dedicated_process_group','pgrp':writer['pgrp'],'checked_at':self.clock()}
        # Production systemd scopes must be unique, not the gateway/container cgroup.
        cgroups = [line.split(':',2)[2] for line in writer['cgroup'].splitlines() if line.startswith('0::')]
        if len(cgroups)!=1 or not re.search(r'/(?:[^/]+\.scope|[^/]+\.service)$',cgroups[0]):
            _deny('WRITER_SCOPE_NOT_ISOLATED',unknown=True)
        group = Path('/sys/fs/cgroup')/cgroups[0].lstrip('/')
        try:
            files = [group/'cgroup.procs', *group.glob('**/cgroup.procs')]
            if any(path.read_text().strip() for path in files):
                _deny('CHILD_WRITER_STILL_ALIVE',unknown=True)
        except FileNotFoundError:
            # A removed unique systemd scope and absent native PID is a terminal scope.
            if group.exists():
                _deny('CHILD_SCOPE_UNVERIFIED',unknown=True)
        except OSError:
            _deny('CHILD_SCOPE_UNVERIFIED',unknown=True)
        return {'kind':'systemd_scope','cgroup':cgroups[0],'checked_at':self.clock()}

    def end_execution(self, identity, writer_proof=None, *, command_id):
        def operation(db):
            execution = self._one(db,'executions','execution_id',identity.get('execution_id'))
            for key in ('role','work_id','execution_generation','generation'):
                if identity.get(key)!=execution[key]:
                    _deny('STALE_EXECUTION_IDENTITY')
            if execution['state']=='ENDED':
                return {'execution_id':execution['execution_id'],'already_ended':True}
            # Caller booleans never grant clearance. Always observe the actual scope.
            proof = self._writer_clear(execution)
            for tool in db.execute('SELECT * FROM execution_tools WHERE execution_id=?',(execution['execution_id'],)):
                tool=_row(tool)
                self._writer_clear(tool)
            work = self._one(db,'works','work_id',execution['work_id'])
            db.execute("UPDATE executions SET state='ENDED',ended_at=? WHERE execution_id=?",(self.clock(),execution['execution_id']))
            db.execute('UPDATE role_current SET writer_execution_id=NULL WHERE role=? AND writer_execution_id=?',(execution['role'],execution['execution_id']))
            db.execute("UPDATE works SET active_execution_id=NULL,state=CASE WHEN state='ENDED' THEN 'ENDED' ELSE 'QUEUED' END WHERE work_id=? AND active_execution_id=?",(work['work_id'],execution['execution_id']))
            event = self._event(db,'EXECUTION_ENDED',{'execution_id':execution['execution_id'],'writer_proof':proof,
                'writer_cleared':True,'business_success':None},run_id=work['run_id'],work_id=work['work_id'],role=work['role'])
            selected = self._authority(db)['current_run_id']
            released = self._release_idle_coordination(db, selected) if selected else []
            return {'event_id':event,'execution_id':execution['execution_id'],'writer_cleared':True,
                    'work_id':work['work_id'], 'released_coordination': released}
        return self._command('end_execution',command_id,[identity,writer_proof],operation)

    def observe_execution_outcome(self, execution_id, native_database, *, command_id):
        """Append native outcome evidence independently of writer clearance."""
        source=Path(native_database).resolve()
        def operation(db):
            execution=self._one(db,'executions','execution_id',execution_id)
            with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True,timeout=1) as native:
                native.row_factory=sqlite3.Row
                row=native.execute('SELECT * FROM executions WHERE id=?',(execution_id,)).fetchone()
                if row is None and native.execute("SELECT 1 FROM sqlite_master WHERE name='execution_history'").fetchone():
                    archived=native.execute('SELECT execution_json FROM execution_history WHERE id=?',(execution_id,)).fetchone()
                    row=json.loads(archived[0]) if archived else None
            if row is None:
                _deny('NATIVE_EXECUTION_OUTCOME_UNAVAILABLE',unknown=True)
            row=dict(row)
            if row.get('pid')!=execution['pid'] or str(row.get('process_started_at'))!=execution['process_start']:
                _deny('NATIVE_EXECUTION_OUTCOME_IDENTITY_MISMATCH',unknown=True)
            if row['status'] not in {'completed','failed','unknown'}:
                _deny('NATIVE_EXECUTION_NOT_TERMINAL',unknown=True)
            payload={'execution_id':execution_id,'native_outcome':row['status'].upper(),
                     'source':str(source),'native_row_sha256':_hash(row),'business_success':None}
            work=self._one(db,'works','work_id',execution['work_id'])
            event=self._event(db,'EXECUTION_OUTCOME_OBSERVED',payload,run_id=work['run_id'],
                              work_id=work['work_id'],role=work['role'])
            return {'event_id':event,**payload}
        return self._command('observe_execution_outcome',command_id,[execution_id,str(source)],operation)

    def register_tool_execution(self, identity, tool, *, command_id):
        """Associate actual tool processes with the native execution's writer fence."""
        def operation(db):
            work,execution=self._identity(db,identity)
            if not isinstance(tool,dict) or set(tool)-{'tool_id','pid','process_start','cgroup'} or not all(key in tool for key in ('tool_id','pid','process_start','cgroup')):
                _deny('EXACT_TOOL_PROCESS_IDENTITY_REQUIRED')
            if not isinstance(tool['tool_id'],str) or not tool['tool_id'] or type(tool['pid']) is not int:
                _deny('INVALID_TOOL_IDENTITY')
            actual=_process(tool['pid'])
            if actual is None or actual.get('state')=='unknown':
                _deny('TOOL_PROCESS_UNVERIFIED',unknown=True)
            if actual['process_start']!=str(tool['process_start']) or actual['cgroup']!=tool['cgroup']:
                _deny('TOOL_PROCESS_IDENTITY_MISMATCH')
            writer={'cgroup':actual['cgroup'],'pgrp':actual['pgrp'],'dedicated_process_group':actual['pgrp']==tool['pid']}
            db.execute('INSERT INTO execution_tools VALUES(?,?,?,?,?,?)',
                       (execution['execution_id'],tool['tool_id'],tool['pid'],actual['process_start'],_json(writer),self.clock()))
            event=self._event(db,'TOOL_EXECUTION_REGISTERED',{'tool_id':tool['tool_id'],'pid':tool['pid'],'process_start':actual['process_start']},
                              run_id=work['run_id'],work_id=work['work_id'],role=work['role'])
            return {'event_id':event,'tool_id':tool['tool_id'],'execution_id':execution['execution_id']}
        return self._command('register_tool_execution',command_id,[identity,tool],operation)

    def _evidence(self, evidence):
        if not isinstance(evidence,dict) or not isinstance(evidence.get('path'),str):
            _deny('EVIDENCE_FILE_REQUIRED')
        try:
            path = Path(evidence['path']).resolve()
            raw = path.read_bytes()
        except OSError:
            _deny('EVIDENCE_UNREADABLE',unknown=True)
        digest = hashlib.sha256(raw).hexdigest()
        if evidence.get('sha256') and evidence['sha256']!=digest:
            _deny('EVIDENCE_HASH_MISMATCH')
        return {**evidence,'path':str(path),'sha256':digest},raw

    def _git(self, repo, *args):
        try:
            return subprocess.check_output(['git','-C',str(repo),*args],stderr=subprocess.DEVNULL,text=True,timeout=15).strip()
        except (OSError,subprocess.SubprocessError):
            _deny('GIT_CANDIDATE_UNVERIFIED',unknown=True)

    def deliver(self, identity, candidate, summary, *, command_id):
        def operation(db):
            work,execution = self._identity(db,identity)
            if work['kind']!='development' or work['role'] not in ('A','B','C'):
                _deny('DEVELOPMENT_WORK_REQUIRED')
            task = _row(db.execute('SELECT * FROM tasks WHERE run_id=? AND task_id=?',(work['run_id'],work['task_id'])).fetchone())
            run = self._one(db,'runs','run_id',work['run_id'])
            required = {'repo','base','head','ac_version','evidence'}
            if not isinstance(candidate,dict) or not required <= candidate.keys() or set(candidate)-required-{'candidate_id'}:
                _deny('INVALID_CANDIDATE_SPEC')
            if not summary or not isinstance(summary,str):
                _deny('DELIVERY_SUMMARY_REQUIRED')
            repo=str(Path(candidate['repo']).resolve())
            expected_workspace=(self.path.parent/'workspaces'/work['work_id']).resolve()
            shared_git=self._git(repo,'rev-parse','--path-format=absolute','--git-common-dir') == self._git(run['repo'],'rev-parse','--path-format=absolute','--git-common-dir')
            deterministic_clone=(Path(repo)==expected_workspace and
                Path(self._git(repo,'config','--get','remote.origin.url')).resolve()==Path(run['repo']))
            if ((expected_workspace.exists() and Path(repo)!=expected_workspace)
                or not (shared_git or deterministic_clone)
                or candidate['base']!=task['base'] or candidate['ac_version']!=task['ac_version']):
                _deny('CANDIDATE_SCOPE_MISMATCH')
            if self._git(run['repo'],'rev-parse','--verify',candidate['base']+'^{commit}')!=candidate['base']:
                _deny('SOURCE_BASE_COMMIT_MISMATCH')
            if not _FULL_COMMIT.fullmatch(str(candidate['head'])):
                _deny('FULL_FROZEN_COMMIT_REQUIRED')
            if self._git(repo,'rev-parse','--verify',candidate['head']+'^{commit}')!=candidate['head']:
                _deny('CANDIDATE_COMMIT_MISMATCH')
            if self._git(repo,'rev-parse','HEAD')!=candidate['head'] or self._git(repo,'status','--porcelain'):
                _deny('CANDIDATE_WORKTREE_CHANGED_OR_DIRTY')
            if self._git(repo,'merge-base',candidate['base'],candidate['head'])!=candidate['base']:
                _deny('CANDIDATE_BASE_NOT_ANCESTOR')
            changed=self._git(repo,'diff','--name-only',candidate['base'],candidate['head']).splitlines()
            if not changed:
                _deny('EMPTY_DEVELOPMENT_CANDIDATE',recovery='未交付时用refresh_development_base刷新基线；不要交付占位候选或绑定其他head测试证据。')
            if any(not any(fnmatch.fnmatchcase(name,scope) or name==scope.rstrip('/') or name.startswith(scope.rstrip('/')+'/') for scope in task['scope']) for name in changed):
                _deny('CANDIDATE_CHANGES_OUTSIDE_SCOPE')
            if not isinstance(candidate['evidence'],list) or not candidate['evidence']:
                _deny('DELIVERY_EVIDENCE_REQUIRED')
            evidence = [self._evidence(v)[0] for v in candidate['evidence']]
            if self._git(repo,'rev-parse','HEAD')!=candidate['head'] or self._git(repo,'status','--porcelain'):
                _deny('CANDIDATE_CHANGED_DURING_FREEZE')
            identifier = candidate.get('candidate_id') or _id('candidate')
            db.execute('INSERT INTO candidates VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (identifier,work['run_id'],work['task_id'],work['attempt'],work['role'],repo,
                        candidate['base'],candidate['head'],candidate['ac_version'],_json(evidence),summary,'CURRENT',self.clock()))
            db.execute("UPDATE tasks SET phase='REVIEW' WHERE run_id=? AND task_id=?",(work['run_id'],work['task_id']))
            db.execute("UPDATE works SET state='ENDED',ended_at=? WHERE work_id=?",(self.clock(),work['work_id']))
            db.execute('UPDATE role_current SET current_work_id=NULL WHERE role=?',(work['role'],))
            db.execute("UPDATE executions SET state='DRAINING' WHERE execution_id=?",(execution['execution_id'],))
            event = self._event(db,'CANDIDATE_DELIVERED',{'candidate_id':identifier,'task_id':work['task_id'],'attempt':work['attempt'],'summary':summary,
                                                       'capacity_released':True,'writer_clearance_required':True},run_id=work['run_id'],work_id=work['work_id'],role=work['role'],notify=True)
            return {'candidate_id':identifier,'event_id':event,'capacity_released':True,'writer_clearance_required':True}
        return self._command('deliver',command_id,[identity,candidate,summary],operation)

    def _candidate_evidence(self, candidate):
        for item in candidate['evidence']:
            self._evidence(item)
        if self._git(candidate['repo'],'rev-parse','--verify',candidate['head']+'^{commit}')!=candidate['head']:
            _deny('CANDIDATE_COMMIT_MISSING',unknown=True)

    def register_external_action(self, identity, action_id, kind, candidate_id, authorization, *, command_id):
        def operation(db):
            work,_ = self._identity(db,identity)
            candidate = self._one(db,'candidates','candidate_id',candidate_id)
            if work['role']!='COORDINATOR' or work['run_id']!=candidate['run_id'] or kind not in ('ocr','strong_review'):
                _deny('COORDINATOR_SERVICE_ACTION_REQUIRED')
            if candidate['state']!='CURRENT':
                _deny('CANDIDATE_SUPERSEDED')
            if not isinstance(authorization,dict) or set(authorization)-{'authorization_id','service_id','service_token_sha256'}:
                _deny('TRUSTED_SERVICE_GRANT_ID_REQUIRED')
            grant=self._authorization(db,authorization.get('authorization_id'),'external')
            spec=grant['spec']
            from workflow_autonomy import covers
            if not covers(spec,candidate,kind):
                _deny('SERVICE_GRANT_SCOPE_MISMATCH')
            consumed=sum(1 for row in db.execute("SELECT authorization FROM external_actions WHERE state!='REJECTED'")
                         if json.loads(row['authorization']).get('authorization_id')==grant['authorization_id'])
            if consumed>=spec['max_calls']:
                _deny('SERVICE_GRANT_CALL_LIMIT')
            frozen_authorization={**authorization,'candidate_id':candidate_id,'grant_sha256':_hash(grant['spec']),
                'admitted_at':self.clock(),
                'execution_deadline':self.clock()+spec.get('execution_seconds',7200)}
            if kind=='strong_review':
                count = db.execute("SELECT COUNT(*) FROM external_actions WHERE kind='strong_review' AND created_at>?",(self.clock()-3600,)).fetchone()[0]
                if count>=5:
                    _deny('STRONG_REVIEW_RATE_LIMIT')
            if kind=='ocr' and db.execute("SELECT 1 FROM external_actions WHERE kind='ocr' AND state IN ('SUBMITTED','UNKNOWN')").fetchone():
                _deny('OCR_SERIAL_ACTION_STILL_ACTIVE_OR_UNKNOWN')
            db.execute('INSERT INTO external_actions(action_id,candidate_id,kind,authorization,state,created_at) VALUES(?,?,?,?,?,?)',
                       (action_id,candidate_id,kind,_json(frozen_authorization),'SUBMITTED',self.clock()))
            event = self._event(db,'EXTERNAL_ACTION_REGISTERED',{'action_id':action_id,'kind':kind,'candidate_id':candidate_id},run_id=work['run_id'],work_id=work['work_id'],role=work['role'])
            return {'action_id':action_id,'event_id':event}
        return self._command('register_external_action',command_id,[identity,action_id,kind,candidate_id,authorization],operation)

    def _trusted_grant(self, provenance):
        if (not isinstance(provenance,dict) or provenance.get('authenticated') is not True
            or provenance.get('current_full') is not True or not provenance.get('message_id')
            or not provenance.get('user_id') or not provenance.get('primary_text')):
            _deny('CURRENT_TRUSTED_AUTHORIZATION_SOURCE_REQUIRED')

    def external_dispatch_readiness(self, identity, action_id):
        """Recheck live control and grant immediately before native dispatch."""
        with self._connection() as db:
            try:
                work,_=self._identity(db,identity)
                action=self._one(db,'external_actions','action_id',action_id)
                candidate=self._one(db,'candidates','candidate_id',action['candidate_id'])
                grant=self._authorization(db,action['authorization'].get('authorization_id'),'external')
                if (work['role']!='COORDINATOR' or work['run_id']!=candidate['run_id']
                    or candidate['state']!='CURRENT' or action['state']!='SUBMITTED'
                    or not __import__('workflow_autonomy').covers(grant['spec'],candidate,action['kind'])):
                    _deny('EXTERNAL_DISPATCH_SCOPE_CHANGED')
                return {'valid':True,'action_id':action_id}
            except _Denied as exc:
                return {'valid':False,'status':'UNVERIFIED' if exc.unknown else 'REJECTED','code':exc.code,**exc.details}

    def _authorization(self, db, authorization_id, kind):
        grant=self._one(db,'authorizations','authorization_id',authorization_id)
        if grant['kind']!=kind or grant['revoked_at'] is not None:
            _deny('AUTHORIZATION_REVOKED_OR_WRONG_KIND')
        if grant['spec']['expires_at']<=self.clock():
            _deny('AUTHORIZATION_EXPIRED')
        seen={authorization_id}; parent_id=grant['spec'].get('parent_policy_id')
        while parent_id:
            if parent_id in seen:
                _deny('AUTHORIZATION_PARENT_CYCLE')
            seen.add(parent_id)
            parent=self._one(db,'authorizations','authorization_id',parent_id)
            if parent['revoked_at'] is not None or parent['spec']['expires_at']<=self.clock():
                _deny('PARENT_AUTHORIZATION_REVOKED_OR_EXPIRED')
            if parent['kind']=='merge_task_policy':
                from workflow_task_merge_policy import boundary
                task=_row(db.execute('SELECT * FROM tasks WHERE run_id=? AND task_id=?',
                    (parent['spec']['run_id'],parent['spec']['task_id'])).fetchone())
                if not task or boundary(task)!=parent['spec']['task_boundary_sha256']:
                    _deny('TASK_MERGE_POLICY_CONTRACT_CHANGED')
            parent_id=parent['spec'].get('parent_policy_id')
        return grant

    def grant_external_authorization(self, spec, provenance, *, command_id):
        def operation(db):
            self._trusted_grant(provenance)
            if (not isinstance(spec,dict) or set(spec)-{'authorization_id','candidate_id','run_id','task_ids','service_kind','max_calls','expires_at','execution_seconds'}
                or spec.get('service_kind') not in ('ocr','strong_review') or type(spec.get('max_calls')) is not int
                or not 1<=spec['max_calls']<=(1000 if spec.get('run_id') else 5)
                or type(spec.get('execution_seconds',7200)) is not int or not 60<=spec.get('execution_seconds',7200)<=7200
                or not isinstance(spec.get('expires_at'),(int,float)) or not self.clock()<spec['expires_at']<=self.clock()+72*3600):
                _deny('EXACT_SERVICE_GRANT_SPEC_REQUIRED')
            if spec.get('candidate_id'):
                if 'run_id' in spec or 'task_ids' in spec:
                    _deny('AMBIGUOUS_SERVICE_GRANT_SCOPE')
                candidate=self._one(db,'candidates','candidate_id',spec['candidate_id'])
                if candidate['state']!='CURRENT':
                    _deny('CANDIDATE_NOT_CURRENT')
                run_id=candidate['run_id']
            else:
                run=self._one(db,'runs','run_id',spec.get('run_id'))
                tasks=spec.get('task_ids')
                if (not isinstance(tasks,list) or not tasks or any(not isinstance(v,str) for v in tasks)
                        or not set(tasks)<=set(run['task_ids']) or len(set(tasks))!=len(tasks)):
                    _deny('EXACT_ACCEPTED_TASK_SCOPE_REQUIRED')
                run_id=run['run_id']
            identifier=spec.get('authorization_id') or _id('service-auth')
            db.execute('INSERT INTO authorizations VALUES(?,?,?,?,?,NULL)',(identifier,'external',_json(spec),_json(provenance),self.clock()))
            event=self._event(db,'EXTERNAL_AUTHORIZATION_GRANTED',{'authorization_id':identifier,'spec':spec},run_id=run_id)
            return {'authorization_id':identifier,'event_id':event}
        return self._command('grant_external_authorization',command_id,[spec,provenance],operation)

    def grant_merge_authorization(self, spec, provenance, *, command_id):
        def operation(db):
            self._trusted_grant(provenance)
            required={'run_id','task_id','attempt','repo','branch','base','ac_version','expires_at'}
            if not isinstance(spec,dict) or not required<=spec.keys() or set(spec)-required-{'authorization_id','postmerge_argv','junit_format','timeout_seconds'}:
                _deny('EXACT_MERGE_GRANT_SPEC_REQUIRED')
            task=_row(db.execute('SELECT * FROM tasks WHERE run_id=? AND task_id=?',(spec['run_id'],spec['task_id'])).fetchone())
            run=self._one(db,'runs','run_id',spec['run_id'])
            if (not task or task['attempt']!=spec['attempt'] or task['base']!=spec['base'] or task['ac_version']!=spec['ac_version']
                or str(Path(spec['repo']).resolve())!=run['repo'] or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9/_.-]{0,150}',spec['branch'])
                or spec['branch'].startswith('-') or '..' in spec['branch']
                or not isinstance(spec['expires_at'],(int,float)) or spec['expires_at']<=self.clock()):
                _deny('MERGE_GRANT_SCOPE_MISMATCH')
            if 'postmerge_argv' in spec and (not isinstance(spec['postmerge_argv'],list) or not spec['postmerge_argv'] or any(not isinstance(v,str) or not v or '\0' in v for v in spec['postmerge_argv'])):
                _deny('INVALID_POSTMERGE_CHECK_ARGV')
            identifier=spec.get('authorization_id') or _id('merge-auth')
            db.execute('INSERT INTO authorizations VALUES(?,?,?,?,?,NULL)',(identifier,'merge',_json(spec),_json(provenance),self.clock()))
            event=self._event(db,'MERGE_AUTHORIZATION_GRANTED',{'authorization_id':identifier,'spec':spec},run_id=spec['run_id'])
            from workflow_task_merge_policy import register
            policy=register(self,db,{'authorization_id':identifier,'kind':'merge','spec':spec,
                'provenance':provenance},task)
            return {'authorization_id':identifier,'event_id':event,**(policy or {})}
        return self._command('grant_merge_authorization',command_id,[spec,provenance],operation)

    def revoke_authorization(self, authorization_id, provenance, *, command_id):
        def operation(db):
            self._trusted_grant(provenance)
            self._one(db,'authorizations','authorization_id',authorization_id)
            db.execute('UPDATE authorizations SET revoked_at=? WHERE authorization_id=?',(self.clock(),authorization_id))
            event=self._event(db,'AUTHORIZATION_REVOKED',{'authorization_id':authorization_id})
            return {'authorization_id':authorization_id,'event_id':event}
        return self._command('revoke_authorization',command_id,[authorization_id,provenance],operation)

    def merge_readiness(self, identity, candidate_id, authorization_id):
        with self._connection() as db:
            try:
                work,_=self._identity(db,identity)
                candidate=self._one(db,'candidates','candidate_id',candidate_id)
                grant=self._authorization(db,authorization_id,'merge')
                spec=grant['spec']
                if work['role']!='COORDINATOR' or work['run_id']!=candidate['run_id']:
                    _deny('COORDINATOR_REQUIRED')
                for key in ('run_id','task_id','attempt','base','ac_version'):
                    if spec[key]!=candidate[key]:
                        _deny('MERGE_GRANT_CANDIDATE_MISMATCH')
                task=_row(db.execute('SELECT * FROM tasks WHERE run_id=? AND task_id=?',
                                     (candidate['run_id'],candidate['task_id'])).fetchone())
                dependency_gate=_dependency_gate(db,task,'integration')
                if not dependency_gate['ready']:
                    _deny('INTEGRATION_DEPENDENCIES_NOT_READY',gate=dependency_gate)
                gate=self._acceptance(db,candidate_id)
                if not gate['ready']:
                    _deny('ACCEPTANCE_GATES_NOT_CLEAR',gates=gate)
                if not db.execute("SELECT 1 FROM decisions WHERE candidate_id=? AND decision='APPROVE'",(candidate_id,)).fetchone():
                    _deny('COORDINATOR_APPROVAL_REQUIRED')
                return {'ready':True,'candidate':candidate,'authorization':grant}
            except _Denied as exc:
                return {'ready':False,'status':'UNVERIFIED' if exc.unknown else 'REJECTED','code':exc.code,**exc.details}

    def record_merge(self, identity, candidate_id, authorization_id, head, evidence, *, command_id):
        def operation(db):
            work,_=self._identity(db,identity)
            candidate=self._one(db,'candidates','candidate_id',candidate_id)
            grant=self._authorization(db,authorization_id,'merge')
            if work['role']!='COORDINATOR' or work['run_id']!=candidate['run_id'] or head!=candidate['head']:
                _deny('MERGE_SCOPE_MISMATCH')
            for key in ('run_id','task_id','attempt','base','ac_version'):
                if grant['spec'][key]!=candidate[key]:
                    _deny('MERGE_GRANT_CANDIDATE_MISMATCH')
            task=_row(db.execute('SELECT * FROM tasks WHERE run_id=? AND task_id=?',
                                 (candidate['run_id'],candidate['task_id'])).fetchone())
            dependency_gate=_dependency_gate(db,task,'integration')
            if not dependency_gate['ready']:
                _deny('INTEGRATION_DEPENDENCIES_NOT_READY',gate=dependency_gate)
            if not self._acceptance(db,candidate_id)['ready'] or not db.execute("SELECT 1 FROM decisions WHERE candidate_id=? AND decision='APPROVE'",(candidate_id,)).fetchone():
                _deny('MERGE_APPROVAL_OR_EVIDENCE_CHANGED')
            frozen,raw=self._evidence(evidence)
            try:
                receipt=json.loads(raw)
            except (ValueError,UnicodeDecodeError):
                _deny('MERGE_RECEIPT_REQUIRED')
            identity_fields=receipt.get('frozen') or {}
            if (identity_fields.get('candidate_id')!=candidate_id or identity_fields.get('authorization_id')!=authorization_id
                or identity_fields.get('candidate_head')!=head or receipt.get('actual_head')!=head
                or receipt.get('execution_status')!='SUCCEEDED' or receipt.get('exit_code')!=0):
                _deny('MERGE_RECEIPT_IDENTITY_OR_RESULT_MISMATCH')
            for source in ('operation','log'):
                self._evidence(receipt.get(source))
            if grant['spec'].get('postmerge_argv'):
                test=receipt.get('postmerge') or {}
                if not receipt.get('postmerge_required') or test.get('mechanical_result')!='PASS' or test.get('executed',0)<=0:
                    _deny('POSTMERGE_TESTS_REQUIRED')
                self._evidence(test)
            from workflow_integration_workspace import verify
            try:
                destination_valid=verify(self,grant['spec'],receipt,head)
            except (OSError,ValueError,subprocess.SubprocessError):
                destination_valid=False
            if not destination_valid:
                _deny('MERGED_WORKSPACE_CHANGED')
            identifier=_id('merge')
            db.execute('INSERT INTO merges VALUES(?,?,?,?,?,?)',(identifier,candidate_id,authorization_id,head,_json(frozen),self.clock()))
            event=self._event(db,'CANDIDATE_MERGED',{'merge_id':identifier,'candidate_id':candidate_id,'head':head,'authorization_id':authorization_id},run_id=candidate['run_id'],role='COORDINATOR',work_id=work['work_id'])
            return {'merge_id':identifier,'event_id':event,'head':head}
        return self._command('record_merge',command_id,[identity,candidate_id,authorization_id,head,evidence],operation)

    def record_service_result(self, action_id, result, evidence, *, service_identity,
                              command_id, supersedes=None):
        """Collect one detached service terminal without a model or business owner.

        The submitting adapter freezes a per-service credential hash in the
        action's authorization. A callback must present that exact credential;
        no role, current run, worker liveness or reviewer work is invented.
        """
        def operation(db):
            action=self._one(db,'external_actions','action_id',action_id)
            candidate=self._one(db,'candidates','candidate_id',action['candidate_id'])
            authorization=action['authorization']
            if (not isinstance(service_identity,dict) or not isinstance(service_identity.get('token'),str)
                or not authorization.get('service_id') or not authorization.get('service_token_sha256')
                or service_identity.get('service_id')!=authorization['service_id']
                or hashlib.sha256(service_identity['token'].encode()).hexdigest()!=authorization['service_token_sha256']):
                _deny('DETACHED_SERVICE_IDENTITY_UNVERIFIED')
            if action['state'] not in ('SUBMITTED','UNKNOWN'):
                _deny('EXTERNAL_ACTION_ALREADY_TERMINAL')
            if result not in ('PASS','FAIL'):
                _deny('SERVICE_TERMINAL_RESULT_REQUIRED')
            frozen,raw=self._evidence(evidence)
            try:
                service=json.loads(raw)
            except (ValueError,UnicodeDecodeError):
                _deny('SERVICE_RESULT_INCOMPLETE')
            if (service.get('action_id')!=action_id or service.get('candidate_id')!=candidate['candidate_id']
                or service.get('head')!=candidate['head'] or service.get('base')!=candidate['base']
                or service.get('ac_version')!=candidate['ac_version'] or service.get('run_id')!=candidate['run_id']
                or service.get('task_id')!=candidate['task_id'] or service.get('attempt')!=candidate['attempt']
                or service.get('service_id')!=authorization['service_id']
                or service.get('result')!=result or service.get('complete') is not True):
                _deny('SERVICE_RESULT_IDENTITY_OR_COVERAGE_MISMATCH')
            closed=supersedes or []
            if not isinstance(closed,list) or len(set(closed))!=len(closed):
                _deny('INVALID_FAILURE_CLOSURE')
            for identifier in closed:
                previous=self._one(db,'verifications','verification_id',identifier)
                old_candidate=self._one(db,'candidates','candidate_id',previous['candidate_id'])
                if (result!='PASS' or previous['result']!='FAIL' or previous['check_name']!=action['kind']
                    or old_candidate['run_id']!=candidate['run_id'] or old_candidate['task_id']!=candidate['task_id']
                    or old_candidate['attempt']>candidate['attempt']):
                    _deny('FAILURE_CLOSURE_SCOPE_MISMATCH')
                self._evidence(previous['evidence'])
            identifier=_id('verification')
            # COORDINATOR is the stable responsible role, not a new worker owner.
            db.execute('INSERT INTO verifications VALUES(?,?,?,?,?,?,?,?,?)',
                       (identifier,candidate['candidate_id'],action['kind'],'COORDINATOR',result,
                        _json(frozen),_json(closed),action_id,self.clock()))
            db.execute("UPDATE external_actions SET state='ENDED' WHERE action_id=?",(action_id,))
            event=self._event(db,'SERVICE_TERMINAL_RECORDED',{'action_id':action_id,'service_id':authorization['service_id'],
                               'candidate_id':candidate['candidate_id'],'verification_id':identifier,'result':result},run_id=candidate['run_id'])
            return {'verification_id':identifier,'event_id':event,'run_id':candidate['run_id'],'candidate_id':candidate['candidate_id']}
        # Store the credential digest rather than its secret in command metadata.
        credential={k:v for k,v in (service_identity or {}).items() if k!='token'}
        credential['token_sha256']=hashlib.sha256(str((service_identity or {}).get('token','')).encode()).hexdigest()
        return self._command('record_service_result',command_id,[action_id,result,evidence,credential,supersedes],operation)

    def record_service_artifact(self, action_id, evidence, *, service_identity, command_id):
        """Record detached execution/coverage facts; never manufacture a PASS."""
        def operation(db):
            action=self._one(db,'external_actions','action_id',action_id)
            candidate=self._one(db,'candidates','candidate_id',action['candidate_id'])
            authorization=action['authorization']
            if (not isinstance(service_identity,dict) or not isinstance(service_identity.get('token'),str)
                or not authorization.get('service_id') or not authorization.get('service_token_sha256')
                or service_identity.get('service_id')!=authorization['service_id']
                or hashlib.sha256(service_identity['token'].encode()).hexdigest()!=authorization['service_token_sha256']):
                _deny('DETACHED_SERVICE_IDENTITY_UNVERIFIED')
            if action['state'] not in ('SUBMITTED','UNKNOWN'):
                _deny('EXTERNAL_ACTION_ALREADY_TERMINAL')
            frozen,raw=self._evidence(evidence)
            try:
                terminal=json.loads(raw)
            except (ValueError,UnicodeDecodeError):
                _deny('SERVICE_RESULT_INCOMPLETE')
            for key in ('run_id','task_id','attempt','base','head','ac_version','candidate_id'):
                if terminal.get(key)!=candidate[key]:
                    _deny('SERVICE_RESULT_IDENTITY_OR_COVERAGE_MISMATCH')
            if (terminal.get('action_id')!=action_id or terminal.get('service_id')!=authorization['service_id']
                or terminal.get('state') not in ('READY_FOR_ADJUDICATION','FAILED','INCOMPLETE','CANCELLED')
                or terminal.get('service_stopped') is not True or not terminal.get('sources')):
                _deny('SERVICE_TERMINAL_FACTS_INCOMPLETE')
            for source in terminal['sources']:
                self._evidence(source)
            state='RESULT_READY' if terminal['state']=='READY_FOR_ADJUDICATION' else terminal['state']
            db.execute('UPDATE external_actions SET state=?,evidence=? WHERE action_id=?',(state,_json(frozen),action_id))
            event=self._event(db,'SERVICE_ARTIFACT_RECORDED',{'action_id':action_id,'candidate_id':candidate['candidate_id'],
                               'state':state,'evidence':frozen,'business_pass':False},run_id=candidate['run_id'],
                              role='COORDINATOR',notify=state in ('FAILED','INCOMPLETE','CANCELLED'))
            return {'action_id':action_id,'state':state,'event_id':event,'business_pass':False}
        credential={k:v for k,v in (service_identity or {}).items() if k!='token'}
        credential['token_sha256']=hashlib.sha256(str((service_identity or {}).get('token','')).encode()).hexdigest()
        return self._command('record_service_artifact',command_id,[action_id,evidence,credential],operation)

    def adjudicate_service(self, identity, action_id, result, summary, *, command_id, supersedes=None):
        """A current coordinator judges frozen service findings after collection."""
        def operation(db):
            work,_=self._identity(db,identity)
            action=self._one(db,'external_actions','action_id',action_id)
            candidate=self._one(db,'candidates','candidate_id',action['candidate_id'])
            if work['role']!='COORDINATOR' or work['run_id']!=candidate['run_id']:
                _deny('COORDINATOR_REQUIRED')
            if action['state']!='RESULT_READY' or candidate['state']!='CURRENT' or result not in ('PASS','FAIL') or not isinstance(summary,str) or not summary.strip():
                _deny('COMPLETE_SERVICE_FACTS_AND_SEMANTIC_DECISION_REQUIRED')
            frozen,raw=self._evidence(action['evidence'])
            terminal=json.loads(raw)
            for source in terminal['sources']:
                self._evidence(source)
            closed=supersedes or []
            if not isinstance(closed,list) or len(set(closed))!=len(closed):
                _deny('INVALID_FAILURE_CLOSURE')
            for identifier in closed:
                prior=self._one(db,'verifications','verification_id',identifier)
                old=self._one(db,'candidates','candidate_id',prior['candidate_id'])
                if (result!='PASS' or prior['result']!='FAIL' or prior['check_name']!=action['kind']
                    or old['run_id']!=candidate['run_id'] or old['task_id']!=candidate['task_id'] or old['attempt']>candidate['attempt']):
                    _deny('FAILURE_CLOSURE_SCOPE_MISMATCH')
                self._evidence(prior['evidence'])
            identifier=_id('verification')
            db.execute('INSERT INTO verifications VALUES(?,?,?,?,?,?,?,?,?)',
                       (identifier,candidate['candidate_id'],action['kind'],'COORDINATOR',result,
                        _json({**frozen,'adjudication_summary':summary}),_json(closed),action_id,self.clock()))
            db.execute("UPDATE external_actions SET state='ENDED' WHERE action_id=?",(action_id,))
            event=self._event(db,'SERVICE_ADJUDICATED',{'action_id':action_id,'candidate_id':candidate['candidate_id'],
                               'result':result,'summary':summary,'verification_id':identifier,'supersedes':closed},
                              run_id=work['run_id'],work_id=work['work_id'],role=work['role'])
            return {'verification_id':identifier,'event_id':event,'result':result}
        return self._command('adjudicate_service',command_id,[identity,action_id,result,summary,supersedes],operation)

    def reject_external_action(self, action_id, *, command_id, reason, identity=None):
        """Void an action that provably produced no external side effect.

        The serial OCR gate is closed by any SUBMITTED/UNKNOWN row, so a pure
        argument error must not stay UNKNOWN forever. This entry is only for
        pre-dispatch failures and verifies that itself: the service sidecar must
        exist and prove no dispatch: a versioned preflight refusal or a narrow
        legacy prepare failure, with no dispatch intent or native execution.
        An action with any real evidence stays UNKNOWN.
        """
        def operation(db):
            action=self._one(db,'external_actions','action_id',action_id)
            if identity is not None:
                work,_=self._identity(db,identity)
                candidate=self._one(db,'candidates','candidate_id',action['candidate_id'])
                if work['role']!='COORDINATOR' or work['run_id']!=candidate['run_id']:
                    _deny('COORDINATOR_REQUIRED')
            if action['state'] not in ('SUBMITTED','UNKNOWN'):
                _deny('EXTERNAL_ACTION_ALREADY_TERMINAL')
            if not reason:
                _deny('REJECTED_ACTION_REASON_REQUIRED')
            directory=self.path.parent/'services'/hashlib.sha256(action_id.encode()).hexdigest()
            sidecar=directory/'invocation.json'
            try:
                value=json.loads(sidecar.read_text())
            except (OSError,ValueError):
                _deny('REJECTION_REQUIRES_DISPATCH_MAPPING')
            if not isinstance(value,dict) or value.get('action_id')!=action_id:
                _deny('REJECTION_MAPPING_IDENTITY_MISMATCH')
            if value.get('reference') or action.get('evidence'):
                _deny('REJECTION_REQUIRES_NO_NATIVE_REFERENCE')
            from workflow_service_recovery import rejection_proof
            if not rejection_proof(value,directory):
                _deny('REJECTION_REQUIRES_ZERO_DISPATCH_PROOF')
            db.execute("UPDATE external_actions SET state='REJECTED' WHERE action_id=?",(action_id,))
            candidate=self._one(db,'candidates','candidate_id',action['candidate_id'])
            event=self._event(db,'EXTERNAL_ACTION_REJECTED',{'action_id':action_id,'reason':reason,
                               'candidate_id':candidate['candidate_id'],'business_pass':False},
                              run_id=candidate['run_id'],role='COORDINATOR',notify=True)
            return {'action_id':action_id,'state':'REJECTED','event_id':event,'business_pass':False}
        return self._command('reject_external_action',command_id,[action_id,reason,identity],operation)

    def mark_external_unknown(self, action_id, *, command_id, reason):
        def operation(db):
            action=self._one(db,'external_actions','action_id',action_id)
            if action['state']!='SUBMITTED':
                _deny('ACTION_NOT_SUBMITTED')
            if not reason:
                _deny('UNKNOWN_ACTION_REASON_REQUIRED')
            db.execute("UPDATE external_actions SET state='UNKNOWN' WHERE action_id=?",(action_id,))
            candidate=self._one(db,'candidates','candidate_id',action['candidate_id'])
            event=self._event(db,'EXTERNAL_ACTION_UNKNOWN',{'action_id':action_id,'reason':reason},run_id=candidate['run_id'])
            return {'action_id':action_id,'state':'UNKNOWN','event_id':event,'replay_allowed':False}
        return self._command('mark_external_unknown',command_id,[action_id,reason],operation)

    def publish_verification(self, identity, candidate_id, check, result, evidence, *, command_id, supersedes=None, action_id=None):
        def operation(db):
            work,execution = self._identity(db,identity,allow_drain=action_id is not None)
            candidate = self._one(db,'candidates','candidate_id',candidate_id)
            if work['run_id']!=candidate['run_id'] or result not in ('PASS','FAIL'):
                _deny('VERIFICATION_SCOPE_MISMATCH')
            if candidate['state']!='CURRENT':
                _deny('CANDIDATE_SUPERSEDED')
            self._candidate_evidence(candidate)
            frozen,raw = self._evidence(evidence)
            if check in ('independent_test','supplemental_mysql'):
                expected_kind='verification' if check=='independent_test' else 'supplemental_verification'
                if work['role']!='TESTER' or work['kind']!=expected_kind or work['candidate_id']!=candidate_id:
                    _deny('INDEPENDENT_TESTER_WORK_REQUIRED')
                if evidence.get('head')!=candidate['head']:
                    _deny('TEST_HEAD_MISMATCH')
                from delivery_evidence import parse_test_results, check_candidate_test_evidence
                stats=parse_test_results(frozen['path'],exit_code=evidence.get('exit_code'))
                if stats['tests']==0 or stats['executed']==0:
                    _deny('ZERO_EXECUTED_TESTS')
                if check=='supplemental_mysql' and (stats['skipped'] or frozen.get('verification_profile')!='mysql_migration'):
                    _deny('REAL_MYSQL_EXECUTION_REQUIRED')
                if stats['mechanical_result']=='UNVERIFIED':
                    _deny('MACHINE_TEST_RESULT_REQUIRED',reasons=stats['reasons'])
                if result=='PASS' and stats['mechanical_result']!='PASS':
                    _deny('TEST_RESULT_NOT_PASS')
                if 'runner_evidence' in frozen:
                    checked=check_candidate_test_evidence(frozen,candidate)
                    if not checked['valid']:
                        _deny('RUNNER_EVIDENCE_UNVERIFIED',reasons=checked['reasons'])
                frozen['test_counts']={key:stats[key] for key in ('tests','passed','failures','errors','skipped','executed')}
            else:
                if work['role']!='COORDINATOR' or check not in ('ocr','strong_review') or not action_id:
                    _deny('AUTHORIZED_SERVICE_RESULT_REQUIRED')
                action = self._one(db,'external_actions','action_id',action_id)
                if action['candidate_id']!=candidate_id or action['kind']!=check or action['state']!='SUBMITTED':
                    _deny('SERVICE_ACTION_IDENTITY_MISMATCH')
                try:
                    service=json.loads(raw)
                except (ValueError,UnicodeDecodeError):
                    _deny('SERVICE_RESULT_INCOMPLETE')
                if (service.get('action_id')!=action_id or service.get('candidate_id')!=candidate_id
                    or service.get('head')!=candidate['head'] or service.get('task_id')!=candidate['task_id']
                    or service.get('result')!=result or service.get('complete') is not True):
                    _deny('SERVICE_RESULT_IDENTITY_OR_COVERAGE_MISMATCH')
                db.execute("UPDATE external_actions SET state='ENDED' WHERE action_id=?",(action_id,))
            closed = supersedes or []
            if not isinstance(closed,list) or len(set(closed))!=len(closed):
                _deny('INVALID_FAILURE_CLOSURE')
            for identifier in closed:
                prior = self._one(db,'verifications','verification_id',identifier)
                earlier = self._one(db,'candidates','candidate_id',prior['candidate_id'])
                from workflow_supplemental import permits_closure
                supplemental_closure=(check=='supplemental_mysql' and permits_closure(db,work,prior,earlier,candidate))
                if (prior['result']!='FAIL' or prior['check_name']!=check or earlier['run_id']!=candidate['run_id']
                    or (not supplemental_closure and (earlier['task_id']!=candidate['task_id'] or earlier['attempt']>candidate['attempt']))
                    or result!='PASS'):
                    _deny('FAILURE_CLOSURE_SCOPE_MISMATCH')
                self._evidence(prior['evidence'])
            if check == 'independent_test' and result == 'PASS':
                history = [_row(row) for row in db.execute(
                    'SELECT v.* FROM verifications v JOIN candidates c USING(candidate_id) '
                    'WHERE c.run_id=? AND c.task_id=? AND c.attempt<=? AND v.check_name=?',
                    (candidate['run_id'], candidate['task_id'], candidate['attempt'], check))]
                already_closed = {key for row in history if row['result'] == 'PASS' for key in row['supersedes']}
                unresolved = {row['verification_id'] for row in history if row['result'] == 'FAIL'} - already_closed
                if not unresolved <= set(closed):
                    _deny('FAILURE_CLOSURE_REQUIRED', verification_ids=sorted(unresolved-set(closed)))
            identifier = _id('verification')
            db.execute('INSERT INTO verifications VALUES(?,?,?,?,?,?,?,?,?)',
                       (identifier,candidate_id,check,work['role'],result,_json(frozen),_json(closed),action_id,self.clock()))
            if check in ('independent_test','supplemental_mysql'):
                db.execute("UPDATE works SET state='ENDED',ended_at=? WHERE work_id=?",(self.clock(),work['work_id']))
                db.execute('UPDATE role_current SET current_work_id=NULL WHERE role=?',(work['role'],))
                db.execute("UPDATE executions SET state='DRAINING' WHERE execution_id=?",(execution['execution_id'],))
            event=self._event(db,'VERIFICATION_PUBLISHED',{'verification_id':identifier,'candidate_id':candidate_id,'check':check,'result':result,'supersedes':closed},run_id=candidate['run_id'],work_id=work['work_id'],role=work['role'])
            return {'verification_id':identifier,'event_id':event}
        return self._command('publish_verification',command_id,[identity,candidate_id,check,result,evidence,supersedes,action_id],operation)

    def close_verification(self, identity, verification_id, supersedes, *, command_id):
        """Reuse frozen same-candidate TESTER evidence for an explicit closure."""
        with self._connection() as db:
            previous = self._one(db, 'verifications', 'verification_id', verification_id)
            if (previous['check_name'] != 'independent_test' or previous['role'] != 'TESTER'
                    or previous['result'] != 'PASS' or not supersedes):
                raise ValueError('EXISTING_INDEPENDENT_PASS_AND_EXPLICIT_CLOSURE_REQUIRED')
        return self.publish_verification(identity, previous['candidate_id'], 'independent_test', 'PASS',
            previous['evidence'], supersedes=supersedes, command_id=command_id)

    def acceptance(self, candidate_id):
        with self._connection() as db:
            try:
                return self._acceptance(db,candidate_id)
            except _Denied as exc:
                return {'ready':False,'reasons':[exc.code]}

    def _acceptance(self, db, candidate_id):
        candidate=self._one(db,'candidates','candidate_id',candidate_id)
        task=_row(db.execute('SELECT * FROM tasks WHERE run_id=? AND task_id=?',(candidate['run_id'],candidate['task_id'])).fetchone())
        reasons=[]
        if candidate['state']!='CURRENT' or candidate['attempt']!=task['attempt'] or task['phase']!='REVIEW':
            reasons.append('CANDIDATE_NOT_CURRENT')
        dependency_gate=_dependency_gate(db,task,'acceptance')
        if not dependency_gate['ready']:
            reasons.append('ACCEPTANCE_DEPENDENCIES_NOT_COMPLETE')
        self._candidate_evidence(candidate)
        all_results=[_row(r) for r in db.execute('SELECT v.* FROM verifications v JOIN candidates c USING(candidate_id) WHERE c.run_id=? AND c.task_id=?',(candidate['run_id'],candidate['task_id']))]
        for value in all_results:
            self._evidence(value['evidence'])
            if value['check_name']=='independent_test' and 'runner_evidence' in value['evidence']:
                from delivery_evidence import check_candidate_test_evidence
                verified_candidate=self._one(db,'candidates','candidate_id',value['candidate_id'])
                checked=check_candidate_test_evidence(value['evidence'],verified_candidate)
                if not checked['valid']:
                    _deny('RUNNER_EVIDENCE_CHANGED',reasons=checked['reasons'])
        current=[r for r in all_results if r['candidate_id']==candidate_id]
        cross=[_row(r) for r in db.execute("SELECT v.* FROM verifications v JOIN candidates c USING(candidate_id) "
            "WHERE c.run_id=? AND v.check_name='supplemental_mysql' AND v.result='PASS'",(candidate['run_id'],))]
        for value in cross:
            self._evidence(value['evidence'])
        closed={v for r in [*all_results,*cross] if r['result']=='PASS' for v in r['supersedes']}
        failures=[r['verification_id'] for r in all_results if r['result']=='FAIL' and r['verification_id'] not in closed]
        # Keep the historical failure relation explicit at the decision
        # boundary.  A coordinator still has to choose and submit the exact
        # supersedes list; this is only a read-only map that prevents a
        # verified candidate from being re-reviewed because a prior failure
        # was forgotten in the adjudication payload.
        failure_closure_preview=[]
        for failure_id in failures:
            failure=next(row for row in all_results if row['verification_id']==failure_id)
            source_candidate=self._one(db,'candidates','candidate_id',failure['candidate_id'])
            evidence=failure.get('evidence') or {}
            failure_closure_preview.append({
                'verification_id': failure_id,
                'check_name': failure['check_name'],
                'source_candidate_id': failure['candidate_id'],
                'source_task_id': source_candidate['task_id'],
                'source_attempt': source_candidate['attempt'],
                'source_head': source_candidate['head'],
                'current_candidate_id': candidate_id,
                'current_attempt': candidate['attempt'],
                'closure_eligible': (source_candidate['run_id']==candidate['run_id']
                                     and source_candidate['task_id']==candidate['task_id']
                                     and source_candidate['attempt']<=candidate['attempt']),
                'evidence': {key: evidence[key] for key in ('path','sha256') if key in evidence},
            })
        for check in task['required_checks']:
            if not any(r['result']=='PASS' and r['check_name']==check for r in current):
                reasons.append('REQUIRED_CHECK_MISSING:'+check)
        if failures:
            reasons.append('UNRESOLVED_FAILURE')
        return {'ready':not reasons,'candidate_id':candidate_id,'reasons':reasons,'open_failure_ids':failures,
                'failure_closure_preview':failure_closure_preview,
                'required_checks':task['required_checks'],'dependency_gate':dependency_gate}

    def dependency_readiness(self, run_id, task_id, stage):
        if stage not in ('development_start','integration','acceptance'):
            return {'ready':False,'stage':stage,'missing':[],'code':'UNKNOWN_DEPENDENCY_STAGE'}
        with self._connection() as db:
            try:
                task=_row(db.execute('SELECT * FROM tasks WHERE run_id=? AND task_id=?',
                                     (run_id,task_id)).fetchone())
                if not task:
                    return {'ready':False,'stage':stage,'missing':[],'code':'TASK_NOT_FOUND'}
                return _dependency_gate(db,task,stage)
            except (OSError,subprocess.SubprocessError) as exc:
                return {'ready':False,'stage':stage,'missing':[],'code':'DEPENDENCY_EVIDENCE_UNVERIFIED',
                        'reason':type(exc).__name__}

    def stage_dependency_ids(self, run_id, task_id, stage):
        if stage not in ('development_start','integration','acceptance'):
            raise ValueError('UNKNOWN_DEPENDENCY_STAGE')
        with self._connection() as db:
            task=_row(db.execute('SELECT * FROM tasks WHERE run_id=? AND task_id=?',
                                 (run_id,task_id)).fetchone())
            if not task:
                raise ValueError('TASK_NOT_FOUND')
            return _stage_dependencies(task,stage)

    def _rework(self, db, candidate_id, reason):
        candidate=self._one(db,'candidates','candidate_id',candidate_id)
        if candidate['state']!='CURRENT':
            _deny('CANDIDATE_NOT_CURRENT')
        task=_row(db.execute('SELECT * FROM tasks WHERE run_id=? AND task_id=?',(candidate['run_id'],candidate['task_id'])).fetchone())
        if task['phase']!='REVIEW' or task['attempt']!=candidate['attempt']:
            _deny('TASK_NOT_AWAITING_DECISION')
        unfinished=[_row(row) for row in db.execute("SELECT * FROM works WHERE run_id=? AND task_id=? AND kind!='coordination' AND (state!='ENDED' OR EXISTS (SELECT 1 FROM executions e WHERE e.work_id=works.work_id AND e.state!='ENDED'))",(candidate['run_id'],candidate['task_id']))]
        for work in unfinished:
            current=self._one(db,'role_current','role',work['role'])
            if (work['kind']!='verification' or work['candidate_id']!=candidate_id
                    or work['attempt']!=candidate['attempt'] or work['state']!='QUEUED'
                    or work['active_execution_id'] or current['writer_execution_id']
                    or db.execute("SELECT 1 FROM executions WHERE work_id=? AND state!='ENDED'",(work['work_id'],)).fetchone()):
                _deny('VERIFICATION_WORK_STILL_OPEN')
        # Explicit REWORK retires an idle verification of the old candidate.
        # It neither marks a check PASS nor deletes FAIL / UNKNOWN evidence.
        retired=[]
        for work in unfinished:
            db.execute("UPDATE works SET state='ENDED',ended_at=? WHERE work_id=?",(self.clock(),work['work_id']))
            db.execute('UPDATE role_current SET current_work_id=NULL WHERE role=? AND current_work_id=?',
                       (work['role'],work['work_id']))
            retired.append(work['work_id'])
        attempt=task['attempt']+1
        db.execute("UPDATE candidates SET state='SUPERSEDED' WHERE candidate_id=?",(candidate_id,))
        db.execute("UPDATE attempts SET state='SUPERSEDED' WHERE run_id=? AND task_id=? AND attempt=?",(candidate['run_id'],candidate['task_id'],task['attempt']))
        db.execute("INSERT INTO attempts VALUES(?,?,?,'CURRENT',?,?)",(candidate['run_id'],candidate['task_id'],attempt,candidate_id,reason))
        db.execute("UPDATE tasks SET phase='PENDING',attempt=? WHERE run_id=? AND task_id=?",(attempt,candidate['run_id'],candidate['task_id']))
        return {'task_id':candidate['task_id'],'attempt':attempt,'previous_candidate_id':candidate_id,
                'retired_verification_work_ids':retired}

    def decide(self, identity, candidate_id, decision, *, command_id, evidence=None):
        def operation(db):
            final_evidence=evidence
            work,_=self._identity(db,identity)
            candidate=self._one(db,'candidates','candidate_id',candidate_id)
            if work['role']!='COORDINATOR' or work['run_id']!=candidate['run_id']:
                _deny('COORDINATOR_REQUIRED')
            existing=db.execute(
                'SELECT * FROM decisions WHERE candidate_id=? AND decision=? '
                'ORDER BY created_at,decision_id LIMIT 1',(candidate_id,decision)).fetchone()
            if existing:
                # Replay records a historical fact, never a fresh acceptance or
                # authority grant. Conflicting evidence must not be overwritten.
                if evidence is not None and _json(evidence)!=existing['evidence']:
                    _deny('DECISION_EVIDENCE_CONFLICT',decision_id=existing['decision_id'])
                return {'status':'DUPLICATE','decision_id':existing['decision_id'],
                        'candidate_id':candidate_id,'recorded_decision':decision,
                        'candidate_state':candidate['state'],'state':'RECORDED',
                        'acceptance_revalidated':False}
            if decision=='REWORK':
                from workflow_verification_lifecycle import request_for_candidate
                pending_work=request_for_candidate(db,self,candidate,'COORDINATOR_REWORK_REQUIRES_NEW_FIXED_CANDIDATE')
                if any(w['active_execution_id'] or self._one(db,'role_current','role',w['role'])['writer_execution_id']
                       or db.execute("SELECT 1 FROM executions WHERE work_id=? AND state!='ENDED'",(w['work_id'],)).fetchone()
                       for w in pending_work):
                    return {'state':'WAITING_FOR_VERIFICATION_SAFE_EXIT','requires_user_approval':False,
                            'retiring_work_ids':[w['work_id'] for w in pending_work],
                            'next_action':'TESTER safe_release后结束执行；writer实际核退后用新command_id接续REWORK，不要求用户授权。'}
                result=self._rework(db,candidate_id,(evidence or {}).get('reason','明确返修裁决'))
            elif decision in ('APPROVE','COMPLETE'):
                gate=self._acceptance(db,candidate_id)
                if not gate['ready']:
                    _deny('ACCEPTANCE_GATES_NOT_CLEAR',gates=gate)
                if decision=='COMPLETE':
                    task=_row(db.execute('SELECT * FROM tasks WHERE run_id=? AND task_id=?',
                                         (candidate['run_id'],candidate['task_id'])).fetchone())
                    completion_gate=_dependency_gate(db,task,'acceptance')
                    if not completion_gate['ready']:
                        _deny('ACCEPTANCE_DEPENDENCIES_NOT_COMPLETE',gate=completion_gate)
                    approved=db.execute("SELECT 1 FROM decisions WHERE candidate_id=? AND decision='APPROVE'",(candidate_id,)).fetchone()
                    if not approved:
                        _deny('APPROVAL_REQUIRED_BEFORE_COMPLETION')
                    if not isinstance(evidence,dict) or not evidence.get('delivery'):
                        _deny('FINAL_DELIVERY_EVIDENCE_REQUIRED')
                    grants=[_row(r) for r in db.execute("SELECT * FROM authorizations WHERE kind='merge'")]
                    merge_required=any(all(g['spec'].get(key)==candidate[key] for key in ('run_id','task_id','attempt','base','ac_version')) for g in grants)
                    if db.execute("SELECT 1 FROM authorizations WHERE kind='merge_policy' AND authorization_id=?",
                                  (candidate['run_id']+'-merge-policy',)).fetchone():
                        merge_required=True
                    if db.execute("SELECT 1 FROM authorizations WHERE kind='development_policy' "
                                  "AND json_extract(spec,'$.run_id')=?",(candidate['run_id'],)).fetchone():
                        merge_required=True
                    if db.execute("SELECT 1 FROM authorizations WHERE kind='merge_task_policy' "
                                  "AND json_extract(spec,'$.run_id')=? AND json_extract(spec,'$.task_id')=?",
                                  (candidate['run_id'],candidate['task_id'])).fetchone():
                        merge_required=True
                    if merge_required:
                        merged=[_row(r) for r in db.execute('SELECT * FROM merges WHERE candidate_id=?',(candidate_id,))]
                        if not merged:
                            _deny('AUTHORIZED_MERGE_AND_POSTMERGE_EVIDENCE_REQUIRED')
                        for row in merged:
                            frozen_merge,raw_merge=self._evidence(row['evidence'])
                            receipt=json.loads(raw_merge)
                            for source in ('operation','log'):
                                self._evidence(receipt[source])
                            if receipt.get('postmerge_required'):
                                self._evidence(receipt['postmerge'])
                            grant=self._one(db,'authorizations','authorization_id',row['authorization_id'])
                            from workflow_integration_workspace import verify
                            try:
                                destination_valid=verify(self,grant['spec'],receipt,candidate['head'])
                            except (OSError,ValueError,subprocess.SubprocessError):
                                destination_valid=False
                            if not destination_valid:
                                _deny('FINAL_MERGED_WORKSPACE_CHANGED')
                    frozen,_=self._evidence(evidence['delivery'])
                    if evidence.get('merged_head'):
                        if not _FULL_COMMIT.fullmatch(str(evidence['merged_head'])) or self._git(candidate['repo'],'rev-parse','--verify',evidence['merged_head']+'^{commit}')!=evidence['merged_head']:
                            _deny('MERGE_COMMIT_UNVERIFIED')
                    db.execute("UPDATE tasks SET phase='COMPLETED' WHERE run_id=? AND task_id=?",(candidate['run_id'],candidate['task_id']))
                    db.execute("UPDATE attempts SET state='COMPLETED' WHERE run_id=? AND task_id=? AND attempt=?",(candidate['run_id'],candidate['task_id'],candidate['attempt']))
                    final_evidence={**evidence,'delivery':frozen}
                result={'candidate_id':candidate_id,'decision':decision}
            else:
                _deny('INVALID_DECISION')
            identifier=_id('decision')
            db.execute('INSERT INTO decisions VALUES(?,?,?,?,?)',(identifier,candidate_id,decision,_json(final_evidence or {}),self.clock()))
            event=self._event(db,'CANDIDATE_DECIDED',{**result,'decision_id':identifier,'decision':decision},run_id=work['run_id'],work_id=work['work_id'],role=work['role'],notify=decision=='COMPLETE')
            return {**result,'decision_id':identifier,'event_id':event}
        return self._command('decide',command_id,[identity,candidate_id,decision,evidence],operation)

    def control_request(self, action, target, provenance, *, command_id):
        def operation(db):
            if action not in CONTROL_ACTIONS or not isinstance(target,dict) or set(target)-{'run_id','role','task_id','candidate_id'} or not target.get('run_id'):
                _deny('INVALID_CONTROL_TARGET')
            run=self._one(db,'runs','run_id',target['run_id'])
            if target.get('role') is not None and target['role'] not in ROLES:
                _deny('INVALID_CONTROL_ROLE')
            if target.get('task_id') and target['task_id'] not in run['task_ids']:
                _deny('CONTROL_TASK_OUTSIDE_SCOPE')
            if not isinstance(provenance,dict) or provenance.get('authenticated') is not True or not provenance.get('message_id') or not provenance.get('user_id'):
                _deny('CURRENT_TRUSTED_CONTROL_GRANT_REQUIRED')
            if provenance.get('current_full') is not True:
                from workflow_intake import scoped_control
                from workflow_runtime import _control_source
                if not scoped_control(provenance,action):
                    _deny('CURRENT_TRUSTED_CONTROL_GRANT_REQUIRED')
                _control_source(self,'COORDINATOR',action,target,provenance['primary_text'])
            if action=='rework':
                candidate=self._one(db,'candidates','candidate_id',target.get('candidate_id'))
                if candidate['run_id']!=run['run_id'] or (target.get('task_id') and candidate['task_id']!=target['task_id']):
                    _deny('CONTROL_CANDIDATE_SCOPE_MISMATCH')
            identifier=_id('control')
            db.execute('INSERT INTO controls VALUES(?,?,?,?,?,?,?)',(identifier,action,_json(target),_json(provenance),'ACCEPTED',None,self.clock()))
            event=self._event(db,'CONTROL_ACCEPTED',{'request_id':identifier,'action':action,'target':target},run_id=run['run_id'])
            return {'request_id':identifier,'event_id':event,'state':'ACCEPTED'}
        return self._command('control_request',command_id,[action,target,provenance],operation)

    def apply_control(self, request_id, *, command_id):
        def operation(db):
            request=self._one(db,'controls','request_id',request_id)
            if request['state']!='ACCEPTED':
                return {'request_id':request_id,'state':request['state'],'result':request['result']}
            target=request['target']; action=request['action']; run_id=target['run_id']
            run=self._one(db,'runs','run_id',run_id)
            if action=='select_run':
                result=self._select(db,run_id)
            elif action in ('pause','resume','continue'):
                # continue schedules accepted work; it does not implicitly resume pause.
                if action=='continue':
                    result={'run_id':run_id,'paused':run['control']=='PAUSED'}
                elif target.get('task_id'):
                    task=db.execute('SELECT * FROM tasks WHERE run_id=? AND task_id=?',(run_id,target['task_id'])).fetchone()
                    if not task:
                        _deny('CONTROL_TASK_NOT_PUBLISHED')
                    if action=='pause':
                        if task['unresolved_reason'] and task['unresolved_reason']!='CONTROL_PAUSED':
                            _deny('TASK_ALREADY_RECONCILIATION_GATED')
                        db.execute("UPDATE tasks SET unresolved_reason='CONTROL_PAUSED' WHERE run_id=? AND task_id=?",(run_id,target['task_id']))
                    else:
                        db.execute("UPDATE tasks SET unresolved_reason=NULL WHERE run_id=? AND task_id=? AND unresolved_reason='CONTROL_PAUSED'",(run_id,target['task_id']))
                    result={'run_id':run_id,'task_id':target['task_id'],'control':'PAUSED' if action=='pause' else 'ACTIVE'}
                elif target.get('role'):
                    db.execute('UPDATE role_current SET control=? WHERE role=?',('PAUSED' if action=='pause' else 'ACTIVE',target['role']))
                    result={'role':target['role'],'control':'PAUSED' if action=='pause' else 'ACTIVE'}
                else:
                    db.execute('UPDATE runs SET control=? WHERE run_id=?',('PAUSED' if action=='pause' else 'ACTIVE',run_id))
                    result={'run_id':run_id,'control':'PAUSED' if action=='pause' else 'ACTIVE'}
            elif action=='rework':
                result=self._rework(db,target['candidate_id'],'用户明确返修控制请求 '+request_id)
            else:
                if target.get('task_id'):
                    task=target['task_id']
                    db.execute("UPDATE tasks SET phase='CANCELLED' WHERE run_id=? AND task_id=? AND phase!='COMPLETED'",(run_id,task))
                    db.execute("UPDATE attempts SET state='CANCELLED' WHERE run_id=? AND task_id=? AND state='CURRENT'",(run_id,task))
                    result={'run_id':run_id,'task_id':task,'cancelled':True}
                else:
                    db.execute("UPDATE runs SET control='CANCELLED' WHERE run_id=?",(run_id,))
                    result={'run_id':run_id,'cancelled':True}
            db.execute("UPDATE controls SET state='APPLIED',result=? WHERE request_id=?",(_json(result),request_id))
            event=self._event(db,'CONTROL_APPLIED',{'request_id':request_id,'action':action,'result':result},run_id=run_id)
            return {'request_id':request_id,'state':'APPLIED','result':result,'event_id':event}
        return self._command('apply_control',command_id,[request_id],operation)

    def release_work(self, identity, *, command_id, reason):
        """Explicit safe-point release for cancelled/old-run coordination work."""
        def operation(db):
            work,execution=self._identity(db,identity,allow_drain=True)
            run=self._one(db,'runs','run_id',work['run_id'])
            task=db.execute('SELECT phase FROM tasks WHERE run_id=? AND task_id=?',(work['run_id'],work['task_id'])).fetchone() if work['task_id'] else None
            from workflow_verification_lifecycle import pending
            retirement_requested=(work['kind'] in ('verification','supplemental_verification')
                                  and bool(pending(db,work['work_id'])))
            if work['kind']=='integration_verification':
                from workflow_completion import target_current
                retirement_requested=not target_current(self,self.snapshot(),work)
            if run['control'] not in ('DRAINING','CANCELLED') and work['kind']!='coordination' and not retirement_requested and (not task or task['phase']!='CANCELLED'):
                _deny('DELIVERY_OR_CANCEL_REQUIRED')
            if not reason:
                _deny('SAFE_RELEASE_REASON_REQUIRED')
            db.execute("UPDATE works SET state='ENDED',ended_at=? WHERE work_id=?",(self.clock(),work['work_id']))
            db.execute('UPDATE role_current SET current_work_id=NULL WHERE role=?',(work['role'],))
            db.execute("UPDATE executions SET state='DRAINING' WHERE execution_id=?",(execution['execution_id'],))
            event=self._event(db,'WORK_RELEASED',{'reason':reason,'writer_clearance_required':True},run_id=work['run_id'],work_id=work['work_id'],role=work['role'])
            return {'event_id':event,'writer_clearance_required':True}
        return self._command('release_work',command_id,[identity,reason],operation)

    def notification(self, notification_id):
        with self._connection() as db:
            row=db.execute('SELECT * FROM outbox WHERE notification_id=?',(notification_id,)).fetchone()
            return _row(row) if row else None

    def outbox_items(self, *, include_terminal=False, at=None, kinds=None, states=None, limit=None):
        with self._connection() as db:
            sql,conditions,params='SELECT * FROM outbox',[],[]
            wanted=states if states is not None else (None if include_terminal else ('PENDING','SENDING','UNKNOWN','SUPPRESSED'))
            for column,values in (('state',wanted),('kind',kinds)):
                if values is not None:
                    if not values or any(not isinstance(value,str) for value in values):
                        raise ValueError('INVALID_OUTBOX_FILTER')
                    conditions.append(column+' IN ('+','.join('?' for _ in values)+')')
                    params.extend(values)
            if conditions: sql+=' WHERE '+' AND '.join(conditions)
            sql+=' ORDER BY event_id,notification_id'
            if limit is not None:
                if type(limit) is not int or limit<1: raise ValueError('INVALID_OUTBOX_LIMIT')
                sql+=' LIMIT ?'; params.append(limit)
            return [_row(r) for r in db.execute(sql,params)]

    def queue_notification(self, kind, payload, *, command_id, source_event_id,
                           run_id=None, role=None, notification_id=None):
        """Queue a read-only report against existing facts, without a business event."""
        def operation(db):
            event=self._one(db,'events','event_id',source_event_id)
            if role is not None and role not in ROLES:
                _deny('INVALID_NOTIFICATION_ROLE')
            if run_id is not None and event['run_id'] not in (None,run_id):
                _deny('NOTIFICATION_SOURCE_SCOPE_MISMATCH')
            if not isinstance(payload,dict) or not kind:
                _deny('INVALID_NOTIFICATION_PAYLOAD')
            identifier=notification_id or _id('notification')
            local=datetime.fromtimestamp(self.clock(),ZoneInfo('Asia/Shanghai'))
            state='SUPPRESSED' if local.hour>=21 or local.hour<8 else 'PENDING'
            db.execute('INSERT INTO outbox VALUES(?,?,?,?,?,?,?,?,?)',
                       (identifier,source_event_id,kind,role,run_id,_json(payload),state,None,self.clock()))
            return {'notification_id':identifier,'state':state}
        return self._command('queue_notification',command_id,[kind,payload,source_event_id,run_id,role,notification_id],operation)

    def claim_delivery(self, notification_id, *, command_id):
        def operation(db):
            value=self._one(db,'outbox','notification_id',notification_id)
            if value['state']!='PENDING':
                _deny('DELIVERY_NOT_PENDING',state=value['state'])
            local=datetime.fromtimestamp(self.clock(),ZoneInfo('Asia/Shanghai'))
            if local.hour>=21 or local.hour<8:
                db.execute("UPDATE outbox SET state='SUPPRESSED' WHERE notification_id=?",(notification_id,))
                return {'notification_id':notification_id,'state':'SUPPRESSED'}
            db.execute("UPDATE outbox SET state='SENDING' WHERE notification_id=?",(notification_id,))
            return {'notification_id':notification_id,'state':'SENDING','payload':value['payload']}
        return self._command('claim_delivery',command_id,[notification_id],operation)

    def ack_delivery(self, notification_id, state, *, command_id, receipt=None):
        def operation(db):
            value=self._one(db,'outbox','notification_id',notification_id)
            if state not in ('SENT','UNKNOWN','FAILED','SUPPRESSED'):
                _deny('INVALID_DELIVERY_STATE')
            if value['state'] not in ('SENDING','UNKNOWN'):
                _deny('DELIVERY_NOT_IN_FLIGHT')
            if state=='SENT' and not receipt:
                _deny('DELIVERY_RECEIPT_REQUIRED')
            db.execute('UPDATE outbox SET state=?,receipt=? WHERE notification_id=?',(state,_json(receipt) if receipt else None,notification_id))
            return {'notification_id':notification_id,'state':state}
        return self._command('ack_delivery',command_id,[notification_id,state,receipt],operation)

    def export_state(self):
        """A consistent final high-water export; callers persist and hash its bytes."""
        result=self.snapshot()
        return {'schema_version':SCHEMA_VERSION,'state':result,'sha256':_hash(result)}

    def freeze_admission(self, reason, *, command_id):
        def operation(db):
            if not isinstance(reason,str) or not reason:
                _deny('MIGRATION_FREEZE_REASON_REQUIRED')
            high=db.execute('SELECT COALESCE(MAX(event_id),0) FROM events').fetchone()[0]
            db.execute('UPDATE authority SET admission_open=0,freeze_reason=? WHERE singleton=1',(reason,))
            event=self._event(db,'ADMISSION_FROZEN',{'reason':reason,'previous_high_water':high})
            return {**self._authority(db),'event_id':event,'previous_high_water':high}
        return self._command('freeze_admission',command_id,[reason],operation)

    def open_admission(self, *, command_id, expected_generation, migration_manifest):
        def operation(db):
            authority=self._authority(db)
            if authority['generation']!=expected_generation:
                _deny('STALE_MODE_GENERATION')
            if not isinstance(migration_manifest,dict) or not migration_manifest.get('validated'):
                _deny('VALIDATED_MIGRATION_MANIFEST_REQUIRED')
            if db.execute("SELECT 1 FROM executions WHERE state IN ('ACTIVE','DRAINING')").fetchone():
                _deny('WRITER_NOT_CLEARED',unknown=True)
            db.execute('UPDATE authority SET admission_open=1,freeze_reason=NULL,migration_manifest=? WHERE singleton=1',(_json(migration_manifest),))
            event=self._event(db,'ADMISSION_OPENED',{'manifest':migration_manifest})
            return {**self._authority(db),'event_id':event}
        return self._command('open_admission',command_id,[expected_generation,migration_manifest],operation)

    def stage_import(self, manifest, proposals, *, command_id):
        """Freeze import proposals only; unresolved history never acquires work."""
        def operation(db):
            if self._authority(db)['mode']!='STAGED':
                _deny('IMPORT_REQUIRES_STAGED_MODE')
            if db.execute("SELECT 1 FROM executions WHERE state IN ('ACTIVE','DRAINING')").fetchone():
                _deny('WRITER_NOT_CLEARED',unknown=True)
            if not isinstance(manifest,dict) or not isinstance(proposals,list):
                _deny('IMPORT_MANIFEST_AND_PROPOSALS_REQUIRED')
            for proposal in proposals:
                if (not isinstance(proposal,dict) or not proposal.get('source')
                    or not re.fullmatch(r'[0-9a-f]{64}',str(proposal.get('source_sha256','')))
                    or not isinstance(proposal.get('identity_fields'),dict) or 'raw_source' not in proposal):
                    _deny('LOSSLESS_IMPORT_IDENTITY_REQUIRED')
            identifier=_id('import')
            frozen={'source_manifest':manifest,'proposals':proposals,'proposal_sha256':_hash(proposals),
                    'state':'PROPOSED','phase':None,'schedulable':False}
            db.execute('INSERT INTO imports VALUES(?,?,?)',(identifier,_json(frozen),self.clock()))
            event=self._event(db,'IMPORT_PROPOSED',{'import_id':identifier,'proposal_count':len(proposals),'schedulable':False})
            return {'import_id':identifier,'event_id':event,'proposal_count':len(proposals),'schedulable':False}
        return self._command('stage_import',command_id,[manifest,proposals],operation)
