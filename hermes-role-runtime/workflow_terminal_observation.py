"""Verify durable service terminal facts independently of transient units."""
import hashlib
import json
from pathlib import Path


def terminal(action, candidate, facts, *, ledger_dir=None):
    if action['state'] in ('SUBMITTED','UNKNOWN'):return None
    if action['state']=='REJECTED' and not action.get('evidence'):
        return rejected(action,candidate,ledger_dir)
    refs=[]
    if action.get('evidence'):refs.append(action['evidence'])
    refs += [v['evidence'] for v in facts['verifications'] if v.get('action_id')==action['action_id']]
    if not refs:raise ValueError('TERMINAL_SERVICE_RECEIPT_MISSING')
    for ref in refs:
        raw=Path(ref['path']).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=ref.get('sha256'):raise ValueError('TERMINAL_SERVICE_RECEIPT_CHANGED')
        value=json.loads(raw)
        if (value.get('action_id')!=action['action_id'] or value.get('candidate_id')!=candidate['candidate_id']
                or any(value.get(k)!=candidate.get(k) for k in ('run_id','task_id','attempt','base','head','ac_version'))
                or value.get('service_id')!=action['authorization'].get('service_id')):
            raise ValueError('TERMINAL_SERVICE_RECEIPT_IDENTITY_MISMATCH')
        if value.get('service_stopped') is not True:raise ValueError('TERMINAL_SERVICE_STOP_NOT_VERIFIED')
        for source in value.get('sources',[]):
            if hashlib.sha256(Path(source['path']).read_bytes()).hexdigest()!=source.get('sha256'):
                raise ValueError('TERMINAL_SERVICE_SOURCE_CHANGED')
    return {'observation':'terminal_receipt_verified','receipt_count':len(refs),
        'writer_liveness_probe_required':False,
        'result_completeness':'INCOMPLETE' if action['state'] in ('INCOMPLETE','FAILED','CANCELLED','REJECTED') else 'RECORDED',
        'business_pass':False}


def rejected(action, candidate, ledger_dir):
    """No-dispatch proof is a separate fact, never a successful service result."""
    from workflow_service_recovery import rejection_proof
    if ledger_dir is None:raise ValueError('REJECTION_LEDGER_DIRECTORY_REQUIRED')
    directory=Path(ledger_dir)/'services'/hashlib.sha256(action['action_id'].encode()).hexdigest()
    path=directory/'invocation.json'
    def sample():
        # Include every file/existence check used by the zero-dispatch proof.
        return {name:(directory/name).read_bytes() if (directory/name).is_file() else
            'EXISTS' if (directory/name).exists() else None for name in
            ('invocation.json','native','native/state.json','native/dispatch-intent.json','native/service.log')}
    before=sample();raw=before['invocation.json'];value=json.loads(raw)
    frozen=(value.get('frozen') or {}).get('candidate') or {}
    if (value.get('action_id')!=action['action_id'] or value.get('candidate_id')!=candidate['candidate_id']
            or any(frozen.get(k)!=candidate.get(k) for k in
                ('candidate_id','run_id','task_id','attempt','base','head','ac_version'))):
        raise ValueError('REJECTION_PROOF_IDENTITY_MISMATCH')
    if not rejection_proof(value,directory):raise ValueError('REJECTION_ZERO_DISPATCH_UNVERIFIED')
    if sample()!=before:raise ValueError('REJECTION_PROOF_CHANGED_DURING_READ')
    return {'observation':'rejection_no_dispatch_verified','dispatch_performed':False,
        'writer_liveness_probe_required':False,'result_completeness':'NOT_DISPATCHED',
        'proof':{'path':str(path),'sha256':hashlib.sha256(raw).hexdigest()},
        'rejection_code':(value.get('preflight_rejection') or {}).get('code') or
            (value.get('native_receipt') or {}).get('error_code') or value.get('error'),
        'business_pass':False,'sampling':'separate source samples, unchanged before/after; not a business verdict'}


def layers(observation):
    current=observation.get('current_run_id');now=observation.get('at',0)
    values=observation.get('anomalies',[])
    history=[r for r in values if r.get('last_seen',now)<now-86400]
    executions={e['execution_id']:e for rows in observation.get('roles',{}).values() for row in rows for e in row.get('executions',[])}
    return {'current_run_id':current,'collector_fault_count':len(observation.get('faults',[])),
        'business_authority':{k:observation.get(k) for k in ('mode','admission_open','event_high_water')},
        'current_writer_count':sum(e.get('run_id')==current and e.get('writers_present') is True for e in executions.values()),
        'unverified_writer_scope_count':sum(e.get('writer_scope_verified') is not True for e in executions.values()),
        'current_inflight_service_count':sum(s.get('run_id')==current and s.get('state') in ('SUBMITTED','UNKNOWN') for s in observation.get('services',[])),
        'historical_evidence_debt_count':len(history),'historical_debt_archived':False,
        'classification':'Age separates display only; no unknown writer is released.',
        'terminal_services_verified':sum(r.get('observation')=='terminal_receipt_verified' for r in observation.get('services',[])),
        'rejections_no_dispatch_verified':sum(r.get('observation')=='rejection_no_dispatch_verified' for r in observation.get('services',[])),
        'terminal_result_incomplete':sum(r.get('result_completeness')=='INCOMPLETE' for r in observation.get('services',[])),
        'activity_policy':observation.get('activity_policy',{'state':'NOT_OBSERVED'}),
        'business_progress':observation.get('business_progress',[]),
        'capacity':observation.get('capacity'),
        'notification_health':observation.get('notification_health')}
