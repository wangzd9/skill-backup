"""Inspect already-rejected service metadata; never reject or dispatch an action."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
HOME = Path('/home/wang/.hermes')
REPO = Path('/home/wang/work/hermes-role-runtime')


def main():
    sys.path.insert(0, str(REPO))
    from workflow_service_recovery import rejection_proof
    source = max((HOME/'cache/role-lease-monitor').glob('*/snapshot.json'), key=lambda p: p.stat().st_mtime)
    health = json.loads(source.read_text())
    ids = sorted({r['action_id'] for r in health.get('faults', [])
        if r.get('type') == 'observer_terminal_service_evidence_unverified'})
    path = HOME/'role-pool.runtime/business.sqlite3'
    db = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=3)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    db.execute('BEGIN')
    rows = []
    for action_id in ids:
        row = db.execute('SELECT a.action_id,a.state,a.evidence,c.run_id,c.task_id,c.attempt,c.state candidate_state '
            'FROM external_actions a JOIN candidates c USING(candidate_id) WHERE action_id=?', (action_id,)).fetchone()
        if row is None:
            rows.append({'action_id': action_id, 'state': 'MISSING'})
            continue
        item = dict(row)
        item['has_terminal_evidence'] = bool(json.loads(item.pop('evidence') or 'null'))
        directory = HOME/'role-pool.runtime/services'/hashlib.sha256(action_id.encode()).hexdigest()
        sidecar = directory/'invocation.json'
        try:
            raw = sidecar.read_bytes()
            invocation = json.loads(raw)
            item.update(sidecar_identity_matches=invocation.get('action_id') == action_id,
                zero_dispatch_proof_currently_valid=rejection_proof(invocation, directory),
                source_sha256=hashlib.sha256(raw).hexdigest(), source_unchanged=sidecar.read_bytes() == raw)
        except (OSError, ValueError, TypeError) as exc:
            item.update(zero_dispatch_proof_currently_valid=False, source_error=type(exc).__name__)
        rows.append(item)
    db.rollback()
    db.close()
    out = {'sampled_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds'),
        'health_sample_at': health.get('at'),
        'scope': 'Read-only existing rejection_proof on already-REJECTED metadata. Ledger/observer/files are separate samples. No command, verdict or dispatch.',
        'proof_code_sha256': hashlib.sha256((REPO/'workflow_service_recovery.py').read_bytes()).hexdigest(),
        'rows': rows}
    (ROOT/'terminal-rejection-proof-summary.json').write_text(json.dumps(out, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({'sampled_at':out['sampled_at'],'rejected':sum(r['state']=='REJECTED' for r in rows),
        'zero_dispatch_proof_valid':sum(r.get('zero_dispatch_proof_currently_valid',False)
            and r.get('sidecar_identity_matches',False) and r.get('source_unchanged',False) for r in rows)},ensure_ascii=False))


if __name__ == '__main__':
    main()
