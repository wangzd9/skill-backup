"""Derive exact candidate permits from an already accepted native merge boundary."""


def authorization(ledger, identity, candidate_id):
    from business_ledger import _deny,_row,_json,_hash,_Denied
    from workflow_task_merge_policy import reconcile
    reconcile(ledger)
    # A denied lookup is not a permanent verdict on this candidate. Bind its
    # receipt to the execution and authorization state, so a later grant can
    # be resolved without replaying a cached rejection/identity conflict.
    snapshot=ledger.snapshot()
    permission_version=_hash({'grants':snapshot['authorizations'],
        'expired':{g['authorization_id']:g['spec'].get('expires_at',0)<=ledger.clock()
                   for g in snapshot['authorizations']},
        'accepted':[e for e in snapshot['events'] if e['kind']=='REQUIREMENT_ACCEPTED']})
    def operation(db):
        work,_=ledger._identity(db,identity)
        candidate=ledger._one(db,'candidates','candidate_id',candidate_id)
        if work['role']!='COORDINATOR' or work['run_id']!=candidate['run_id']:
            _deny('COORDINATOR_REQUIRED')
        for row in db.execute("SELECT * FROM authorizations WHERE kind='merge' ORDER BY created_at DESC"):
            grant=_row(row);spec=grant['spec']
            if (grant['revoked_at'] is None and spec['expires_at']>ledger.clock()
                    and all(spec.get(key)==candidate[key] for key in ('run_id','task_id','attempt','base','ac_version'))):
                try:
                    ledger._authorization(db,grant['authorization_id'],'merge')
                except _Denied:
                    continue
                return {'authorization_id':grant['authorization_id']}
        from workflow_development_policy import derive as development_permit
        derived=development_permit(ledger,db,candidate)
        if derived:
            return derived
        from workflow_task_merge_policy import derive
        derived=derive(ledger,db,work,candidate)
        if derived:
            return derived
        events=[_row(r) for r in db.execute("SELECT * FROM events WHERE run_id=? AND kind='REQUIREMENT_ACCEPTED' ORDER BY event_id DESC",
                                           (candidate['run_id'],))]
        if not events or not events[0]['payload'].get('merge_policy'):
            _deny('ACCEPTED_REQUIREMENT_MERGE_POLICY_REQUIRED')
        accepted=events[0];policy=accepted['payload']['merge_policy']
        parent=ledger._one(db,'authorizations','authorization_id',candidate['run_id']+'-merge-policy')
        if parent['revoked_at'] is not None:
            _deny('REQUIREMENT_MERGE_POLICY_REVOKED')
        if policy['expires_at']<=ledger.clock() or candidate['task_id'] not in accepted['payload']['task_ids']:
            _deny('REQUIREMENT_MERGE_POLICY_EXPIRED_OR_OUTSIDE_SCOPE')
        identifier='accepted-merge-'+candidate_id
        prior=db.execute('SELECT * FROM authorizations WHERE authorization_id=?',(identifier,)).fetchone()
        if prior:
            _deny('ACCEPTED_CANDIDATE_MERGE_PERMIT_REVOKED_OR_EXPIRED')
        spec={key:candidate[key] for key in ('run_id','task_id','attempt','base','ac_version')}
        spec.update(repo=policy['repo'],branch=policy['branch'],expires_at=policy['expires_at'],
                    accepted_event_id=accepted['event_id'],parent_policy_id=parent['authorization_id'])
        db.execute('INSERT INTO authorizations VALUES(?,?,?,?,?,NULL)',
            (identifier,'merge',_json(spec),_json(accepted['payload']['provenance']),ledger.clock()))
        event=ledger._event(db,'ACCEPTED_MERGE_PERMIT_DERIVED',{'authorization_id':identifier,
            'candidate_id':candidate_id,'accepted_event_id':accepted['event_id'],'spec':spec},
            run_id=candidate['run_id'],role='COORDINATOR',work_id=work['work_id'])
        return {'authorization_id':identifier,'event_id':event}
    command_id='accepted-merge-permit:'+_hash([identity,candidate_id,permission_version])
    result=ledger._command('accepted_merge_permit',command_id,[identity,candidate_id,permission_version],operation)
    if result.get('status')=='DUPLICATE' and result.get('original_status') not in ('COMMITTED','DUPLICATE'):
        return {**result,'status':result['original_status'],'replayed':True}
    return result
