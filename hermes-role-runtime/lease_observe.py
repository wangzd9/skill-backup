#!/usr/bin/env python3
"""Read real pool/frozen roots and persist stable mechanical monitor receipts.

No model, transport, reconciliation or lease writes. Timestamps and ages stay
in JSONL; stdout contains only discrete source/lease facts.
"""
import argparse
import json
import os
from pathlib import Path
import sys

source_root = Path(__file__).resolve().parent
if not (source_root/'role_observer.py').is_file():
    # Cron requires a regular entry inside its profile's scripts directory.
    # The installed plugin is the explicit canonical runtime dependency.
    source_root = source_root.parent/'plugins/role-workflow-runtime'
if not (source_root/'role_observer.py').is_file():
    raise RuntimeError('ROLE_RUNTIME_INSTALLATION_REQUIRED')
sys.path.insert(0, str(source_root.resolve()))
import role_observer as observer


def main():
    home = Path(os.environ.get('HERMES_HOME') or Path.home()/'.hermes')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=home)
    parser.add_argument('--pool', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--state-dir', type=Path)
    args = parser.parse_args()
    result = observer.observe_pool_leases(args.pool or args.home/'role-pool.json',
                                          home=args.home, state_dir=args.state_dir)
    # Full current facts and every anomaly transition are already persisted by
    # observe_pool_leases. Keep periodic sampling logs bounded and mechanical.
    import logging
    from logging.handlers import RotatingFileHandler
    from collections import Counter
    output = args.output or args.home/'cache/scratch/lease_observe-summary.jsonl'
    output.parent.mkdir(parents=True, exist_ok=True)
    summary = {'schema_version':2,'at':result['at'],'pool_path':result['pool_path'],
        'health':result['health'],'fault_counts':dict(Counter(row['type'] for row in result['faults'])),
        'transition_count':len(result['transitions']), 'outbox':result['outbox'],
        'full_snapshot':str(Path(result['outbox']).parent/'snapshot.json'),
        'roles':{role:[{key:row.get(key) for key in ('project_root','state','expires_at','owner_process_state')}
                       for row in rows] for role,rows in result['roles'].items()}}
    with observer.runtime.locked(output.with_suffix('.lock')):
        handler=RotatingFileHandler(output,maxBytes=2*1024*1024,backupCount=3,encoding='utf-8')
        try:
            handler.setFormatter(logging.Formatter('%(message)s'))
            handler.emit(logging.LogRecord('lease_observe',logging.INFO,__file__,0,
                         json.dumps(summary,ensure_ascii=False),(),None))
            output.chmod(0o600)
        finally:
            handler.close()
    lines = []
    for role in observer.ROLES:
        values = result['roles'][role]
        if not values:
            lines.append(role+'=unverified')
        for value in values:
            work = value.get('current_work') or {}
            source = value.get('project_root') or work.get('run_id') or result.get('authority_source', 'unverified')
            lines.append(role+'='+str(source)+'/'+value['state']+'/'+
                         str(value.get('binding_type', value.get('binding_state', 'unverified'))))
    from collections import Counter
    counts=Counter(row['type'] for row in result['faults'])
    lines.extend('MONITOR='+kind+'/count='+str(count) for kind,count in sorted(counts.items()))
    if result['faults']:
        lines.append('MONITOR_SNAPSHOT='+str(Path(result['outbox']).parent/'snapshot.json'))
    print('\n'.join(lines))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
