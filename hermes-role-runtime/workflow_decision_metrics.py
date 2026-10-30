"""Metadata-only preparation/request linkage. Shared requests carry no split cost."""
import hashlib
import os
from pathlib import Path
import time
from runtime_common import locked, read_json, save_json


def _root(ledger):
    root=ledger.path.parent/'decision-metrics'
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    os.chmod(root,0o700)
    return root


def _key(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def observe(ledger, scope, operation, result, items):
    root=_root(ledger);path=root/'works'/(_key(scope['work_id'])+'.json')
    with locked(root/'observations.lock'):
        old=read_json(path,{}) or {};now=time.time();known=old.get('items',{})
        current={i['decision_id'] for i in items}
        for item in items:
            prior=known.get(item['decision_id'],{})
            known[item['decision_id']]={'first_seen':prior.get('first_seen',now),'last_seen':now,
                'fingerprint':item['material_fingerprint'],'action':item['action'],
                'state':'IN_LATEST_DIRECTORY','query_count':prior.get('query_count',0)}
        for key,row in known.items():
            if key not in current:row['state']='NOT_IN_LATEST_DIRECTORY_NOT_A_VERDICT'
        selected=result.get('decision_id') if 'material' in result and operation=='prepare' else None
        if selected in known:known[selected]['query_count']+=1
        save_json(path,{'work_id':scope['work_id'],'role':scope['role'],'run_id':scope['run_id'],
            'observed_at':now,'items':known,'notice':'absence is not completion; no prompt or conclusion text'})
        if selected:
            active=root/'executions'/(_key(scope['execution_id'])+'.json')
            prior=read_json(active,{}) or {};ids=list(dict.fromkeys([*prior.get('decision_ids',[]),selected]))[-8:]
            fingerprints={**prior.get('material_fingerprints',{}),selected:known[selected]['fingerprint']}
            save_json(active,{'decision_ids':ids,'material_fingerprints':{k:fingerprints.get(k) for k in ids},'observed_at':now})


def request(ledger, scope, metadata):
    if not scope or scope.get('kind')!='business':return
    physical=metadata.get('physical_attempt_id');logical=metadata.get('api_request_id')
    if not physical or not logical:return  # Never fabricate physical identity.
    root=_root(ledger)
    active=read_json(root/'executions'/(_key(scope['execution_id'])+'.json'),{}) or {}
    ids=active.get('decision_ids',[])
    if not ids:return
    path=root/'requests'/(_key([scope['execution_id'],physical])+'.json')
    with locked(root/'requests.lock'):
        if path.exists():return
        save_json(path,{'physical_attempt_id':physical,'api_request_id':logical,
            'execution_id':scope['execution_id'],'work_id':scope['work_id'],
            'decision_ids':ids,'material_fingerprints':active.get('material_fingerprints',{}),
            'attribution':'shared_context_no_cost_split',
            'usage_source':'role-usage.db; count the physical request once, not once per decision',
            'observed_at':time.time()})
