#!/usr/bin/env python3
"""Document observer / bounded report projection. No model, business writes or messaging."""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import sqlite3
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

# 心跳陈旧仅表示活动证据不足，不证明进程/原生回合结束，不允许第二owner。
STALE_HEARTBEAT_SECONDS = 1800


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
    if (not isinstance(record, dict) or record.get('schema_version') != 1
            or not isinstance(record.get('entries'), list)
            or any(not isinstance(entry, dict) for entry in record['entries'])):
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


def archival_session_fingerprint(root, role, body, source, rules):
    """Keep a pure rotation equivalent without hiding any new business byte."""
    if any(entry.get('kind') == 'session_block' and entry.get('source') == source for entry in rules):
        # These offsets refer to this view; never apply them to an old snapshot.
        return session_fingerprint(body, source, rules)
    record = runtime.load(root/f'inbox/control/runtime/observation-index/{role}.json', {}) or {}
    archived = record.get('archived_entries', [])
    if not isinstance(archived, list) or any(not isinstance(item, dict)
            or not re.fullmatch(r'[a-f0-9]{32}', str(item.get('generation', '')))
            or not isinstance(item.get('entries'), list)
            or any(not isinstance(entry, dict) for entry in item['entries']) for item in archived):
        raise ValueError('DOCUMENT_CONFLICT: invalid archived observation provenance')
    by_generation = {}
    for item in archived:
        selected = [entry for entry in item['entries']
                    if entry.get('kind') == 'session_block' and entry.get('source') == source]
        if selected:
            by_generation.setdefault(item.get('generation'), []).extend(selected)
    from document_store import archival_snapshot
    previous, generations = archival_snapshot(root, root/source, body, stop_generations=by_generation)
    recovered = by_generation.get(generations[-1], []) if generations else []
    return session_fingerprint(previous, source, rules+recovered)


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
            index_path = root/f'inbox/control/runtime/observation-index/{role}.json'
            with runtime.locked(index_path.with_suffix('.lock')):
                existing = observation_rules(root, role)
                merged = {digest(item): item for item in existing + entries}
                index = runtime.load(index_path, {}) or {}
                runtime.save(index_path, {**index, 'schema_version': 1, 'role': role,
                             'registered_at': stamp(), 'entries': list(merged.values())})
            return {'role': role, 'registered': len(entries), 'total': len(merged), 'acknowledged': False}


def process_health(lease, identity=None):
    """Preserve all independent legacy reasons; do not infer execution authority."""
    from workflow_observation import process_facts
    identity = identity or runtime.pid_identity
    actual = identity(lease['pid']) if lease.get('pid') and lease.get('process_start') else None
    return process_facts(lease, actual, time.time())


def owner_blocks_new_execution(health):
    from workflow_observation import owner_blocks_facts
    return owner_blocks_facts(health)


def source_states(snapshot, prior, now):
    """Carry per-source success times; partial blindness is never pool health."""
    old = (prior or {}).get('source_states', {})
    result = {source: {**facts, 'state': 'NOT_CHECKED', 'checked_at': now}
              for source, facts in old.items()}
    for source in snapshot.get('sources', []):
        result[str(source)] = {'state': 'OBSERVED', 'checked_at': now,
                               'last_verified_at': now}
    fields = ('role', 'execution_id', 'tool_id', 'action_id', 'work_id')
    def key(row):
        if str(row.get('source', '')).startswith('activity:'):
            return row['source']  # already includes the full work/execution identity
        scope = {name: row[name] for name in fields if row.get(name) is not None}
        return str(row.get('source', 'UNKNOWN'))+('#'+digest(scope) if scope else '')
    global_failures = {str(row.get('source', 'UNKNOWN')) for row in snapshot.get('faults', [])
                       if not any(row.get(name) is not None for name in fields)}
    for row in snapshot.get('anomalies', []):
        row['source_key'] = key(row)
    for row in [*snapshot.get('anomalies', []), *(prior or {}).get('active', {}).values()]:
        source = str(row.get('source', 'UNKNOWN'))
        source_key = key(row)
        if source in snapshot.get('sources', []) and source not in global_failures:
            result[source_key] = {'state': 'OBSERVED', 'checked_at': now, 'last_verified_at': now}
    for fault in snapshot.get('faults', []):
        source = str(fault.get('source', 'UNKNOWN'))
        source_key = key(fault)
        value = result.setdefault(source_key, {})
        value.update(state='UNVERIFIED', checked_at=now,
            last_verified_at=old.get(source_key, {}).get('last_verified_at'),
            reason_codes=sorted(set(value.get('reason_codes', []) +
                                    fault.get('reason_codes', [fault['type']]))))
    return result


def workflow_activities(database, pool, home, facts, now):
    """Allowlisted actual boundaries; incomplete collectors stay incomplete.

    Main and auxiliary request hooks require the exact execution identity.
    Each source proves its own coverage; a readable legacy timing database or
    tool registration cannot certify request completeness or a normal wait.
    """
    policy_file = Path(database).parent/'observation-policy.json'
    threshold, policy_reason = None, 'POLICY_UNCONFIGURED'
    try:
        policy = json.loads(text_read(policy_file))
        if (policy.get('schema_version') != 1 or type(policy.get('no_activity_seconds')) not in (int, float)
                or not __import__('math').isfinite(policy['no_activity_seconds']) or policy['no_activity_seconds'] <= 0):
            raise ValueError('invalid observation policy')
        threshold, policy_reason = policy['no_activity_seconds'], 'CONFIGURED'
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError, KeyError):
        policy_reason = 'POLICY_UNVERIFIED'
    activities = []
    works = {row['work_id']: row for row in facts['works']}
    executions = {row['execution_id']: row for row in facts['executions']}
    checkpoints = {}
    with sqlite3.connect(Path(database).as_uri()+'?mode=ro', uri=True, timeout=.05) as db:
        for work_id, at in db.execute("SELECT work_id,MAX(created_at) FROM events WHERE kind='CHECKPOINT_SAVED' GROUP BY work_id"):
            checkpoints[work_id] = at
    for role in facts['roles']:
        work = works.get(role.get('current_work_id'))
        if not work or work['state'] == 'ENDED':
            continue
        if (work['state']=='QUEUED' and not work.get('active_execution_id')
                and not role.get('writer_execution_id')):
            # Retained business responsibility without a current execution has
            # no model/tool activity to collect. Do not invent a null identity.
            continue
        execution = executions.get(role.get('writer_execution_id'), {})
        identity = {'pool': str(pool), 'generation': facts['generation'], 'run_id': work['run_id'],
            'role': role['role'], 'work_id': work['work_id'], 'task_id': work['task_id'],
            'attempt': work['attempt'], 'execution_id': execution.get('execution_id'),
            'execution_generation': work['execution_generation']}
        from workflow_activity import read_activity, read_model_activity
        tool_observation = read_activity(home, identity, now=now)
        model_observation = read_model_activity(home, identity, now=now)
        sources = [*tool_observation['sources'], *model_observation['sources']]
        # Native binding precedes the first on_llm hook. This startup interval
        # is neither certified coverage nor a collector outage. Do not grant a
        # rolling grace: it is anchored once to the native execution start.
        started = execution.get('started_at')
        if type(started) in (int, float) and 0 <= now-started <= 60:
            for source in sources:
                if source['reason_codes'] in (['TOOL_COLLECTOR_COVERAGE_NOT_STARTED'],
                                               ['MODEL_COLLECTOR_COVERAGE_NOT_STARTED']):
                    source['coverage_grace_until'] = started+60
        lifecycles = [*tool_observation['lifecycles'], *model_observation['lifecycles']]
        # Native hooks attribute through this execution/session mapping. Its
        # loss cannot leave an older collector epoch certified complete.
        try:
            scope_file = Path(home)/'runtime/role-pool-executions'/(execution['execution_id']+'.json')
            scope = json.loads(text_read(scope_file))
            keys = ('generation', 'run_id', 'role', 'work_id', 'execution_id', 'execution_generation')
            if (any(scope.get(key) != identity[key] for key in keys)
                    or scope.get('pool_path') != str(pool)
                    or not isinstance(scope.get('session_id'), str) or not scope['session_id'].strip()):
                raise ValueError('execution session mapping mismatch')
        except (OSError, ValueError, TypeError, KeyError):
            for source in sources:
                source['complete'] = False
                source.pop('coverage_grace_until', None)
                source['reason_codes'] = sorted(set(source.get('reason_codes', [])+['EXECUTION_SESSION_UNVERIFIED']))
        activities.append({'identity': identity, 'current_work_valid': work['state'] in ('ACTIVE', 'RUNNING', 'WAITING'),
            'required_sources': ['tools', 'model_requests'], 'sources': sources, 'lifecycles': lifecycles,
            'checkpoint_at': checkpoints.get(work['work_id']),
            'checkpoint_evidence_ref': str(database)+'#checkpoint:'+work['work_id']})
    progress=[]
    for role in facts['roles']:
        work=works.get(role.get('current_work_id'))
        if work:
            progress.append({'role':role['role'],'work_id':work['work_id'],'state':work['state'],
                'processed_through':work['processed_through'],'issued_through':work['issued_through'],
                'last_checkpoint_at':checkpoints.get(work['work_id']),
                'checkpoint_is_not_proof_of_progress':True})
    return {'activities': activities, 'no_activity_seconds': threshold,'business_progress':progress,
            'activity_policy': {'state': policy_reason, 'source': str(policy_file),
                'no_activity_seconds':threshold,'adoption':'READ_FROM_SOURCE' if policy_reason=='CONFIGURED' else 'NOT_ADOPTED',
                'meaning':'观察阈值，不是任务超时或接管权限；登记中的合法操作按任务期限观察。'}}


def _workflow_observation(database, pool, home, instant):
    """Observe schema-2 responsibility and native writers without renewing them."""
    from business_ledger import BusinessLedger
    snapshot = {'schema_version': 2, 'observation_schema': 2, 'at': instant,
        'pool_path': str(pool), 'authority_source': str(database), 'health': 'unverified',
        'sources': [str(database)], 'roles': {role: [] for role in ROLES},
        'services': [], 'faults': [], 'anomalies': []}
    def fault(kind, source, **details):
        # Keep a stable episode identity while retaining the latest diagnostic
        # details. Otherwise each changing exception string creates a new alert.
        value = {'type': kind, 'source': str(source), **details}
        if kind == 'observer_write_diagnostics_unverified':
            value['reason_codes'] = [details.get('reason_code', 'WRITE_DIAGNOSTICS_SOURCE_UNVERIFIED')]
            value['identity'] = digest([kind, str(source)])
        snapshot['faults'].append(value)
        snapshot['anomalies'].append({**value, 'identity': value.get('identity', digest(value)), 'severity': 1})
    try:
        facts = BusinessLedger(database, readonly=True).snapshot()
        if facts['mode'] != 'ACTIVE' or {row['role'] for row in facts['roles']} != set(ROLES):
            raise ValueError('active five-role authority required')
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        fault('observer_business_ledger_unverified', database, error=type(exc).__name__)
        return snapshot
    snapshot.update(mode=facts['mode'], generation=facts['generation'],
        current_run_id=facts['current_run_id'], admission_open=facts['admission_open'],
        event_high_water=facts['event_high_water'])
    from workflow_notification_registry import diagnostics as notification_diagnostics
    observed_ledger=BusinessLedger(database,readonly=True,clock=lambda:instant)
    snapshot['notification_health']=notification_diagnostics(observed_ledger)
    database_bytes=Path(database).stat().st_size
    snapshot['capacity']={'database_bytes':database_bytes,'warning_bytes':2*1024**3,
        'state':'CAPACITY_WARNING' if database_bytes>=2*1024**3 else 'OBSERVED',
        'new_input_page_limit_bytes':65536,'historical_compaction_performed':False,
        'retention':'保留命令、原始事件及不可变页；归档须一并保存哈希引用，不在线删库或VACUUM。'}
    snapshot['closed_work_ids'] = [row['work_id'] for row in facts['works'] if row['state'] == 'ENDED']
    snapshot['closed_execution_ids'] = []
    try:
        import workflow_diagnostics
        diagnostics = workflow_diagnostics.observe(pool, now=instant)
        snapshot['write_diagnostics'] = {key: diagnostics.get(key) for key in
            ('rule_version', 'source', 'source_state', 'error', 'error_stage',
             'verification_source_state', 'write_paths', 'verification', 'closures')}
        snapshot['anomalies'].extend(diagnostics['anomalies'])
        source_degraded = diagnostics['source_state'] == 'DEGRADED'
        verification_degraded = diagnostics.get('verification_source_state') == 'DEGRADED'
        if source_degraded or verification_degraded:
            fault('observer_write_diagnostics_unverified', diagnostics['source'],
                  reason_code=('WRITE_DIAGNOSTICS_SOURCE_DEGRADED' if source_degraded
                               else 'WRITE_DIAGNOSTICS_VERIFICATION_DEGRADED'),
                  error=diagnostics.get('error'), error_stage=diagnostics.get('error_stage'),
                  verification_source_state=diagnostics.get('verification_source_state'))
        elif diagnostics['source_state'] == 'OBSERVED':
            snapshot['sources'].append(diagnostics['source'])
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        fault('observer_write_diagnostics_unverified', database,
              reason_code='WRITE_DIAGNOSTICS_OBSERVER_EXCEPTION',
              error=type(exc).__name__, error_stage='observer.workflow_diagnostics.observe')
    try:
        snapshot.update(workflow_activities(database, pool, home, facts, instant))
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        fault('observer_activity_sources_unverified', database, error=type(exc).__name__)
    works = {row['work_id']: row for row in facts['works']}
    runs = {row['run_id']: row for row in facts['runs']}
    executions = {row['execution_id']: row for row in facts['executions']}
    processes, scan_verified = {}, True
    try:
        for proc in Path('/proc').iterdir():
            if not proc.name.isdigit():
                continue
            try:
                tail = (proc/'stat').read_text().rsplit(') ', 1)[1].split()
                processes[int(proc.name)] = {'state': tail[0], 'process_start': tail[19],
                    'pgrp': int(tail[2]), 'cgroup': (proc/'cgroup').read_text()}
            except (FileNotFoundError, ProcessLookupError):
                continue
            except (OSError, ValueError, IndexError):
                scan_verified = False
    except OSError:
        scan_verified = False
    if not scan_verified:
        fault('observer_process_scopes_unverified', '/proc')
    else:
        snapshot['sources'].append('/proc')
    observed = {}
    for execution in executions.values():
        identifier, role = execution['execution_id'], execution['role']
        actual = processes.get(execution['pid'])
        state = ('unverified' if not actual and not scan_verified else
                 'exited_or_reused' if not actual or actual['process_start'] != str(execution['process_start'])
                 or actual['state'] in ('Z', 'X') else 'alive')
        scope = execution['scope']
        expected_cgroup = scope.get('cgroup') if isinstance(scope, dict) else scope
        writer = execution.get('writer') or {}
        group = writer.get('pgrp') if writer.get('dedicated_process_group') is True else None
        dedicated_scope = isinstance(expected_cgroup, str) and bool(re.search(
            r'hermes-worker-cron-[A-Za-z0-9_-]+-exec-'+re.escape(identifier)+r'\.scope', expected_cgroup))
        def within_scope(actual_cgroup):
            expected = [line.split(':',2)[2].rstrip('/') for line in expected_cgroup.splitlines()
                        if line.startswith('0::')]
            actual_paths = [line.split(':',2)[2].rstrip('/') for line in actual_cgroup.splitlines()
                            if line.startswith('0::')]
            return any(path == parent or path.startswith(parent+'/') for parent in expected for path in actual_paths)
        descendants = sorted(pid for pid, value in processes.items()
            if pid != execution['pid'] and value['state'] not in ('Z', 'X') and
            ((group is not None and value['pgrp'] == group) or
             (dedicated_scope and within_scope(value['cgroup']))))
        tools = []
        for tool in facts.get('execution_tools', []):
            if tool['execution_id'] != identifier:
                continue
            actual_tool = processes.get(tool['pid'])
            tool_writer = tool['writer']
            tool_group = tool_writer.get('pgrp') if tool_writer.get('dedicated_process_group') is True else None
            tool_alive = bool(actual_tool and actual_tool['state'] not in ('Z', 'X') and
                actual_tool['process_start'] == str(tool['process_start']))
            children = sorted(pid for pid, value in processes.items() if tool_group is not None and
                value['pgrp'] == tool_group and value['state'] not in ('Z', 'X'))
            tools.append({'tool_id': tool['tool_id'], 'pid': tool['pid'], 'alive': tool_alive,
                'descendant_pids': children, 'identity_verified': bool(actual_tool and
                    actual_tool['process_start'] == str(tool['process_start']) and
                    actual_tool['cgroup'] == tool_writer.get('cgroup')),
                'writer_group_verified': tool_group is not None})
            descendants = sorted(set(descendants)|set(children)|({tool['pid']} if tool_alive else set()))
            if tool_group is None or (tool_alive and not tools[-1]['identity_verified']) or not scan_verified:
                fault('observer_tool_writer_unverified', database, execution_id=identifier,
                      tool_id=tool['tool_id'], role=role)
        terminal_scopes_verified = (scan_verified and (group is not None or dedicated_scope)
            and all(tool['writer_group_verified'] and not tool['alive'] and not tool['descendant_pids']
                    for tool in tools))
        if (execution['state'] == 'ENDED' and state == 'exited_or_reused' and not descendants
                and terminal_scopes_verified):
            snapshot['closed_execution_ids'].append(identifier)
            continue
        work = works.get(execution['work_id'])
        value = {'execution_id': identifier, 'role': role, 'work_id': execution['work_id'],
            'generation': execution['generation'], 'execution_generation': execution['execution_generation'],
            'ledger_state': execution['state'], 'pid': execution['pid'],
            'process_start': execution['process_start'], 'owner_process_state': state,
            'scope_identity_verified': bool(actual and actual['cgroup'] == expected_cgroup),
            'descendant_pids': descendants, 'tools': tools, 'writers_present': state == 'alive' or bool(descendants),
            'writer_scope_verified': scan_verified and (group is not None or dedicated_scope),
            'run_id': work['run_id'] if work else None}
        observed[identifier] = value
        if (state == 'unverified' or not value['writer_scope_verified'] or
                (state == 'alive' and not value['scope_identity_verified'])):
            fault('observer_execution_identity_unverified', database, execution_id=identifier, role=role)
        if not work or work['role'] != role:
            fault('observer_execution_work_unverified', database, execution_id=identifier, role=role)
        if execution['state'] == 'ENDED' and value['writers_present']:
            fault('observer_ended_execution_writer_present', database, execution_id=identifier, role=role)
        if execution['state'] in ('ACTIVE', 'DRAINING') and state == 'exited_or_reused' and not descendants:
            fault('observer_business_writer_exited', database, execution_id=identifier, role=role,
                  work_id=execution['work_id'])
    for current in facts['roles']:
        role = current['role']; work = works.get(current['current_work_id'])
        writer = observed.get(current['writer_execution_id'])
        values = [value for value in observed.values() if value['role'] == role]
        result = {'role': role, 'observed_at': instant, 'state': 'observed', 'control': current['control'],
            'current_work': ({key: work.get(key) for key in ('work_id', 'run_id', 'task_id', 'attempt',
                'state', 'candidate_id', 'active_execution_id', 'execution_generation', 'checkpoint')}
                if work else None), 'writer_execution_id': current['writer_execution_id'], 'executions': values,
            'business_responsibility': 'unclosed' if work and work['state'] != 'ENDED' else 'closed_or_absent',
            'run_control': runs.get(work['run_id'], {}).get('control') if work else None}
        snapshot['roles'][role].append(result)
        if current['current_work_id'] and (not work or work['role'] != role):
            result['state'] = 'unverified'
            fault('observer_current_work_unverified', database, role=role)
        if current['writer_execution_id'] and (not writer or writer['work_id'] != current['current_work_id']):
            result['state'] = 'unverified'
            fault('observer_role_writer_unverified', database, role=role,
                  execution_id=current['writer_execution_id'])
        for value in values:
            if value['ledger_state'] in ('ACTIVE', 'DRAINING') and value['execution_id'] != current['writer_execution_id']:
                fault('observer_unbound_business_writer', database, role=role, execution_id=value['execution_id'])
    # A service is a separate lifecycle. Reading its unit cannot close business
    # work, create verification facts, release a role or authorize another run.
    import subprocess
    for action in facts['external_actions']:
        authorization = action.get('authorization') or {}
        unit = None
        value = {key: action.get(key) for key in ('action_id', 'candidate_id', 'kind', 'state')}
        value.update(service_id=None, unit=None, observation='unverified')
        service_candidate=next((r for r in facts['candidates'] if r['candidate_id']==action['candidate_id']),{})
        value['run_id']=service_candidate.get('run_id')
        snapshot['services'].append(value)
        if action['state'] not in ('SUBMITTED','UNKNOWN'):
            try:
                from workflow_terminal_observation import terminal
                candidate=next(r for r in facts['candidates'] if r['candidate_id']==action['candidate_id'])
                value.update(terminal(action,candidate,facts))
            except (OSError,ValueError,KeyError,TypeError,StopIteration) as exc:
                value.update(observation='terminal_receipt_unverified',reason=str(exc)[:160])
                fault('observer_terminal_service_evidence_unverified',database,
                    action_id=action['action_id'],reason=str(exc)[:160])
            continue
        invocation_path = Path(database).parent/'services'/hashlib.sha256(action['action_id'].encode()).hexdigest()/'invocation.json'
        try:
            invocation = json.loads(text_read(invocation_path))
            reference = invocation['reference']; frozen = invocation['frozen']
            candidate = next(row for row in facts['candidates'] if row['candidate_id'] == action['candidate_id'])
            spec_file = Path(reference['spec']['path'])
            raw = spec_file.read_bytes()
            spec = json.loads(raw)
            from workflow_observation_services import verify
            value['identity_basis']=verify(invocation,action,candidate,facts,raw)
            unit = reference['unit']
            value.update(unit=unit, service_id=reference['native_id'], invocation_source=str(invocation_path),
                         spec_sha256=reference['spec']['sha256'])
            snapshot['sources'].extend([str(invocation_path), str(spec_file)])
        except (OSError, ValueError, TypeError, KeyError, StopIteration):
            if action['state'] in ('SUBMITTED', 'UNKNOWN'):
                fault('observer_service_identity_unverified', invocation_path, action_id=action['action_id'])
            continue
        if not isinstance(unit, str) or not re.fullmatch(r'[A-Za-z0-9_.@:-]+\.(?:service|scope)', unit):
            if action['state'] in ('SUBMITTED', 'UNKNOWN'):
                fault('observer_service_identity_unverified', database, action_id=action['action_id'])
            continue
        try:
            output = subprocess.run(['systemctl', '--user', 'show', unit,
                '--property=LoadState,ActiveState,SubState,MainPID,ControlGroup,Result'],
                capture_output=True, text=True, timeout=5)
            properties = dict(line.split('=', 1) for line in output.stdout.splitlines() if '=' in line)
            if output.returncode or properties.get('LoadState') == 'not-found' or not properties.get('ActiveState'):
                raise ValueError('service unavailable')
            cgroup = properties.get('ControlGroup', '')
            if properties['ActiveState'] in ('active', 'activating', 'deactivating'):
                if not cgroup.endswith('/'+unit) or 'hermes-worker-cron-' in cgroup:
                    raise ValueError('service not independent')
                value['observation'] = 'active_independent_service'
            elif properties['ActiveState'] in ('inactive', 'failed'):
                value['observation'] = 'service_exited_result_uncollected' if action['state'] in ('SUBMITTED', 'UNKNOWN') else 'service_exited'
            else:
                raise ValueError('service state unknown')
            value['properties'] = properties
            snapshot['sources'].append(unit)
        except (OSError, ValueError, subprocess.SubprocessError):
            fault('observer_independent_service_unverified', unit, action_id=action['action_id'])
    # Only already-verified ended scopes may close old capability incidents.
    # This cache write is never a successful business write or a writer release.
    diagnostics = snapshot.get('write_diagnostics') or {}
    closed_any = False
    for path in diagnostics.get('write_paths', []) or []:
        if (path.get('write_path_state') not in {'WRITE_AUTHORITY_ANOMALY', 'WRITE_AUTHORITY_UNVERIFIED'} or
                path.get('execution_id') not in snapshot['closed_execution_ids']):
            continue
        execution = executions.get(path['execution_id'], {})
        work = works.get(execution.get('work_id'), {})
        if any(path.get(key) != expected for key, expected in {
                'generation': execution.get('generation'), 'run_id': work.get('run_id'),
                'role': execution.get('role'), 'work_id': execution.get('work_id'),
                'execution_generation': execution.get('execution_generation')}.items()):
            continue
        import workflow_diagnostics
        closed = workflow_diagnostics.close_terminated(pool, path, at=instant, evidence_ref={
            'terminal_verified': True, 'terminal_kind': 'EXECUTION', 'source': str(database),
            'execution_id': execution['execution_id'], 'checked_at': instant,
            'pid_start_exited': True, 'child_scopes_verified_empty': True})
        if closed.get('state') == 'DEGRADED':
            fault('observer_write_diagnostics_unverified', diagnostics.get('source', database),
                  reason_code='WRITE_DIAGNOSTICS_CLOSURE_DEGRADED',
                  error=closed.get('error'), error_stage=closed.get('error_stage', 'close_terminated'))
        closed_any = closed_any or bool(closed.get('closed_paths'))
    if closed_any:
        old_ids = {value['identity'] for value in snapshot['anomalies']
                   if value.get('source') == diagnostics.get('source')}
        refreshed = workflow_diagnostics.read_projection(pool)
        snapshot['anomalies'] = [value for value in snapshot['anomalies'] if value['identity'] not in old_ids]
        snapshot['anomalies'].extend(refreshed['anomalies'])
        snapshot['write_diagnostics'] = {key: refreshed.get(key) for key in
            ('rule_version', 'source', 'source_state', 'write_paths', 'closures')}
    snapshot['observation_closures'] = (snapshot.get('write_diagnostics') or {}).get('closures') or []
    snapshot['real_execution_observed'] = any(row['writers_present'] and row['scope_identity_verified']
        and row['writer_scope_verified'] for row in observed.values())
    snapshot['health'] = 'unverified' if snapshot['faults'] else 'observed'
    return snapshot


def collect_pool_leases(pool_path, *, home=None, clock=time.time):
    """Read current pool roots AND frozen scopes of owners not proven exited.

    This is observation only: no reconciliation, lease maintenance, model or
    transport. Failure to read an authority is an observer fault, never no_lease.
    """
    home = Path(home or os.environ.get('HERMES_HOME') or Path.home()/'.hermes').resolve()
    pool_path = Path(pool_path).resolve()
    instant = clock()
    snapshot = {'schema_version': 1, 'at': instant, 'pool_path': str(pool_path), 'health': 'unverified',
                'sources': [], 'roles': {role: [] for role in ROLES}, 'faults': [], 'anomalies': []}
    def fault(kind, source, **details):
        value = {'type': kind, 'source': str(source), **details}
        if kind == 'observer_active_execution_without_lease':
            # Membership is evidence, not the identity of a continuing incident.
            value.update(pool_path=str(pool_path), generation=details.get('generation', snapshot.get('generation')),
                         identity_version=2)
            value['identity'] = digest({k: v for k, v in value.items() if k != 'executions'})
        elif kind == 'observer_frozen_execution_unverified':
            # Keep the existing incident identity when its evidence/reason
            # changes. Additional diagnostics must not synthesize recovery.
            value['identity'] = digest({k:value[k] for k in ('type','source','role','execution_id')})
        snapshot['faults'].append(value)
        snapshot['anomalies'].append({**value, 'identity': value.get('identity') or digest(value), 'severity': 1})
    def read(path):
        result = json.loads(text_read(path))
        if not isinstance(result, dict):
            raise ValueError('expected JSON object')
        snapshot['sources'].append(str(path))
        return result
    try:
        pool = read(pool_path)
        if (pool.get('schema_version') != 1 or type(pool.get('generation')) is not int
                or pool['generation'] < 0 or not isinstance(pool.get('roles'), dict)
                or set(pool['roles']) != set(ROLES)
                or any(not isinstance(value, dict) for value in pool['roles'].values())
                or pool.get('control') not in ('ACTIVE', 'PAUSED')):
            raise ValueError('invalid pool generation/roles')
        snapshot['generation'] = pool['generation']
        current = pool.get('current')
        if not isinstance(current, dict):
            raise ValueError('pool has no accepted current selection')
        if current.get('role_roots') is not None and not isinstance(current['role_roots'], dict):
            raise ValueError('invalid current role roots')
    except (OSError, ValueError, TypeError, KeyError) as exc:
        fault('observer_pool_unverified', pool_path, error=type(exc).__name__)
        return snapshot
    database = pool_path.with_suffix('.runtime')/'business.sqlite3'
    if database.exists():
        try:
            from business_ledger import BusinessLedger
            mode = BusinessLedger(database, readonly=True).mode()['mode']
            if mode == 'ACTIVE':
                return _workflow_observation(database, pool_path, home, instant)
            snapshot['business_mode'] = mode
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
            fault('observer_business_authority_unverified', database, error=type(exc).__name__)
            return snapshot
    targets = {}
    for role in ROLES:
        root = (current.get('role_roots') or {}).get(role) or current.get('runtime_root')
        if not isinstance(root, str) or not Path(root).is_absolute():
            fault('observer_role_root_unverified', pool_path, role=role)
            continue
        root = str(Path(root).resolve())
        targets[(role, root)] = {'role': role, 'project_root': root,
                                'generation': pool['generation'], 'current': True, 'executions': []}
    jobs = {value.get('poll_job_id'): role for role, value in pool['roles'].items()
            if isinstance(value, dict) and value.get('poll_job_id')}
    executions, executions_verified = {}, False
    ledger = home/'cron/executions.db'
    folder = home/'runtime/role-pool-executions'
    scope_paths = sorted(folder.glob('*.json'))
    try:
        with sqlite3.connect(ledger.as_uri()+'?mode=ro', uri=True, timeout=1) as db:
            db.row_factory = sqlite3.Row
            executions = {row['id']: dict(row) for row in db.execute('SELECT * FROM executions')
                          if row['job_id'] in jobs}
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_history'").fetchone():
                identities=[p.stem for p in scope_paths if p.stem not in executions]
                for offset in range(0,len(identities),400):
                    batch=identities[offset:offset+400]
                    query='SELECT execution_json FROM execution_history WHERE id IN ('+','.join('?' for _ in batch)+')'
                    for saved in db.execute(query,batch):
                        row = json.loads(saved['execution_json'])
                        if row.get('job_id') in jobs:
                            executions.setdefault(row['id'], row)
        executions_verified = True
        snapshot['sources'].append(str(ledger))
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        fault('observer_executions_unverified', ledger, error=type(exc).__name__)
    # A native row may be terminal before a child scope has actually drained.
    # Cgroup presence protects those writers; command-text matches grant nothing.
    scope_writers, scope_writer_jobs, scope_scan_verified = set(), {}, True
    processes = {}
    try:
        from execution_retention import process_inventory, termination
        processes = process_inventory()
        for process in processes.values():
            if process['state'] in ('Z','X'):
                continue
            for job, execution in re.findall(r'hermes-worker-cron-([A-Za-z0-9_-]+)-exec-([a-f0-9]{32})\.scope',process['cgroup']):
                if job in jobs:
                    scope_writers.add(execution)
                    scope_writer_jobs[execution] = job
    except (OSError, ValueError, IndexError) as exc:
        scope_scan_verified = False
        fault('observer_process_scopes_unverified', '/proc', error=type(exc).__name__)
    if not scope_scan_verified and not any(row['type'] == 'observer_process_scopes_unverified' for row in snapshot['faults']):
        fault('observer_process_scopes_unverified', '/proc', error='scope_read_unavailable')
    active = {key: row for key, row in executions.items()
              if runtime.process_state(row.get('pid'), str(row.get('process_started_at') or ''))
                 != 'exited_or_reused' or key in scope_writers}
    seen = set()
    for path in scope_paths:
        try:
            scope = read(path)
            if scope.get('pool_path') != str(pool_path) or scope.get('kind') != 'business':
                continue
            execution = scope.get('execution_id')
            if (scope.get('schema_version') != 1 or execution != path.stem
                    or not re.fullmatch(r'[a-f0-9]{32}', str(execution))
                    or scope.get('role') not in ROLES or type(scope.get('generation')) is not int
                    or not isinstance(scope.get('project_root'), str)
                    or not Path(scope['project_root']).is_absolute()):
                raise ValueError('invalid frozen scope')
            seen.add(execution)
            row = executions.get(execution)
            # Missing ledger evidence is not proof of a safe exit.
            if executions_verified and scope_scan_verified and row is not None and termination(scope,row,processes):
                continue
            role = scope['role']
            if row is not None and row.get('job_id') != scope.get('job_id'):
                raise ValueError('frozen execution job mismatch')
            root = str(Path(scope['project_root']).resolve())
            target = targets.setdefault((role, root), {'role': role, 'project_root': root,
                'generation': scope['generation'], 'current': False, 'executions': []})
            target['executions'].append({'execution_id': execution, 'generation': scope['generation'],
                'pid': row.get('pid') if row else None,
                'process_start': str(row.get('process_started_at')) if row else None,
                'state': runtime.process_state(row.get('pid'), str(row.get('process_started_at') or '')) if row else 'unverified',
                'scope_writer_present': execution in scope_writers})
            if row is None or (target['executions'][-1]['state'] != 'alive' and execution not in scope_writers):
                owner_state=target['executions'][-1]['state']
                reasons=[]
                if not executions_verified:
                    reasons.append('NATIVE_EXECUTION_SOURCE_UNVERIFIED')
                reasons.append('NATIVE_EXECUTION_ROW_MISSING' if row is None else
                    'OWNER_PROCESS_UNVERIFIED' if owner_state=='unverified' else 'OWNER_EXITED_OR_REUSED')
                if row is not None and row.get('status') not in ('completed','failed','unknown'):
                    reasons.append('NATIVE_STATUS_NOT_TERMINAL')
                if not scope_scan_verified:
                    reasons.append('PROCESS_INVENTORY_UNVERIFIED')
                reasons.append('TERMINATION_PROOF_UNVERIFIED')
                fault('observer_frozen_execution_unverified', path, role=role, execution_id=execution,
                    observation_state='UNKNOWN', native_row_state='MISSING' if row is None else 'PRESENT',
                    native_status=row.get('status') if row else None, owner_process_state=owner_state,
                    scope_writer_present=execution in scope_writers, termination_state='UNVERIFIED',
                    reason_codes=reasons)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            fault('observer_frozen_scope_unverified', path, error=type(exc).__name__)
    for execution, row in active.items():
        if execution not in seen:
            fault('observer_active_execution_scope_missing', folder/(execution+'.json'),
                  role=jobs[row['job_id']], execution_id=execution)
    for execution in scope_writers-set(executions):
        fault('observer_untracked_writer_scope', folder/(execution+'.json'),
              role=jobs[scope_writer_jobs[execution]], execution_id=execution)
    for (role, root_text), target in sorted(targets.items()):
        root = Path(root_text)
        result = {**target, 'observed_at': instant, 'state': 'unverified'}
        result['worker_heartbeats'] = []
        for execution in target['executions']:
            heartbeat = home/'runtime/legacy-heartbeats'/(execution['execution_id']+'.json')
            try:
                receipt = json.loads(text_read(heartbeat))
                if any(receipt.get(key) != value for key,value in {
                        'execution_id':execution['execution_id'],'project_root':root_text,'role':role,
                        'pid':execution['pid'],'process_start':execution['process_start']}.items()):
                    raise ValueError('heartbeat identity mismatch')
                result['worker_heartbeats'].append({'source':str(heartbeat),**receipt})
            except (OSError,ValueError,TypeError):
                result['worker_heartbeats'].append({'execution_id':execution['execution_id'],
                    'source':str(heartbeat),'state':'UNVERIFIED','reason':'WORKER_HEARTBEAT_RECEIPT_UNAVAILABLE'})
        snapshot['roles'][role].append(result)
        snapshot['sources'].append(root_text)
        source = root/'ROLE-BINDINGS.json'
        try:
            binding = read(source)
            if binding.get('schema_version') != 1 or Path(binding.get('project_root', '')).resolve() != root:
                raise ValueError('wrong binding root')
            if target['current']:
                bound = binding.get('roles', {}).get(role) if isinstance(binding.get('roles'), dict) else None
                expected = pool['roles'][role]
                if (not isinstance(bound, dict)
                        or any(bound.get(key) != expected.get(key) for key in ('skill', 'chat_id', 'poll_job_id'))
                        or binding.get('role_pool') != {'path': str(pool_path), 'generation': pool['generation']}):
                    raise ValueError('unbound or wrong binding generation')
            result['binding_state'] = 'current_verified' if target['current'] else 'frozen_old_execution'
            checkpoint_path = root/f'inbox/control/runtime/checkpoints/{role}.json'
            source = checkpoint_path
            checkpoint = read(checkpoint_path) if checkpoint_path.exists() else {}
            result['current_work'] = {key: checkpoint.get(key) for key in
                                      ('work_id', 'run_id', 'task', 'attempt', 'status', 'next')}
            result['business_responsibility'] = 'unverified'
            if checkpoint:
                from role_status import has_released_slot
                result['business_responsibility'] = ('released_verified' if has_released_slot(checkpoint, root)
                                                      else 'unclosed_or_unverified')
                result['checkpoint_age_seconds'] = max(0, instant-checkpoint_path.stat().st_mtime)
            path = runtime.lease_path(root, role)
            source = path
            if not path.exists():
                result['state'] = 'no_lease'
                writers = [row['execution_id'] for row in target['executions']
                           if row['state'] == 'alive' or row['scope_writer_present']]
                result['execution_observation'] = {
                    'active': writers,
                    'unknown': [row['execution_id'] for row in target['executions']
                                if row['execution_id'] not in writers]}
                if writers:
                    generations = {row['generation'] for row in target['executions'] if row['execution_id'] in writers}
                    for generation in sorted(generations):
                        fault('observer_active_execution_without_lease', path, role=role,
                              generation=generation, executions=sorted(row['execution_id'] for row in target['executions']
                                  if row['execution_id'] in writers and row['generation']==generation))
                continue
            lease = read(path)
            if not isinstance(lease.get('owner'), str) or not lease['owner'] or type(lease.get('expires_at')) not in (int, float):
                raise ValueError('lease owner/expiry missing')
            result.update(owner=lease['owner'], pid=lease.get('pid'), process_start=lease.get('process_start'),
                          expires_at=lease['expires_at'], expired=lease['expires_at'] <= instant,
                          owner_process_state=runtime.owner_state(lease))
            from workflow_observation import process_facts
            health = process_facts(lease, runtime.pid_identity(lease['pid'])
                if lease.get('pid') and lease.get('process_start') else None, instant)
            result.update({key: health.get(key) for key in ('reason', 'reason_codes', 'business_authority_state',
                'heartbeat_state', 'activity_source_state', 'checked_at')})
            session, turn = lease.get('manual_session_id'), lease.get('manual_turn_id')
            manual = (isinstance(session, str) and bool(session) and isinstance(turn, str) and bool(turn)
                      and lease['owner'] == 'manual:'+hashlib.sha256((session+'\0'+turn).encode()).hexdigest())
            # A matching manual tuple distinguishes a gateway conversation; it
            # does not grant authority or prove that its native turn is active.
            result['binding_type'] = ('manual_native_identity_recorded' if manual else
                                      'cron_worker' if lease.get('pid') and runtime.is_cron_worker(lease['pid']) else
                                      'unverified')
            if lease.get('pid') and not manual:
                gateway = runtime.gateway_pid()
                if gateway and lease['pid'] == gateway:
                    result['binding_type'] = 'invalid_cron_gateway_binding'
                    fault('observer_invalid_cron_gateway_binding', path, role=role,
                          owner=lease['owner'], pid=lease['pid'], process_start=lease.get('process_start'))
            result['state'] = ('expired_owner_alive' if result['expired'] and result['owner_process_state'] == 'alive'
                               else 'unverified' if result['owner_process_state'] == 'unverified' else 'observed')
            if result['owner_process_state'] == 'unverified':
                fault('observer_lease_owner_unverified', path, role=role, owner=lease['owner'])
            if result['state'] == 'expired_owner_alive' and result['business_responsibility'] != 'released_verified':
                identity = {key: result.get(key) for key in ('project_root', 'role', 'owner', 'pid', 'process_start')}
                identity['work'] = {key: checkpoint.get(key) for key in ('work_id', 'run_id', 'task', 'attempt')}
                # The exact lease owner/start and work identify the incident;
                # changing sampled execution membership does not recover it.
                identity['generation'] = target['generation']
                identity['pool_path'] = str(pool_path)
                age = instant-lease['expires_at']
                snapshot['anomalies'].append({'type': 'lease_expired_owner_alive', 'identity': digest(identity),
                    'severity': 3 if age >= 3600 else 2 if age >= 900 else 1,
                    'role': role, 'source': str(path), 'expired_at': lease['expires_at'],
                    'owner_process_state': 'alive', 'current_work': result['current_work'],
                    'business_responsibility': result['business_responsibility'],
                    'instruction': '租约已失效而owner进程仍存活；不得推定停工、抢占或启动第二owner。'})
        except (OSError, ValueError, TypeError, KeyError) as exc:
            fault('observer_role_source_unverified', source,
                  role=role, error=type(exc).__name__)
    snapshot['sources'] = sorted(set(snapshot['sources']))
    snapshot['legacy_classification_version'] = 2
    snapshot['classification_complete'] = executions_verified and scope_scan_verified
    snapshot['health'] = 'unverified' if snapshot['faults'] else 'observed'
    return snapshot


def observe_pool_leases(pool_path, *, home=None, state_dir=None, clock=time.time):
    """Persist mechanical anomaly transitions and separate delivery receipts."""
    home = Path(home or os.environ.get('HERMES_HOME') or Path.home()/'.hermes').resolve()
    base = Path(state_dir) if state_dir else home/'cache/role-lease-monitor'/digest(str(Path(pool_path).resolve()))
    with runtime.locked(base/'monitor.lock'):
        snapshot = collect_pool_leases(pool_path, home=home, clock=clock)
        try:
            cursor = runtime.load(base/'state.json', {}) or {}
            if not isinstance(cursor, dict) or not isinstance(cursor.get('active', {}), dict):
                raise ValueError('invalid monitor cursor')
            if any(not isinstance(key, str) or not isinstance(row, dict)
                   or not isinstance(row.get('episode'), str) or type(row.get('severity')) is not int
                   for key, row in cursor.get('active', {}).items()):
                raise ValueError('invalid monitor incident identity')
        except (OSError, ValueError, TypeError) as exc:
            path = base/'state.json'
            raw = path.read_bytes() if path.is_file() else b''
            archive = base/'recovery'/('cursor-'+hashlib.sha256(raw).hexdigest()+'.raw')
            if not archive.exists():
                archive.parent.mkdir(parents=True, exist_ok=True)
                archive.write_bytes(raw)
            cursor = {}
            fault = {'type': 'observer_cursor_unverified', 'source': str(path), 'error': type(exc).__name__}
            snapshot['faults'].append(fault)
            snapshot['anomalies'].append({**fault, 'identity': digest(fault), 'severity': 1})
            snapshot['health'] = 'unverified'
        # Recent display rows can omit an old closure. Resolve a bounded page
        # of already-active incident identities by primary key, never history.
        closure_after = ''
        if snapshot.get('observation_schema') == 2 and cursor.get('active'):
            import workflow_diagnostics
            page = workflow_diagnostics.query_closures(pool_path, list(cursor['active'].values()),
                after=cursor.get('write_closure_after',''), limit=128)
            closure_after = page['next_after']
            snapshot['write_closure_page'] = {key:page[key] for key in
                ('source_state','queried','next_after','has_more')}
            snapshot['observation_closures'] = list({row['identity']:row for row in
                [*snapshot.get('observation_closures',[]),*page['closures']]}.values())
            if page['source_state'] == 'DEGRADED':
                fault = {'type':'observer_write_closure_source_unverified',
                    'source':str(workflow_diagnostics.store_path(pool_path)),
                    'reason_codes':['WRITE_CLOSURE_SOURCE_UNAVAILABLE']}
                snapshot['faults'].append(fault)
                snapshot['anomalies'].append({**fault,'identity':digest(fault),'severity':1})
                snapshot['health'] = 'unverified'
        from workflow_observation import step
        if snapshot.get('legacy_classification_version') == 2 and snapshot.get('classification_complete'):
            snapshot['observation_reclassifications'] = [key for key,row in cursor.get('active',{}).items()
                if row.get('type')=='observer_active_execution_without_lease' and row.get('identity_version') != 2]
        snapshot['source_states'] = source_states(snapshot, cursor, snapshot['at'])
        next_cursor, transitions = step(cursor, snapshot, snapshot['at'])
        next_cursor['write_closure_after'] = closure_after
        snapshot['activity'] = next_cursor.get('activity_states', {})
        if any(item.get('active') for item in snapshot['activity'].values()):
            snapshot['health'] = 'unverified'
        snapshot['anomalies'] = list(next_cursor['active'].values())
        from workflow_terminal_observation import layers
        snapshot['health_layers']=layers(snapshot)
        snapshot['source_states'] = next_cursor['source_states']
        quiet = activity_report.quiet(datetime.fromtimestamp(snapshot['at'], ZoneInfo('Asia/Shanghai')))
        for event in transitions:
            payload = {**event, 'generated_at': snapshot['at'], 'pool_path': snapshot['pool_path']}
            record = {'schema_version': 1, 'event': payload,
                      'generation_receipt': {'state': 'GENERATED', 'at': snapshot['at'], 'sha256': digest(payload)},
                      'delivery_receipt': {'state': 'QUIET_DEFERRED' if quiet else 'PENDING',
                                           'reason': 'night_quiet_hours' if quiet else 'awaiting_existing_notification_transport'}}
            path = base/'outbox'/(event['event_id']+'.json')
            if not path.exists():
                from monitor_queue import enqueue
                enqueue(path, record)
        runtime.save(base/'snapshot.json', snapshot)
        runtime.save(base/'state.json', next_cursor)
        return {**snapshot, 'transitions': transitions, 'outbox': str(base/'outbox'), 'quiet_hours': quiet}


def drain_monitor_notifications(outbox, send, *, at=None, **budgets):
    """Use the existing notification transport; never retry uncertain delivery.

    ``send(event)`` must return a dict with ``confirmed=True`` for a successful
    receipt. Tests inject a recorder; this module never selects a group/API.
    """
    from monitor_notifications import drain
    return drain(outbox, send, at=at, **budgets)


def monitor_sender(pool_path, *, transport=None):
    """Bind mechanical alerts to existing role chats and existing Hermes send.

    Constructing this closure sends nothing. The receipt drainer owns quiet
    hours, a persisted UNKNOWN before send, and at-most-once attempts.
    """
    pool_path = Path(pool_path).resolve()
    if transport is None:
        from runtime_tools import send_claim_notice
        transport = send_claim_notice
    def send(event):
        if event.get('pool_path') != str(pool_path):
            raise ValueError('MONITOR_POOL_IDENTITY_MISMATCH')
        pool = json.loads(text_read(pool_path))
        if (pool.get('schema_version') != 1 or set(pool.get('roles', {})) != set(ROLES)
            or type(pool.get('generation')) is not int):
            raise ValueError('MONITOR_DESTINATION_UNVERIFIED')
        role = event.get('role') if event.get('role') in ROLES else 'COORDINATOR'
        chat = pool['roles'][role].get('chat_id')
        if not isinstance(chat, str) or not chat or any(ord(char) < 32 for char in chat):
            raise ValueError('MONITOR_DESTINATION_UNVERIFIED')
        if event.get('delivery_group_key'):
            from monitor_queue import digest as queue_digest
            if event['delivery_group_key'] != queue_digest(['chat',chat]):
                raise ValueError('MONITOR_DESTINATION_CHANGED_AFTER_RESERVATION')
        state = {'first': '发现异常', 'escalated': '异常升级', 'recovered': '已恢复',
                 'closed_with_work': '随工作终止关闭', 'closed_with_execution': '随执行结束关闭'}.get(event.get('transition'), '待核实')
        text = ('运行观察：'+state+'\n角色：'+role+'\n类型：'+plain(event.get('type'))+
            '\n来源：'+plain(event.get('source'))+'\n观察编号：'+plain(event.get('event_id')))
        if event.get('type') == 'observer_history_summary':
            text = ('运行观察：历史记录接续摘要\n角色：'+role+
                '\n历史记录：'+str(event.get('member_count',0))+' 条'+
                '\n当前核对：'+plain(event.get('current_state'))+
                '\n分类：'+plain(event.get('current_counts',event.get('counts',{})),500)+
                '\n摘要编号：'+plain(event.get('event_id'))+
                '\n历史恢复记录不代表当前已恢复；不在当前活动范围不等于问题未解决，快照过期不等于事件未核实。')
        for label,key in (('发生时间','oldest_generated_at' if event.get('summary_key') else 'occurred_at'),
                          ('观测时间','observed_at'),('发送时间','delivery_started_at')):
            value=event.get(key)
            if type(value) in (int,float):
                value=datetime.fromtimestamp(value,ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')
            text += '\n'+label+'：'+plain(value or '未知')
        reasons = event.get('reason_codes', [])
        if reasons:
            text += '\n原因：'+'；'.join(plain(value, 140) for value in reasons)
        if event.get('type') == 'OPERATION_OBSERVATION_OVERDUE':
            text += '\n含义：超过观察宽限期仍无已核验的结束回执；需核查耗时及回执，不等于卡死或业务失败。'
        elif event.get('type') == 'ACTIVITY_SOURCE_UNVERIFIABLE':
            text += '\n含义：活动观测存在覆盖或回执缺口，不代表模型请求失败或执行停工。'
        if event.get('transition') != 'recovered':
            text += '\n请核对原生执行与业务事实；保留原owner，当前观察不授权接管或重试。'
        receipt = transport({'chat_id': chat, 'text': text, 'event_id': event.get('event_id')})
        confirmed = isinstance(receipt, dict) and receipt.get('delivered') is True and bool(receipt.get('message_id'))
        return {'confirmed': confirmed, 'message_id': receipt.get('message_id') if isinstance(receipt, dict) else None,
                'role': role, 'chat_id': chat}
    return send


def observation_tick(home, pool_path, *, at=None, sender=None):
    """Existing scheduler hook: at most one mechanical sample per 60 seconds.

    No model or default transport. The cache lock also coalesces independent
    native ticks; an injected existing sender can consume the same outbox.
    ``at`` may be an aware datetime or a Unix timestamp for isolated checks.
    """
    home, pool_path = Path(home).resolve(), Path(pool_path).resolve()
    if isinstance(at, datetime):
        if at.tzinfo is None:
            raise ValueError('monitor tick requires an aware datetime')
        instant = at.timestamp()
    elif at is None:
        instant = time.time()
    elif type(at) in (int, float):
        instant = float(at)
    else:
        raise ValueError('monitor tick time must be an aware datetime or timestamp')
    base = home/'cache/role-lease-monitor'/digest(str(pool_path))
    with runtime.locked(base/'tick.lock'):
        try:
            tick = runtime.load(base/'tick.json', {}) or {}
            previous = tick.get('sampled_at') if isinstance(tick, dict) else None
        except (OSError, ValueError, TypeError):
            previous = None
        elapsed = instant-previous if type(previous) in (int, float) else None
        if elapsed is not None and 0 <= elapsed < 60:
            result = {'sampled': False, 'reason': 'sample_interval_not_elapsed',
                      'sampled_at': previous, 'outbox': str(base/'outbox')}
        else:
            result = observe_pool_leases(pool_path, home=home, clock=lambda: instant)
            result.update(sampled=True, reason='mechanical_observation_only')
            runtime.save(base/'tick.json', {'sampled_at': instant, 'health': result['health']})
    if sender is not None:
        result['deliveries'] = drain_monitor_notifications(result['outbox'], sender,
            at=datetime.fromtimestamp(instant, ZoneInfo('Asia/Shanghai')))
    return result


def ocr_process_state(state, spec):
    """Verify worker disappearance without systemd polling or trusting a TTL."""
    if state['state'] != 'RUNNING':
        return 'not_applicable'
    pid = state.get('pid')
    if pid is None:
        return 'unbound'
    if type(pid) is not int or pid <= 0:
        raise ValueError('OCR worker PID is invalid')
    identity = runtime.pid_identity(pid)
    if identity is None:
        return 'exited_or_reused'
    if identity == 'UNVERIFIABLE':
        return 'unverified'
    if state.get('process_start_ticks'):
        return 'alive' if identity == str(state['process_start_ticks']) else 'exited_or_reused'
    # Legacy v1 runs recorded a unique dedicated unit cgroup but no worker
    # start ticks. PID absence is sufficient to flag interruption; PID presence
    # alone is not sufficient to claim liveness after PID reuse.
    cgroup, unit = state.get('cgroup'), spec.get('unit')
    if isinstance(cgroup, str) and isinstance(unit, str) and ('/'+unit) in cgroup:
        try:
            actual = Path(f'/proc/{pid}/cgroup').read_text().strip()
            return 'alive' if actual == cgroup.strip() else 'exited_or_reused'
        except (FileNotFoundError, ProcessLookupError):
            return 'exited_or_reused'
        except OSError:
            return 'unverified'
    return 'unverified'


def ocr_observations(root, selected_scope=None):
    """Read bounded OCR metadata, never invoke OCR or infer an acceptance.

    Old runs in reviews with the supported frozen spec are included even if
    their launcher predates the runtime index. All inputs are project-confined;
    an unreadable registered run remains an actionable error, not "no change".
    Ordinary batch progress and log growth do not enter the notification key.
    """
    def confined(path):
        path = Path(path)
        path = (root/path if not path.is_absolute() else path).resolve()
        if not path.is_relative_to(root):
            raise ValueError('OCR observation path outside project: ' + str(path))
        return path

    def object_read(path):
        path = confined(path)
        source = str(path.relative_to(root))
        try:
            if path.stat().st_size > 4*1024*1024:
                raise ValueError('metadata exceeds 4 MiB')
            value = json.loads(text_read(path))
            if not isinstance(value, dict):
                raise ValueError('expected JSON object')
            return value
        except (OSError, ValueError, TypeError) as exc:
            raise ValueError('OCR observation unavailable: ' + source + ': ' + short(str(exc), 100)) from exc

    registered = {}
    for index in sorted((root/'inbox/control/runtime/ocr').glob('*.json')):
        record = object_read(index)
        if not isinstance(record.get('run'), str) or not record['run']:
            raise ValueError('OCR index needs run path: ' + str(index.relative_to(root)))
        directory = confined(record['run'])
        if directory in registered and registered[directory].get('unit') != record.get('unit'):
            raise ValueError('OCR conflicting runtime index: ' + str(index.relative_to(root)))
        registered[directory] = record
    directories = set(registered)
    for spec_path in sorted((root/'reviews').rglob('spec.json')):
        # Supported legacy means the same v1 frozen OCR contract without an
        # index. Other review tools' valid JSON specs are outside this observer.
        spec = object_read(spec_path)
        if spec.get('schema') == 'coordinator.ocr-run/v1':
            directories.add(confined(spec_path.parent))

    active_states = {'PREPARED', 'SUBMITTED', 'RUNNING'}
    known_states = active_states | {'AWAITING_DECISION', 'REVIEW_COMPLETE', 'LAUNCH_FAILED',
        'CANCELLED', 'HARD_DEADLINE', 'REVIEW_INCOMPLETE', 'BLOCKED', 'UNKNOWN', 'INTERRUPTED', 'FAILED'}
    observations, watch = [], {}
    for directory in sorted(directories):
        spec = object_read(directory/'spec.json')
        state_path = directory/'state.json'
        source = str(state_path.relative_to(root))
        if spec.get('schema') != 'coordinator.ocr-run/v1':
            raise ValueError('OCR unsupported registered spec: ' + str(directory.relative_to(root)))
        if not isinstance(spec.get('project'), str) or Path(spec['project']).resolve() != root:
            raise ValueError('OCR spec project mismatch: ' + source)
        for field in ('run_id', 'review_identity', 'base', 'head', 'task', 'attempt'):
            if not isinstance(spec.get(field), str) or not spec[field]:
                raise ValueError('OCR spec needs ' + field + ': ' + source)
        if selected_scope is not None:
            from requirement_scope import task_allowed
            if not task_allowed(selected_scope, spec['task']):
                continue
        state = object_read(state_path)
        if state.get('state') not in known_states:
            raise ValueError('OCR unknown lifecycle state: ' + source)
        for field in ('run_id', 'unit'):
            if field in state and state[field] != spec.get(field):
                raise ValueError('OCR state identity mismatch (' + field + '): ' + source)
        if directory in registered and registered[directory].get('unit') not in (None, spec.get('unit')):
            raise ValueError('OCR index unit mismatch: ' + source)
        batches = state.get('batches', [])
        if not isinstance(batches, list) or any(not isinstance(item, dict) for item in batches):
            raise ValueError('OCR invalid batch results: ' + source)
        for batch in batches:
            if (not isinstance(batch.get('id'), str) or not batch['id']
                    or batch.get('state') not in known_states
                    or not isinstance(batch.get('files'), list)
                    or not all(isinstance(item, str) for item in batch['files'])):
                raise ValueError('OCR invalid batch identity/state/files: ' + source)
        # On an active run, completing a normal batch is progress, not a reason
        # to start a coordinator worker. A failed batch is immediately actionable
        # even before the worker's final overall-state write.
        results = [{k: batch[k] for k in ('id', 'files', 'state', 'returncode', 'session_file', 'error') if k in batch}
                   for batch in batches if (state['state'] not in active_states
                       or batch.get('state') not in active_states | {'REVIEW_COMPLETE'})]
        identity = {k: spec.get(k) for k in ('run_id', 'review_identity', 'task', 'attempt', 'base', 'head')}
        process = ocr_process_state(state, spec)
        effective_state = ('INTERRUPTED' if process == 'exited_or_reused'
                           else 'UNKNOWN' if process == 'unverified' else state['state'])
        semantic = {**identity, 'spec_sha256': digest(spec), 'state': effective_state,
                    'recorded_state': state['state'], 'worker_process_state': process,
                    'gate_decision': state.get('gate_decision', 'NOT_EVALUATED'),
                    'error': state.get('error'), 'batches': results}
        watch[source] = digest(semantic)
        observations.append({**identity, 'source': source, 'state': effective_state,
                             'recorded_state': state['state'], 'worker_process_state': process,
                             'gate_decision': state.get('gate_decision', 'NOT_EVALUATED'),
                             'error': state.get('error'), 'batches': results,
                             'ended_at': state.get('ended_at'), 'updated_at': state.get('updated_at'),
                             'action': 'Review actual findings and coverage; result readiness is not PASS.'})
    return observations, watch


def collect(root, role, *, publish_indexes=True):
    root, binding = runtime.binding(root)
    if role not in binding['roles']:
        raise ValueError('role not bound')
    # An explicitly accepted README-only requirement is a planning input, not
    # a broken dispatch. Preserve absence as a fact until the coordinator
    # creates the actual task table; never invent rows or acknowledge work.
    todo_exists = (root/'TODO.md').exists()
    todo = text_read(root/'TODO.md') if todo_exists else ''
    taskrows = rows(todo) if todo_exists else {}
    scope = None
    if binding.get('requirement_authorization', {}).get('requirement_root'):
        from requirement_scope import task_scope, task_allowed
        scope = task_scope(root)
        if scope is not None:
            taskrows = {task: value for task, value in taskrows.items() if task_allowed(scope, task)}
    def selected_task(row, source):
        if scope is None:
            return True
        if not isinstance(row.get('task'), str) or not row['task'].strip():
            raise ValueError('兼容需求范围无法核验：'+source+'缺少明确task身份')
        return task_allowed(scope, row['task'])
    from progress_projection import _owner_role
    control = binding.get('control')
    idle, reasons = ((False, []) if role == 'COORDINATOR' else
                     report_gate.routing(root, role) if todo_exists else
                     (False, ['TODO.md 尚未创建，等待协调者准备需求']))
    # Only roles still present in the project binding participate in the
    # fingerprint; after a role migration (REBIND) the unbound roles'
    # documents are owned by the new project and must not crash the poll.
    selected = [role] if role != 'COORDINATOR' else [r for r in ROLES if r in binding['roles']]
    watch = {}
    if binding.get('role_pool'):
        authorization = binding.get('requirement_authorization') or {}
        watch['role-pool:selection'] = digest({
            'pool': binding['role_pool'],
            'requirement_id': authorization.get('requirement_id'),
            'requirement_root': authorization.get('requirement_root'),
            'request_id': authorization.get('request_id'),
        })
    if scope is not None:
        watch['role-pool:task-scope'] = digest(scope)
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
    from context_input import pending_continuations
    continuations = [item for item in pending_continuations(root, role)
                     if selected_task(item, 'continuation')]
    for continuation in continuations:
        watch[continuation['source']] = digest({k:v for k,v in continuation.items() if k != 'source'})
    for who in selected:
        for operation in operation_states(root, who):
            if not selected_task(operation, 'operation'):
                continue
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
        if not selected_task(notice, 'task observation'):
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
    if todo_exists:
        watchfile(root/'TODO.md')
    else:
        watch['TODO.md'] = None
    if scope is not None:
        watch['TODO.md'] = digest(taskrows)
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
            watch[source] = archival_session_fingerprint(root, who, text_read(session), source, exclusions)
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
    ocr_runs = []
    if role == 'COORDINATOR':
        ocr_runs, ocr_watch = (ocr_observations(root, selected_scope=scope) if scope is not None
                               else ocr_observations(root))
        watch.update(ocr_watch)
    legacy = {who: runtime.legacy_busy(binding, who) for who in selected}
    # Only bound-liveness states (alive / exited_or_reused) are meaningful for
    # wake-up: an unbound owner is a volatile cron job name that appears and
    # expires every poll, and 'none' vs 'unverified' flips with it. Normalizing
    # both to {} keeps real worker-death transitions waking the model while
    # lease bookkeeping stops doing so.
    from role_status import DELIVERED, TERMINAL, has_released_slot
    def unfinished_owner(who):
        checkpoint = runtime.load(root/f'inbox/control/runtime/checkpoints/{who}.json', {}) or {}
        # A completed delivery waiting for adjudication has no role action to
        # resume merely because its short-lived cron worker exited. Keep the
        # actual lease visible, and still watch new dispatches/results normally.
        role_continuations = (continuations if who == role else
                              [item for item in pending_continuations(root, who)
                               if selected_task(item, 'continuation')])
        if any(item.get('runnable_now', item.get('runnable')) is True for item in role_continuations):
            return True
        if any(item.get('role') == who and item.get('status') in {'PENDING', 'SUBMITTED', 'RUNNING', 'UNKNOWN'}
               for item in operations):
            return True
        if scope is not None and not task_allowed(scope, checkpoint.get('task')):
            return False
        if str(checkpoint.get('status', '')).upper() not in DELIVERED | TERMINAL | {'IDLE', 'IDLE_WAITING'}:
            return True
        if who != 'COORDINATOR' and has_released_slot(checkpoint, root):
            return False
        identity = checkpoint.get('event_id')
        if not isinstance(identity, str) or not identity.startswith(who+':'):
            return True
        source = 'events/'+who+'/'+hashlib.sha256(identity.encode()).hexdigest()+'.json'
        if checkpoint.get('event') != source:
            return True
        event = runtime.load(root/source, {}) or {}
        # A label alone is insufficient: require the actual stable delivery
        # event, including its evidence, to match this exact checkpoint.
        completed = (event.get('schema_version') == 1 and event.get('kind') == 'business'
                     and event.get('role') == who and event.get('event_id') == identity
                     and event.get('status') in DELIVERED | TERMINAL
                     and bool(event.get('evidence'))
                     and all(checkpoint.get(key) == value for key, value in event.items()))
        return not completed
    health_fp = {who: proc for who, proc in health.items()
                 if (proc.get('state') == 'exited_or_reused' or proc.get('reason') in
                     ('lease_expired_owner_alive', 'heartbeat_stale_owner_activity_unverified'))
                 and unfinished_owner(who)}
    # Expires naturally without a file mutation. Status reads the existing ledger, never calls Codex.
    strong = None
    if role == 'COORDINATOR':
        from strong_review import Ledger
        strong = Ledger().status(str(root))
    own_checkpoint = runtime.load(root/f'inbox/control/runtime/checkpoints/{role}.json', {}) or {}
    if scope is not None and not task_allowed(scope, own_checkpoint.get('task')):
        own_checkpoint = {}
    review_semantic = ({k: strong.get(k) for k in ('authorization', 'grant_id', 'expires_at', 'active_calls', 'last_call')}
                       if strong else None)
    if strong and own_checkpoint.get('status') == 'WAITING_REVIEW_QUOTA':
        review_semantic['quota_available'] = strong.get('used_last_60_minutes', 0) < strong.get('limit', 5)
    semantic = {'control': control, 'binding': binding['roles'][role], 'watch': watch,
                'health': health_fp, 'legacy': legacy,
                'review': review_semantic}
    fingerprint = hashlib.sha256(json.dumps(semantic, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    relevant = []
    for key, cells in taskrows.items():
        if role == 'COORDINATOR' or _owner_role(cells[-1]) == role or any(m in cells[-1] for m in MARKERS[role]):
            from progress_projection import TERMINAL, PAUSED
            if cells[1] not in TERMINAL | PAUSED:
                relevant.append({'task': key, 'title':short(cells[2],160),'state': short(cells[1],80), 'progress': short(cells[-2],320), 'owner': short(cells[-1],100)})
    latest = {}
    if scope is not None:
        events = [event for event in events if task_allowed(scope, event.get('task'))]
        from progress_projection import TASK_PATTERN
        dispatches = [dispatch for dispatch in dispatches
                      if any(task_allowed(scope, task) for task in re.findall(
                          TASK_PATTERN, dispatch['source']+'\n'+dispatch.get('summary', '')))]
    for row in sorted(events, key=event_order):
        latest[row['role']] = {k: short(v,320 if k == 'summary' else 180) for k,v in row.items() if k != 'sources'}
        latest[row['role']]['sources'] = row['sources']
    latest_dispatch = {}
    for row in sorted(dispatches, key=lambda d: d['mtime']):
        latest_dispatch[row['role']] = row
    from role_status import refresh_dispatch_index
    for who in selected if publish_indexes else ():
        if who == 'COORDINATOR':
            continue
        # Source discovery remains the existing observer's responsibility. Keep
        # uncertain old cards visible; only explicit acceptance/closure removes one.
        current_dispatches = []
        for entry in dispatches:
            if entry['role'] != who:
                continue
            path = root/entry['source']
            if entry['status'].upper() in report_gate.TERMINAL or report_gate.accepted_dispatch(root, path):
                continue
            current_dispatches.append(entry)
        refresh_dispatch_index(root, who, current_dispatches,
                               operations=[operation for operation in operations if operation.get('role') == who])
    sources = {'TODO.md': (datetime.fromtimestamp((root/'TODO.md').stat().st_mtime,
                 ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds') if todo_exists else None)}
    snapshot = {'schema_version': 1, 'project': str(root), 'role': role, 'observed_at': stamp(), 'control': control,
                'confirmed_idle': idle, 'classification_reasons': [short(r) for r in reasons[:6]],
                'tasks': relevant, 'events': latest, 'event_conflicts': event_conflicts,
                'dispatches': latest_dispatch, 'processes': health,
                'sources': sources, 'strong_review': strong, 'fingerprint': fingerprint}
    snapshot['coordinator_requests'] = [{'request_id': r['request_id'], 'received_at': r['received_at'],
                                        'source': r['source'], 'summary': short(r['text'],180)} for r in requests]
    snapshot['operations'] = operations
    snapshot['task_observations'] = task_observations
    snapshot['ocr_runs'] = ocr_runs
    snapshot['continuations'] = continuations
    if role == 'COORDINATOR' and binding['roles'][role].get('coordinated_projects'):
        from coordination_inputs import collect_coordinated_inputs, context_view
        coordinated = collect_coordinated_inputs(root, binding, collect)
        if coordinated['projects']:
            watch.update(coordinated['watch'])
            # Recompute with the namespaced subscription facts; the target
            # collector never publishes indices or changes ownership/ACKs.
            semantic['watch'] = watch
            fingerprint = digest(semantic)
            snapshot['fingerprint'] = fingerprint
            snapshot['coordinated_inputs'] = context_view(coordinated)
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
                changes = (['TODO.md', 'inbox/PROTOCOL.md']
                           + [v['source'] for v in snapshot['dispatches'].values()]
                           + [v['source'] for v in snapshot['events'].values()]
                           + [v['source'] for v in snapshot.get('ocr_runs', [])])
            # Durable, coalesced notification queue; never a second task-state store.
            # An independent scanner can enqueue changes while this role's native job
            # is busy. Repeated observations do not consume them or launch a worker.
            pending = state.get('pending') or {}
            wake = (current != ack.get('fingerprint') or bool(pending)
                    or bool(snapshot.get('coordinator_requests'))
                    or any(item.get('runnable_now', item.get('runnable')) is True
                           for item in snapshot.get('continuations', [])))
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
            if wake and owner_blocks_new_execution(snapshot.get('processes',{}).get(role,{})):
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
                    'instruction': '读取简短快照与变更来源（event:ROLE:ID 通过快照 events/sources 定位）；处理后重新poll，核验新业务输入，再ACK准确的最终指纹。纯观察且无变化时仅用固定助手记录运行时观察，不追加会话或业务事件。原有ACK、租约和门禁继续有效。北京时间21:00–08:00照常工作并落盘，禁止主动群通知，最终输出[SILENT]。执行者、测试者白天总结时点为11:00、15:00、18:00；协调者白天每20分钟状态快报，08:00夜间汇总。对用户的说明和总结使用中文。'}
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
                busy=owner_blocks_new_execution(process_health(runtime.load(runtime.lease_path(root,role),{}) or {}))
            except (OSError,ValueError,KeyError,TypeError):
                busy=True  # Unknown ownership must not wake a second business owner.
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
    result, seen, pools = [], set(), set()
    default_pool = Path(registry).resolve().parent/'role-pool.json'
    if default_pool.is_file():
        pools.add(default_pool)
    for entry in entries:
        try:
            if not isinstance(entry, dict):
                raise ValueError('Invalid registry entry')
            root, binding = runtime.binding(entry['project_root'])
            bound_pool = default_pool
            if isinstance(binding.get('role_pool'), dict) and binding['role_pool'].get('path'):
                bound_pool = Path(binding['role_pool']['path']).resolve()
                pools.add(bound_pool)
            if root in seen:
                continue
            seen.add(root)
            from workflow_runtime import active
            if bound_pool.is_file() and active(bound_pool):
                result.append({'project':str(root),'mode':'ACTIVE','wakeAgent':False,
                               'reason':'business_ledger_observation_only'})
                continue
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
    for pool_path in sorted(pools):
        monitor = observe_pool_leases(pool_path, home=Path(registry).resolve().parent)
        result.append({'mode': 'lease_monitor', 'wakeAgent': False, 'reason': 'mechanical_observation_only',
                       'health': monitor['health'], **monitor_fault_summary(monitor),
                       'transition_count': len(monitor['transitions']), 'outbox': monitor['outbox']})
    return result


def _pool_adapter(pool_path):
    """The standalone scanner uses the same admission gate as cron workers."""
    home = Path(os.environ.get('HERMES_HOME') or Path.home()/'.hermes')
    core = Path.home()/'.hermes/hermes-agent'
    if str(core) not in sys.path:
        sys.path.append(str(core))
    from cron.role_pool_scheduler import CronRolePoolAdapter
    return CronRolePoolAdapter(pool_path, home=home)


def pool_poll(pool_path, role, *, reconcile=True):
    """Resolve one accepted pool target; never wake an obsolete job snapshot."""
    pool_path = Path(pool_path).resolve()
    try:
        from workflow_runtime import active, open_ledger
        if active(pool_path):
            hint = open_ledger(pool_path).admission_hint(role)
            return {'checked_at':stamp(),'role':role,'mode':'ACTIVE',
                    'wakeAgent':hint['can_start_model'],'reason':hint['reason'],
                    'source':str(pool_path.with_suffix('.runtime')/'business.sqlite3')}
        module = runtime.requirement_module()
        # A cron worker already reconciled and froze its scope before launching
        # this script. Independent scans reconcile without borrowing that owner.
        if reconcile and not os.environ.get('HERMES_ROLE_POOL_PATH'):
            module.reconcile(pool_path, scheduler_adapter=_pool_adapter(pool_path), apply=True)
        scope = module.resolve_role(pool_path, role)
        if scope.get('state') != 'ACTIVE':
            return {'checked_at': stamp(), 'role': role, 'wakeAgent': False,
                    'reason': 'role_pool_'+str(scope.get('state', 'unknown')).lower(),
                    'role_pool': scope}
        frozen = os.environ.get('HERMES_ROLE_POOL_PATH')
        if not frozen and module.status(pool_path).get('pending'):
            # Independent scans may collect input while a selection waits, but
            # must not advertise an obsolete root as runnable business work.
            return {'checked_at': stamp(), 'role': role, 'wakeAgent': False,
                    'reason': 'role_pool_selection_pending', 'role_pool': scope}
        if frozen:
            if (Path(frozen).resolve() != pool_path
                    or os.environ.get('HERMES_ROLE_NAME') != role
                    or os.environ.get('HERMES_ROLE_POOL_GENERATION') != str(scope['generation'])
                    or Path(os.environ.get('HERMES_ROLE_PROJECT_ROOT', '')).resolve()
                       != Path(scope['project_root']).resolve()
                    or os.environ.get('HERMES_ROLE_REQUIREMENT_ID') != str(scope.get('requirement_id') or '')
                    or (os.environ.get('HERMES_ROLE_REQUIREMENT_ROOT')
                        and Path(os.environ['HERMES_ROLE_REQUIREMENT_ROOT']).resolve()
                        != Path(scope.get('requirement_root') or scope['project_root']).resolve())):
                return {'checked_at': stamp(), 'role': role, 'wakeAgent': False,
                        'reason': 'role_pool_frozen_scope_changed', 'role_pool': scope}
        module.validate_role_scope(pool_path, role, scope['project_root'], scope['generation'])
        result = observe(Path(scope['project_root']), role)
        if scope.get('preparation_required'):
            snapshot = runtime.load(Path(result['snapshot']), {}) if result.get('snapshot') else {}
            if role != 'COORDINATOR' and not snapshot.get('dispatches'):
                # Keep the observer's pending input intact. A subsequent real
                # dispatch is still observed; waiting does not consume an ACK.
                result.update(wakeAgent=False, reason='role_pool_waiting_requirement_preparation')
            elif role == 'COORDINATOR':
                result['instruction'] = ('当前需求准备材料尚未齐全：'+
                    '、'.join(scope.get('missing_documents') or [])+
                    '。先依据需求README及原授权准备规划、协议与实际任务，脚本未生成派单。'+
                    result.get('instruction', ''))
        return {**result, 'role_pool': scope,
                'instruction': ('本轮只处理已明确接收的需求 '+str(scope.get('requirement_id'))+
                                '；需求包目录 '+str(scope.get('requirement_root') or scope['project_root'])+
                                '；业务根目录 '+scope['project_root']+'；代际 '+str(scope['generation'])+
                                '。后台沿用该需求原授权范围，不适用群聊本轮全部权限门槛。'+
                                result.get('instruction', ''))}
    except (OSError, ValueError, KeyError, TypeError, ImportError) as error:
        # Malformed pool/transition state is a control-plane failure, never a
        # reason to fall back to a hard-coded previous project or wake a model.
        return {'checked_at': stamp(), 'role': role, 'wakeAgent': False,
                'reason': 'role_pool_scope_unverified', 'error': type(error).__name__}


def monitor_fault_summary(monitor):
    from collections import Counter
    faults=monitor['faults']
    return {'faults': faults[:16], 'fault_count':len(faults),
            'faults_truncated':len(faults)>16,
            'fault_counts':dict(sorted(Counter(row['type'] for row in faults).items())),
            'full_snapshot':str(Path(monitor['outbox']).parent/'snapshot.json')}


def scan_pool(pool_path):
    """Reconcile accepted control requests and sample only current role roots."""
    pool_path = Path(pool_path).resolve()
    monitor = observe_pool_leases(pool_path)
    from workflow_runtime import active
    if active(pool_path):
        return [{'mode':'ACTIVE','wakeAgent':False,'reason':'business_ledger_observation_only',
                 'health':monitor['health'],**monitor_fault_summary(monitor),
                 'transition_count':len(monitor['transitions']),'outbox':monitor['outbox']}]
    try:
        module = runtime.requirement_module()
        module.reconcile(pool_path, scheduler_adapter=_pool_adapter(pool_path), apply=True)
    except (OSError, ValueError, KeyError, TypeError, ImportError) as error:
        return [{'wakeAgent': False, 'reason': 'role_pool_reconcile_unverified',
                 'error': type(error).__name__}]
    return [pool_poll(pool_path, role, reconcile=False) for role in ROLES] + [
        {'mode': 'lease_monitor', 'wakeAgent': False, 'reason': 'mechanical_observation_only',
         'health': monitor['health'], **monitor_fault_summary(monitor),
         'transition_count': len(monitor['transitions']), 'outbox': monitor['outbox']}]


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
    p.add_argument('action', choices=['poll','pool-poll','ack','snapshot','report','idle-report','scan','register-observation'])
    p.add_argument('--project', type=Path)
    p.add_argument('--role', choices=ROLES)
    p.add_argument('--registry', type=Path)
    p.add_argument('--pool-state', '--pool', dest='pool_state', type=Path)
    p.add_argument('--fingerprint')
    p.add_argument('--registration', type=Path)
    p.add_argument('--token-file', type=Path)
    a=p.parse_args()
    if a.action == 'pool-poll' or (a.action == 'poll' and a.pool_state):
        if not a.pool_state or not a.role:
            p.error('pool-poll requires --pool-state and --role')
        print(json.dumps(pool_poll(a.pool_state, a.role), ensure_ascii=False))
        return
    if a.action == 'scan':
        if not a.registry and not a.pool_state:
            p.error('scan requires --registry or --pool-state')
        result = scan_pool(a.pool_state) if a.pool_state else scan_registry(a.registry)
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
    if a.action == 'report' and a.role == 'COORDINATOR':
        from role_status import collect_role_statuses, render_status_line
        print(render_status_line(collect_role_statuses(a.project)))
        return
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
