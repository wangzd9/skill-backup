"""Retire unusable verification work without taking over a live writer.

Requests are durable events. Only the owning TESTER can release a live work;
the observer can retire it after native execution/tool clearance. No PASS,
FAIL, event ACK, process kill, or user approval is invented.
"""
import re


def pending(db, work_id):
    from business_ledger import _row
    return _row(db.execute("SELECT * FROM events WHERE work_id=? AND kind='VERIFICATION_RETIREMENT_REQUESTED' "
                           'ORDER BY event_id DESC LIMIT 1', (work_id,)).fetchone())


def no_rerun_candidate_ids(db, run_id):
    """Return candidates explicitly retired as UNVERIFIED with no rerun."""
    from business_ledger import _row
    result = set()
    for row in db.execute("SELECT payload FROM events WHERE run_id=? "
                           "AND kind='VERIFICATION_RETIREMENT_REQUESTED'", (run_id,)):
        payload = _row(row)['payload']
        decision = payload.get('decision') or {}
        if (decision.get('result') == 'UNVERIFIED'
                and decision.get('reason_code') == 'TEST_RESULT_NONDETERMINISTIC'
                and decision.get('rerun_authorized') is False
                and decision.get('evidence_preserved') is True):
            candidate_id = payload.get('candidate_id')
            if isinstance(candidate_id, str) and candidate_id:
                result.add(candidate_id)
    return result


def request(db, ledger, work, reason, *, target_base=None, decision=None):
    if pending(db, work['work_id']):
        return None
    payload = {
        'candidate_id': work['candidate_id'], 'reason': reason, 'target_base': target_base,
        'requires_user_approval': False, 'business_result': None,
        'next': 'TESTER用safe_release结束旧验证；存活/未知writer保留围栏，实际退出后运行时自动收口。'}
    if decision:
        payload.update({'decision': decision, 'decision_request_id': decision['request_id'],
                        'decision_event_id': decision['event_id']})
    return ledger._event(db, 'VERIFICATION_RETIREMENT_REQUESTED', payload,
        run_id=work['run_id'], role='TESTER', work_id=work['work_id'])


def request_from_resolved_decision(db, ledger, original, resolution):
    """Convert one explicit non-deterministic close ruling into a safe-point request."""
    from business_ledger import _row, _hash
    source = original.get('payload', {})
    ruling = resolution.get('payload', {})
    answer = source.get('answer', '')
    if (source.get('decision_id') != 'close_verification'
            or ruling.get('decision_id') != 'close_verification'
            or ruling.get('request_id') != source.get('request_id')
            or not isinstance(answer, str)
            or not re.search(r'\bclose_verification\b', answer)
            or not re.search(r'\bUNVERIFIED\b', answer, re.I)
            or 'TEST_RESULT_NONDETERMINISTIC' not in answer
            or not any(phrase in answer for phrase in ('不再重跑', '不再重新运行', '不要重跑'))
            or 'independent_test' not in answer
            or not any(phrase in answer for phrase in ('不要 APPROVE', '不 APPROVE', '不得 APPROVE'))):
        return {'requested': False, 'reason': 'RULING_DOES_NOT_MATCH_SAFE_CLOSE_CONTRACT'}
    evidence = ruling.get('evidence') or {}
    related = evidence.get('related_objects') or {}
    def identity(value, prefix):
        if not isinstance(value, str):
            return None
        matches = list(dict.fromkeys(re.findall(
            rf'(?<![A-Za-z0-9]){re.escape(prefix)}-[0-9a-f]{{8,32}}(?![A-Za-z0-9])',
            value, re.I)))
        return matches[0] if len(matches) == 1 else None

    target_id = identity(related.get('work_running'), 'work')
    candidate_ref = identity(related.get('candidate_unverified'), 'candidate')
    next_candidate_ref = identity(related.get('candidate_next_test'), 'candidate')
    if not all((target_id, candidate_ref, next_candidate_ref)):
        return {'requested': False, 'reason': 'RESOLVED_OBJECT_BINDINGS_REQUIRED'}
    work_refs = re.findall(r'\bwork-[0-9a-f]{8,32}\b', answer, re.I)
    candidate_refs = re.findall(r'(?<![A-Za-z0-9])[0-9a-f]{7,40}(?![A-Za-z0-9])', answer, re.I)
    current_candidates = [_row(row) for row in db.execute(
        "SELECT * FROM candidates WHERE run_id=? AND state='CURRENT'", (original.get('run_id'),))]
    explicit_next_candidate = False
    for token in candidate_refs:
        matches = [item for item in current_candidates
            if item['candidate_id'].removeprefix('candidate-').lower().startswith(token.lower())
            or item['head'].lower().startswith(token.lower())]
        if (len(matches) == 1
                and matches[0]['candidate_id'].lower().startswith(next_candidate_ref.lower())):
            explicit_next_candidate = True
            next_candidate_id = matches[0]['candidate_id']
            break
    if (not any(target_id.lower().startswith(ref.lower()) for ref in work_refs)
            or not explicit_next_candidate):
        return {'requested': False, 'reason': 'RULING_OBJECT_NOT_EXPLICIT_IN_USER_SOURCE'}
    work = _row(db.execute('SELECT * FROM works WHERE work_id=?', (target_id,)).fetchone())
    candidate_id = work.get('candidate_id') if work else None
    candidate = _row(db.execute('SELECT * FROM candidates WHERE candidate_id=?', (candidate_id,)).fetchone())
    next_candidate = _row(db.execute('SELECT * FROM candidates WHERE candidate_id=?', (next_candidate_id,)).fetchone())
    if (not work or not candidate or not next_candidate or work['run_id'] != original.get('run_id')
            or work['role'] != 'TESTER' or work['kind'] != 'supplemental_verification'
            or work['candidate_id'] != candidate_id or work['state'] not in ('QUEUED', 'RUNNING')
            or not candidate_id.lower().startswith(candidate_ref.lower())
            or candidate['run_id'] != work['run_id'] or candidate['state'] != 'CURRENT'
            or next_candidate['run_id'] != work['run_id'] or next_candidate['state'] != 'CURRENT'
            or next_candidate['candidate_id'] == candidate_id):
        return {'requested': False, 'reason': 'RULING_TARGET_NOT_CURRENT'}
    already = pending(db, target_id)
    if already:
        return {'requested': False, 'reason': 'RETIREMENT_ALREADY_PENDING',
                'work_id': target_id, 'event_id': already['event_id']}
    source_events = [_row(row) for row in db.execute(
        'SELECT * FROM events WHERE run_id=? AND work_id=? ORDER BY event_id',
        (work['run_id'], target_id))]
    decision = {'request_id': source.get('request_id'), 'event_id': resolution.get('event_id'),
        'recorded_event_id': original.get('event_id'),
        'resolved_event_id': resolution.get('event_id'), 'result': 'UNVERIFIED',
        'reason_code': 'TEST_RESULT_NONDETERMINISTIC', 'evidence_preserved': True,
        'preserved_source_event_ids': [event['event_id'] for event in source_events],
        'source_checkpoint_sha256': _hash(work['checkpoint']), 'rerun_authorized': False,
        'next_candidate_id': next_candidate_id, 'next_check': 'independent_test',
        'approval_blocked_until_next_check_passes': True}
    retirement_event = request(db, ledger, work,
        '用户裁决：保留既有验证证据，记录UNVERIFIED/TEST_RESULT_NONDETERMINISTIC并停止同候选重跑。',
        decision=decision)
    queued = []
    for row in db.execute("SELECT * FROM works WHERE run_id=? AND candidate_id=? AND role='TESTER' "
                          "AND kind='supplemental_verification' AND state='QUEUED' AND work_id!=?",
                          (work['run_id'], candidate_id, target_id)):
        duplicate = _row(row)
        if pending(db, duplicate['work_id']):
            continue
        event_id = request(db, ledger, duplicate,
            '用户裁决明确要求不再重跑；保留该排队工作的原证据并安全退休。')
        queued.append({'work_id': duplicate['work_id'], 'event_id': event_id})
    return {'requested': retirement_event is not None, 'work_id': target_id,
            'event_id': retirement_event, 'queued_duplicates': queued,
            'result': 'UNVERIFIED', 'reason_code': 'TEST_RESULT_NONDETERMINISTIC',
            'requires_user_approval': False}


def apply_resolved_decisions(ledger):
    """Translate explicit UNVERIFIED/no-rerun rulings into safe retire requests.

    The user's original native answer, resolved decision, target work and frozen
    candidate are all rechecked in one transaction. This requests the TESTER's
    own safe-point release; it never ends a live writer or changes test results.
    """
    from business_ledger import _row, _hash

    snapshot = ledger.snapshot()
    run_id = snapshot['current_run_id']
    if not run_id:
        return []
    recorded = {event['payload'].get('request_id'): event for event in snapshot['events']
                if event['run_id'] == run_id and event['kind'] == 'USER_DECISION_RECORDED'}
    existing = {event['payload'].get('decision_request_id') for event in snapshot['events']
                if event['run_id'] == run_id and event['kind'] == 'VERIFICATION_RETIREMENT_REQUESTED'}
    results = []
    for resolution in snapshot['events']:
        if (resolution['run_id'] != run_id or resolution['kind'] != 'USER_DECISION_RESOLVED'
                or resolution['payload'].get('decision_id') != 'close_verification'):
            continue
        request_id = resolution['payload'].get('request_id')
        if not request_id or request_id in existing or request_id not in recorded:
            continue
        source = recorded[request_id]['payload']
        def operation(db, source=source, resolution=resolution, request_id=request_id):
            resolved = _row(db.execute('SELECT * FROM events WHERE event_id=? AND kind=?',
                (resolution['event_id'], 'USER_DECISION_RESOLVED')).fetchone())
            original = _row(db.execute('SELECT * FROM events WHERE event_id=? AND kind=?',
                (recorded[request_id]['event_id'], 'USER_DECISION_RECORDED')).fetchone())
            if (not resolved or not original or original['payload'].get('answer') != source.get('answer')
                    or resolved['payload'].get('request_id') != request_id
                    or resolved['payload'].get('decision_id') != 'close_verification'):
                return {'requested': False, 'reason': 'DECISION_SOURCE_CHANGED'}
            return request_from_resolved_decision(db, ledger, original, resolved)

        results.append(ledger._command('apply_resolved_verification_decision',
            'verification-decision-v2:'+_hash([request_id, resolution['event_id']]),
            [request_id, resolution['event_id']], operation))
    return results


def request_for_candidate(db, ledger, candidate, reason, *, target_base=None):
    from business_ledger import _row
    works = [_row(row) for row in db.execute("SELECT * FROM works WHERE candidate_id=? "
            "AND kind='verification' AND (state!='ENDED' OR EXISTS (SELECT 1 FROM executions e WHERE e.work_id=works.work_id AND e.state!='ENDED'))", (candidate['candidate_id'],))]
    for work in works:
        request(db, ledger, work, reason, target_base=target_base)
    return works


def reconcile(ledger):
    from business_ledger import _row
    snapshot = ledger.snapshot()
    run_id = snapshot['current_run_id']
    candidates = {c['candidate_id']: c for c in snapshot['candidates']}
    requests = {e['work_id']: e for e in snapshot['events']
                if e['run_id'] == run_id and e['kind']=='VERIFICATION_RETIREMENT_REQUESTED'}
    retired = {e['work_id'] for e in snapshot['events']
               if e['run_id'] == run_id and e['kind'] == 'VERIFICATION_RETIRED'}
    results = []
    for work in snapshot['works']:
        if (work['run_id'] != run_id or work['kind'] not in ('verification','supplemental_verification')
                or work['work_id'] in retired):
            continue
        candidate = candidates.get(work['candidate_id'])
        request_event = requests.get(work['work_id'])
        empty = (work['run_id'] == run_id and work['kind'] == 'verification'
                 and candidate and candidate['base'] == candidate['head'])
        if not empty and not request_event:
            continue
        # Recheck every writer in the transaction; a changing generation cannot
        # turn an earlier idle observation into permission to steal a live work.
        def operation(db, work=work):
            current = ledger._one(db, 'works', 'work_id', work['work_id'])
            if current['kind'] not in ('verification','supplemental_verification'):
                return {'retired': False, 'reason': 'NOT_VERIFICATION_WORK'}
            candidate = ledger._one(db, 'candidates', 'candidate_id', current['candidate_id'])
            if current['kind'] == 'verification' and candidate['base'] == candidate['head']:
                request(db, ledger, current, 'EMPTY_CANDIDATE_HAS_NO_DEVELOPMENT_DELTA')
            retirement = pending(db, current['work_id'])
            if not retirement:
                return {'retired': False}
            live = db.execute("SELECT 1 FROM executions WHERE work_id=? AND state!='ENDED'",
                              (current['work_id'],)).fetchone()
            slot = ledger._one(db, 'role_current', 'role', current['role'])
            slot_writer_is_this_work = (slot['current_work_id'] == current['work_id']
                and bool(slot['writer_execution_id']))
            if current['active_execution_id'] or live or slot_writer_is_this_work:
                return {'retired': False, 'state': 'WAITING_FOR_NATIVE_WRITER_EXIT',
                        'requires_user_approval': False, 'work_id': current['work_id']}
            if current['state'] != 'ENDED':
                db.execute("UPDATE works SET state='ENDED',ended_at=? WHERE work_id=?",
                           (ledger.clock(), current['work_id']))
            db.execute('UPDATE role_current SET current_work_id=NULL WHERE role=? AND current_work_id=?',
                       (current['role'], current['work_id']))
            closure = retirement['payload'].get('decision')
            event_payload = {'candidate_id': current['candidate_id'],
                'reason': retirement['payload']['reason'], 'writer_cleared': True,
                'business_result': ({'result': closure['result'], 'reason_code': closure['reason_code'],
                    'evidence_preserved': closure['evidence_preserved'],
                    'preserved_source_event_ids': closure['preserved_source_event_ids'],
                    'source_checkpoint_sha256': closure['source_checkpoint_sha256'],
                    'rerun_authorized': closure['rerun_authorized'],
                    'next_candidate_id': closure['next_candidate_id'],
                    'next_check': closure['next_check'],
                    'approval_blocked_until_next_check_passes': closure[
                        'approval_blocked_until_next_check_passes']} if closure else None),
                'decision_request_id': retirement['payload'].get('decision_request_id'),
                'requires_user_approval': False}
            event = ledger._event(db, 'VERIFICATION_RETIRED', event_payload,
                run_id=current['run_id'], role=current['role'], work_id=current['work_id'])
            return {'retired': True, 'work_id': current['work_id'], 'event_id': event,
                    'business_result': event_payload['business_result']}
        version = [work['work_id'], work['state'], work['active_execution_id'], work['execution_generation'],
                   next(r['writer_execution_id'] for r in snapshot['roles'] if r['role']==work['role']),
                   [(e['execution_id'],e['state']) for e in snapshot['executions'] if e['work_id']==work['work_id']]]
        from business_ledger import _hash
        results.append(ledger._command('retire_obsolete_verification', 'verification-retire:'+_hash(version),
                                       [work['work_id']], operation))
    return results


def facts(snapshot, run_id):
    works = {w['work_id']: w for w in snapshot['works']}
    retired = {e['work_id']: e for e in snapshot['events']
               if e['run_id'] == run_id and e['kind'] == 'VERIFICATION_RETIRED'}
    return [{**e['payload'], 'event_id': e['event_id'], 'work_id': e['work_id'],
             'state': works[e['work_id']]['state'],
             'retirement_event_id': retired.get(e['work_id'], {}).get('event_id'),
             'retirement_result': retired.get(e['work_id'], {}).get('payload', {}).get('business_result')}
            for e in snapshot['events'] if e['run_id']==run_id and e['kind']=='VERIFICATION_RETIREMENT_REQUESTED'
            and e['work_id'] in works]
