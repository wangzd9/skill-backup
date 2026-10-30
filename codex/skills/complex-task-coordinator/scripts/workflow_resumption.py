"""Bound repeated resumption without treating old unacknowledged input as new."""
import json


def admission(ledger, db, work, relevant):
    rows=db.execute("SELECT bound_at,ended_at FROM executions WHERE work_id=? ORDER BY bound_at DESC LIMIT 8",
        (work['work_id'],)).fetchall()
    if len(rows)<3:return None
    last=rows[0]['bound_at']
    if any(e['created_at']>last for e in relevant):return None
    checkpoints=db.execute("SELECT payload,created_at FROM events WHERE work_id=? AND kind='CHECKPOINT_SAVED' AND created_at>=? ORDER BY event_id",
        (work['work_id'],rows[-1]['bound_at'])).fetchall()
    progressed=[r['created_at'] for r in checkpoints if json.loads(r['payload']).get('acknowledged') is True
        and json.loads(r['payload']).get('processed_through',0)>json.loads(r['payload']).get('previous_processed_through',0)]
    boundary=max(progressed,default=rows[-1]['bound_at']-1)
    attempts=[r for r in rows if r['bound_at']>boundary]
    if len(attempts)<3:return None
    delay=min(900,60*2**min(4,len(attempts)-3))
    until=max(rows[0]['ended_at'] or last,last)+delay
    if ledger.clock()>=until:return None
    return {'can_start_model':False,'reason':'NO_PROGRESS_RESUMPTION_BACKOFF',
        'work_id':work['work_id'],'retry_after':until,'backoff_seconds':delay,
        'processed_through':work['processed_through'],'issued_through':work['issued_through'],
        'unchanged_resumptions':len(attempts),'ack_required':work['issued_through']>work['processed_through'],
        'diagnostic':'既有事件待处理；反复恢复没有推进确认水位。新业务事件可提前解除退避，不确认未读事件。'}
