"""Business transitions and global fences, distinct from reading/ACK activity."""


def progress(snapshot, work, at):
    kinds={'CANDIDATE_DELIVERED','CANDIDATE_DECIDED','VERIFICATION_PUBLISHED','MERGE_RECORDED',
        'TASK_COMPLETED','CANDIDATE_MERGED','WORK_RELEASED','INTEGRATION_TEST_RECORDED','INTEGRATION_VERIFICATION_REPORTED',
        'FINAL_ACCEPTANCE_REWORK_QUEUED','REQUIREMENT_COMPLETED','EXTERNAL_ACTION_REGISTERED',
        'SERVICE_ARTIFACT_RECORDED','EXTERNAL_ACTION_REJECTED','VERIFICATION_EXCEPTION_RECORDED',
        'VERIFICATION_EXCEPTION_DISPOSITION_RECORDED','VERIFICATION_EXCEPTION_REPAIR_QUEUED',
        'VERIFICATION_EXCEPTION_CLOSED','VERIFICATION_RETIREMENT_REQUESTED','WORK_ENDED','PLAN_PUBLISHED'}
    rows=[e for e in snapshot['events'] if e.get('work_id')==work['work_id'] and e['kind'] in kinds]
    execution=next((e for e in snapshot.get('executions',[]) if e['execution_id']==work.get('active_execution_id')),{})
    started=execution.get('bound_at')
    bound=next((e['event_id'] for e in reversed(snapshot['events']) if e['kind']=='EXECUTION_BOUND'
        and e.get('payload',{}).get('execution_id')==execution.get('execution_id')),None)
    active=[e for e in rows if bound is not None and e['event_id']>bound]
    def ref(event):
        return {k:event.get(k) for k in ('event_id','kind','created_at','role','work_id')}
    latest=rows[-1] if rows else None
    return {'owner':work['role'],'work_id':work['work_id'],'work_state':work['state'],
        'last_business_transition':ref(latest) if latest else None,
        'seconds_since_business_transition':max(0,at-latest['created_at']) if latest else None,
        'first_effective_action_in_execution':ref(active[0]) if active else None,
        'effective_action_observed':bool(active),
        'execution_id':execution.get('execution_id'),'native_outcome':execution.get('native_outcome'),
        'processed_through':work['processed_through'],'issued_through':work['issued_through'],
        'policy':'ACK只衡量事件消费；检查点、worker退出与模型请求不算业务交付；没有状态转换不等于没有有用分析。'}


def global_review_fences(ledger):
    with ledger._connection() as db:
        rows=db.execute("SELECT a.action_id,a.kind,a.state,c.run_id,c.task_id,c.attempt,c.candidate_id "
            "FROM external_actions a JOIN candidates c USING(candidate_id) "
            "WHERE a.kind='ocr' AND a.state IN ('SUBMITTED','UNKNOWN') ORDER BY a.action_id").fetchall()
    return [dict(r) for r in rows]
