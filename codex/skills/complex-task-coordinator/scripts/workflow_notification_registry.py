"""Business outbox contracts shared by producers, renderer and consumer."""

REGISTRY={kind:{'producer':producer,'renderer':'workflow_reports.render_notification',
    'consumer':'workflow_notifications.drain_business_notifications','target':'event_role',
    'quiet':'suppress','max_pending_age_seconds':1800 if kind.startswith(('INTEGRATION_','FINAL_','REQUIREMENT_')) else None}
    for kind,producer in {
        'WORK_CLAIMED':'business_ledger','CANDIDATE_DELIVERED':'business_ledger',
        'CANDIDATE_DECIDED':'business_ledger','COORDINATOR_PROGRESS':'workflow_reports',
        'SERVICE_ARTIFACT_RECORDED':'business_ledger','EXTERNAL_ACTION_REJECTED':'business_ledger',
        'INTEGRATION_VERIFICATION_QUEUED':'workflow_completion',
        'INTEGRATION_VERIFICATION_REPORTED':'workflow_completion',
        'FINAL_ACCEPTANCE_REWORK_QUEUED':'workflow_completion_repair',
        'REQUIREMENT_COMPLETED':'workflow_completion'}.items()}


def diagnostics(ledger):
    items=ledger.outbox_items(states=('PENDING',),limit=256)
    return [{'notification_id':r['notification_id'],'kind':r['kind'],
        'age_seconds':max(0,ledger.clock()-r['created_at']),
        'reason':'NO_REGISTERED_CONSUMER' if r['kind'] not in REGISTRY else 'PENDING_AGE_EXCEEDED'}
        for r in items if r['kind'] not in REGISTRY or ledger.clock()-r['created_at']>1800]


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
