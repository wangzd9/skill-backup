"""Advance an unmerged task after a sibling's audited, completed integration.

This creates a new attempt on the verified destination base. It never rebases a
live writer, carries old test approval forward, or changes acceptance criteria.
"""
import json
import subprocess


def prepare_pending_base(ledger, db, task):
    """Anchor newly eligible development to an audited integrated descendant."""
    from workflow_integration_workspace import completed_head
    from business_ledger import _row
    snapshot=ledger.snapshot(task['run_id'])
    head=completed_head(ledger,snapshot,task['run_id'])
    if not head or head==task['base']:
        return task
    run=ledger._one(db,'runs','run_id',task['run_id'])
    if subprocess.run(['git','-C',run['repo'],'merge-base','--is-ancestor',task['base'],head],
                      capture_output=True,timeout=15).returncode:
        return task
    db.execute('UPDATE tasks SET base=? WHERE run_id=? AND task_id=?',(head,task['run_id'],task['task_id']))
    ledger._event(db,'UNDELIVERED_TASK_BASE_REFRESHED',{'task_id':task['task_id'],'attempt':task['attempt'],
        'previous_base':task['base'],'base':head,'original_plan_unchanged':True,
        'requires_user_approval':False,'prior_checks_reused':False},run_id=task['run_id'],role=task['role'])
    return _row(db.execute('SELECT * FROM tasks WHERE run_id=? AND task_id=?',(task['run_id'],task['task_id'])).fetchone())


def refresh_undelivered(ledger, identity, task_id, reason, *, command_id):
    """A developer never needs to deliver a dummy candidate to update a base."""
    from business_ledger import _deny, _row
    def operation(db):
        owner,_=ledger._identity(db,identity)
        if owner['role']!='COORDINATOR' or not isinstance(reason,str) or not reason.strip():
            _deny('COORDINATOR_AND_BASE_REFRESH_REASON_REQUIRED')
        task=_row(db.execute('SELECT * FROM tasks WHERE run_id=? AND task_id=?',(owner['run_id'],task_id)).fetchone())
        if not task or task['phase'] not in ('PENDING','DEVELOPING'):
            _deny('UNDELIVERED_TASK_REQUIRED')
        works=[_row(w) for w in db.execute("SELECT * FROM works WHERE run_id=? AND task_id=? AND state!='ENDED'",
                                         (owner['run_id'],task_id))]
        slot=ledger._one(db,'role_current','role',task['role'])
        if slot['writer_execution_id'] or any(w['active_execution_id'] or db.execute(
                "SELECT 1 FROM executions WHERE work_id=? AND state!='ENDED'",(w['work_id'],)).fetchone() for w in works):
            for work in works:
                if not db.execute("SELECT 1 FROM events WHERE work_id=? AND kind='DEVELOPMENT_BASE_REFRESH_REQUESTED'",
                                  (work['work_id'],)).fetchone():
                    ledger._event(db,'DEVELOPMENT_BASE_REFRESH_REQUESTED',{'task_id':task_id,'reason':reason,
                        'requires_user_approval':False,'next':'保存当前成果commit/head与路径；checkpoint(yield_execution=true)后退出，等待协调者刷新未交付任务基线。禁止空占位交付。'},
                        run_id=task['run_id'],role=task['role'],work_id=work['work_id'])
            return {'state':'WAITING_FOR_DEVELOPER_SAFE_POINT','requires_user_approval':False,'task_id':task_id}
        updated=prepare_pending_base(ledger,db,task)
        if updated['base']==task['base']:
            return {'state':'BASE_ALREADY_CURRENT','task_id':task_id}
        for work in works:
            db.execute("UPDATE works SET state='ENDED',ended_at=? WHERE work_id=?",(ledger.clock(),work['work_id']))
            db.execute('UPDATE role_current SET current_work_id=NULL WHERE role=? AND current_work_id=?',
                       (work['role'],work['work_id']))
        db.execute("UPDATE tasks SET phase='PENDING' WHERE run_id=? AND task_id=?",(task['run_id'],task_id))
        event=ledger._event(db,'UNDELIVERED_DEVELOPMENT_REQUEUED',{'task_id':task_id,'base':updated['base'],
            'reason':reason,'preserved_source_work_ids':[w['work_id'] for w in works],
            'requires_user_approval':False},run_id=task['run_id'],role='COORDINATOR',work_id=owner['work_id'])
        return {'task_id':task_id,'base':updated['base'],'event_id':event,'preserved_source_work_ids':[w['work_id'] for w in works]}
    return ledger._command('refresh_development_base',command_id,[identity,task_id,reason],operation)


def refresh_base(ledger, identity, candidate_id, reason, *, command_id):
    from business_ledger import _deny,_row
    def operation(db):
        work,_=ledger._identity(db,identity)
        candidate=ledger._one(db,'candidates','candidate_id',candidate_id)
        if work['role']!='COORDINATOR' or work['run_id']!=candidate['run_id']:
            _deny('COORDINATOR_REQUIRED')
        if not isinstance(reason,str) or not reason.strip():
            _deny('BASE_REFRESH_REASON_REQUIRED')
        run=ledger._one(db,'runs','run_id',candidate['run_id'])
        from workflow_integration_workspace import verify
        merged=[_row(r) for r in db.execute(
            'SELECT m.* FROM merges m JOIN candidates c USING(candidate_id) '
            'JOIN tasks t ON t.run_id=c.run_id AND t.task_id=c.task_id '
            "WHERE c.run_id=? AND t.phase='COMPLETED' AND c.task_id!=?",
            (run['run_id'],candidate['task_id']))]
        proof=None
        for row in merged:
            _,raw=ledger._evidence(row['evidence']);receipt=json.loads(raw)
            frozen=receipt.get('frozen') or {}
            grant=ledger._one(db,'authorizations','authorization_id',row['authorization_id'])
            head=receipt.get('actual_head')
            if (head and frozen.get('destination_repo')==run['repo']
                    and verify(ledger,grant['spec'],receipt,head)):
                for source in ('operation','log'):
                    ledger._evidence(receipt[source])
                if receipt.get('postmerge_required'):
                    ledger._evidence(receipt['postmerge'])
                proof=row;break
        if not proof:
            _deny('BASE_REFRESH_REQUIRES_COMPLETED_SIBLING_MERGE_PROOF')
        if head==candidate['base']:
            _deny('TASK_BASE_ALREADY_CURRENT')
        if subprocess.run(['git','-C',run['repo'],'merge-base','--is-ancestor',candidate['base'],head],
                          capture_output=True,timeout=15).returncode:
            _deny('BASE_REFRESH_DESTINATION_NOT_DESCENDANT')
        if db.execute("SELECT 1 FROM external_actions WHERE candidate_id=? AND state IN ('SUBMITTED','UNKNOWN','RESULT_READY')",
                      (candidate_id,)).fetchone():
            _deny('BASE_REFRESH_EXTERNAL_ACTION_STILL_OPEN')
        from workflow_verification_lifecycle import request_for_candidate
        verification_work=request_for_candidate(db,ledger,candidate,'BASE_REFRESH_REQUIRES_NEW_FIXED_CANDIDATE',target_base=head)
        if any(w['active_execution_id'] or ledger._one(db,'role_current','role',w['role'])['writer_execution_id']
               or db.execute("SELECT 1 FROM executions WHERE work_id=? AND state!='ENDED'",(w['work_id'],)).fetchone()
               for w in verification_work):
            return {'state':'WAITING_FOR_VERIFICATION_SAFE_EXIT','requires_user_approval':False,
                    'retiring_work_ids':[w['work_id'] for w in verification_work],
                    'target_base':head,'next_action':'TESTER按持久退休请求safe_release；observer核退后自动收口，协调者接续refresh_task_base。'}
        result=ledger._rework(db,candidate_id,'合入基线接续（不代表代码失败）：'+reason)
        db.execute('UPDATE tasks SET base=? WHERE run_id=? AND task_id=?',
                   (head,run['run_id'],candidate['task_id']))
        event=ledger._event(db,'TASK_INTEGRATION_BASE_REFRESHED',{
            **result,'previous_base':candidate['base'],'base':head,'merge_id':proof['merge_id'],
            'reason':reason,'original_plan_unchanged':True,'prior_checks_reused':False,
            'source_candidate_repo':candidate['repo'],'source_candidate_head':candidate['head']},
            run_id=run['run_id'],role='COORDINATOR',work_id=work['work_id'])
        return {**result,'base':head,'event_id':event,'prior_checks_reused':False,
                'next_action':'在新私有工作区迁移旧候选的本任务改动；冲突由开发者修复，新候选重新审查、测试和批准'}
    return ledger._command('refresh_task_base',command_id,[identity,candidate_id,reason],operation)
