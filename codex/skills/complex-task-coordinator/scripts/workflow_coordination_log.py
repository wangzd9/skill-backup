"""Append one milestone to the selected requirement log, with crash-safe replay."""
import hashlib
import os
from pathlib import Path
import stat

from runtime_common import locked


def append(ledger, identity, content, *, command_id, source_event_ids=None):
    from business_ledger import _deny
    digest = hashlib.sha256(command_id.encode()).hexdigest()
    begin = ('\n<!-- coordination-log:' + digest + ' -->\n').encode()
    end = ('\n<!-- /coordination-log:' + digest + ' -->\n').encode()
    if not isinstance(content, str) or not content.strip() or len(content.encode()) > 65536:
        raise ValueError('MILESTONE_CONTENT_REQUIRED')
    refs=[] if source_event_ids is None else source_event_ids
    if (not isinstance(refs,list) or len(refs)>32 or any(type(e) is not int or e<1 for e in refs)
            or len(set(refs))!=len(refs)):
        raise ValueError('INVALID_LOG_SOURCE_EVENTS')
    def operation(db):
        work, _ = ledger._identity(db, identity)
        if work['role'] != 'COORDINATOR':
            _deny('COORDINATOR_REQUIRED')
        run = ledger._one(db, 'runs', 'run_id', work['run_id'])
        facts=[]
        from business_ledger import _row, _json
        for event_id in refs:
            row=_row(db.execute('SELECT * FROM events WHERE event_id=? AND run_id=?',
                (event_id,work['run_id'])).fetchone())
            if row is None:_deny('LOG_SOURCE_EVENT_OUTSIDE_RUN_OR_MISSING')
            facts.append({'event_id':event_id,'kind':row['kind'],'created_at':row['created_at'],
                'role':row['role'],'work_id':row['work_id'],
                'fixed_facts':{k:v for k,v in row['payload'].items() if isinstance(v,(str,int,float,bool,type(None))) and k in
                    ('candidate_id','action_id','verification_id','head','ac_version','result','decision')}})
        suffix=('\n\n账本事实引用（历史事件，实时状态须重查）：\n```json\n'+_json(facts)+'\n```') if facts else ''
        block = begin + (content+suffix).encode() + end
        root = Path(run['requirement_root']).resolve()
        path = root / 'DEVELOPMENT-LOG.md'
        # No model path, arbitrary file writes, shell, or symlink traversal.
        with locked(ledger.path.parent / ('coordination-log-' + hashlib.sha256(str(root).encode()).hexdigest() + '.lock')):
            try:
                fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o644)
            except OSError:
                _deny('DEVELOPMENT_LOG_UNAVAILABLE')
            with os.fdopen(fd, 'r+b') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    _deny('DEVELOPMENT_LOG_NOT_REGULAR')
                existing = stream.read()
                if begin in existing:
                    if block not in existing:
                        _deny('DEVELOPMENT_LOG_PARTIAL_OR_CONFLICT', unknown=True)
                else:
                    stream.write(block)
                    stream.flush()
                    os.fsync(stream.fileno())
        event = ledger._event(db, 'DEVELOPMENT_LOG_APPENDED',
            {'path': str(path), 'content_sha256': hashlib.sha256(content.encode()).hexdigest(),
             **({'source_event_ids':refs} if refs else {})},
            run_id=work['run_id'], work_id=work['work_id'], role=work['role'])
        return {'path': str(path), 'event_id': event}
    return ledger._command('append_development_log', command_id, [identity, content,*([refs] if refs else [])], operation)
