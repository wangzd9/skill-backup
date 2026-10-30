"""Read-only post-release audit; export metadata and counts, never message bodies."""
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sqlite3
import statistics
import subprocess
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
HOME = Path('/home/wang/.hermes')
REPO = Path('/home/wang/work/hermes-role-runtime')
ZONE = ZoneInfo('Asia/Shanghai')
END = time.time()
START = datetime(2026, 10, 30, 12, tzinfo=ZONE).timestamp()
TOKEN_LIVE = datetime.fromisoformat('2026-10-30T16:31:45+08:00').timestamp()
SEMANTIC_LIVE = datetime.fromisoformat('2026-10-30T17:29:59+08:00').timestamp()
VERSIONS = {
    'b1a3be329a3e15496a4ddc5093e8220bf86b341d306d443ce5f7684e6e3e7354': '0.14.0',
    '0b6d088583f6e7332596463e7cb2233dbbfba8d47281e8fc6384d5d18baae148': '0.15.0',
}


def stamp(value):
    return datetime.fromtimestamp(value, ZONE).isoformat()


def read_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def connect(path):
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=3)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    return db


def distribution(values):
    values = sorted(values)
    return {'n': len(values), 'p50': statistics.median(values) if values else None,
            'p90': values[min(len(values)-1, int((len(values)-1)*.9))] if values else None,
            'max': max(values) if values else None}


def usage_stats(rows):
    prompts = [r['usage']['prompt_tokens'] for r in rows if r['usage'].get('prompt_tokens') is not None]
    outputs = [r['usage']['output_tokens'] for r in rows if r['usage'].get('output_tokens') is not None]
    duration = [r['ended_at']-r['started_at'] for r in rows if r['ended_at'] is not None]
    return {'physical_requests': len(rows), 'known_prompt_requests': len(prompts),
            'sessions': len({r['session_id'] for r in rows}),
            'prompt_tokens': sum(prompts), 'output_tokens': sum(outputs),
            'prompt_per_request': distribution(prompts), 'client_duration_seconds': distribution(duration),
            'cache_read_coverage': sum(r['usage'].get('cache_read_tokens') is not None for r in rows),
            'statuses': dict(Counter(r['status'] for r in rows)),
            'models': dict(Counter(r['model'] for r in rows)),
            'routes': dict(Counter(r['route'] for r in rows)),
            'extra_physical_requests': len(rows)-len({(r['session_id'], r['logical_request_id']) for r in rows})}


def extract_packet(content):
    decoder = json.JSONDecoder()
    for marker in ('{"accepted_requirements"', '{"candidate_approvals"', '{"candidates"'):
        at = content.find(marker)
        if at >= 0:
            try:
                packet, _ = decoder.raw_decode(content[at:])
                if isinstance(packet, dict) and packet.get('kind') == 'current-work-input':
                    return packet
            except ValueError:
                pass
    return None


def source_state():
    control_path = HOME/'hermes-agent/gateway/control_socket.py'
    spec = importlib.util.spec_from_file_location('audit_gateway_control', control_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    status = module.query_gateway_control(HOME, 'status', timeout=5) or {}
    receipt = read_json(HOME/'runtime/role-observation/mechanism-activation.json', {})
    recorded = read_json(Path('/home/wang/work/.local/semantic-decision-20261030/live-verification.json'), {})
    def verify(value):
        hashes = value.get('source_sha256', {})
        return {'files': len(hashes), 'mismatches': [name for name, digest in hashes.items()
                if hashlib.sha256((REPO/name).read_bytes()).hexdigest() != digest]}
    return {'head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip(),
            'gateway': {k: status.get(k) for k in ('pid', 'answering_pid', 'gateway_state', 'answered_at')},
            'platform_states': {k: v.get('state') for k, v in status.get('platforms', {}).items()},
            'current_activation': {k: receipt.get(k) for k in ('process_id', 'state', 'mode')} | verify(receipt),
            'recorded_gateway_activation': {k: recorded.get(k) for k in ('checked_at', 'gateway_pid',
                'activation_pid', 'activation_state', 'mode')} | verify(recorded),
            'notice': 'Latest shared receipt may belong to a worker; it is not automatically gateway proof. No reload performed.'}


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    usage_db = connect(HOME/'runtime/role-usage.db')
    rows = []
    scopes = {}
    for r in usage_db.execute("SELECT * FROM role_usage WHERE started_at>=? AND started_at<? AND task_kind='business_cron' ORDER BY started_at", (START, END)):
        r = dict(r)
        r['usage'] = json.loads(r['usage_json'] or '{}')
        eid = r['execution_id']
        if eid not in scopes:
            scopes[eid] = read_json(HOME/'runtime/role-pool-executions'/(eid+'.json'), {})
        r['version'] = VERSIONS.get(scopes[eid].get('runtime_version_digest'), 'older_or_unmapped')
        rows.append(r)
    usage_db.close()
    periods = {
        'pre_token_12_to_1447': (START, datetime.fromisoformat('2026-10-30T14:47:50+08:00').timestamp()),
        'health_and_drain': (datetime.fromisoformat('2026-10-30T15:25:34+08:00').timestamp(), TOKEN_LIVE),
        'token_live_before_semantic': (TOKEN_LIVE, SEMANTIC_LIVE),
        'after_semantic_gateway_activation': (SEMANTIC_LIVE, END),
    }
    aggregate = {}
    for key, (start, end) in periods.items():
        chosen = [r for r in rows if start <= r['started_at'] < end]
        aggregate[key] = {'start': stamp(start), 'end': stamp(end), 'roles': {
            role: usage_stats([r for r in chosen if r['role'] == role]) for role in sorted({r['role'] for r in chosen})}}
    by_version = {}
    for key in sorted({(r['version'], r['role']) for r in rows}):
        chosen = [r for r in rows if (r['version'], r['role']) == key]
        by_version['/'.join(key)] = usage_stats(chosen)

    state = connect(HOME/'state.db')
    sessions = []
    tool_groups = defaultdict(lambda: {'replies': 0, 'errors': 0, 'codes': Counter(), 'bytes': []})
    input_queries, decision_queries, command_actions = Counter(), Counter(), Counter()
    for sid in sorted({r['session_id'] for r in rows}):
        sr = [r for r in rows if r['session_id'] == sid]
        first = sr[0]
        role, version = first['role'], first['version']
        initial = state.execute("SELECT content FROM messages WHERE session_id=? AND role='user' ORDER BY id LIMIT 1", (sid,)).fetchone()
        packet = extract_packet(initial['content'] or '') if initial else None
        tools = Counter()
        for m in state.execute('SELECT role,tool_name,content,tool_calls,timestamp FROM messages WHERE session_id=? AND timestamp>=? AND timestamp<? ORDER BY id', (sid, START, END)):
            if m['role'] == 'tool':
                name = m['tool_name'] or '(unknown)'
                body = m['content'] or ''
                group = tool_groups[(version, role, name)]
                group['replies'] += 1
                group['bytes'].append(len(body.encode()))
                tools[name] += 1
                try:
                    parsed = json.loads(body)
                except ValueError:
                    parsed = {}
                if not isinstance(parsed, dict):
                    parsed = {}
                error = bool(parsed.get('error')) or parsed.get('status') in ('REJECTED', 'UNVERIFIED') or body.startswith(('Error:', '[TOOL_ERROR]'))
                group['errors'] += int(error)
                for code in ('PAGE_AFTER_NOT_PREVIOUSLY_ISSUED', 'CHECKPOINT_CONTROL_FIELDS_MISPLACED',
                             'ROLE_CAPABILITY_NOT_ALLOWED', 'QUERY_METADATA_EXCEEDS_BYTE_BUDGET',
                             'INVALID_COMMAND_PAYLOAD', 'PAYLOAD_MUST_BE_OBJECT', 'SESSION_CHECKPOINT_REQUIRED'):
                    if error and code in body:
                        group['codes'][code] += 1
            elif m['role'] == 'assistant' and m['tool_calls']:
                try:
                    calls = json.loads(m['tool_calls'])
                except ValueError:
                    continue
                if not isinstance(calls, list):
                    continue
                for call in calls:
                    function = call.get('function') or call
                    name = function.get('name')
                    args = function.get('arguments', {})
                    try:
                        args = json.loads(args) if isinstance(args, str) else args
                    except ValueError:
                        continue
                    if not isinstance(args, dict):
                        continue
                    key = version+'/'+role
                    if name == 'role_work_input':
                        input_queries[(key, 'history' if args.get('history') else 'events_only' if args.get('events_only') else 'full')] += 1
                    elif name == 'role_decision_context':
                        decision_queries[(key, args.get('operation', 'directory'))] += 1
                    elif name in ('role_coordination_command', 'role_test_command', 'role_development_command'):
                        command_actions[(key, name, args.get('action', '(missing)'))] += 1
        scope = scopes.get(first['execution_id'], {})
        audit = read_json(HOME/'runtime/prefix-audit'/(hashlib.sha256(sid.encode()).hexdigest()+'.json'), {})
        sessions.append({'session_id': sid, 'execution_id': first['execution_id'], 'role': role, 'version': version,
            'started_at': stamp(first['started_at']), 'known_first_prompt_tokens': first['usage'].get('prompt_tokens'),
            'requests': len(sr), 'prompt_tokens': sum(r['usage'].get('prompt_tokens') or 0 for r in sr),
            'runtime_digest': scope.get('runtime_version_digest'), 'tool_names': audit.get('tool_names'),
            'input_projection': packet.get('input_projection') if packet else None,
            'packet_bytes': len(json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()) if packet else None,
            'initial_message_bytes': len((initial['content'] or '').encode()) if initial else None,
            'has_decision_directory': bool(packet and packet.get('decision_context')),
            'initial_next_actions': [a['action'] for a in (packet or {}).get('next_actions', [])],
            'initial_processed_through': ((packet or {}).get('work') or {}).get('processed_through'),
            'initial_work_memory_present': bool(((packet or {}).get('work') or {}).get('checkpoint', {}).get('work_memory')),
            'tool_reply_counts': dict(tools)})
    state.close()

    ledger = connect(HOME/'role-pool.runtime/business.sqlite3')
    ledger.execute('BEGIN')
    authority = dict(ledger.execute('SELECT mode,generation,current_run_id,admission_open,freeze_reason FROM authority').fetchone())
    run = authority['current_run_id']
    tasks = [dict(r) for r in ledger.execute('SELECT task_id,role,phase,attempt,unresolved_reason FROM tasks WHERE run_id=?', (run,))]
    works = [dict(r) for r in ledger.execute("SELECT work_id,task_id,attempt,role,kind,state,processed_through,issued_through,active_execution_id,created_at FROM works WHERE run_id=? AND state!='ENDED'", (run,))]
    for work in works:
        work['pending_relevant_event_count'] = ledger.execute('SELECT COUNT(*) FROM events WHERE event_id>? AND (run_id=? OR run_id IS NULL)', (work['processed_through'], run)).fetchone()[0]
        work['pending_nonmechanical_event_count'] = ledger.execute("SELECT COUNT(*) FROM events WHERE event_id>? AND (run_id=? OR run_id IS NULL) AND kind NOT IN ('EXECUTION_BOUND','EXECUTION_ENDED','EXECUTION_OUTCOME_OBSERVED','TOOL_EXECUTION_REGISTERED','CHECKPOINT_SAVED','WORK_CLAIMED')", (work['processed_through'], run)).fetchone()[0]
    executions = []
    for r in ledger.execute('SELECT e.*,w.kind FROM executions e JOIN works w USING(work_id) WHERE w.run_id=? AND e.bound_at>=? ORDER BY e.bound_at', (run, START)):
        e = dict(r)
        scope = read_json(HOME/'runtime/role-pool-executions'/(e['execution_id']+'.json'), {})
        executions.append({k: e[k] for k in ('execution_id', 'work_id', 'role', 'kind', 'state', 'bound_at', 'ended_at', 'pid')} |
                          {'session_id': scope.get('session_id'), 'runtime_digest': scope.get('runtime_version_digest')})
    events = []
    for row in ledger.execute('SELECT * FROM events WHERE (run_id=? OR run_id IS NULL) AND created_at>=? ORDER BY event_id', (run, START)):
        r = dict(row)
        p = json.loads(r.pop('payload'))
        r['facts'] = {k: p[k] for k in ('task_id', 'attempt', 'candidate_id', 'head', 'result', 'state', 'owner',
            'observation_event_id', 'disposition', 'processed_through', 'previous_processed_through', 'acknowledged',
            'yield_execution', 'execution_id', 'native_outcome', 'code') if k in p}
        if r['kind'] == 'INTEGRATION_VERIFICATION_REPORTED':
            r['facts']['checks'] = [{'id': c.get('id'), 'result': c.get('result'), 'mode': c.get('mode')} for c in p.get('checks', [])]
        events.append(r)
    recent_commands = [dict(r) for r in ledger.execute('SELECT verb,created_at,LENGTH(CAST(result AS BLOB)) AS bytes,json_extract(result,\'$.status\') AS status FROM commands WHERE rowid>(SELECT MAX(rowid)-350 FROM commands) ORDER BY rowid')]
    outbox = [dict(r) for r in ledger.execute("SELECT kind,state,COUNT(*) AS count,MIN(created_at) AS oldest FROM outbox WHERE run_id=? AND state IN ('PENDING','UNKNOWN') GROUP BY kind,state", (run,))]
    external = [dict(r) for r in ledger.execute('SELECT a.kind,a.state,COUNT(*) AS count FROM external_actions a JOIN candidates c USING(candidate_id) WHERE c.run_id=? GROUP BY a.kind,a.state', (run,))]
    ledger.rollback()
    ledger.close()
    health_files = list((HOME/'cache/role-lease-monitor').glob('*/snapshot.json'))
    health = read_json(max(health_files, key=lambda p: p.stat().st_mtime), {}) if health_files else {}
    health_summary = {k: health.get(k) for k in ('checked_at', 'observed_at', 'health', 'activity_policy', 'health_layers')}
    health_summary.update(anomalies=len(health.get('anomalies', [])), faults=len(health.get('faults', [])),
        anomaly_types=dict(Counter(a.get('type') for a in health.get('anomalies', []))),
        fault_types=dict(Counter(a.get('type') for a in health.get('faults', []))))
    metrics = HOME/'role-pool.runtime/decision-metrics'
    metrics_summary = {d.name: len(list(d.glob('*.json'))) for d in metrics.iterdir() if d.is_dir()} if metrics.exists() else {}
    data = {'sampled_at': stamp(END), 'source': source_state(), 'scope': 'Read-only observations; no business actions, ACKs, reloads or model calls.',
        'usage_basis': 'Business-cron physical requests by request start; missing usage unknown; version by persisted execution digest.',
        'windows': aggregate, 'by_runtime_and_role': by_version,
        'tools': [{'version': k[0], 'role': k[1], 'tool': k[2], 'replies': v['replies'], 'error_or_unverified_replies': v['errors'],
                  'known_codes': dict(v['codes']), 'reply_bytes': distribution(v['bytes'])} for k, v in sorted(tool_groups.items())],
        'submitted_work_input_modes': [{'version_role': k[0], 'mode': k[1], 'count': v} for k, v in sorted(input_queries.items())],
        'submitted_decision_queries': [{'version_role': k[0], 'operation': k[1], 'count': v} for k, v in sorted(decision_queries.items())],
        'submitted_commands': [{'version_role': k[0], 'tool': k[1], 'action': k[2], 'count': v} for k, v in sorted(command_actions.items())],
        'sessions': sessions, 'authority': authority, 'tasks': tasks, 'open_works': works, 'executions_since_noon': executions,
        'events_since_noon': events, 'recent_350_commands': recent_commands, 'pending_notifications': outbox,
        'external_action_states': external, 'health': health_summary, 'decision_metrics_files': metrics_summary,
        'limitations': ['Different role phases and maintenance drain confound time-window comparisons.',
            '0.15 workers have only a short observation window; existing workers retain their earlier code.',
            'Client duration is not pure inference latency; byte reduction is not measured token saving.',
            'Submitted tool commands are not automatically committed business outcomes.']}
    path = ROOT/'effects-summary.json'
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({'output': str(path), 'sampled_at': data['sampled_at'], 'source': data['source'],
        'by_runtime_and_role': by_version, 'open_works': works, 'decision_metrics_files': metrics_summary,
        'new_tool_queries': data['submitted_decision_queries'], 'input_modes': data['submitted_work_input_modes'],
        'health': health_summary}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
