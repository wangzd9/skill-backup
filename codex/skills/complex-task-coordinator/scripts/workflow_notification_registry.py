"""Business outbox contracts shared by producers, renderer and consumer."""

REGISTRY={kind:{'producer':producer,'renderer':'workflow_reports.render_notification',
    'consumer':'workflow_notifications.drain_business_notifications','target':'event_role',
    'quiet':'suppress','max_pending_age_seconds':1800 if kind.startswith(('INTEGRATION_','FINAL_','REQUIREMENT_','VERIFICATION_EXCEPTION_')) else None}
    for kind,producer in {
        'WORK_CLAIMED':'business_ledger','CANDIDATE_DELIVERED':'business_ledger',
        'CANDIDATE_DECIDED':'business_ledger','COORDINATOR_PROGRESS':'workflow_reports',
        'SERVICE_ARTIFACT_RECORDED':'business_ledger','EXTERNAL_ACTION_REJECTED':'business_ledger',
        'INTEGRATION_VERIFICATION_QUEUED':'workflow_completion',
        'INTEGRATION_VERIFICATION_REPORTED':'workflow_completion',
        'FINAL_ACCEPTANCE_REWORK_QUEUED':'workflow_completion_repair',
        'VERIFICATION_EXCEPTION_REPAIR_QUEUED':'workflow_verification_exceptions',
        'REQUIREMENT_COMPLETED':'workflow_completion'}.items()}


def diagnostics(ledger):
    now=ledger.clock()
    with ledger._connection() as db:
        groups=[dict(r) for r in db.execute('SELECT state,kind,COUNT(*) AS count,MIN(created_at) AS oldest_at '
            'FROM outbox GROUP BY state,kind ORDER BY state,kind')]
    samples=ledger.outbox_items(states=('PENDING','UNKNOWN'),limit=256)
    def owner(kind):
        if kind=='PERIODIC_SUMMARY':return 'workflow_reports.deliver_notification'
        return REGISTRY.get(kind,{}).get('consumer','UNREGISTERED_CONSUMER')
    states={}
    for row in groups:
        states[row['state']]=states.get(row['state'],0)+row['count']
        row.update(oldest_age_seconds=max(0,now-row['oldest_at']),owner=owner(row['kind']),
            delivery_confirmation='UNKNOWN_NO_REPLAY' if row['state']=='UNKNOWN' else
                'RECORDED_SENT' if row['state']=='SENT' else 'NOT_CONFIRMED')
    pending=states.get('PENDING',0);unknown=states.get('UNKNOWN',0)
    return {'counts_by_state':states,'counts_by_state_kind':groups,'unresolved_count':pending+unknown,
        'confirmation_coverage':{'sent':states.get('SENT',0),'unknown':unknown,
            'policy':'SENT按既有确认回执记账；UNKNOWN没有平台送达证明，不作已送或丢失推断。'},
        'samples':[{'notification_id':r['notification_id'],'kind':r['kind'],'state':r['state'],
            'age_seconds':max(0,now-r['created_at']),'owner':owner(r['kind']),
            'reason':'DELIVERY_UNKNOWN_NO_AUTOMATIC_REPLAY' if r['state']=='UNKNOWN' else
                'NO_REGISTERED_CONSUMER' if owner(r['kind'])=='UNREGISTERED_CONSUMER' else
                'PENDING_AGE_EXCEEDED' if now-r['created_at']>1800 else 'PENDING'} for r in samples],
        'samples_omitted':max(0,pending+unknown-len(samples)),
        'unknown_policy':'保留UNKNOWN；核平台回执或明确处置，不自动重发。'}


def suppress_stale(ledger, item):
    """Explicit durable disposition; never resend UNKNOWN or rewrite events."""
    age=REGISTRY[item['kind']]['max_pending_age_seconds']
    if age is None or ledger.clock()-item['created_at']<=age:return None
    def operation(db):
        from business_ledger import _deny,_json
        current=ledger._one(db,'outbox','notification_id',item['notification_id'])
        if current['state']!='PENDING':_deny('STALE_NOTICE_NOT_PENDING')
        receipt={'reason':'STALE_BUSINESS_NOTICE_NOT_REPLAYED','at':ledger.clock(),
            'max_age_seconds':age,'original_event_id':current['event_id'],
            'business_state_unchanged':True}
        db.execute("UPDATE outbox SET state='SUPPRESSED',receipt=? WHERE notification_id=?",
            (_json(receipt),current['notification_id']))
        return {'notification_id':current['notification_id'],'state':'SUPPRESSED','receipt':receipt}
    return ledger._command('suppress_stale_notification','stale-business-notice:'+item['notification_id'],
        [item['notification_id']],operation)
