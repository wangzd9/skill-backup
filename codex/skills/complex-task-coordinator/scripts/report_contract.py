"""One role-scoped report view for both model input and mechanical rendering.

This is a pure projection of progress evidence, never a second task-state store.
Business status comes from TODO; activity, review execution and approval remain
separate. Raw technical narratives stay accessible through evidence references.
"""
from datetime import datetime
from pathlib import Path
import re
from zoneinfo import ZoneInfo

ROLES = {'COORDINATOR': '协调者', 'A': '执行者 A', 'B': '执行者 B',
         'C': '执行者 C', 'TESTER': '测试者'}
COORDINATOR_SECTIONS = (
    ('overall', '整体目标与进度'), ('executors', '执行者进度'),
    ('tester', '测试者进度'), ('ocr', 'OCR情况和后续计划'),
    ('strong_review', '强审查情况'), ('risks', '风险'), ('next', '下一步计划'),
)
TERMINAL = {'DONE', 'ACCEPTED', 'ACCEPT_MERGED', 'ACCEPTED_MERGED', 'CLOSED'}
DELIVERED = {'DELIVERED', 'DELIVERED_FROZEN', 'DELIVERED_REVIEW_PENDING', 'REVIEW_PENDING'}
STATES = {
    'DISPATCHED': '已派单，完成情况待核实', 'RUNNING': '推进中',
    'REWORK': '返修中', 'REWORK_DISPATCHED': '已派返修',
    'ACKED': '已领取', 'ACK': '已领取', 'READY': '待派单',
    'PLANNED': '已规划', 'REOPENED': '已恢复推进', 'PARTIAL': '部分通过，仍有验收缺口',
    'BLOCKED': '受阻', 'WAITING': '等待前置条件', 'NEEDS_REVIEW': '审查未完成',
    'CONFLICT': '业务状态存在冲突，需核对', 'UNKNOWN': '业务状态未明确记录',
}
ACTIVITY = {'PREP': '准备与增量验证', 'TESTING': '验证中', 'DEVELOPING': '开发中',
            'WORKING': '处理中', 'RUNNING': '处理中', 'OBSERVING': '观察后台任务',
            'BLOCKED': '遇到阻塞', 'FAILED': '执行失败', 'TEST_REPORT_PENDING': '验证报告待裁决',
            'IDLE': '等待后续安排', 'IDLE_WAITING': '等待后续安排',
            'REVIEW_PENDING': '交付后等待审核', 'NO_CHANGE': '核对后无新变化'}
OCR_STATES = {
    'PREPARED': '已准备，尚未启动', 'SUBMITTED': '已提交', 'RUNNING': '正在审查',
    'AWAITING_DECISION': '审查已结束，等待协调者裁决', 'REVIEW_COMPLETE': '运行已完成，批准状态另行核对',
    'FAILED': '审查失败', 'LAUNCH_FAILED': '启动失败', 'INTERRUPTED': '运行中断',
    'HARD_DEADLINE': '达到运行时限，审查未完成', 'REVIEW_INCOMPLETE': '审查未完整完成',
    'CANCELLED': '已取消，审查未完成', 'UNKNOWN': '审查状态待核对',
}
OCR_INCOMPLETE = {'FAILED', 'LAUNCH_FAILED', 'INTERRUPTED', 'HARD_DEADLINE', 'REVIEW_INCOMPLETE', 'CANCELLED'}


def value(fact, default=None):
    return fact.get('value', default) if isinstance(fact, dict) else default


def _text(text):
    return re.sub(r'\s+', ' ', str(text or '')).strip().replace('`', '')


def _title(packet, task):
    return _text(value(packet.get('tasks', {}).get(task, {}).get('title'), '功能名称未记录'))


def _goal(packet):
    text = _text(value(packet.get('goal'), '当前整体目标未明确记录'))
    # A historical instruction is not a business-facing objective. Prefer its
    # explicit named milestone, rather than rewriting request verbs into a goal.
    if text.startswith('用户'):
        named = re.search(r'\bv\d+(?:\.\d+)+\s+[^，。；]+', text)
        return named[0] if named else '当前业务目标尚未独立记录，需要协调者明确目标描述。'
    return text


def _minute(raw):
    try:
        dt = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
        if dt.tzinfo is not None:
            return dt.astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M +08:00')
    except (ValueError, TypeError):
        pass
    return None


def _reference(packet, path):
    if path and path[0]=='runtime_observation' and packet.get('runtime_observation',{}).get('evidence_reference'):
        base = packet['runtime_observation']['evidence_reference']
        path = path[1:]
    else:
        base = packet.get('evidence_reference') or 'evidence.json'
    pointer = '/'.join(str(p).replace('~', '~0').replace('/', '~1') for p in path)
    return base + ('#/' + pointer if path else '#')


def _item(packet, text, *facts, group=None):
    sources = []
    for fact, path in facts:
        if not isinstance(fact, dict):
            continue
        sources.append({**{k: fact.get(k) for k in ('source', 'project_root', 'line', 'occurred_at', 'recorded_at', 'observed_at')},
                        'evidence_reference': fact.get('evidence_reference') or _reference(packet, path)})
    if not sources:
        sources.append({'source': packet.get('evidence_reference') or 'evidence.json', 'line': None,
                        'occurred_at': None, 'recorded_at': None, 'observed_at': packet.get('observed_at'),
                        'evidence_reference': _reference(packet, ())})
    result = {'text': text, 'sources': sources}
    if group:
        result['group'] = group
    return result


def _task_fact(packet, task, field):
    return (packet['tasks'][task].get(field), ('tasks', task, field))


def _role_fact(packet, role, field):
    return (packet.get('roles', {}).get(role, {}).get(field), ('roles', role, field))


def _review_no_blocking_recorded(packet, task):
    for _, run in _current_runs(packet):
        if value(run.get('task')) != task or value(run.get('current_snapshot')) != 'MATCH':
            continue
        if value(run.get('state')) in {'RUNNING', 'SUBMITTED'}:
            continue
        records = [value(record, {}) for record in run.get('adjudication_records', [])
                   if value(record, {}).get('final_record')]
        if records and all(record.get('followup_kind') == 'NO_BLOCKING_RECORDED' for record in records):
            return True
    return False


def _review_decision_recorded(packet, task):
    return any(value(run.get('task')) == task and value(run.get('current_snapshot')) == 'MATCH'
               and value(run.get('state')) not in {'RUNNING', 'SUBMITTED'} and run.get('adjudication')
               for _, run in _current_runs(packet))


def _status(packet, task):
    row = packet['tasks'][task]
    state, testing, merge = (value(row.get(k)) for k in ('state', 'testing', 'merge'))
    if state in TERMINAL:
        if merge == 'NOT_MERGED':
            return '开发交付已接受，尚未合入主分支'
        if merge == 'RECORDED_MERGED' or state in {'ACCEPT_MERGED', 'ACCEPTED_MERGED'}:
            return '已接受并合入主分支'
        return '已登记完成，合入情况未明确记录'
    if state in DELIVERED:
        records = [value(record, {}) for record in row.get('validation_records', [])]
        passes = [record for record in records if record.get('conclusion') == 'PASS_RECORDED']
        matching = [record for record in passes if record.get('current_snapshot') == 'MATCH']
        if matching:
            scopes = list(dict.fromkeys(_validation_scope(record) for record in matching))
            phase = ('等待协调者接受' if _review_no_blocking_recorded(packet, task) else
                     '审查已裁决，等待接受核对' if _review_decision_recorded(packet, task) else '等待审查接受')
            return '已交付，当前版本' + '、'.join(scopes) + '有通过记录，' + phase
        if passes and all(record.get('current_snapshot') == 'DIFFERENT' for record in passes):
            return '返修已交付，此前版本验证通过，当前版本需增量验证'
        if _review_no_blocking_recorded(packet, task):
            return '已交付，OCR已记录当前版本无阻塞，等待核对独立验证并接受'
        if testing == 'RECORDED_PASSED':
            return '已交付，独立验证有通过记录，等待核对并接受'
        return '已交付，等待审查和独立验证'
    return STATES.get(state, '业务状态待核对')


def _validation_scope(record):
    return {'INCREMENTAL': '增量验证', 'REWORK': '返修验证',
            'INTEGRATION': '联合验收', 'UNKNOWN': '验证'}.get(record.get('scope'), '验证')


def _validation_items(packet, *, role=None, scope=None):
    """Render independent evidence with its snapshot boundary, never a new PASS."""
    rows = []
    role_task = value(packet.get('roles', {}).get(role, {}).get('primary_task')) if role else None
    normalized_role_task = re.sub(r'[^A-Z0-9]', '', str(role_task or '').upper())
    for task, row in packet['tasks'].items():
        has_current_pass = any(value(record, {}).get('conclusion') == 'PASS_RECORDED'
                               and value(record, {}).get('current_snapshot') == 'MATCH'
                               for record in row.get('validation_records', []))
        for index, record in enumerate(row.get('validation_records', [])):
            evidence = value(record, {})
            if evidence.get('conclusion') != 'PASS_RECORDED':
                continue
            if has_current_pass and evidence.get('current_snapshot') == 'DIFFERENT':
                continue
            source_identity = re.sub(r'[^A-Z0-9]', '', str(record.get('source') or '').upper())
            if scope is not None and task not in scope and not (normalized_role_task and normalized_role_task in source_identity):
                continue
            boundary = {'MATCH': '对应当前交付版本', 'DIFFERENT': '对应此前版本，当前返修需补增量验证',
                        'UNKNOWN': '适用交付版本尚未明确'}.get(evidence.get('current_snapshot'), '适用交付版本尚未明确')
            rows.append(_item(packet, '**' + _title(packet, task) + '：** ' + _validation_scope(evidence) + '有通过记录；' + boundary + '。',
                              (record, ('tasks', task, 'validation_records', index))))
    return rows


def _role_tasks(packet, role):
    facts = packet.get('roles', {}).get(role, {})
    assigned = [t for t in value(facts.get('tasks'), []) if t in packet['tasks']]
    completed = [t for t in value(facts.get('recent_completed'), []) if t in packet['tasks']]
    if 'primary_task' in facts:
        primary = value(facts['primary_task'])
    else:
        activity = value(facts.get('activity_task'))
        primary = activity if activity in assigned + completed else next(iter(assigned + completed), None)
    live = packet.get('runtime_observation', {}).get('roles', {}).get(role, {})
    checkpoint = live.get('checkpoint', {})
    current = checkpoint.get('value', checkpoint).get('task')
    if current:
        primary = current if current in packet['tasks'] else None
    if primary not in packet['tasks'] and not current:
        primary = next(iter(completed), None)
    related = value(facts.get('related_tasks'), None)
    related = assigned + completed if related is None else related
    return primary, list(dict.fromkeys(t for t in related if t in packet['tasks'] and t != primary))


def _runtime_items(packet, role, *, group=None):
    observation = packet.get('runtime_observation', {})
    current = observation.get('roles', {}).get(role)
    if not current:
        return [] if not observation else [_item(packet, '**本次核验：** 该角色的运行状态未取得，待核实。', group=group)]
    checked = _minute(current.get('checked_at') or observation.get('checked_at')) or '时间未知'
    owner = current.get('owner', {})
    label = {'alive': '业务 owner 进程存活', 'exited_or_reused': '原业务 owner 已退出或 PID 已复用',
             'none': '当前没有业务 owner', 'unverified': '业务 owner 进程身份未核实'}.get(owner.get('state'), '业务 owner 状态待核实')
    source = {'source': current.get('source') or 'live-runtime-observation',
              'project_root': current.get('project_root') or packet.get('project'), 'observed_at': checked}
    items = [_item(packet, '**本次核验：** ' + checked + '；' + label + '。', (source, ('runtime_observation', 'roles', role)), group=group)]
    checkpoint_meta = current.get('checkpoint', {})
    checkpoint = checkpoint_meta.get('value', checkpoint_meta)
    if checkpoint.get('task'):
        local = not current.get('project_root') or current['project_root'] == packet.get('project')
        name = _title(packet, checkpoint['task']) if local and checkpoint['task'] in packet['tasks'] else checkpoint['task']
        stamp = _minute(checkpoint.get('recorded_at')) or '时间未记录'
        from workflow_query import history
        items.append(_item(packet, history(stamp)['notice'] + ' **最近业务检查点：** ' + name + '；' + _text(checkpoint.get('status') or '阶段未记录') + '。',
                           ({'source': checkpoint_meta.get('source') or current.get('source'),
                             'project_root': source['project_root'], 'recorded_at': checkpoint.get('recorded_at'), 'observed_at': checked},
                            ('runtime_observation', 'roles', role, 'checkpoint')), group=group))
    worktree = current.get('worktree', {})
    if worktree.get('head'):
        detail = '存在未提交变更' if worktree.get('dirty') is True else '工作区干净' if worktree.get('dirty') is False else '未提交变更情况待核实'
        items.append(_item(packet, '**当前代码：** HEAD ' + _text(worktree['head'])[:12] + '；' + detail + '。', (source, ('runtime_observation', 'roles', role, 'worktree')), group=group))
    elif worktree.get('state') == 'NOT_CONFIGURED' and checkpoint.get('task'):
        items.append(_item(packet, '**代码快照缺口：** 当前任务工作区未登记，本次未核验其 HEAD 和未提交变更。',
                           (source, ('runtime_observation', 'roles', role, 'worktree')), group=group))
    active = [row for row in current.get('operations', []) if row.get('status') in {'PENDING', 'RUNNING', 'UNKNOWN'}]
    if active:
        items.append(_item(packet, '**工程操作现查：** ' + '；'.join(_text(row.get('operation_id') or '操作') + '：' +
            {'PENDING': '待执行', 'RUNNING': '运行中', 'UNKNOWN': '状态待核实'}[row['status']] for row in active) + '。',
            (source, ('runtime_observation', 'roles', role, 'operations')), group=group))
    for error in current.get('errors', []):
        items.append(_item(packet, '**核验缺口：** ' + _runtime_error(error) + '。', (source, ('runtime_observation', 'roles', role)), group=group))
    return items


def _runtime_error(error):
    raw = str(error)
    known = {'owner_process_exited_reused_or_unverifiable': '原业务 owner 已退出、PID 已复用或缺少可核实的启动身份',
        'availability_unverifiable': '可领取任务状态缺少一致的当前证据',
        'worktree_HEAD_differs_from_recorded_checkpoint': '实际 HEAD 已不同于最近业务检查点，原阶段或验证记录需核对适用版本',
        'OCR_service_or_process_unverifiable_or_interrupted': 'OCR 服务或进程身份未确认，或已中断',
        'OCR_service_query_unverifiable': '未取得 OCR 服务的当前状态',
        'OCR_prepared_record_service_active_conflict': 'OCR 仍记录为未启动，但专属服务处于活动状态，启动身份与阶段待核实'}
    return known.get(raw, _text(raw))


def _live_review_items(packet, kind):
    observation = packet.get('runtime_observation')
    if not observation:
        return []
    checked = _minute(observation.get('checked_at')) or '时间未知'
    if kind == 'ocr':
        runs = observation.get('ocr', [])
        if not runs:
            return [_item(packet, '**本次运行核验：** 未发现已登记的 OCR 服务；其他历史审查状态见下列业务记录。')]
        rows = []
        for index, run in enumerate(runs):
            if run.get('recorded_state') not in {'PREPARED', 'SUBMITTED', 'RUNNING'} and not run.get('errors') and not run.get('service_active'):
                continue
            task = run.get('task')
            title = _title(packet, task) if task in packet['tasks'] else task or 'OCR'
            service = run.get('service') or {}
            detail = '运行状态待核实' if run.get('state') != 'VERIFIED' else OCR_STATES.get(run.get('observed_state'), '状态待核实')
            if service.get('ActiveState'):
                detail += '；服务 ' + _text(service['ActiveState'])
            if run.get('errors'):
                detail += '；' + '；'.join(_runtime_error(error) for error in run['errors'])
            prepared = run.get('recorded_state') == 'PREPARED' and run.get('in_flight') is False
            prefix = '**最近准备记录（未启动）：** ' if prepared else '**本次运行核验：** '
            if prepared:
                detail += '；仅有准备材料，不计为在途审查'
            rows.append(_item(packet, prefix + title + '，' + checked + '；' + detail + '。',
                ({'source': run.get('source'), 'observed_at': checked}, ('runtime_observation', 'ocr', index))))
        return rows or [_item(packet, '**本次运行核验：** 已登记 OCR 无在途服务；审查结论与接受情况按适用回执核对。')]
    current = observation.get('strong_review', {})
    status = current.get('value') or {}
    if current.get('state') == 'NOT_CONFIGURED':
        text = '本机没有强审账本，不能确认历史调用'
    elif current.get('errors'):
        text = '当前强审服务身份待核实；' + '；'.join(_runtime_error(error) for error in current['errors'])
    else:
        active = status.get('active_calls', [])
        own = [call for call in active if call.get('project') == packet.get('project')]
        text = '本项目在途 ' + str(len(own)) + ' 次（全局在途 ' + str(len(active)) + ' 次）'
        for call in own:
            text += '；服务 ' + _text(call.get('service_state') or '待核实') + '，进程身份 ' + _text(call.get('service_identity') or '待核实')
    return [_item(packet, '**本次运行核验：** ' + checked + '；' + text + '。',
        ({'source': current.get('source'), 'observed_at': checked}, ('runtime_observation', 'strong_review')))]


def _live_task_items(packet):
    observation = packet.get('runtime_observation', {})
    task = observation.get('task_observation', {})
    if not task:
        return []
    source = {'source': task.get('source'), 'observed_at': task.get('checked_at')}
    if task.get('state') == 'NOT_CONFIGURED':
        return [_item(packet, '**业务运行探针：** 当前项目未配置任务探针，无法核验生产运行步骤；已核验的角色和审查服务状态见各区。',
                      (source, ('runtime_observation', 'task_observation')))]
    observed = task.get('value') or {}
    pipeline = observed.get('pipeline', {})
    title = _title(packet, observed['task']) if observed.get('task') in packet['tasks'] else observed.get('task') or '已配置任务'
    details = ['**业务运行现查：** ' + title + '；' + (_minute(task.get('checked_at')) or '时间未知')]
    if pipeline.get('status'):
        details.append('后端状态 ' + _text(pipeline['status']) + '，attempt ' + str(pipeline.get('attempt', '未知')))
    steps = observed.get('scene', {}).get('steps') or pipeline.get('steps') or []
    if isinstance(steps, list):
        active = [row for row in steps if isinstance(row, dict) and str(row.get('status', '')).upper() in {'RUNNING', 'FAILED', 'BLOCKED'}]
        details.extend(_text(row.get('step') or row.get('step_name') or row.get('name') or row.get('id') or '步骤') + '：' + _text(row['status']) for row in active)
    health = observed.get('health') or {}
    labels = {'heartbeat': '心跳', 'proxy_run': '运行注册', 'container': '容器', 'disk': '磁盘'}
    if health:
        details.append('，'.join(labels[key] + ' ' + _text(health[key]) for key in labels if key in health))
    if task.get('state') != 'VERIFIED':
        details.append('存在核验缺口，未取得的步骤状态待核实')
    details.extend(_runtime_error(error) for error in task.get('errors', []))
    return [_item(packet, '；'.join(details) + '。', (source, ('runtime_observation', 'task_observation')))]


def _role_next(packet, role, primary):
    observation = packet.get('runtime_observation')
    if observation:
        runtime = observation.get('roles', {}).get(role, {})
        checkpoint_meta = runtime.get('checkpoint', {})
        checkpoint = checkpoint_meta.get('value', checkpoint_meta)
        stamp = _minute(checkpoint.get('recorded_at')) or '时间未知'
        planned = _text(checkpoint.get('next'))
        prefix = '先核对本次运行缺口及新输入；' if runtime.get('state') in {'UNKNOWN', 'UNVERIFIED'} or observation.get('errors') else ''
        from workflow_query import history
        return prefix + (history(stamp)['notice'] + ' 检查点当时的接续建议：' + planned if planned else
                         '当前检查点没有明确接续动作，需按本次核验和有效派单核实，不沿用历史任务的下一步。')
    facts = packet.get('roles', {}).get(role, {})
    focus = value(facts.get('activity_focus'), {})
    if focus.get('kind') == 'OBSERVATION' and focus.get('task') in packet['tasks']:
        return '继续观察 ' + _title(packet, focus['task']) + ' 的后台结果，出现终态变化后更新证据。'
    if not primary:
        return '核对有效派单；没有新任务时等待接续。'
    row = packet['tasks'][primary]
    state = value(row.get('state'))
    if state in TERMINAL:
        return '未记录新派单，等待协调者接续安排。'
    if state in DELIVERED:
        if role == 'TESTER':
            return '等待协调者裁决验证报告；按反馈补充验证证据。'
        passes = [value(record, {}) for record in row.get('validation_records', [])
                  if value(record, {}).get('conclusion') == 'PASS_RECORDED']
        if any(record.get('current_snapshot') == 'MATCH' for record in passes):
            return ('等待协调者核对已登记的审查裁决、独立验证范围及接受记录。' if _review_decision_recorded(packet, primary)
                    else '等待协调者完成剩余审查并接受交付。')
        if passes and all(record.get('current_snapshot') == 'DIFFERENT' for record in passes):
            return '等待测试者完成当前返修的增量验证、协调者安排适用审查；收到问题后返修。'
        if value(row.get('testing')) == 'RECORDED_PASSED':
            return '等待协调者核对独立验证适用版本，并完成审查与接受。'
        return '等待协调者审查和测试者独立验证。'
    if state == 'CONFLICT':
        return '先由协调者核对冲突来源，再按确认的状态接续。'
    if state in {'BLOCKED', 'WAITING'}:
        return '核对并等待派单列明的前置条件；条件解除后接续，不重复启动受阻工作。'
    if role == 'TESTER':
        return '按当前验证派单继续；联合验收以批准后的固定代码版本为准。'
    return '按当前有效派单核对接续动作；完成后提交对应交付证据。'


def _current_runs(packet):
    """Select newer runs only when explicit runtime timestamps prove the order."""
    grouped = {}
    for index, run in enumerate(packet.get('ocr', {}).get('runs', [])):
        grouped.setdefault(value(run.get('task')), []).append((index, run))
    selected = []
    for rows in grouped.values():
        dated = [(row, _minute(row[1].get('state', {}).get('occurred_at'))) for row in rows]
        if len(rows) > 1 and all(stamp for _, stamp in dated):
            newest = max(stamp for _, stamp in dated)
            selected.extend(row for row, stamp in dated if stamp == newest)
        else:
            selected.extend(rows)
    return sorted(selected, key=lambda row: row[0])


def _role_items(packet, role, *, include_next=True, grouped=False):
    primary, related = _role_tasks(packet, role)
    availability = packet.get('role_statuses', {}).get(role, {})
    from role_status import LABELS
    group = (ROLES[role]+'（'+LABELS.get(availability.get('status'), '待核实')+'）') if grouped else None
    items = _runtime_items(packet, role, group=group)
    def add(text, *facts):
        items.append(_item(packet, text, *facts, group=group))
    runtime = packet.get('runtime_observation', {}).get('roles', {}).get(role, {})
    if 'coordinated_business' in runtime or runtime.get('report_scope_conflict'):
        business = runtime.get('coordinated_business', {})
        source = {'source': runtime.get('project_root'), 'observed_at': runtime.get('checked_at')}
        pointer = ('runtime_observation', 'roles', role)
        if runtime.get('report_scope_conflict'):
            add('**报告来源：** 当前角色存在多个项目来源或订阅冲突，待核实；不能选择旧材料作为当前进展。', (source, pointer))
            return items
        add('**所属项目：** '+_text(runtime.get('project_root'))+'（显式只读订阅）。', (source, pointer))
        event = business.get('latest_event') or {}
        checkpoint = runtime.get('checkpoint', {}).get('value', {})
        task = event.get('task') or checkpoint.get('task')
        if task:
            from workflow_query import history
            add(history(event.get('recorded_at') or checkpoint.get('recorded_at'))['notice'] +
                ' **最新业务记录：** '+_text(task)+' '+_text(event.get('attempt') or checkpoint.get('attempt'))+
                '；'+_text(event.get('summary') or '仅取得当前检查点，完成情况按回执核对')+'。',
                (source, (*pointer, 'coordinated_business')))
        else:
            add('**当前业务记录：** 未取得明确任务及回执；不据此推定空闲或完成。', (source, pointer))
        if include_next:
            from workflow_query import history
            add('**下一步：** '+history(checkpoint.get('recorded_at'))['notice']+' '+
                _text(checkpoint.get('next') or business.get('checkpoint', {}).get('next') or
                '按所属项目的当前派单、依赖和回执接续。'), (source, (*pointer, 'checkpoint')))
        return items
    if primary:
        completed = value(packet['tasks'][primary].get('state')) in TERMINAL
        evidence = [_task_fact(packet, primary, key) for key in ('state', 'testing', 'merge', 'delivery_snapshot')]
        evidence += [(record, ('tasks', primary, 'validation_records', i))
                     for i, record in enumerate(packet['tasks'][primary].get('validation_records', []))]
        for i, run in _current_runs(packet):
            if value(run.get('task')) == primary:
                evidence += [(record, ('ocr', 'runs', i, 'adjudication_records', n))
                             for n, record in enumerate(run.get('adjudication_records', []))]
        add('**' + ('业务记录阶段' if packet.get('runtime_observation') else '最近完成' if completed else '当前任务') + '：** ' + _title(packet, primary) + '，**' + _status(packet, primary) + '**。',
            *evidence)
    else:
        add('**当前任务：** 未记录明确的当前派单，需要核对；不据此判定全部完成。', _role_fact(packet, role, 'tasks'))
    for task in related:
        add('**关联事项：** ' + _title(packet, task) + '，**' + _status(packet, task) + '**。',
            _task_fact(packet, task, 'state'))
    facts = packet.get('roles', {}).get(role, {})
    for task in value(facts.get('observed_tasks'), []):
        if task in packet['tasks'] and task != primary and task not in related:
            add('**持续观察：** ' + _title(packet, task) + ' 的后台运行结果。',
                _role_fact(packet, role, 'observed_tasks'))
    dispatch = facts.get('dispatch', {})
    notice = value(dispatch, {})
    new_task_kinds = {'new_task', 'NEW_TASK_PENDING', 'NEW_ATTEMPT'}
    rework_kinds = {'rework', 'correction', 'REWORK_PENDING'}
    if notice.get('acknowledged') is False and notice.get('notification_kind') in new_task_kinds | rework_kinds:
        add('**待处理指令：** 有' + ('新派单' if notice['notification_kind'] in new_task_kinds else '返修指令') + '尚未确认领取，等待本角色处理。',
            _role_fact(packet, role, 'dispatch'))
    activity = facts.get('activity', {})
    activity_label = ACTIVITY.get(value(activity))
    # Unknown technical event labels remain in evidence rather than leaking into
    # user prose. A document read time never becomes a role action time.
    occurred = _minute(activity.get('occurred_at'))
    recorded = _minute(activity.get('recorded_at'))
    if occurred or recorded:
        time_label = occurred or ('发生时间未记录，补录于 ' + recorded)
        label = activity_label or '动作阶段未明确登记'
        add('**最近记录：** ' + label + '（' + time_label + '）。', _role_fact(packet, role, 'activity'))
    if include_next:
        add('**下一步：** ' + _role_next(packet, role, primary),
            _role_fact(packet, role, 'tasks'), _role_fact(packet, role, 'next'))
    return items


def _coordinated_review_items(packet, kind):
    rows = []
    projects = packet.get('runtime_observation', {}).get('coordinated_inputs', {}).get('projects', [])
    for index, project in enumerate(projects):
        root = project['project_root']
        reviews = project.get('reviews', {})
        base = ('runtime_observation', 'coordinated_inputs', 'projects', index)
        source = {'source': root, 'project_root': root,
                  'observed_at': packet.get('runtime_observation', {}).get('checked_at')}
        errors = [error for error in project.get('errors', []) if error.get('code') == 'COORDINATED_REVIEWS_UNVERIFIED']
        if errors:
            rows.append(_item(packet, '**订阅项目 '+Path(root).name+'：** 审查来源未核实；'+
                '；'.join(_runtime_error(error.get('error')) for error in errors)+'。', (source, (*base, 'errors'))))
        if kind == 'ocr':
            for offset, run in enumerate(reviews.get('ocr_runs', [])):
                text = ('**订阅项目 '+Path(root).name+'：** '+_text(run.get('task') or 'OCR')+
                    '（'+_text(run.get('run_id'))[:12]+'），'+OCR_STATES.get(run.get('state'), '审查记录状态待核实'))
                if run.get('error'):
                    text += '；'+_runtime_error(run['error'])
                rows.append(_item(packet, text+'；仍需核对适用结果和门禁，不能据此判定任务已接受。',
                    ({**source, 'source': str(Path(root)/run['source']) if run.get('source') else root},
                     (*base, 'reviews', 'ocr_runs', offset))))
        else:
            strong = reviews.get('strong_review', {})
            calls = [(call, ('active_calls', offset)) for offset, call in enumerate(strong.get('active_calls', []))]
            last = strong.get('last_call')
            if last and last.get('id') not in {call.get('id') for call, _ in calls}:
                calls.append((last, ('last_call',)))
            labels = {'RESERVED': '已预留', 'RUNNING': '运行中', 'SUCCEEDED': '调用成功',
                      'FAILED': '调用失败', 'LOST': '运行身份丢失', 'TIMED_OUT': '已超时', 'CANCELLED': '已取消'}
            for call, pointer in calls:
                text = ('**订阅项目 '+Path(root).name+'：** 强审 '+_text(call.get('review_id') or call.get('id'))+
                    '，'+labels.get(call.get('status'), '调用状态待核实')+'；结果哈希与未关闭问题仍需核验。')
                rows.append(_item(packet, text, ({**source, 'source': strong.get('source') or root},
                    (*base, 'reviews', 'strong_review', *pointer))))
    return rows


def _ocr_items(packet):
    rows = _live_review_items(packet, 'ocr')
    started = {value(run.get('task')) for _, run in _current_runs(packet)
               if value(run.get('state')) in {'RUNNING', 'SUBMITTED', 'AWAITING_DECISION', 'REVIEW_COMPLETE'}}
    for i, run in _current_runs(packet):
        task, state = value(run.get('task')), value(run.get('state'))
        final_records = [(n, record) for n, record in enumerate(run.get('adjudication_records', []))
                         if value(record, {}).get('final_record')]
        if state in {'RUNNING', 'SUBMITTED'}:
            label = OCR_STATES[state]
        elif final_records:
            followups = {value(record, {}).get('followup_kind') for _, record in final_records}
            if value(run.get('current_snapshot')) == 'DIFFERENT':
                label = '此前版本审查已有裁决，当前交付版本的适用审查需另行确认'
            elif len(followups) > 1:
                label = '裁决记录存在不同处理要求，需要协调者核对'
            else:
                label = {'REWORK': '已裁决，需要返修', 'REVIEW_REQUIRED': '已裁决，需要增量补审',
                         'NO_BLOCKING_RECORDED': '审查记录已明确无阻塞，接受和合入分别确认',
                         'UNCONFIRMED': '已有最终审查裁决，适用范围需按记录核对'}.get(next(iter(followups)), '已有最终审查裁决')
        else:
            label = '已有审查裁决，运行与批准状态分别保留' if run.get('adjudication') else OCR_STATES.get(state, '审查状态待核对')
        rows.append(_item(packet, ('**记录阶段：** ' if packet.get('runtime_observation') else '**当前：** ') + _title(packet, task) + '，**' + label + '**。',
                          (run.get('state'), ('ocr', 'runs', i, 'state')),
                          (run.get('adjudication'), ('ocr', 'runs', i, 'adjudication')),
                          *[(record, ('ocr', 'runs', i, 'adjudication_records', n)) for n, record in final_records]))
    categories = {'blocked': ('障碍', '审查前置条件仍未满足，需协调者核对解除条件'),
                  'queued': ('后续', '已登记等待串行审查，前一项结束后由协调者接续'),
                  'resolved': ('已解除', '对应审查前置条件已解除'),
                  'nonblocking': ('后续跟进', '存在非阻塞审查事项，不影响已明确允许的当前验收范围')}
    seen = set()
    for i, row in enumerate(packet.get('ocr', {}).get('declared', [])):
        task, category = value(row.get('task')), row.get('category')
        if category not in categories or (task, category) in seen:
            continue
        if category == 'queued' and task in started:
            continue
        seen.add((task, category))
        heading, text = categories[category]
        rows.append(_item(packet, '**' + heading + '：** ' + _title(packet, task) + '，' + text + '。',
                          (row.get('detail'), ('ocr', 'declared', i, 'detail'))))
        if row.get('queue_state') == 'QUEUED' and task not in started and category != 'queued' and (task, 'queued') not in seen:
            seen.add((task, 'queued'))
            rows.append(_item(packet, '**后续：** ' + _title(packet, task) + '，已登记等待串行审查，前一项结束后由协调者接续。',
                              (row.get('detail'), ('ocr', 'declared', i, 'detail'))))
    current = [run for _, run in _current_runs(packet)]
    if any(value(run.get('state')) in {'RUNNING', 'SUBMITTED'} for run in current):
        plan = '协调者待当前审查结束后核对覆盖和结果，执行裁决，再按已登记串行队列接续。'
    elif any(value(run.get('state')) in OCR_INCOMPLETE and not run.get('adjudication') for run in current):
        plan = '协调者核对失败终因及覆盖范围后裁定处置，不自动增加重跑。'
    elif any(run.get('adjudication') for run in current):
        plan = '执行已有裁决中的返修或接受条件，并核对适用版本，不重复要求裁决。'
    elif rows:
        plan = '协调者核对已登记的串行队列和入口条件，再按既有顺序接续。'
    else:
        plan = '未找到明确的审查运行或队列记录，由协调者先核对当前安排。'
    rows.append(_item(packet, '**后续计划：** ' + plan))
    return rows


def _strong_items(packet):
    review = packet.get('strong_review', {})
    live = packet.get('runtime_observation', {}).get('strong_review')
    if live is not None:
        current = live.get('value', {}) if live.get('state') == 'VERIFIED' else {}
        review = {key: {'value': current.get(key), 'source': live.get('source'),
                       'observed_at': live.get('checked_at'),
                       'evidence_reference':_reference(packet,('runtime_observation','strong_review','value',key))}
                  for key in ('authorization', 'expires_at', 'active_calls', 'used_last_60_minutes', 'limit', 'last_call', 'cost') if key in current}
    labels = {'ACTIVE': '有效', 'AUTHORIZED': '有效', 'REVOKED': '已撤销', 'EXPIRED': '已到期',
              'MANUAL_ONLY': '未授权，需人工触发', 'SCHEDULED': '尚未生效', 'SCOPE_COMPLETE': '授权范围已完成'}
    def item(text, key):
        path = ('runtime_observation', 'strong_review', 'value', key) if live is not None else ('strong_review', key)
        return _item(packet, text, (review.get(key), path))
    rows = _live_review_items(packet, 'strong_review')
    rows.append(item('**授权：** ' + labels.get(value(review.get('authorization')), '状态待核对') + '。', 'authorization'))
    expires = _minute(value(review.get('expires_at')))
    if expires:
        rows.append(item('**授权截止：** ' + expires + '。', 'expires_at'))
    active = value(review.get('active_calls'))
    rows.append(item('**调用：** 当前在途 ' + (str(len(active)) if isinstance(active, list) else '未知') + ' 次；近 60 分钟 ' + str(value(review.get('used_last_60_minutes'), '未知')) + '/' + str(value(review.get('limit'), 5)) + ' 次。', 'used_last_60_minutes'))
    last = value(review.get('last_call'))
    if isinstance(last, dict):
        # A successful process is not an accepted code-review conclusion.
        label = {'SUCCEEDED': '调用完成，审查结论以结果文档为准', 'FAILED': '调用失败',
                 'RUNNING': '调用进行中', 'PENDING': '等待执行'}.get(last.get('status'), '调用状态待核对')
        rows.append(item('**最近调用：** ' + label + '。', 'last_call'))
    rows.append(item('**费用：** ' + str(value(review.get('cost')) if value(review.get('cost')) is not None else '未知') + '。', 'cost'))
    return rows


def _risks(packet, *, task_scope=None):
    rows = []
    known_conditions = set()
    for i, run in _current_runs(packet):
        task, state = value(run.get('task')), value(run.get('state'))
        if task_scope is not None and task not in task_scope:
            continue
        if state in OCR_INCOMPLETE and not run.get('adjudication'):
            rows.append(_item(packet, '**' + _title(packet, task) + '：** ' + OCR_STATES[state] + '；不能据此放行对应功能。',
                              (run.get('state'), ('ocr', 'runs', i, 'state'))))
    for i, row in enumerate(packet.get('ocr', {}).get('declared', [])):
        task = value(row.get('task'))
        if task_scope is not None and task not in task_scope:
            continue
        if row.get('is_blocking') is True:
            gate = {'entry': '审查入口条件', 'acceptance': '交付接受门禁', 'coverage': '审查覆盖条件'}.get(row.get('gate_kind'), '审查前置条件')
            text = gate + '尚未满足；协调者需确认解除条件。'
        elif row.get('category') == 'unknown' and row.get('unresolved_current_claim', True):
            text = '审查条件记录不明确；协调者需核对，不能据此认定通过或阻塞。'
        else:
            continue
        rows.append(_item(packet, '**' + _title(packet, task) + '：** ' + text,
                          (row.get('detail'), ('ocr', 'declared', i, 'detail'))))
        known_conditions.add((task, row.get('detail', {}).get('source'), row.get('detail', {}).get('line'), row.get('gate_kind')))
    for i, conflict in enumerate(packet.get('current_conflicts', packet.get('conflicts', []))):
        task = conflict.get('task')
        if task_scope is not None and task not in task_scope:
            continue
        prefix = _title(packet, task) + '：' if task in packet['tasks'] else ''
        if conflict.get('decision'):
            description = '运行记录仍显示等待裁决，但已有对应裁决文档'
        elif conflict.get('runtime_claim'):
            description = '任务表登记审查受阻，但运行记录已启动'
        elif conflict.get('todo_source'):
            description = '任务表与角色事件登记的业务状态不同'
        elif conflict.get('event_id'):
            description = '同一事件编号的记录内容不一致'
        else:
            description = '状态记录存在冲突'
        rows.append(_item(packet, '**' + prefix + description + '**；协调者需核对来源并同步对应记录。',
                          (conflict, ('current_conflicts', i))))
    # Only already-classified business risks are eligible. Free-form historical
    # clauses such as "未覆盖但不阻塞" cannot create new global blockers.
    for i, risk in enumerate(packet.get('risks', [])):
        task = risk.get('task')
        if risk.get('is_blocking') is not True or (task_scope is not None and task not in task_scope):
            continue
        if (task, risk.get('source'), risk.get('line'), risk.get('gate_kind')) in known_conditions:
            continue
        prefix = _title(packet, task) + '：' if task in packet['tasks'] else ''
        detail = _text(risk.get('value'))
        gate = {'entry': '审查入口条件尚未满足', 'acceptance': '交付接受门禁仍未满足',
                'coverage': '审查覆盖尚未完整确认'}.get(risk.get('gate_kind'))
        # Short, already-classified business evidence can state exactly what is
        # missing. Technical logs are cited instead of copied into the report.
        technical = re.search(r'\b[0-9a-f]{7,64}\b|(?:inbox|reviews|scratch|receipts)/|叶|\b(?:rc|HEAD|SHA|seed)\b', detail, re.I)
        detail = gate or (detail if detail and len(detail) <= 120 and not technical else '必要验收证据仍有缺口，需核对具体缺项')
        rows.append(_item(packet, '**' + prefix + '** ' + detail + '；协调者需确认补证据的负责人。',
                          (risk, ('risks', i))))
    return rows


def build_report(packet, role='COORDINATOR'):
    """Build role-limited business prose with citations, without side effects."""
    if role not in ROLES:
        raise ValueError('unsupported report role')
    if not isinstance(packet.get('tasks'), dict):
        raise ValueError('progress tasks missing')
    if not isinstance(packet.get('role_statuses'), dict):
        from role_status import collect_role_statuses
        project = packet.get('project')
        statuses = collect_role_statuses(project) if project and Path(project).is_dir() else {}
        packet = {**packet, 'role_statuses': statuses}
    observation = packet.get('runtime_observation')
    if observation:
        statuses = {who: dict(state) for who, state in packet['role_statuses'].items()}
        for who, current in observation.get('roles', {}).items():
            if current.get('state') in {'UNKNOWN', 'UNVERIFIED'} or observation.get('errors'):
                statuses[who] = {**statuses.get(who, {}), 'status': 'UNKNOWN', 'label': '待核实'}
        packet = {**packet, 'role_statuses': statuses}
    report = {k: packet.get(k) for k in ('project', 'observed_at', 'fingerprint', 'evidence_reference')}
    report.update(schema_version=1, kind='role-progress-report', role=role, sections=[])
    report['report_basis'] = 'live_runtime_with_latest_records' if observation else 'recorded_evidence'
    if observation:
        report['runtime_observation'] = observation if role == 'COORDINATOR' else {
            'kind': observation.get('kind'), 'checked_at': observation.get('checked_at'),
            'state': observation.get('roles', {}).get(role, {}).get('state'),
            'errors': observation.get('roles', {}).get(role, {}).get('errors', []),
            'collection_gaps': observation.get('collection_gaps', []),
            'evidence_reference': observation.get('evidence_reference'),
            'roles': {role: observation.get('roles', {}).get(role, {})}}
        report['checked_at'] = observation.get('checked_at')
    def section(key, title, items):
        report['sections'].append({'key': key, 'title': title, 'items': items})
    if role != 'COORDINATOR':
        primary, related = _role_tasks(packet, role)
        scope = set(related + ([primary] if primary else []))
        section('own_progress', '当前工作', _role_items(packet, role, include_next=False))
        results = []
        if role == 'TESTER':
            explicit_scope = set(scope)
            for task in scope:
                explicit_scope.update(re.findall(r'(?:TASK|NC|MN)-\d+[A-Z]?(?:-[A-Z0-9]+)*',
                                                str(value(packet['tasks'][task].get('dependencies'), ''))))
            results = _validation_items(packet, role=role, scope=explicit_scope)
            all_verified = [t for t, row in packet['tasks'].items() if value(row.get('testing')) == 'RECORDED_PASSED']
            verified = [t for t in all_verified if t in explicit_scope]
            if not results and verified:
                results.append(_item(packet,
                    '**已记录通过：** ' + '、'.join(_title(packet, t) for t in verified) + '；仅代表各自验证范围，不代表联合验收完成。',
                    *[_task_fact(packet, t, 'testing') for t in verified]))
            elif not results and all_verified:
                results.append(_item(packet, '**已有验证记录：** 任务表登记了独立验证通过；与本次派单的具体范围、版本对应关系仍需核对。',
                                     _role_fact(packet, role, 'tasks')))
        elif primary and value(packet['tasks'][primary].get('state')) in TERMINAL | DELIVERED:
            results.append(_item(packet, '**已有交付：** ' + _title(packet, primary) + '；交付、接受和合入分别确认，不据此宣称本轮新增完成。',
                                 _task_fact(packet, primary, 'delivery')))
        section('own_results', '本次完成', results or [_item(packet, '本次未记录可确认的新增交付或验证结果。')])
        dependencies = _risks(packet, task_scope=scope)
        if observation:
            dependencies = [_item(packet, '**本次证据缺口：** ' + _runtime_error(error) + '；相关业务记录待核实。')
                for error in observation.get('collection_gaps', [])] + dependencies
        if primary and role == 'TESTER':
            dependencies.append(_item(packet, '**联合验收依赖：** 按派单确认相关功能已接受，并提供固定代码版本；准备完成不代表联合验收通过。',
                                      _task_fact(packet, primary, 'dependencies')))
        section('dependencies', '阻塞与依赖', dependencies or [_item(packet, '未记录影响本角色工作的明确阻塞；不代表全局门禁通过。')])
        section('next', '下一步计划', [_item(packet, _role_next(packet, role, primary), _role_fact(packet, role, 'tasks'))])
        return report
    overview = _live_task_items(packet) + [_item(packet, '**目标：** ' + _goal(packet),
                      (packet.get('goal'), ('goal',)))]
    delivered = [t for t, row in packet['tasks'].items() if value(row.get('state')) in DELIVERED]
    completed = list(dict.fromkeys(t for facts in packet.get('roles', {}).values()
                                  for t in value(facts.get('recent_completed'), []) if t in packet['tasks']))
    if delivered:
        overview.append(_item(packet, '**开发已交付：** ' + '、'.join(_title(packet, t) for t in delivered) + '。',
                              *[_task_fact(packet, t, 'state') for t in delivered]))
    if completed:
        overview.append(_item(packet, '**交付已接受：** ' + '、'.join(_title(packet, t) for t in completed) + '；合入与联合验收分别确认。',
                              *[_task_fact(packet, t, 'state') for t in completed]))
    overview.append(_item(packet, '**整体完成条件：** 剩余审查、独立验证与接受均按任务记录确认，不能用局部交付代替整体闭环。',
                          (packet.get('overall', {}).get('current_tasks'), ('overall', 'current_tasks'))))
    section(*COORDINATOR_SECTIONS[0], overview)
    section(*COORDINATOR_SECTIONS[1], [item for who in ('A', 'B', 'C') for item in _role_items(packet, who, grouped=True)])
    tester = _role_items(packet, 'TESTER', grouped=True)
    validation_items = _validation_items(packet)
    tester.extend(validation_items)
    verified = [t for t, row in packet['tasks'].items() if value(row.get('testing')) == 'RECORDED_PASSED']
    if verified and not validation_items:
        tester.append(_item(packet, '**已记录独立验证通过：** ' + '、'.join(_title(packet, t) for t in verified) + '；联合验收尚需单独确认。',
                            *[_task_fact(packet, t, 'testing') for t in verified]))
    section(*COORDINATOR_SECTIONS[2], tester)
    section(*COORDINATOR_SECTIONS[3], [*_ocr_items(packet), *_coordinated_review_items(packet, 'ocr')])
    section(*COORDINATOR_SECTIONS[4], [*_strong_items(packet), *_coordinated_review_items(packet, 'strong')])
    risks = _risks(packet)
    if observation:
        risks = [_item(packet, '**本次核验缺口：** ' + _runtime_error(error) + '；采集不完整或输入发生变化，相关当前状态待重新核对。')
                 for error in [*observation.get('errors', []), *observation.get('collection_gaps', [])]] + risks
    section(*COORDINATOR_SECTIONS[5], risks or [_item(packet, '未记录新的明确阻塞；未记录或冲突的状态仍需核对。')])
    next_items = []
    pending_review = [task for task in delivered if not _review_no_blocking_recorded(packet, task)]
    ready_review = [task for task in delivered if task not in pending_review]
    if pending_review:
        next_items.append(_item(packet, '**审查与验证：** 接续 ' + '、'.join(_title(packet, t) for t in pending_review) + ' 的当前审查与所需独立验证，再由协调者作出接受决定。',
                                *[_task_fact(packet, t, 'state') for t in pending_review]))
    if ready_review:
        next_items.append(_item(packet, '**接受核对：** ' + '、'.join(_title(packet, t) for t in ready_review) + ' 已有审查裁决；协调者核对独立验证适用版本及接受记录，按原门禁收口。',
                                *[_task_fact(packet, t, 'state') for t in ready_review]))
    if any(value(run.get('state')) in {'RUNNING', 'SUBMITTED'} for run in packet.get('ocr', {}).get('runs', [])):
        next_items.append(_item(packet, '**OCR接续：** 当前审查结束后先核对覆盖和结论，再按串行队列推进；不因运行结束自动批准。'))
    if risks:
        next_items.append(_item(packet, '**阻塞处置：** 核对上述未完成审查、缺失证据或来源冲突，再决定返修或补审。'))
    next_items.append(_item(packet, '**联合验收：** 满足审查和接受门禁后，提供固定代码版本交测试者完成联合验证。'))
    if observation:
        from role_status import LABELS
        next_items = [_item(packet, '**' + ROLES[who] + '（' + LABELS.get(packet['role_statuses'].get(who,{}).get('status'),'待核实') + '）：** ' + _role_next(packet, who, None),
            ({'source': current.get('checkpoint', {}).get('source'),
              'recorded_at': current.get('checkpoint', {}).get('value', {}).get('recorded_at'),
              'observed_at': observation.get('checked_at')}, ('runtime_observation', 'roles', who, 'checkpoint')))
            for who, current in observation.get('roles', {}).items() if who in ROLES]
        if not next_items:
            next_items = [_item(packet, '本次没有取得可核实的接续检查点，需核对当前有效派单和回执。')]
    section(*COORDINATOR_SECTIONS[6], next_items)
    return report


def render_report(report):
    """Render the exact report contract; caller-specific templates cannot diverge."""
    live = report.get('report_basis') == 'live_runtime_with_latest_records'
    out = ['**' + ROLES[report['role']] + ('｜当前进度核验 ' if live else '｜进度记录汇总 ') +
           str(_minute(report.get('checked_at')) if live else report.get('observed_at') or '读取时间未记录') + '**']
    for section in report['sections']:
        out.extend(['', '**' + section['title'] + '**', ''])
        last_group = None
        for item in section['items']:
            group = item.get('group')
            if group and group != last_group:
                if last_group:
                    out.append('')
                out.extend(['**' + group + '**', ''])
            text = item['text']
            # Items are produced from structured facts, but role summaries can
            # still contain a plain sentence.  Preserve wording and sources
            # while making the leading fact label scannable in Feishu.
            if isinstance(text, str) and text and not text.lstrip().startswith('**'):
                # A timestamp's colon is part of the evidence, not a field
                # separator.  Keep historical times intact in rendered text.
                has_clock_time = re.search(r'\b\d{1,2}:\d{2}\b', text) is not None
                notice = re.match(r'^(【[^】]+】)', text)
                match = re.match(r'^([^：:，。；、]{1,24})([：:])\s*(.*)$', text)
                if notice:
                    text = '**' + notice.group(1) + '**' + text[notice.end():]
                elif match and not has_clock_time:
                    text = '**' + match.group(1) + match.group(2) + '** ' + match.group(3)
                elif not has_clock_time:
                    boundary = re.search(r'[；;。！？!?]', text)
                    end = boundary.start() if boundary and boundary.start() <= 40 else min(len(text), 24)
                    while end > 0 and text[:end].count('`') % 2:
                        end -= 1
                    if end:
                        text = '**' + text[:end] + '**' + text[end:]
            out.append('- ' + text)
            last_group = group
    if live:
        out.extend(['', '运行状态为本次查询核验结果；业务阶段和成果按各项检查点、回执时间说明，缺失或冲突项仍待核实。'])
    else:
        out.extend(['', '业务记录读取时间：' + str(report.get('observed_at') or '未知时间') + '；本报告未执行当次运行核验，当前状态请使用 live-progress。'])
    return '\n'.join(out)
