"""Bounded read-only follow-up; persist counts/identifiers, never message prose."""
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
HOME = Path('/home/wang/.hermes')
REPO = Path('/home/wang/work/hermes-role-runtime')
ZONE = ZoneInfo('Asia/Shanghai')
START = datetime.fromisoformat('2026-10-30T17:29:59+08:00').timestamp()
MECHANICAL = {'EXECUTION_BOUND', 'EXECUTION_ENDED', 'EXECUTION_OUTCOME_OBSERVED',
              'TOOL_EXECUTION_REGISTERED', 'WORK_CLAIMED', 'CHECKPOINT_SAVED'}


def stamp(value):
    return datetime.fromtimestamp(value, ZONE).isoformat(timespec='seconds')


def read(path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def connect(path):
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=3)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    deadline = time.monotonic() + 20
    db.set_progress_handler(lambda: time.monotonic() > deadline, 10000)
    db.execute('BEGIN')
    return db


def main():
    cutoff = time.time()
    dbpath = HOME / 'role-pool.runtime/business.sqlite3'
    db = connect(dbpath)
    authority = dict(db.execute('SELECT mode,generation,current_run_id,admission_open FROM authority').fetchone())
    run = authority['current_run_id']
    high = db.execute('SELECT MAX(event_id) FROM events').fetchone()[0]
    tasks = [dict(r) for r in db.execute('SELECT task_id,role,phase,attempt FROM tasks WHERE run_id=?', (run,))]
    works = []
    for row in db.execute("SELECT work_id,task_id,attempt,role,kind,state,processed_through,issued_through,"
                          "active_execution_id,checkpoint FROM works WHERE run_id=? AND state!='ENDED'", (run,)):
        item = dict(row)
        checkpoint = json.loads(item.pop('checkpoint') or '{}')
        item['work_memory_present'] = 'work_memory' in checkpoint
        groups = Counter({r['kind']: r['n'] for r in db.execute('SELECT kind,COUNT(*) n FROM events '
            'WHERE event_id>? AND event_id<=? AND (run_id=? OR run_id IS NULL) GROUP BY kind',
            (item['processed_through'], high, run))})
        item['unprocessed_raw_events'] = sum(groups.values())
        item['unprocessed_nonmechanical_events'] = sum(v for k, v in groups.items() if k not in MECHANICAL)
        item['unprocessed_top_kinds'] = groups.most_common(6)
        works.append(item)
    events = []
    for row in db.execute('SELECT event_id,kind,role,work_id,payload,created_at FROM events '
                          'WHERE run_id=? AND created_at>=? ORDER BY event_id', (run, START)):
        if row['kind'].startswith('VERIFICATION_EXCEPTION_') or row['kind'] in (
                'VERIFICATION_RETIRED', 'INTEGRATION_VERIFICATION_REPORTED', 'REQUIREMENT_COMPLETED',
                'SUPPLEMENTAL_REVALIDATION_QUEUED'):
            payload = json.loads(row['payload'])
            events.append({k: row[k] for k in ('event_id', 'kind', 'role', 'created_at')} | {
                'facts': {k: payload[k] for k in ('task_id', 'attempt', 'disposition', 'owner', 'result',
                    'observation_event_id', 'verification_id', 'original_request_event_id', 'code') if k in payload}})
    notifications = [dict(r) for r in db.execute("SELECT kind,state,COUNT(*) count,MIN(created_at) oldest "
        "FROM outbox WHERE run_id=? AND state IN ('PENDING','UNKNOWN') GROUP BY kind,state", (run,))]
    reviews = [dict(r) for r in db.execute("SELECT a.action_id,a.kind,a.state,c.task_id,c.attempt,"
        "c.state candidate_state,t.attempt current_attempt,t.phase current_task_phase FROM external_actions a "
        "JOIN candidates c USING(candidate_id) JOIN tasks t ON c.run_id=t.run_id AND c.task_id=t.task_id "
        "WHERE c.run_id=? AND a.state IN ('SUBMITTED','UNKNOWN','INCOMPLETE','RESULT_READY')", (run,))]
    memory_count = db.execute("SELECT COUNT(*) FROM works WHERE run_id=? AND json_type(checkpoint,'$.work_memory') IS NOT NULL", (run,)).fetchone()[0]
    executions = [dict(r) for r in db.execute('SELECT execution_id,role,work_id,state,bound_at,ended_at '
        'FROM executions WHERE bound_at>=? AND work_id IN (SELECT work_id FROM works WHERE run_id=?)', (START, run))]
    db.rollback()
    db.close()

    state = connect(HOME / 'state.db')
    execution_stats = []
    for execution in executions:
        scope = read(HOME / 'runtime/role-pool-executions' / (execution['execution_id'] + '.json'), {})
        sid = scope.get('session_id')
        item = dict(execution)
        item['runtime_version_digest'] = scope.get('runtime_version_digest')
        budget = read(HOME / 'runtime/role-session-budgets' /
            (hashlib.sha256(execution['execution_id'].encode()).hexdigest() + '.json'), {})
        item['session_budget'] = {k: budget.get(k) for k in ('observed_at', 'requests',
            'approx_input_tokens', 'policy', 'checkpoint_required')}
        tools, codes, actions, input_modes, query_shapes = Counter(), Counter(), Counter(), Counter(), Counter()
        memory_writes = 0
        failures, verification_calls = [], []
        if sid:
            for row in state.execute('SELECT id,timestamp,role,tool_name,tool_calls,content FROM messages '
                                     'WHERE session_id=? AND timestamp<=? ORDER BY id', (sid, cutoff)):
                if row['role'] == 'tool':
                    tools[row['tool_name']] += 1
                    value = read_value(row['content'])
                    if value.get('status') in ('REJECTED', 'UNVERIFIED') or value.get('error'):
                        codes[value.get('code') or 'OTHER_ERROR'] += 1
                        failures.append({'message_id': row['id'], 'at': row['timestamp'], 'tool': row['tool_name'],
                            'code': value.get('code'), 'phase': value.get('phase'),
                            'side_effect_state': value.get('side_effect_state'),
                            'invocation_started': (value.get('recovery') or {}).get('invocation_started'),
                            'session_checkpoint_guard_text': isinstance(value.get('error'), str)
                                and '安全交接阈值' in value['error']})
                elif row['role'] == 'assistant' and row['tool_calls']:
                    for call in read_value(row['tool_calls'], []):
                        function = call.get('function') or call
                        args = function.get('arguments') or {}
                        args = read_value(args) if isinstance(args, str) else args
                        if not isinstance(args, dict):
                            continue
                        name = function.get('name')
                        if name == 'role_work_input':
                            input_modes['events_only' if args.get('events_only') else 'full'] += 1
                        elif name == 'role_decision_context':
                            query_shapes[str((args.get('operation'), tuple(sorted(args))))] += 1
                        elif name in ('role_coordination_command', 'role_test_command', 'role_development_command'):
                            actions[args.get('action', '(missing)')] += 1
                            payload = args.get('payload') or {}
                            cp = (payload.get('checkpoint') or {}) if isinstance(payload, dict) else {}
                            memory_writes += int(isinstance(cp, dict) and 'work_memory' in cp)
                            if args.get('action') == 'verify_integration' and isinstance(payload, dict):
                                verification_calls.append({'message_id': row['id'], 'at': row['timestamp'],
                                    'report_format': payload.get('report_format'),
                                    'junit_placeholder_present': '{junit_xml}' in json.dumps(payload.get('argv', []))})
        item.update(tool_counts=dict(tools), error_codes=dict(codes), command_action_counts=dict(actions),
                    work_input_modes=dict(input_modes), decision_argument_shapes=dict(query_shapes),
                    work_memory_checkpoint_calls=memory_writes, tool_failures=failures,
                    integration_verification_calls=verification_calls)
        execution_stats.append(item)
    state.rollback()
    state.close()

    health_path = max((HOME / 'cache/role-lease-monitor').glob('*/snapshot.json'), key=lambda p: p.stat().st_mtime)
    health = read(health_path, {})
    health_summary = {k: health.get(k) for k in ('at', 'health', 'event_high_water', 'notification_health', 'capacity')}
    health_summary['health_layers'] = {k: v for k, v in health.get('health_layers', {}).items() if k in {
        'current_run_id', 'collector_fault_count', 'business_authority', 'current_writer_count',
        'unverified_writer_scope_count', 'current_inflight_service_count', 'historical_evidence_debt_count',
        'historical_debt_archived', 'terminal_services_verified', 'terminal_result_incomplete'}}
    health_summary.update(age_seconds=round(cutoff - health.get('at', cutoff), 3),
        fault_types=dict(Counter(a.get('type') for a in health.get('faults', []))),
        anomaly_types=dict(Counter(a.get('type') for a in health.get('anomalies', []))))
    release = read(Path('/home/wang/work/.local/review-report-20261030/live-verification.json'), {})
    hashes = release.get('source_sha256', {})
    checks = {k: release.get(k) for k in ('checked_at', 'gateway_pid', 'activation_pid', 'activation_state', 'mode')}
    checks.update(source_files=len(hashes), source_mismatches=[k for k, v in hashes.items()
        if hashlib.sha256((REPO/k).read_bytes()).hexdigest() != v])
    metrics_root = HOME / 'role-pool.runtime/decision-metrics'
    usage = connect(HOME / 'runtime/role-usage.db')
    request_links = []
    for path in sorted((metrics_root / 'requests').glob('*.json')):
        link = read(path, {})
        row = usage.execute('SELECT execution_id,status,usage_present FROM role_usage WHERE physical_attempt_id=?',
            (link.get('physical_attempt_id'),)).fetchone()
        request_links.append({'physical_attempt_id': link.get('physical_attempt_id'),
            'execution_id': link.get('execution_id'), 'decision_ids_count': len(link.get('decision_ids', [])),
            'usage_row_found': row is not None,
            'execution_matches': bool(row and row['execution_id'] == link.get('execution_id')),
            'usage_status': row['status'] if row else None, 'usage_present': row['usage_present'] if row else None})
    usage.rollback()
    usage.close()
    result = {'sampled_at': stamp(cutoff), 'message_cutoff': cutoff,
        'sampling': 'Ledger, messages and cached observer sampled separately; each SQLite connection is query_only/read-only. Not a cross-database atomic snapshot.',
        'source_head': subprocess.check_output(['git','rev-parse','HEAD'],cwd=REPO,text=True).strip(),
        'authority': authority, 'event_high_water': high, 'tasks': tasks, 'open_works': works,
        'exception_and_completion_events': events, 'pending_notifications': notifications,
        'review_action_bindings': reviews, 'works_with_work_memory': memory_count,
        'executions_since_semantic_activation': execution_stats, 'health': health_summary,
        'recorded_gateway_0151': checks, 'decision_request_linkage': request_links,
        'storage': {'database_bytes': dbpath.stat().st_size,
            'wal_bytes': Path(str(dbpath)+'-wal').stat().st_size if Path(str(dbpath)+'-wal').exists() else 0},
        'side_effects': 'Only this audit JSON written; no ACK, business command, reload, restart, service, model, test or notification.'}
    (ROOT / 'current-review-summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))


def read_value(raw, default=None):
    if default is None:
        default = {}
    try:
        value = json.loads(raw or '{}')
        return value if isinstance(value, type(default)) else default
    except (ValueError, TypeError):
        return default


if __name__ == '__main__':
    main()
