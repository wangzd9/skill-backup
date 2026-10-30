"""Bounded, cached storage observations; never delete or compact audit sources."""
from pathlib import Path


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
        remaining=max_entries;attachments={};complete=True
        for name in ('input-pages','decision-views','query-views','delivery-evidence','decision-metrics','operations','services','mysql-sessions'):
            total=0;files=0;done=True;stack=[root/name]
            while stack and remaining:
                folder=stack.pop()
                if not folder.is_dir() or folder.is_symlink():continue
                with os.scandir(folder) as entries:
                    for item in entries:
                        if not remaining:done=False;break
                        remaining-=1
                        if item.is_symlink():continue
                        if item.is_dir(follow_symlinks=False):stack.append(Path(item.path))
                        elif item.is_file(follow_symlinks=False):total+=item.stat(follow_symlinks=False).st_size;files+=1
            done=done and not stack
            if not remaining:done=False
            attachments[name]={'bytes':total,'files':files,'complete':done};complete=complete and done
        database=file_size(ledger.path);wal=file_size(Path(str(ledger.path)+'-wal'))
        elapsed=now-old.get('sampled_at',now)
        previous=old.get('database_bytes',database)+old.get('wal_bytes',wal)
        value={'database_bytes':database,'wal_bytes':wal,'attachments':attachments,'attachments_complete':complete,
            'total_bytes_lower_bound':database+wal+sum(v['bytes'] for v in attachments.values()),
            'sampled_at':now,'cached':False,'sample_age_seconds':0,'scan_limit_entries':max_entries,
            'database_wal_growth_bytes_per_second':(database+wal-previous)/elapsed if elapsed>0 and old else None,
            'state':'CAPACITY_WARNING' if database+wal>=2*1024**3 else 'OBSERVED',
            'retention':'不在线删除/VACUUM；一致归档须覆盖数据库、WAL和全部引用附件并验证还原。',
            'attachment_growth_bytes_per_second':None,'historical_compaction_performed':False}
        if complete and old.get('attachments_complete') and elapsed>0:
            value['attachment_growth_bytes_per_second']=(sum(v['bytes'] for v in attachments.values())-
                sum(v['bytes'] for v in old['attachments'].values()))/elapsed
        save_json(cache,value)
        return value
