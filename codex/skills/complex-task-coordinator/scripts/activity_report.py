#!/usr/bin/env python3
"""无模型的活动观察、分时段总结与夜间汇总；不写业务状态或发送消息。"""
import argparse
from copy import deepcopy
from datetime import datetime, timedelta
from functools import lru_cache
import hashlib
import importlib.util
import json
import re
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

import role_runtime as runtime

TZ = ZoneInfo('Asia/Shanghai')
LABELS = {'COORDINATOR': '协调者', 'A': '执行者 A', 'B': '执行者 B', 'C': '执行者 C', 'TESTER': '测试者'}
CATEGORIES = {'working': '开发/测试/协调阶段', 'waiting': '等待任务/外部处理/审核', 'blocked': '阻塞', 'idle': '确认空闲', 'unknown': '待补证据'}
MAX_GAP = 600
POLICY = Path('/home/wang/.hermes/role-reporting-policy.json')
TIME_INDEX = Path.home() / '.hermes/runtime/role-time.db'
CRON_EXECUTIONS = Path.home() / '.hermes/cron/executions.db'


@lru_cache(maxsize=1)
def time_accounting_module():
    candidates = (Path(__file__).resolve().parents[3] / 'runtime/time_accounting.py',
                  Path('/home/wang/work/hermes-role-runtime/time_accounting.py'))
    source = next((path for path in candidates if path.is_file()), None)
    if source is None:
        raise OSError('时间证据模块尚未安装')
    spec = importlib.util.spec_from_file_location('role_activity_time_accounting', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def local_now():
    return datetime.now(TZ)


def quiet(at=None):
    at = (at or local_now()).astimezone(TZ)
    return at.hour >= 21 or at.hour < 8


def event_state(status):
    s = str(status or '').upper()
    if s in {'RUNNING', 'ACK', 'ACKED', 'ACKED_RUNNING', 'IN_PROGRESS', 'DEVELOPING', 'TESTING', 'REWORK', 'FIXING', 'STARTED', 'PREP', 'PREPARING', 'VERIFYING'}:
        return 'working'
    if 'BLOCK' in s or s in {'NEEDS_REVIEW', 'WAITING_AUTH'}:
        return 'blocked'
    if any(part in s for part in ('WAIT', 'PENDING', 'DELIVER', 'PASS', 'DONE', 'MERGED', 'ACCEPTED')):
        return 'waiting'
    if s in {'OBSERVING', 'MONITORING', 'IDLE', 'IDLE_POLL', 'NO_CHANGE', 'POLL_NO_CHANGE', 'NO_ACTION'}:
        # These are documented waiting/observation phases. An IDLE claim alone
        # does not establish that no valid assignment or active work exists.
        return 'waiting'
    return 'unknown'


def summary_text(value):
    """Legacy prose is evidence text, never a source for reconstructing event time."""
    return re.sub(r'(?<!\d)\d{1,2}:\d*[xX?？][\dxX?？]*', '时间未精确记录', str(value or ''))


def sample(snapshot, tick=None):
    role = snapshot['role']
    event = snapshot.get('events', {}).get(role, {})
    health = snapshot.get('processes', {}).get(role, {}).get('state')
    state = event_state(event.get('status'))
    reason = 'event_status:' + str(event.get('status') or 'missing')
    if str(event.get('status') or '').upper() == 'IDLE':
        reason = 'reported_idle_assignment_or_owner_not_verified'
    dispatch = snapshot.get('dispatches', {}).get(role)
    new_dispatch = False
    if dispatch and str(dispatch.get('status', '')).upper() not in {
            'ACCEPT', 'ACCEPTED', 'DONE', 'MERGED', 'COMPLETED', 'CLOSED', 'CANCELLED', 'CANCELED', 'REPLACED'}:
        try:
            evidence_time=event.get('occurred_at') or event.get('observed_at')
            new_dispatch = not event or dispatch['mtime'] > parse_at(evidence_time).timestamp()
        except (ValueError, KeyError, TypeError):
            new_dispatch = True
    if snapshot.get('error') or any('unavailable' in r or 'absent' in r for r in snapshot.get('classification_reasons', [])):
        state = 'unknown'
        reason = 'source_unavailable:' + str(snapshot.get('error') or '; '.join(snapshot.get('classification_reasons', [])))
    elif snapshot.get('confirmed_idle') and health != 'alive':
        state = 'idle'
        reason = 'documents_confirm_no_pending_assignment_or_active_owner'
    elif role == 'COORDINATOR' and not snapshot.get('tasks') and (tick or {}).get('reason') == 'checked_no_actionable_change' and health != 'alive':
        state = 'idle'
        reason = 'coordinator_no_open_task_or_new_input'
    elif new_dispatch:
        state = 'unknown'
        reason = 'new_dispatch_not_yet_matched_by_current_role_checkpoint'
    elif state == 'unknown' and role == 'COORDINATOR':
        state = 'waiting'
    row = {'at': snapshot['observed_at'], 'category': state, 'role': role,
           'task': str(event.get('task') or ','.join(t['task'] for t in snapshot.get('tasks', [])[:4])),
           'attempt': str(event.get('attempt') or ''), 'summary': summary_text(event.get('summary'))[:320],
           'source': event.get('source') or f'inbox/control/runtime/snapshots/{role}.json',
           'basis': 'script_observation', 'occurred_at': event.get('occurred_at'),
           'observed_at': snapshot['observed_at'], 'recorded_at': event.get('recorded_at'),
           'raw_status': event.get('status'), 'classification_reason': reason,
           'confirmed_idle': bool(snapshot.get('confirmed_idle')),
           'owner_process_state': health, 'classification_reasons': snapshot.get('classification_reasons', []),
           'dispatch_source': (dispatch or {}).get('source'), 'dispatch_mtime': (dispatch or {}).get('mtime')}
    root = Path(snapshot['project'])
    at = datetime.fromisoformat(row['at']).astimezone(TZ)
    path = root / f'inbox/control/runtime/activity/{at:%Y-%m-%d}/{role}.jsonl'
    with runtime.locked(path.with_suffix('.lock')):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a') as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
    return row


def window(kind, at):
    at = at.astimezone(TZ)
    if kind == 'morning':
        end = at.replace(hour=8, minute=0, second=0, microsecond=0)
        if at < end:
            end -= timedelta(days=1)
        return end - timedelta(hours=11), end
    hours = [h for h in (11, 15, 18) if at.hour >= h]
    if not hours:
        raise ValueError('当前尚未到白天总结时点')
    hour = max(hours)
    start_hour = {11: 8, 15: 11, 18: 15}[hour]
    return at.replace(hour=start_hour, minute=0, second=0, microsecond=0), at.replace(hour=hour, minute=0, second=0, microsecond=0)


def parse_at(value):
    at = datetime.fromisoformat(str(value))
    if at.tzinfo is None:
        raise ValueError('活动时间缺少时区')
    return at.astimezone(TZ)


def recover_legacy_status(root, role, row):
    """Only recover an old mapper omission from the exact contemporaneous event.

    A mutable snapshot is never accepted. File mtime is a rejection check for a
    subsequently changed source, never an occurrence timestamp or duration.
    """
    import role_observer as observer
    if row.get('category') != 'unknown' or row.get('basis') != 'script_observation':
        return
    reason = row.get('classification_reason', '')
    if reason.startswith(('source_unavailable', 'new_dispatch')):
        return
    raw_status = str(row.get('raw_status') or '').upper()
    if not raw_status:
        try:
            source = Path(str(row.get('source') or ''))
            allowed = {('events', role), ('inbox', 'events', role)}
            if source.is_absolute() or source.parts[:-1] not in allowed or source.suffix not in {'.json', '.md'}:
                raise ValueError('source_is_not_this_roles_event')
            path = root / source
            if not path.resolve().is_relative_to(root.resolve()):
                raise ValueError('source_escapes_project')
            if path.resolve().relative_to(root.resolve()).parts[:-1] not in allowed:
                raise ValueError('source_resolves_to_another_roles_event')
            event = observer.event_info(path, root, include_observation=True)
            if row.get('role') != role or event.get('role', role) != role:
                raise ValueError('event_role_mismatch')
            if any(str(event.get(key) or '') != str(row.get(key) or '') for key in ('task', 'attempt')):
                raise ValueError('event_task_or_attempt_mismatch')
            expected = summary_text(observer.short(event.get('summary'), 320))[:320]
            if not expected or summary_text(row.get('summary'))[:320] != expected:
                raise ValueError('event_summary_mismatch')
            event_time = parse_at(event.get('occurred_at') or event.get('observed_at'))
            if event_time > row['_at']:
                raise ValueError('event_time_after_sample')
            if event.get('recorded_at') and parse_at(event['recorded_at']) > row['_at']:
                raise ValueError('event_recorded_after_sample')
            if path.stat().st_mtime >= row['_at'].timestamp() + 1:
                raise ValueError('source_modified_after_sample')
            raw_status = str(event.get('status') or '').upper()
            row['legacy_status_evidence'] = {'source': str(source), 'occurred_or_observed_at': event_time.isoformat(),
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                'basis': 'role_task_attempt_summary_and_preexisting_event_time_match'}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            row['classification_reason'] = 'legacy_status_recovery_refused:' + str(exc)
            return
    if raw_status == 'ACKED_RUNNING':
        row['category'] = 'working'
        row['raw_status'] = raw_status
        row['classification_reason'] = 'legacy_status_mapping_recovered:ACKED_RUNNING'
    elif raw_status in {'IDLE', 'OBSERVING', 'MONITORING'}:
        row['category'] = 'waiting'
        row['raw_status'] = raw_status
        row['classification_reason'] = ('legacy_status_mapping_recovered:reported_idle_assignment_or_owner_not_verified'
            if raw_status == 'IDLE' else 'legacy_status_mapping_recovered:external_observation_or_waiting')


def points(root, role, start, end):
    """读取原始观察与实质事件，缺记录时保持未知，不倒填工时。"""
    import role_observer as observer
    rows, events, errors = [], [], []
    date = (start - timedelta(seconds=MAX_GAP)).date()
    while date <= end.date():
        path = root / f'inbox/control/runtime/activity/{date}/{role}.jsonl'
        if path.exists():
            for line in path.read_text().splitlines():
                try:
                    row = json.loads(line)
                    row['_at'] = parse_at(row['at'])
                    if row['_at'] <= end and row['_at'] >= start - timedelta(seconds=MAX_GAP):
                        recover_legacy_status(root, role, row)
                        rows.append(row)
                except (ValueError, KeyError, TypeError):
                    errors.append(str(path.relative_to(root)) + ': 存在损坏记录')
        date += timedelta(days=1)
    seen = set()
    for folder in (root / 'events' / role, root / 'inbox/events' / role):
        if not folder.exists():
            continue
        for path in folder.iterdir():
            if path.suffix not in ('.json', '.md') or not path.is_file():
                continue
            try:
                event = observer.event_info(path, root)
                if not event:
                    continue
                # 文件mtime不冒充事件实际发生时间。
                time_value=event.get('occurred_at') or event.get('observed_at')
                at = parse_at(time_value)
                key = event.get('event_id') or event['source']
                if start <= at < end and key not in seen:
                    seen.add(key)
                    events.append(event)
                    rows.append({'_at': at, 'category': event_state(event.get('status')),
                                 'task': str(event.get('task') or ''), 'attempt': str(event.get('attempt') or ''),
                                 'summary': summary_text(event.get('summary'))[:320], 'source': event['source'],
                                 'basis': 'recorded_event' if event.get('occurred_at') else 'event_observation',
                                 'occurred_at': event.get('occurred_at'), 'observed_at': event.get('observed_at'),
                                 'recorded_at': event.get('recorded_at'), 'raw_status': event.get('status'),
                                 'classification_reason': ('reported_idle_assignment_or_owner_not_verified' if str(event.get('status')).upper() == 'IDLE' else 'recorded_event_status:' + str(event.get('status') or 'missing'))})
            except (OSError, ValueError, KeyError, TypeError):
                errors.append(str(path.relative_to(root)) + ': 时间或内容待核对')
    rows.sort(key=lambda row: row['_at'])
    return rows, events, sorted(set(errors))


def intervals(rows, start, end):
    """观察间隔超过10分钟的部分标未知；边界事件不计入上一段。"""
    segments = []

    def append(a, b, row=None, gap_reason=None, previous=None):
        if b <= a:
            return
        item = {'start': a.isoformat(), 'end': b.isoformat(), 'category': (row or {}).get('category', 'unknown'),
                'task': (row or {}).get('task', ''), 'attempt': (row or {}).get('attempt', ''),
                'summary': (row or {}).get('summary', ''), 'sources': [(row or {})['source']] if (row or {}).get('source') else [],
                'raw_status': (row or {}).get('raw_status'),
                'reason': gap_reason or (row or {}).get('classification_reason') or 'legacy_sample_without_classification_diagnostics',
                'recovery_sources': [s for s in ((row or {}).get('source'), (previous or {}).get('source')) if s]}
        if item['category'] == 'unknown':
            item['recovery_sources'] += ['inbox/control/runtime/activity/<date>/<role>.jsonl',
                                        'events/<role>/', 'inbox/events/<role>/', '~/.hermes/cron/executions.db', '~/.hermes/logs/agent.log*']
        if (row or {}).get('legacy_status_evidence'):
            item['legacy_status_evidence'] = row['legacy_status_evidence']
        if segments and all(segments[-1][k] == item[k] for k in ('category', 'task', 'attempt', 'summary', 'raw_status', 'reason')) and segments[-1]['end'] == item['start']:
            segments[-1]['end'] = item['end']
            segments[-1]['sources'] = sorted(set(segments[-1]['sources'] + item['sources']))
        else:
            segments.append(item)

    before = [r for r in rows if r['_at'] <= start]
    prev = before[-1] if before else None
    cursor = start
    for row in [r for r in rows if start < r['_at'] < end] + [{'_at': end}]:
        until = min(row['_at'], end)
        expiry = prev['_at'] + timedelta(seconds=MAX_GAP) if prev else cursor
        known_end = max(cursor, min(until, expiry))
        append(cursor, known_end, prev)
        append(known_end, until, gap_reason='sample_expired_after_600_seconds' if prev else 'no_observation_at_window_start', previous=prev)
        cursor, prev = until, row
    totals = dict.fromkeys(CATEGORIES, 0)
    for seg in segments:
        totals[seg['category']] += (parse_at(seg['end']) - parse_at(seg['start'])).total_seconds()
    return segments, totals


def summarize(root, role, start, end, *, timing_index=None, executions_db=None):
    root = Path(root)
    rows, events, errors = points(root, role, start, end)
    segments, totals = intervals(rows, start, end)
    delivered = [e for e in events if any(p in str(e.get('status', '')).upper() for p in ('DELIVER', 'PASS', 'DONE', 'MERGED', 'COMPLETED', 'ACCEPTED'))]
    gaps = [s for s in segments if s['category'] == 'unknown']
    reasons = sorted(set(s['reason'] for s in gaps))
    result = {'role': role, 'start': start.isoformat(), 'end': end.isoformat(), 'totals_seconds': totals,
            'segments': segments, 'events': events, 'delivery_records': delivered, 'unverified_sources': errors,
            'unclassified_intervals': gaps, 'classification_reasons': reasons}
    try:
        from report_gate import task_rows
        result['task_functions'] = {key: cells[2] for key, cells in task_rows((root/'TODO.md').read_text()).items()}
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        result['task_functions'] = {}
    evidence_errors, cron_records = [], []
    try:
        accounting = time_accounting_module()
        try:
            _, binding = runtime.binding(root)
            jobs = {item['poll_job_id']: who for who, item in binding['roles'].items() if item.get('poll_job_id')}
            cron_records = accounting.read_cron_records(executions_db or CRON_EXECUTIONS, jobs, start, end)
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            evidence_errors.append({'dimension': 'role_task_dispatch', 'reason': 'cron_or_role_binding_unavailable:' + type(exc).__name__,
                'recovery_sources': [str(root/'ROLE-BINDINGS.json'), str(executions_db or CRON_EXECUTIONS)]})
        result['time_accounting'] = accounting.summarize(timing_index or TIME_INDEX, role, start, end,
            business_segments=segments, cron_records=cron_records, project=root.resolve())
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        result['time_accounting'] = {'dimensions_are_additive': False, 'dimensions': {}}
        evidence_errors.append({'dimension': 'client_model_wait', 'reason': 'timing_index_or_module_unavailable:' + type(exc).__name__,
            'recovery_sources': [str(timing_index or TIME_INDEX), '~/.hermes/logs/agent.log*']})
    result['time_accounting']['collection_errors'] = evidence_errors
    return result


def duration(seconds):
    return f'{round(seconds / 60, 1):g}分钟'


def category_label(category, role):
    if category == 'working':
        return {'COORDINATOR':'协调处理中', 'TESTER':'独立测试阶段',
                'A':'开发/自测阶段', 'B':'开发/自测阶段', 'C':'开发/自测阶段'}.get(role, '处理中')
    return CATEGORIES[category]


def reason_text(value):
    value = str(value or '')
    recorded_status = {'recorded_event_status:NIGHT_SUMMARY': '夜间汇总事件未定义业务阶段',
                       'recorded_event_status:SUPERSEDED_A_DISPATCH': '旧派单替换事件未定义业务阶段',
                       'recorded_event_status:DISPATCHED_TESTER_VERIFY': '测试派发事件未定义业务阶段'}
    if value in recorded_status:
        return recorded_status[value]
    if 'reported_idle' in value:
        return '角色已报告空闲；派单/占用尚未确认，不计确认空闲'
    if value == 'sample_expired_after_600_seconds':
        return '上一观察超过10分钟，缺少连续状态证据'
    if value == 'no_observation_at_window_start':
        return '时段起点没有有效观察'
    if value.startswith('new_dispatch'):
        return '新派单尚未匹配角色检查点'
    if value.startswith('source_unavailable'):
        return '数据源缺失或采集失败'
    if value.startswith('legacy_status_recovery_refused'):
        return '历史事件身份或时间未通过核对，未回填阶段'
    if value.startswith('legacy_sample'):
        return '历史观察未保存分类决策'
    return '业务状态尚未分类'


def timeline(item, limit=6):
    segments = item['segments']
    if len(segments) > limit:
        selected = segments[:limit // 2] + segments[-limit // 2:]
    else:
        selected = segments
    entries = []
    for s in selected:
        note = '（' + reason_text(s.get('reason')) + '）' if s['category'] == 'unknown' or 'reported_idle' in s.get('reason', '') else ''
        entries.append(f"{parse_at(s['start']):%Y-%m-%d %H:%M} +08:00–{parse_at(s['end']):%Y-%m-%d %H:%M} +08:00 {category_label(s['category'], item.get('role'))} {s['task']} {s['attempt']} {summary_text(s['summary'])[:90]}{note}".strip())
    if len(segments) > limit:
        entries.insert(limit // 2, '中间时段见完整记录')
    return '；'.join(entries)


def display_segments(item):
    """Compact display only. Artifact segments keep original summaries/evidence."""
    grouped = []
    for source in item['segments']:
        current = {'start': source['start'], 'end': source['end'], 'category': source['category'],
                   'task': source.get('task', ''), 'reasons': [source.get('reason', '')]}
        if grouped and all(grouped[-1][key] == current[key] for key in ('category', 'task')) and grouped[-1]['end'] == current['start']:
            grouped[-1]['end'] = current['end']
            grouped[-1]['reasons'] = sorted(set(grouped[-1]['reasons'] + current['reasons']))
        else:
            grouped.append(current)
    return grouped


def interval_label(segment):
    start, end = parse_at(segment['start']), parse_at(segment['end'])
    seconds = (end-start).total_seconds()
    if start.replace(second=0, microsecond=0) == end.replace(second=0, microsecond=0):
        return f'{start:%Y-%m-%d %H:%M}该分钟内（不足1分钟） +08:00'
    tail = f'{end:%H:%M}' if start.date() == end.date() else f'{end:%Y-%m-%d %H:%M}'
    return f'{start:%Y-%m-%d %H:%M}–{tail}' + ('（不足1分钟）' if seconds < 60 else '') + ' +08:00'


def task_identity(value):
    from progress_projection import TASK_PATTERN
    match = re.search(r'(?<![A-Z0-9_-])(' + TASK_PATTERN + r')(?![A-Z0-9_-])', str(value or ''))
    return match[1] if match else ''


def task_function(item, task, title=None):
    import role_observer as observer
    task = task_identity(task)
    functions = item.get('task_functions', {})
    title = title or functions.get(task)
    if not title:
        parent = next((key for key in sorted(functions, key=len, reverse=True) if task.startswith(key+'-')), None)
        title = functions.get(parent) if parent else None
    label = observer.plain(summary_text(title or ('对应任务功能待核对' if task else '角色观察/待确认阶段')), 65)
    return {'聚合':'整体任务跟进', '子聚合':'整体任务跟进', '叶':'具体功能跟进',
            '叶任务':'具体功能跟进'}.get(label, label)


def delivery_lines(item, limit=3):
    records = item.get('delivery_records', [])
    out = [f'- **本期角色交付/接受记录：** {len(records)}条；任务最终完成仍以验收裁决为准。']
    tasks = {}
    for event in records:
        key = task_identity(event.get('task')) or str(event.get('task') or '')
        tasks.setdefault(key, event)
    for event in list(tasks.values())[:limit]:
        out.append('- **本期角色记录：** ' + task_function(item, event.get('task'), event.get('title')) + '。')
    if len(tasks) > limit:
        out.append(f'- **交付记录补充：** 另{len(tasks)-limit}项，完整集合见证据。')
    return out


def progress_value(fact, default=None):
    return fact.get('value', default) if isinstance(fact, dict) else default


def digest_report_packet(data):
    """Adapt legacy saved snapshots only when the shared evidence is unavailable.

    This temporary view is never persisted or used for acceptance. The caller
    explicitly labels its fallback source; raw snapshots remain in the artifact.
    """
    from progress_projection import state_code, TERMINAL
    packet = deepcopy(data.get('progress_facts') or {})
    shared = isinstance(data.get('progress_facts'), dict)
    packet.setdefault('tasks', {})
    packet.setdefault('roles', {})
    observed = packet.get('observed_at') or data['current'].get(data['role'], {}).get('observed_at') or data['generated_at']
    try:
        packet['observed_at'] = f'{parse_at(observed):%Y-%m-%d %H:%M} +08:00'
    except (ValueError, TypeError):
        packet['observed_at'] = '采集时间待核对'
    packet.setdefault('goal', {'value': '当前整体目标未明确记录', 'source': data['artifact']})
    packet.setdefault('overall', {'current_tasks': {'value': []}})
    packet.setdefault('ocr', {'runs': [], 'declared': []})
    packet.setdefault('strong_review', {})
    def fact(value, role):
        return {'value': value, 'source': data['artifact'], 'observed_at': data['current'].get(role, {}).get('observed_at')}
    if not shared:
        for item in data['roles']:
            role, identities, completed = item['role'], [], []
            snapshot = data['current'].get(role, {})
            for raw in snapshot.get('tasks', []):
                task = task_identity(raw.get('task'))
                if not task:
                    continue
                state = state_code(raw.get('state'))
                packet['tasks'][task] = {key: fact(value, role) for key, value in {
                    'title': task_function(item, task, raw.get('title')), 'state': state,
                    'testing': 'NOT_CONFIRMED', 'merge': 'NOT_CONFIRMED',
                    'dependencies': raw.get('dependencies', '')}.items()}
                (completed if state in TERMINAL else identities).append(task)
            active = (snapshot.get('events', {}).get(role) or {}).get('task')
            primary = active if active in identities else identities[0] if len(identities) == 1 else None
            packet['roles'][role] = {key: fact(value, role) for key, value in {
                'tasks': identities, 'primary_task': primary,
                'related_tasks': [task for task in identities if task != primary], 'recent_completed': completed}.items()}
        strong = data['current'].get('COORDINATOR', {}).get('strong_review') or {}
        packet['strong_review'] = {key: fact(value, 'COORDINATOR') for key, value in strong.items()}
    # Generic old task labels are not useful feature names, in either source.
    for task, row in packet['tasks'].items():
        title = progress_value(row.get('title'))
        if title in {'聚合', '子聚合', '叶', '叶任务'}:
            row['title'] = {**row['title'], 'value': task_function({}, task, title)}
    return packet, shared


def digest_timing_lines(item, snapshot):
    """Keep the existing timing dimensions and exact gaps; no business inference."""
    import role_observer as observer
    lines = []
    for key in CATEGORIES:
        seconds = item['totals_seconds'].get(key, 0)
        if seconds or key == 'idle':
            lines.append(f'**{category_label(key, item["role"])}：** {duration(seconds)}。')
    for segment in display_segments(item):
        lines.append(f"**{interval_label(segment)}：** {category_label(segment['category'], item['role'])} · {task_function(item, segment['task'])}。")
        if segment['category'] == 'unknown':
            reasons = '；'.join(dict.fromkeys(reason_text(code) for code in segment['reasons']))
            lines.append('**该段补证：** ' + observer.plain(reasons, 95) + '；核原始角色事件与派单，入口见证据。')
    if any('reported_idle' in segment.get('reason', '') for segment in item['segments']):
        lines.append('**空闲核对：** 角色曾报告空闲，但派单/占用未确认；上述等待不计确认空闲。')
    accounting = item.get('time_accounting', {})
    dimensions = accounting.get('dimensions', {})
    api = dimensions.get('client_model_wait', {})
    if api.get('observed_seconds') is not None:
        approximate = any(r.get('timing_basis') == 'log_completion_backfill' for r in api.get('requests', []))
        measured = ('约' if approximate else '') + duration(api['observed_seconds'])
        lines.append(f"**模型响应：** {measured}，{len(api.get('requests', []))}条请求" + ('（由历史日志回建）。' if approximate else '（原生请求事件）。'))
        lines.append(f"**未观测到 API 响应占用：** {duration(api.get('not_observed_api_occupancy_seconds', api.get('unobserved_seconds', 0)))}；可含工具执行或没有请求，不等于空闲。")
        if api.get('open_requests'):
            lines.append(f"**未结束请求：** {len(api['open_requests'])}条，未猜测结束时刻；起止补证见证据。")
    else:
        lines.append('**模型响应：** 计时证据尚未取得；需核本地时间索引及 API 日志，不能据此认定空闲。')
    dispatch = dimensions.get('role_task_dispatch', {})
    if dispatch.get('records'):
        lines.extend([f"**调度滞后：** {duration(dispatch['schedule_delay_seconds'])}。",
                      f"**派发等待：** {duration(dispatch['dispatch_wait_seconds'])}。",
                      f"**执行区间：** {duration(dispatch['execution_seconds'])}；含脚本/模型/工具，与阶段重叠。"])
    if accounting.get('collection_errors'):
        missing = '、'.join('角色绑定/cron执行记录' if e['dimension'] == 'role_task_dispatch' else '模型请求计时索引' for e in accounting['collection_errors'])
        lines.append(f'**计时核对：** {missing}暂不可读；具体原因与恢复来源见证据。')
    if item['unverified_sources'] or snapshot.get('error'):
        lines.append('**历史时间待核：** ' + (observer.plain(summary_text(snapshot['error']), 65) if snapshot.get('error') else str(len(item['unverified_sources'])) + '份事件缺少可用时间，已排除时长计算') + '；来源见证据。')
    return lines


def render_digest(data):
    """The common report contract owns every section and role boundary."""
    from report_contract import build_report, render_report
    packet, shared = digest_report_packet(data)
    report = build_report(packet, data['role'])
    sections = {section['key']: section for section in report['sections']}
    coordinator = data['role'] == 'COORDINATOR'
    main = sections['overall' if coordinator else 'own_progress']
    def items(lines, pointer, group=None):
        return [{'text': text, **({'group': group} if group else {}),
                 'sources': [{'source': data['artifact'], 'observed_at': data['generated_at'],
                              'evidence_reference': data['artifact'] + '#/' + pointer}]} for text in lines]
    def insert(section, additions, group=None):
        if group:
            indices = [n for n, item in enumerate(section['items']) if item.get('group') == group]
            end = indices[-1] + 1 if indices else len(section['items'])
            section['items'][end:end] = additions
        else:
            section['items'].extend(additions)
    intro = [f"**统计时段：** {parse_at(data['start']):%Y-%m-%d %H:%M}–{parse_at(data['end']):%Y-%m-%d %H:%M} +08:00。",
             '**报告类型：** ' + ('夜间汇总。' if data['kind'] == 'morning' else '时段总结。'),
             '**计时口径：** 业务阶段按声明和观察估算，含巡检、工具和模型响应，不能当实际编码工时。',
             '**并行统计：** 模型响应含网络、重试、排队与推理，与业务阶段重叠，**不可相加**；不能隔离纯服务器排队。',
             '**当前工作来源：** ' + ('共同进度事实，采集 ' + str(packet['observed_at']) + '；与统计时段分列。' if shared else '未取得共同进度事实，以下为保存快照回退。')]
    main['items'][:0] = items(intro, 'progress_facts')
    for index, item in enumerate(data['roles']):
        who = item['role']
        if not coordinator and who != data['role']:
            continue
        target = main if who == 'COORDINATOR' or not coordinator else sections['tester' if who == 'TESTER' else 'executors']
        group = LABELS[who] if coordinator and who in {'A', 'B', 'C'} else '协调者时段统计' if coordinator and who == 'COORDINATOR' else None
        snapshot = data['current'].get(who, {})
        lines = digest_timing_lines(item, snapshot)
        role_facts = packet['roles'].get(who, {})
        dispatch = progress_value(role_facts.get('dispatch'), {})
        assigned = progress_value(role_facts.get('tasks'), [])
        completed = progress_value(role_facts.get('recent_completed'), [])
        if not assigned and not completed:
            confirmed = snapshot.get('confirmed_idle') is True and not snapshot.get('error') and not dispatch.get('newer_than_activity')
            if confirmed:
                for current_item in target['items']:
                    if current_item['text'].startswith('**当前任务：**') and (not group or current_item.get('group') == group):
                        current_item['text'] = '**当前任务：** 已确认无派单。'
            else:
                lines.append('**当前任务核对：** 当前派单/状态待核。')
        insert(target, items(lines, f'roles/{index}', group), group)
        delivery_target = target if coordinator else sections['own_results']
        insert(delivery_target, items([line.removeprefix('- ') for line in delivery_lines(item)], f'roles/{index}/delivery_records', group), group)
    for index, item in enumerate(data.get('evening_supplement', [])):
        who = item['role']
        if not coordinator and who != data['role']:
            continue
        target = main if who == 'COORDINATOR' or not coordinator else sections['tester' if who == 'TESTER' else 'executors']
        group = LABELS[who] if coordinator and who in {'A', 'B', 'C'} else '协调者时段统计' if coordinator and who == 'COORDINATOR' else None
        insert(target, items([f"**晚间补充（前日18:00–21:00）：** 交付/接受事件{len(item['delivery_records'])}条；确认空闲{duration(item['totals_seconds']['idle'])}。"],
                             f'evening_supplement/{index}', group), group)
    evidence = ['**完整记录：** `' + data['artifact'] + '`。', '**可核验内容：** 完整时间段、原始摘要、事件来源、逐段缺口原因及恢复入口。']
    if packet.get('evidence_reference'):
        evidence.append('**当前工作证据：** `' + packet['evidence_reference'] + '`。')
    if data.get('progress_facts_error'):
        evidence.append('**共同事实待核：** 本次读取失败，已明确按保存快照回退；原因见完整记录。')
    sections['next']['items'].extend(items(evidence, ''))
    return render_report(report)


def build(root, role, kind, at=None, *, timing_index=None, executions_db=None):
    import role_observer as observer
    from runtime_tools import refresh_progress_facts
    root = Path(root).resolve()
    at = at or local_now()
    start, end = window(kind, at)
    selected = runtime.ROLES if kind == 'morning' else (role,)
    current = {}
    for who in selected:
        try:
            current[who] = observer.refresh(root, who)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            current[who] = {'role': who, 'observed_at': at.isoformat(), 'error': str(exc), 'project': str(root)}
            sample(current[who])
    artifact = f'inbox/control/runtime/digests/{end:%Y-%m-%d}/{role}-{kind}-{end:%H%M}.json'
    data = {'schema_version': 2, 'generated_at': at.isoformat(), 'role': role, 'kind': kind,
            'start': start.isoformat(), 'end': end.isoformat(), 'artifact': artifact,
            'roles': [summarize(root, who, start, end, timing_index=timing_index, executions_db=executions_db) for who in selected], 'current': current}
    try:
        data['progress_facts'] = refresh_progress_facts(root, at=at, snapshot=current.get(role) or next(iter(current.values()), None))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        data['progress_facts'] = None
        data['progress_facts_error'] = type(exc).__name__ + ': ' + str(exc)
    if kind == 'morning':
        data['evening_supplement'] = [summarize(root, who, start - timedelta(hours=3), start, timing_index=timing_index, executions_db=executions_db) for who in selected]
    runtime.save(root / artifact, data)
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind', choices=['phase', 'morning'])
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--role', choices=runtime.ROLES, required=True)
    parser.add_argument('--time-index', type=Path, default=TIME_INDEX)
    args = parser.parse_args()
    if quiet():
        return
    if args.kind == 'morning' and args.role != 'COORDINATOR':
        raise ValueError('夜间汇总仅由协调者生成')
    print(render_digest(build(args.project, args.role, args.kind, timing_index=args.time_index)))


if __name__ == '__main__':
    main()
