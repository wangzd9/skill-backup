"""Authorized fast-forward integration through the existing operation runner.

No reset, force push, conflict resolution or automatic completion is provided.
The destination branch/base and any post-merge commands come from a trusted
native authorization, never model-supplied shell text.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import uuid

from business_ledger import _process
from runtime_common import locked, read_json, save_json


def _git(repo,*args):
    return subprocess.check_output(['git','-C',str(repo),*args],stderr=subprocess.DEVNULL,text=True,timeout=20).strip()


def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def merge_candidate(ledger, identity, candidate_id, authorization_id, *, command_id):
    """Integrate one approved exact candidate; completion remains a model decision."""
    from operation_jobs import execute_plan
    from delivery_evidence import parse_test_results
    readiness=ledger.merge_readiness(identity,candidate_id,authorization_id)
    if not readiness['ready']:
        return readiness
    candidate=readiness['candidate']; grant=readiness['authorization']['spec']; authorized_repo=Path(grant['repo']).resolve()
    key=hashlib.sha256(command_id.encode()).hexdigest()
    directory=ledger.path.parent/'merges'/key
    resource=ledger.path.parent/'merge-locks'/hashlib.sha256(str(authorized_repo).encode()).hexdigest()
    frozen={'candidate_id':candidate_id,'authorization_id':authorization_id,
            'candidate_head':candidate['head'],'source_repo':candidate['repo'],'destination_repo':str(authorized_repo),
            'base':candidate['base'],'branch':grant['branch'],'grant_sha256':hashlib.sha256(json.dumps(grant,sort_keys=True).encode()).hexdigest()}
    with locked(resource.with_suffix('.lock')):
        intent=directory/'intent.json'; result_path=directory/'result.json'
        if intent.exists():
            previous=read_json(intent)
            if {k:v for k,v in previous['frozen'].items() if k!='integration_worktree'}!=frozen:
                return {'status':'REJECTED','code':'MERGE_COMMAND_ID_CONFLICT'}
            if previous.get('result'):
                return {**previous['result'],'status':'DUPLICATE','original_status':previous['result']['status']}
            # Git may already have changed. Reconcile actual evidence; never run it twice.
            return {'status':'UNVERIFIED','code':'MERGE_SIDE_EFFECT_REQUIRES_RECONCILIATION','replay_allowed':False,
                    'state_path':str(result_path),'actual_head':_git(authorized_repo,'rev-parse','refs/heads/'+grant['branch'])}
        readiness=ledger.merge_readiness(identity,candidate_id,authorization_id)
        if not readiness['ready']:
            return readiness
        from workflow_integration_workspace import select
        try:
            repo, workspace_steps, isolated = select(ledger,grant,candidate['base'])
        except ValueError as exc:
            return {'status':'REJECTED','code':str(exc)}
        if isolated:
            frozen['integration_worktree']=str(repo)
        if not workspace_steps and (_git(repo,'status','--porcelain') or _git(repo,'rev-parse','HEAD')!=candidate['base']):
            return {'status':'REJECTED','code':'MERGE_BASE_CHANGED_OR_DIRTY'}
        branch_steps=workspace_steps
        if not workspace_steps and _git(repo,'symbolic-ref','--short','HEAD')!=grant['branch']:
            ref='refs/heads/'+grant['branch']
            if subprocess.run(['git','-C',str(repo),'check-ref-format','--branch',grant['branch']],capture_output=True).returncode:
                return {'status':'REJECTED','code':'MERGE_TARGET_BRANCH_INVALID'}
            existing=subprocess.run(['git','-C',str(repo),'rev-parse','--verify',ref],capture_output=True,text=True)
            if existing.returncode==0:
                if existing.stdout.strip()!=candidate['base']:
                    return {'status':'REJECTED','code':'MERGE_TARGET_BRANCH_CHANGED'}
                argv=['git','-C',str(repo),'-c','core.hooksPath=/dev/null','checkout',grant['branch']]
            elif existing.returncode==128:
                argv=['git','-C',str(repo),'-c','core.hooksPath=/dev/null','checkout','-b',grant['branch'],candidate['base']]
            else:
                return {'status':'REJECTED','code':'MERGE_TARGET_BRANCH_UNVERIFIED'}
            branch_steps=[{'argv':argv}]
        # Both object identities and source cleanliness are checked immediately
        # before the fixed runner; a new source head cannot inherit old approval.
        if (_git(candidate['repo'],'rev-parse','HEAD')!=candidate['head']
            or _git(candidate['repo'],'status','--porcelain')):
            return {'status':'REJECTED','code':'MERGE_CANDIDATE_WORKTREE_CHANGED'}
        steps=branch_steps+[{'argv':['git','-C',str(repo),'-c','protocol.file.allow=always','fetch','--no-tags',
                        '--no-write-fetch-head',candidate['repo'],candidate['head']]},
               {'argv':['git','-C',str(repo),'-c','core.hooksPath=/dev/null','merge','--ff-only','--no-edit',candidate['head']]}]
        junit=directory/'postmerge.xml'
        if grant.get('postmerge_argv'):
            argv=list(grant['postmerge_argv'])
            if grant.get('junit_format','pytest')=='pytest':
                argv+=['-p','no:cacheprovider','--junitxml='+str(junit)]
            elif grant.get('junit_format')=='junit':
                if not any('{junit_xml}' in arg for arg in argv):
                    return {'status':'REJECTED','code':'POSTMERGE_JUNIT_OUTPUT_REQUIRED'}
                argv=[arg.replace('{junit_xml}',str(junit)) for arg in argv]
            else:
                return {'status':'REJECTED','code':'INVALID_POSTMERGE_RESULT_FORMAT'}
            steps.append({'argv':argv})
        plan={'operation_id':'merge-'+key[:40],'task':candidate['task_id'],'attempt':candidate['attempt'],
              'role':'COORDINATOR','cwd':str(repo),'steps':steps,
              'timeout_seconds':grant.get('timeout_seconds',300),
              'environment':{'PYTHONDONTWRITEBYTECODE':'1','GIT_TERMINAL_PROMPT':'0'}}
        directory.mkdir(parents=True,exist_ok=True)
        if isolated:
            repo.mkdir(parents=True,exist_ok=True)
        save_json(intent,{'frozen':frozen,'plan':plan,'state':'STARTING'})
        def process_hook(process):
            state=_process(process.pid)
            if state is None:
                raise ValueError('MERGE_TOOL_PROCESS_UNVERIFIED')
            registered=ledger.register_tool_execution(identity,{
                'tool_id':plan['operation_id']+'-'+str(process.pid),'pid':process.pid,
                'process_start':state['process_start'],'cgroup':state['cgroup']},
                command_id=plan['operation_id']+':tool:'+str(process.pid))
            if registered['status']!='COMMITTED':
                raise ValueError(registered.get('code','MERGE_TOOL_PROCESS_NOT_REGISTERED'))
        executed=execute_plan(plan,result_path,process_hook=process_hook)
        actual_head=_git(repo,'rev-parse','HEAD')
        evidence={'schema_version':1,'frozen':frozen,'operation':{'path':str(result_path),'sha256':_hash(result_path)},
                  'log':{'path':str(result_path.with_suffix('.log')),'sha256':_hash(result_path.with_suffix('.log'))},
                  'actual_head':actual_head,'execution_status':executed['status'],'exit_code':executed['exit_code'],
                  'postmerge_required':bool(grant.get('postmerge_argv')),'postmerge':None,'business_complete':False}
        if grant.get('postmerge_argv'):
            evidence['postmerge']=parse_test_results(junit,execution_status=executed['status'],exit_code=executed['exit_code'])
        evidence_path=directory/'merge-evidence.json'; save_json(evidence_path,evidence)
        if (executed['status']!='SUCCEEDED' or executed['exit_code']!=0 or actual_head!=candidate['head']
            or _git(repo,'status','--porcelain') or _git(repo,'symbolic-ref','--short','HEAD')!=grant['branch']
            or (evidence['postmerge_required'] and evidence['postmerge']['mechanical_result']!='PASS')):
            result={'status':'UNVERIFIED','code':'MERGE_OR_POSTMERGE_CHECK_NOT_CLEAR','actual_head':actual_head,
                    'evidence':str(evidence_path),'replay_allowed':False,'business_complete':False}
        else:
            result=ledger.record_merge(identity,candidate_id,authorization_id,actual_head,
                {'path':str(evidence_path),'sha256':_hash(evidence_path)},command_id=command_id+':record')
            result.update(evidence=str(evidence_path),business_complete=False)
        save_json(intent,{'frozen':frozen,'plan':plan,'state':'FINISHED','result':result})
        return result
