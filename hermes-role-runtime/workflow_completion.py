"""Requirement-level acceptance, backed by native events and writer-bound works.

Development tasks keep their existing contracts. Final independent validation
and coordinator closure are separate, durable plan nodes, not fake candidates.
"""
import hashlib
import json
from pathlib import Path
import re
import subprocess


def _events(snapshot, run_id, kind):
    return [e for e in snapshot['events'] if e['run_id']==run_id and e['kind']==kind]


def plan(snapshot, run_id):
    rows=_events(snapshot,run_id,'COMPLETION_PLAN_PUBLISHED')
    return rows[-1]['payload'] if rows else None


def _accepted_sources(snapshot, run_id):
    accepted=_events(snapshot,run_id,'REQUIREMENT_ACCEPTED')
    if not accepted:
        return None
    source=accepted[-1]
    if not source['payload'].get('sources'):
        return None
    texts=[]
    for ref in source['payload'].get('sources',[]):
        raw=Path(ref['frozen_path']).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=ref['sha256']:
            raise ValueError('COMPLETION_ACCEPTED_SOURCE_CHANGED')
        texts.append((ref,raw.decode('utf-8')))
    return source,texts


def _policy(snapshot, run_id, now):
    from workflow_task_merge_policy import live
    grants={g['authorization_id']:g for g in snapshot['authorizations']}
    run=next(r for r in snapshot['runs'] if r['run_id']==run_id)
    return next((g for g in reversed(snapshot['authorizations'])
        if g['kind']=='development_policy' and g['spec']['run_id']==run_id
        and set(run['task_ids'])<=set(g['spec']['task_ids'])
        and live(grants,g['authorization_id'],now)),None)


def ensure_plan(ledger):
    snapshot=ledger.snapshot();run_id=snapshot['current_run_id']
    if not run_id or plan(snapshot,run_id):
        return None
    run=next(r for r in snapshot['runs'] if r['run_id']==run_id)
    if snapshot['mode']!='ACTIVE' or not snapshot['admission_open'] or run['control']!='ACTIVE':
        return None
    policy=_policy(snapshot,run_id,ledger.clock())
    accepted=_accepted_sources(snapshot,run_id) if policy else None
    if not accepted:
        return None
    source,texts=accepted
    acceptance=[text for ref,text in texts if Path(ref['path']).name.upper()=='ACCEPTANCE.MD']
    identifiers=list(dict.fromkeys(re.findall(r'\b[A-Z][A-Z0-9_-]*-AC-\d+\b','\n'.join(acceptance))))
    identifiers=identifiers or ['accepted-contract']
    milestones=[]
    for ref,text in texts:
        if Path(ref['path']).name=='REGISTRATION.json':
            milestones=json.loads(text).get('milestones',[])
    ids=[re.match(r'([A-Za-z0-9_.-]+)',m).group(1) for m in milestones if isinstance(m,str) and re.match(r'[A-Za-z0-9_.-]+',m)]
    spec={'version':1,'validation_task_id':ids[0] if ids else 'FINAL-INTEGRATION',
        'closure_task_id':ids[1] if len(ids)>1 else 'FINAL-CLOSURE',
        'dependencies':source['payload']['task_ids'],
        'checks':identifiers,'sources':[ref for ref,text in texts],
        'accepted_event_id':source['event_id'],'policy_id':policy['authorization_id'],
        'branch':policy['spec']['branch'],'scope':'accepted_local_development',
        'requires_isolated_mysql':any(re.search(r'隔离\s*MySQL',text,re.I) for text in acceptance),
        'production_permission':False,'push_permission':False,'planner_trigger':'manual'}
    def operation(db):
        from business_ledger import _deny
        current=ledger._authority(db)
        if current['current_run_id']!=run_id or current['mode']!='ACTIVE' or not current['admission_open']:
            _deny('COMPLETION_CURRENT_RUN_CHANGED')
        existing=db.execute("SELECT event_id FROM events WHERE run_id=? AND kind='COMPLETION_PLAN_PUBLISHED'",(run_id,)).fetchone()
        if existing:
            return {'event_id':existing['event_id'],'already_published':True}
        grant=ledger._authorization(db,spec['policy_id'],'development_policy')
        if ledger._one(db,'runs','run_id',run_id)['control']!='ACTIVE':
            _deny('COMPLETION_RUN_PAUSED')
        if grant['spec']['branch']!=spec['branch']:
            _deny('COMPLETION_POLICY_CHANGED')
        event=ledger._event(db,'COMPLETION_PLAN_PUBLISHED',spec,run_id=run_id,role='COORDINATOR')
        return {'event_id':event,'plan':spec}
    return ledger._command('ensure_completion_plan','completion-plan:'+run_id,[run_id,spec],operation)


def frozen_target(ledger, snapshot, spec):
    run_id=next((r['run_id'] for r in snapshot['runs'] if r['run_id']==snapshot['current_run_id']),None)
    run=next((r for r in snapshot['runs'] if r['run_id']==run_id),None)
    from workflow_task_merge_policy import live
    grants={g['authorization_id']:g for g in snapshot['authorizations']}
    grant=grants.get(spec['policy_id'])
    if (not run or not grant or grant['kind']!='development_policy'
            or not live(grants,spec['policy_id'],ledger.clock())
            or grant['spec']['run_id']!=run_id or grant['spec']['repo']!=run['repo']
            or grant['spec']['branch']!=spec['branch']
            or not set(spec['dependencies'])<=set(grant['spec']['task_ids'])):
        raise ValueError('COMPLETION_LOCAL_POLICY_UNAVAILABLE')
    accepted=_accepted_sources(snapshot,run_id)
    if not accepted or accepted[0]['event_id']!=spec['accepted_event_id'] or [r for r,t in accepted[1]]!=spec['sources']:
        raise ValueError('COMPLETION_ACCEPTED_CONTRACT_CHANGED')
    tasks=[t for t in snapshot['tasks'] if t['run_id']==run_id]
    if set(t['task_id'] for t in tasks)!=set(spec['dependencies']) or any(t['phase']!='COMPLETED' for t in tasks):
        raise ValueError('COMPLETION_DEVELOPMENT_NOT_COMPLETE')
    from workflow_integration_workspace import git
    head=git(run['repo'],'rev-parse','--verify','refs/heads/'+spec['branch'])
    candidates=[c for c in snapshot['candidates'] if c['run_id']==run_id and c['state']=='CURRENT']
    selected=[]
    for task in tasks:
        candidate=next((c for c in candidates if c['task_id']==task['task_id'] and c['attempt']==task['attempt']),None)
        merge=next((m for m in snapshot['merges'] if candidate and m['candidate_id']==candidate['candidate_id']),None)
        if not candidate or not merge:
            raise ValueError('COMPLETION_SUPPLIER_MERGE_MISSING')
        for commit in (candidate['head'],merge['head']):
            if subprocess.run(['git','-C',run['repo'],'merge-base','--is-ancestor',commit,head],capture_output=True,timeout=15).returncode:
                raise ValueError('COMPLETION_SUPPLIER_NOT_IN_SNAPSHOT')
        selected.append({key:candidate[key] for key in ('candidate_id','task_id','attempt','head','ac_version')})
    if not any(m['head']==head and any(c['candidate_id']==m['candidate_id'] for c in candidates) for m in snapshot['merges']):
        raise ValueError('COMPLETION_BRANCH_HAS_UNACCOUNTED_COMMIT')
    from workflow_integration_workspace import completed_head
    if completed_head(ledger,snapshot,run_id)!=head:
        raise ValueError('COMPLETION_MERGED_WORKSPACE_NOT_VERIFIED')
    return {'repo':run['repo'],'head':head,'base':head,'run_id':run_id,
        'branch':spec['branch'],'suppliers':selected,'plan_version':spec['version'],
        'ac_version':hashlib.sha256(json.dumps(spec['sources'],sort_keys=True).encode()).hexdigest()}


def context(snapshot, work):
    rows=[e for e in _events(snapshot,work['run_id'],'INTEGRATION_VERIFICATION_QUEUED') if e['work_id']==work['work_id']]
    if len(rows)!=1:
        raise ValueError('INTEGRATION_CONTEXT_MISSING_OR_AMBIGUOUS')
    return rows[0]['payload']


def facts(snapshot, run_id):
    spec=plan(snapshot,run_id)
    if not spec:
        return {'state':'NOT_PLANNED','plan':None}
    queued=_events(snapshot,run_id,'INTEGRATION_VERIFICATION_QUEUED')
    latest=queued[-1] if queued else None
    reported=[e for e in _events(snapshot,run_id,'INTEGRATION_VERIFICATION_REPORTED') if latest and e['work_id']==latest['work_id']]
    closed=[e for e in _events(snapshot,run_id,'REQUIREMENT_COMPLETED') if latest and e['payload']['validation_work_id']==latest['work_id']]
    state='WAITING_DEVELOPMENT'
    work=next((w for w in snapshot['works'] if latest and w['work_id']==latest['work_id']),None)
    if latest:
        state='VERIFYING' if work and work['state']=='RUNNING' else 'VERIFICATION_QUEUED'
    if reported:
        state={'PASS':'READY_FOR_CLOSURE','FAIL':'REPAIR_REQUIRED'}.get(reported[-1]['payload']['result'],'WAITING_ENVIRONMENT')
    if closed:
        state='COMPLETED'
    current={t['task_id']:t for t in snapshot['tasks'] if t['run_id']==run_id}
    if latest and any(c['task_id'] not in current or current[c['task_id']]['phase']!='COMPLETED'
                      or current[c['task_id']]['attempt']!=c['attempt'] for c in latest['payload']['target']['suppliers']):
        state='REPAIRING' if reported else 'SNAPSHOT_STALE'
    return {'state':state,'plan':spec,'work_id':latest['work_id'] if latest else None,
        'target':latest['payload']['target'] if latest else None,
        'report':reported[-1] if reported else None,'completion_event_id':closed[-1]['event_id'] if closed else None}


def target_current(ledger, snapshot, work):
    try:
        recorded=context(snapshot,work)
        return recorded['target']==frozen_target(ledger,snapshot,recorded['plan'])
    except (ValueError,OSError,subprocess.SubprocessError):
        return False


def reconcile(ledger):
    ensure_plan(ledger)
    snapshot=ledger.snapshot();run_id=snapshot['current_run_id']
    spec=plan(snapshot,run_id)
    run=next((r for r in snapshot['runs'] if r['run_id']==run_id),None)
    if not spec or snapshot['mode']!='ACTIVE' or not snapshot['admission_open'] or run['control']!='ACTIVE':
        return []
    from workflow_completion_repair import queue_repairs
    repairs=queue_repairs(ledger,snapshot,run_id)
    snapshot=ledger.snapshot()
    state=facts(snapshot,run_id)
    try:
        target=frozen_target(ledger,snapshot,spec)
    except (ValueError,OSError,subprocess.SubprocessError):
        return repairs
    old=next((w for w in snapshot['works'] if w['work_id']==state.get('work_id')),None)
    if old and (old['state']!='ENDED' or any(e['work_id']==old['work_id'] and e['state']!='ENDED' for e in snapshot['executions'])):
        return repairs
    if state.get('target')==target:
        report=state.get('report')
        if report and report['payload']['result'] in ('PASS','FAIL'):
            return repairs
        # Environmental gaps do not reopen code tasks or spin forever. Keep
        # the evidence and retry a bounded number of times with backoff.
        same=[e for e in _events(snapshot,run_id,'INTEGRATION_VERIFICATION_QUEUED') if e['payload']['target']==target]
        if len(same)>=3 or (report and ledger.clock()<report['created_at']+300):
            return repairs
    predecessor=state.get('work_id')
    fingerprint=hashlib.sha256(json.dumps([target,predecessor],sort_keys=True).encode()).hexdigest()
    def operation(db):
        from business_ledger import _id,_json,_deny
        authority=ledger._authority(db)
        if authority['current_run_id']!=run_id or authority['mode']!='ACTIVE' or not authority['admission_open']:
            _deny('COMPLETION_CURRENT_RUN_CHANGED')
        if ledger._one(db,'runs','run_id',run_id)['control']!='ACTIVE':
            _deny('COMPLETION_RUN_PAUSED')
        ledger._authorization(db,spec['policy_id'],'development_policy')
        current=ledger.snapshot()
        if facts(current,run_id).get('work_id')!=predecessor:
            _deny('COMPLETION_QUEUE_ALREADY_ADVANCED')
        if frozen_target(ledger,current,spec)!=target:
            _deny('COMPLETION_TARGET_CHANGED')
        work_id=_id('work')
        attempt=len(_events(current,run_id,'INTEGRATION_VERIFICATION_QUEUED'))+1
        db.execute("INSERT INTO works(work_id,run_id,task_id,attempt,role,kind,state,checkpoint,created_at) VALUES(?,?,?,?,'TESTER','integration_verification','QUEUED','{}',?)",
            (work_id,run_id,spec['validation_task_id'],attempt,ledger.clock()))
        event=ledger._event(db,'INTEGRATION_VERIFICATION_QUEUED',{'plan':spec,'target':target,
            'work_id':work_id,'attempt':attempt,'automatic':True,'requires_user_approval':False},
            run_id=run_id,role='TESTER',work_id=work_id,notify=True)
        return {'event_id':event,'work_id':work_id}
    queued=ledger._command('queue_integration_verification','completion-queue:'+fingerprint,[target],operation)
    return [*repairs,queued]


def record_test(ledger, identity, target, evidence, stats, *, command_id):
    def operation(db):
        from business_ledger import _deny
        work,_=ledger._identity(db,identity)
        if work['role']!='TESTER' or work['kind']!='integration_verification':
            _deny('INTEGRATION_TESTER_REQUIRED')
        frozen=context(ledger.snapshot(),work)['target']
        if any(target.get(k)!=frozen.get(k) for k in ('head','repo','run_id','ac_version')):
            _deny('INTEGRATION_TEST_TARGET_MISMATCH')
        if not target_current(ledger,ledger.snapshot(),work):
            _deny('INTEGRATION_SNAPSHOT_CHANGED')
        event=ledger._event(db,'INTEGRATION_TEST_RECORDED',{'target':target,'evidence':evidence,'stats':stats},
            run_id=work['run_id'],role='TESTER',work_id=work['work_id'])
        return {'event_id':event,'result':stats['mechanical_result']}
    return ledger._command('record_integration_test',command_id,[identity,target,evidence,stats],operation)


def deliver(ledger, identity, payload, *, command_id):
    from business_ledger import _deny
    def operation(db):
        work,execution=ledger._identity(db,identity)
        if work['role']!='TESTER' or work['kind']!='integration_verification':
            _deny('INTEGRATION_TESTER_REQUIRED')
        snapshot=ledger.snapshot();ctx=context(snapshot,work)
        if not target_current(ledger,snapshot,work):
            _deny('INTEGRATION_SNAPSHOT_CHANGED')
        checks=payload['checks'];issues=payload['issues']
        if (len(checks)!=len(ctx['plan']['checks']) or {c['id'] for c in checks}!=set(ctx['plan']['checks'])):
            _deny('INTEGRATION_ALL_ACCEPTANCE_CHECKS_REQUIRED')
        runs={e['event_id']:e for e in _events(snapshot,work['run_id'],'INTEGRATION_TEST_RECORDED') if e['work_id']==work['work_id']}
        from delivery_evidence import check_candidate_test_evidence
        for check in checks:
            if check['result']=='PASS' and check['mode']=='not_executed':
                _deny('INTEGRATION_UNEXECUTED_CHECK_CANNOT_PASS')
            if check['result']=='PASS' and not check['test_event_ids']:
                _deny('INTEGRATION_EXECUTED_EVIDENCE_REQUIRED')
            for event_id in check['test_event_ids']:
                row=runs.get(event_id)
                if not row:
                    _deny('INTEGRATION_TEST_REFERENCE_INVALID')
                data=row['payload']
                checked=check_candidate_test_evidence(data['evidence'],data['target'])
                if not checked['valid']:
                    _deny('INTEGRATION_TEST_EVIDENCE_CHANGED',reasons=checked['reasons'])
                if check['result']=='PASS' and (data['stats']['mechanical_result']!='PASS' or data['stats']['skipped']):
                    _deny('INTEGRATION_FAILED_OR_SKIPPED_EVIDENCE_CANNOT_PASS')
        if len({i['id'] for i in issues})!=len(issues):
            _deny('INTEGRATION_UNIQUE_ISSUE_IDS_REQUIRED')
        for issue in issues:
            if issue.get('kind')=='environment':
                continue
            if (issue['task_id'] not in ctx['plan']['dependencies'] or not issue['test_event_ids']
                    or not set(issue['test_event_ids'])<=set(runs)
                    or not any(runs[eid]['payload']['stats']['mechanical_result']=='FAIL' for eid in issue['test_event_ids'])):
                _deny('INTEGRATION_ISSUE_OUTSIDE_ACCEPTED_SCOPE')
            for eid in issue['test_event_ids']:
                data=runs[eid]['payload']
                if not check_candidate_test_evidence(data['evidence'],data['target'])['valid']:
                    _deny('INTEGRATION_ISSUE_EVIDENCE_CHANGED')
        failing=[c for c in checks if c['result']=='FAIL']
        code_issues=[i for i in issues if i.get('kind')=='code']
        if failing and (not code_issues or any(not set(c['test_event_ids']) & {eid for i in code_issues for eid in i['test_event_ids']} for c in failing)):
            _deny('INTEGRATION_CODE_FAILURE_REQUIRES_MAPPED_ISSUE')
        result=('FAIL' if code_issues else 'PASS' if all(c['result']=='PASS' for c in checks) and not issues else 'UNVERIFIED')
        if result=='PASS' and ctx['plan']['requires_isolated_mysql']:
            used={eid for check in checks for eid in check['test_event_ids']}
            if not any(runs[eid]['payload']['evidence'].get('verification_profile')=='integration_mysql'
                       and runs[eid]['payload']['stats']['mechanical_result']=='PASS' for eid in used):
                _deny('INTEGRATION_REAL_ISOLATED_MYSQL_REQUIRED')
        from workflow_completion_reports import validation_receipt
        receipt=validation_receipt(ledger,work,ctx,payload,result)
        event=ledger._event(db,'INTEGRATION_VERIFICATION_REPORTED',{
            'result':result,'checks':checks,'issues':issues,'summary':payload['summary'],
            'target':ctx['target'],'receipt':receipt,'scope':'accepted_local_development'},
            run_id=work['run_id'],role='TESTER',work_id=work['work_id'],notify=True)
        db.execute("UPDATE works SET state='ENDED',ended_at=? WHERE work_id=?",(ledger.clock(),work['work_id']))
        db.execute('UPDATE role_current SET current_work_id=NULL WHERE role=?',('TESTER',))
        db.execute("UPDATE executions SET state='DRAINING' WHERE execution_id=?",(execution['execution_id'],))
        return {'event_id':event,'result':result,'receipt':receipt}
    return ledger._command('deliver_integration',command_id,[identity,payload],operation)


def complete(ledger, identity, payload, *, command_id):
    def operation(db):
        from business_ledger import _deny
        work,_=ledger._identity(db,identity)
        if work['role']!='COORDINATOR':
            _deny('COORDINATOR_REQUIRED')
        snapshot=ledger.snapshot();state=facts(snapshot,work['run_id'])
        if state['state']!='READY_FOR_CLOSURE':
            _deny('REQUIREMENT_FINAL_VERIFICATION_NOT_PASSED')
        validation=next(w for w in snapshot['works'] if w['work_id']==state['work_id'])
        if not target_current(ledger,snapshot,validation):
            _deny('REQUIREMENT_FINAL_SNAPSHOT_CHANGED')
        if any(e['work_id']==validation['work_id'] and e['state']!='ENDED' for e in snapshot['executions']):
            _deny('REQUIREMENT_TESTER_WRITER_NOT_CLEARED')
        if any(w['run_id']==work['run_id'] and w['role']=='TESTER' and w['state']!='ENDED' for w in snapshot['works']):
            _deny('REQUIREMENT_VERIFICATION_WORK_PENDING')
        report=state['report']['payload']
        ledger._evidence(report['receipt'])
        # Revalidate raw test evidence at the final acceptance boundary as well.
        from delivery_evidence import check_candidate_test_evidence
        used={eid for check in report['checks'] for eid in check['test_event_ids']}
        records={e['event_id']:e for e in _events(snapshot,work['run_id'],'INTEGRATION_TEST_RECORDED')}
        for eid in used:
            data=records[eid]['payload']
            if not check_candidate_test_evidence(data['evidence'],data['target'])['valid']:
                _deny('REQUIREMENT_FINAL_EVIDENCE_CHANGED')
        pending=historical_failures(snapshot,work['run_id'])
        dispositions=payload['historical_failures']
        if len(dispositions)!=len(pending) or {d['verification_id'] for d in dispositions}!={v['verification_id'] for v in pending}:
            _deny('REQUIREMENT_HISTORICAL_FAILURE_DISPOSITION_REQUIRED')
        for disposition in dispositions:
            if not disposition['test_event_ids'] or not set(disposition['test_event_ids'])<=used:
                _deny('REQUIREMENT_HISTORICAL_FAILURE_PROOF_REQUIRED')
            old=next(v for v in pending if v['verification_id']==disposition['verification_id'])
            if old['check_name'] in ('ocr','strong_review'):
                _deny('REQUIREMENT_REVIEW_FAILURE_REQUIRES_ORIGINAL_REVIEW_CLOSURE')
            if old['check_name']=='supplemental_mysql' and not any(records[eid]['payload']['evidence'].get('verification_profile')=='integration_mysql' for eid in disposition['test_event_ids']):
                _deny('REQUIREMENT_HISTORICAL_MYSQL_PROOF_REQUIRED')
        if any(a['state'] in ('SUBMITTED','UNKNOWN','RESULT_READY') for a in snapshot['external_actions']
               if any(c['candidate_id']==a['candidate_id'] and c['run_id']==work['run_id'] for c in snapshot['candidates'])):
            _deny('REQUIREMENT_EXTERNAL_RESULT_PENDING')
        from workflow_completion_reports import closure_receipt
        receipt=closure_receipt(ledger,work,state,payload)
        event=ledger._event(db,'REQUIREMENT_COMPLETED',{'validation_work_id':state['work_id'],
            'validation_event_id':state['report']['event_id'],'target':state['target'],
            'summary':payload['summary'],'review':payload['review'],'receipt':receipt,
            'historical_failures':dispositions,
            'scope':'accepted_local_development','production_complete':False,'push_performed':False},
            run_id=work['run_id'],role='COORDINATOR',work_id=work['work_id'],notify=True)
        return {'event_id':event,'state':'COMPLETED','receipt':receipt}
    return ledger._command('complete_requirement',command_id,[identity,payload],operation)


def historical_failures(snapshot, run_id):
    ids={c['candidate_id'] for c in snapshot['candidates'] if c['run_id']==run_id}
    rows=[v for v in snapshot['verifications'] if v['candidate_id'] in ids]
    closed={vid for v in rows if v['result']=='PASS' for vid in v['supersedes']}
    return [v for v in rows if v['result']=='FAIL' and v['verification_id'] not in closed]


def verify(ledger, identity, payload, *, command_id):
    verified=ledger.validate_identity(identity)
    if not verified['valid']:
        return verified
    work=verified['work']
    if work['role']!='TESTER' or work['kind']!='integration_verification':
        raise ValueError('INTEGRATION_TESTER_REQUIRED')
    from delivery_evidence import execute_candidate_tests
    kwargs={'command_id':command_id,'timeout_seconds':payload.get('timeout_seconds',300),
        'report_format':payload.get('report_format','pytest'),'evidence':payload.get('evidence',{})}
    if payload.get('profile','local')=='mysql':
        from workflow_supplemental import mysql_session
        directory=ledger.path.parent/'mysql-sessions'/hashlib.sha256((work['work_id']+command_id).encode()).hexdigest()[:16]
        with mysql_session(directory) as (url,marker):
            kwargs['evidence']={**kwargs['evidence'],'inputs':[*(kwargs['evidence'].get('inputs') or []),str(marker)]}
            return execute_candidate_tests(ledger,identity,None,payload['argv'],
                verification_profile='integration_mysql',environment_overrides={'MN01_MYSQL_URL':url},**kwargs)
    return execute_candidate_tests(ledger,identity,None,payload['argv'],verification_profile='integration_local',**kwargs)


def next_actions(packet, ledger):
    snapshot=ledger.snapshot();work=packet.get('work') or {}
    run_id=(packet.get('run') or {}).get('run_id')
    state=facts(snapshot,run_id)
    if work.get('kind')=='integration_verification':
        if not target_current(ledger,snapshot,work):
            return [{'action':'safe_release','payload':{'reason':'最终集成快照已变化，保留证据并交还原生工作单'},
                'reason':'版本变化使本次验证失效；安全退出后系统自动冻结新候选，不需要用户触发。'}]
        return [{'action':'verify_integration','reason':'按completion_plan原合同逐AC独立取证；使用受控测试入口，可选mysql profile提供真实隔离8.4，随后deliver_integration提交覆盖与问题。不可改业务代码，不执行真实远端/NAS操作。'},
                {'action':'deliver_integration','reason':'每项AC显式引用本单INTEGRATION_TEST_RECORDED事件；失败按问题ID关联原开发task，环境缺口留UNVERIFIED，不虚构PASS。'}]
    if packet['role']=='COORDINATOR' and state['state']=='READY_FOR_CLOSURE':
        return [{'action':'complete_requirement','reason':'原生最终验证已PASS；核对固定版本、逐AC证据与遗留问题，在本地授权内完成收口，不再索要用户派发或收口口令。'}]
    return []
