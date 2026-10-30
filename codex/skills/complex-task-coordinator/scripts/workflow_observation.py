"""Pure observation transitions shared by the real monitor and temporal replay.

This projection grants no authority and consumes no business input. Source
recovery is independent: blindness cannot certify recovery of another source.
"""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
from zoneinfo import ZoneInfo

SCHEMA_VERSION = 1
RULE_VERSION = 'ruflo-observation/2'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _rule(facts, events, recovery):
    return {'version': RULE_VERSION, 'facts': facts, 'stages': ['observation'],
            'event_types': events, 'dedup': 'source/work/execution/capability identity',
            'escalation': 'strictly greater evidenced severity', 'recovery': recovery,
            'notification': 'existing monitor outbox; quiet hours defer delivery',
            'authority_effect': 'NONE'}


RULES = {
    'source_health': _rule(['source identity', 'checked_at', 'completeness', 'freshness'],
        ['source_unverified'], 'same source verified; other source faults do not mask it'),
    'write_authority': _rule(['decision receipt', 'same-time authority capture', 'capability'],
        ['WRITE_AUTHORITY_ANOMALY', 'WRITE_AUTHORITY_UNVERIFIED'],
        'new actual COMMITTED write for same identity/capability, or explicit terminated closure'),
    'activity': _rule(['valid current work', 'complete lifecycle coverage', 'configured threshold',
        'last tool/request/checkpoint activity', 'registered wait contract'],
        ['NO_ACTIVITY', 'OPERATION_WAIT_EXPIRED', 'ACTIVITY_SOURCE_UNVERIFIABLE'],
        'verified activity or explicit work termination; heartbeat is never activity'),
    'writer_identity': _rule(['PID/start', 'child scope', 'generation', 'current writer binding'],
        ['observer_execution_identity_unverified', 'observer_business_writer_exited',
         'lease_expired_owner_alive'], 'same source verified; never grants takeover'),
}


def process_facts(lease, actual, now):
    """Legacy lease compatibility; independent facts never revive TTL authority."""
    base = {'state': 'none', 'owner_process_state': 'none', 'reason_codes': [],
            'business_authority_state': 'NOT_CHECKED', 'heartbeat_state': 'NOT_CHECKED',
            'activity_source_state': 'NOT_CHECKED', 'checked_at': now,
            'source': 'legacy_lease_and_pid_start'}
    if not lease:
        return base
    base.update(owner=lease.get('owner'), pid=lease.get('pid'), start=lease.get('process_start'))
    reasons = base['reason_codes']
    if not lease.get('owner'):
        reasons.append('lease_owner_missing')
    if not lease.get('pid') or not lease.get('process_start'):
        reasons.append('process_identity_fields_missing')
        owner = 'unverified'
    elif actual == 'UNVERIFIABLE':
        owner = 'unverified'
        reasons.append('process_identity_unreadable')
    else:
        owner = 'alive' if actual == lease['process_start'] else 'exited_or_reused'
    base.update(owner_process_state=owner, state=owner)
    expires = lease.get('expires_at')
    if type(expires) in (int, float):
        base.update(lease_valid=expires >= now, business_authority_state='EXPIRED' if expires < now else 'LEGACY_LEASE_VALID')
        if expires < now and owner == 'alive':
            reasons.append('lease_expired_owner_alive')
    last = lease.get('last_seen')
    if type(last) in (int, float):
        base['heartbeat_state'] = 'STALE' if now-last > 1800 else 'OBSERVED'
        if now-last > 1800:
            reasons.append('heartbeat_stale_owner_activity_unverified')
    if reasons:
        base['state'] = 'unverified' if owner != 'exited_or_reused' else owner
        base['reason'] = reasons[0]
    return base


def owner_blocks_facts(facts):
    return facts.get('owner_process_state', facts.get('state')) in ('alive', 'unverified')


def rule_registry():
    from activity_signals import RULE_VERSION as activity_version
    from workflow_diagnostics import VERSION as diagnostics_version
    rules = deepcopy(RULES)
    rules['activity']['version'] = activity_version
    rules['write_authority']['version'] = diagnostics_version
    return {'schema_version': SCHEMA_VERSION, 'ruleset_version': RULE_VERSION, 'rules': rules}


def _write_closure(old, proof):
    """Source health/absence alone cannot prove a capability write recovered."""
    from workflow_diagnostics import VERSION
    fields = ('pool','generation','run_id','role','work_id','execution_id',
              'execution_generation','capability')
    if proof.get('identity') != old['identity'] or proof.get('rule_version') != VERSION:
        return False
    if any(old.get(key) is None or proof.get(key) != old[key] for key in fields):
        return False
    if proof.get('transition') == 'closed_with_execution':
        evidence = proof.get('evidence_ref') or {}
        return evidence.get('terminal_verified') is True and evidence.get('terminal_kind') == 'EXECUTION'
    return (proof.get('transition') == 'recovered' and proof.get('status') == 'COMMITTED'
        and proof.get('decision_phase') == 'TRANSACTION_GATE'
        and proof.get('recovery_basis') == 'NEW_BUSINESS_WRITE_SUCCEEDED'
        and bool(proof.get('decision_id')) and proof['decision_id'] != old.get('decision_ref')
        and bool(proof.get('operation_id')) and proof['operation_id'] != old.get('operation_id')
        and type(proof.get('closed_at')) in (int,float)
        and proof['closed_at'] >= old.get('last_seen',old.get('first_seen',float('inf'))))


def step(prior_state, observation, now):
    """Return next projection and notification intents using only supplied facts.

    Activity detection is the same activity_step used by collection. Anomaly
    facts may come from the actual write-decision projection or process source.
    Neither replay nor this transition invokes a business/model/sender API.
    """
    if type(now) not in (int, float) or not math.isfinite(now):
        raise ValueError('OBSERVATION_TIME_INVALID')
    previous = deepcopy((prior_state or {}).get('active', {}))
    sources = deepcopy((prior_state or {}).get('source_states', {}))
    for name, facts in observation.get('source_states', {}).items():
        old = sources.get(name, {})
        sources[name] = {**old, **facts}
        if facts.get('state') == 'OBSERVED':
            sources[name]['last_verified_at'] = facts.get('checked_at', now)
    def is_diagnostic(row):
        return row.get('type') in {'observer_write_diagnostics_unverified',
                                   'write_diagnostics_source_unverified',
                                   'observer_write_closure_source_unverified'}

    def diagnostic_identity(row):
        # The observer fault and the projection fallback describe one source
        # episode. Their payloads (and therefore their old identities) differ,
        # so key them by the stable diagnostics source before notification
        # transitions are calculated. This prevents two alerts for one outage.
        source = str(row.get('source', 'UNKNOWN'))
        return legacy_diagnostic_keys.get(source) or digest(['write-diagnostics-source', source])

    # Old cursors predate source deduplication and notification debounce. Keep
    # their reported episode rather than issuing a new first alert on upgrade.
    legacy_diagnostic_keys = {str(row.get('source', 'UNKNOWN')): key
                              for key, row in previous.items() if is_diagnostic(row)}
    normalized = {}
    for key, row in previous.items():
        if is_diagnostic(row):
            if 'notification_ready_at' not in row:
                row.setdefault('notified_at', row.get('first_seen', now))
            normalized_key = diagnostic_identity(row)
            old = normalized.get(normalized_key)
            if old is not None and 'notified_at' in old:
                continue
            normalized[normalized_key] = {**row, 'identity': normalized_key}
        else:
            normalized[key] = row
    previous = normalized

    current = {row['identity']: deepcopy(row) for row in observation.get('anomalies', [])
               if not is_diagnostic(row)}
    for fault in observation.get('faults', []):
        if not is_diagnostic(fault):
            key = fault.get('identity') or digest(fault)
            current.setdefault(key, {**fault, 'identity': key, 'severity': 1,
                                     'reason_codes': fault.get('reason_codes', [fault['type']])})
    for row in [*observation.get('anomalies', []), *observation.get('faults', [])]:
        if is_diagnostic(row):
            key = diagnostic_identity(row)
            old = current.get(key, {})
            current[key] = {**old, **deepcopy(row), 'identity': key, 'severity': 1,
                'reason_codes': sorted(set(old.get('reason_codes', []) +
                                           row.get('reason_codes', [row['type']])))}
    write_paths = deepcopy((prior_state or {}).get('write_paths', {}))
    write_closures = list(observation.get('observation_closures', []))
    if observation.get('write_decisions'):
        from workflow_diagnostics import (operation_context, build_receipt,
                                           write_path_step, IDENTITY_FIELDS, projection_anomalies)
        for decision in observation['write_decisions']:
            scope = {**decision['identity'], 'pool_path': decision['pool_path']}
            with operation_context(scope, **decision['context']):
                receipt = build_receipt(decision['pool_path'], decision['result'], decision['capture'], at=now)
            key = digest([receipt['pool'], *[receipt.get(name) for name in IDENTITY_FIELDS
                                            if name not in ('task_id', 'attempt')], receipt['capability']])
            value = write_path_step(write_paths.get(key), receipt)
            if value is not None:
                write_paths[key] = value
    if write_paths:
        from workflow_diagnostics import projection_anomalies, projection_closures
        current.update({row['identity']: row for row in projection_anomalies(list(write_paths.values()), 'write-decisions')})
        write_closures.extend(projection_closures(list(write_paths.values())))
        sources['write-decisions'] = {'state': 'OBSERVED', 'checked_at': now}
    activity_states = deepcopy((prior_state or {}).get('activity_states', {}))
    generated_sources = set()
    for activity in observation.get('activities', []):
        from activity_signals import activity_step
        key = digest(activity['identity'])
        facts, _ = activity_step(activity_states.get(key), activity, now,
            threshold_seconds=observation.get('no_activity_seconds'))
        activity_states[key] = facts
        for anomaly_id, value in facts['active'].items():
            current[anomaly_id] = {**value, 'identity': anomaly_id,
                'work_identity': value['identity'], 'source': 'activity:'+key,
                'role': activity['identity'].get('role')}
        sources['activity:'+key] = {'state': 'UNVERIFIED' if facts['state'] == 'UNVERIFIABLE' else 'OBSERVED',
                                  'checked_at': now}
        generated_sources.add('activity:'+key)
    process_states = {}
    for process in observation.get('processes', []):
        facts = process_facts(process.get('lease'), process.get('actual'), now)
        process_states[process['identity']] = {**facts, 'blocks_new_owner': owner_blocks_facts(facts)}
        for reason in facts['reason_codes']:
            key = digest([process['identity'], reason])
            current[key] = {'identity': key, 'type': reason, 'reason_codes': facts['reason_codes'],
                'severity': process.get('severity', 1), 'source': process.get('source', 'process'),
                'facts': facts}
    transitions = []
    terminal_closures=deepcopy((prior_state or {}).get('terminal_service_closures',{}))
    terminal_services={s['action_id']:s for s in observation.get('services',[])
        if s.get('observation')=='terminal_receipt_verified'}
    # SQLite/cache pressure can make a single observer sample temporarily
    # unverifiable. Keep the health fact immediately, but require a stable
    # 120-second episode before notifying and the same stable interval before
    # announcing recovery. This suppresses alert flapping without hiding the
    # source state from read-only health views.
    diagnostic_debounce = 120
    for key, row in list(current.items()):
        old = previous.get(key)
        if is_diagnostic(row):
            reported = old is not None and 'notified_at' in old
            restarted = old is None or (not reported and
                (old.get('clear_candidate_at') is not None or old.get('failure_interrupted')))
            first_seen = now if restarted else old['first_seen']
            row = {**row, 'first_seen': first_seen, 'last_seen': now,
                   'episode': digest([key, now]) if restarted else old['episode'],
                   'notification_ready_at': first_seen + diagnostic_debounce}
            if reported:
                row['notified_at'] = old['notified_at']
            row.pop('clear_candidate_at', None)
            current[key] = row
            if not reported and now >= row['notification_ready_at']:
                transitions.append({**row, 'transition': 'first', 'occurred_at': row['first_seen'],
                                    'observed_at': now, 'event_id': digest([row['episode'], 'first'])})
                current[key]['notified_at'] = now
            continue
        if old is None or row['severity'] > old.get('severity', 0):
            transition = 'first' if old is None else 'escalated'
            episode = old.get('episode') if old else digest([key, now])
            transitions.append({**row, 'occurred_at': old.get('first_seen', now) if old else now,
                                'observed_at': now, 'event_id': digest([episode, transition, row['severity']]),
                                'episode': episode, 'transition': transition})
        current[key] = {**row, 'severity': max(row['severity'], old.get('severity', 0) if old else 0),
                        'first_seen': old.get('first_seen', now) if old else now, 'last_seen': now,
                        'episode': old.get('episode') if old else digest([key, now])}
    for key, old in previous.items():
        if key in current:
            continue
        if old.get('type')=='observer_independent_service_unverified' and old.get('action_id') in terminal_services:
            # Reclassify only on verified durable stop evidence. Archive the
            # original incident without replaying dozens of old recovery alerts.
            terminal_closures[key]={'original':old,'closed_at':now,
                'basis':terminal_services[old['action_id']], 'transition':'terminal_receipt_verified'}
            continue
        if key in observation.get('observation_reclassifications',[]):
            transitions.append({**old,'event_id':digest([old['episode'],'reclassified_unverified']),
                'transition':'reclassified_unverified','severity':0,'occurred_at':now,'observed_at':now,
                'reason_codes':['LEGACY_ACTIVE_CLASSIFICATION_REPLACED_WITH_EVIDENCED_THREE_STATE'],
                'instruction':'旧观察分类已更正；未知scope继续保留，不代表事实恢复或授权接管。'})
            continue
        source = old.get('source_key', old.get('source'))
        source_fact = (sources.get(source) if source in generated_sources or source == 'write-decisions' and write_paths
                       else observation.get('source_states', {}).get(source))
        # A fresh per-source fact can certify just that source. Older callers
        # without source maps retain their conservative global-blindness rule.
        verified = source_fact and source_fact.get('state') == 'OBSERVED'
        if is_diagnostic(old):
            if not verified:
                # Missing/unknown samples prove neither sustained failure nor
                # recovery. Interrupt both stability windows and retain health.
                retained = {**old, 'failure_interrupted': True}
                retained.pop('clear_candidate_at', None)
                current[key] = retained
                continue
            clear_at = old.get('clear_candidate_at')
            if clear_at is None:
                current[key] = {**old, 'clear_candidate_at': now}
                continue
            if now - clear_at < diagnostic_debounce:
                current[key] = old
                continue
            if 'notified_at' not in old:
                # The source recovered before the incident became notifyable;
                # remove the internal health episode without emitting a pair
                # of user-facing messages for a transient blip.
                continue
        needs_explicit = old.get('type', '').startswith(('write_authority_', 'ACTIVITY_', 'NO_ACTIVITY', 'OPERATION_'))
        closed = old.get('work_identity', {}).get('work_id') in observation.get('closed_work_ids', [])
        execution_closed = old.get('work_identity', {}).get('execution_id') in observation.get('closed_execution_ids', [])
        is_write = old.get('type','').startswith('write_authority_')
        explicit_closure = next((row for row in write_closures if row['identity'] == key
            and (not is_write or _write_closure(old,row))), None)
        if is_write and explicit_closure is None:
            current[key] = old
            continue
        if not closed and not execution_closed and not explicit_closure and not verified and (observation.get('faults') or source_fact or needs_explicit):
            current[key] = old
            continue
        transition = (explicit_closure['transition'] if explicit_closure else
                      'closed_with_work' if closed else 'closed_with_execution' if execution_closed else 'recovered')
        transitions.append({**old, 'event_id': digest([old['episode'], transition]),
                            'transition': transition, 'severity': 0,'occurred_at':now,'observed_at':now})
    quiet = not 8 <= datetime.fromtimestamp(now, ZoneInfo('Asia/Shanghai')).hour < 21
    outputs = [{**row, 'rule_version': row.get('rule_version', RULE_VERSION), 'ruleset_version': RULE_VERSION,
                'notification_state': 'QUIET_DEFERRED' if quiet else 'PENDING'} for row in transitions]
    activity_states = {key: value for key, value in activity_states.items()
        if value['identity'].get('work_id') not in observation.get('closed_work_ids', []) and
           value['identity'].get('execution_id') not in observation.get('closed_execution_ids', [])}
    next_state = {'schema_version': SCHEMA_VERSION, 'ruleset_version': RULE_VERSION,
                  'observed_at': now, 'active': current, 'source_states': sources,
                  'activity_states': activity_states, 'process_states': process_states, 'write_paths': write_paths,
                  'terminal_service_closures':terminal_closures}
    return next_state, outputs


def saved_health(home, pool_path, *, now, max_age=180):
    """Check freshness independently and return a bounded, evidence-linked view.

    Every caller (including legacy progress and periodic reports) receives the
    same <8 KiB contract; raw diagnostics remain immutable supplemental evidence.
    """
    from pathlib import Path
    from observation_view import health_view
    path = Path(home)/'cache/role-lease-monitor'/digest(str(Path(pool_path).resolve()))/'snapshot.json'
    try:
        value = json.loads(path.read_text())
        checked = value['at']
        if type(checked) not in (int, float) or not math.isfinite(checked):
            raise ValueError('bad timestamp')
        age = now - checked
        if age < 0 or age > max_age:
            return {'state': 'DEGRADED', 'reason_codes': ['observer_snapshot_stale'],
                    'source': str(path), 'checked_at': now, 'last_verified_at': checked,
                    'age_seconds': age}
        observation = {'state': 'OBSERVED' if value['health'] == 'observed' else 'UNVERIFIED',
                'source': str(path), 'checked_at': now, 'observed_at': checked,
                'source_states': value.get('source_states', {}),
                'reason_codes': sorted({reason for row in value.get('anomalies', [])
                                        for reason in row.get('reason_codes', [row['type']])}),
                'anomalies': value.get('anomalies', []),
                'write_diagnostics': value.get('write_diagnostics'),
                'health_layers':value.get('health_layers'),
                'activity_policy':value.get('activity_policy'),
                'business_progress':value.get('business_progress',[]),
                'activity': value.get('activity', {})}
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {'state': 'DEGRADED', 'reason_codes': ['observer_snapshot_unavailable'],
                'source': str(path), 'checked_at': now, 'error': type(exc).__name__}
    # Artifact failures must remain failures, rather than disguising corrupted
    # supplemental evidence as a successful/stale observer query.
    return health_view(observation, path.parent/'health-views')
