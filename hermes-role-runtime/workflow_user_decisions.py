"""Durable user answers, separate from acceptance, permissions and gate closure."""
import re

from manual_access import full_permission_action


def answer_source(text):
    if not isinstance(text, str) or len(text) > 8192:
        return None
    action = full_permission_action(text)
    primary = action if action is not None else text.strip()
    # Keep quoted/questions and negations out, but accept an explicit Chinese
    # ruling sentence as well as the older short-choice form. The native
    # gateway still supplies and verifies the message provenance; this only
    # broadens semantic recognition after that verification.
    if re.search(r'[?？`\n]|^(?:引用|如果|假如|不要|暂不|是否|能否)', primary):
        return None
    short = re.fullmatch(r'(?:请)?(?:按|采用|选择)(?:解读|方案|选项)\s*([a-zA-Z0-9])\s*[。！!]?', primary)
    if short:
        return {'answer': primary, 'choice': short[1].lower(), 'decision_id': None}
    explicit = re.fullmatch(r'([A-Za-z][A-Za-z0-9_.:-]{0,159})\s+(?:请)?(?:按|采用|选择|裁决为)\s*(\S.*)', primary)
    if explicit:
        return {'answer': primary, 'choice': None, 'decision_id': explicit[1]}
    close_request = re.match(
        r'^(?:(?:裁决|决定)\s*[:：]\s*)?(?:请)?对\s+work-[0-9a-f]{8,32}\s+执行\s+close_verification\b',
        primary, re.I)
    if (close_request and re.search(r'结果(?:记录|记)为\s*UNVERIFIED\b', primary, re.I)
            and 'TEST_RESULT_NONDETERMINISTIC' in primary
            and any(phrase in primary for phrase in ('不再重跑', '不再重新运行', '不要重跑'))
            and 'independent_test' in primary
            and any(phrase in primary for phrase in ('不要 APPROVE', '不 APPROVE', '不得 APPROVE'))):
        return {'answer': primary, 'choice': None, 'decision_id': 'close_verification'}
    return None


def record(ledger, target, provenance, *, command_id):
    parsed = answer_source(provenance.get('primary_text'))
    if (not parsed or provenance.get('authenticated') is not True or
            any(not provenance.get(key) for key in ('chat_id', 'message_id', 'user_id'))):
        raise ValueError('EXPLICIT_NATIVE_USER_DECISION_REQUIRED')
    if not isinstance(target, dict) or set(target)-{'run_id', 'decision_id'}:
        raise ValueError('EXACT_USER_DECISION_TARGET_REQUIRED')
    if target.get('decision_id') and target['decision_id'] != parsed['decision_id']:
        # A short answer is still saved; a model cannot fabricate its referent.
        raise ValueError('DECISION_REFERENCE_NOT_IN_PRIMARY_SOURCE')
    run_id = target.get('run_id') or ledger.mode()['current_run_id']
    from business_ledger import _deny, _row
    def operation(db):
        if run_id != ledger.mode()['current_run_id']:
            _deny('USER_DECISION_OUTSIDE_CURRENT_RUN')
        ledger._one(db, 'runs', 'run_id', run_id)
        context_at = (provenance.get('recovery') or {}).get('original_at', ledger.clock())
        row = _row(db.execute("SELECT * FROM events WHERE run_id=? AND role='COORDINATOR' "
                             "AND kind='CHECKPOINT_SAVED' AND created_at<=? ORDER BY event_id DESC LIMIT 1",
                             (run_id,context_at)).fetchone())
        context = {'event_id': row['event_id'], 'checkpoint': row['payload'].get('checkpoint', {})} if row else None
        request_id = 'user-decision:' + provenance['chat_id'] + ':' + provenance['message_id']
        event_id = ledger._event(db, 'USER_DECISION_RECORDED', {
            'request_id': request_id, **parsed, 'provenance': provenance, 'context': context,
            'state': 'PENDING_COORDINATOR_REVIEW',
            'notice': '已登记原文，尚未核对语义或关闭门禁；短答复由协调者对照原提问关联，不能当作新授权。'},
            run_id=run_id, role='COORDINATOR')
        return {'request_id': request_id, 'event_id': event_id,
                'state': 'PENDING_COORDINATOR_REVIEW', 'decision_id': parsed['decision_id']}
    source_identity = {key: provenance.get(key) for key in
                       ('platform','chat_id','message_id','user_id','primary_text')}
    return ledger._command('record_user_decision', command_id, [run_id, parsed, source_identity], operation)


def pending(snapshot, run_id):
    events = [e for e in snapshot['events'] if e['run_id'] == run_id]
    resolved = {e['payload']['request_id'] for e in events if e['kind'] == 'USER_DECISION_RESOLVED'}
    return [e for e in events if e['kind'] == 'USER_DECISION_RECORDED'
            and e['payload']['request_id'] not in resolved]


def resolve(ledger, identity, payload, *, command_id):
    from business_ledger import _deny, _row
    def operation(db):
        work, _ = ledger._identity(db, identity)
        if work['role'] != 'COORDINATOR':
            _deny('COORDINATOR_REQUIRED')
        events = [_row(row) for row in db.execute(
            "SELECT * FROM events WHERE run_id=? AND kind='USER_DECISION_RECORDED'", (work['run_id'],))]
        recorded = next((e for e in events if e['payload']['request_id'] == payload['request_id']), None)
        if not recorded:
            _deny('USER_DECISION_REQUEST_NOT_FOUND')
        original_id = recorded['payload'].get('decision_id')
        if original_id and payload['decision_id'] != original_id:
            _deny('USER_DECISION_REFERENCE_MISMATCH')
        # Reconcile the answer explicitly. This event does not approve a
        # candidate, modify AC, grant service/push rights or fabricate a PASS.
        existing = [_row(row) for row in db.execute(
            "SELECT * FROM events WHERE run_id=? AND kind='USER_DECISION_RESOLVED'", (work['run_id'],))]
        if any(e['payload']['request_id'] == payload['request_id'] for e in existing):
            _deny('USER_DECISION_ALREADY_RESOLVED')
        event_id = ledger._event(db, 'USER_DECISION_RESOLVED', payload,
            run_id=work['run_id'], work_id=work['work_id'], role='COORDINATOR')
        result={'event_id': event_id, 'request_id': payload['request_id'], 'state': 'RESOLVED'}
        if payload['decision_id']=='close_verification':
            from workflow_verification_lifecycle import request_from_resolved_decision
            resolved=_row(db.execute('SELECT * FROM events WHERE event_id=?',(event_id,)).fetchone())
            result['retirement']=request_from_resolved_decision(db,ledger,recorded,resolved)
        return result
    return ledger._command('resolve_user_decision', command_id, [identity, payload], operation)


def recover_native_answer(pool_path, state_db, row_id, *, expected_run_id):
    """Maintenance-only replay of a lost native answer, with original message ID.

    No general permission can be recovered this way: record only creates a
    pending answer. The original coordinator still reviews and applies it.
    """
    import hashlib
    import json
    from pathlib import Path
    import sqlite3
    import workflow_runtime as workflow
    with sqlite3.connect('file:'+str(Path(state_db).resolve())+'?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        row = db.execute('SELECT m.*,s.source,s.user_id,s.chat_id FROM messages m JOIN sessions s '
                         'ON m.session_id=s.id WHERE m.id=?', (row_id,)).fetchone()
    pool = json.loads(Path(pool_path).read_text())
    if (not row or row['role'] != 'user' or row['source'] != 'feishu' or
            row['chat_id'] != pool['roles']['COORDINATOR']['chat_id'] or
            not row['platform_message_id'] or not row['user_id']):
        raise ValueError('NATIVE_USER_ANSWER_HISTORY_NOT_VERIFIED')
    prefix = '[角色群只读/控制请求] 角色：COORDINATOR\n'
    marker = '\n用户原文及附件/引用如下：\n'
    content = row['content']
    if not content.startswith(prefix) or content.count(marker) != 1:
        raise ValueError('NATIVE_PRIMARY_ANSWER_ENVELOPE_REQUIRED')
    primary = content.split(marker, 1)[1]
    if not answer_source(primary):
        raise ValueError('NATIVE_HISTORY_NOT_A_DECISION_ANSWER')
    if workflow.mode(pool_path)['current_run_id'] != expected_run_id:
        raise ValueError('HISTORICAL_ANSWER_RUN_CHANGED')
    provenance = {'platform':'feishu','chat_id':row['chat_id'],'user_id':row['user_id'],
        'message_id':row['platform_message_id'],'primary_text':primary,'authenticated':True,
        'current_full':False,'role':'COORDINATOR','source_kind':'native_history_recovery',
        'recovery':{'message_db':str(Path(state_db).resolve()),'row_id':row_id,
                    'session_id':row['session_id'],'original_at':row['timestamp'],
                    'content_sha256':hashlib.sha256(content.encode()).hexdigest()}}
    return workflow.submit_control(pool_path,'COORDINATOR','record_user_decision',
                                   {'run_id':expected_run_id},provenance)
