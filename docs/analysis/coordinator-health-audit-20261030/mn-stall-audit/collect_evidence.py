"""MN incident evidence: read-only databases/artifacts/Git; no execution of tests."""
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import time
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
HOME = Path('/home/wang/.hermes')
WORK = Path('/home/wang/work')
RUN = 'prepared-rosbag-catalog-a100-multi-node'
ZONE = ZoneInfo('Asia/Shanghai')


def stamp(value):
    return datetime.fromtimestamp(value, ZONE).isoformat() if value is not None else None


def read(path):
    return json.loads(path.read_text())


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def db(path):
    c = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=3)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA query_only=ON')
    return c


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    sampled = time.time()
    c = db(HOME / 'role-pool.runtime/business.sqlite3')
    c.execute('BEGIN')
    authority = dict(c.execute('SELECT mode,generation,current_run_id,admission_open FROM authority').fetchone())
    run = dict(c.execute('SELECT * FROM runs WHERE run_id=?', (RUN,)).fetchone())
    tasks = [dict(r) for r in c.execute('SELECT task_id,role,phase,attempt,base,ac_version,required_checks '
                                      'FROM tasks WHERE run_id=?', (RUN,))]
    attempts = []
    for r in c.execute('SELECT task_id,attempt,state,reason FROM attempts WHERE run_id=? ORDER BY task_id,attempt', (RUN,)):
        r = dict(r)
        reason = r.pop('reason') or ''
        category = ('initial' if r['attempt'] == 1 else 'base_refresh' if reason.startswith('合入基线接续')
                    else 'integration_issue' if reason.startswith('最终集成验收问题')
                    else 'verification_exception' if r['task_id'] == 'MN-04' and r['attempt'] == 8 else 'rework')
        attempts.append({**r, 'category': category, 'reason_sha256': hashlib.sha256(reason.encode()).hexdigest()})
    candidates = [dict(r) for r in c.execute('SELECT candidate_id,task_id,attempt,head,base,state,created_at '
                                            'FROM candidates WHERE run_id=? ORDER BY created_at', (RUN,))]
    verifications = []
    for r in c.execute('SELECT v.*,c.task_id,c.attempt FROM verifications v JOIN candidates c USING(candidate_id) '
                       'WHERE c.run_id=? ORDER BY v.created_at', (RUN,)):
        r = dict(r)
        e = json.loads(r.pop('evidence'))
        r['evidence_reference'] = {k: e[k] for k in ('path', 'sha256') if k in e}
        verifications.append(r)
    works = [dict(r) for r in c.execute('SELECT work_id,task_id,attempt,role,kind,state,candidate_id,created_at,'
              'ended_at,processed_through,issued_through,active_execution_id FROM works WHERE run_id=?', (RUN,))]
    execution_counts = dict(c.execute('SELECT role,COUNT(*) FROM executions WHERE work_id IN '
              '(SELECT work_id FROM works WHERE run_id=?) GROUP BY role', (RUN,)).fetchall())
    event_counts = dict(c.execute('SELECT kind,COUNT(*) FROM events WHERE run_id=? GROUP BY kind', (RUN,)).fetchall())
    events = []
    significant = {'CANDIDATE_MERGED', 'INTEGRATION_VERIFICATION_QUEUED', 'INTEGRATION_VERIFICATION_REPORTED',
        'FINAL_ACCEPTANCE_REWORK_QUEUED', 'COMPLETION_OBLIGATIONS_FROZEN', 'USER_DECISION_RECORDED',
        'USER_DECISION_RESOLVED', 'VERIFICATION_EXCEPTION_RECORDED', 'VERIFICATION_EXCEPTION_DISPOSITION_RECORDED',
        'VERIFICATION_EXCEPTION_REPAIR_QUEUED', 'VERIFICATION_EXCEPTION_CLOSED', 'VERIFICATION_RETIRED'}
    for r in c.execute('SELECT * FROM events WHERE run_id=? ORDER BY event_id', (RUN,)):
        r = dict(r)
        if r['kind'] not in significant and r['event_id'] not in (14095, 14113, 14119, 14222):
            continue
        raw = r.pop('payload')
        p = json.loads(raw)
        r['facts'] = {k: p[k] for k in ('task_id', 'attempt', 'candidate_id', 'head', 'result', 'disposition',
            'observation_event_id', 'owner', 'request_id', 'state') if k in p}
        r['payload_sha256'] = hashlib.sha256(raw.encode()).hexdigest()
        r['at'] = stamp(r['created_at'])
        events.append(r)
    approvals = [dict(r) for r in c.execute("SELECT json_extract(payload,'$.candidate_id') candidate_id,COUNT(*) count,"
        "MIN(created_at) first,MAX(created_at) last FROM events WHERE run_id=? AND kind='CANDIDATE_DECIDED' "
        "AND json_extract(payload,'$.decision')='APPROVE' GROUP BY candidate_id ORDER BY count DESC", (RUN,))]
    rejected = []
    for r in c.execute("SELECT command_id,verb,created_at,result FROM commands WHERE rowid>"
            "(SELECT MAX(rowid)-1400 FROM commands) AND verb IN ('deliver','deliver_integration','decide',"
            "'release_work','extend_task_scope','publish_plan') ORDER BY rowid"):
        v = json.loads(r['result'])
        if v.get('status') in ('REJECTED', 'UNVERIFIED'):
            rejected.append({k: r[k] for k in ('command_id', 'verb', 'created_at')} |
                            {k: v[k] for k in ('status', 'code') if k in v})
    both = c.execute("SELECT COUNT(*) FROM candidates c WHERE run_id=? AND EXISTS(SELECT 1 FROM verifications v "
        "WHERE v.candidate_id=c.candidate_id AND check_name='independent_test' AND result='PASS') AND "
        "EXISTS(SELECT 1 FROM verifications v WHERE v.candidate_id=c.candidate_id AND check_name='ocr' AND result='FAIL')",
        (RUN,)).fetchone()[0]
    high = c.execute('SELECT MAX(event_id) FROM events').fetchone()[0]
    c.rollback()
    c.close()

    operations = []
    for p in sorted((HOME / 'role-pool.runtime/operations').glob('*/verification.json')):
        v = read(p)
        candidate = v.get('candidate') or {}
        if candidate.get('run_id') != RUN:
            continue
        stats = v.get('test_stats') or {}
        failures, xml_sources = [], []
        for name in ('junit.xml', 'junit-repeat.xml'):
            xml = p.parent / 'artifacts' / name
            if not xml.exists():
                continue
            xml_sources.append({'path': str(xml), 'sha256': digest(xml)})
            for case in ET.parse(xml).getroot().iter('testcase'):
                for item in case.findall('failure') + case.findall('error'):
                    body = (item.text or '') + ' ' + item.get('message', '')
                    markers = [code for code in ('1062', '1064', '1213', '1451', 'CircularDependencyError') if code in body]
                    if markers:
                        failures.append({'report': name, 'case': case.get('name'), 'markers': markers})
        runner = p.parent / 'result.evidence.json'
        receipt = read(runner) if runner.exists() else {}
        operations.append({'operation_id': p.parent.name, 'verification_path': str(p), 'sha256': digest(p),
            'candidate': candidate, 'status': v.get('status'), 'code': v.get('code'),
            'mechanical_result': stats.get('mechanical_result'), 'reasons': stats.get('reasons'),
            'case_fingerprint': stats.get('case_fingerprint'),
            'stability_exit_codes': (stats.get('stability_evidence') or {}).get('exit_codes'),
            'started_at': receipt.get('started_at'), 'ended_at': receipt.get('ended_at'),
            'failure_markers': failures, 'junit_sources': xml_sources})

    # Inspect tool envelopes, never assistant narrative or terminal command text.
    state = db(HOME / 'state.db')
    session = 'cron_d5cb80cf2fc5_20261030_175501'
    delivery_calls, tool_counts, porcelain = [], Counter(), []
    for r in state.execute('SELECT id,role,tool_name,content,tool_calls,timestamp FROM messages '
                           'WHERE session_id=? ORDER BY id', (session,)):
        if r['role'] == 'assistant' and r['tool_calls']:
            for call in json.loads(r['tool_calls']):
                f = call.get('function') or call
                if f.get('name') != 'role_development_command':
                    continue
                args = f.get('arguments', {})
                args = json.loads(args) if isinstance(args, str) else args
                if args.get('action') == 'deliver':
                    delivery_calls.append({'message_id': r['id'], 'at': r['timestamp'], 'kind': 'call',
                        'command_id': args.get('command_id'), 'candidate': args.get('payload', {}).get('candidate')})
        elif r['role'] == 'tool':
            tool_counts[r['tool_name']] += 1
            try:
                v = json.loads(r['content'])
            except ValueError:
                continue
            if not isinstance(v, dict):
                continue
            if r['tool_name'] == 'role_development_command':
                delivery_calls.append({'message_id': r['id'], 'at': r['timestamp'], 'kind': 'reply',
                    'fields': {k: v[k] for k in ('status', 'code', 'candidate_id', 'event_id') if k in v}})
            if r['tool_name'] == 'terminal':
                lines = [line for key in ('output', 'stdout') if isinstance(v.get(key), str)
                         for line in v[key].splitlines() if line.startswith('?? .mn04_a8_')]
                if lines:
                    porcelain.append({'message_id': r['id'], 'lines': lines})
    state.close()

    repo = WORK / 'rosbag-catalog'
    old = subprocess.check_output(['git', '-C', str(repo), 'show',
        'c59c0f7390d5b0d28f1d5a065e3791cdf0c5eeb1:tests/test_mn01_migration.py'])
    (ROOT / 'candidate-c59c0f7-test_mn01_migration.txt').write_bytes(old)
    runtime = WORK / 'hermes-role-runtime'
    sources = []
    for name in ('business_ledger.py', 'workflow_test_environment.py', 'workflow_completion.py',
                 'workflow_verification_exceptions.py', 'workflow_progression.py', 'workflow_recovery.py'):
        p = runtime / name
        sources.append({'path': str(p), 'sha256': digest(p)})
    duplicate = [o for o in operations if any('1062' in f['markers'] for f in o['failure_markers'])]
    data = {'sampled_at': stamp(sampled), 'event_high_water': high,
        'scope': 'Read-only diagnosis; no ACK, business mutation, service launch, test/model invocation or remote operation.',
        'runtime_head': git(runtime, 'rev-parse', 'HEAD'), 'authority': authority, 'run': run, 'tasks': tasks,
        'attempts': attempts, 'attempt_categories': dict(Counter(a['category'] for a in attempts)),
        'candidates': candidates, 'verifications': verifications, 'works': works,
        'execution_counts_by_role': execution_counts, 'event_counts': event_counts,
        'significant_events': events, 'approval_groups': approvals, 'recent_rejections': rejected,
        'independent_pass_and_ocr_fail_candidates': both, 'operations': operations,
        'same_1062_summary': {'operations': len(duplicate),
            'by_attempt': dict(Counter(str(o['candidate'].get('attempt')) for o in duplicate)),
            'case_fingerprints': dict(Counter(o['case_fingerprint'] for o in duplicate)),
            'exit_codes': dict(Counter(str(o['stability_exit_codes']) for o in duplicate)),
            'notice': 'Existing operation artifacts only; codes in failure/error nodes are not additional reproductions.'},
        'a8_delivery_session': session, 'a8_tool_counts': dict(tool_counts),
        'a8_delivery_envelopes': delivery_calls, 'a8_untracked_evidence_observation': porcelain,
        'frozen_fixture': {'commit': 'c59c0f7390d5b0d28f1d5a065e3791cdf0c5eeb1',
            'source_path': 'tests/test_mn01_migration.py', 'audit_copy': 'candidate-c59c0f7-test_mn01_migration.txt',
            'sha256': hashlib.sha256(old).hexdigest()}, 'runtime_sources': sources,
        'limitations': ['Historical events span mechanism versions; counts are not a current failure rate.',
                       'An independent PASS followed by OCR FAIL indicates coverage differences, not that all tests were ineffective.',
                       'No causal share of total delivery delay or token usage was measured.']}
    target = ROOT / 'evidence.json'
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'output': str(target), 'sampled_at': data['sampled_at'], 'tasks': tasks,
        'attempt_categories': data['attempt_categories'], 'operations': len(operations),
        'same_1062_summary': data['same_1062_summary']}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
