"""Audited UNVERIFIED handoff and in-scope repair; never synthesize a verdict.

The runner records a finding, the owning TESTER drains, and the coordinator
chooses a disposition. Only an accepted local policy can reopen a completed
task. An execution ending alone never completes work or creates a repair.
"""
import fnmatch
import hashlib
import json
from pathlib import Path


def facts(snapshot, run_id):
    events=[e for e in snapshot['events'] if e['run_id']==run_id]
    resolved={e['payload']['observation_event_id'] for e in events if e['kind']=='VERIFICATION_EXCEPTION_CLOSED'}
    rows=[]
    for event in events:
        if event['kind']!='VERIFICATION_EXCEPTION_RECORDED':continue
        eid=event['event_id']
        decisions=[e for e in events if e['kind']=='VERIFICATION_EXCEPTION_DISPOSITION_RECORDED'
            and e['payload']['observation_event_id']==eid]
        decision=decisions[-1] if decisions else None
        queued=next((e for e in events if e['kind']=='VERIFICATION_EXCEPTION_REPAIR_QUEUED'
            and decision and e['payload']['disposition_event_id']==decision['event_id']),None)
        faults=[e for e in events if e['kind']=='VERIFICATION_EXCEPTION_REPAIR_BLOCKED'
            and decision and e['payload']['disposition_event_id']==decision['event_id']]
        rows.append({**event['payload'],'observation_event_id':eid,'work_id':event['work_id'],
            'state':'CLOSED' if eid in resolved else 'REPAIR_QUEUED' if queued else
                'BLOCKED_REFRESH_REQUIRED' if faults else
                'WAITING_SAFE_EXIT' if decision and decision['payload']['disposition']=='repair' else
                'INVESTIGATING' if decision else 'AWAITING_COORDINATOR_DISPOSITION',
            'responsible_role':'COORDINATOR','disposition':decision,
            'repair':queued['payload'] if queued else None,
            'mechanism_fault':faults[-1]['payload'] if faults else None,
            'blocking':eid not in resolved})
    return rows


def _result_source(ledger, folder):
    """Read only a native operation directory; callers never supply file paths."""
    from runtime_common import read_json
    from delivery_evidence import check_candidate_test_evidence, _file_hash
    base=ledger.path.parent/'operations';folder=Path(folder)
    if folder.is_symlink() or folder.parent.resolve()!=base.resolve():
        raise ValueError('EXCEPTION_OPERATION_SOURCE_OUTSIDE_RUNTIME')
    manifest=read_json(folder/'request.json',{}) or {}
    candidate=manifest.get('candidate') or {}
    phase=read_json(folder/'execution-phase.json',{}) or {}
    command_id=phase.get('command_id')
    if (not isinstance(command_id,str) or hashlib.sha256(command_id.encode()).hexdigest()[:32]!=folder.name
            or not candidate.get('work_id')):
        raise ValueError('EXCEPTION_NATIVE_REQUEST_REQUIRED')
    path=folder/'verification.json'
    if not path.is_file():path=folder/'environment-failure.json'
    result=read_json(path,{}) or {}
    if result.get('status')!='UNVERIFIED' or result.get('candidate')!=candidate:
        raise ValueError('EXCEPTION_UNVERIFIED_FIXED_RESULT_REQUIRED')
    # A frozen native reference must stay inside this operation. Missing or
    # changed evidence is an uncertainty, never permission to publish FAIL.
    refs=[]
    runner=result.get('runner_evidence')
    if runner:
        expected=folder/'result.evidence.json'
        if runner.get('path')!=str(expected) or expected.is_symlink() or _file_hash(expected)!=runner.get('sha256'):
            raise ValueError('EXCEPTION_RUNNER_SOURCE_CHANGED')
        refs.append(runner)
    frozen={**candidate,'path':str(folder/'artifacts/junit.xml'),
        'sha256':(result.get('test_stats') or {}).get('sha256'),
        'exit_code':(read_json(folder/'result.evidence.json',{}) or {}).get('exit_code'),
        'runner_evidence':runner or {}}
    if (result.get('test_stats') or {}).get('stability_evidence'):
        frozen['stability']=result['test_stats']['stability_evidence']
    checked=check_candidate_test_evidence(frozen,candidate) if runner else {'valid':False,'reasons':['ENVIRONMENT_NOT_VERIFIED']}
    return candidate,{'path':str(path),'sha256':_file_hash(path)},result,checked,refs


def record(ledger, folder):
    """Record native uncertainty and request technical retirement, not closure."""
    from business_ledger import _deny
    candidate,source,result,checked,refs=_result_source(ledger,folder)
    def operation(db):
        work=ledger._one(db,'works','work_id',candidate['work_id'])
        if (work['kind']!='supplemental_verification' or work['candidate_id']!=candidate['candidate_id']
                or work['run_id']!=candidate['run_id'] or work['attempt']!=candidate['attempt']):
            _deny('EXCEPTION_SUPPLEMENTAL_BINDING_MISMATCH')
        current=ledger._one(db,'candidates','candidate_id',work['candidate_id'])
        if any(current[k]!=candidate[k] for k in ('head','ac_version','base','repo','task_id','run_id','attempt')):
            _deny('EXCEPTION_CANDIDATE_CHANGED')
        if ledger._authority(db)['current_run_id']!=work['run_id']:
            _deny('EXCEPTION_NOT_CURRENT_RUN')
        from workflow_supplemental import _context_from_event
        context=_context_from_event(db,work)
        ledger._evidence(source)
        payload={'candidate_id':work['candidate_id'],'head':candidate['head'],
            'task_id':work['task_id'],'attempt':work['attempt'],'result':'UNVERIFIED',
            'profile':context['profile'],'request_event_id':context['request_event_id'],
            'source':source,'runner_sources':refs,
            'reason_codes':list(dict.fromkeys([*(result.get('test_stats') or {}).get('reasons',[]),
                *checked['reasons'],result.get('code','TEST_RESULT_UNVERIFIED')])),
            'responsible_role':'COORDINATOR','business_result_unchanged':True,
            'next':'协调者核原始证据后处置；TESTER safe_release并结束执行，原生核退后释放角色；不重跑旧结果、不判PASS/FAIL。'}
        event=ledger._event(db,'VERIFICATION_EXCEPTION_RECORDED',payload,
            run_id=work['run_id'],role='COORDINATOR',work_id=work['work_id'])
        from workflow_verification_lifecycle import request
        retirement=request(db,ledger,work,'UNVERIFIED证据已保留并移交协调者，技术工作单安全退休；不是业务通过。')
        return {'event_id':event,'retirement_event_id':retirement,'result':'UNVERIFIED','work_id':work['work_id'],
            'responsible_role':'COORDINATOR','writer_clearance_required':True}
    key=hashlib.sha256(json.dumps([candidate['work_id'],source],sort_keys=True).encode()).hexdigest()
    return ledger._command('record_verification_exception','verification-exception:'+key,[candidate,source],operation)


def recover(ledger):
    """Recover previously saved results only for still-open supplemental work."""
    from runtime_common import read_json
    snapshot=ledger.snapshot();run_id=snapshot['current_run_id']
    known={e['work_id'] for e in snapshot['events'] if e['kind']=='VERIFICATION_EXCEPTION_RECORDED'}
    pending={w['work_id'] for w in snapshot['works'] if w['run_id']==run_id and
        w['kind']=='supplemental_verification' and w['state']!='ENDED' and w['work_id'] not in known}
    if not pending:return []
    results=[]
    for folder in sorted((ledger.path.parent/'operations').iterdir()) if (ledger.path.parent/'operations').is_dir() else []:
        if not folder.is_dir() or folder.is_symlink():continue
        try:
            manifest=read_json(folder/'request.json',{}) or {}
            work_id=(manifest.get('candidate') or {}).get('work_id')
            if work_id not in pending:continue
            result=record(ledger,folder)
            if result.get('status') in ('COMMITTED','DUPLICATE'):
                results.append(result);pending.remove(work_id)
            if not pending:break
        except (OSError,ValueError,TypeError,KeyError):
            continue  # Untrusted/missing evidence remains unknown, never success.
    return results


def _repair_context(db, ledger, run_id, payload):
    """One canonical preflight shared by request and deferred execution."""
    from business_ledger import _deny, _row
    authority=ledger._authority(db);run=ledger._one(db,'runs','run_id',run_id)
    if (authority['mode']!='ACTIVE' or not authority['admission_open'] or authority['current_run_id']!=run_id
            or run['control']!='ACTIVE'):_deny('EXCEPTION_REPAIR_RUN_NOT_ACTIVE')
    candidate=ledger._one(db,'candidates','candidate_id',payload['candidate_id'])
    task=_row(db.execute('SELECT * FROM tasks WHERE run_id=? AND task_id=?',(run_id,candidate['task_id'])).fetchone())
    if (candidate['run_id']!=run_id or candidate['state']!='CURRENT' or not task
            or task['attempt']!=candidate['attempt'] or task['phase']!='COMPLETED'):
        _deny('EXCEPTION_REPAIR_CURRENT_COMPLETED_TASK_REQUIRED')
    if not db.execute('SELECT 1 FROM merges WHERE candidate_id=?',(candidate['candidate_id'],)).fetchone():
        _deny('EXCEPTION_REPAIR_MERGED_SUPPLIER_REQUIRED')
    paths=payload.get('affected_paths')
    if (not isinstance(paths,list) or not paths or len(paths)>32 or any(not isinstance(p,str)
            or Path(p).is_absolute() or '..' in Path(p).parts or '.git' in Path(p).parts
            or any(c in p for c in '*?[') or not any(fnmatch.fnmatchcase(p,scope) for scope in task['scope']) for p in paths)):
        _deny('EXCEPTION_REPAIR_MUST_STAY_IN_ORIGINAL_SCOPE')
    from workflow_development_policy import policies
    snapshot={'authorizations':[_row(r) for r in db.execute('SELECT * FROM authorizations ORDER BY created_at')],
        'runs':[run]}
    grants=policies(snapshot,candidate,ledger.clock())
    if not grants:_deny('EXCEPTION_REPAIR_LOCAL_POLICY_REQUIRED')
    grant=grants[0];ledger._authorization(db,grant['authorization_id'],'development_policy')
    from workflow_integration_workspace import completed_head,git
    snapshot=ledger.snapshot(run_id)
    base=completed_head(ledger,snapshot,run_id)
    if not base or git(run['repo'],'rev-parse','refs/heads/'+grant['spec']['branch'])!=base:
        _deny('EXCEPTION_REPAIR_AUDITED_INTEGRATION_BASE_REQUIRED')
    if git(run['repo'],'merge-base',candidate['head'],base)!=candidate['head']:
        _deny('EXCEPTION_REPAIR_SUPPLIER_NOT_IN_BASE')
    return candidate,task,base


def resolve(ledger, identity, payload, *, command_id):
    from business_ledger import _deny, _row
    def operation(db):
        work,_=ledger._identity(db,identity)
        if work['role']!='COORDINATOR':_deny('COORDINATOR_REQUIRED')
        observed=ledger._one(db,'events','event_id',payload['observation_event_id'])
        if observed['kind']!='VERIFICATION_EXCEPTION_RECORDED' or observed['run_id']!=work['run_id']:
            _deny('EXCEPTION_OBSERVATION_SCOPE_MISMATCH')
        source=observed['payload'];ledger._evidence(source['source'])
        events=[_row(r) for r in db.execute('SELECT * FROM events WHERE run_id=? ORDER BY event_id',(work['run_id'],))]
        prior=[e for e in events if e['kind']=='VERIFICATION_EXCEPTION_DISPOSITION_RECORDED'
            and e['payload']['observation_event_id']==observed['event_id']]
        if any(e['kind']=='VERIFICATION_EXCEPTION_CLOSED' and e['payload']['observation_event_id']==observed['event_id'] for e in events):
            _deny('EXCEPTION_ALREADY_CLOSED')
        if not isinstance(payload.get('reason'),str) or not payload['reason'].strip():_deny('EXCEPTION_SEMANTIC_REASON_REQUIRED')
        if payload['disposition']=='repair':
            if prior and prior[-1]['payload']['disposition']=='repair' and not any(
                    e['kind']=='VERIFICATION_EXCEPTION_REPAIR_BLOCKED' and e['payload']['disposition_event_id']==prior[-1]['event_id'] for e in events):
                _deny('EXCEPTION_REPAIR_ALREADY_REQUESTED',disposition_event_id=prior[-1]['event_id'])
            candidate,task,base=_repair_context(db,ledger,work['run_id'],payload)
            detail={**payload,'task_id':task['task_id'],'owner':task['role'],'expected_attempt':task['attempt'],
                'source_work_id':observed['work_id'],
                'expected_head':candidate['head'],'base_at_request':base,'scope_unchanged':True,
                'requires_user_approval':False,'next':'原writer核退后由原生tick落实新attempt；不等待集成验证先失败。'}
        elif payload['disposition']=='investigate':
            if prior and prior[-1]['payload']['disposition']=='repair' and not any(
                    e['kind']=='VERIFICATION_EXCEPTION_REPAIR_BLOCKED' and e['payload']['disposition_event_id']==prior[-1]['event_id'] for e in events):
                _deny('EXCEPTION_PENDING_REPAIR_CANNOT_BE_OVERWRITTEN')
            if not isinstance(payload.get('next_action'),str) or not payload['next_action'].strip():_deny('EXCEPTION_INVESTIGATION_EXIT_CONDITION_REQUIRED')
            detail={**payload,'owner':'COORDINATOR','review_after':ledger.clock()+300,
                'next':'到期复核新证据后选择原范围返修或提交真实复验依据；不把机制缺口转为等待用户授权。'}
        elif payload['disposition']=='close':
            verification=ledger._one(db,'verifications','verification_id',payload['verification_id'])
            if verification['check_name']!='supplemental_mysql' or verification['result']!='PASS':
                _deny('EXCEPTION_CLOSURE_REQUIRES_REAL_SUPPLEMENTAL_PASS')
            published=next((e for e in events if e['kind']=='VERIFICATION_PUBLISHED'
                and e['payload']['verification_id']==verification['verification_id']),None)
            if not published or published['event_id']<=observed['event_id']:_deny('EXCEPTION_CLOSURE_NEW_PASS_REQUIRED')
            target=ledger._one(db,'works','work_id',published['work_id'])
            from workflow_supplemental import _context_from_event
            ctx=_context_from_event(db,target)
            if ctx['request_event_id']!=source['request_event_id']:_deny('EXCEPTION_CLOSURE_PROFILE_LINEAGE_MISMATCH')
            ledger._evidence(verification['evidence'])
            from delivery_evidence import check_candidate_test_evidence
            candidate=ledger._one(db,'candidates','candidate_id',target['candidate_id'])
            task=_row(db.execute('SELECT * FROM tasks WHERE run_id=? AND task_id=?',
                (work['run_id'],candidate['task_id'])).fetchone())
            if (candidate['state']!='CURRENT' or not task or task['phase']!='COMPLETED'
                    or task['attempt']!=candidate['attempt']):_deny('EXCEPTION_CLOSURE_CURRENT_SUPPLIER_REQUIRED')
            if not check_candidate_test_evidence(verification['evidence'],candidate)['valid']:
                _deny('EXCEPTION_CLOSURE_EVIDENCE_CHANGED')
            event=ledger._event(db,'VERIFICATION_EXCEPTION_CLOSED',{**payload,'previous_result':'UNVERIFIED',
                'previous_result_unchanged':True,'pass_event_id':published['event_id']},
                run_id=work['run_id'],work_id=work['work_id'],role='COORDINATOR')
            return {'event_id':event,'closed':True,'previous_result_unchanged':True}
        else:_deny('EXCEPTION_DISPOSITION_UNSUPPORTED')
        event=ledger._event(db,'VERIFICATION_EXCEPTION_DISPOSITION_RECORDED',detail,
            run_id=work['run_id'],work_id=work['work_id'],role='COORDINATOR')
        return {'event_id':event,'state':'PENDING_NATIVE_REPAIR' if payload['disposition']=='repair' else 'INVESTIGATING',
            'owner':detail['owner'],'previous_result':'UNVERIFIED','requires_user_approval':False}
    return ledger._command('resolve_verification_exception',command_id,[identity,payload],operation)


def reconcile(ledger):
    """Apply a recorded repair once, only after the real writer fence clears."""
    from business_ledger import _deny, _row
    snapshot=ledger.snapshot();run_id=snapshot['current_run_id'];results=[]
    for item in facts(snapshot,run_id):
        if item['state']!='WAITING_SAFE_EXIT':continue
        decision=item['disposition'];payload=decision['payload']
        # Avoid storing a permanent no-op command while the writer is alive.
        if any(w['run_id']==run_id and (w['task_id']==payload['task_id'] or w['work_id']==payload['source_work_id']) and
                (w['state']!='ENDED' or w['active_execution_id'] or any(e['work_id']==w['work_id']
                    and e['state']!='ENDED' for e in snapshot['executions'])) for w in snapshot['works']):continue
        def operation(db,payload=payload,decision=decision):
            candidate,task,base=_repair_context(db,ledger,run_id,payload)
            live=db.execute("SELECT 1 FROM works w WHERE w.run_id=? AND (w.task_id=? OR w.work_id=?) AND "
                "(w.state!='ENDED' OR w.active_execution_id IS NOT NULL OR EXISTS "
                "(SELECT 1 FROM executions e WHERE e.work_id=w.work_id AND e.state!='ENDED'))",
                (run_id,task['task_id'],payload['source_work_id'])).fetchone()
            if live:_deny('EXCEPTION_REPAIR_WAITING_FOR_NATIVE_WRITER')
            if task['attempt']!=payload['expected_attempt'] or candidate['head']!=payload['expected_head']:
                _deny('EXCEPTION_REPAIR_TARGET_CHANGED')
            attempt=task['attempt']+1
            db.execute("UPDATE candidates SET state='SUPERSEDED' WHERE candidate_id=?",(candidate['candidate_id'],))
            db.execute("UPDATE attempts SET state='SUPERSEDED' WHERE run_id=? AND task_id=? AND attempt=?",
                (run_id,task['task_id'],task['attempt']))
            db.execute("INSERT INTO attempts VALUES(?,?,?,'CURRENT',?,?)",
                (run_id,task['task_id'],attempt,candidate['candidate_id'],payload['reason']))
            db.execute("UPDATE tasks SET phase='PENDING',attempt=?,base=? WHERE run_id=? AND task_id=?",
                (attempt,base,run_id,task['task_id']))
            event=ledger._event(db,'VERIFICATION_EXCEPTION_REPAIR_QUEUED',{
                'observation_event_id':payload['observation_event_id'],'disposition_event_id':decision['event_id'],
                'task_id':task['task_id'],'attempt':attempt,'previous_candidate_id':candidate['candidate_id'],
                'base':base,'owner':task['role'],'affected_paths':payload['affected_paths'],
                'reason':payload['reason'],'scope_unchanged':True,'ac_unchanged':True,'required_checks_unchanged':True},
                run_id=run_id,role=task['role'],notify=True)
            return {'event_id':event,'queued':True,'task_id':task['task_id'],'attempt':attempt}
        # Current prerequisites participate in retry identity; a transient refusal
        # is not replayed forever after controls/authority have changed.
        from business_ledger import _hash
        version=[decision['event_id'],snapshot['generation'],snapshot['mode'],snapshot['admission_open'],
            [r for r in snapshot['runs'] if r['run_id']==run_id],
            [t for t in snapshot['tasks'] if t['task_id']==payload['task_id']],
            [(a['authorization_id'],a['revoked_at']) for a in snapshot['authorizations']],
            [(m['candidate_id'],m['head']) for m in snapshot['merges']]]
        result=ledger._command('queue_verification_exception_repair','exception-repair:'+_hash(version),
            [decision['event_id']],operation)
        results.append(result)
        if result.get('status') in ('REJECTED','UNVERIFIED'):
            def fault(db,result=result,decision=decision):
                event=ledger._event(db,'VERIFICATION_EXCEPTION_REPAIR_BLOCKED',{
                    'observation_event_id':decision['payload']['observation_event_id'],
                    'disposition_event_id':decision['event_id'],'code':result['code'],
                    'diagnostic_id':(result.get('decision') or {}).get('decision_id'),
                    'owner':'COORDINATOR','next':'刷新原合同/固定候选/授权/集成基线并重新处置；非业务FAIL，不同因循环提交。'},
                    run_id=run_id,role='COORDINATOR')
                return {'event_id':event,'mechanism_fault_recorded':True}
            results.append(ledger._command('record_exception_repair_blocker',
                'exception-repair-blocked:'+_hash(version),[decision['event_id'],result['code']],fault))
    return results


def revalidation_ids(snapshot, previous_work_ids, candidate):
    return [item['observation_event_id'] for item in facts(snapshot,candidate['run_id'])
        if item['blocking'] and item['repair'] and item['work_id'] in previous_work_ids
        and repair_descendant(snapshot,item,candidate)]


def repair_descendant(snapshot, item, candidate):
    """Follow native attempt links; an additional repair must not orphan UNVERIFIED."""
    repair=item.get('repair') or {}
    if repair.get('task_id')!=candidate['task_id']:return False
    attempts={a['attempt']:a for a in snapshot.get('attempts',[]) if a['run_id']==candidate['run_id']
        and a['task_id']==candidate['task_id']}
    candidates={c['candidate_id']:c for c in snapshot.get('candidates',[]) if c['run_id']==candidate['run_id']
        and c['task_id']==candidate['task_id']}
    current=candidate
    while current['attempt']>repair['attempt']:
        link=attempts.get(current['attempt'],{}).get('previous_candidate_id')
        parent=candidates.get(link)
        if not parent or parent['attempt']>=current['attempt']:return False
        current=parent
    return current['attempt']==repair['attempt']


def repair_followup(snapshot, item):
    """Name the actual repair/supplemental owner rather than the old candidate."""
    repair=item.get('repair')
    if not repair:return None
    task=next((t for t in snapshot.get('tasks',[]) if t['run_id']==item['disposition']['run_id']
        and t['task_id']==repair['task_id']),None)
    candidate=next((c for c in snapshot.get('candidates',[]) if task and c['run_id']==task['run_id']
        and c['task_id']==task['task_id'] and c['attempt']==task['attempt'] and c['state']=='CURRENT'),None)
    result={'task_id':repair['task_id'],'repair_attempt':repair['attempt'],
        'current_attempt':task['attempt'] if task else None,'phase':task['phase'] if task else 'MISSING',
        'candidate_id':candidate['candidate_id'] if candidate else None,
        'head':candidate['head'] if candidate else None,'gate_satisfied':False}
    if not task:
        return {**result,'owner':'COORDINATOR','code':'REPAIR_TASK_MISSING','next':'核返修派单与当前计划，保持异常未关闭。'}
    if task['phase']!='COMPLETED':
        return {**result,'owner':'COORDINATOR' if task['phase']=='REVIEW' else task['role'],
            'code':'REPAIR_REVIEW_REQUIRED' if task['phase']=='REVIEW' else 'REPAIR_DEVELOPMENT_REQUIRED',
            'next':'先处理本返修候选的独立测试、OCR、批准及合入完成；不是重新处置旧候选，不重复创建返修。'}
    passes=closure_passes(snapshot,item)
    if passes:
        return {**result,'owner':'COORDINATOR','code':'VERIFICATION_EXCEPTION_CLOSE_REQUIRED',
            'later_pass_options':passes,
            'next':'同源supplemental_mysql PASS已落账；协调者核原始补验证据后显式close，不再等待或重复派复验。异常保持未关闭，普通独立测试不能替代该证据。'}
    queued=[e for e in snapshot['events'] if e['kind']=='SUPPLEMENTAL_REVALIDATION_QUEUED'
        and candidate and e['payload'].get('candidate_id')==candidate['candidate_id']
        and e['payload'].get('original_request_event_id')==item['request_event_id']]
    return {**result,'owner':'TESTER' if queued else 'COORDINATOR',
        'code':'SUPPLEMENTAL_REVALIDATION_QUEUED' if queued else 'SUPPLEMENTAL_REVALIDATION_RECONCILE_REQUIRED',
        'work_id':queued[-1]['work_id'] if queued else None,
        'next':'等待原请求谱系的真实supplemental_mysql复验；普通独立测试PASS不能关闭异常。复验PASS后协调者显式close。'}


def closure_passes(snapshot, item):
    """Later current PASS options, including a subsequent repair in the lineage.

    This is a read-only index, not evidence validation or a closure verdict.
    resolve() still verifies the original native profile and runner evidence.
    """
    roots={}
    for event in snapshot['events']:
        if event['kind']=='SUPPLEMENTAL_VERIFICATION_REQUESTED':
            roots[event['payload']['work_id']]=event['event_id']
        elif event['kind']=='SUPPLEMENTAL_REVALIDATION_QUEUED':
            roots[event['work_id']]=event['payload']['original_request_event_id']
    candidates={c['candidate_id']:c for c in snapshot['candidates'] if c['state']=='CURRENT'}
    checks={v['verification_id']:v for v in snapshot['verifications']
        if v['check_name']=='supplemental_mysql' and v['result']=='PASS'}
    result=[]
    for event in snapshot['events']:
        if (event['kind']!='VERIFICATION_PUBLISHED' or event['event_id']<=item['observation_event_id']
                or roots.get(event['work_id'])!=item['request_event_id']):continue
        check=checks.get(event['payload']['verification_id'])
        candidate=candidates.get(check['candidate_id']) if check else None
        if candidate and any(t['run_id']==candidate['run_id'] and t['task_id']==candidate['task_id']
                and t['attempt']==candidate['attempt'] and t['phase']=='COMPLETED' for t in snapshot['tasks']):
            result.append({'verification_id':check['verification_id'],'candidate_id':candidate['candidate_id'],
                'published_event_id':event['event_id']})
    return result


def blockers(snapshot, run_id):
    """Expose structural waits with a named owner; do not infer a business FAIL."""
    rows=[];works={w['work_id']:w for w in snapshot['works']}
    integration=[w for w in snapshot['works'] if w['run_id']==run_id and
        w['kind']=='integration_verification' and w['state']!='ENDED']
    for item in facts(snapshot,run_id):
        if not item['blocking']:continue
        work=works[item['work_id']]
        rows.append({'code':'VERIFICATION_EXCEPTION_HANDOFF_REQUIRED' if not item['disposition'] else
            'VERIFICATION_EXCEPTION_REPAIR_PENDING','observation_event_id':item['observation_event_id'],
            'owner':'COORDINATOR','state':item['state'],'work_id':item['work_id'],
            'wait_chain':(['integration:'+w['work_id'] for w in integration]+['TESTER:'+work['work_id'],
                'COORDINATOR:resolve_verification_exception']),
            'business_result':'UNVERIFIED','requires_user_approval':False,
            'next':'使用异常处置入口登记原范围返修或调查，不重复补验、不凭worker退出推定work完成。'})
    return rows
