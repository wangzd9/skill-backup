"""Archive frozen scopes only after native and descendant termination proof.

Missing historical rows remain UNKNOWN. Archive retains original scope bytes,
full native identity and observed termination proof; it never releases work.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time

import role_runtime as runtime


def process_inventory():
    rows = {}
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            fields = (proc/'stat').read_text().rsplit(') ', 1)[1].split()
            rows[int(proc.name)] = {'state': fields[0], 'process_start': fields[19],
                'pgrp': int(fields[2]), 'cgroup': (proc/'cgroup').read_text()}
        except (FileNotFoundError, ProcessLookupError):
            continue
        # Unreadable scope means the inventory is incomplete. Do not archive.
    return rows


def termination(scope, native, processes):
    if not native or native.get('id') != scope.get('execution_id') or native.get('job_id') != scope.get('job_id'):
        return None
    if native.get('status') not in ('completed', 'failed', 'unknown'):
        return None
    pid, start = native.get('pid'), native.get('process_started_at')
    if type(pid) is not int or pid <= 1 or not start:
        return None
    actual = processes.get(pid)
    if actual and actual['process_start'] == str(start) and actual['state'] not in ('Z', 'X'):
        return None
    writer = native.get('writer_scope')
    writer = json.loads(writer) if isinstance(writer, str) else writer
    if not isinstance(writer, dict) or writer.get('pid') != pid or str(writer.get('process_start')) != str(start):
        return None
    cgroup = writer.get('cgroup', '')
    unit = 'hermes-worker-cron-'+scope['job_id']+'-exec-'+scope['execution_id']+'.scope'
    paths = [line.split(':', 2)[2].rstrip('/') for line in cgroup.splitlines()
             if line.startswith('0::') and line.rstrip('/').endswith('/'+unit)]
    if not paths:
        return None
    for value in processes.values():
        if value['state'] in ('Z', 'X'):
            continue
        actual_paths = [line.split(':', 2)[2].rstrip('/') for line in value['cgroup'].splitlines()
                        if line.startswith('0::')]
        if any(path == parent or path.startswith(parent+'/') for parent in paths for path in actual_paths):
            return None
    return {'pid_start_exited': True, 'child_scopes_verified_empty': True,
            'writer_scope': writer, 'process_scan_sha256': hashlib.sha256(
                json.dumps(processes, sort_keys=True).encode()).hexdigest()}


def archive(home, pool_path, *, apply=False, min_age_seconds=7*86400, max_items=100, max_scan=4096, at=None):
    if min_age_seconds < 0 or max_items < 1 or max_scan < 1:
        raise ValueError('INVALID_RETENTION_BUDGET')
    home, pool = Path(home).resolve(), Path(pool_path).resolve()
    now = time.time() if at is None else at
    rows, results = {}, []
    source = home/'cron/executions.db'
    with runtime.locked(pool.with_suffix('.admission.lock')), runtime.locked(home/'runtime/scope-retention.lock'):
        with sqlite3.connect(source.as_uri()+'?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            rows = {r['id']: dict(r) for r in db.execute('SELECT * FROM executions')}
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='execution_history'").fetchone():
                for r in db.execute('SELECT execution_json FROM execution_history'):
                    value = json.loads(r[0]); rows.setdefault(value['id'], value)
        processes = process_inventory()
        closed = None
        for path in sorted((home/'runtime/role-pool-executions').glob('*.json'))[:max_scan]:
            if len(results) >= max_items:
                break
            raw = path.read_bytes(); scope = json.loads(raw)
            if scope.get('pool_path') != str(pool) or scope.get('kind') != 'business':
                continue
            if (not re.fullmatch('[a-f0-9]{32}', path.stem) or scope.get('execution_id') != path.stem
                    or now-path.stat().st_mtime < min_age_seconds):
                continue
            proof = termination(scope, rows.get(path.stem), processes)
            if proof is None:
                continue
            if scope.get('workflow_version'):
                if closed is None:
                    from role_observer import collect_pool_leases
                    observed = collect_pool_leases(pool, home=home)
                    closed = set(observed.get('closed_execution_ids', []))
                if path.stem not in closed:
                    continue
            receipt = {'schema_version': 1, 'execution_id': path.stem, 'checked_at': now,
                'pool_path': str(pool), 'generation': scope.get('generation'),
                'scope_sha256': hashlib.sha256(raw).hexdigest(), 'native_source': str(source),
                'native_execution': rows[path.stem], 'termination': proof}
            if apply:
                destination = home/'runtime/role-pool-execution-archive'/path.stem
                destination.mkdir(parents=True, exist_ok=True)
                heartbeat = home/'runtime/legacy-heartbeats'/(path.stem+'.json')
                heartbeat_raw = None
                if heartbeat.is_file():
                    observed_raw=heartbeat.read_bytes()
                    value = json.loads(observed_raw)
                    native = rows[path.stem]
                    if all(value.get(key)==expected for key,expected in {
                            'execution_id':path.stem,'pool_path':str(pool),'project_root':scope['project_root'],
                            'role':scope['role'],'pid':native['pid'],
                            'process_start':str(native['process_started_at'])}.items()):
                        heartbeat_raw=observed_raw
                        receipt['heartbeat']={'source':str(heartbeat),'archive':str(destination/'heartbeat.json'),
                            'sha256':hashlib.sha256(heartbeat_raw).hexdigest()}
                # Copy evidence before removing the live pointer. A crash leaves
                # a duplicate, never lost evidence; rerun verifies exact bytes.
                frozen = destination/'scope.json'
                if frozen.exists() and frozen.read_bytes() != raw:
                    raise ValueError('ARCHIVE_IDENTITY_CONFLICT')
                runtime.save(destination/'receipt.json', receipt)
                if heartbeat_raw is not None:
                    archived_heartbeat=destination/'heartbeat.json'
                    if archived_heartbeat.exists() and archived_heartbeat.read_bytes()!=heartbeat_raw:
                        raise ValueError('HEARTBEAT_ARCHIVE_IDENTITY_CONFLICT')
                    temporary=destination/'heartbeat.tmp'; temporary.write_bytes(heartbeat_raw); temporary.replace(archived_heartbeat)
                temporary = destination/'scope.tmp'; temporary.write_bytes(raw); temporary.replace(frozen)
                if path.read_bytes() != raw:
                    raise ValueError('SCOPE_CHANGED_DURING_ARCHIVE')
                path.unlink()
                if heartbeat_raw is not None and heartbeat.read_bytes()==heartbeat_raw:
                    heartbeat.unlink()
            results.append({'execution_id': path.stem, 'state': 'ARCHIVED' if apply else 'ELIGIBLE', 'proof': receipt})
    return {'schema_version': 1, 'apply': apply, 'items': results,
            'unknown_policy': 'retain; missing native or descendant proof never authorizes archive'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', required=True, type=Path)
    parser.add_argument('--pool', required=True, type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--min-age-seconds', type=float, default=7*86400)
    parser.add_argument('--max-items', type=int, default=100)
    args = parser.parse_args()
    print(json.dumps(archive(args.home,args.pool,apply=args.apply,
        min_age_seconds=args.min_age_seconds,max_items=args.max_items),ensure_ascii=False,indent=2))
