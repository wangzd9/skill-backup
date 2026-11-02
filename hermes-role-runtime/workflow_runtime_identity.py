"""Per-process activation and actual-call receipts, with immutable manifests."""
import hashlib
import json
import os
from pathlib import Path
import time


def record_activation(home, activation):
    from runtime_common import save_json
    from business_ledger import _process
    root=Path(home)/'runtime/role-observation';process=_process(os.getpid()) or {}
    start=process.get('process_start','unknown')
    from workflow_migration import code_version
    version=code_version()
    digest=version['digest']
    manifest=root/'releases'/(digest+'.json')
    if not manifest.exists():save_json(manifest,version)
    activation_path=root/'processes'/(str(os.getpid())+'-'+str(start)+'-activation.json')
    save_json(activation_path,activation)
    row={'pid':os.getpid(),'process_start':start,'activation_digest':digest,'manifest':str(manifest),
        'activation_receipt':str(activation_path),'activated_at':time.time(),'state':activation.get('state'),
        'scope':'named future-call activation; running frames retain their prior code'}
    save_json(root/'processes'/(str(os.getpid())+'-'+str(start)+'.json'),row)
    cmdline=Path('/proc/self/cmdline').read_bytes().split(b'\0')
    gateway=any(cmdline[i:i+3]==[b'hermes_cli.main',b'gateway',b'run'] for i in range(len(cmdline)))
    legacy=root/'mechanism-activation.json'
    if gateway or not legacy.exists():save_json(legacy,{**activation,'process_identity':row})
    return row


def adopt(home, identity, scope, request_id):
    if not identity or not request_id:return
    from runtime_common import save_json,read_json,locked
    root=Path(home)/'runtime/role-observation/adoptions'
    name=hashlib.sha256((identity['activation_digest']+':'+str((scope or {}).get('execution_id') or
        str(identity['pid'])+':'+str(identity['process_start']))).encode()).hexdigest()
    path=root/(name+'.json')
    with locked(path.with_suffix('.lock')):
        previous=read_json(path,{}) or {}
        save_json(path,{**identity,'first_adopted_at':previous.get('first_adopted_at',time.time()),
            'adopted_at':time.time(),'first_request_id':previous.get('first_request_id',request_id),
            'latest_api_request_id':request_id,'request_hook_calls':previous.get('request_hook_calls',0)+1,
            'role':(scope or {}).get('role'),'run_id':(scope or {}).get('run_id'),
            'work_id':(scope or {}).get('work_id'),'execution_id':(scope or {}).get('execution_id'),
            'proof':'API hook invoked by this process; per-request timing remains in role-time.db, not a business result'})
