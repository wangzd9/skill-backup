"""Audit unchanged failed prerequisites without inventing a business verdict."""
import hashlib
import json
from pathlib import Path


def basis(ledger, candidate, argv, profile, report_format, evidence, environment):
    import shutil
    from delivery_evidence import _file_hash
    files=[]
    for kind in ('inputs','config'):
        for name in evidence.get(kind,[]):
            path=(Path(candidate['repo'])/name).resolve()
            if profile in ('mysql_migration','integration_mysql') and path.name=='ready.json' and path.is_relative_to(ledger.path.parent/'mysql-sessions'):
                # A fresh disposable instance is not a fixture repair. Preserve
                # the actual image/version and isolation contract, not its ID.
                value=json.loads(path.read_bytes())
                files.append({'kind':kind,'mysql_profile':{k:value.get(k) for k in ('image','version','network','database')}})
            else:files.append({'kind':kind,'path':str(path),'sha256':_file_hash(path)})
    program=argv[0] if Path(argv[0]).is_absolute() else shutil.which(argv[0])
    paths=[*environment.get('dependencies',[]),program,str(Path(__file__).with_name('workflow_test_environment.py'))]
    dependencies=[{'path':p,'sha256':_file_hash(p)} for p in dict.fromkeys(paths) if p and Path(p).is_file()]
    value={'candidate':{k:candidate.get(k) for k in ('run_id','task_id','attempt','candidate_id','head','base','ac_version')},
        'argv':argv,'profile':profile,'report_format':report_format,'files':files,
        'environment':{k:v for k,v in environment.items() if k not in ('argv_sha256','dependencies')},
        'dependencies':dependencies,'declared_environment_hash':hashlib.sha256(json.dumps(
            {k:__import__('os').environ.get(k) for k in evidence.get('environment',[])},sort_keys=True).encode()).hexdigest()}
    return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()


def claim(ledger, key, folder):
    from runtime_common import locked,read_json,save_json
    root=ledger.path.parent/'verification-retry';root.mkdir(mode=0o700,parents=True,exist_ok=True)
    with locked(root/(key+'.lock')):
        path=root/(key+'.json');old=read_json(path,{}) or {}
        if old and old['operation_directory']!=str(folder) and old.get('state') in ('STARTED','NONPASS'):
            ref=old.get('source');valid=False
            if ref:
                try:valid=hashlib.sha256(Path(ref['path']).read_bytes()).hexdigest()==ref['sha256']
                except OSError:pass
            return {'status':'REJECTED' if valid else 'UNVERIFIED','code':'UNCHANGED_VERIFICATION_PREREQUISITES',
                'phase':'RETRY_PREFLIGHT','test_started':False,'publication':None,'business_pass':False,
                'responsible_role':'COORDINATOR','prior_operation':old['operation_directory'],
                'prior_evidence':ref,'prior_evidence_verified':valid,'failure_signature':old.get('failure_signature'),
                'next_action':'核原结果并落实候选/真实环境/声明输入变更后再验证；未知执行先核退。换command_id、重建同版隔离库不是修复。保留稳定性复跑。'}
        save_json(path,{'state':'STARTED','operation_directory':str(folder),'basis':key})
    return None


def finish(ledger, key, folder, result):
    from runtime_common import locked,save_json
    root=ledger.path.parent/'verification-retry';path=root/(key+'.json')
    source=folder/'verification.json';stats=result.get('test_stats') or {}
    signature=hashlib.sha256(json.dumps([stats.get('case_fingerprint'),stats.get('reasons'),
        (stats.get('stability_evidence') or {}).get('sha256')],sort_keys=True).encode()).hexdigest()
    with locked(root/(key+'.lock')):
        save_json(path,{'state':'PASS' if stats.get('mechanical_result')=='PASS' and
            (result.get('validation') or {}).get('valid') else 'NONPASS','basis':key,
            'operation_directory':str(folder),'failure_signature':signature,
            'source':{'path':str(source),'sha256':hashlib.sha256(source.read_bytes()).hexdigest()},
            'business_result_unchanged':True})


def blocked(ledger, identity, candidate, key, refusal, folder):
    """Make the refused continuation visible to the existing coordinator work."""
    def operation(db):
        work,_=ledger._identity(db,identity)
        event=ledger._event(db,'VERIFICATION_RETRY_BLOCKED',{
            'candidate_id':candidate.get('candidate_id'),'head':candidate['head'],
            'task_id':work['task_id'],'attempt':work['attempt'],'work_kind':work['kind'],
            'code':refusal['code'],'prior_evidence':refusal.get('prior_evidence'),
            'prior_evidence_verified':refusal.get('prior_evidence_verified'),
            'owner':'COORDINATOR','business_result_unchanged':True,
            'next':'核原证据；落实候选/环境/输入变更后由原TESTER验证，或按当前任务生命周期返修/安全退休。拒绝本身不判代码FAIL、不完成work。'},
            run_id=work['run_id'],work_id=work['work_id'],role='COORDINATOR')
        return {'event_id':event,'owner':'COORDINATOR','work_ended':False}
    return ledger._command('record_verification_retry_block','verification-retry-block:'+hashlib.sha256(
        (key+str(folder)).encode()).hexdigest(),[identity,key,str(folder)],operation)
