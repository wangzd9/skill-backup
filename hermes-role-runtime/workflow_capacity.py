"""Bounded, cached storage observations; never delete or compact audit sources."""
from pathlib import Path

ATTACHMENT_ROOTS=('input-pages','model-views','decision-views','query-views','delivery-evidence',
    'decision-metrics','operations','services','mysql-sessions','input-archives')


def _advance(root, state, budget, at):
    """Resume directory enumeration; completed roots do not monopolize a tick."""
    import os
    if not state or state.get('complete'):
        last={k:state[k] for k in ('bytes','files','started_at','sampled_at')} if state and state.get('complete') else None
        state={'queue':[{'path':'.','offset':0}], 'bytes':0,'files':0,'complete':False,'started_at':at,'last_complete':last}
    remaining=budget
    while state['queue'] and remaining:
        entry=state['queue'][0];folder=root/entry['path']
        if folder.is_symlink():state['queue'].pop(0);continue
        try:
            stamp=folder.stat().st_mtime_ns
            if entry.get('stamp',stamp)!=stamp:
                # An in-flight directory changed: restart only this root. Its
                # partial total is never advertised as a complete inventory.
                return {'queue':[{'path':'.','offset':0}], 'bytes':0,'files':0,
                    'complete':False,'started_at':at,'restart_reason':'DIRECTORY_CHANGED',
                    'last_complete':state.get('last_complete')},budget-remaining
            entry['stamp']=stamp
            with os.scandir(folder) as entries:
                exhausted=True
                for index,item in enumerate(entries):
                    if index<entry['offset']:continue
                    if remaining==0:exhausted=False;break
                    remaining-=1;entry['offset']=index+1
                    if item.is_symlink():continue
                    if item.is_dir(follow_symlinks=False):
                        state['queue'].append({'path':str(Path(item.path).relative_to(root)),'offset':0})
                    elif item.is_file(follow_symlinks=False):
                        state['bytes']+=item.stat(follow_symlinks=False).st_size;state['files']+=1
            if exhausted:state['queue'].pop(0)
        except FileNotFoundError:
            state['queue'].pop(0)
        except OSError as exc:
            state['error']=type(exc).__name__;break
    state['complete']=not state['queue'];state['sampled_at']=at
    return state,budget-remaining


def observe(ledger, *, at=None, ttl=300, max_entries=4096):
    import os
    import time
    from runtime_common import read_json,save_json,locked
    now=time.time() if at is None else at
    root=ledger.path.parent
    cache=root/'capacity-observation.json'
    def file_size(path):
        try:return path.stat().st_size
        except FileNotFoundError:return 0
    with locked(root/'capacity-observation.lock'):
        try:old=read_json(cache,{}) or {}
        except (OSError,ValueError):old={}
        if not isinstance(old,dict) or not isinstance(old.get('sampled_at',0),(int,float)):old={}
        if old and 0<=now-old['sampled_at']<ttl:return {**old,'cached':True,'sample_age_seconds':now-old['sampled_at']}
        if type(max_entries) is not int or max_entries<1:raise ValueError('INVALID_CAPACITY_SCAN_BUDGET')
        scan_path=root/'capacity-scan.json'
        try:scan=read_json(scan_path,{}) or {}
        except (OSError,ValueError):scan={}
        states=scan.get('roots',{});cursor=scan.get('cursor',0)%len(ATTACHMENT_ROOTS)
        remaining=max_entries;attachments={}
        for offset in range(len(ATTACHMENT_ROOTS)):
            name=ATTACHMENT_ROOTS[(cursor+offset)%len(ATTACHMENT_ROOTS)]
            quota=max(1,remaining//(len(ATTACHMENT_ROOTS)-offset)) if remaining else 0
            if quota:
                states[name],used=_advance(root/name,states.get(name),quota,now);remaining-=used
            state=states.get(name,{})
            sample=state if state.get('complete') else state.get('last_complete') or state
            attachments[name]={k:sample.get(k,0) for k in ('bytes','files')}
            attachments[name].update(complete=bool(state.get('complete') or state.get('last_complete')),
                refresh_in_progress=not state.get('complete',False),
                started_at=sample.get('started_at'),sampled_at=sample.get('sampled_at'),
                pending_directories=len(state.get('queue',[])),error=state.get('error'),
                observation='directory enumeration; concurrent changes may require another cycle')
        save_json(scan_path,{'version':1,'cursor':(cursor+1)%len(ATTACHMENT_ROOTS),'roots':states})
        complete=all(item['complete'] for item in attachments.values())
        database=file_size(ledger.path);wal=file_size(Path(str(ledger.path)+'-wal'))
        elapsed=now-old.get('sampled_at',now)
        previous=old.get('database_bytes',database)+old.get('wal_bytes',wal)
        value={'database_bytes':database,'wal_bytes':wal,'attachments':attachments,'attachments_complete':complete,
            'total_bytes_lower_bound':database+wal+sum(v['bytes'] for v in attachments.values()),
            'sampled_at':now,'cached':False,'sample_age_seconds':0,'scan_limit_entries':max_entries,
            'scan_entries_processed':max_entries-remaining,'scan_cursor_persisted':True,
            'database_wal_growth_bytes_per_second':(database+wal-previous)/elapsed if elapsed>0 and old else None,
            'state':'CAPACITY_WARNING' if database+wal>=2*1024**3 else 'OBSERVED',
            'retention':'不在线删除/VACUUM；一致归档须覆盖数据库、WAL和全部引用附件并验证还原。',
            'attachment_growth_bytes_per_second':None,'historical_compaction_performed':False}
        if complete and not any(v['refresh_in_progress'] for v in attachments.values()) and old.get('attachments_complete') and elapsed>0:
            value['attachment_growth_bytes_per_second']=(sum(v['bytes'] for v in attachments.values())-
                sum(v['bytes'] for v in old['attachments'].values()))/elapsed
        save_json(cache,value)
        return value
