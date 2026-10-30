"""Accept an explicit native requirement package without a maintenance-only API."""
import hashlib
from pathlib import Path
import re
import subprocess


def explicit_development(primary):
    """Accept common native request phrasing without treating queries as commands."""
    if re.search(r'不要(?:开发|实现|交付|自动推进)|暂不(?:开发|实现)|不授权(?:开发|自动推进)',primary):
        return False
    imperative=re.match(r'^(?:(?:请|帮我|现在|立即|我要|我需要|请你|我想|需要|麻烦|帮忙|给我|我们|能否|能不能)\s*)*'
                        r'(?:开发|实现|交付|自动推进|新增|增加|修复|优化|完成)',primary)
    introduced=re.match(r'^(?:我(?:有|要新增|想新增)(?:一)?个需求|新增需求|需求(?:说明)?)[：:\s]',primary)
    return bool(imperative or introduced and re.search(r'请(?:帮我)?(?:开发|实现|修复|自动推进|自动交付)|自动推进并交付',primary))


def accept(ledger, target, provenance, *, command_id):
    from business_ledger import _deny, _json
    from manual_access import full_permission_action
    primary=full_permission_action(provenance['primary_text'])
    primary=primary if primary is not None else provenance['primary_text'].strip()
    if (provenance.get('authenticated') is not True or not provenance.get('message_id') or not provenance.get('user_id')
            or not explicit_development(primary)):
        raise ValueError('EXPLICIT_REQUIREMENT_DEVELOPMENT_SOURCE_REQUIRED')
    required={'requirement_id','requirement_root','repo','task_ids'}
    if not isinstance(target,dict) or not required<=set(target) or set(target)-required-{'run_id','merge_branch','contract_files','use_native_requirement'}:
        raise ValueError('EXACT_REQUIREMENT_PACKAGE_REQUIRED')
    root=Path(target['requirement_root']).resolve();repo=Path(target['repo']).resolve()
    if not root.is_dir() or not repo.is_dir() or not any(value in primary for value in (target['requirement_id'],str(root))):
        raise ValueError('REQUIREMENT_NOT_IN_NATIVE_PRIMARY_SOURCE')
    known=ledger.snapshot()['runs']
    if not any(str(repo)==row['repo'] for row in known) and str(repo) not in primary:
        raise ValueError('NEW_REPOSITORY_MUST_BE_EXPLICIT_IN_NATIVE_SOURCE')
    files=target.get('contract_files',[])
    inline=target.get('use_native_requirement') is True
    if inline and files:
        raise ValueError('AMBIGUOUS_REQUIREMENT_SOURCE')
    if inline:
        if (len(primary)>65536 or not isinstance(target['task_ids'],list) or not target['task_ids']
                or any(not isinstance(task,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}',task) for task in target['task_ids'])):
            raise ValueError('BOUNDED_NATIVE_REQUIREMENT_REQUIRED')
        raw=(primary+'\n\n运行时分解用任务身份（不增加需求范围）：\n'+'\n'.join(target['task_ids'])).encode()
        sha=hashlib.sha256(raw).hexdigest()
        frozen=ledger.path.parent/'requirements'/sha;frozen.parent.mkdir(exist_ok=True)
        if not frozen.exists():
            with frozen.open('xb') as output:output.write(raw)
            frozen.chmod(0o400)
        elif frozen.read_bytes()!=raw:
            raise ValueError('FROZEN_REQUIREMENT_SOURCE_CHANGED')
        sources=[{'path':str(frozen),'frozen_path':str(frozen),'sha256':sha,'origin':'native_primary_text'}]
        texts=[raw.decode()]
    else:
        sources=[];texts=[]
    if not isinstance(files,list) or (not files and not inline) or len(files)>20:
        raise ValueError('FROZEN_REQUIREMENT_CONTRACT_FILES_REQUIRED')
    for name in files:
        path=(root/name).resolve()
        if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size>1024*1024:
            raise ValueError('REQUIREMENT_CONTRACT_FILE_OUTSIDE_ROOT_OR_TOO_LARGE')
        raw=path.read_bytes(); texts.append(raw.decode('utf-8'))
        sha=hashlib.sha256(raw).hexdigest()
        frozen=ledger.path.parent/'requirements'/sha
        frozen.parent.mkdir(exist_ok=True)
        if frozen.exists() and frozen.read_bytes()!=raw:
            raise ValueError('FROZEN_REQUIREMENT_SOURCE_CHANGED')
        if not frozen.exists():
            with frozen.open('xb') as output:
                output.write(raw)
            frozen.chmod(0o400)
        sources.append({'path':str(path),'sha256':sha,'frozen_path':str(frozen)})
    tasks=target['task_ids']
    if (not isinstance(tasks,list) or not tasks or any(not isinstance(task,str)
            or not any(re.search(r'(?<![A-Za-z0-9_-])'+re.escape(task)+r'(?![A-Za-z0-9_-])',text) for text in texts) for task in tasks)):
        raise ValueError('TASK_IDS_MUST_BE_PRESENT_IN_ACCEPTED_CONTRACT')
    subprocess.run(['git','-C',str(repo),'rev-parse','--verify','HEAD'],check=True,capture_output=True,timeout=15)
    branch=target.get('merge_branch')
    if branch:
        if any(re.search(r'(?:不要|不得|禁止|暂不|不允许)[^,，;；。\n]{0,60}(?:合入|合并)',clause)
               for clause in re.split(r'[,，;；。\n]',primary)):
            raise ValueError('MERGE_BOUNDARY_MUST_BE_UNAMBIGUOUS')
        if (not isinstance(branch,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9/_.-]{0,150}',branch)
                or '..' in branch or branch not in primary or not re.search(r'合入|合并',primary)):
            raise ValueError('EXACT_MERGE_BRANCH_NOT_IN_REQUIREMENT_SOURCE')
        actual=subprocess.check_output(['git','-C',str(repo),'symbolic-ref','--short','HEAD'],text=True,timeout=15).strip()
        if actual!=branch:
            raise ValueError('REQUIREMENT_MERGE_BRANCH_MUST_MATCH_CURRENT_DESTINATION')
    from workflow_registration import prepared_package
    registered=prepared_package(ledger,target)
    if registered and registered.get('requires_explicit_task_ids') and any(
            not re.search(r'(?<![A-Za-z0-9_-])'+re.escape(task)+r'(?![A-Za-z0-9_-])',primary) for task in tasks):
        raise ValueError('HISTORICAL_REQUIREMENT_NEEDS_EXPLICIT_CURRENT_TASK_SCOPE')
    identifier=(registered or {}).get('run_id') or target.get('run_id') or 'accepted-'+hashlib.sha256(command_id.encode()).hexdigest()[:24]
    if not registered:
        created=ledger.create_run(target['requirement_id'],root,repo,tasks,run_id=identifier,command_id=command_id+':register')
        if created.get('status') not in ('COMMITTED','DUPLICATE'):
            return created
    def freeze(db):
        if registered:
            run=ledger._one(db,'runs','run_id',identifier)
            if (run['control']!='PAUSED' or run['plan_version'] is not None
                    or db.execute('SELECT 1 FROM works WHERE run_id=?',(identifier,)).fetchone()
                    or db.execute("SELECT 1 FROM events WHERE run_id=? AND kind='REQUIREMENT_ACCEPTED'",(identifier,)).fetchone()):
                _deny('REGISTERED_RUN_ALREADY_ACCEPTED_USE_EXISTING_RUN_CONTROL')
            db.execute("UPDATE runs SET task_ids=?,control='ACTIVE' WHERE run_id=?",(_json(tasks),identifier))
        event=ledger._event(db,'REQUIREMENT_ACCEPTED',{'sources':sources,'provenance':provenance,
            'task_ids':tasks,'production_permission':False,
            'merge_policy':{'repo':str(repo),'branch':branch,'expires_at':ledger.clock()+72*3600} if branch else None},
            run_id=identifier,role='COORDINATOR')
        if branch:
            policy={'run_id':identifier,'repo':str(repo),'branch':branch,
                    'expires_at':ledger.clock()+72*3600,'accepted_event_id':event}
            db.execute('INSERT INTO authorizations VALUES(?,?,?,?,?,NULL)',
                (identifier+'-merge-policy','merge_policy',_json(policy),_json(provenance),ledger.clock()))
        from workflow_development_policy import default_for_request
        autonomy=default_for_request(db,ledger,identifier,tasks,primary,provenance,
                                     branch=branch,accepted_event_id=event)
        return {'run_id':identifier,'event_id':event,'sources':sources,'selected':False,
                'production_permission':False,'development_automation':autonomy}
    result=ledger._command('accept_requirement',command_id,[target,provenance,sources],freeze)
    if result.get('status') not in ('COMMITTED','DUPLICATE'):
        return result
    # A single explicit autonomous requirement accepts the documented finite
    # review envelope. Ordinary registration does not silently change the run.
    if re.search(r'自动推进|自动交付|全流程|按默认流程',primary):
        result['review_policy']=requirement_review_policy(ledger,identifier,result['event_id'],provenance,
                                                         command_id=command_id+':ocr-policy')
        def select(db):
            # This run is exactly the immutable package just accepted above.
            # _select keeps old owners draining; never force-clears a writer.
            return ledger._select(db,identifier)
        result['selection']=ledger._command('select_accepted_requirement',command_id+':select',
                                            [identifier,result['event_id']],select)
        result['next_action']='WAIT_FOR_APPLIED_SELECTION_AND_BACKGROUND_PLAN'
    return result


def scoped_control(provenance, action):
    """A specific imperative is consent to that control, never full tool access."""
    return (action in {'continue','pause','resume','accept_requirement','request_supplemental_verification','record_user_decision'} and isinstance(provenance,dict)
            and provenance.get('authenticated') is True and provenance.get('message_id')
            and provenance.get('user_id') and isinstance(provenance.get('primary_text'),str))


def requirement_review_policy(ledger, run_id, accepted_event_id, provenance, *, command_id):
    """A package acceptance permits only its documented OCR envelope, never full rights."""
    from business_ledger import _deny,_json,_row
    def operation(db):
        accepted=_row(db.execute('SELECT * FROM events WHERE event_id=?',(accepted_event_id,)).fetchone())
        if (not accepted or accepted['kind']!='REQUIREMENT_ACCEPTED' or accepted['run_id']!=run_id
                or accepted['payload']['provenance']!=provenance):
            _deny('FROZEN_NATIVE_REQUIREMENT_ACCEPTANCE_REQUIRED')
        run=ledger._one(db,'runs','run_id',run_id)
        identifier=run_id+'-ocr-policy'
        spec={'run_id':run_id,'task_ids':run['task_ids'],'service_kind':'ocr',
              'max_calls':min(100,max(30,2*len(run['task_ids'])+2)),'expires_at':ledger.clock()+72*3600,
              'execution_seconds':7200,'accepted_event_id':accepted_event_id}
        db.execute('INSERT INTO authorizations VALUES(?,?,?,?,?,NULL)',
                   (identifier,'external',_json(spec),_json(provenance),ledger.clock()))
        event=ledger._event(db,'REQUIREMENT_REVIEW_POLICY_GRANTED',{'authorization_id':identifier,'spec':spec},
                            run_id=run_id,role='COORDINATOR')
        return {'authorization_id':identifier,'event_id':event,'scope':'accepted_requirement_only'}
    return ledger._command('requirement_review_policy',command_id,[run_id,accepted_event_id,provenance],operation)
