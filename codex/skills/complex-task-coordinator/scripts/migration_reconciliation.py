"""Accept reviewed legacy responsibilities into a frozen STAGED ledger.

This adapter never authorizes a writer, dispatches a service, acknowledges old
inputs, or manufactures verification results. Original import bytes remain in
imports; accepted task/attempt mappings and evidence are explicit commands.
"""
from pathlib import Path
import hashlib
import secrets

from business_ledger import _deny, _json, _hash, _row


def accept(ledger, import_id, run_id, review, *, command_id):
    def operation(db):
        authority=ledger._authority(db)
        if authority['mode']!='STAGED' or authority['admission_open']:
            _deny('FROZEN_STAGED_IMPORT_REQUIRED')
        if db.execute('SELECT 1 FROM executions').fetchone() or db.execute('SELECT 1 FROM works').fetchone():
            _deny('MIGRATION_TARGET_ALREADY_USED')
        imported=ledger._one(db,'imports','import_id',import_id)
        frozen=imported['manifest']; manifest=frozen['source_manifest']
        if frozen['state']!='PROPOSED':
            _deny('IMPORT_ALREADY_RECONCILED')
        if not isinstance(review,dict) or not isinstance(review.get('reviewed_by'),str) or not review['reviewed_by'].strip():
            _deny('EXPLICIT_RECONCILIATION_REVIEWER_REQUIRED')
        if review.get('inventory_manifest_sha256')!=manifest['sha256']:
            _deny('EXACT_IMPORT_MANIFEST_REQUIRED')
        records={_hash([r['source'],r['source_sha256'],r['identity']]):r for r in manifest['records']}
        resolutions=review.get('resolutions',[])
        if len(resolutions)!=len(records) or {r.get('record_sha256') for r in resolutions}!=set(records):
            _deny('EVERY_RAW_IDENTITY_REQUIRES_EXPLICIT_RECONCILIATION')
        run=ledger._one(db,'runs','run_id',run_id)
        tasks={r['task_id']:_row(r) for r in db.execute('SELECT * FROM tasks WHERE run_id=?',(run_id,))}
        states=review.get('task_states',[])
        if len(states)!=len(tasks) or {r.get('task_id') for r in states}!=set(tasks) or set(tasks)!=set(run['task_ids']):
            _deny('EXACT_ACCEPTED_TASK_STATES_REQUIRED')
        if not run['plan_version'] or any(t['phase']!='PENDING' or t['attempt']!=1 for t in tasks.values()):
            _deny('PRISTINE_ACCEPTED_PLAN_REQUIRED')
        selected={s['task_id']:s for s in states}
        resolved={r['record_sha256']:r for r in resolutions}
        for row in resolutions:
            if row.get('excluded') is True:
                if not isinstance(row.get('reason'),str) or not row['reason'].strip():
                    _deny('HISTORICAL_RETENTION_REASON_REQUIRED')
                continue
            item=selected.get(row.get('task_id'))
            if not item or row.get('run_id')!=run_id or type(row.get('attempt')) is not int or row['attempt']!=item['attempt']:
                _deny('EXPLICIT_INTEGER_ATTEMPT_AND_TASK_MAPPING_REQUIRED')
        for item in states:
            task=tasks[item['task_id']]
            if type(item.get('attempt')) is not int or item['attempt']<1 or item.get('phase') not in ('PENDING','DEVELOPING','REVIEW'):
                _deny('EXPLICIT_NONTERMINAL_TASK_STATE_REQUIRED')
            if not item.get('legacy_task') or not item.get('legacy_attempt') or not isinstance(item.get('checkpoint'),dict):
                _deny('LEGACY_IDENTITY_AND_CHECKPOINT_REQUIRED')
            source=records.get(item.get('record_sha256'))
            mapping=resolved.get(item.get('record_sha256'),{})
            if (mapping.get('excluded') is True or mapping.get('task_id')!=item['task_id']
                or mapping.get('run_id')!=run_id or mapping.get('attempt')!=item['attempt']):
                _deny('TASK_SOURCE_RECONCILIATION_MISMATCH')
            if (not source or source['raw'].get('role')!=task['role']
                or source['raw'].get('task',source['raw'].get('task_id'))!=item['legacy_task']
                or source['raw'].get('attempt')!=item['legacy_attempt']):
                _deny('TASK_SOURCE_IDENTITY_MISMATCH')
            evidence=item.get('evidence')
            if not isinstance(evidence,list) or not evidence:
                _deny('PINNED_MIGRATION_EVIDENCE_REQUIRED')
            for entry in evidence:
                if not entry.get('sha256'):_deny('PINNED_MIGRATION_EVIDENCE_REQUIRED')
                ledger._evidence(entry)
            for attempt in range(2,item['attempt']+1):
                db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?)',(run_id,task['task_id'],attempt,'CURRENT',None,'Explicit legacy attempt mapping'))
            db.execute("UPDATE attempts SET state='SUPERSEDED' WHERE run_id=? AND task_id=? AND attempt<?",(run_id,task['task_id'],item['attempt']))
            db.execute('UPDATE tasks SET phase=?,attempt=? WHERE run_id=? AND task_id=?',(item['phase'],item['attempt'],run_id,task['task_id']))
            if item['phase']=='REVIEW':
                candidate=item.get('candidate',{})
                if candidate.get('base')!=task['base'] or candidate.get('ac_version')!=task['ac_version']:
                    _deny('CANDIDATE_SCOPE_MISMATCH')
                repo=candidate.get('repo','')
                if ledger._git(repo,'rev-parse','--path-format=absolute','--git-common-dir')!=ledger._git(run['repo'],'rev-parse','--path-format=absolute','--git-common-dir'):
                    _deny('CANDIDATE_REPOSITORY_MISMATCH')
                head=candidate.get('head','')
                if ledger._git(repo,'rev-parse','--verify',head+'^{commit}')!=head or ledger._git(repo,'merge-base','--is-ancestor',task['base'],head)!='':
                    _deny('FROZEN_CANDIDATE_REQUIRED')
                identifier='migrated-'+_hash([import_id,run_id,task['task_id'],item['attempt'],head])[:32]
                db.execute('INSERT INTO candidates VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(identifier,run_id,task['task_id'],item['attempt'],task['role'],repo,task['base'],head,task['ac_version'],_json(evidence),item['checkpoint'].get('summary') or 'Reviewed legacy candidate; gates remain pending','CURRENT',ledger.clock()))
            elif item['phase']=='DEVELOPING':
                if db.execute('SELECT 1 FROM role_current WHERE role=? AND current_work_id IS NOT NULL',(task['role'],)).fetchone():
                    _deny('DUPLICATE_ROLE_RESPONSIBILITY')
                updated={**task,'attempt':item['attempt']}
                work=ledger._new_work(db,run_id,task['role'],'development',updated)
                db.execute('UPDATE works SET checkpoint=? WHERE work_id=?',(_json(item['checkpoint']),work['work_id']))
        coord=review.get('coordinator_checkpoint')
        if not isinstance(coord,dict) or not isinstance(coord.get('pending_actions'),list) or not isinstance(coord.get('open_gates'),list):
            _deny('COORDINATOR_OBLIGATIONS_REQUIRED')
        work=ledger._new_work(db,run_id,'COORDINATOR','coordination')
        db.execute('UPDATE works SET checkpoint=? WHERE work_id=?',(_json(coord),work['work_id']))
        frozen.update(state='RECONCILED',review=review,run_id=run_id,schedulable=False)
        db.execute('UPDATE imports SET manifest=? WHERE import_id=?',(_json(frozen),import_id))
        event=ledger._event(db,'MIGRATION_RECONCILED',{'import_id':import_id,'review_sha256':_hash(review),'task_ids':sorted(tasks),'no_verifications_created':True},run_id=run_id)
        return {'import_id':import_id,'run_id':run_id,'event_id':event,'task_count':len(tasks),'no_verifications_created':True}
    return ledger._command('accept_migration',command_id,[import_id,run_id,review],operation)


def adopt_ocr(ledger, import_id, task_id, directory, *, legacy_attempt, command_id):
    """Attach an already dispatched OCR for collection; never relaunch or approve it."""
    from runtime_common import locked, read_json, save_json
    from workflow_services import _directory
    from workflow_migration import service, file_sha
    directory=Path(directory).resolve();spec_path=directory/'spec.json';spec=read_json(spec_path)
    snapshot=ledger.snapshot()
    if snapshot['mode']!='STAGED' or snapshot['admission_open']:
        raise ValueError('FROZEN_STAGED_IMPORT_REQUIRED')
    with ledger._connection() as db:imported=ledger._one(db,'imports','import_id',import_id)
    frozen=imported['manifest']
    if frozen['state']!='RECONCILED':raise ValueError('RECONCILED_IMPORT_REQUIRED')
    item=next(s for s in frozen['review']['task_states'] if s['task_id']==task_id)
    run=next(r for r in snapshot['runs'] if r['run_id']==frozen['run_id'])
    candidate=next(c for c in snapshot['candidates'] if c['run_id']==run['run_id'] and c['task_id']==task_id and c['state']=='CURRENT')
    source=next((s for s in frozen['source_manifest']['sources'] if s['path']==str(spec_path)),None)
    if (not source or source['sha256']!=file_sha(spec_path) or spec.get('task')!=item['legacy_task']
        or spec.get('attempt')!=legacy_attempt or any(spec.get(k)!=candidate[k] for k in ('base','head','repo'))
        or Path(spec.get('project','')).resolve()!=Path(run['requirement_root']).resolve()
        or not spec.get('run_id') or spec.get('unit')!='coordinator-ocr-'+spec['run_id']+'.service'):
        raise ValueError('EXACT_EXISTING_OCR_SCOPE_REQUIRED')
    observed=service(spec['unit'])
    if observed['state']=='unknown':raise ValueError('EXISTING_OCR_SERVICE_UNVERIFIED')
    action_id='migrated-ocr-'+spec['run_id'];target=_directory(ledger,action_id)
    with locked(target.with_suffix('.lock')):
        target.mkdir(parents=True,exist_ok=True);sidecar=target/'invocation.json'
        value=read_json(sidecar) if sidecar.exists() else None
        fingerprint=_hash([import_id,task_id,str(spec_path),source['sha256'],legacy_attempt])
        if value and value['input_sha256']!=fingerprint:raise ValueError('MIGRATED_SERVICE_MAPPING_CONFLICT')
        if not value:
            token=secrets.token_hex(32)
            value={'schema_version':1,'input_sha256':fingerprint,'ledger_database':str(ledger.path),
                'action_id':action_id,'kind':'ocr','candidate_id':candidate['candidate_id'],'state':'ADOPTING',
                'service_identity':{'service_id':'ocr:'+action_id,'token':token},
                'frozen':{'candidate':candidate,'run':run},
                'reference':{'kind':'ocr','native_directory':str(directory),'native_id':spec['run_id'],
                    'unit':spec['unit'],'spec':{'path':str(spec_path),'sha256':source['sha256']}},
                'legacy_adoption':{'import_id':import_id,'original_task':spec['task'],
                    'original_attempt':legacy_attempt,'dispatch_replay_allowed':False,
                    'service_observation':observed}}
            save_json(sidecar,value)
        service_identity=value['service_identity']
        authorization={'service_id':service_identity['service_id'],
            'service_token_sha256':hashlib.sha256(service_identity['token'].encode()).hexdigest(),
            'legacy_adoption':value['legacy_adoption'],'source_spec':value['reference']['spec']}
        def operation(db):
            authority=ledger._authority(db)
            if authority['mode']!='STAGED' or authority['admission_open']:_deny('FROZEN_STAGED_IMPORT_REQUIRED')
            if db.execute('SELECT 1 FROM external_actions WHERE action_id=?',(action_id,)).fetchone():_deny('EXTERNAL_ACTION_ALREADY_EXISTS')
            db.execute('INSERT INTO external_actions VALUES(?,?,?,?,?,?,?)',
                (action_id,candidate['candidate_id'],'ocr',_json(authorization),'SUBMITTED',ledger.clock(),None))
            event=ledger._event(db,'EXISTING_SERVICE_ADOPTED',{'action_id':action_id,'candidate_id':candidate['candidate_id'],
                'reference':value['reference'],'no_dispatch':True,'business_pass':False},run_id=run['run_id'])
            return {'action_id':action_id,'event_id':event,'no_dispatch':True,'business_pass':False}
        result=ledger._command('adopt_existing_ocr',command_id,[fingerprint,authorization],operation)
        if result.get('original_status',result['status'])=='COMMITTED':
            value['state']='SUBMITTED';save_json(sidecar,value)
        return result
