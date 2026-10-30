"""Read actual dispatch stages and preserve missing/outstanding denominators."""
from collections import defaultdict
import math

STAGES = {
    't_commit': ('commit', 'COMMITTED'), 't_prepared': ('prepared', 'PREPARED'),
    't_enqueued': ('enqueued', 'SUBMITTED'), 't_join': ('join', 'COALESCED'),
    't_dispatch': ('request_entered', 'ENTERED'), 't_claim': ('claim_decided', 'ACQUIRED'),
    't_agent_start': ('agent_started', 'STARTED'), 't_input_ready': ('input_ready', 'READY'),
    't_first_request': ('first_model_request', 'STARTED'),
    't_result_visible': ('result_visible', 'VISIBLE'), 't_end': ('execution_ended', 'ENDED'),
    't_native_return': ('native_return', 'RETURNED'), 't_adapter_finish': ('finish', None),
}


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def timeline(rows, *, now):
    ids = {row['request_id'] for row in rows}
    if len(ids) != 1 or not _finite(now):
        raise ValueError('ONE_REQUEST_AND_FINITE_CLOCK_REQUIRED')
    phases, selected = {}, {}
    for label, (stage, outcome) in STAGES.items():
        events = [row for row in rows if row.get('stage') == stage and
                  (outcome is None or row.get('outcome') == outcome) and _finite(row.get('at'))]
        if events:
            selected[label] = events[0]
            phases[label] = {'state': 'OBSERVED', 'at': events[0]['at'], 'source': stage}
        else:
            phases[label] = {'state': 'UNKNOWN', 'at': None, 'reason': 'STAGE_NOT_MEASURED_OR_NOT_REACHED'}
    durations = {}
    for name, left, right in [('commit_to_dispatch', 't_commit', 't_dispatch'),
            ('dispatch_to_claim', 't_dispatch', 't_claim'),
            ('commit_to_agent_start', 't_commit', 't_agent_start'),
            ('commit_to_result_visible', 't_commit', 't_result_visible'),
            ('native_call', 't_dispatch', 't_native_return'),
            ('adapter_call', 't_prepared', 't_adapter_finish')]:
        start, end = selected.get(left), selected.get(right)
        seconds, basis = None, None
        if start and end:
            if (start.get('pid') == end.get('pid') and _finite(start.get('monotonic_ns')) and
                    _finite(end.get('monotonic_ns'))):
                seconds = (end['monotonic_ns']-start['monotonic_ns'])/1e9
                basis = 'same_process_monotonic'
            elif end['at'] >= start['at']:
                seconds, basis = end['at']-start['at'], 'wall_clock'
        if seconds is not None and seconds < 0:
            seconds, basis = None, None
        durations[name] = {'state': 'OBSERVED' if seconds is not None else 'UNKNOWN',
                           'seconds': seconds, 'basis': basis}
    waiting = not selected.get('t_adapter_finish') and not any(row.get('stage') in ('deferred', 'start_failed') for row in rows)
    prepared = selected.get('t_prepared')
    return {'request_id': next(iter(ids)), 'stages': phases, 'durations': durations,
            'outstanding': waiting,
            'outstanding_age_seconds': max(0, now-prepared['at']) if waiting and prepared else None,
            'deferred': any(row.get('stage') in ('deferred', 'start_failed') for row in rows),
            'claim_outcomes': sorted({row['outcome'] for row in rows if row.get('stage') == 'claim_decided'}),
            'unknown_is_not_success': True}


def _commit_duration(start, end):
    """A joined member cannot inherit an entry that predates its commit."""
    unknown = {'state': 'UNKNOWN', 'seconds': None, 'basis': None}
    if end is None or not _finite(start.get('at')) or not _finite(end.get('at')):
        return {**unknown, 'reason': 'STAGE_NOT_MEASURED_OR_NOT_ATTRIBUTED'}
    identity = ('pid', 'process_start', 'boot_id')
    same_process = all(start.get(key) is not None and start.get(key) == end.get(key)
                       for key in identity)
    if same_process and _finite(start.get('monotonic_ns')) and _finite(end.get('monotonic_ns')):
        seconds = (end['monotonic_ns']-start['monotonic_ns'])/1e9
        basis = 'same_process_monotonic'
    else:
        seconds, basis = end['at']-start['at'], 'wall_clock'
    if seconds < 0:
        return {**unknown, 'reason': 'STAGE_PRECEDES_COMMIT'}
    return {'state': 'OBSERVED', 'seconds': seconds, 'basis': basis}


def _same_scope(left, right):
    return all(left.get(key) == right.get(key) for key in
               ('request_id', 'pool_path', 'generation', 'run_id', 'role', 'work_id', 'job_id'))


def _join_matches(commit, join, event_id, commit_count):
    references = join.get('source_event_ids') or []
    if references:
        return event_id is not None and event_id in references
    for key in ('source', 'source_event_high_water'):
        if commit.get(key) is not None and join.get(key) is not None and commit[key] != join[key]:
            return False
    high_water = (commit.get('source') is not None and commit.get('source') == join.get('source')
                  and commit.get('source_event_high_water') is not None
                  and commit.get('source_event_high_water') == join.get('source_event_high_water'))
    return high_water or commit_count == 1


def _commit_observations(rows, grouped):
    """Keep commit members and pool/event units alongside legacy call units.

    A receipt without a usable pool/event reference remains its own explicit
    input receipt. A later stage is attributed only within the same request and
    scope. Joins additionally need member/high-water evidence, or an unambiguous
    single commit receipt; they never stand in for dispatch or business stages.
    """
    commits = [(index, row) for index, row in enumerate(rows)
               if row.get('stage') == 'commit' and row.get('outcome') == 'COMMITTED']
    counts = defaultdict(int)
    for _, row in commits:
        counts[row['request_id']] += 1
    members, events = [], {}
    metric_names = ('commit_to_dispatch', 'commit_to_join', 'commit_to_agent_start',
                    'commit_to_result_visible')
    for index, commit in commits:
        own = [row for row in grouped[commit['request_id']] if _same_scope(commit, row)]
        pool = commit.get('pool_path')
        event_ids = list(dict.fromkeys(value for value in (commit.get('source_event_ids') or [])
                                     if type(value) in (str, int))) if pool else []
        receipt_ref = commit.get('receipt_id') or 'input_row:' + str(index)
        for event_id in event_ids or [None]:
            key = ('event', pool, type(event_id).__name__, event_id) if event_id is not None else ('receipt', receipt_ref)
            event = events.setdefault(key, {'unit': 'pool_event' if event_id is not None else 'commit_receipt',
                'pool_path': pool, 'event_id': event_id, 'receipt_ref': receipt_ref if event_id is None else None,
                'member_indexes': []})
            stages = {}
            for label, (stage, outcome) in STAGES.items():
                if label in ('t_commit', 't_prepared', 't_enqueued', 't_join'):
                    continue
                stages[label] = next((row for row in own if row.get('stage') == stage and
                                      (outcome is None or row.get('outcome') == outcome)), None)
            joins = []
            for row in own:
                if row.get('stage') != 'join' or row.get('outcome') != 'COALESCED':
                    continue
                if (_join_matches(commit, row, event_id, counts[commit['request_id']])
                        and _commit_duration(commit, row)['state'] == 'OBSERVED'):
                    joins.append(row)
            joined = joins[0] if joins else None
            dispatch = _commit_duration(commit, stages['t_dispatch'])
            durations = {'commit_to_dispatch': dispatch, 'commit_to_join': _commit_duration(commit, joined)}
            for metric, label in (('commit_to_agent_start', 't_agent_start'),
                                  ('commit_to_result_visible', 't_result_visible')):
                durations[metric] = (_commit_duration(commit, stages[label]) if dispatch['state'] == 'OBSERVED'
                                     else {'state': 'UNKNOWN', 'seconds': None, 'basis': None,
                                           'reason': 'NO_ATTRIBUTED_DISPATCH_AFTER_COMMIT'})
            member = {'receipt_ref': receipt_ref, 'input_row_index': index,
                'pool_path': pool, 'event_id': event_id, 'request_id': commit['request_id'],
                'role': commit.get('role'), 'source': commit.get('source'),
                'committed_at': commit.get('at'), 'joined_at': joined.get('at') if joined else None,
                'coalesced': joined is not None, 'durations': durations,
                'owner_read': {'state': 'UNKNOWN', 'reason': 'JOIN_DOES_NOT_PROVE_OWNER_READ'}}
            event['member_indexes'].append(len(members))
            members.append(member)
    # An event with several recorded recipient members is fully measured only
    # when each member is measured. This cannot hide an unknown joined recipient
    # behind a different recipient's quick dispatch.
    for event in events.values():
        own = [members[index] for index in event['member_indexes']]
        event['coalesced'] = any(member['coalesced'] for member in own)
        event['durations'] = {}
        for metric in metric_names:
            values = [member['durations'][metric] for member in own]
            if all(value['state'] == 'OBSERVED' for value in values):
                event['durations'][metric] = {'state': 'OBSERVED',
                    'seconds': max(value['seconds'] for value in values), 'basis': 'maximum_recorded_member_duration'}
            else:
                event['durations'][metric] = {'state': 'UNKNOWN', 'seconds': None,
                    'basis': None, 'reason': 'ONE_OR_MORE_RECORDED_MEMBERS_UNKNOWN'}
    return members, list(events.values()), len(commits), metric_names


def _metrics(samples, names, targets=None):
    metrics = {}
    for metric in names:
        values = sorted(row['durations'][metric]['seconds'] for row in samples
                        if row['durations'][metric]['state'] == 'OBSERVED')
        target = (targets or {}).get(metric)
        if target is not None and (not _finite(target) or target < 0):
            raise ValueError('INVALID_FROZEN_PERFORMANCE_TARGET')
        metrics[metric] = {'requests': len(samples), 'measured': len(values),
            'unknown_or_not_applicable': len(samples)-len(values),
            'p50_seconds': values[math.ceil(len(values)*.5)-1] if values else None,
            'p95_seconds': values[math.ceil(len(values)*.95)-1] if values else None,
            'max_seconds': values[-1] if values else None,
            'target_seconds': target,
            'over_target_count': sum(value > target for value in values) if target is not None else None}
    return metrics


def summarize_dispatch(rows, *, now, targets=None):
    rows = list(rows)
    grouped = defaultdict(list)
    for row in rows:
        grouped[row['request_id']].append(row)
    requests = [timeline(grouped[key], now=now) for key in sorted(grouped)]
    metrics = _metrics(requests, ['commit_to_dispatch', 'dispatch_to_claim', 'commit_to_agent_start',
                                'commit_to_result_visible', 'native_call', 'adapter_call'], targets)
    members, events, commit_receipts, event_metric_names = _commit_observations(rows, grouped)
    member_metrics = _metrics(members, event_metric_names, targets)
    event_metrics = _metrics(events, event_metric_names, targets)
    for name, values in (('commit_members', member_metrics), ('events', event_metrics)):
        for metric in values.values():
            metric['samples'] = metric.pop('requests')
            metric['unit'] = name
    return {'schema_version': 1, 'requests': requests, 'metrics': metrics,
            'commit_members': members, 'commit_member_metrics': member_metrics,
            'events': events, 'event_metrics': event_metrics,
            'commit_denominators': {'commit_receipts': commit_receipts, 'commit_members': len(members),
                'events': len(events), 'coalesced_members': sum(member['coalesced'] for member in members),
                'coalesced_events': sum(event['coalesced'] for event in events)},
            'event_aggregation': 'all recorded members must be observed; maximum member duration',
            'outstanding': sum(row['outstanding'] for row in requests),
            'deferred': sum(row['deferred'] for row in requests),
            'performance_gate': 'TARGETS_NOT_FROZEN' if not targets else 'REQUIRES_COVERAGE_AND_SAMPLE_REVIEW',
            'capacity_domain': 'process role adapter calls; ticker pool and OS workers are separate'}
