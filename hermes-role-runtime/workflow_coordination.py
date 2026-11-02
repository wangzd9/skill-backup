"""Accepted prerequisite scheduling and durable coordinator attention.

Selection is a native safe-point handoff, never a cross-run business identity.
No verdict, verification, merge permission or user pause is synthesized here.
"""
import hashlib
import json
import re


def active_handoffs(snapshot):
    closed = {e['payload']['handoff_id'] for e in snapshot['events']
              if e['kind'] in ('DEPENDENCY_RUN_RETURNED', 'DEPENDENCY_RUN_INTERRUPTED')}
    return [e for e in snapshot['events'] if e['kind'] == 'DEPENDENCY_RUN_SCHEDULED'
            and e['payload']['handoff_id'] not in closed]


def related_runs(snapshot, run_id):
    result = {run_id}
    for event in active_handoffs(snapshot):
        pair = {event['payload']['consumer_run_id'], event['payload']['upstream_run_id']}
        if run_id in pair:
            result.update(pair)
    return result


def questions(snapshot, run_id):
    runs = related_runs(snapshot, run_id)
    resolved = {e['payload']['decision_id'] for e in snapshot['events']
                if e['run_id'] in runs and e['kind'] == 'USER_DECISION_RESOLVED'}
    return [e for e in snapshot['events'] if e['run_id'] in runs
            and e['kind'] == 'USER_DECISION_REQUESTED' and e['payload']['decision_id'] not in resolved]


def request_questions(ledger, db, work, values):
    from business_ledger import _deny, _row
    if (not isinstance(values, list) or len(values) > 8 or any(
            not isinstance(v, dict) or set(v) != {'decision_id', 'question', 'options', 'reason'}
            or any(not isinstance(v.get(k), str) or not v[k].strip() or len(v[k]) > 600
                   for k in ('decision_id', 'question', 'reason'))
            or not isinstance(v.get('options'), list) or not 2 <= len(v['options']) <= 6
            or any(not isinstance(o, str) or not o.strip() or len(o) > 300 for o in v['options'])
            for v in values)):
        _deny('STRUCTURED_USER_QUESTIONS_REQUIRED')
    if values and work['role'] != 'COORDINATOR':
        _deny('USER_QUESTIONS_REQUIRE_COORDINATOR')
    for value in values:
        prior = _row(db.execute("SELECT * FROM events WHERE run_id=? AND kind='USER_DECISION_REQUESTED' "
            "AND json_extract(payload,'$.decision_id')=? ORDER BY event_id DESC LIMIT 1",
            (work['run_id'], value['decision_id'])).fetchone())
        if prior:
            if prior['payload'] != value:
                _deny('USER_QUESTION_ID_CONTENT_CHANGED')
            continue
        ledger._event(db, 'USER_DECISION_REQUESTED', value, run_id=work['run_id'],
                      work_id=work['work_id'], role='COORDINATOR', notify=True)


def _automated(snapshot, run, now):
    from workflow_task_merge_policy import live
    grants = {g['authorization_id']: g for g in snapshot['authorizations']}
    allowed = set()
    for grant in grants.values():
        spec = grant['spec']
        if (grant['kind'] == 'development_policy' and spec.get('run_id') == run['run_id']
                and spec.get('repo') == run['repo'] and live(grants, grant['authorization_id'], now)):
            allowed.update(spec['task_ids'])
    tasks = {t['task_id'] for t in snapshot['tasks'] if t['run_id'] == run['run_id']}
    return bool(tasks) and tasks <= allowed


def dependencies(snapshot, run_id, now):
    """Only frozen dependency inputs plus an exact requirement reference qualify."""
    run = next((r for r in snapshot['runs'] if r['run_id'] == run_id), None)
    if not run or run['control'] != 'ACTIVE' or not _automated(snapshot, run, now):
        return []
    waiting = [w for w in snapshot['works'] if w['run_id'] == run_id
               and w['state'] != 'ENDED' and w['checkpoint'].get('waiting_external')]
    if not waiting:
        return []
    result = []
    for task in snapshot['tasks']:
        if task['run_id'] != run_id or task['phase'] in ('COMPLETED', 'CANCELLED'):
            continue
        if not any(w['kind'] == 'coordination' or w.get('task_id') == task['task_id'] for w in waiting):
            continue
        contract = task.get('contract') or {}
        inputs = str(contract.get('inputs') or '')
        material = json.dumps(contract, ensure_ascii=False)
        for upstream in snapshot['runs']:
            if (upstream['run_id'] == run_id or upstream['repo'] != run['repo']
                    or upstream['control'] not in ('ACTIVE', 'DRAINING')
                    or not _automated(snapshot, upstream, now)):
                continue
            name = upstream['requirement_id']
            explicit = re.search(r'(?<![A-Za-z0-9_-])' + re.escape(name) + r'(?![A-Za-z0-9_-])', material)
            if not explicit or not (name in inputs or re.search(r'\bDEP-[A-Z0-9-]+', inputs)):
                continue
            tasks = [t for t in snapshot['tasks'] if t['run_id'] == upstream['run_id']]
            unfinished = [t['task_id'] for t in tasks if t['phase'] != 'COMPLETED']
            if any(t['phase'] == 'CANCELLED' for t in tasks):
                continue
            if not unfinished:
                from workflow_completion import facts as completion_facts
                completion = completion_facts(snapshot, upstream['run_id'])
                if not completion.get('plan') or completion['state'] == 'COMPLETED':
                    continue
            result.append({'consumer_run_id': run_id, 'consumer_task_id': task['task_id'],
                           'upstream_run_id': upstream['run_id'], 'control': upstream['control'],
                           'required_task_ids': [t['task_id'] for t in tasks],
                           'unfinished_task_ids': unfinished,
                           'owner': 'COORDINATOR', 'requires_user_approval': False,
                           'reason': '冻结输入依赖已接受的本地开发需求；后台在写执行全部核退后接续前置需求，完成后返回。'})
    return result


def facts(snapshot, run_id, now):
    from workflow_user_decisions import pending
    related = related_runs(snapshot, run_id)
    from workflow_query import review_action_projection
    review_ids = {a['action_id'] for related_run in related
                  for a in review_action_projection(snapshot, related_run)['attention']}
    review_events = {e['payload']['action_id']: e for e in snapshot['events']
                     if e['kind'] == 'SERVICE_ARTIFACT_RECORDED'}
    return {'dependencies': dependencies(snapshot, run_id, now),
            'user_questions': [{'event_id': e['event_id'], **e['payload']} for e in questions(snapshot, run_id)],
            'handoffs': [e['payload'] for e in active_handoffs(snapshot)
                         if run_id in (e['payload']['consumer_run_id'], e['payload']['upstream_run_id'])],
            'pending_answers': [{'request_id': e['payload']['request_id'], 'event_id': e['event_id'],
                                 'run_id': e['run_id'], 'state': 'PENDING_COORDINATOR_REVIEW'}
                                for e in pending(snapshot, run_id)],
            'reviews': [{'run_id': c['run_id'], 'task_id': c['task_id'], 'candidate_id': c['candidate_id'],
                         'action_id': a['action_id'], 'state': a['state'], 'owner': 'COORDINATOR',
                         'event_id': review_events.get(a['action_id'], {}).get('event_id'),
                         'requires_user_approval': False}
                        for a in snapshot['external_actions'] for c in snapshot['candidates']
                        if a['candidate_id'] == c['candidate_id'] and c['state'] == 'CURRENT'
                        and a['action_id'] in review_ids
                        and c['run_id'] in related and a['state'] in ('RESULT_READY', 'INCOMPLETE')]}


def admission_barrier(ledger, role, work):
    """Let existing writers finish without idle polls starving a safe handoff.

    Native prepare checks this under the same admission lock as the scheduler.
    No writer or execution is changed, and recovery work remains admissible.
    """
    from runtime_common import read_json
    policy = read_json(ledger.path.parent / 'coordination-policy.json', {})
    if policy.get('version') != 1 or policy.get('enabled') is not True:
        return None
    snapshot = ledger.snapshot()
    selected = snapshot['current_run_id']
    if work and (work['run_id'] != selected or work['state'] != 'QUEUED'
                 or work.get('active_execution_id')):
        return None
    if any(a['state'] in ('SUBMITTED', 'UNKNOWN') for a in snapshot['external_actions']):
        return None
    from workflow_final_delivery import states
    if any(r['state'] in ('RUNNING', 'UNVERIFIED') for r in states(snapshot, selected)):
        return None
    if role == 'COORDINATOR':
        from workflow_user_decisions import pending
        if pending(snapshot, selected):
            return None
    active = active_handoffs(snapshot)
    if active:
        handoff = active[-1]['payload']
        if selected != handoff['upstream_run_id'] or any(
                e['event_id'] > active[-1]['event_id'] and e['kind'] == 'CONTROL_APPLIED'
                and e['payload'].get('action') == 'select_run' for e in snapshot['events']):
            return None
        tasks = [t for t in snapshot['tasks'] if t['run_id'] == selected
                 and t['task_id'] in handoff['required_task_ids']]
        if len(tasks) != len(handoff['required_task_ids']) or any(t['phase'] != 'COMPLETED' for t in tasks):
            return None
        from workflow_user_decisions import pending
        if pending(snapshot, selected) or questions(snapshot, selected):
            return None
        consumer = next(r for r in snapshot['runs'] if r['run_id'] == handoff['consumer_run_id'])
        if consumer['control'] != 'DRAINING' or not _automated(snapshot, consumer, ledger.clock()):
            return None
        from workflow_completion import facts as completion_facts
        completion = completion_facts(snapshot, selected)
        if completion.get('plan') and completion['state'] != 'COMPLETED':
            return None
        destination = handoff['consumer_run_id']
    else:
        choices = dependencies(snapshot, selected, ledger.clock())
        if not choices:
            return None
        destination = choices[0]['upstream_run_id']
    return {'can_start_model': False, 'reason': 'DEPENDENCY_HANDOFF_PENDING',
            'destination': destination, 'writer_clearance_required': True,
            'requires_user_approval': False}


def reconcile(ledger):
    """Run only from the existing scheduler, under the ledger transaction."""
    from runtime_common import read_json
    policy = read_json(ledger.path.parent / 'coordination-policy.json', {})
    if policy.get('version') != 1 or policy.get('enabled') is not True:
        return {'state': 'DEPENDENCY_SCHEDULER_DISABLED'}
    def operation(db):
        snapshot = ledger.snapshot()
        selected = snapshot['current_run_id']
        if snapshot['mode'] != 'ACTIVE' or not snapshot['admission_open'] or not selected:
            return {'state': 'CONTROL_BLOCKED'}
        selected_run = next(r for r in snapshot['runs'] if r['run_id'] == selected)
        if selected_run['control'] != 'ACTIVE':
            return {'state': 'CONTROL_BLOCKED'}
        active = active_handoffs(snapshot)
        handoff = active[-1]['payload'] if active else None
        if handoff:
            explicit_selection = any(e['event_id'] > active[-1]['event_id']
                and e['kind'] == 'CONTROL_APPLIED' and e['payload'].get('action') == 'select_run'
                for e in snapshot['events'])
            if selected != handoff['upstream_run_id'] or explicit_selection:
                event = ledger._event(db, 'DEPENDENCY_RUN_INTERRUPTED', {**handoff,
                    'reason': 'Native selection changed; no automatic override'}, role='COORDINATOR')
                return {'state': 'INTERRUPTED', 'event_id': event}
            upstream_tasks = [t for t in snapshot['tasks'] if t['run_id'] == selected
                              and t['task_id'] in handoff['required_task_ids']]
            if (len(upstream_tasks) != len(handoff['required_task_ids'])
                    or any(t['phase'] != 'COMPLETED' for t in upstream_tasks)):
                return {'state': 'UPSTREAM_IN_PROGRESS', 'handoff_id': handoff['handoff_id']}
            from workflow_user_decisions import pending
            if pending(snapshot, selected) or questions(snapshot, selected):
                return {'state': 'UPSTREAM_USER_DECISION_PENDING', 'handoff_id': handoff['handoff_id']}
            from workflow_completion import facts as completion_facts
            completion = completion_facts(snapshot, selected)
            if completion.get('plan') and completion['state'] != 'COMPLETED':
                return {'state': 'UPSTREAM_FINAL_ACCEPTANCE_PENDING', 'handoff_id': handoff['handoff_id']}
            destination = handoff['consumer_run_id']
            consumer = next(r for r in snapshot['runs'] if r['run_id'] == destination)
            if consumer['control'] != 'DRAINING' or not _automated(snapshot, consumer, ledger.clock()):
                event = ledger._event(db, 'DEPENDENCY_RUN_INTERRUPTED', {**handoff,
                    'reason': 'Consumer control or accepted policy changed'}, role='COORDINATOR')
                return {'state': 'INTERRUPTED', 'event_id': event}
            kind = 'DEPENDENCY_RUN_RETURNED'
        else:
            choices = dependencies(snapshot, selected, ledger.clock())
            if not choices:
                return {'state': 'NO_DEPENDENCY_HANDOFF'}
            handoff = choices[0]
            handoff['handoff_id'] = 'dependency-' + hashlib.sha256(
                json.dumps(handoff, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:32]
            destination = handoff['upstream_run_id']
            kind = 'DEPENDENCY_RUN_SCHEDULED'
        # Never clear a writer, terminate a process, or infer absence of effects.
        if (any(e['state'] in ('ACTIVE', 'DRAINING') for e in snapshot['executions'])
                or any(r.get('writer_execution_id') for r in snapshot['roles'])
                or any(a['state'] in ('SUBMITTED', 'UNKNOWN') for a in snapshot['external_actions'])):
            return {'state': 'WAITING_SAFE_POINT', 'destination': destination}
        from workflow_final_delivery import states
        if any(r['state'] in ('RUNNING', 'UNVERIFIED') for r in states(snapshot, selected)):
            return {'state': 'WAITING_DELIVERY', 'destination': destination}
        # Park exact queued work IDs, checkpoints, private trees and ACK cursors.
        # bind_execution will restore these objects when this run is selected.
        for row in snapshot['roles']:
            if not row.get('current_work_id'):
                continue
            work = ledger._one(db, 'works', 'work_id', row['current_work_id'])
            if work['run_id'] != selected:
                return {'state': 'WAITING_FOREIGN_WORK_DRAIN', 'work_id': work['work_id']}
            if work['state'] != 'QUEUED' or work['active_execution_id']:
                return {'state': 'WORK_NOT_AT_SAFE_POINT', 'work_id': work['work_id']}
        parked = [r['current_work_id'] for r in snapshot['roles'] if r.get('current_work_id')]
        db.execute('UPDATE role_current SET current_work_id=NULL WHERE writer_execution_id IS NULL')
        selection = ledger._select(db, destination)
        event = ledger._event(db, kind, {**handoff, 'parked_work_ids': parked,
            'selection_event_id': selection.get('event_id'), 'candidate_gates_unchanged': True},
            role='COORDINATOR', notify=True)
        return {'state': kind, 'event_id': event, 'destination': destination,
                'handoff_id': handoff['handoff_id']}
    # Changed facts must be reevaluated. This is not a replayable business command.
    with ledger._connection() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            result = operation(db)
            db.commit()
            return result
        except BaseException:
            db.rollback()
            raise


def attention(ledger):
    """One notice per unresolved fact set; quiet hours defer, rather than lose it."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    hour = datetime.fromtimestamp(ledger.clock(), ZoneInfo('Asia/Shanghai')).hour
    if hour < 8 or hour >= 21:
        return []
    snapshot = ledger.snapshot()
    run_id = snapshot['current_run_id']
    if not run_id or snapshot['mode'] != 'ACTIVE':
        return []
    view = facts(snapshot, run_id, ledger.clock())
    # Wait declarations are retained verbatim as evidence. They are not proof
    # of missing user permission; the owner must classify and reconcile them.
    waits = [{'work_id': w['work_id'], 'role': w['role'], 'conditions': w['checkpoint']['waiting_external']}
             for w in snapshot['works'] if w['run_id'] == run_id and w['state'] != 'ENDED'
             and w['checkpoint'].get('waiting_external')]
    payload = {**view, 'waiting': waits, 'run_id': run_id,
               'owner': 'COORDINATOR', 'notice': '内部审查由协调者处理；仅真实范围、授权或合同歧义需要用户决定。'}
    # Changing timestamps/round numbers in prose must not defeat deduplication.
    identity = {k: view[k] for k in ('dependencies', 'pending_answers', 'reviews', 'user_questions')}
    identity['waiting_work_ids'] = [w['work_id'] for w in waits]
    pending_ids = {v['event_id'] for key in ('pending_answers', 'user_questions') for v in view[key]}
    pending_ids.update(r['event_id'] for r in view['reviews'] if r.get('event_id') is not None)
    timestamps = [e['created_at'] for e in snapshot['events'] if e['event_id'] in pending_ids]
    age = max(0, ledger.clock() - min(timestamps)) if timestamps else 0
    identity['escalation_level'] = 2 if age >= 3600 else 1 if age >= 900 else 0
    payload['escalation_level'] = identity['escalation_level']
    if not (waits or any(view[k] for k in ('dependencies', 'pending_answers', 'reviews', 'user_questions'))):
        return []
    digest = hashlib.sha256(json.dumps([run_id, identity], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    def operation(db):
        event = ledger._event(db, 'COORDINATION_ATTENTION_REQUIRED', payload,
                              run_id=run_id, role='COORDINATOR', notify=True)
        return {'event_id': event, 'state': 'PENDING_DELIVERY'}
    return [ledger._command('coordination_attention', 'coordination-attention:' + digest,
                            [run_id, identity], operation)]


def notice_current(ledger, item):
    snapshot = ledger.snapshot()
    if item['kind'] == 'USER_DECISION_REQUESTED':
        return any(e['event_id'] == item['event_id'] for e in questions(snapshot, item['run_id']))
    if item['kind'] != 'COORDINATION_ATTENTION_REQUIRED':
        return True
    if item['run_id'] not in related_runs(snapshot, snapshot['current_run_id']):
        return False
    current = facts(snapshot, item['run_id'], ledger.clock())
    old = item['payload']
    for key, identifier in (('pending_answers', 'request_id'), ('user_questions', 'decision_id'),
                            ('reviews', 'action_id'), ('dependencies', 'upstream_run_id')):
        if {v[identifier] for v in old.get(key, [])} & {v[identifier] for v in current[key]}:
            return True
    waiting_ids = {v['work_id'] for v in old.get('waiting', [])}
    return any(w['work_id'] in waiting_ids and w['state'] != 'ENDED'
               and w['checkpoint'].get('waiting_external') for w in snapshot['works'])


def parked_work(db, run_id, role):
    from business_ledger import _row
    return _row(db.execute("SELECT w.* FROM works w WHERE w.run_id=? AND w.role=? AND w.state='QUEUED' "
        "AND w.active_execution_id IS NULL AND NOT EXISTS (SELECT 1 FROM executions x WHERE x.work_id=w.work_id AND x.state!='ENDED') "
        "AND EXISTS (SELECT 1 FROM events e,json_each(e.payload,'$.parked_work_ids') p "
        "WHERE e.kind IN ('DEPENDENCY_RUN_SCHEDULED','DEPENDENCY_RUN_RETURNED') AND p.value=w.work_id) "
        "ORDER BY w.created_at LIMIT 1", (run_id, role)).fetchone())
