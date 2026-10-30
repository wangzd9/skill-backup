"""Frozen candidate adapters to the existing OCR and strong-review services.

This module launches neither a new worker nor a polling loop. It invokes the
existing sole service entries, collects their immutable evidence, and leaves
findings to an explicit coordinator adjudication. Unknown dispatches never retry.
"""
import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import time

from runtime_common import locked, read_json, save_json


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class PreDispatchRejected(ValueError):
    """A refusal raised before any native dispatch, so no side effect exists."""


def _canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))


def _directory(ledger, action_id):
    return ledger.path.parent/'services'/hashlib.sha256(action_id.encode()).hexdigest()


def _sources(paths):
    return [{'path':str(Path(path).resolve()),'sha256':_digest(path)} for path in paths]


def _action(ledger, action_id):
    rows=[row for row in ledger.snapshot()['external_actions'] if row['action_id']==action_id]
    if len(rows)!=1:
        raise ValueError('UNKNOWN_EXTERNAL_ACTION')
    return rows[0]


def _background_bytes(request):
    text=request.get('background_text')
    if (not isinstance(text,str) or not text.strip() or len(text)>65536
            or len(text.encode('utf-8'))>262144 or 'background_file' in request):
        raise ValueError('OCR_BACKGROUND_TEXT_INVALID_OR_AMBIGUOUS')
    return text.encode('utf-8')


def _freeze_background(request, directory):
    """Service-owned, bounded input; no model-selected output path or shell."""
    data=_background_bytes(request)
    path=directory/'background.md'
    try:
        descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o400)
    except FileExistsError:
        if path.is_symlink() or not path.is_file() or path.read_bytes()!=data:
            raise ValueError('OCR_FROZEN_BACKGROUND_CHANGED')
    else:
        with os.fdopen(descriptor,'wb') as handle:
            handle.write(data);handle.flush();os.fsync(handle.fileno())
    return path


def _request_inputs(kind, request, run):
    """Resolve declared inputs before any ledger effect.

    Raises :class:`PreDispatchRejected`, which callers treat as a provably
    side-effect-free refusal: an UNKNOWN row left by a pure argument error
    would hold the serial OCR gate closed forever.
    """
    if kind!='ocr':
        return {}
    allowed={'background_file','background_text','batches_file','reuse_action_id','retry_reason'}
    if set(request)-allowed or not (request.get('background_file') or request.get('background_text')):
        raise PreDispatchRejected('OCR_REQUIRES_FROZEN_BACKGROUND_FILE')
    requirement=Path(run['requirement_root']).resolve()
    files={}
    for key in ('background_file','batches_file'):
        if request.get(key):
            path=Path(request[key]).resolve()
            if not path.is_relative_to(requirement) or not path.is_file():
                raise PreDispatchRejected('OCR_INPUT_MUST_BE_REGULAR_FILE_IN_FROZEN_REQUIREMENT')
            files[key]=str(path)
    return files


class NativeServices:
    """Thin argv adapters to the installed service entries, never another runner."""
    def __init__(self):
        self.scripts=Path(__file__).resolve().parent

    def _cli(self, script, arguments, *, evidence_read=False):
        run=subprocess.run([sys.executable,str(self.scripts/script),*map(str,arguments)],
                           capture_output=True,text=True,timeout=1500 if script=='ocr_runtime.py' and str(arguments[0]) in ('preflight','prepare') else 120)
        try:
            value=json.loads(run.stdout)
        except ValueError as exc:
            raise RuntimeError('NATIVE_SERVICE_RECEIPT_UNVERIFIED') from exc
        # This versioned receipt is emitted only by prepare or the start guard,
        # before a durable dispatch intent. Arbitrary errors remain uncertain.
        refusal=value.get('preflight_rejection') if isinstance(value,dict) else None
        if (script=='ocr_runtime.py' and str(arguments[0]) in ('preflight','prepare','start')
                and run.returncode==78 and isinstance(refusal,dict)
                and refusal.get('schema')=='ocr.preflight-rejection/v1'
                and refusal.get('stage')=='preflight'
                and refusal.get('operation')==str(arguments[0])
                and type(refusal.get('side_effects')) is int and refusal['side_effects']==0
                and refusal.get('dispatch_performed') is False
                and isinstance(refusal.get('code'),str) and refusal['code']):
            error=PreDispatchRejected(refusal['code'])
            error.preflight_rejection=refusal
            raise error
        expected_incomplete=(evidence_read and run.returncode==2 and value.get('evidence_state')=='INSUFFICIENT_EVIDENCE')
        mutating=str(arguments[0]) in ('prepare','start','run')
        if ((run.returncode and not expected_incomplete) or value.get('success') is False
                or mutating and value.get('state') in ('BLOCKED','LAUNCH_FAILED')):
            # Even an error cannot prove a provider/systemd side effect did not occur.
            import re
            code=re.match(r'[A-Z][A-Z0-9_]+',str(value.get('error','')))
            error=RuntimeError('NATIVE_SERVICE_SUBMISSION_UNCONFIRMED')
            error.native_receipt={'script':script,'operation':str(arguments[0]),'returncode':run.returncode,
                                  'state':value.get('state'),'error_code':code.group(0) if code else 'UNCLASSIFIED'}
            raise error
        return value

    def preflight(self, kind):
        if kind!='ocr':
            return {'ready':True,'dispatch_performed':False}
        try:
            value=self._cli('ocr_runtime.py',['preflight'])
        except PreDispatchRejected as exc:
            return {'ready':False,'dispatch_performed':False,'code':str(exc),
                    'preflight_rejection':exc.preflight_rejection,
                    'message':exc.preflight_rejection.get('detail')}
        return {'ready':value.get('ready') is True,'dispatch_performed':False,
                'code':'NATIVE_OCR_BUSY' if value.get('ready') is not True else 'READY',
                'owner_pids':value.get('owner_pids',[])}

    def recover_reference(self, kind, frozen, directory):
        """Read a lost native handoff receipt; never prepare/start/retry a run."""
        if kind!='ocr':
            raise ValueError('NATIVE_HANDOFF_RECOVERY_UNAVAILABLE')
        native=directory/'native'
        self._cli('ocr_runtime.py',['status','--run',native])  # validates all frozen hashes
        path=native/'spec.json';spec=read_json(path);candidate=frozen['candidate']
        if (spec.get('workflow_action')!=str((directory/'invocation.json').resolve())
                or any(spec.get(key)!=candidate[key] for key in ('repo','base','head'))
                or spec.get('task')!=candidate['task_id'] or spec.get('attempt')!=str(candidate['attempt'])
                or spec.get('authorization_source')!=frozen['authorization_id']
                or spec.get('unit')!='coordinator-ocr-'+str(spec.get('run_id'))+'.service'):
            raise ValueError('NATIVE_HANDOFF_SCOPE_MISMATCH')
        return {'kind':kind,'native_directory':str(native),'native_id':spec['run_id'],
                'unit':spec['unit'],'spec':_sources([path])[0],
                'recovery':'EXISTING_FROZEN_NATIVE_RUN_ONLY','dispatch_performed':False}

    def submit(self, kind, frozen, request, directory):
        candidate=frozen['candidate']; run=frozen['run']
        if kind=='ocr':
            # Resolved before any native effect; a bad path raises
            # PreDispatchRejected so no UNKNOWN row can hold the serial gate.
            files=_request_inputs(kind,request,run)
            if 'background_text' in request:
                files['background_file']=str(_freeze_background(request,directory))
            native=directory/'native'
            arguments=['prepare','--project',run['requirement_root'],'--repo',candidate['repo'],
                       '--base',candidate['base'],'--head',candidate['head'],'--task',candidate['task_id'],
                       '--attempt',str(candidate['attempt']),'--authorization',frozen['authorization_id'],
                       '--background-file',files['background_file'],'--output',native,
                       '--workflow-action',directory/'invocation.json']
            if request.get('batches_file'):
                arguments+=['--batches-file',files['batches_file']]
            if request.get('reuse_action_id'):
                from workflow_service_recovery import reuse_source
                from business_ledger import BusinessLedger
                old,_=reuse_source(BusinessLedger(read_json(directory/'invocation.json')['ledger_database'],readonly=True),
                                   request['reuse_action_id'],candidate['candidate_id'])
                arguments+=['--reuse-run',old,'--retry-reason',request['retry_reason']]
            prepared=self._cli('ocr_runtime.py',arguments)
            spec_path=native/'spec.json'
            spec=read_json(spec_path)
            if (spec.get('repo')!=candidate['repo'] or spec.get('base')!=candidate['base'] or spec.get('head')!=candidate['head']
                or spec.get('task')!=candidate['task_id'] or spec.get('attempt')!=str(candidate['attempt'])):
                raise ValueError('NATIVE_OCR_PREPARED_SCOPE_MISMATCH')
            submitted=self._cli('ocr_runtime.py',['start','--run',native])
            return {'kind':kind,'native_directory':str(native),'native_id':spec['run_id'],
                    'unit':spec['unit'],'spec':_sources([spec_path])[0],
                    'inputs':_sources(files.values()),
                    'submission':submitted,'prepared':prepared}
        if kind=='strong_review':
            allowed={'package','risk','question','ac','gate','timeout_seconds'}
            if set(request)-allowed or any(not isinstance(request.get(key),str) or not request[key].strip()
                                          for key in ('package','risk','question','ac','gate')):
                raise ValueError('SPECIFIC_STRONG_REVIEW_PACKAGE_AND_QUESTION_REQUIRED')
            if type(request.get('timeout_seconds',1800)) is not int or request.get('timeout_seconds',1800)<=0:
                raise ValueError('INVALID_REVIEW_TIMEOUT')
            submitted=self._cli('strong_review.py',['run','--project',run['requirement_root'],
                '--repo',candidate['repo'],'--review-id',frozen['action_id'],'--task-id',candidate['task_id'],
                '--base',candidate['base'],'--target',candidate['head'],'--package',request['package'],
                '--risk',request['risk'],'--question',request['question'],'--ac',request['ac'],'--gate',request['gate'],
                '--timeout-seconds',request.get('timeout_seconds',1800),'--decision','REVIEW_NOW',
                '--workflow-action',directory/'invocation.json'])
            native=Path(submitted['result_path']).parent
            spec=read_json(native/'spec.json')
            if (spec.get('repo')!=candidate['repo'] or spec.get('base')!=candidate['base'] or spec.get('target')!=candidate['head']
                or spec.get('task_id')!=candidate['task_id'] or spec.get('review_id')!=frozen['action_id']):
                raise ValueError('NATIVE_STRONG_REVIEW_SCOPE_MISMATCH')
            return {'kind':kind,'native_directory':str(native),'native_id':submitted['call_id'],
                    'unit':spec['unit'],'spec':_sources([native/'spec.json'])[0],'submission':submitted}
        raise ValueError('UNSUPPORTED_SERVICE_KIND')

    def collect(self, kind, reference):
        directory=Path(reference['native_directory'])
        if _digest(reference['spec']['path'])!=reference['spec']['sha256']:
            raise ValueError('NATIVE_SERVICE_SPEC_CHANGED')
        if kind=='ocr':
            status=self._cli('ocr_runtime.py',['status','--run',directory])
            service=status.get('service') or {}
            if service.get('ActiveState') not in ('inactive','failed') or str(service.get('MainPID',''))!='0':
                return {'state':'WAITING','service':service}
            facts=self._cli('ocr_runtime.py',['results','--run',directory],evidence_read=True)
            ready=facts.get('evidence_state')=='READY_FOR_ADJUDICATION' and facts.get('coverage_complete') is True
            state='READY_FOR_ADJUDICATION' if ready else 'INCOMPLETE'
            paths=[directory/'spec.json',directory/'state.json']
            # inspect_results already validates real sessions/diagnoses/logs. Preserve
            # those exact bytes as a source index rather than replacing its checks.
            paths.extend(p for p in directory.rglob('*') if p.is_file() and p.name not in ('spec.json','state.json','service.log'))
            for source in facts.get('sources',[]):
                if _digest(source['path'])!=source['sha256']:
                    raise ValueError('NATIVE_OCR_RESULT_SOURCE_CHANGED')
                paths.append(Path(source['path']))
            return {'state':state,'service_stopped':True,'facts':facts,'sources':_sources(paths)}
        if kind=='strong_review':
            # The existing service ledger is the authority for its invocation.
            import importlib.util
            spec=importlib.util.spec_from_file_location('_workflow_native_strong_review',self.scripts/'strong_review.py')
            module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
            call=module.Ledger().call(reference['native_id'])
            service=module.service_info(reference['unit'])
            if service.get('ActiveState') not in ('inactive','failed') or str(service.get('MainPID',''))!='0':
                return {'state':'WAITING','service':service}
            if call['status'] in ('RESERVED','RUNNING'):
                return {'state':'UNKNOWN','service':service}
            result=Path(call['result_path'])
            ready=call['status']=='SUCCEEDED' and result.is_file() and bool(result.read_text().strip())
            paths=[directory/'spec.json',directory/'input.md',directory/'package.md']
            if result.exists(): paths.append(result)
            if Path(call['log_path']).exists(): paths.append(Path(call['log_path']))
            return {'state':'READY_FOR_ADJUDICATION' if ready else 'FAILED',
                    'service_stopped':True,'facts':{'call_id':call['id'],'status':call['status'],
                    'result':str(result),'approval':'NOT_DECIDED'},'sources':_sources(paths)}
        raise ValueError('UNSUPPORTED_SERVICE_KIND')


def submit_service(ledger, identity, candidate_id, kind, request, *, action_id,
                   authorization, command_id, adapter=None):
    """Register once, then call the sole native service. An unknown launch stays unknown."""
    checked=ledger.validate_identity(identity)
    if not checked['valid']:
        return checked
    if checked['work']['role']!='COORDINATOR':
        return {'status':'REJECTED','code':'COORDINATOR_REQUIRED'}
    snapshot=ledger.snapshot()
    candidate=next((v for v in snapshot['candidates'] if v['candidate_id']==candidate_id),None)
    if not candidate or candidate['run_id']!=checked['work']['run_id'] or candidate['state']!='CURRENT':
        return {'status':'REJECTED','code':'CANDIDATE_SCOPE_MISMATCH'}
    if not authorization:
        from workflow_autonomy import select_grant
        try:
            authorization=select_grant(ledger,candidate,kind)
        except ValueError as exc:
            return {'status':'REJECTED','code':str(exc).split(':',1)[0], 'message':str(exc),
                    'dispatch_performed':False,'authorization_consumed':False}
    if kind not in ('ocr','strong_review') or not isinstance(request,dict) or not isinstance(authorization,dict):
        return {'status':'REJECTED','code':'INVALID_SERVICE_REQUEST'}
    if kind=='ocr' and 'background_text' in request:
        try:
            _background_bytes(request)
        except ValueError as exc:
            return {'status':'REJECTED','code':str(exc)}
    run=next(v for v in snapshot['runs'] if v['run_id']==candidate['run_id'])
    directory=_directory(ledger,action_id)
    fingerprint=hashlib.sha256(_canonical([identity,candidate_id,kind,request,authorization]).encode()).hexdigest()
    with locked(directory.with_suffix('.lock')):
        sidecar=directory/'invocation.json'
        if sidecar.exists():
            previous=read_json(sidecar)
            if previous['input_sha256']!=fingerprint:
                return {'status':'REJECTED','code':'EXTERNAL_ACTION_ID_CONFLICT'}
            return {'status':'DUPLICATE','action_id':action_id,'state':previous['state'],
                    'reference':previous.get('reference'),'replay_allowed':False}
        selected_adapter=adapter or NativeServices()
        if hasattr(selected_adapter,'preflight'):
            try:
                readiness=selected_adapter.preflight(kind)
            except (OSError,RuntimeError,ValueError,subprocess.TimeoutExpired):
                return {'status':'UNVERIFIED','code':'NATIVE_SERVICE_PREFLIGHT_UNVERIFIED',
                        'dispatch_performed':False,'authorization_consumed':False}
            if readiness.get('ready') is not True:
                return {'status':'REJECTED','code':readiness.get('code','NATIVE_SERVICE_NOT_READY'),
                        'preflight':readiness,'dispatch_performed':False,'authorization_consumed':False}
        token=secrets.token_hex(32)
        service_id=kind+':'+action_id
        accepted={'authorization_id':authorization.get('authorization_id'),'service_id':service_id,
                  'service_token_sha256':hashlib.sha256(token.encode()).hexdigest()}
        registered=ledger.register_external_action(identity,action_id,kind,candidate_id,accepted,command_id=command_id)
        if registered['status'] not in ('COMMITTED','DUPLICATE'):
            if registered.get('code')=='SERVICE_GRANT_CALL_LIMIT':
                registered={**registered,'message':'额度用尽，需提额度',
                            'dispatch_performed':False,'authorization_consumed':False}
            return registered
        # If a prior process committed registration but died before this mapping,
        # do not invent a fresh credential or resubmit an unknown external action.
        if registered['status']=='DUPLICATE':
            return {'status':'UNVERIFIED','code':'EXTERNAL_DISPATCH_MAPPING_MISSING','action_id':action_id,'replay_allowed':False}
        directory.mkdir(parents=True,exist_ok=True)
        frozen={'action_id':action_id,'candidate':candidate,'run':run,
                'authorization_id':authorization.get('authorization_id')}
        value={'schema_version':1,'ledger_database':str(ledger.path.resolve()),'input_sha256':fingerprint,'state':'DISPATCHING',
               'action_id':action_id,'kind':kind,'candidate_id':candidate_id,'frozen':frozen,
               'service_identity':{'service_id':service_id,'token':token},'request':request}
        save_json(sidecar,value); sidecar.chmod(0o600)
        try:
            # Recheck revocation just before the native entry may submit work.
            if not ledger.external_dispatch_readiness(identity,action_id)['valid']:
                raise ValueError('CONTROL_REVOKED_BEFORE_SERVICE_SUBMISSION')
            reference=selected_adapter.submit(kind,frozen,request,directory)
            value.update(state='SUBMITTED',reference=reference)
            save_json(sidecar,value); sidecar.chmod(0o600)
            return {'status':'COMMITTED','action_id':action_id,'state':'SUBMITTED','reference':reference,
                    'event_id':registered.get('event_id'),'approval':'NOT_DECIDED'}
        except Exception as exc:
            # Only our own pre-dispatch validation can prove no side effect.
            # A transport error from an adapter may still have dispatched, so
            # it stays UNKNOWN and keeps the serial gate closed.
            if isinstance(exc,PreDispatchRejected):
                value.update(state='REJECTED',error=type(exc).__name__)
                if getattr(exc,'preflight_rejection',None):
                    value['preflight_rejection']=exc.preflight_rejection
                save_json(sidecar,value); sidecar.chmod(0o600)
                rejected=ledger.reject_external_action(action_id,
                    reason='提交前校验失败，未进入原生服务；'+str(exc),command_id=command_id+':rejected')
                if rejected['status'] not in ('COMMITTED','DUPLICATE'):
                    value.update(state='UNKNOWN',error_code='PRE_DISPATCH_PROOF_CONFLICT')
                    save_json(sidecar,value); sidecar.chmod(0o600)
                    ledger.mark_external_unknown(action_id,reason='预检回执与派发证据冲突，不得释放串行门',command_id=command_id+':unknown')
                    return {'status':'UNVERIFIED','code':'PRE_DISPATCH_REJECTION_NOT_RECORDED','ledger':rejected}
                return {'status':'REJECTED','code':str(exc),'action_id':action_id,
                        'detail':(value.get('preflight_rejection') or {}).get('detail',str(exc)),
                        'preflight_rejection':value.get('preflight_rejection'),
                        'replay_allowed':False,'dispatch_performed':False,
                        'authorization_consumed':False,'ledger':rejected}
            from workflow_service_recovery import error_receipt
            value.update(state='UNKNOWN',**error_receipt(exc))
            if getattr(exc,'native_receipt',None):
                value['native_receipt']=exc.native_receipt
            save_json(sidecar,value); sidecar.chmod(0o600)
            ledger.mark_external_unknown(action_id,reason='独立服务提交结果未核实；'+type(exc).__name__,command_id=command_id+':unknown')
            return {'status':'UNVERIFIED','code':'EXTERNAL_SUBMISSION_UNKNOWN','action_id':action_id,'replay_allowed':False}


def collect_service_result(ledger, action_id, *, command_id, adapter=None):
    """Collect one existing service terminal; caller need not own business work."""
    directory=_directory(ledger,action_id)
    with locked(directory.with_suffix('.lock')):
        value=read_json(directory/'invocation.json')
        if not value:
            return {'status':'UNVERIFIED','code':'EXTERNAL_DISPATCH_MAPPING_MISSING','replay_allowed':False}
        action=_action(ledger,action_id)
        if action['state'] not in ('SUBMITTED','UNKNOWN'):
            return {'status':'DUPLICATE','state':action['state'],'action_id':action_id,'business_pass':False}
        try:
            selected_adapter=adapter or NativeServices()
            if not value.get('reference'):
                if not hasattr(selected_adapter,'recover_reference'):
                    return {'status':'UNVERIFIED','code':'EXTERNAL_DISPATCH_MAPPING_MISSING','replay_allowed':False}
                value['reference']=selected_adapter.recover_reference(value['kind'],value['frozen'],directory)
                value['reference_recovered_at']=time.time()
                save_json(directory/'invocation.json',value);(directory/'invocation.json').chmod(0o600)
            observed=selected_adapter.collect(value['kind'],value['reference'])
            if observed.get('state') in ('WAITING','UNKNOWN') or not observed.get('service_stopped'):
                return {'status':'UNVERIFIED','code':'SERVICE_NOT_TERMINAL','action_id':action_id,'observation':observed,'replay_allowed':False}
            candidate=value['frozen']['candidate']
            terminal={key:candidate[key] for key in ('candidate_id','run_id','task_id','attempt','base','head','ac_version')}
            terminal.update(observed,action_id=action_id,service_id=value['service_identity']['service_id'],business_pass=False)
            path=directory/'terminal.json'
            if path.exists() and read_json(path)!=terminal:
                return {'status':'REJECTED','code':'SERVICE_TERMINAL_EVIDENCE_CHANGED'}
            save_json(path,terminal)
            return ledger.record_service_artifact(action_id,{'path':str(path),'sha256':_digest(path)},
                service_identity=value['service_identity'],command_id=command_id)
        except (OSError,ValueError,RuntimeError,KeyError) as exc:
            from workflow_service_recovery import error_receipt
            return {'status':'UNVERIFIED','code':'SERVICE_TERMINAL_EVIDENCE_UNVERIFIED','action_id':action_id,
                    'replay_allowed':False,**error_receipt(exc)}


def adjudicate_service(ledger, identity, action_id, result, summary, *, command_id, supersedes=None):
    return ledger.adjudicate_service(identity,action_id,result,summary,command_id=command_id,supersedes=supersedes)


def validate_native_action(path, kind, scope):
    """Authenticate one pre-registered service across the caller's lifetime.

    This is a narrow alternative to the legacy ROLE-BINDINGS guard, only for
    the exact private adapter receipt. It creates no role, work or execution.
    """
    from business_ledger import BusinessLedger
    path=Path(path).resolve()
    stat=path.stat()
    if stat.st_uid!=os.getuid() or stat.st_mode & 0o077:
        raise ValueError('WORKFLOW_ACTION_RECEIPT_NOT_PRIVATE')
    value=read_json(path)
    ledger=BusinessLedger(value['ledger_database'],readonly=True)
    if path!=_directory(ledger,value['action_id'])/'invocation.json' or value['kind']!=kind:
        raise ValueError('WORKFLOW_ACTION_RECEIPT_SCOPE_MISMATCH')
    if value['state'] not in ('DISPATCHING','SUBMITTED','UNKNOWN'):
        raise ValueError('WORKFLOW_ACTION_NOT_INFLIGHT')
    snapshot=ledger.snapshot()
    action=next((row for row in snapshot['external_actions'] if row['action_id']==value['action_id']),None)
    candidate=value['frozen']['candidate']; run=value['frozen']['run']
    if (not action or action['kind']!=kind or action['candidate_id']!=candidate['candidate_id']
        or action['state'] not in ('SUBMITTED','UNKNOWN')):
        raise ValueError('WORKFLOW_REGISTERED_ACTION_REQUIRED')
    service=value['service_identity']; auth=action['authorization']
    if (auth.get('service_id')!=service['service_id'] or
        auth.get('service_token_sha256')!=hashlib.sha256(service['token'].encode()).hexdigest()):
        raise ValueError('WORKFLOW_SERVICE_CREDENTIAL_MISMATCH')
    grant=next((row for row in snapshot['authorizations'] if row['authorization_id']==auth['authorization_id']),None)
    from workflow_autonomy import covers
    deadline=auth.get('execution_deadline', (grant or {}).get('spec',{}).get('expires_at',0))
    if (not grant or grant['kind']!='external' or grant['revoked_at'] is not None
        or not covers(grant['spec'],candidate,kind) or deadline<=time.time()):
        raise ValueError('WORKFLOW_FROZEN_SERVICE_AUTHORIZATION_REQUIRED')
    if auth.get('grant_sha256')!=hashlib.sha256(_canonical(grant['spec']).encode()).hexdigest():
        raise ValueError('WORKFLOW_SERVICE_GRANT_CHANGED')
    current_run=next((row for row in snapshot['runs'] if row['run_id']==run['run_id']),None)
    current_candidate=next((row for row in snapshot['candidates'] if row['candidate_id']==candidate['candidate_id']),None)
    current_task=next((row for row in snapshot['tasks'] if row['run_id']==run['run_id'] and row['task_id']==candidate['task_id']),None)
    if (not current_candidate or current_candidate['state']!='CURRENT'
        or any(current_candidate[key]!=candidate[key] for key in ('run_id','task_id','attempt','repo','base','head','ac_version'))
        or not current_run or current_run['control'] not in ('ACTIVE','DRAINING') or not current_task
        or current_task['phase']=='CANCELLED' or current_task['unresolved_reason']=='CONTROL_PAUSED'):
        raise ValueError('WORKFLOW_SERVICE_CONTROL_REVOKED')
    expected={'project':run['requirement_root'],'repo':candidate['repo'],'base':candidate['base'],
              'head':candidate['head'],'task':candidate['task_id'],'attempt':str(candidate['attempt']),
              'review_id':value['action_id']}
    if any(str(value)!=str(expected[key]) for key,value in scope.items()):
        raise ValueError('WORKFLOW_NATIVE_SERVICE_SCOPE_MISMATCH')
    return {'authorization_id':auth['authorization_id'],'project':run['requirement_root'],
            'repo':candidate['repo'],'task_id':candidate['task_id'],'action_id':value['action_id']}


@contextmanager
def native_dispatch_guard(path, kind, scope):
    """Serialize grant revocation with the actual provider process creation.

    The lock spans only identity checking and Popen, never model execution.
    No business row, lease, event or clock is written by the service.
    """
    value=read_json(Path(path).resolve())
    database=Path(value['ledger_database']).resolve()
    connection=sqlite3.connect(database.as_uri()+'?mode=rw',uri=True,timeout=15)
    try:
        connection.execute('BEGIN IMMEDIATE')
        validate_native_action(path,kind,scope)
        yield
    finally:
        connection.rollback()
        connection.close()
