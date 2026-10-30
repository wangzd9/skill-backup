"""Explicit issue semantics; absence of classification stays conservatively blocking."""


def normalized(issue):
    return {'blocking':True,'confidence':'confirmed','accepted_scope':'local',
        'disposition':'repair' if issue['kind']=='code' else 'wait_environment',
        'owner':issue.get('task_id','COORDINATOR'),'ac_ids':[],**issue}


def blocking(issue):
    item=normalized(issue)
    return item['blocking'] or item['confidence']!='confirmed' or item['accepted_scope']!='local' and item['disposition']!='excluded_with_review'


def auto_repair(issue):
    item=normalized(issue)
    return (item['kind']=='code' and item['blocking'] is True and item['confidence']=='confirmed'
        and item['accepted_scope']=='local' and item['disposition']=='repair')


def validate(issues, checks, *, structured=False):
    from business_ledger import _deny
    check_ids={c['id'] for c in checks}
    for original in issues:
        item=normalized(original)
        required={'blocking','confidence','accepted_scope','disposition','owner','ac_ids'}
        if structured and not required<=original.keys():_deny('INTEGRATION_STRUCTURED_ISSUE_REQUIRED',issue_id=item['id'])
        if not set(item['ac_ids'])<=check_ids:_deny('INTEGRATION_ISSUE_AC_REFERENCE_INVALID')
        if not item['blocking']:
            if not required<=original.keys() or not item.get('rationale') or not item.get('review_required'):
                _deny('INTEGRATION_NONBLOCKING_ISSUE_REVIEW_REQUIRED')
            if item['disposition'] not in ('investigate','advisory','excluded_with_review'):
                _deny('INTEGRATION_NONBLOCKING_DISPOSITION_INVALID')
        if item['accepted_scope']!='local' and item['disposition']!='excluded_with_review':
            _deny('INTEGRATION_OUT_OF_SCOPE_DISPOSITION_REQUIRED')
        if item['confidence']!='confirmed' and item['disposition']!='investigate':
            _deny('INTEGRATION_UNCONFIRMED_ISSUE_REQUIRES_INVESTIGATION')


def review_dispositions(report, payload):
    from business_ledger import _deny
    exempt=[i for i in report['issues'] if not blocking(i)]
    rows=payload.get('issue_dispositions',[])
    if len(rows)!=len(exempt) or {r.get('issue_id') for r in rows}!={i['id'] for i in exempt}:
        _deny('REQUIREMENT_NONBLOCKING_ISSUE_DISPOSITION_REQUIRED')
    for row in rows:
        if not row.get('reason') or not row.get('evidence'):_deny('REQUIREMENT_ISSUE_DISPOSITION_EVIDENCE_REQUIRED')


def environment_recovered(ledger, identity, payload, *, command_id):
    """Explicit audited new budget, bound to the current target and failed report."""
    from workflow_completion import facts
    from business_ledger import _deny
    def operation(db):
        work,_=ledger._identity(db,identity)
        state=facts(ledger.snapshot(),work['run_id'])
        report=state.get('report')
        if work['role']!='COORDINATOR' or not report or report['payload']['result']!='UNVERIFIED':
            _deny('ENVIRONMENT_RECOVERY_WAITING_REPORT_REQUIRED')
        if report['event_id']!=payload['validation_event_id']:_deny('ENVIRONMENT_RECOVERY_REPORT_CHANGED')
        prior=[e for e in ledger.snapshot()['events'] if e['run_id']==work['run_id'] and e['kind']=='INTEGRATION_ENVIRONMENT_RECOVERED']
        if prior and ledger.clock()-prior[-1]['created_at']<300:_deny('ENVIRONMENT_RECOVERY_RATE_LIMIT')
        if any(e['payload']['target']==state['target'] and e['payload']['evidence']==payload['evidence'] for e in prior):
            _deny('ENVIRONMENT_RECOVERY_NEW_EVIDENCE_REQUIRED')
        for ref in payload['evidence']:ledger._evidence(ref)
        if not payload['evidence']:_deny('ENVIRONMENT_RECOVERY_EVIDENCE_REQUIRED')
        event=ledger._event(db,'INTEGRATION_ENVIRONMENT_RECOVERED',{
            **payload,'target':state['target'],'previous_result_unchanged':True,'budget':3},
            run_id=work['run_id'],role='COORDINATOR',work_id=work['work_id'])
        return {'event_id':event,'retry_budget':3,'previous_report_unchanged':True}
    return ledger._command('integration_environment_recovered',command_id,[identity,payload],operation)
