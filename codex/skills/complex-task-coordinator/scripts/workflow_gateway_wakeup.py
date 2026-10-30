"""Coalesced worker hints consumed by the existing gateway event tick.

Hints grant no authority and never ACK business events. No new ticker, native
claim or model is created in the submitting worker.
"""
import hashlib
from pathlib import Path
import time
import uuid

from runtime_common import locked,read_json,save_json


def _path(home,pool_path,role):
    key=hashlib.sha256((str(Path(pool_path).resolve())+'\0'+role).encode()).hexdigest()
    return Path(home)/'runtime/role-gateway-wakeups'/(key+'.json')


def submit(home,pool_path,role,committed_at):
    path=_path(home,pool_path,role)
    with locked(path.with_suffix('.lock')):
        old=read_json(path) if path.exists() else {}
        from runtime_common import process_identity
        import os
        origin=process_identity(os.getpid()) or {'pid':os.getpid(),'process_start':None,'boot_id':None}
        markers=([committed_at] if isinstance(committed_at,dict) else committed_at) or []
        markers=[{**marker,'origin_process':marker.get('origin_process') or origin} for marker in markers]
        # Retain the earliest delivery latency origin, and latest business input.
        value={'token':uuid.uuid4().hex,'pool_path':str(Path(pool_path).resolve()),'role':role,
               'created_at':old.get('created_at',time.time()),'updated_at':time.time(),
               'first_commit':old.get('first_commit') or next(iter(markers),None),
               'latest_commit':next(iter(markers[::-1]),old.get('latest_commit'))}
        save_json(path,value)
    return {'state':'PENDING_GATEWAY','reason':'EXTERNAL_WORKER_REQUIRES_GATEWAY_DISPATCH',
            'event_preserved':True,'request_id':value['token'],
            'recovery':'existing_gateway_event_tick'}


def pending(home,pool_path,role):
    path=_path(home,pool_path,role)
    value=read_json(path) if path.exists() else None
    if value and (value.get('pool_path')!=str(Path(pool_path).resolve()) or value.get('role')!=role):
        raise ValueError('GATEWAY_WAKEUP_SCOPE_MISMATCH')
    return value


def acknowledge(home,pool_path,role,token):
    path=_path(home,pool_path,role)
    with locked(path.with_suffix('.lock')):
        value=read_json(path) if path.exists() else {}
        if value.get('token')==token:
            path.unlink()
