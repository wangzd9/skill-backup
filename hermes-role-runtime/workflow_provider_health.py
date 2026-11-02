"""Bounded provider cooldowns. No route, quota, or business-result changes."""
import hashlib
from pathlib import Path
import time


def _path(home, provider, model):
    key=hashlib.sha256((str(provider)+'\0'+str(model)).encode()).hexdigest()
    return Path(home)/'runtime/provider-health'/(key+'.json')


def record(home, *, success, provider='', model='', at=None, scope=None, **metadata):
    from runtime_common import locked,read_json,save_json
    request_id=metadata.get('api_request_id')
    if not request_id or metadata.get('retry_pending'):return None
    at=time.time() if at is None else at;path=_path(home,provider,model)
    text=str(metadata.get('error_message','')).lower()
    status=metadata.get('status_code');kind=str(metadata.get('error_type','')).lower()
    quota=status==402 or any(s in text for s in ('insufficient_quota','insufficient balance','余额不足','credit balance'))
    transient=status in (408,429,500,502,503,504) or any(s in kind for s in ('timeout','connection'))
    with locked(path.with_suffix('.lock')):
        old=read_json(path,{}) or {}
        if old.get('api_request_id')==request_id and old.get('success')==success:return old
        failures=0 if success else old.get('consecutive_failures',0)+1 if quota or transient else 0
        delay=900 if quota else min(900,60*2**min(4,failures-3)) if failures>=3 else 0
        row={'provider':provider,'model':model,'state':'COOLDOWN' if delay else 'HEALTHY' if success else 'ERROR_OBSERVED',
            'api_request_id':request_id,'success':success,'observed_at':at,'consecutive_failures':failures,
            'failure_class':'QUOTA' if quota else 'TRANSIENT' if transient else None,
            'retry_after':at+delay,'responsible_role':'MAINTENANCE',
            'work_id':(scope or {}).get('work_id'),'run_id':(scope or {}).get('run_id'),
            'next_action':'冷却到期按原模型策略重核；保留工作与未知动作，不能因供应故障重放业务副作用。',
            'business_result_unchanged':True}
        save_json(path,row);return row


def cooldown(home, provider, model, *, at=None):
    from runtime_common import read_json
    row=read_json(_path(home,provider,model),{}) or {}
    at=time.time() if at is None else at
    return row if row.get('state')=='COOLDOWN' and at<row.get('retry_after',0) else None


def summary(home, *, at=None):
    from runtime_common import read_json
    at=time.time() if at is None else at;rows=[]
    for path in sorted((Path(home)/'runtime/provider-health').glob('*.json')):
        try:row=read_json(path,{}) or {}
        except (OSError,ValueError):continue
        if row.get('state')!='HEALTHY':
            rows.append({k:row.get(k) for k in ('provider','model','state','failure_class','retry_after',
                'observed_at','responsible_role','work_id','next_action')})
    return {'checked_at':at,'observations':rows,
        'policy':'供应故障及过期观察不等于业务失败；冷却到期依原路线复核，不自动换模型。'}
