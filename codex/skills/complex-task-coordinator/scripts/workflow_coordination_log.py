"""Append one milestone to the selected requirement log, with crash-safe replay."""
import hashlib
import os
from pathlib import Path
import stat

from runtime_common import locked


def append(ledger, identity, content, *, command_id):
    from business_ledger import _deny
    digest = hashlib.sha256(command_id.encode()).hexdigest()
    begin = ('\n<!-- coordination-log:' + digest + ' -->\n').encode()
    end = ('\n<!-- /coordination-log:' + digest + ' -->\n').encode()
    if not isinstance(content, str) or not content.strip() or len(content.encode()) > 65536:
        raise ValueError('MILESTONE_CONTENT_REQUIRED')
    block = begin + content.encode() + end
    def operation(db):
        work, _ = ledger._identity(db, identity)
        if work['role'] != 'COORDINATOR':
            _deny('COORDINATOR_REQUIRED')
        run = ledger._one(db, 'runs', 'run_id', work['run_id'])
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
            {'path': str(path), 'content_sha256': hashlib.sha256(content.encode()).hexdigest()},
            run_id=work['run_id'], work_id=work['work_id'], role=work['role'])
        return {'path': str(path), 'event_id': event}
    return ledger._command('append_development_log', command_id, [identity, content], operation)
