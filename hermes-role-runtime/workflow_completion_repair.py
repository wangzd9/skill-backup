"""Return final integration defects to original owners without erasing evidence."""


def queue_repairs(ledger, snapshot, run_id):
    from business_ledger import _row,_deny
    from workflow_completion import facts
    state=facts(snapshot,run_id)
    report=state.get('report')
    if not report or report['payload']['result']!='FAIL':
        return []
    grouped={}
    for issue in report['payload']['issues']:
        if issue.get('kind')=='code':
            grouped.setdefault(issue['task_id'],[]).append(issue)
    results=[]
    for task_id,issues in grouped.items():
        if any(e['kind']=='FINAL_ACCEPTANCE_REWORK_QUEUED' and e['run_id']==run_id
               and e['payload']['validation_event_id']==report['event_id'] and e['payload']['task_id']==task_id for e in snapshot['events']):
            continue
        task=next((t for t in snapshot['tasks'] if t['run_id']==run_id and t['task_id']==task_id),None)
        supplier=next((c for c in report['payload']['target']['suppliers'] if c['task_id']==task_id),None)
        if (not task or not supplier or task['phase']!='COMPLETED' or task['attempt']!=supplier['attempt']
                or any(w['run_id']==run_id and w['task_id']==task_id and
                    (w['state']!='ENDED' or any(e['work_id']==w['work_id'] and e['state']!='ENDED' for e in snapshot['executions'])) for w in snapshot['works'])):
            continue
        if any(e['work_id']==state['work_id'] and e['state']!='ENDED' for e in snapshot['executions']):
            continue
        def operation(db,task_id=task_id,issues=issues):
            authority=ledger._authority(db)
            if authority['current_run_id']!=run_id or authority['mode']!='ACTIVE' or not authority['admission_open'] or ledger._one(db,'runs','run_id',run_id)['control']!='ACTIVE':
                _deny('COMPLETION_REPAIR_RUN_NOT_ACTIVE')
            from workflow_completion import plan, _accepted_sources
            current=ledger.snapshot()
            spec=plan(current,run_id)
            grant=ledger._authorization(db,spec['policy_id'],'development_policy')
            accepted=_accepted_sources(current,run_id)
            run=ledger._one(db,'runs','run_id',run_id)
            if (not accepted or accepted[0]['event_id']!=spec['accepted_event_id']
                    or [r for r,t in accepted[1]]!=spec['sources']
                    or grant['spec']['branch']!=spec['branch'] or grant['spec']['repo']!=run['repo']):
                _deny('COMPLETION_REPAIR_CONTRACT_CHANGED')
            from workflow_integration_workspace import git
            if git(run['repo'],'rev-parse','refs/heads/'+spec['branch'])!=report['payload']['target']['head']:
                _deny('COMPLETION_REPAIR_SNAPSHOT_CHANGED')
            task=_row(db.execute('SELECT * FROM tasks WHERE run_id=? AND task_id=?',(run_id,task_id)).fetchone())
            if not task or task['phase']!='COMPLETED' or task['attempt']!=supplier['attempt']:
                _deny('COMPLETION_REPAIR_ALREADY_ACTIVE_OR_TASK_NOT_COMPLETE')
            writers=db.execute("SELECT 1 FROM works w WHERE w.run_id=? AND w.task_id=? AND "
                "(w.state!='ENDED' OR EXISTS(SELECT 1 FROM executions e WHERE e.work_id=w.work_id AND e.state!='ENDED'))",
                (run_id,task_id)).fetchone()
            if writers:
                _deny('COMPLETION_REPAIR_WAITING_FOR_ORIGINAL_WRITER')
            candidate=_row(db.execute("SELECT * FROM candidates WHERE run_id=? AND task_id=? AND attempt=? AND state='CURRENT'",
                (run_id,task_id,task['attempt'])).fetchone())
            if not candidate:
                _deny('COMPLETION_REPAIR_SOURCE_CANDIDATE_MISSING')
            # Same original task/scope/AC/role. Only the attempt and integrated
            # base change; ordinary candidate tests/reviews/merge gates remain.
            attempt=task['attempt']+1
            reason='最终集成验收问题：'+','.join(i['id'] for i in issues)
            db.execute("UPDATE candidates SET state='SUPERSEDED' WHERE candidate_id=?",(candidate['candidate_id'],))
            db.execute("UPDATE attempts SET state='SUPERSEDED' WHERE run_id=? AND task_id=? AND attempt=?",
                (run_id,task_id,task['attempt']))
            db.execute("INSERT INTO attempts VALUES(?,?,?,'CURRENT',?,?)",
                (run_id,task_id,attempt,candidate['candidate_id'],reason))
            db.execute("UPDATE tasks SET phase='PENDING',attempt=?,base=? WHERE run_id=? AND task_id=?",
                (attempt,report['payload']['target']['head'],run_id,task_id))
            event=ledger._event(db,'FINAL_ACCEPTANCE_REWORK_QUEUED',{'task_id':task_id,
                'attempt':attempt,'issues':issues,'validation_event_id':report['event_id'],
                'previous_candidate_id':candidate['candidate_id'],'scope_unchanged':True},
                run_id=run_id,role=task['role'],notify=True)
            return {'event_id':event,'task_id':task_id,'attempt':attempt,'queued':True}
        results.append(ledger._command('queue_final_acceptance_repair',
            'completion-repair:'+str(report['event_id'])+':'+task_id,[report['event_id'],task_id,issues],operation))
    return results


def task_issues(snapshot, run_id, task):
    if not task:
        return []
    return [e['payload'] for e in snapshot['events'] if e['run_id']==run_id
        and e['kind']=='FINAL_ACCEPTANCE_REWORK_QUEUED'
        and e['payload']['task_id']==task['task_id'] and e['payload']['attempt']==task['attempt']]
