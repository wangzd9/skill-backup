#!/usr/bin/env python3
"""Document observer / bounded report projection. No model, business writes or messaging."""
import argparse
import hashlib
import json
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import role_runtime as runtime
import report_gate
import activity_report

ROLES = runtime.ROLES
LABELS = {'COORDINATOR': '协调者', **{k: v[1] for k, v in report_gate.ROLES.items()}}
MARKERS = {'A': ('1号','执行者A','执行者 A'), 'B': ('2号','执行者B','执行者 B'), 'C': ('3号','执行者C','执行者 C'), 'TESTER': ('TESTER','测试者')}
ROOT_FIELDS = ('TODO.md', 'inbox/PROTOCOL.md')


def stamp():
    return datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')


def short(value, limit=220):
    value = re.sub(r'\s+', ' ', str(value or '')).strip()
    return value if len(value) <= limit else value[:limit//2] + ' … ' + value[-limit//2:]


def plain(value, limit=220):
    return short(value, limit).replace('`', "'").replace('*', '').replace('<', '‹').replace('>', '›')


def text_read(path):
    # Fail closed on unstable writes. This observer never repairs a business document.
    before = path.stat()
    body = path.read_text()
    after = path.stat()
    if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
        raise ValueError('source changed during read: ' + str(path))
    if not body.strip():
        raise ValueError('empty source: ' + str(path))
    return body


def rows(body):
    found = report_gate.task_rows(body)
    if not found:
        raise ValueError('unrecognized TODO task table')
    return found


def event_info(path, root, include_observation=False):
    body = text_read(path)
    if path.suffix == '.json':
        row = json.loads(body)
        if not isinstance(row, dict):
            raise ValueError('invalid event: ' + str(path))
    else:
        row = {}
        for key in ('event_id', 'occurred_at', 'recorded_at', 'task', 'attempt', 'status', 'summary', 'evidence', 'next'):
            m = re.search(r'^\s*-?\s*(?:\*\*)?' + key + r'(?:\*\*)?\s*[:：]\s*(.*)$', body, re.M | re.I)
            if m:
                row[key] = m[1].strip()
        # Legacy event formats stay as evidence, never as an accepted business status.
        row.setdefault('summary', short(body, 320))
        # Unknown Markdown context is still input. Identical mirrors share this
        # digest, but changes outside the parsed fields must remain observable.
        row['raw_body_sha256'] = hashlib.sha256(body.encode()).hexdigest()
    # A free-text "no change" substring is not authority to discard an event.
    # Explicit legacy observation statuses remain supported; unclassified events
    # (including OBSERVING / IN_PROGRESS) remain business input unless registered.
    quiet_status = row.get('status', '').upper() in ('IDLE_POLL', 'NO_CHANGE', 'POLL_NO_CHANGE')
    if (path.suffix == '.json' and quiet_status and row.get('business_change') is not True
            and str(row.get('kind', '')).lower() != 'business' and not include_observation):
        return None
    row['source'] = str(path.relative_to(root))
    row['source_mtime'] = datetime.fromtimestamp(path.stat().st_mtime, ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')
    return row


def event_payload(row):
    """Delivery path, observation time and mirror metadata are not business identity."""
    return {k: v for k, v in row.items() if k not in ('source', 'source_mtime', 'recorded_at')}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def event_order(row):
    def aware(value):
        try:
            parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            return parsed.timestamp() if parsed.tzinfo is not None else None
        except (ValueError, TypeError, OverflowError):
            return None
    occurred, source = aware(row.get('occurred_at')), aware(row.get('source_mtime'))
    row['event_time_basis'] = 'occurred_at' if occurred is not None else 'source_mtime'
    row['event_time_status'] = 'known' if occurred is not None else 'unknown'
    # mtime is only a fallback ordering fact; never replace occurred_at with it.
    return (occurred if occurred is not None else source if source is not None else float('-inf'),
            source if source is not None else float('-inf'), row.get('source', ''))


def observation_rules(root, role):
    """Exact, opt-in legacy observation exclusions; unknown/tampered data stays visible."""
    record = runtime.load(root/f'inbox/control/runtime/observation-index/{role}.json', {}) or {}
    if not record:
        return []
    if record.get('schema_version') != 1 or not isinstance(record.get('entries'), list):
        raise ValueError('unrecognized observation index for ' + role)
    return record['entries']


def session_fingerprint(body, source, rules):
    raw = body.encode()
    spans = []
    for entry in rules:
        if entry.get('kind') != 'session_block' or entry.get('source') != source:
            continue
        start, length = entry.get('offset'), entry.get('length')
        if type(start) is not int or type(length) is not int or start < 0 or length <= 0:
            raise ValueError('invalid registered observation span')
        end = start + length
        if end <= len(raw) and hashlib.sha256(raw[start:end]).hexdigest() == entry.get('sha256'):
            spans.append((start, end))
    # A modified observation is automatically business input again. Never guess
    # its location after edits or strip similar text in another part of a file.
    previous, pieces = 0, []
    for start, end in sorted(spans):
        if start < previous:
            raise ValueError('overlapping observation spans')
        pieces.append(raw[previous:start]); previous = end
    pieces.append(raw[previous:])
    return hashlib.sha256(b''.join(pieces)).hexdigest()


def registered_event_payloads(root, rules):
    ignored = set()
    for entry in rules:
        if entry.get('kind') != 'event':
            continue
        path = (root/entry['source']).resolve()
        if not path.is_relative_to(root):
            raise ValueError('registered observation outside project')
        if not path.exists():
            continue
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() == entry.get('sha256'):
            # Hash and parse the very same byte snapshot. Reading the path again
            # here could accidentally exclude a concurrent new BLOCKED event.
            row = json.loads(raw)
            if not isinstance(row, dict):
                raise ValueError('invalid registered event')
            ignored.add(digest(event_payload(row)))
    return ignored


def register_observations(root, role, manifest, token):
    """Register only explicitly reviewed, byte-exact legacy read-only outputs.

    This is not an ACK, cannot register role inbox/decisions, and never changes a
    business document. The caller must classify the selected material; no text
    heuristics or blanket session exclusions are accepted.
    """
    root, binding = runtime.binding(root)
    if role not in binding['roles']:
        raise ValueError('role not bound')
    if (manifest.get('schema_version') != 1 or manifest.get('classification') != 'READ_ONLY_OBSERVATION'
            or manifest.get('business_change') is not False or not isinstance(manifest.get('entries'), list)
            or not manifest['entries']):
        raise ValueError('explicit READ_ONLY_OBSERVATION manifest is required')
    with runtime.locked(runtime.lease_lock(root, role)):
        runtime.guard(binding, role)
        runtime.valid_lease(root, role, token)
        with runtime.locked(root/f'inbox/control/runtime/{role}-observer.lock'):
            allowed_session = (root/binding['roles'][role]['session_file']).resolve()
            entries = []
            for entry in manifest['entries']:
                source = entry.get('source')
                if not isinstance(source, str):
                    raise ValueError('observation source is required')
                path = (root/source).resolve()
                if not path.is_relative_to(root) or path.is_symlink():
                    raise ValueError('observation source outside project')
                canonical = str(path.relative_to(root))
                if source != canonical:
                    raise ValueError('observation source must be canonical')
                raw = path.read_bytes()
                if entry.get('kind') == 'session_block':
                    if path != allowed_session:
                        raise ValueError('only the bound role session may be registered')
                    start, length = entry.get('offset'), entry.get('length')
                    if type(start) is not int or type(length) is not int or start < 0 or length <= 0 or start+length > len(raw):
                        raise ValueError('invalid observation span')
                    if start == 0 and length == len(raw):
                        raise ValueError('blanket session exclusion is not allowed')
                    end = start+length
                    if (start and raw[start-1:start] != b'\n') or (end != len(raw) and raw[end-1:end] != b'\n'):
                        raise ValueError('observation span must contain complete lines')
                    selected = raw[start:end]
                    selected.decode('utf-8')
                    accepted = {k: entry[k] for k in ('kind', 'source', 'offset', 'length', 'sha256')}
                elif entry.get('kind') == 'event':
                    if path.parent != root/'events'/role or path.suffix != '.json':
                        raise ValueError('only canonical role JSON events may be registered')
                    row = json.loads(raw)
                    if not isinstance(row, dict) or not row.get('event_id') or row.get('role') != role:
                        raise ValueError('event identity does not match role')
                    if str(row.get('kind', '')).lower() == 'business' or row.get('business_change') is True:
                        raise ValueError('explicit business event cannot be registered as an observation')
                    selected = raw
                    accepted = {k: entry[k] for k in ('kind', 'source', 'sha256')}
                else:
                    raise ValueError('unsupported observation kind')
                if hashlib.sha256(selected).hexdigest() != entry.get('sha256'):
                    raise ValueError('OBSERVATION_CONTENT_CHANGED: re-review exact material')
                entries.append(accepted)
            existing = observation_rules(root, role)
            merged = {digest(item): item for item in existing + entries}
            runtime.save(root/f'inbox/control/runtime/observation-index/{role}.json',
                         {'schema_version': 1, 'role': role, 'registered_at': stamp(), 'entries': list(merged.values())})
            return {'role': role, 'registered': len(entries), 'total': len(merged), 'acknowledged': False}


def process_health(lease, identity=None):
    identity = identity or runtime.pid_identity
    if not lease.get('owner'):
        return {'state': 'none'}
    pid, start = lease.get('pid'), lease.get('process_start')
    if not pid or not start:
        return {'state': 'unverified', 'owner': lease['owner']}
    actual = identity(pid)
    state = 'unverified' if actual == 'UNVERIFIABLE' else ('alive' if actual == start else 'exited_or_reused')
    return {'state': state, 'pid': pid, 'start': start, 'owner': lease['owner']}


def collect(root, role):
    root, binding = runtime.binding(root)
    if role not in binding['roles']:
        raise ValueError('role not bound')
    todo = text_read(root/'TODO.md')
    taskrows = rows(todo)
    from progress_projection import _owner_role
    control = binding.get('control')
    idle, reasons = (False, []) if role == 'COORDINATOR' else report_gate.routing(root, role)
    selected = [role] if role != 'COORDINATOR' else list(ROLES)
    watch = {}
    def watchfile(p):
        body = text_read(p)
        watch[str(p.relative_to(root))] = hashlib.sha256(body.encode()).hexdigest()
        return body
    # 仅明确业务待办进入队列；群/poll共用角色租约。工程操作仅看结果和进程身份。
    from runtime_tools import pending_requests, operation_states
    requests = pending_requests(root) if role == 'COORDINATOR' else []
    for request in requests:
        watchfile(Path(request['source']))
    operations = []
    for who in (ROLES if role == 'COORDINATOR' else (role,)):
        for operation in operation_states(root, who):
            source = str(Path(operation['source']).relative_to(root))
            watch[source] = hashlib.sha256(json.dumps(operation, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            operations.append({k: operation.get(k) for k in ('operation_id','role','task','attempt','status','exit_code','source','log','reason')})
    task_observations = []
    notice_folder = root/'inbox/control/runtime/task-notices'
    for notice_path in sorted(notice_folder.glob('*.json')):
        body = text_read(notice_path); notice = json.loads(body)
        if not isinstance(notice, dict) or notice.get('schema_version') != 1 or not isinstance(notice.get('notify_roles'), list):
            raise ValueError('unrecognized task observation notice')
        identity = notice.get('observation_id')
        if not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', identity):
            raise ValueError('invalid task observation identity')
        if role not in notice['notify_roles']:
            continue
        watch[str(notice_path.relative_to(root))] = hashlib.sha256(body.encode()).hexdigest()
        latest_path = root/f'inbox/control/runtime/task-observations/{identity}/latest.json'
        latest = json.loads(text_read(latest_path)) if latest_path.exists() else notice
        # Fresh evidence is available for restoration/reporting; its ordinary
        # heartbeat / CPU / mtime changes do not enter the business fingerprint.
        task_observations.append({k: latest.get(k) for k in ('observation_id','task','run_id','attempt','observed_at',
                                 'revision','health','heartbeat','semantic','scope')})
        task_observations[-1]['source'] = str(latest_path.relative_to(root))
    # Full hashes do not truncate critical changes; the model receives only bounded locations.
    watchfile(root/'TODO.md')
    if role != 'COORDINATOR':
        own_rows = {k: v for k, v in taskrows.items() if _owner_role(v[-1]) == role or any(m in v[-1] for m in MARKERS[role])}
        watch['TODO.md'] = hashlib.sha256(json.dumps(own_rows, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if (root/'inbox/PROTOCOL.md').exists():
        watchfile(root/'inbox/PROTOCOL.md')
    events, dispatches, health, owned_tasks = [], [], {}, set()
    event_conflicts = []
    for who in selected:
        item = binding['roles'][who]
        exclusions = observation_rules(root, who)
        ignored_events = registered_event_payloads(root, exclusions)
        health[who] = process_health(runtime.load(runtime.lease_path(root, who), {}) or {})
        # Follow the canonical role document from the binding, not stale mirrors.
        session = (root / item['session_file']).resolve()
        if not session.is_relative_to(root):
            raise ValueError('session document outside project')
        if session.exists():
            source = str(session.relative_to(root))
            watch[source] = session_fingerprint(text_read(session), source, exclusions)
        folder = root/'inbox'/('coordinator' if who == 'COORDINATOR' else report_gate.ROLES[who][0])
        if folder.exists():
            for p in folder.rglob('*.md'):
                body = watchfile(p)
                from progress_projection import TASK_PATTERN
                owned_tasks.update(re.findall(TASK_PATTERN, str(p.relative_to(root)) + '\n' + body[:1800]))
                if re.search(r'ack', p.stem, re.I):
                    continue
                if re.search(r'dispatch|rework', p.stem, re.I) or re.search(r'^#.*(?:派单|返修)', body[:1800], re.M):
                    m = re.search(r'^\s*-?\s*(?:\*\*)?status(?:\*\*)?\s*[:：]\s*(\w+)', body, re.M | re.I)
                    dispatches.append({'role': who, 'source': str(p.relative_to(root)), 'status': m[1] if m else '待核对',
                                       'mtime': p.stat().st_mtime, 'summary': short(body[:1400], 220)})
        grouped_events = {}
        for folder in (root/'events'/who, root/'inbox/events'/who):
            if folder.exists():
                for p in folder.iterdir():
                    if p.suffix not in ('.md', '.json') or not p.is_file():
                        continue
                    row = event_info(p, root)
                    if row:
                        row['role'] = who
                        identity = str(row.get('event_id') or row['source'])
                        payload_hash = digest(event_payload(row))
                        variants = grouped_events.setdefault(identity, {})
                        if payload_hash in variants:
                            variants[payload_hash]['sources'].append(row['source'])
                        else:
                            variants[payload_hash] = {**row, 'sources': [row['source']]}
        for identity, variants in grouped_events.items():
            # Identical canonical/inbox mirrors are one business event. A changed
            # payload under the same ID is a conflict, never an overwrite or a
            # reason to trust the observation exclusion.
            if len(variants) > 1:
                event_conflicts.append({'role': who, 'event_id': identity,
                                        'sources': sorted(s for row in variants.values() for s in row['sources'])})
            active = {key: row for key, row in variants.items()
                      if len(variants) > 1 or key not in ignored_events}
            if active:
                watch['event:' + who + ':' + identity] = digest(sorted(active))
                events.extend(active.values())
    # Acceptance / receipts / OCR and strong results must wake the coordinator, not only a summary event.
    folders = ['decision', 'inbox/coordinator', 'receipts', 'test-receipts', 'reviews'] if role == 'COORDINATOR' else ['decision', 'inbox/coordinator']
    for folder in folders:
        if (root/folder).exists():
            for p in (root/folder).rglob('*.md'):
                if role == 'COORDINATOR':
                    watchfile(p)
                else:
                    body = text_read(p)
                    from progress_projection import TASK_PATTERN
                    if owned_tasks.intersection(re.findall(TASK_PATTERN, str(p.relative_to(root)) + '\n' + body[:1800])):
                        watch[str(p.relative_to(root))] = hashlib.sha256(body.encode()).hexdigest()
    legacy = {who: runtime.legacy_busy(binding, who) for who in selected}
    # Expires naturally without a file mutation. Status reads the existing ledger, never calls Codex.
    strong = None
    if role == 'COORDINATOR':
        from strong_review import Ledger
        strong = Ledger().status(str(root))
    semantic = {'control': control, 'binding': binding['roles'][role], 'watch': watch,
                'health': health, 'legacy': legacy,
                'review': {k: strong.get(k) for k in ('authorization', 'grant_id', 'expires_at', 'used_last_60_minutes', 'active_calls', 'last_call')} if strong else None}
    fingerprint = hashlib.sha256(json.dumps(semantic, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    relevant = []
    for key, cells in taskrows.items():
        if role == 'COORDINATOR' or _owner_role(cells[-1]) == role or any(m in cells[-1] for m in MARKERS[role]):
            from progress_projection import TERMINAL, PAUSED
            if cells[1] not in TERMINAL | PAUSED:
                relevant.append({'task': key, 'title':short(cells[2],160),'state': short(cells[1],80), 'progress': short(cells[-2],320), 'owner': short(cells[-1],100)})
    latest = {}
    for row in sorted(events, key=event_order):
        latest[row['role']] = {k: short(v,320 if k == 'summary' else 180) for k,v in row.items() if k != 'sources'}
        latest[row['role']]['sources'] = row['sources']
    latest_dispatch = {}
    for row in sorted(dispatches, key=lambda d: d['mtime']):
        latest_dispatch[row['role']] = row
    sources = {'TODO.md': datetime.fromtimestamp((root/'TODO.md').stat().st_mtime, ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')}
    snapshot = {'schema_version': 1, 'project': str(root), 'role': role, 'observed_at': stamp(), 'control': control,
                'confirmed_idle': idle, 'classification_reasons': [short(r) for r in reasons[:6]],
                'tasks': relevant, 'events': latest, 'event_conflicts': event_conflicts,
                'dispatches': latest_dispatch, 'processes': health,
                'sources': sources, 'strong_review': strong, 'fingerprint': fingerprint}
    snapshot['coordinator_requests'] = [{'request_id': r['request_id'], 'received_at': r['received_at'],
                                        'source': r['source'], 'summary': short(r['text'],180)} for r in requests]
    snapshot['operations'] = operations
    snapshot['task_observations'] = task_observations
    return snapshot, watch


def observer_state(path):
    state=runtime.load(path,{}) or {}
    if not isinstance(state,dict): raise ValueError('invalid observer cursor')
    for key in ('pending','acknowledged','observed'):
        if key in state and not isinstance(state[key],dict): raise ValueError('invalid observer '+key)
    for key in ('acknowledged','observed'):
        if not isinstance(state.get(key,{}).get('watch',{}),dict): raise ValueError('invalid observer watch')
    sources=state.get('pending',{}).get('changed_sources',[])
    if not isinstance(sources,list) or any(not isinstance(item,str) for item in sources):
        raise ValueError('invalid pending source index')
    return state


def observe(root, role, clock=time.time):
    root = Path(root).resolve()
    base = root/'inbox/control/runtime'
    with runtime.locked(base/f'{role}-observer.lock'):
        try:
            snapshot, watch = collect(root, role)
            state = observer_state(base/f'observer/{role}.json')
            current = snapshot['fingerprint']
            ack = state.get('acknowledged', {})
            oldwatch = ack.get('watch', {})
            changes = [p for p in sorted(set(watch)|set(oldwatch)) if watch.get(p) != oldwatch.get(p)]
            if not ack:
                changes = ['TODO.md', 'inbox/PROTOCOL.md'] + [v['source'] for v in snapshot['dispatches'].values()] + [v['source'] for v in snapshot['events'].values()]
            # Durable, coalesced notification queue; never a second task-state store.
            # An independent scanner can enqueue changes while this role's native job
            # is busy. Repeated observations do not consume them or launch a worker.
            pending = state.get('pending') or {}
            wake = current != ack.get('fingerprint') or bool(pending) or bool(snapshot.get('coordinator_requests'))
            reason = 'new_or_unacknowledged_change' if wake else 'checked_no_actionable_change'
            if wake:
                changes = sorted(set(changes) | set(pending.get('changed_sources', [])))
                state['pending'] = {
                    'fingerprint': current, 'changed_sources': changes,
                    'first_seen_at': pending.get('first_seen_at', snapshot['observed_at']),
                    'last_seen_at': snapshot['observed_at'],
                }
            if snapshot['control'] != 'ACTIVE' and current == ack.get('fingerprint'):
                wake, reason = False, 'control_not_active'
            # A human role owner and a native cron owner share one execution lease.
            # Keep every change pending while that owner is alive; do not start a
            # second model worker merely to discover that it cannot acquire it.
            if wake and snapshot.get('processes',{}).get(role,{}).get('state') == 'alive':
                wake, reason = False, 'queued_while_role_owner_alive'
            runtime.save(base/f'snapshots/{role}.json', snapshot)
            state['observed'] = {'fingerprint': current, 'watch': watch, 'at': clock()}
            runtime.save(base/f'observer/{role}.json', state)
            tick = {'checked_at': snapshot['observed_at'], 'role': role, 'mode': 'script_poll',
                    'control': snapshot['control'], 'wakeAgent': wake, 'reason': reason,
                    'fingerprint': current, 'changed_sources': changes[:12]}
            runtime.save(base/f'ticks/{role}.json', tick)
            activity_report.sample(snapshot, tick)
            notification=state.get('pending')
            preview=({**notification,'changed_sources':notification['changed_sources'][:12],
                      'changed_source_count':len(notification['changed_sources']),
                      'full_source_index':str(base/f'observer/{role}.json')} if notification else None)
            return {**tick, 'snapshot': str(base/f'snapshots/{role}.json'),
                    'pending': preview,
                    'quiet_hours': activity_report.quiet(),
                    'instruction': 'Read short snapshot and changed sources (event:ROLE:ID resolves through snapshot events/sources); after handling, poll again, verify new business input, and ack the exact final fingerprint. Pure unchanged observations go only to runtime observations through the fixed helper, never append new session/business events. Existing ACK/lease/gates still apply. Asia/Shanghai 21:00-08:00: keep working and recording business changes; no proactive group messages, final [SILENT]. Daytime executor/tester summaries only at 11:00/15:00/18:00; coordinator daytime reports every20min and night summary at08:00.'}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            # Unreadable documents are still a durable notification. A parser
            # failure is no reason to launch another model over a live owner.
            cursor=base/f'observer/{role}.json'
            try:
                state=observer_state(cursor)
            except (OSError,ValueError,KeyError,TypeError):
                # Preserve unreadable pending input before reconstructing a
                # notification baseline from authoritative documents.
                raw=cursor.read_bytes() if cursor.is_file() else b''
                archive=base/f'observer-recovery/{role}-{hashlib.sha256(raw).hexdigest()}.raw'
                archive.parent.mkdir(parents=True,exist_ok=True)
                if not archive.exists(): archive.write_bytes(raw)
                state={'recovery_required':{'source':str(cursor),'preserved':str(archive)},
                       'pending':{'changed_sources':[str(archive.relative_to(root)),'TODO.md','inbox/PROTOCOL.md']}}
            pending=state.get('pending') or {}
            pending.update(first_seen_at=pending.get('first_seen_at',stamp()),
                           last_seen_at=stamp(),error=short(str(exc),240))
            pending.setdefault('changed_sources',[])
            state['pending']=pending
            runtime.save(base/f'observer/{role}.json',state)
            try:
                busy=process_health(runtime.load(runtime.lease_path(root,role),{}) or {}).get('state')=='alive'
            except (OSError,ValueError,KeyError,TypeError):
                busy=False  # Unknown ownership needs verification, never a forced claim.
            tick = {'checked_at': stamp(), 'role': role, 'mode': 'script_poll', 'wakeAgent': True,
                    'reason': 'observer_unavailable', 'error': short(str(exc),240)}
            if busy:
                tick.update(wakeAgent=False,reason='source_unavailable_queued_while_role_owner_alive')
            runtime.save(base/f'ticks/{role}.json', tick)
            activity_report.sample({'role': role, 'project': str(root), 'observed_at': tick['checked_at'], 'error': tick['error']})
            return tick


def acknowledge(root, role, fingerprint):
    root, binding = runtime.binding(root)
    if role not in binding['roles']:
        raise ValueError('role not bound')
    base = root/'inbox/control/runtime'
    with runtime.locked(base/f'{role}-observer.lock'):
        state = runtime.load(base/f'observer/{role}.json', {}) or {}
        observed = state.get('observed', {})
        if not fingerprint or fingerprint != observed.get('fingerprint'):
            raise ValueError('STALE_OBSERVATION: do not consume a newer event with an old fingerprint')
        # An independent scanner need not have run between the new input and
        # ACK. Recollect under the observer lock so stale ACK cannot swallow it.
        snapshot, _ = collect(root, role)
        if snapshot['fingerprint'] != fingerprint:
            raise ValueError('STALE_OBSERVATION: business sources changed after the observation; poll and handle them first')
        state['acknowledged'] = {**observed, 'at': time.time()}
        state.pop('pending', None)
        runtime.save(base/f'observer/{role}.json', state)
        return {'acknowledged': fingerprint, 'role': role}


def scan_registry(registry):
    """Script-only sweep, independent of long-running native model jobs.

    No cron dispatch, business writes, messaging, process termination or ACK.
    The existing role job consumes the durable pending notification when free.
    """
    entries = runtime.load(Path(registry), {}).get('projects')
    if not isinstance(entries, list):
        raise ValueError('Invalid project registry')
    result, seen = [], set()
    for entry in entries:
        try:
            if not isinstance(entry, dict):
                raise ValueError('Invalid registry entry')
            root, binding = runtime.binding(entry['project_root'])
            if root in seen:
                continue
            seen.add(root)
            from task_observation import sample_registered
            sample_registered(root)
            for role in ROLES:
                if role not in binding['roles']:
                    continue
                tick = observe(root, role)
                result.append({
                    'project': str(root), 'role': role,
                    'checked_at': tick['checked_at'], 'wakeAgent': tick['wakeAgent'],
                    'reason': tick['reason'], 'pending': tick.get('pending'),
                    **({'error': tick['error']} if tick.get('error') else {}),
                })
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result.append({'project': entry.get('project_root') if isinstance(entry, dict) else None,
                           'error': short(exc, 240)})
    return result


def refresh(root, role):
    root = Path(root).resolve()
    base = root/'inbox/control/runtime'
    with runtime.locked(base/f'{role}-observer.lock'):
        snapshot, _ = collect(root, role)
        runtime.save(base/f'snapshots/{role}.json', snapshot)
        activity_report.sample(snapshot)
        return snapshot


def render(snapshot):
    from progress_display import render_projection
    from runtime_tools import refresh_progress_facts
    # Only the display path reads quiet confirmations. Scheduler fingerprints are unchanged.
    root=Path(snapshot['project'])
    try:
        packet=refresh_progress_facts(root,snapshot=snapshot)
    except (OSError,ValueError,KeyError,TypeError) as exc:
        return ('**'+LABELS.get(snapshot.get('role'),'角色')+'：当前进展无法核实**\n\n'
                '- **原因：** 当前来源读取失败或正在变化（'+type(exc).__name__+'）。\n'
                '- **下一步：** 核对最新 TODO、派单与回执；本次没有用旧快照判定完成。')
    return render_projection(packet,role=snapshot.get('role','COORDINATOR'))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['poll','ack','snapshot','report','idle-report','scan','register-observation'])
    p.add_argument('--project', type=Path)
    p.add_argument('--role', choices=ROLES)
    p.add_argument('--registry', type=Path)
    p.add_argument('--fingerprint')
    p.add_argument('--registration', type=Path)
    p.add_argument('--token-file', type=Path)
    a=p.parse_args()
    if a.action == 'scan':
        if not a.registry:
            p.error('scan requires --registry')
        result = scan_registry(a.registry)
        print(json.dumps(result, ensure_ascii=False))
        if any(row.get('error') for row in result):
            raise SystemExit(1)
        return
    if not a.project or not a.role:
        p.error('this action requires --project and --role')
    if a.action == 'register-observation':
        if not a.registration or not a.token_file:
            p.error('register-observation requires --registration and --token-file')
        print(json.dumps(register_observations(a.project, a.role, runtime.load(a.registration),
                                               runtime.load(a.token_file).get('token')), ensure_ascii=False))
        return
    if a.action=='poll':
        print(json.dumps(observe(a.project,a.role), ensure_ascii=False))
        return
    if a.action=='ack':
        print(json.dumps(acknowledge(a.project,a.role,a.fingerprint),ensure_ascii=False))
        return
    if a.action in ('report', 'idle-report') and activity_report.quiet():
        return
    # Deprecated wrappers cannot continue the old two-hour idle notifications.
    if a.action == 'idle-report':
        return
    if a.action == 'report' and a.role != 'COORDINATOR':
        print(activity_report.render_digest(activity_report.build(a.project, a.role, 'phase')))
        return
    if a.action == 'report' and activity_report.local_now().hour == 8 and activity_report.local_now().minute < 20:
        return  # The separate 08:00 morning job owns this summary.
    try:
        s=refresh(a.project,a.role)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        s={'role':a.role,'observed_at':stamp(),'error':'采集失败：'+short(str(exc)), 'project':str(a.project)}
    if a.action=='snapshot':
        print(json.dumps(s, ensure_ascii=False,indent=2))
    elif a.role=='COORDINATOR' or not s.get('confirmed_idle'):
        print(render(s))


if __name__=='__main__':
    main()
