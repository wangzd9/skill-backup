"""Versioned, mechanical activity observations and human feedback.

This projection never grants execution authority, retries an operation, calls a
model, or sends a notification. Heartbeats deliberately do not count as activity.
Only derived lifecycle metadata is accepted; command arguments and model bodies
are not sampled. The pure transition is also used by the replay fixtures.
"""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3

RULE_VERSION = 'activity-signals-v2'
LABELS = {'TRUE_POSITIVE', 'FALSE_POSITIVE', 'UNREVIEWED'}
IDENTITY_FIELDS = ('pool', 'generation', 'run_id', 'role', 'work_id', 'task_id',
                   'attempt', 'execution_id', 'execution_generation')


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _epoch(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
    else:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('activity time requires timezone')
        number = parsed.timestamp()
    if not math.isfinite(number):
        raise ValueError('activity time must be finite')
    return number


def _key(identity, kind, member=None):
    return hashlib.sha256(_json([identity, kind, member]).encode()).hexdigest()


def activity_step(prior, observation, now, *, threshold_seconds=None):
    """Return ``(state, transitions)`` using facts at the supplied clock.

    Input: ``identity``, ``current_work_valid``; ``sources`` with source_id,
    check_status=VERIFIED, complete=True, last_verified_at, max_gap_seconds;
    ``lifecycles`` with operation_id/kind/started_at/ended_at and optional
    wait_until; optional checkpoint_at/checkpoint_evidence_ref. All lifecycle
    times must have already been identity-verified by the caller. Each lifecycle
    therefore carries verified=True and source_id referring to a valid source.

    An absent/incomplete/stale source means UNKNOWN, never no activity. The
    caller must enumerate required_sources (normally tools and model_requests),
    so a healthy model collector cannot conceal a missing tool collector.
    Outputs have stable anomaly_id, type, transition, reason_codes and evidence;
    they are notification intentions only. No evidence of recovery is invented
    for an old no-activity episode when the current sources are blind.
    """
    now = _epoch(now)
    if threshold_seconds is not None and (isinstance(threshold_seconds, bool)
            or not isinstance(threshold_seconds, (int, float))
            or not math.isfinite(threshold_seconds) or threshold_seconds <= 0):
        raise ValueError('no-activity threshold must be positive')
    identity = {key: observation.get('identity', {}).get(key) for key in IDENTITY_FIELDS}
    same = isinstance(prior, dict) and prior.get('identity') == identity
    previous = deepcopy(prior.get('active', {})) if same else {}
    facts = {'schema_version': 1, 'rule_version': RULE_VERSION, 'identity': identity,
             'checked_at': now, 'checkpoint_age': None, 'last_activity_at': None,
             'source_coverage': [], 'normal_waits': [], 'in_flight': [], 'reason_codes': [], 'active': {}}
    checkpoint = observation.get('checkpoint_at')
    if checkpoint is not None:
        checkpoint = _epoch(checkpoint)
        if checkpoint > now:
            raise ValueError('checkpoint follows observation')
        facts['checkpoint_age'] = now-checkpoint
    available = {}
    for item in observation.get('sources', []):
        source = {key: item.get(key) for key in ('source_id', 'source_version', 'check_status',
            'complete', 'last_verified_at', 'max_gap_seconds', 'reason_codes', 'evidence_ref',
            'coverage_started_at', 'collector_epoch')}
        source['coverage_grace_until'] = item.get('coverage_grace_until')
        if not isinstance(source['source_id'], str) or not source['source_id']:
            raise ValueError('activity source_id required')
        if source['source_id'] in available:
            raise ValueError('duplicate activity source')
        fresh = False
        if source['last_verified_at'] is not None and source['max_gap_seconds'] is not None:
            checked = _epoch(source['last_verified_at'])
            gap = source['max_gap_seconds']
            if not isinstance(gap, (int, float)) or isinstance(gap, bool) or gap < 0:
                raise ValueError('invalid activity source validity')
            fresh = 0 <= now-checked <= gap
        source['boundaries_verified'] = source['check_status'] == 'VERIFIED' and fresh
        source['valid'] = source['boundaries_verified'] and source['complete'] is True
        # Legacy fixtures supply explicitly complete historical coverage. Native
        # hook collectors must additionally prove the entire inactivity window;
        # installing hooks cannot turn an old checkpoint into inactivity proof.
        source['inactivity_window_verified'] = True
        if 'coverage_started_at' in item:
            start = source['coverage_started_at']
            source['inactivity_window_verified'] = (start is not None and
                0 <= now-_epoch(start) and (threshold_seconds is None or now-_epoch(start) >= threshold_seconds))
        available[source['source_id']] = source
        facts['source_coverage'].append(source)
    required = observation.get('required_sources')
    if not isinstance(required, list) or not required:
        required = ['tools', 'model_requests']
    starting = {name for name in required if available.get(name, {}).get('coverage_grace_until') is not None
                and 0 <= _epoch(available[name]['coverage_grace_until'])-now <= 60
                and available[name].get('reason_codes') in (['TOOL_COLLECTOR_COVERAGE_NOT_STARTED'],
                                                            ['MODEL_COLLECTOR_COVERAGE_NOT_STARTED'])}
    missing = sorted({name for name in required if not available.get(name, {}).get('valid') and name not in starting})
    warming = sorted(name for name in required if available.get(name, {}).get('valid')
                     and not available[name]['inactivity_window_verified'])
    warming = sorted(set(warming) | starting)
    if warming:
        facts['reason_codes'].append('INACTIVITY_WINDOW_WARMING_UP')
    current = {}
    def anomaly(kind, reasons, evidence=(), member=None):
        key = _key(identity, kind, member)
        old = previous.get(key, {})
        current[key] = {'anomaly_id': key, 'type': kind, 'rule_version': RULE_VERSION,
            'identity': identity, 'reason_codes': sorted(set(reasons)), 'evidence_refs': list(evidence),
            'first_seen_at': old.get('first_seen_at', now), 'last_seen_at': now,
            'severity': 1, 'member': member}
    if missing:
        anomaly('ACTIVITY_SOURCE_UNVERIFIABLE', ['source_missing_incomplete_or_stale:'+name for name in missing],
                [available[name]['evidence_ref'] for name in missing if name in available])
        facts['reason_codes'].append('ACTIVITY_SOURCE_UNVERIFIABLE')
    activities = [checkpoint] if checkpoint is not None else []
    open_unknown, ended_operations = False, set()
    for item in observation.get('lifecycles', []):
        if item.get('verified') is not True or not available.get(item.get('source_id'), {}).get('boundaries_verified'):
            continue
        started = _epoch(item['started_at']) if item.get('started_at') is not None else None
        ended = _epoch(item['ended_at']) if item.get('ended_at') is not None else None
        if (started is not None and started > now) or (ended is not None and ended > now):
            raise ValueError('lifecycle follows observation')
        if started is not None and ended is not None and ended < started:
            raise ValueError('lifecycle end precedes start')
        activities.extend(t for t in (started, ended) if t is not None)
        if ended is not None:
            ended_operations.add(item.get('operation_id'))
        if ended is None:
            wait = item.get('wait_until')
            grace = item.get('observation_grace_seconds')
            if wait is None and started is not None and grace is not None:
                if (type(grace) not in (int, float) or not math.isfinite(grace) or grace <= 0
                        or item.get('grace_policy') != 'open-observation/1'):
                    raise ValueError('invalid open observation policy')
                if now-started <= grace:
                    facts['in_flight'].append({**{key: item.get(key) for key in
                        ('operation_id', 'kind', 'source_id', 'started_at', 'evidence_ref', 'grace_policy')},
                        'age_seconds': now-started, 'observation_grace_seconds': grace,
                        'grace_until': started+grace, 'completion_verified': False})
                else:
                    anomaly('OPERATION_OBSERVATION_OVERDUE', ['open_operation_exceeds_observation_grace'],
                            [item.get('evidence_ref')], member=item.get('operation_id'))
                continue
            if started is None or wait is None:
                open_unknown = True
                continue
            wait = _epoch(wait)
            if wait < started:
                raise ValueError('wait contract precedes operation')
            if now <= wait:
                facts['normal_waits'].append({key: item.get(key) for key in
                    ('operation_id', 'kind', 'started_at', 'wait_until', 'evidence_ref')})
            else:
                anomaly('OPERATION_WAIT_EXPIRED', ['registered_operation_wait_contract_expired'],
                        [item.get('evidence_ref')], member=item.get('operation_id'))
    if open_unknown:
        anomaly('ACTIVITY_SOURCE_UNVERIFIABLE', ['open_operation_lifecycle_or_wait_contract_missing'],
                member='open-lifecycle')
        facts['reason_codes'].append('open_operation_lifecycle_or_wait_contract_missing')
    if activities:
        facts['last_activity_at'] = max(activities)
    if threshold_seconds is None:
        facts['reason_codes'].append('POLICY_UNCONFIGURED')
    elif (isinstance(threshold_seconds, bool) or not isinstance(threshold_seconds, (int, float))
          or not math.isfinite(threshold_seconds) or threshold_seconds <= 0):
        raise ValueError('no-activity threshold must be positive')
    elif (observation.get('current_work_valid') is True and not missing and not warming and not open_unknown
          and not facts['normal_waits'] and not facts['in_flight']
          and not any(value['type'] in ('OPERATION_WAIT_EXPIRED', 'OPERATION_OBSERVATION_OVERDUE')
                      for value in current.values())):
        if facts['last_activity_at'] is None:
            anomaly('ACTIVITY_SOURCE_UNVERIFIABLE', ['no_verified_activity_boundary'], member='boundaries')
        elif now-facts['last_activity_at'] > threshold_seconds:
            anomaly('NO_ACTIVITY', ['verified_activity_older_than_policy_threshold'],
                    [observation.get('checkpoint_evidence_ref')])
    transitions = []
    for key, item in current.items():
        if key not in previous:
            transitions.append({**item, 'transition': 'first'})
    for key, old in previous.items():
        if key in current:
            continue
        closing = observation.get('work_terminated') is True
        if old['type'] == 'NO_ACTIVITY' and (missing or open_unknown) and not closing:
            current[key] = old  # blindness cannot establish recovery
            continue
        # Legitimate work termination closes an episode, it is not activity recovery.
        if old['type'] in ('OPERATION_WAIT_EXPIRED', 'OPERATION_OBSERVATION_OVERDUE') and not closing and old.get('member') not in ended_operations:
            current[key] = old
            continue
        if old['type'] == 'NO_ACTIVITY' and not closing:
            fresh_activity = facts['last_activity_at']
            if fresh_activity is None or fresh_activity <= old['last_seen_at']:
                current[key] = old
                continue
        transitions.append({**old, 'transition': 'closed_with_work' if closing else 'recovered',
                            'last_seen_at': now})
    facts['active'] = current
    facts['state'] = ('UNVERIFIABLE' if any(row['type'] == 'ACTIVITY_SOURCE_UNVERIFIABLE' for row in current.values())
                      else 'NO_ACTIVITY' if any(row['type'] == 'NO_ACTIVITY' for row in current.values())
                      else 'WAIT_EXPIRED' if any(row['type'] == 'OPERATION_WAIT_EXPIRED' for row in current.values())
                      else 'OBSERVATION_OVERDUE' if any(row['type'] == 'OPERATION_OBSERVATION_OVERDUE' for row in current.values())
                      else 'IN_FLIGHT' if facts['in_flight']
                      else 'NORMAL_WAIT' if facts['normal_waits'] else 'WARMING_UP' if warming else 'OBSERVED')
    return facts, transitions


def record_feedback(path, *, anomaly_id, rule_version, label, reviewer, at, basis, scope, rule_id=None):
    """Append a human judgment, keeping acknowledgements separate from labels."""
    if label not in LABELS or not all(isinstance(value, str) and value.strip() for value in
                                    (anomaly_id, rule_version, reviewer, basis)) or not isinstance(scope, dict):
        raise ValueError('explicit anomaly, rule, reviewer, basis, scope and feedback label required')
    row = {'anomaly_id': anomaly_id, 'rule_id': rule_id or rule_version, 'rule_version': rule_version,
           'label': label, 'reviewer': reviewer, 'at': _epoch(at), 'basis': basis, 'scope': scope}
    identity = hashlib.sha256(_json(row).encode()).hexdigest()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=1) as db:
        os.chmod(path, 0o600)
        db.execute('CREATE TABLE IF NOT EXISTS feedback (feedback_id TEXT PRIMARY KEY, at REAL NOT NULL, value TEXT NOT NULL)')
        db.execute('INSERT OR IGNORE INTO feedback VALUES (?,?,?)', (identity, row['at'], _json(row)))
    return {**row, 'feedback_id': identity}


def feedback_summary(path, start, end, *, anomalies=(), rule_version=None, scope=None):
    """Report the latest judgment per anomaly inside an explicit rolling window.

    ``anomalies`` supplies the full denominator, including unreviewed episodes.
    Reading/acknowledging a notification never calls record_feedback.
    """
    left, right = _epoch(start), _epoch(end)
    if right <= left:
        raise ValueError('feedback end must follow start')
    latest = {}
    path = Path(path)
    if path.is_file():
        with sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=1) as db:
            for (body,) in db.execute('SELECT value FROM feedback WHERE at>=? AND at<? ORDER BY at,feedback_id', (left,right)):
                row = json.loads(body)
                if (rule_version is None or row['rule_version'] == rule_version) and (scope is None or row['scope'] == scope):
                    latest[row['anomaly_id']] = row
    ids = set(anomalies) | set(latest)
    counts = {key: 0 for key in LABELS}
    for identity in ids:
        counts[latest.get(identity, {}).get('label', 'UNREVIEWED')] += 1
    reviewed = counts['TRUE_POSITIVE'] + counts['FALSE_POSITIVE']
    return {'rule_version': rule_version, 'scope': scope, 'start': left, 'end': right,
            'total': len(ids), 'reviewed': reviewed, 'counts': counts,
            'reviewed_fraction': reviewed/len(ids) if ids else None,
            'false_positive_rate': counts['FALSE_POSITIVE']/reviewed if reviewed else None,
            'automatic_downgrade': False}
