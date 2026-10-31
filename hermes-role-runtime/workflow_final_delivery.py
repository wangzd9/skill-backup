"""显式授权的最终 Git 交付；固定本地入口，不开放通用 shell。"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from runtime_common import locked, read_json, save_json

SCRIPTS = ('scripts/local_stack.sh', 'scripts/local_quality_gate.sh')
STAGES = ('fetch', 'merge', 'push', 'local_up', 'local_test', 'quality_gate')


def git(repo, *args):
    env = {k:v for k,v in os.environ.items() if not k.startswith('GIT_')}
    env['GIT_TERMINAL_PROMPT'] = '0'
    return subprocess.check_output(['git', '-C', str(repo), *args], env=env,
        stderr=subprocess.DEVNULL, text=True, timeout=30).strip()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def source(text, target):
    """模型只能解析用户当前明确指定的分支、remote和本地测试操作。"""
    from manual_access import full_permission_action
    text = full_permission_action(text) or text.strip()
    if (len(text)>8192 or re.search(r'[?？`\n]|如果|假如|引用|转述|不要合|禁止合|暂不|不授权', text)
            or not re.match(r'^(?:(?:请|你|把|将|授权|现在|立即|全部权限|：|:|\s))*合[入并]', text)):
        raise ValueError('EXPLICIT_FINAL_DELIVERY_SOURCE_REQUIRED')
    for key in ('source_branch', 'target_branch', 'remote'):
        if (not isinstance(target.get(key), str) or
                not re.search(r'(?<![A-Za-z0-9/_.-])'+re.escape(target[key])+r'(?![A-Za-z0-9/_.-])', text)):
            raise ValueError('FINAL_DELIVERY_TARGET_NOT_IN_PRIMARY_SOURCE')
    if (not re.search(r'push|推送', text, re.I) or
            not re.search(r'更新本地测试环境|本地栈|local_stack', text)):
        raise ValueError('EXPLICIT_PUSH_AND_LOCAL_STACK_REQUIRED')


def preflight(ledger, target):
    from workflow_completion import facts
    snapshot=ledger.snapshot()
    run_id=target.get('run_id') or snapshot['current_run_id']
    run=next((r for r in snapshot['runs'] if r['run_id']==run_id), None)
    if (not run or run_id!=snapshot['current_run_id'] or snapshot['mode']!='ACTIVE'
            or not snapshot['admission_open'] or run['control']!='ACTIVE'):
        raise ValueError('FINAL_DELIVERY_CURRENT_ACTIVE_RUN_REQUIRED')
    final=facts(snapshot,run_id)
    if final['state']!='COMPLETED':
        raise ValueError('FINAL_DELIVERY_REQUIREMENT_NOT_COMPLETED')
    source_branch=target.get('source_branch');destination=target.get('target_branch')
    remote=target.get('remote')
    for value in (source_branch,destination):
        if not isinstance(value,str) or value.startswith('-'):
            raise ValueError('FINAL_DELIVERY_INVALID_BRANCH')
        git(run['repo'],'check-ref-format','refs/heads/'+value)
    if destination!='main' or source_branch==destination or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,80}',remote or ''):
        raise ValueError('FINAL_DELIVERY_MAIN_AND_EXACT_REMOTE_REQUIRED')
    repo=run['repo'];head=git(repo,'rev-parse','refs/heads/'+source_branch)
    if head!=final['target']['head'] or source_branch!=final['target']['branch']:
        raise ValueError('FINAL_DELIVERY_ACCEPTED_HEAD_CHANGED')
    if git(repo,'status','--porcelain'):
        raise ValueError('FINAL_DELIVERY_DIRTY_REPOSITORY')
    current=git(repo,'symbolic-ref','--short','HEAD')
    if current not in (source_branch,destination):
        raise ValueError('FINAL_DELIVERY_UNEXPECTED_CHECKOUT')
    base=git(repo,'rev-parse','refs/heads/'+destination)
    # fetch和push必须指向同一明确remote；拒绝隐藏的pushurl改道。
    remote_url=git(repo,'remote','get-url',remote)
    if git(repo,'remote','get-url','--push',remote)!=remote_url:
        raise ValueError('FINAL_DELIVERY_DISTINCT_PUSH_URL')
    scripts={}
    for name in SCRIPTS:
        path=Path(repo)/name
        if path.is_symlink() or not path.is_file():
            raise ValueError('FINAL_DELIVERY_LOCAL_PROFILE_UNAVAILABLE')
        raw=path.read_bytes()
        if raw!=subprocess.check_output(['git','-C',repo,'show',head+':'+name],stderr=subprocess.DEVNULL):
            raise ValueError('FINAL_DELIVERY_SCRIPT_DIFFERS_FROM_ACCEPTED_HEAD')
        scripts[name]=hashlib.sha256(raw).hexdigest()
    ancestor=subprocess.run(['git','-C',repo,'merge-base','--is-ancestor',base,head],capture_output=True).returncode==0
    common=git(repo,'merge-base',base,head)
    main_files=git(repo,'diff','--name-only',common,base).splitlines()
    feature_files=git(repo,'diff','--name-only',common,head).splitlines()
    # 只预演Git对象；不checkout、不改index、不连接处理机。
    preview=subprocess.run(['git','-C',repo,'merge-tree','--write-tree',base,head],capture_output=True,text=True,timeout=30)
    if preview.returncode:
        raise ValueError('FINAL_DELIVERY_MERGE_CONFLICT_OR_PREVIEW_FAILED')
    if not ancestor and set(main_files)&set(feature_files):
        raise ValueError('FINAL_DELIVERY_DIVERGED_SCOPE_OVERLAP_REQUIRES_REVIEW')
    return {'run_id':run_id,'repo':repo,'source_branch':source_branch,'target_branch':destination,
        'remote':remote,'remote_url_sha256':hashlib.sha256(remote_url.encode()).hexdigest(),
        'source_head':head,'target_head':base,'completion_event_id':final['completion_event_id'],
        'relation':'FF' if ancestor else 'DIVERGED','main_only_commits':git(repo,'rev-list',head+'..'+base).splitlines(),
        'main_files':main_files,'feature_files':feature_files,'overlap':sorted(set(main_files)&set(feature_files)),
        'merged_tree':preview.stdout.splitlines()[0],'script_sha256':scripts,
        'local_profile':'rosbag_catalog_mock','production_permission':False}


def request(ledger, target, provenance, *, command_id, operator=False):
    from business_ledger import _deny
    if not isinstance(target,dict) or set(target)-{'run_id','source_branch','target_branch','remote'}:
        raise ValueError('EXACT_FINAL_DELIVERY_TARGET_REQUIRED')
    if operator:
        if provenance.get('platform')!='codex' or provenance.get('source_kind')!='direct_user_maintenance':
            raise ValueError('EXPLICIT_OPERATOR_SOURCE_REQUIRED')
        ref=provenance.get('evidence',{})
        if hashlib.sha256(Path(ref['path']).read_bytes()).hexdigest()!=ref.get('sha256'):
            raise ValueError('OPERATOR_AUTHORIZATION_EVIDENCE_CHANGED')
    else:
        if (provenance.get('authenticated') is not True or
                any(not provenance.get(k) for k in ('chat_id','message_id','user_id'))):
            raise ValueError('NATIVE_FINAL_DELIVERY_SOURCE_REQUIRED')
        source(provenance.get('primary_text',''),target)
    # Idempotence depends on the original source/target, not mutable Git HEAD.
    def operation(db):
        frozen=preflight(ledger,target)
        events=[r for r in ledger.snapshot()['events'] if r['run_id']==frozen['run_id'] and r['kind']=='FINAL_DELIVERY_REQUESTED']
        if events:
            _deny('FINAL_DELIVERY_ALREADY_REQUESTED_USE_EXISTING_REQUEST')
        request_id='final-delivery-'+digest([command_id,frozen['run_id']])[:32]
        eid=ledger._event(db,'FINAL_DELIVERY_REQUESTED',{'request_id':request_id,'frozen':frozen,
            'provenance':provenance,'authorization_scope':'exact_final_git_and_local_mock_only'},
            run_id=frozen['run_id'],role='COORDINATOR')
        return {'request_id':request_id,'event_id':eid,'state':'PENDING','frozen':frozen}
    return ledger._command('request_final_delivery',command_id,[target,provenance],operation)


def states(snapshot, run_id):
    output=[]
    for event in snapshot['events']:
        if event['run_id']!=run_id or event['kind']!='FINAL_DELIVERY_REQUESTED':continue
        item={**event['payload'],'request_event_id':event['event_id'],'state':'PENDING','stages':[]}
        for result in snapshot['events']:
            if result['run_id']!=run_id or result['payload'].get('request_id')!=item['request_id']:continue
            if result['kind']=='FINAL_DELIVERY_STAGE_STARTED':item.update(state='RUNNING',stage=result['payload']['stage'])
            if result['kind']=='FINAL_DELIVERY_STAGE_RECORDED':
                item['stages'].append(result['payload']);item.update(state=result['payload']['state'],stage=result['payload']['stage'])
        output.append(item)
    return output


def summary(snapshot, run_id):
    return [{k:s[k] for k in ('request_id','state','stage','stages') if k in s}
            for s in states(snapshot,run_id)]


def next_actions(snapshot, run_id):
    return [{'action':'finalize_delivery','payload':{'request_id':s['request_id']},
        'reason':'最终交付已获精确授权；使用原生固定入口逐步合入main、push和本地mock栈验证。失败/未知保留证据，不重放副作用。'}
        for s in states(snapshot,run_id) if s['state'] in ('PENDING','READY','RUNNING')]


def _record(ledger,identity,request_id,stage,payload,*,started=False):
    from business_ledger import _deny
    def operation(db):
        work,_=ledger._identity(db,identity)
        if work['role']!='COORDINATOR':_deny('COORDINATOR_REQUIRED')
        row=next((s for s in states(ledger.snapshot(),work['run_id']) if s['request_id']==request_id),None)
        if not row:_deny('FINAL_DELIVERY_REQUEST_NOT_IN_WORK')
        eid=ledger._event(db,'FINAL_DELIVERY_STAGE_STARTED' if started else 'FINAL_DELIVERY_STAGE_RECORDED',
            {'request_id':request_id,'stage':stage,**payload},run_id=work['run_id'],role='COORDINATOR',work_id=work['work_id'])
        return {'event_id':eid,**payload}
    return ledger._command('final_delivery_stage',request_id+':'+stage+(':start' if started else ':record'),
        [request_id,stage,payload],operation)


def execute(ledger, identity, payload, *, command_id):
    """同一request跨回合接续；每个副作用只有一个START和固定操作ID。"""
    from business_ledger import _process
    from operation_jobs import execute_plan
    request_id=payload['request_id']
    snapshot=ledger.snapshot()
    with ledger._connection() as db:
        work,_=ledger._identity(db,identity)
        if work['role']!='COORDINATOR':raise ValueError('COORDINATOR_REQUIRED')
    row=next((s for s in states(snapshot,work['run_id']) if s['request_id']==request_id),None)
    if not row:raise ValueError('FINAL_DELIVERY_REQUEST_NOT_IN_WORK')
    frozen=row['frozen'];repo=Path(frozen['repo'])
    directory=ledger.path.parent/'final-deliveries'/request_id
    resource=ledger.path.parent/'merge-locks'/hashlib.sha256(str(repo.resolve()).encode()).hexdigest()
    with locked(resource.with_suffix('.lock')):
        row=next(s for s in states(ledger.snapshot(),work['run_id']) if s['request_id']==request_id)
        if row['state'] in ('COMPLETED','BLOCKED','UNVERIFIED'):
            return {'status':row['state'],'request_id':request_id,'stages':row['stages'],'replay_allowed':False}
        done={s['stage']:s for s in row['stages'] if s['state'] in ('READY','COMPLETED')}
        for stage in STAGES:
            if stage in done:continue
            result_path=directory/(stage+'.json')
            # A started operation may have changed Git/the stack before a crash.
            # Reconcile its persisted terminal result; never launch it again.
            persisted=read_json(result_path,{})
            already_started=row['state']=='RUNNING' and row.get('stage')==stage
            if already_started and persisted.get('status')!='SUCCEEDED':
                return _record(ledger,identity,request_id,stage,{'state':'UNVERIFIED',
                    'code':'FINAL_DELIVERY_INTERRUPTED_RECONCILIATION_REQUIRED','evidence':str(result_path),
                    'replay_allowed':False})
            try:
                from workflow_completion import facts as completion_facts
                current_final=completion_facts(ledger.snapshot(),work['run_id'])
                if (current_final['state']!='COMPLETED' or current_final['completion_event_id']!=frozen['completion_event_id']
                        or current_final['target']['head']!=frozen['source_head']):
                    raise ValueError('FINAL_DELIVERY_ACCEPTANCE_CHANGED')
                expected=done.get('merge',{}).get('head',frozen['target_head'])
                if git(repo,'remote','get-url',frozen['remote'])!=git(repo,'remote','get-url','--push',frozen['remote']):
                    raise ValueError('FINAL_DELIVERY_DISTINCT_PUSH_URL')
                if hashlib.sha256(git(repo,'remote','get-url',frozen['remote']).encode()).hexdigest()!=frozen['remote_url_sha256']:
                    raise ValueError('FINAL_DELIVERY_REMOTE_CHANGED')
                if git(repo,'rev-parse','refs/heads/'+frozen['source_branch'])!=frozen['source_head']:
                    raise ValueError('FINAL_DELIVERY_SOURCE_CHANGED')
                if not (already_started and stage=='merge') and git(repo,'rev-parse','refs/heads/main')!=expected:
                    raise ValueError('FINAL_DELIVERY_TARGET_CHANGED')
                if git(repo,'status','--porcelain'):
                    raise ValueError('FINAL_DELIVERY_DIRTY_REPOSITORY')
                if stage in ('merge','push'):
                    remote_head=git(repo,'ls-remote','--heads',frozen['remote'],'refs/heads/main').split()[0]
                    if remote_head not in (frozen['target_head'],expected):
                        raise ValueError('FINAL_DELIVERY_REMOTE_MAIN_CHANGED')
                if stage in ('local_up','local_test','quality_gate'):
                    if git(repo,'symbolic-ref','--short','HEAD')!='main':raise ValueError('FINAL_DELIVERY_CHECKOUT_CHANGED')
                    for name,sha in frozen['script_sha256'].items():
                        if hashlib.sha256((repo/name).read_bytes()).hexdigest()!=sha:
                            raise ValueError('FINAL_DELIVERY_LOCAL_SCRIPT_CHANGED')
                argv={
                    'fetch':[['git','fetch','--no-tags',frozen['remote']]],
                    'merge':[['git','checkout','main'],['git','merge','--no-edit',frozen['source_head']]],
                    'push':[['git','push',frozen['remote'],expected+':refs/heads/main']],
                    'local_up':[['./scripts/local_stack.sh','--mode','mock','up']],
                    'local_test':[['./scripts/local_stack.sh','--mode','mock','test']],
                    'quality_gate':[['./scripts/local_quality_gate.sh']]}
                if stage=='push' and git(repo,'symbolic-ref','--short','HEAD')!='main':
                    raise ValueError('FINAL_DELIVERY_CHECKOUT_CHANGED')
                if not already_started:
                    started=_record(ledger,identity,request_id,stage,{'state':'RUNNING'},started=True)
                    if started['status'] not in ('COMMITTED','DUPLICATE'):return started
                    def process_hook(process):
                        actual=_process(process.pid)
                        if actual is None:raise ValueError('FINAL_DELIVERY_PROCESS_UNVERIFIED')
                        result=ledger.register_tool_execution(identity,{'tool_id':request_id+'-'+stage+'-'+str(process.pid),
                            'pid':process.pid,'process_start':actual['process_start'],'cgroup':actual['cgroup']},
                            command_id=request_id+':tool:'+str(process.pid))
                        if result['status']!='COMMITTED':raise ValueError('FINAL_DELIVERY_PROCESS_NOT_REGISTERED')
                    # Local-only profile ignores inherited routing overrides. No
                    # SSH/NAS/A100/production command is part of this operation.
                    env={'GIT_TERMINAL_PROMPT':'0','PYTHONDONTWRITEBYTECODE':'1',
                         'LOCAL_NAS_PATH':str(repo/'.local/nas'),'LOCAL_TEST_MODE':'mock',
                         'DOCKER_HOST':'unix:///var/run/docker.sock','DOCKER_CONTEXT':'default'}
                    persisted=execute_plan({'operation_id':request_id+'-'+stage,'role':'COORDINATOR','cwd':str(repo),
                        'steps':[{'argv':['env',*[v for k in os.environ if k.startswith(('GIT_','LOCAL_','DOCKER_')) or k in ('COMPOSE_FILE','COMPOSE_PROJECT_NAME','PYTHON_BIN','RUFF_BIN','DATABASE_URL','TEST_MYSQL_URL','MYSQL_TEST_URL','MN_TEST_MYSQL_URL') for v in ('-u',k)],*[k+'='+v for k,v in env.items()],*a]} for a in argv[stage]],'timeout_seconds':3600 if stage in ('local_up','local_test','quality_gate') else 120},result_path,process_hook=process_hook)
                head=git(repo,'rev-parse','refs/heads/main')
                if persisted['status']!='SUCCEEDED' or persisted.get('exit_code')!=0:
                    raise ValueError('FINAL_DELIVERY_'+stage.upper()+'_FAILED')
                if stage=='merge' and (git(repo,'rev-parse','HEAD^{tree}')!=frozen['merged_tree'] or git(repo,'status','--porcelain')):
                    raise ValueError('FINAL_DELIVERY_MERGE_POSTCHECK_FAILED')
                if stage=='push' and git(repo,'ls-remote','--heads',frozen['remote'],'refs/heads/main').split()[0]!=head:
                    raise ValueError('FINAL_DELIVERY_PUSH_POSTCHECK_FAILED')
                evidence={'path':str(result_path),'sha256':hashlib.sha256(result_path.read_bytes()).hexdigest()}
                result=_record(ledger,identity,request_id,stage,{'state':'COMPLETED' if stage==STAGES[-1] else 'READY',
                    'head':head,'exit_code':persisted['exit_code'],'evidence':evidence,'production_permission':False})
                if result['status'] not in ('COMMITTED','DUPLICATE'):return result
                done[stage]=result
                row['state']='READY'
            except (ValueError,OSError,subprocess.SubprocessError,IndexError) as exc:
                code=str(exc) if isinstance(exc,ValueError) else 'FINAL_DELIVERY_'+type(exc).__name__.upper()
                return _record(ledger,identity,request_id,stage,{'state':'BLOCKED','code':code,
                    'head':git(repo,'rev-parse','refs/heads/main'),'evidence':str(result_path),
                    'replay_allowed':False,'next':'保留已完成步骤与原日志，核对真实阻塞；不能冒充交付成功或用新ID重复副作用。'})
        return {'status':'COMPLETED','request_id':request_id,'head':git(repo,'rev-parse','refs/heads/main'),
            'push_verified':True,'local_validation':'PASS','production_permission':False}
