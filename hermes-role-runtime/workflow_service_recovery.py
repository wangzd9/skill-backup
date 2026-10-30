"""Resume an observed stopped OCR chain, reusing only verified successful batches."""
import hashlib
import json
from pathlib import Path

from runtime_common import read_json


def reuse_source(ledger, action_id, candidate_id):
    from workflow_services import _action, _directory, _digest, PreDispatchRejected
    prior=_action(ledger,action_id)
    if prior['kind']!='ocr' or prior['candidate_id']!=candidate_id or prior['state']!='INCOMPLETE':
        raise PreDispatchRejected('RESUME_REQUIRES_OBSERVED_STOPPED_INCOMPLETE_OCR')
    evidence=prior.get('evidence') or {}
    if not evidence.get('path') or _digest(evidence['path'])!=evidence.get('sha256'):
        raise PreDispatchRejected('RESUME_TERMINAL_EVIDENCE_CHANGED')
    terminal=read_json(evidence['path'])
    if not terminal.get('service_stopped'):
        raise PreDispatchRejected('RESUME_SERVICE_NOT_STOPPED')
    for source in terminal.get('sources',[]):
        if _digest(source['path'])!=source['sha256']:
            raise PreDispatchRejected('RESUME_SOURCE_EVIDENCE_CHANGED')
    directory=_directory(ledger,action_id)
    value=read_json(directory/'invocation.json')
    reference=value.get('reference') or {}
    native=directory/'native'
    if Path(reference.get('native_directory','')).resolve()!=native.resolve():
        raise PreDispatchRejected('RESUME_NATIVE_DIRECTORY_MISMATCH')
    return native,value


def resume_readiness(ledger, action_id):
    """不消耗补审机会：先识别既有补审与固定截止，原因文字不等于修复证据。"""
    from workflow_services import _action, _directory
    prior=_action(ledger,action_id)
    native=_directory(ledger,action_id)/'native'
    try:
        spec=read_json(native/'spec.json')
        state=read_json(native/'state.json')
        if not isinstance(spec,dict) or not isinstance(state,dict):
            return {'allowed':False,'code':'RESUME_DIAGNOSIS_UNVERIFIED'}
        if spec.get('retry_depth',0)>=1:
            return {'allowed':False,'code':'RETRY_LIMIT'}
        for other in ledger.snapshot()['external_actions']:
            if other['candidate_id']!=prior['candidate_id'] or other['state']=='REJECTED':
                continue
            invocation=read_json(_directory(ledger,other['action_id'])/'invocation.json',{})
            if not isinstance(invocation,dict):
                return {'allowed':False,'code':'RESUME_DIAGNOSIS_UNVERIFIED'}
            if invocation.get('request',{}).get('reuse_action_id')==action_id:
                return {'allowed':False,'code':'DUPLICATE_REVIEW'}
        if state.get('state')=='HARD_DEADLINE':
            return {'allowed':False,'code':'ROOT_CAUSE_REPAIR_REQUIRED'}
        return {'allowed':True,'code':'DIAGNOSIS_AND_REPAIR_REASON_REQUIRED'}
    except (OSError,ValueError,TypeError):
        return {'allowed':False,'code':'RESUME_DIAGNOSIS_UNVERIFIED'}


def resume(ledger, identity, action_id, new_action_id, reason, *, authorization=None, command_id):
    from workflow_services import _action, submit_service
    if not isinstance(reason,str) or not reason.strip() or action_id==new_action_id:
        raise ValueError('RESUME_REQUIRES_NEW_ACTION_ID_AND_REPAIR_REASON')
    readiness=resume_readiness(ledger,action_id)
    if not readiness['allowed']:
        return {'status':'REJECTED','code':readiness['code'],'dispatch_performed':False,
                'authorization_consumed':False,
                'next_action':'先核已有发现并按原授权返修；审查覆盖保留。固定截止需版本化修复，不能原样耗用补审。'}
    prior=_action(ledger,action_id)
    _, value=reuse_source(ledger,action_id,prior['candidate_id'])
    request={**value['request'],'reuse_action_id':action_id,'retry_reason':reason}
    return submit_service(ledger,identity,prior['candidate_id'],'ocr',request,action_id=new_action_id,
                          authorization=authorization,command_id=command_id)


def error_receipt(exc):
    """Keep actionable codes at the front; never echo provider payloads or tokens."""
    import re
    import traceback
    match=re.match(r'[A-Z][A-Z0-9_]{2,}',str(exc))
    frames=traceback.extract_tb(exc.__traceback__)
    return {'error':type(exc).__name__,'error_code':match.group(0) if match else 'SERVICE_EVIDENCE_UNVERIFIED',
            'location':Path(frames[-1].filename).name+':'+str(frames[-1].lineno) if frames else None,
            'next_action':'核对该 action 的原生状态和冻结证据；未知副作用不能重投'}


def rejection_proof(value, directory):
    """Fail closed on any evidence of dispatch, including an interrupted start.

    Legacy receipts are accepted only for the audited prepare version refusal;
    prepare in that runtime never invoked systemd or an OCR review.
    """
    native=Path(directory)/'native'
    if value.get('reference'):
        return False
    receipt=value.get('preflight_rejection') or {}
    proven=(receipt.get('schema')=='ocr.preflight-rejection/v1'
        and receipt.get('stage')=='preflight' and receipt.get('operation') in ('prepare','start')
        and type(receipt.get('side_effects')) is int and receipt['side_effects']==0
        and receipt.get('dispatch_performed') is False and bool(receipt.get('code')))
    legacy=value.get('native_receipt')
    legacy_proven=(legacy=={'script':'ocr_runtime.py','operation':'prepare','returncode':2,
                           'state':'BLOCKED','error_code':'OCR_VERSION_UNVALIDATED'} and not native.exists())
    if legacy and not legacy_proven:
        return False
    if native.exists():
        try:
            state=read_json(native/'state.json',{})
        except (OSError,ValueError):
            return False
        if not isinstance(state,dict):
            return False
        allowed_state=(state.get('state')=='PREPARED' if receipt.get('operation')=='start'
                       else state.get('state') in (None,'PREPARED'))
        if (not proven or not allowed_state
                or any((native/name).exists() for name in ('dispatch-intent.json','service.log'))
                or state.get('pid') or state.get('batches')):
            return False
    # Original in-process argument validation predates the receipt protocol.
    argument_refusal=(value.get('state')=='REJECTED' and value.get('error')=='PreDispatchRejected'
                      and not native.exists() and not legacy)
    return proven or legacy_proven or argument_refusal


def reject(ledger, identity, action_id, reason, *, command_id):
    """Coordinator-bound reconciliation; never dispatch, retry or approve."""
    from workflow_services import _action, _directory
    checked=ledger.validate_identity(identity)
    if not checked.get('valid'):
        return checked
    if checked['work']['role']!='COORDINATOR':
        return {'status':'REJECTED','code':'COORDINATOR_REQUIRED'}
    action=_action(ledger,action_id)
    candidate=next(c for c in ledger.snapshot()['candidates'] if c['candidate_id']==action['candidate_id'])
    if candidate['run_id']!=checked['work']['run_id']:
        return {'status':'REJECTED','code':'CANDIDATE_SCOPE_MISMATCH'}
    from runtime_common import locked
    with locked(_directory(ledger,action_id).with_suffix('.lock')):
        return ledger.reject_external_action(action_id,reason=reason,command_id=command_id,identity=identity)
