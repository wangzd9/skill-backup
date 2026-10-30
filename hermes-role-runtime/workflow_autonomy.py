"""Bounded workflow capabilities and auditable repairs, never synthetic success."""
from pathlib import Path


def covers(spec, candidate, kind):
    if spec.get('service_kind') != kind:
        return False
    if spec.get('candidate_id'):
        return spec['candidate_id'] == candidate['candidate_id']
    return (spec.get('run_id') == candidate['run_id']
            and candidate['task_id'] in spec.get('task_ids', []))


def select_grant(ledger, candidate, kind):
    snapshot = ledger.snapshot(candidate['run_id'])
    exhausted = False
    for grant in reversed(snapshot['authorizations']):
        if (grant['kind']=='external' and grant['revoked_at'] is None
                and grant['spec']['expires_at'] > ledger.clock()
                and covers(grant['spec'], candidate, kind)):
            used = sum(row['state']!='REJECTED' and row['authorization'].get('authorization_id')==grant['authorization_id']
                       for row in snapshot['external_actions'])
            if used < grant['spec']['max_calls']:
                return {'authorization_id':grant['authorization_id']}
            exhausted = True
    if exhausted:
        raise ValueError('SERVICE_GRANT_CALL_LIMIT: 额度用尽，需提额度')
    raise ValueError('SERVICE_POLICY_OR_EXPLICIT_AUTHORIZATION_REQUIRED')


def extend_scope(ledger, identity, task_id, additions, reason, *, command_id):
    from business_ledger import _deny, _row, _json, _hash
    def operation(db):
        work, _ = ledger._identity(db, identity)
        if work['role']!='COORDINATOR':
            _deny('COORDINATOR_REQUIRED')
        task = _row(db.execute('SELECT * FROM tasks WHERE run_id=? AND task_id=?',
                              (work['run_id'],task_id)).fetchone())
        if not task or task['phase'] not in ('PENDING','DEVELOPING'):
            _deny('SCOPE_AMENDMENT_REQUIRES_UNDELIVERED_TASK')
        if (not isinstance(additions,list) or not additions or len(additions)>100
                or any(not isinstance(v,str) or not v or Path(v).is_absolute()
                       or '..' in Path(v).parts or '.git' in Path(v).parts or v in ('.','*','**') for v in additions)
                or not isinstance(reason,str) or not reason.strip()):
            _deny('EXPLICIT_SCOPE_ADDITIONS_AND_REASON_REQUIRED')
        siblings = [_row(r) for r in db.execute('SELECT * FROM tasks WHERE run_id=? AND task_id!=?',
                                               (work['run_id'],task_id))]
        if any(ledger._scope_overlap(additions,t['scope']) and t['phase'] not in ('COMPLETED','CANCELLED') for t in siblings):
            _deny('SCOPE_AMENDMENT_CONFLICTS_WITH_OTHER_TASK')
        updated = list(dict.fromkeys(task['scope']+additions))
        db.execute('UPDATE tasks SET scope=? WHERE run_id=? AND task_id=?',(_json(updated),work['run_id'],task_id))
        event=ledger._event(db,'TASK_SCOPE_EXTENDED',{'task_id':task_id,'additions':additions,
            'previous_scope_sha256':_hash(task['scope']),'effective_scope_sha256':_hash(updated),
            'original_plan_unchanged':True,'reason':reason},run_id=work['run_id'],role='COORDINATOR',work_id=work['work_id'])
        return {'task_id':task_id,'scope':updated,'event_id':event,'original_plan_unchanged':True}
    return ledger._command('extend_task_scope',command_id,[identity,task_id,additions,reason],operation)


def sweep(ledger):
    """Repair only schema faults with deterministic evidence; unknown effects stay fenced."""
    from business_ledger import _row, _json
    with ledger._connection() as db:
        invalid=[_row(row) for row in db.execute("SELECT * FROM works WHERE state!='ENDED'")
                 if not isinstance(_row(row)['checkpoint'].get('waiting_external',[]),list)]
    results=[]
    from workflow_verification_lifecycle import apply_resolved_decisions
    results.extend(apply_resolved_decisions(ledger))
    from workflow_task_merge_policy import reconcile
    results.extend(reconcile(ledger))
    from workflow_verification_exceptions import recover, reconcile as reconcile_exceptions
    results.extend(recover(ledger))
    from workflow_verification_lifecycle import reconcile as retire_verifications
    results.extend(retire_verifications(ledger))
    results.extend(reconcile_exceptions(ledger))
    from workflow_supplemental import queue_repairs
    queue_repairs(ledger)
    from workflow_completion import reconcile as reconcile_completion
    results.extend(reconcile_completion(ledger))
    for row in invalid:
        def operation(db, row=row):
            current=ledger._one(db,'works','work_id',row['work_id'])
            if isinstance(current['checkpoint'].get('waiting_external',[]),list):
                return {'already_repaired':True}
            checkpoint={**current['checkpoint']}
            previous=checkpoint.pop('waiting_external')
            checkpoint['legacy_waiting_external']=previous
            checkpoint['waiting_external']=[]
            checkpoint['mechanism_repair_required']='LEGACY_WAIT_CONTRACT_INVALID'
            db.execute('UPDATE works SET checkpoint=? WHERE work_id=?',(_json(checkpoint),row['work_id']))
            event=ledger._event(db,'WAIT_CONTRACT_REPAIRED',{'work_id':row['work_id'],
                'original_retained':True,'business_success':None},run_id=row['run_id'],role=row['role'],work_id=row['work_id'])
            return {'work_id':row['work_id'],'event_id':event}
        results.append(ledger._command('repair_wait_contract','repair-wait:'+row['work_id'],[row['work_id']],operation))
    with ledger._connection() as db:
        rejected=[_row(row) for row in db.execute("SELECT * FROM external_actions WHERE state IN ('SUBMITTED','UNKNOWN')")]
    from runtime_common import read_json
    import hashlib
    for action in rejected[:20]:
        directory=ledger.path.parent/'services'/hashlib.sha256(action['action_id'].encode()).hexdigest()
        value=read_json(directory/'invocation.json',{})
        # Only the adapter's recorded pre-dispatch refusal establishes zero
        # effects. Absence of a reference alone can be a crash after dispatch.
        if value.get('state')=='REJECTED' and not value.get('reference'):
            results.append(ledger.reject_external_action(action['action_id'],
                reason='恢复服务提交前已落盘的零副作用拒绝回执',command_id='repair-rejection:'+action['action_id']))
    return results
