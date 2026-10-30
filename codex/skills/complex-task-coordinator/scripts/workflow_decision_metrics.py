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
            if key not in current:
                row['state']='NOT_IN_LATEST_DIRECTORY_NOT_A_VERDICT'
                row.setdefault('invalidated_at',now)
        selected=result.get('decision_id') if 'material' in result and operation=='prepare' and (
            result.get('status','FOUND')=='FOUND' and result.get('source_status','FOUND')=='FOUND'
            and not result.get('refresh_required')) else None
        if selected in known:known[selected]['query_count']+=1
        save_json(path,{'work_id':scope['work_id'],'role':scope['role'],'run_id':scope['run_id'],
            'observed_at':now,'items':known,'notice':'absence is not completion; no prompt or conclusion text'})
        active=root/'executions'/(_key(scope['execution_id'])+'.json')
        prior=read_json(active,{}) or {};fingerprints=prior.get('material_fingerprints',{})
        invalid_selected=result.get('decision_id') if operation=='prepare' and (
            result.get('refresh_required') or result.get('source_status','FOUND')!='FOUND') else None
        ids=[key for key in prior.get('decision_ids',[]) if key in current and
            fingerprints.get(key)==known[key]['fingerprint'] and key!=invalid_selected]
        retired=[{'decision_id':key,'invalidated_at':now,'reason':'REMOVED_OR_MATERIAL_CHANGED'}
            for key in prior.get('decision_ids',[]) if key not in ids]
        if selected:
            ids=list(dict.fromkeys([*ids,selected]))[-8:];fingerprints[selected]=known[selected]['fingerprint']
        save_json(active,{'decision_ids':ids,'material_fingerprints':{k:fingerprints.get(k) for k in ids},
            'retired':[*prior.get('retired',[]),*retired][-32:],
            'sampling':result.get('sampling'),'observed_at':now})
        if result.get('query_measurement'):
            from workflow_views import size
            save_json(root/'queries'/(_key([scope['execution_id'],operation])+'.json'),
                {'operation':operation,'execution_id':scope['execution_id'],'observed_at':now,
                 'response_bytes':size(result),**result['query_measurement']})


def request(ledger, scope, metadata):
    if not scope or scope.get('kind')!='business':return
    physical=metadata.get('physical_attempt_id');logical=metadata.get('api_request_id')
    if not physical or not logical:return  # Never fabricate physical identity.
    root=_root(ledger)
    active=read_json(root/'executions'/(_key(scope['execution_id'])+'.json'),{}) or {}
    ids=active.get('decision_ids',[])
    if not ids:return
    sampling=active.get('sampling') or {}
    # Conservative and cheap: any new ledger event ends the attribution window
    # until a fresh query revalidates materials. No claim about causality.
    if sampling:
        with ledger._connection() as db:
            high=db.execute('SELECT COALESCE(MAX(event_id),0) FROM events').fetchone()[0]
        if high!=sampling.get('event_high_water'):return
    path=root/'requests'/(_key([scope['execution_id'],physical])+'.json')
    with locked(root/'requests.lock'):
        if path.exists():return
        save_json(path,{'physical_attempt_id':physical,'api_request_id':logical,
            'execution_id':scope['execution_id'],'work_id':scope['work_id'],
            'decision_ids':ids,'material_fingerprints':active.get('material_fingerprints',{}),
            'validated_at':active.get('observed_at'),'lifecycle_boundary':'latest directory/material validation; ledger high-water unchanged when available',
            'attribution':'shared_context_no_cost_split',
            'usage_source':'role-usage.db; count the physical request once, not once per decision',
            'observed_at':time.time()})
