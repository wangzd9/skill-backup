"""Narrow read-only follow-up; export tool contracts and state metadata, not prose."""
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
HOME = Path('/home/wang/.hermes')
REPO = Path('/home/wang/work/hermes-role-runtime')
ZONE = ZoneInfo('Asia/Shanghai')


def connect(path):
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=3)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    return db


def stamp(value):
    return datetime.fromtimestamp(value, ZONE).isoformat()


def read(path):
    return json.loads(path.read_text())


def main():
    now = time.time()
    frozen = read(ROOT / 'effects-summary.json')
    db = connect(HOME / 'state.db')
    tool_observations = []
    event_pages = []
    for session in frozen['sessions']:
        if session['version'] != '0.15.0':
            continue
        for row in db.execute('SELECT id,role,tool_name,tool_calls,content,timestamp FROM messages '
                              'WHERE session_id=? AND timestamp<? ORDER BY id', (session['session_id'], now)):
            base = {'session_id': session['session_id'], 'role': session['role'],
                    'message_id': row['id'], 'at': row['timestamp']}
            if row['role'] == 'assistant' and row['tool_calls']:
                for call in json.loads(row['tool_calls']):
                    function = call.get('function') or call
                    if function.get('name') not in ('role_decision_context', 'role_work_input'):
                        continue
                    args = function.get('arguments', {})
                    args = json.loads(args) if isinstance(args, str) else args
                    tool_observations.append({**base, 'direction': 'call', 'tool': function['name'],
                                              'tool_call_id': call.get('id'), 'arguments': args})
            elif row['role'] == 'tool' and row['tool_name'] in ('role_decision_context', 'role_work_input'):
                value = json.loads(row['content'])
                keys = ('status', 'code', 'phase', 'source_status', 'refresh_required', 'can_ack',
                        'side_effect_state', 'decision_id', 'material_fingerprint', 'processed_through',
                        'issued_through', 'through', 'frozen_through', 'next_after', 'has_more', 'items_total')
                item = {**base, 'direction': 'reply', 'tool': row['tool_name'],
                        'bytes': len(row['content'].encode()), 'fields': {k: value[k] for k in keys if k in value}}
                if row['tool_name'] == 'role_decision_context':
                    item['material_present'] = 'material' in value
                    item['material_inline'] = value.get('material', {}).get('inline', True)
                else:
                    event_pages.append(item)
                tool_observations.append(item)
    db.close()

    db = connect(HOME / 'role-pool.runtime/business.sqlite3')
    db.execute('BEGIN')
    authority = dict(db.execute('SELECT mode,generation,current_run_id,admission_open FROM authority').fetchone())
    run = authority['current_run_id']
    tasks = [dict(r) for r in db.execute('SELECT task_id,role,phase,attempt FROM tasks WHERE run_id=?', (run,))]
    works = [dict(r) for r in db.execute("SELECT work_id,task_id,attempt,role,kind,state,processed_through,"
            "issued_through,active_execution_id FROM works WHERE run_id=? AND state!='ENDED'", (run,))]
    high = db.execute('SELECT MAX(event_id) FROM events').fetchone()[0]
    events = []
    for row in db.execute('SELECT * FROM events WHERE run_id=? AND created_at>=? ORDER BY event_id',
                         (run, datetime.fromisoformat('2026-10-30T17:29:59+08:00').timestamp())):
        r = dict(row)
        payload = json.loads(r.pop('payload'))
        r['facts'] = {k: payload[k] for k in ('task_id', 'attempt', 'candidate_id', 'result', 'state',
            'owner', 'observation_event_id', 'disposition', 'processed_through', 'previous_processed_through',
            'acknowledged', 'execution_id', 'native_outcome', 'code') if k in payload}
        events.append(r)
    notifications = [dict(r) for r in db.execute("SELECT kind,state,COUNT(*) count,MIN(created_at) oldest "
            "FROM outbox WHERE run_id=? AND state IN ('PENDING','UNKNOWN') GROUP BY kind,state", (run,))]
    reviews = [dict(r) for r in db.execute("SELECT a.action_id,a.kind,a.state,c.candidate_id,c.task_id,"
            "c.attempt,c.state candidate_state,t.attempt current_attempt,t.phase current_task_phase "
            "FROM external_actions a JOIN candidates c USING(candidate_id) "
            "JOIN tasks t ON c.run_id=t.run_id AND c.task_id=t.task_id "
            "WHERE c.run_id=? AND a.state IN ('SUBMITTED','UNKNOWN','INCOMPLETE','RESULT_READY') "
            "ORDER BY a.action_id", (run,))]
    db.rollback()
    db.close()

    hp = max((HOME / 'cache/role-lease-monitor').glob('*/snapshot.json'), key=lambda p: p.stat().st_mtime)
    health = read(hp)
    health_data = {k: health[k] for k in ('at', 'health', 'event_high_water', 'health_layers')}
    health_data.update(snapshot_at=stamp(health['at']), age_seconds=round(now-health['at'], 3),
                       anomaly_types=dict(Counter(a.get('type') for a in health.get('anomalies', []))),
                       fault_types=dict(Counter(a.get('type') for a in health.get('faults', []))))

    metrics_root = HOME / 'role-pool.runtime/decision-metrics'
    metrics = {p.name: len(list(p.glob('*.json'))) for p in metrics_root.iterdir() if p.is_dir()}
    usage = connect(HOME / 'runtime/role-usage.db')
    links = []
    for path in (metrics_root / 'requests').glob('*.json'):
        item = read(path)
        row = usage.execute('SELECT physical_attempt_id,logical_request_id,execution_id,status,usage_present '
                            'FROM role_usage WHERE physical_attempt_id=?', (item['physical_attempt_id'],)).fetchone()
        links.append({'physical_attempt_id': item['physical_attempt_id'], 'decision_ids': item['decision_ids'],
                      'attribution': item['attribution'], 'usage_row_found': row is not None,
                      'execution_matches': bool(row and row['execution_id'] == item['execution_id']),
                      'usage_status': row['status'] if row else None,
                      'usage_present': row['usage_present'] if row else None})
    usage.close()

    evidence = []
    names = ['__init__.py', 'business_ledger.py', 'workflow_runtime.py', 'workflow_progression.py',
             'workflow_model_input.py', 'workflow_toolsets.py', 'workflow_resumption.py',
             'workflow_decision_context.py', 'workflow_decision_metrics.py', 'workflow_work_memory.py',
             'workflow_verification_exceptions.py', 'workflow_completion.py', 'workflow_recovery.py',
             'workflow_query.py', 'workflow_reports.py', 'workflow_notification_registry.py',
             'workflow_terminal_observation.py',
             'tests/test_semantic_decision.py', 'tests/test_verification_exceptions.py']
    for name in names:
        data = (REPO / name).read_bytes()
        evidence.append({'path': str(REPO / name), 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
    tests = []
    for name in ('token-optimization-20261030/tests-final.log', 'semantic-decision-20261030/release-tests.log'):
        path = Path('/home/wang/work/.local') / name
        data = path.read_bytes()
        body = data.decode()
        tests.append({'path': str(path), 'sha256': hashlib.sha256(data).hexdigest(),
                      'result': re.findall(r'Ran \d+ tests in [\d.]+s\s+OK', body)[-1:]})

    out = {'sampled_at': stamp(now), 'frozen_primary_sample': frozen['sampled_at'],
           'scope': 'Read-only follow-up; separate cut-off from primary usage statistics. No narrative/model reasoning exported.',
           'tool_observations': tool_observations, 'authority': authority, 'tasks': tasks, 'open_works': works,
           'event_high_water': high, 'events_since_semantic_activation': events,
           'pending_notifications': notifications, 'review_action_bindings': reviews, 'health': health_data,
           'decision_metrics_files': metrics, 'request_linkage': links,
           'code_evidence': evidence, 'release_test_evidence': tests}
    target = ROOT / 'followup-summary.json'
    target.write_text(json.dumps(out, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: out[k] for k in ('sampled_at', 'open_works', 'decision_metrics_files',
                                        'request_linkage', 'release_test_evidence')}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
