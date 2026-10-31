"""最终交付的显式版本恢复：保留旧回执，只重验本地步骤，不重放 Git 写操作。"""
import hashlib
import os
from pathlib import Path
import re
import subprocess

from runtime_common import read_json, save_json
from workflow_final_delivery import SCRIPTS, digest, git

STAGES = ('reconcile', 'local_up', 'local_test', 'quality_gate')


def executor_digest():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def project(item, events, run_id):
    for event in events:
        p = event['payload']
        if event['run_id'] != run_id or p.get('request_id') != item['request_id']:
            continue
        if event['kind'] == 'FINAL_DELIVERY_RECOVERY_REQUESTED':
            if item.get('recovery'):
                item.setdefault('recovery_history',[]).append({'recovery':item['recovery'],
                    'state':item['state'],'stage':item['stage'],'stages':item.get('recovery_stages',[])})
            item.update(recovery=p['recovery'], recovery_stages=[], state='PENDING', stage='reconcile')
        if event['kind'] in ('FINAL_DELIVERY_RECOVERY_STARTED', 'FINAL_DELIVERY_RECOVERY_RECORDED'):
            if p.get('recovery_id') != (item.get('recovery') or {}).get('recovery_id'):
                continue
            item.update(state=p['state'], stage=p['stage'])
            if event['kind'] == 'FINAL_DELIVERY_RECOVERY_RECORDED':
                item['recovery_stages'].append({'event_id':event['event_id'],**p})


def _check(ledger, row, recovery):
    from workflow_completion import facts
    frozen = row['frozen']; repo = Path(frozen['repo'])
    if recovery.get('executor_sha256') and recovery['executor_sha256'] != executor_digest():
        raise ValueError('FINAL_DELIVERY_EXECUTOR_CHANGED')
    s = ledger.snapshot(); run = next(r for r in s['runs'] if r['run_id'] == frozen['run_id'])
    if (s['mode'] != 'ACTIVE' or not s['admission_open'] or s['current_run_id'] != run['run_id']
            or run['control'] != 'ACTIVE'):
        raise ValueError('FINAL_DELIVERY_CURRENT_ACTIVE_RUN_REQUIRED')
    final = facts(s, run['run_id'])
    if (final['state'] != 'COMPLETED' or final['completion_event_id'] != frozen['completion_event_id']
            or final['target']['head'] != frozen['source_head']):
        raise ValueError('FINAL_DELIVERY_ACCEPTANCE_CHANGED')
    if git(repo, 'rev-parse', 'refs/heads/'+frozen['source_branch']) != frozen['source_head']:
        raise ValueError('FINAL_DELIVERY_SOURCE_CHANGED')
    head = recovery['validation_head']
    if (git(repo, 'rev-parse', 'refs/heads/main') != head
            or git(repo, 'symbolic-ref', '--short', 'HEAD') != 'main'):
        raise ValueError('FINAL_DELIVERY_TARGET_CHANGED')
    if git(repo, 'status', '--porcelain'):
        raise ValueError('FINAL_DELIVERY_DIRTY_REPOSITORY')
    for ancestor in (frozen['source_head'], recovery['previous_head']):
        if subprocess.run(['git','-C',str(repo),'merge-base','--is-ancestor',ancestor,head],
                capture_output=True, timeout=30).returncode != 0:
            raise ValueError('FINAL_DELIVERY_RECOVERY_NOT_DESCENDANT')
    remote = frozen['remote']; url = git(repo, 'remote', 'get-url', remote)
    if (git(repo, 'remote', 'get-url', '--push', remote) != url
            or hashlib.sha256(url.encode()).hexdigest() != frozen['remote_url_sha256']):
        raise ValueError('FINAL_DELIVERY_REMOTE_CHANGED')
    lines = git(repo, 'ls-remote', '--heads', remote, 'refs/heads/main').split()
    if not lines or lines[0] != head:
        raise ValueError('FINAL_DELIVERY_RECOVERY_REMOTE_NOT_ALIGNED')
    for name, sha in recovery['script_sha256'].items():
        path = repo/name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != sha:
            raise ValueError('FINAL_DELIVERY_LOCAL_SCRIPT_CHANGED')
    return head


def request_recovery(ledger, target, provenance, *, command_id, operator=False):
    from workflow_final_delivery import states
    if (set(target) != {'request_id','expected_head','reason'}
            or not isinstance(target.get('request_id'),str) or not target['request_id']
            or not isinstance(target.get('expected_head'),str)
            or not re.fullmatch('[0-9a-f]{40}', target['expected_head'])
            or not isinstance(target.get('reason'),str) or not target['reason'].strip()
            or len(target['reason']) > 2000):
        raise ValueError('EXACT_FINAL_DELIVERY_RECOVERY_REQUIRED')
    if not operator:
        text = provenance.get('primary_text','').strip()
        if (not re.match(r'^(?:请|现在|立即|全部权限|：|:|\s)*恢复最终交付',text)
                or any(x not in text for x in (target['request_id'],target['expected_head'],'main'))
                or not re.search('本地栈|本地测试环境|local_stack',text)
                or re.search(r'[?？`\n]|如果|假如|引用|转述|禁止|不授权|暂不',text)):
            raise ValueError('EXPLICIT_FINAL_DELIVERY_RECOVERY_SOURCE_REQUIRED')
    def operation(db):
        snapshot = ledger.snapshot(); run_id = snapshot['current_run_id']
        row = next((r for r in states(snapshot,run_id) if r['request_id'] == target['request_id']),None)
        if not row or row['state'] != 'BLOCKED' or row.get('stage') not in STAGES:
            raise ValueError('FINAL_DELIVERY_TERMINAL_LOCAL_FAILURE_REQUIRED')
        if not any(r['stage'] == 'merge' and r['state'] == 'READY' for r in row['stages']):
            raise ValueError('FINAL_DELIVERY_SUCCESSFUL_MERGE_REQUIRED')
        previous = next(r['head'] for r in row['stages'] if r['stage'] == 'merge' and r['state'] == 'READY')
        previous = (row.get('recovery') or {}).get('validation_head',previous)
        executor_sha256 = executor_digest()
        recovery_id = 'recovery-'+digest([row['request_id'],target['expected_head'],executor_sha256])[:24]
        if any(e['kind'] == 'FINAL_DELIVERY_RECOVERY_REQUESTED' and
                e['payload'].get('recovery',{}).get('recovery_id') == recovery_id for e in snapshot['events']):
            raise ValueError('FINAL_DELIVERY_RECOVERY_SAME_HEAD_ALREADY_REQUESTED')
        old = (row.get('recovery_stages') or row['stages'])[-1]
        ref = old.get('evidence')
        previous_failure = {'event_id':old.get('event_id'),'payload_sha256':digest(old)}
        path = Path(ref['path'] if isinstance(ref,dict) else ref) if ref else None
        raw = path.read_bytes() if path else b''; result = read_json(path,{}) if path else {}
        expected_id = (row['request_id']+'-'+old['stage'] if not row.get('recovery')
            else row['recovery']['recovery_id']+'-'+old['stage'])
        if old.get('phase') == 'NOT_STARTED':
            if path or any(e['kind'] == 'FINAL_DELIVERY_RECOVERY_STARTED' and
                    e['payload'].get('recovery_id') == row['recovery']['recovery_id'] and
                    e['payload'].get('stage') == old['stage'] for e in snapshot['events']):
                raise ValueError('FINAL_DELIVERY_TERMINAL_FAILURE_RECEIPT_REQUIRED')
        elif (result.get('status') != 'FAILED' or result.get('operation_id') != expected_id
                or result.get('exit_code') in (None,0)
                or isinstance(ref,dict) and hashlib.sha256(raw).hexdigest() != ref.get('sha256')):
            raise ValueError('FINAL_DELIVERY_TERMINAL_FAILURE_RECEIPT_REQUIRED')
        if path:
            previous_failure = {'path':str(path),'sha256':hashlib.sha256(raw).hexdigest()}
        repo = Path(row['frozen']['repo']); scripts = {}
        if git(repo,'rev-parse','refs/heads/main') != target['expected_head']:
            raise ValueError('FINAL_DELIVERY_TARGET_CHANGED')
        for name in SCRIPTS:
            p = repo/name
            if p.is_symlink() or p.read_bytes() != subprocess.check_output(
                    ['git','-C',str(repo),'show',target['expected_head']+':'+name],stderr=subprocess.DEVNULL):
                raise ValueError('FINAL_DELIVERY_RECOVERY_COMMITTED_SCRIPT_REQUIRED')
            scripts[name] = hashlib.sha256(p.read_bytes()).hexdigest()
        recovery = {'recovery_id':recovery_id,'validation_head':target['expected_head'],
            'previous_head':previous,'script_sha256':scripts,'reason':target['reason'],
            'executor_sha256':executor_sha256,
            'previous_failure':previous_failure,
            'provenance':provenance,'production_permission':False}
        _check(ledger,row,recovery)
        eid = ledger._event(db,'FINAL_DELIVERY_RECOVERY_REQUESTED',
            {'request_id':row['request_id'],'recovery':recovery},run_id=run_id,role='COORDINATOR')
        return {'request_id':row['request_id'],'recovery_id':recovery_id,'event_id':eid,
            'state':'PENDING','validation_head':target['expected_head']}
    return ledger._command('request_final_delivery_recovery',command_id,[target,provenance],operation)


def _record(ledger, identity, row, stage, payload, *, started=False):
    from workflow_final_delivery import states
    rid = row['recovery']['recovery_id']
    def operation(db):
        work, _ = ledger._identity(db,identity)
        current = next((s for s in states(ledger.snapshot(),work['run_id'])
            if s['request_id'] == row['request_id']),None)
        if (work['role'] != 'COORDINATOR' or not current
                or (current.get('recovery') or {}).get('recovery_id') != rid):
            raise ValueError('FINAL_DELIVERY_RECOVERY_NOT_IN_WORK')
        eid = ledger._event(db,'FINAL_DELIVERY_RECOVERY_STARTED' if started else 'FINAL_DELIVERY_RECOVERY_RECORDED',
            {'request_id':row['request_id'],'recovery_id':rid,'stage':stage,**payload},
            run_id=work['run_id'],role='COORDINATOR',work_id=work['work_id'])
        return {'event_id':eid,**payload}
    return ledger._command('final_delivery_recovery_stage',rid+':'+stage+(':start' if started else ':record'),
        [row['request_id'],stage,payload],operation)


def execute_recovery(ledger, identity, row, directory):
    from business_ledger import _process
    from operation_jobs import execute_plan
    recovery = row['recovery']; rid = recovery['recovery_id']; repo = Path(row['frozen']['repo'])
    if row['state'] in ('BLOCKED','UNVERIFIED','COMPLETED'):
        return {'status':row['state'],'request_id':row['request_id'],'replay_allowed':False}
    done = {r['stage'] for r in row['recovery_stages'] if r['state'] in ('READY','COMPLETED')}
    for stage in STAGES:
        if stage in done:
            continue
        path = directory/'recoveries'/rid/(stage+'.json'); persisted = read_json(path,{})
        started = row['state'] == 'RUNNING' and row['stage'] == stage
        launched = started
        if started and persisted.get('status') != 'SUCCEEDED' and stage != 'reconcile':
            return _record(ledger,identity,row,stage,{'state':'UNVERIFIED',
                'code':'FINAL_DELIVERY_INTERRUPTED_RECONCILIATION_REQUIRED','replay_allowed':False})
        try:
            head = _check(ledger,row,recovery)
            if not started:
                result = _record(ledger,identity,row,stage,{'state':'RUNNING'},started=True)
                if result['status'] not in ('COMMITTED','DUPLICATE'):
                    return result
                launched = True
            if stage == 'reconcile':
                if not persisted:
                    persisted = {'status':'SUCCEEDED','exit_code':0,'head':head,
                        'remote_head':head,'git_write_performed':False}
                    save_json(path,persisted)
            elif not started:
                def hook(process):
                    actual = _process(process.pid)
                    if actual is None:
                        raise ValueError('FINAL_DELIVERY_PROCESS_UNVERIFIED')
                    result = ledger.register_tool_execution(identity,{'tool_id':rid+'-'+stage+'-'+str(process.pid),
                        'pid':process.pid,'process_start':actual['process_start'],'cgroup':actual['cgroup']},
                        command_id=rid+':tool:'+str(process.pid))
                    if result['status'] != 'COMMITTED':
                        raise ValueError('FINAL_DELIVERY_PROCESS_NOT_REGISTERED')
                env = {'GIT_TERMINAL_PROMPT':'0','PYTHONDONTWRITEBYTECODE':'1',
                    'LOCAL_NAS_PATH':str(repo/'.local/nas'),'LOCAL_TEST_MODE':'mock',
                    'DOCKER_HOST':'unix:///var/run/docker.sock','DOCKER_CONTEXT':'default'}
                unset = [v for k in os.environ if k.startswith(('GIT_','LOCAL_','DOCKER_','PYTHON','PYTEST_')) or k in
                    ('COMPOSE_FILE','COMPOSE_PROJECT_NAME','PYTHON_BIN','RUFF_BIN','DATABASE_URL',
                     'TEST_MYSQL_URL','MYSQL_TEST_URL','MN_TEST_MYSQL_URL') for v in ('-u',k)]
                argv = {'local_up':['./scripts/local_stack.sh','--mode','mock','up'],
                    'local_test':['./scripts/local_stack.sh','--mode','mock','test'],
                    'quality_gate':['./scripts/local_quality_gate.sh']}[stage]
                persisted = execute_plan({'operation_id':rid+'-'+stage,'role':'COORDINATOR','cwd':str(repo),
                    'steps':[{'argv':['env',*unset,*[k+'='+v for k,v in env.items()],*argv]}],
                    'timeout_seconds':3600},path,process_hook=hook)
            if persisted.get('status') != 'SUCCEEDED' or persisted.get('exit_code') != 0:
                raise ValueError('FINAL_DELIVERY_'+stage.upper()+'_FAILED')
            _check(ledger,row,recovery)
            result = _record(ledger,identity,row,stage,{'state':'COMPLETED' if stage == STAGES[-1] else 'READY',
                'head':head,'exit_code':0,'evidence':{'path':str(path),
                    'sha256':hashlib.sha256(path.read_bytes()).hexdigest()},'production_permission':False})
            if result['status'] not in ('COMMITTED','DUPLICATE'):
                return result
            row['state'] = 'READY'
        except (ValueError,OSError,subprocess.SubprocessError) as exc:
            return _record(ledger,identity,row,stage,{'state':'BLOCKED',
                'code':str(exc) if isinstance(exc,ValueError) else 'FINAL_DELIVERY_'+type(exc).__name__.upper(),
                'head':recovery['validation_head'],'exit_code':persisted.get('exit_code'),
                'phase':'STARTED' if launched else 'NOT_STARTED',
                'evidence':{'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()} if path.is_file() else None,
                'replay_allowed':False})
    return {'status':'COMPLETED','request_id':row['request_id'],'head':recovery['validation_head'],
        'push_verified':True,'local_validation':'PASS','production_permission':False}
