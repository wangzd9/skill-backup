#!/usr/bin/env python3
"""Small file protocol helper; no scheduler, model calls, Git changes or messaging."""
import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

ROLES = ('COORDINATOR', 'A', 'B', 'C', 'TESTER')


def now():
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec='seconds')


def load(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def atomic(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def save(path, value):
    atomic(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


@contextlib.contextmanager
def locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def pid_identity(pid):
    try:
        return Path(f'/proc/{int(pid)}/stat').read_text().rsplit(') ', 1)[1].split()[19]
    except (FileNotFoundError, ProcessLookupError):
        return None
    except (OSError, ValueError, IndexError):
        return 'UNVERIFIABLE'


def binding(project):
    root = Path(project).resolve()
    data = load(root / 'ROLE-BINDINGS.json')
    if not data or data.get('schema_version') != 1 or Path(data['project_root']).resolve() != root:
        raise ValueError('Invalid project binding; do not guess another project')
    return root, data


def legacy_busy(data, role):
    busy = []
    for row in data.get('legacy_runs', []):
        if row['role'] != role:
            continue
        if not row.get('pid'):
            if not row.get('stopped_verified'):
                busy.append(row)
            continue
        identity = pid_identity(row['pid'])
        if identity is None:
            continue
        if identity == 'UNVERIFIABLE' or not row.get('process_start') or identity == row.get('process_start'):
            busy.append(row)
    return busy


def guard(data, role):
    if data.get('control') != 'ACTIVE':
        raise ValueError('Project is not ACTIVE: no new business actions')
    if legacy_busy(data, role):
        raise ValueError('MIGRATION_WAIT: legacy owner still alive; do not take over')


def lease_path(root, role):
    return root / 'inbox/control/runtime/leases' / (role + '.json')


def lease_lock(root, role):
    # Keep the historical coordinator.lock inode for compatibility with in-flight owners.
    return root / 'inbox' / ('coordinator.lock' if role == 'COORDINATOR' else f'{role}.lock')


def valid_lease(root, role, token, allow_expired=False):
    lease = load(lease_path(root, role), {})
    if not token or lease.get('token') != token or (not allow_expired and lease.get('expires_at', 0) <= dt.datetime.now().timestamp()):
        raise ValueError('Missing, stale or expired writer token; recheck owner before continuing')
    return lease


def execute(args):
    if args.action == 'resolve':
        registry = load(Path(args.registry), {'projects': []})
        matches = []
        for entry in registry['projects']:
            root, data = binding(entry['project_root'])
            for role, item in data['roles'].items():
                if (not args.chat_id or item.get('chat_id') == args.chat_id) and (not args.role or role == args.role):
                    matches.append({'project_root': str(root), 'role': role, **item})
        if len(matches) != 1:
            raise ValueError(f'AMBIGUOUS_BINDING: {len(matches)} matches; ask for project/role')
        return matches[0]

    root, data = binding(args.project)
    role = args.role
    if role not in data['roles']:
        raise ValueError('Role not registered')
    if args.action == 'preflight':
        stamp = {'checked_at': now(), 'role': role, 'mode': args.mode,
                 'control': data.get('control'), 'legacy_busy': legacy_busy(data, role)}
        with locked(root / f'inbox/control/runtime/{role}-tick.lock'):
            save(root / f'inbox/control/runtime/ticks/{role}.json', stamp)
        stamp.update({'skill': data['roles'][role]['skill'], 'session_file': data['roles'][role]['session_file']})
        return stamp
    if args.action == 'status':
        return {'role': role, 'legacy_busy': legacy_busy(data, role),
                'lease': {k: v for k, v in load(lease_path(root, role), {}).items() if k != 'token'},
                'last_tick': load(root / f'inbox/control/runtime/ticks/{role}.json', {})}
    if args.action == 'recover':
        guard(data, role)
        with locked(lease_lock(root, role)):
            old = load(lease_path(root, role), {})
            if not old or old.get('owner') != args.owner:
                raise ValueError('Recovery owner does not match; recheck status')
            if old.get('expires_at', 0) > dt.datetime.now().timestamp():
                raise ValueError('Lease not expired; no forced takeover')
            identity = pid_identity(old['pid']) if old.get('pid') else None
            if old.get('pid') and identity is not None and (identity == 'UNVERIFIABLE' or not old.get('process_start') or identity == old['process_start']):
                raise ValueError('Owner process is still alive; no takeover')
            if not args.confirmed_stopped or not args.evidence or not Path(args.evidence).is_file():
                raise ValueError('Recovery needs confirmed stopped owner and an existing verification evidence file')
            record = {'recovered_at': now(), 'role': role, 'old_owner': args.owner,
                      'verification_evidence': str(Path(args.evidence).resolve()),
                      'lease': {k: v for k, v in old.items() if k != 'token'}}
            save(root / 'inbox/control/runtime/recoveries' / (uuid.uuid4().hex + '.json'), record)
            lease_path(root, role).unlink()
            return {'recovered': True, 'evidence': record['verification_evidence']}
    if args.action == 'claim':
        guard(data, role)
        with locked(lease_lock(root, role)):
            old = load(lease_path(root, role), {})
            if old:
                # Expiration alone never proves that an owner has stopped writing.
                identity = pid_identity(old['pid']) if old.get('pid') else None
                proven_dead = old.get('pid') and old.get('process_start') and identity != 'UNVERIFIABLE' and identity != old['process_start']
                if old.get('expires_at', 0) > dt.datetime.now().timestamp() or not proven_dead:
                    return {'claimed': False, 'reason': 'OWNER_BUSY_OR_UNVERIFIED',
                            'owner': old.get('owner'), 'expires_at': old.get('expires_at')}
            if args.pid and not pid_identity(args.pid):
                raise ValueError('Owner PID is not alive')
            lease = {'owner': args.owner, 'token': uuid.uuid4().hex, 'claimed_at': now(),
                     'expires_at': dt.datetime.now().timestamp() + args.ttl,
                     'pid': args.pid, 'process_start': pid_identity(args.pid) if args.pid else None}
            save(lease_path(root, role), lease)
            return {'claimed': True, **lease}
    if args.action in ('renew', 'release', 'run', 'event', 'timeline'):
        with locked(lease_lock(root, role)):
            if args.action != 'release':
                guard(data, role)
            # The same owner may release or renew an expired token if no takeover replaced it.
            lease = valid_lease(root, role, args.token, allow_expired=args.action in ('release', 'renew'))
            if args.action == 'renew':
                lease['expires_at'] = dt.datetime.now().timestamp() + args.ttl
                save(lease_path(root, role), lease)
                return {'renewed': True, 'expires_at': lease['expires_at']}
            if args.action == 'release':
                lease_path(root, role).unlink()
                return {'released': True}
            if args.action == 'run':
                command = args.command[1:] if args.command[:1] == ['--'] else args.command
                if not command:
                    raise ValueError('Missing command')
                # Only short control mutations belong here. Long OCR/tests keep separate locks.
                return subprocess.run(command, cwd=root, timeout=min(args.timeout, 90)).returncode
            if args.action == 'event':
                if args.occurred_at:
                    if not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d(?:\.\d+)?)?(?:Z|[+-]\d\d:\d\d)', args.occurred_at):
                        raise ValueError('occurred_at needs date, known minutes and timezone; unknown history uses runtime_tools role-record')
                    dt.datetime.fromisoformat(args.occurred_at.replace('Z', '+00:00'))
                row = {'event_id': args.event_id, 'occurred_at': args.occurred_at or now(),
                       'recorded_at': now(), 'role': role, 'task': args.task, 'attempt': args.attempt,
                       'status': args.status, 'summary': args.summary, 'evidence': args.evidence, 'next': args.next}
                path = root / 'events' / role / (hashlib.sha256(args.event_id.encode()).hexdigest() + '.json')
                old = load(path)
                if old:
                    # Retrying an ID is safe, but may not silently replace another event.
                    fields = ('role', 'task', 'attempt', 'status', 'summary', 'evidence', 'next')
                    if any(old[k] != row[k] for k in fields):
                        raise ValueError('EVENT_ID_CONFLICT')
                    return {'created': False, 'path': str(path)}
                save(path, row)
                return {'created': True, 'path': str(path)}
            if role != 'COORDINATOR':
                raise ValueError('Only coordinator can write TIMELINE')
            target = root / 'TIMELINE.md'
            text = target.read_text() if target.exists() else '# 项目时间线\n'
            rows = [_event_row(p) for p in (root / 'events').glob('*/*.json')]
            rows = [row for row in rows if row]
            added = 0
            for row in sorted(rows, key=lambda r: (r.get('occurred_at') or r.get('recorded_at') or '', r['event_id'])):
                marker = '<!-- role-event:' + hashlib.sha256(row['event_id'].encode()).hexdigest() + ' -->'
                if marker in text:
                    continue
                clean = lambda value: str(value).replace('\n', ' ').replace('\r', ' ')
                text += '\n' + marker + '\n- ' + clean(row.get('occurred_at') or '发生时间未知') + ' ' + clean(row['role'])
                text += ' ' + clean(row['task']) + '/' + clean(row['attempt']) + ' ' + clean(row['status'])
                text += '：' + clean(row['summary']) + '；证据：' + clean(row['evidence'])
                text += '；下一步：' + clean(row['next']) + '；补录：' + clean(row['recorded_at']) + '\n'
                added += 1
            atomic(target, text)
            return {'appended': added, 'path': str(target)}
    raise ValueError('Unknown action')


def _event_row(p):
    """Tolerant load: legacy events lack occurred_at/recorded_at; bad JSON is skipped."""
    try:
        row = load(p)
    except Exception:
        return None
    if not isinstance(row, dict) or 'event_id' not in row:
        return None
    row.setdefault('occurred_at', row.get('checked_at') or row.get('time') or row.get('ts') or '')
    row.setdefault('recorded_at', '')
    row.setdefault('role', '')
    row.setdefault('task', 'none')
    row.setdefault('attempt', 'none')
    row.setdefault('status', '')
    row.setdefault('summary', '')
    row.setdefault('evidence', '')
    row.setdefault('next', '')
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['resolve', 'preflight', 'status', 'claim', 'renew', 'release', 'recover', 'run', 'event', 'timeline'])
    parser.add_argument('--project')
    parser.add_argument('--role', choices=ROLES)
    parser.add_argument('--registry', default='/home/wang/.hermes/role-projects.json')
    parser.add_argument('--chat-id')
    parser.add_argument('--mode', choices=['poll', 'report'], default='poll')
    parser.add_argument('--owner')
    parser.add_argument('--pid', type=int)
    parser.add_argument('--token')
    parser.add_argument('--ttl', type=int, default=300)
    parser.add_argument('--timeout', type=int, default=60)
    parser.add_argument('--confirmed-stopped', action='store_true')
    for key in ['event-id', 'occurred-at', 'task', 'attempt', 'status', 'summary', 'evidence', 'next']:
        parser.add_argument('--' + key)
    raw = sys.argv[1:]
    split = raw.index('--') if '--' in raw else len(raw)
    args = parser.parse_args(raw[:split])
    args.command = raw[split + 1:] if split < len(raw) else []
    if args.command and args.action != 'run':
        parser.error('Only run accepts a command after --')
    if args.action != 'resolve' and (not args.project or not args.role):
        parser.error('--project and --role are required')
    if args.action == 'claim' and (not args.owner or not 30 <= args.ttl <= 600):
        parser.error('claim needs --owner and ttl between 30 and 600 seconds')
    if args.action == 'renew' and not 30 <= args.ttl <= 600:
        parser.error('ttl between 30 and 600 seconds')
    if args.action == 'event' and not all(getattr(args, k) for k in ['event_id','task','attempt','status','summary','evidence','next']):
        parser.error('event identity, summary, evidence and next are required; use explicit none values')
    try:
        result = execute(args)
        if isinstance(result, int):
            return result
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
