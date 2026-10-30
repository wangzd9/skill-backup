"""Immutable input pages: bounded transport and deduplicated command replay.

These files are part of the ledger's audit store, not disposable cache. Never
remove them by age; a command may refer to one forever. No read implies ACK.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile


def store(ledger, value):
    raw=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
    digest=hashlib.sha256(raw).hexdigest()
    folder=ledger.path.parent/'input-pages';folder.mkdir(mode=0o700,parents=True,exist_ok=True)
    path=folder/(digest+'.json')
    if not path.exists():
        descriptor,temporary=tempfile.mkstemp(dir=folder,prefix='.page-')
        try:
            with os.fdopen(descriptor,'wb') as stream:
                stream.write(raw);stream.flush();os.fsync(stream.fileno())
            os.chmod(temporary,0o600)
            os.replace(temporary,path)
            descriptor=os.open(folder,os.O_RDONLY)
            try:os.fsync(descriptor)
            finally:os.close(descriptor)
        finally:Path(temporary).unlink(missing_ok=True)
    if path.is_symlink() or path.read_bytes()!=raw:
        raise ValueError('INPUT_PAGE_CONTENT_CHANGED')
    return {'path':str(path),'sha256':digest,'bytes':len(raw)}


def read(ledger, reference):
    path=Path(reference['path'])
    if path!=ledger.path.parent/'input-pages'/(reference['sha256']+'.json') or path.is_symlink():
        raise ValueError('INPUT_PAGE_REFERENCE_INVALID')
    raw=path.read_bytes()
    if len(raw)!=reference['bytes'] or hashlib.sha256(raw).hexdigest()!=reference['sha256']:
        raise ValueError('INPUT_PAGE_CONTENT_CHANGED')
    return json.loads(raw)


def compact_result(ledger, result):
    if 'events' not in result:return result
    reference=store(ledger,result['events'])
    return {**{k:v for k,v in result.items() if k!='events'},'events_ref':reference}


def expand_result(ledger, result):
    if 'events_ref' not in result or 'events' in result:return result
    return {**result,'events':read(ledger,result['events_ref'])}


def bounded_events(ledger, rows, *, max_bytes=65536):
    """Oversize events keep their ID and a complete, immutable evidence ref."""
    result=[];size=2
    for row in rows:
        raw=json.dumps(row,ensure_ascii=False,sort_keys=True).encode()
        if len(raw)>max_bytes//2:
            row={**{k:v for k,v in row.items() if k!='payload'},'payload':{
                'projection':'full-event-reference-v1','full_event':store(ledger,row),
                'read_hint':'先读取完整事件再确认；此引用不代表已处理。'}}
            raw=json.dumps(row,ensure_ascii=False,sort_keys=True).encode()
        if size+len(raw)+1>max_bytes:break
        result.append(row);size+=len(raw)+1
    return result


def capacity(ledger):
    """Read-only inventory. This never vacuums, deletes or rewrites history."""
    with ledger._connection() as db:
        total=db.execute('PRAGMA page_count').fetchone()[0]*db.execute('PRAGMA page_size').fetchone()[0]
        commands=db.execute('SELECT COUNT(*),COALESCE(SUM(LENGTH(CAST(result AS BLOB))),0) FROM commands').fetchone()
    return {'database_bytes':total,'command_count':commands[0],'command_result_bytes':commands[1],
        'state':'CAPACITY_WARNING' if total>=2*1024**3 else 'OBSERVED',
        'retention':'Commands and referenced immutable pages retained; no automatic deletion.',
        'archive_required':total>=2*1024**3,'page_limit_bytes':65536}
