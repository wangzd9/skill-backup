"""Read-only report facts and analysis using the business ledger's sole outbox.

Callbacks receive facts or rendered text only. This module never binds work,
acknowledges business inputs, starts a provider worker or creates another ledger.
"""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from runtime_common import locked
from time_accounting import epoch, iso
from usage_accounting import ROLES, summarize
from report_summary import _usage_packet

ZONE = ZoneInfo('Asia/Shanghai')
ANALYSIS_FIELDS = ('accomplishments', 'issues', 'next_steps')
COORDINATOR_SECTIONS = ('整体目标与进度', '执行者进度', '测试者进度', 'OCR情况和后续计划',
                        '强审查情况', '风险', '下一步计划')
PERSONAL_SECTIONS = ('当前工作', '本次完成', '阻塞与依赖', '下一步计划')
_BOLD_REPORT_FIELD = re.compile(r'^\*\*[^*\n]{1,40}[：:]\*\*\s*\S')
_COORDINATOR_SECTION_LIMITS = {'整体目标与进度': 2, '执行者进度': 5, '测试者进度': 7,
    'OCR情况和后续计划': 8, '强审查情况': 8, '风险': 8, '下一步计划': 8}
_PERSONAL_SECTION_LIMITS = {'当前工作': 4, '本次完成': 3, '阻塞与依赖': 5, '下一步计划': 8}


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _observation_facts(ledger, observation):
    """Keep health/UNKNOWN and reason counts inline, full diagnostics by hash.

    This changes the model view only. Observation sources, incident state and
    notification receipts are neither deleted nor marked recovered.
    """
    from observation_view import health_view
    return health_view(observation, ledger.path.parent/'observation-views')


def _write_diagnostic_facts(ledger, diagnostics):
    if len(json.dumps(diagnostics, ensure_ascii=False).encode()) <= 4096:
        return diagnostics
    from collections import Counter
    from workflow_runtime import verified_input_view
    reference = verified_input_view(ledger.path.parent/'diagnostic-views', 'writes', diagnostics)
    return {'source_state': diagnostics.get('source_state'), 'source': diagnostics.get('source'),
        'projection': 'write-diagnostics-reference-v1', 'full_diagnostics': reference,
        'write_path_states': dict(Counter(row.get('write_path_state', 'UNKNOWN')
            for row in diagnostics.get('write_paths', []))),
        'anomaly_count': len(diagnostics.get('anomalies', [])),
        'closure_count': len(diagnostics.get('closures', [])),
        'reason_codes': sorted({code for field in ('write_paths', 'anomalies', 'closures')
            for row in diagnostics.get(field, []) for code in row.get('reason_codes', [])})}


def _existing(ledger, notification_id):
    return ledger.notification(notification_id)


def summary_identifier(role, start, end, run_id):
    return 'summary-' + _hash([role, iso(epoch(start)), iso(epoch(end)), run_id])[:32]


def _timing_facts(usage_index, role, start, end, project, pool_path):
    from runtime_common import read_json
    from time_accounting import summarize as timing_summary, read_cron_records
    index = Path(usage_index).with_name('role-time.db')
    home = Path(usage_index).parent.parent
    try:
        job_roles = {}
        if pool_path is not None:
            for name, binding in read_json(pool_path).get('roles', {}).items():
                for key in ('poll_job_id', 'report_job_id', 'morning_report_job_id'):
                    if binding.get(key):
                        job_roles[binding[key]] = name
        cron_gap = None
        try:
            records = read_cron_records(home/'cron/executions.db', job_roles, start, end)
        except Exception as exc:
            records, cron_gap = [], type(exc).__name__
        value = timing_summary(index, role, start, end, project=project, cron_records=records)
        model, dispatch = value['dimensions']['client_model_wait'], value['dimensions']['role_task_dispatch']
        return {'state': 'OBSERVED' if model['requests'] or dispatch['records'] else 'UNKNOWN',
            'client_request_count': len(model['requests']), 'client_request_sum_seconds': model['request_sum_seconds'],
            'client_occupancy_seconds': model['observed_seconds'], 'open_requests': len(model['open_requests']),
            'dispatch_records': len(dispatch['records']), 'schedule_delay_seconds': dispatch['schedule_delay_seconds'],
            'dispatch_wait_seconds': dispatch['dispatch_wait_seconds'],
            'cron_source_gap': cron_gap,
            'client_gap_count': len(model['gaps']), 'dispatch_gap_count': len(dispatch['gaps']),
            'business_phase': '未提供连续业务观察边界，不能推算开发或确认空闲时长',
            'limitations': '客户端时长包含网络、SDK重试和服务端等待；不是纯推理或服务端排队。各维度不能相加。'}
    except Exception as exc:
        return {'state': 'UNKNOWN', 'error': type(exc).__name__,
                'reason': '时间源未采集或无法核验，不把无模型调用等同空闲'}


def build_facts(ledger, role, start, end, *, usage_index, project, pool_path=None, run_id=None):
    if role not in ROLES:
        raise ValueError('ROLE_NOT_IN_RUNTIME')
    left, right = epoch(start), epoch(end)
    if right <= left:
        raise ValueError('EMPTY_REPORT_WINDOW')
    snapshot = ledger.snapshot()
    run_id = run_id or snapshot.get('current_run_id')
    # The snapshot already freezes the complete event stream and its high water.
    # Taking the oldest 10,000 rows before filtering the window silently loses
    # every recent result once a long-running run exceeds that many events.
    events = [item for item in snapshot['events'] if item['run_id'] in (None, run_id)]
    window = [item for item in events if left <= item['created_at'] < right and
              (role == 'COORDINATOR' or item['role'] in (None, role))]
    from workflow_runtime import project_events
    window = project_events(ledger, window)
    candidate_ids = {item['candidate_id'] for item in snapshot['candidates']
                     if item['run_id'] == run_id}
    if pool_path is not None:
        from workflow_runtime import role_state
        statuses = [role_state(pool_path, item, at=datetime.fromtimestamp(ledger.clock(), ZONE)) for item in sorted(ROLES)]
    else:
        statuses = [{'role': item, 'status': 'UNKNOWN', 'label': '待核实', 'available': False,
                     'reasons': ['本次报告未提供原生运行采集入口']} for item in sorted(ROLES)]
    works = [item for item in snapshot['works'] if item['run_id'] == run_id and
             (role == 'COORDINATOR' or item['role'] == role) and item['state'] != 'ENDED']
    from workflow_query import checkpoint_history, history
    works = [{**work, 'checkpoint_history': checkpoint_history(work, snapshot['events'])} for work in works]
    for status in statuses:
        if status.get('write_diagnostics'):
            status['write_diagnostics'] = _write_diagnostic_facts(ledger, status['write_diagnostics'])
        for index, work in enumerate(works):
            if status['role'] == work['role'] and status.get('checkpoint') == work['checkpoint']:
                # Same object appears in current_works; retain an explicit JSON
                # pointer instead of duplicating complete checkpoint obligations.
                status['checkpoint'] = {'same_as': '/current_works/'+str(index)+'/checkpoint'}
                break
    # A coordination work has no task row. Its saved checkpoint remains a real
    # result even when an execution has ended or the task table is empty.
    checkpoints = [dict(event_id=item['event_id'], work_id=item['work_id'],
        role=item['role'], saved_at=iso(item['created_at']),
        checkpoint=item['payload']['checkpoint']) for item in window
        if item['kind'] == 'CHECKPOINT_SAVED' and 'checkpoint' in item['payload']]
    for checkpoint in checkpoints:
        checkpoint['history'] = history(checkpoint['saved_at'], event_id=checkpoint['event_id'])
    scoped_candidates=[item for item in snapshot['candidates'] if item['run_id'] == run_id and
                       (role == 'COORDINATOR' or item['role'] == role)]
    scoped_candidate_ids={item['candidate_id'] for item in scoped_candidates}
    facts = {'schema_version': 1, 'kind': 'readonly-periodic-report', 'role': role,
        'run_id': run_id, 'window': {'start': iso(left), 'end': iso(right), 'basis': 'completed_at_half_open'},
        'checked_at': iso(ledger.clock()), 'source_event_id': events[-1]['event_id'] if events else None,
        'event_high_water': snapshot['event_high_water'],
        'events': window, 'evidence_ids': ['event:' + str(item['event_id']) for item in window],
        'role_states': statuses, 'current_works': works,
        'work_checkpoint_events': checkpoints,
        'run': next((item for item in snapshot['runs'] if item['run_id'] == run_id), None),
        'tasks': [item for item in snapshot['tasks'] if item['run_id'] == run_id and
                  (role == 'COORDINATOR' or item['role'] == role)],
        'external_actions': [item for item in snapshot['external_actions'] if item['candidate_id'] in candidate_ids and
                             role == 'COORDINATOR'],
        'candidates': scoped_candidates,
        # Keep check results/supersedes explicit even if historical event
        # bodies move to evidence pages to fit the report input budget.
        'verifications': [item for item in snapshot['verifications']
                          if item['candidate_id'] in scoped_candidate_ids],
        'usage': _usage_packet(summarize(usage_index, role, left, right, project=project)),
        'timing': _timing_facts(usage_index, role, left, right, project, pool_path),
        'event_coverage': {'basis': 'frozen_snapshot', 'may_have_more': False,
                           'matched_events': len(window)},
        'history_policy': 'checkpoint/检查点及其叙述只证明当时记录；未经本轮核验，不得作为当前结论。当前状态使用本次账本时间和水位。',
        'permissions': '只读分析；不领工作、不确认业务输入、不调用shell、派单或审查'}
    from workflow_supplemental import summary
    facts['supplemental_verifications']=summary(snapshot,run_id)
    from workflow_development_policy import facts as automation_facts
    from workflow_verification_lifecycle import facts as retirement_facts
    facts['local_development_automation']=automation_facts(snapshot,run_id)
    facts['verification_retirements']=retirement_facts(snapshot,run_id)
    from workflow_verification_exceptions import facts as exception_facts,blockers
    facts['verification_exceptions']=exception_facts(snapshot,run_id)
    facts['mechanism_blockers']=blockers(snapshot,run_id)
    if pool_path is not None:
        from workflow_observation import saved_health
        facts['observation'] = _observation_facts(ledger,
            saved_health(Path(usage_index).parent.parent, pool_path, now=ledger.clock()))
    from workflow_views import report_packet, fit_report
    from report_summary import MAX_INPUT_BYTES
    report_packet(ledger,facts)
    fit_report(ledger,facts,MAX_INPUT_BYTES-256)
    facts['facts_sha256'] = _hash(facts)
    return facts


def _validated_section_bullets(text, heading, limit=8):
    if not isinstance(text, str) or not 1 <= len(text) <= 3000:
        raise ValueError('REPORT_SECTION_TEXT_REQUIRED:' + heading)
    rows=[]
    for raw in text.splitlines():
        line=re.sub(r'^\s*(?:[-*+]\s+|\d+[.)]\s+)', '', raw).strip()
        if line:
            rows.append(line)
    if not 1 <= len(rows) <= limit:
        raise ValueError('REPORT_SECTION_BULLET_COUNT_REQUIRED:' + heading)
    for line in rows:
        if (len(line)>240 or not re.search(r'[\u3400-\u9fff]',line)
                or not _BOLD_REPORT_FIELD.search(line)
                or len(re.findall(r'[。！？]',line))>1):
            raise ValueError('BOLD_SHORT_SINGLE_FACT_BULLET_REQUIRED:' + heading)
    return rows


def _analysis(value, facts):
    if not isinstance(value, dict):
        raise ValueError('REPORT_MODEL_ANALYSIS_REQUIRED')
    # Native report callers may return the analysis object directly; keep the
    # wrapped form for older providers and fixtures.
    analysis = value.get('analysis') if isinstance(value.get('analysis'), dict) else value
    allowed = {*ANALYSIS_FIELDS, 'evidence_ids', 'sections'}
    if set(analysis) - allowed:
        raise ValueError('UNKNOWN_REPORT_ANALYSIS_FIELD')
    if facts['role'] == 'COORDINATOR':
        for key in ANALYSIS_FIELDS:
            text = analysis.get(key)
            if not isinstance(text, str) or not 1 <= len(text) <= 3000 or not re.search(r'[\u3400-\u9fff]', text):
                raise ValueError('CHINESE_REPORT_ANALYSIS_REQUIRED:' + key)
        sections = analysis.get('sections')
        if not isinstance(sections, dict) or set(sections) != set(COORDINATOR_SECTIONS):
            raise ValueError('COORDINATOR_REPORT_SECTIONS_REQUIRED')
        for key, text in sections.items():
            _validated_section_bullets(text,key,_COORDINATOR_SECTION_LIMITS[key])
    else:
        for key in ANALYSIS_FIELDS:
            text = analysis.get(key)
            if text is not None and (not isinstance(text, str) or not 1 <= len(text) <= 3000
                                     or not re.search(r'[\u3400-\u9fff]', text)):
                raise ValueError('CHINESE_REPORT_ANALYSIS_REQUIRED:' + key)
        sections = analysis.get('sections')
        if not isinstance(sections, dict) or set(sections) != set(PERSONAL_SECTIONS):
            raise ValueError('PERSONAL_REPORT_SECTIONS_REQUIRED')
        for key, items in sections.items():
            if not isinstance(items, list) or not 1 <= len(items) <= _PERSONAL_SECTION_LIMITS[key]:
                raise ValueError('PERSONAL_REPORT_BULLETS_REQUIRED:' + key)
            for item in items:
                if (not isinstance(item, str) or not 1 <= len(item) <= 240
                        or not re.search(r'[\u3400-\u9fff]', item)
                        or not _BOLD_REPORT_FIELD.search(item)):
                    raise ValueError('BOLD_CHINESE_REPORT_BULLET_REQUIRED:' + key)
                if len(re.findall(r'[。！？]', item)) > 1:
                    raise ValueError('PERSONAL_REPORT_ONE_FACT_PER_BULLET_REQUIRED:' + key)
    references = analysis.get('evidence_ids', [])
    if not isinstance(references, list) or any(ref not in facts['evidence_ids'] for ref in references):
        raise ValueError('REPORT_EVIDENCE_REFERENCE_MISMATCH')
    if not value.get('model_request_id') or not value.get('model') or value.get('route') not in ('local', 'cloud', 'unknown'):
        raise ValueError('REPORT_MODEL_REQUEST_RECEIPT_REQUIRED')
    return analysis


def _usage_text(usage):
    def number(value):
        return str(value) if isinstance(value, (int, float)) else '未知'
    def ratio(value):
        return f'{value:.1%}' if isinstance(value, (int, float)) else '未知'
    routes=[]
    token_parts=[]
    for route, label in (('local', '本地'), ('cloud', '云端'), ('unknown', '未知路由')):
        row = usage.get('routes', {}).get(route, {})
        tokens = row.get('known_tokens', {})
        routes.append(f'{label}{number(row.get("calls"))}次/{ratio(row.get("call_ratio"))}')
        token_parts.append(f'{label}{number(tokens.get("prompt_tokens"))}/{number(tokens.get("output_tokens"))}/'
                           f'{number(tokens.get("total_tokens"))}（{ratio(row.get("known_token_ratio"))}）')
    lines = ['**调用分布：** ' + '；'.join(routes),
        '**Token用量：** 本地/云端/未知的 prompt/输出/总量 = ' + '；'.join(token_parts)]
    cache = usage.get('cache') or {}
    lines.append(f'**请求覆盖：** 已采集{number(usage.get("calls"))}次，逻辑请求{number(usage.get("logical_requests"))}次；'
        f'额外尝试{number(usage.get("extra_physical_attempts"))}次，usage覆盖 {ratio(usage.get("usage_coverage_ratio"))}；'
        f'缺usage {number(usage.get("missing_usage_calls"))}次。')
    lines.append(f'**缓存用量：** 读/写token {number(cache.get("cache_read_tokens"))}/{number(cache.get("cache_write_tokens"))}；'
        f'读取比例{ratio(cache.get("known_cache_read_ratio"))}，覆盖{ratio(usage.get("cache_ratio_coverage_ratio"))}；已包含在prompt中。')
    if not usage.get('source_available'):
        lines.append('**用量缺口：** 采集源不可用，不能把未采集当作零调用。')
    elif usage.get('historical_gap') or usage.get('legacy_identity_calls'):
        lines.append('**用量缺口：** 存在历史采集或身份缺口，以上仅为已采集范围。')
    lines.append('**费用：** ' + ('未采集。' if usage.get('cost') is None else str(usage.get('cost'))))
    return lines


def _analysis_bullets(text, label):
    """Turn model prose into short, labeled bullets without changing facts."""
    result = []
    for raw in str(text or '').replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        raw = re.sub(r'^\s*(?:[-*+]\s+|\d+[.)]\s+)', '', raw).strip()
        if not raw:
            continue
        parts = re.split(r'(?<=[。！？])\s*', raw)
        for part in parts:
            part = part.strip()
            if not part:
                continue
            if part.startswith('**') and '**' in part[2:]:
                result.append(part)
                continue
            field = re.match(r'^([^：:，,。；;\n]{1,24})[：:]\s*(.*)$', part)
            if field and field.group(2):
                result.append('**' + field.group(1).strip() + '：** ' + field.group(2).strip())
            else:
                result.append('**' + label + '：** ' + part)
    return result or ['**' + label + '：** 待核实。']


def render_periodic(facts, analysis, *, generated_at=None):
    role = facts['role']
    label = next((row['label'] for row in facts['role_states'] if row['role'] == role), '待核实')
    start = datetime.fromtimestamp(epoch(facts['window']['start']), ZONE).strftime('%m-%d %H:%M')
    end = datetime.fromtimestamp(epoch(facts['window']['end']), ZONE).strftime('%m-%d %H:%M')
    usage = facts['usage']
    evidence_ids=analysis.get('evidence_ids', [])
    references = '、'.join(evidence_ids[:4]) if evidence_ids else '无新增事件引用'
    if len(evidence_ids)>4:
        references += f'等{len(evidence_ids)}项'
    title = {'COORDINATOR': '协调者', 'TESTER': '测试者'}.get(role, '执行者' + role)
    generated = datetime.fromtimestamp(epoch(generated_at), ZONE) if generated_at is not None else datetime.now(ZONE)
    lines = [f'**{title}（{label}）时段总结 · {generated:%Y-%m-%d %H:%M} +08:00**',
             f'- **报告窗口：** {start} 至 {end}（北京时间）；运行 `{facts["run_id"]}`。']
    coverage=facts.get('input_coverage',{})
    observation = facts.get('observation') or {}
    timing = facts.get('timing') or {}
    if timing.get('state') == 'OBSERVED':
        model_time = (f'{timing["client_occupancy_seconds"]:.1f}秒' if timing['client_request_count'] else '待核实')
        wait_time = (f'{timing["dispatch_wait_seconds"]:.1f}秒' if timing['dispatch_records'] else '待核实')
        timing_text = ('**时段计量：** 客户端模型占用 '+model_time+'；角色调度等待 '+wait_time+
                     '；业务阶段及确认空闲时长待核实，维度不相加。')
    else:
        timing_text = '**时段计量：** 客户端模型时长、任务调度等待、业务阶段及确认空闲时长待核实。'
    sections = analysis.get('sections', {})
    if role == 'COORDINATOR':
        rows = {item['role']: item for item in facts['role_states']}
        labels = {'整体目标与进度':'进度要点','执行者进度':'执行者状态',
            '测试者进度':'测试状态','OCR情况和后续计划':'OCR状态',
            '强审查情况':'强审查状态','风险':'风险','下一步计划':'下一步'}
        for heading in COORDINATOR_SECTIONS:
            lines += ['', '**' + heading + '**']
            lines += ['- ' + item for item in _analysis_bullets(sections[heading], labels[heading])]
            roles = ('A', 'B', 'C') if heading == '执行者进度' else ('TESTER',) if heading == '测试者进度' else ()
            for item in roles:
                name = '测试者' if item == 'TESTER' else '执行者' + item
                state = rows.get(item, {}).get('label', '待核实')
                lines.append(f'- **{name}（{state}）：** 详见本区核实条目。')
            if heading == '整体目标与进度':
                lines += ['- ' + value for value in _usage_text(usage)]
                lines += ['- **证据引用：** ' + references + '；事实包 `' + facts['facts_sha256'][:16] +
                          '`；核验时间 ' + facts['checked_at']]
            if heading == '风险' and coverage.get('checkpoint_events_omitted'):
                lines.append('- **证据覆盖：** 检查点共 '+str(coverage['checkpoint_events_total'])+
                    ' 条；最近保留 '+str(coverage['checkpoint_events_retained'])+' 条索引，历史 '+
                    str(coverage['checkpoint_events_omitted'])+' 条按哈希保留；未展开正文未作模型分析。')
    else:
        state = next((row.get('label', '待核实') for row in facts['role_states']
                      if row.get('role') == role), '待核实')
        current_works = facts.get('current_works', [])
        current_facts = ['**角色状态：** ' + state]
        if current_works:
            current_facts += ['**业务工作单：** ' +
                ('任务 '+str(item.get('task_id') or item.get('kind') or '未命名') +
                 (f' / 第 {item["attempt"]} 次' if item.get('attempt') is not None else '') +
                 (f' / '+{'QUEUED':'排队','RUNNING':'进行中','ENDED':'已结束'}.get(item['state'],item['state']) if item.get('state') else ''))
                for item in current_works]
        else:
            current_facts.append('**业务工作单：** 当前没有占用业务工作单。')
        for heading in PERSONAL_SECTIONS:
            lines += ['', '**' + heading + '**']
            if heading == '当前工作':
                items = current_facts + [timing_text] + sections[heading]
            elif heading == '本次完成':
                items = sections[heading] + _usage_text(usage) + [
                    '**证据引用：** ' + references + '；事实包 `' + facts['facts_sha256'][:16] +
                    '`；核验时间 ' + facts['checked_at']]
            elif heading == '阻塞与依赖':
                items = sections[heading]
                if observation:
                    observed_at = observation.get('observed_at', observation.get('last_verified_at'))
                    sample_time = iso(observed_at) if observed_at is not None else '待核实'
                    reasons = observation.get('reason_codes', [])
                    reason_text = '、'.join(str(reason)[:36] for reason in reasons[:2])
                    if len(reasons) > 2:
                        reason_text += f'等{len(reasons)}项'
                    source_text='状态 '+observation.get('state', 'UNKNOWN')+'；采样 '+sample_time
                    if reason_text:
                        source_text+='；主要缺口 '+reason_text
                    items += [_bounded_report_item('观察来源',source_text)]
                if coverage.get('checkpoint_events_omitted'):
                    items += [_bounded_report_item('证据覆盖','检查点共 '+str(coverage['checkpoint_events_total'])+
                        '条；最近保留 '+str(coverage['checkpoint_events_retained'])+'条索引，历史 '+
                        str(coverage['checkpoint_events_omitted'])+'条按哈希保留；未展开正文未作模型分析。')]
                if coverage.get('omitted_sections'):
                    items += [_bounded_report_item('预算省略',
                        str(coverage.get('notice') or '部分正文未展开')+'；共 '+
                        str(len(coverage['omitted_sections']))+'组明细按哈希保留，未展开内容不视为已阅读。')]
                elif facts.get('detail_artifacts') and not coverage.get('checkpoint_events_omitted'):
                    items += [_bounded_report_item('证据覆盖',
                        '补充正文按哈希保留；未展开内容不视为已阅读，门禁以实际验证记录为准。')]
            else:
                items = sections[heading]
            lines += ['- ' + item for item in items]
    rendered='\n'.join(lines)
    invalid=_periodic_render_format_error(rendered,role)
    if invalid:
        raise ValueError(invalid)
    return rendered


def failed_periodic_report(facts, *, generated_at=None, failure_reason=None):
    """Keep the role's report contract when analysis fails; expose only collected facts."""
    generated = datetime.fromtimestamp(epoch(generated_at), ZONE) if generated_at is not None else datetime.now(ZONE)
    role = facts.get('role')
    title = {'COORDINATOR': '协调者', 'TESTER': '测试者'}.get(role, '执行者' + str(role))
    role_states = {row.get('role'): row for row in facts.get('role_states', []) if isinstance(row, dict)}
    works = facts.get('current_works', []) if isinstance(facts.get('current_works'), list) else []
    events = facts.get('events', []) if isinstance(facts.get('events'), list) else []
    evidence = facts.get('evidence_ids', []) if isinstance(facts.get('evidence_ids'), list) else []
    reference = '、'.join(str(item) for item in evidence[:3]) if evidence else '无新增事件引用'
    if len(evidence) > 3:
        reference += f'等{len(evidence)}项'
    fact_ref = str(facts.get('facts_sha256', '待核实'))[:16]
    window = facts.get('window') or {}
    try:
        start = datetime.fromtimestamp(epoch(window['start']), ZONE).strftime('%m-%d %H:%M')
        end = datetime.fromtimestamp(epoch(window['end']), ZONE).strftime('%m-%d %H:%M')
        window_text = f'{start} 至 {end}（北京时间）'
    except (KeyError, TypeError, ValueError):
        window_text = '时间范围待核实'
    role_label = role_states.get(role, {}).get('label', '待核实')
    lines = [f'**{title}（{role_label}）时段总结 · {generated:%Y-%m-%d %H:%M} +08:00**',
             _live_bullet('报告窗口', window_text)]

    def status(who):
        return role_states.get(who, {}).get('label', '待核实')

    if role == 'COORDINATOR':
        lines += ['', '**整体目标与进度**', '',
                  _live_bullet('汇报状态', '本次总结未生成，未形成业务分析；已采集事实仍可追溯。'),
                  *('- ' + item for item in _usage_text(facts.get('usage') or {})),
                  _live_bullet('证据引用', f'{reference}；事实包 `{fact_ref}`；事件数 {len(events)}。'),
                  '', '**执行者进度**']
        for who in ('A', 'B', 'C'):
            own = [row for row in works if row.get('role') == who]
            detail = '；'.join(str(row.get('task_id') or row.get('kind') or '工作单待核实') for row in own[:2])
            lines.append(_live_bullet(f'执行者{who}（{status(who)}）', '当前任务：'+detail if detail else '本次总结未分析该角色的工作明细。'))
        tester_works = [row for row in works if row.get('role') == 'TESTER']
        tester_detail = '；'.join(str(row.get('task_id') or row.get('kind') or '工作单待核实') for row in tester_works[:2])
        lines += ['', '**测试者进度**',
                  _live_bullet(f'测试者（{status("TESTER")}）', '当前任务：'+tester_detail if tester_detail else '本次总结未分析测试明细，验收结论以账本回执为准。'),
                  '', '**OCR情况和后续计划**',
                  _live_bullet('OCR状态', '本次总结未完成分析；当前状态以本轮账本记录为准。'),
                  '', '**强审查情况**',
                  _live_bullet('强审状态', '本次总结未完成分析；授权与结果以独立记录为准。'),
                  '', '**风险**',
                  _live_bullet('报告缺口', '本次模型总结未生成；不据此改变验收门禁或业务状态。'),
                  '', '**下一步计划**',
                  _live_bullet('接续', '按既定巡检与汇报周期继续；收到有效业务回执后再核对结论。')]
    else:
        own = [row for row in works if row.get('role') == role]
        current = '；'.join(str(row.get('task_id') or row.get('kind') or '工作单待核实') for row in own[:3])
        lines += ['', '**当前工作**',
                  _live_bullet('角色状态', status(role)),
                  _live_bullet('业务工作单', '当前任务：'+current if current else '当前没有占用工作单记录。'),
                  '', '**本次完成**',
                  _live_bullet('汇报状态', '本次总结未生成，未形成新增成果分析。'),
                  *('- ' + item for item in _usage_text(facts.get('usage') or {})),
                  _live_bullet('证据引用', f'{reference}；事实包 `{fact_ref}`；事件数 {len(events)}。'),
                  '', '**阻塞与依赖**',
                  _live_bullet('核验状态', '总结未生成不代表业务失败或通过；状态与门禁以账本回执为准。'),
                  '', '**下一步计划**',
                  _live_bullet('接续', '按本人现有派单和前置条件继续；需复核时使用当次只读进度。')]
    if failure_reason:
        position = lines.index('**风险**') if role == 'COORDINATOR' else lines.index('**阻塞与依赖**')
        lines.insert(position + 1, _live_bullet('未生成原因', failure_reason))
    rendered = '\n'.join(lines)
    invalid = _periodic_render_format_error(rendered, role)
    if invalid:
        raise ValueError('FAILED_PERIODIC_REPORT_FORMAT_INVALID:' + invalid)
    return rendered


def _bounded_report_item(label, text, limit=205):
    """Keep generated evidence notes scannable while pointing to the full packet."""
    value=re.sub(r'\s+',' ',str(text or '')).strip()
    if len(value)>limit:
        value=value[:limit-1].rstrip()+'…'
    return '**'+label+'：** '+value


def _periodic_render_format_error(text, role):
    expected=COORDINATOR_SECTIONS if role=='COORDINATOR' else PERSONAL_SECTIONS
    lines=text.splitlines()
    headings=['**'+name+'**' for name in expected]
    positions=[index for index,line in enumerate(lines) if line in headings]
    if [lines[index] for index in positions]!=headings:
        return 'PERIODIC_REPORT_SECTIONS_INVALID'
    if not lines or not lines[0].startswith('**') or not lines[0].endswith('**'):
        return 'PERIODIC_REPORT_TITLE_INVALID'
    for index,line in enumerate(lines):
        if index==0 or not line.strip() or line in headings:
            continue
        if not line.startswith('- ') or not _BOLD_REPORT_FIELD.match(line[2:]):
            return 'PERIODIC_REPORT_BULLET_INVALID'
        body=re.sub(r'^\*\*[^*]+\*\*\s*','',line[2:])
        body=re.sub(r'`[^`]*`','',body)
        chinese=len(re.findall(r'[\u3400-\u9fff]',body))
        english_words=len(re.findall(r'\b[A-Za-z]{3,}\b',body))
        if (len(line[2:])>240 or not chinese or english_words>max(10,chinese*2)
                or len(re.findall(r'[。！？]',body))>4):
            return 'PERIODIC_REPORT_BULLET_INVALID'
    if '[SILENT]' in text or '[NO_REPLY]' in text:
        return 'PERIODIC_REPORT_SILENT_MIXED'
    return None


def generate_periodic(ledger, role, start, end, *, usage_index, project, analyze,
                      command_id, pool_path=None):
    """Generate once per role/window; failed analysis is recorded as ungenerated."""
    run_id = ledger.snapshot().get('current_run_id')
    identifier = summary_identifier(role, start, end, run_id)
    with locked(ledger.path.with_suffix('.reports.lock')):
        existing = _existing(ledger, identifier)
        if existing:
            return {'status': 'DUPLICATE', 'notification_id': identifier, 'state': existing['state'],
                    'generation': existing['payload']['generation']}
        facts = build_facts(ledger, role, start, end, usage_index=usage_index, project=project, pool_path=pool_path)
        started = ledger.clock()
        generation = {'state': 'FAILED', 'started_at': iso(started), 'facts_sha256': facts['facts_sha256']}
        try:
            response = analyze(facts)
            analysis = _analysis(response, facts)
            generation.update(state='GENERATED', model_request_id=response['model_request_id'],
                model=response['model'], route=response['route'], analysis_sha256=_hash(analysis))
            text = render_periodic(facts, analysis, generated_at=ledger.clock())
        except Exception as exc:
            # Record a bounded error class; provider exception text can contain
            # request data and credentials, and is never copied into group text.
            generation.update(error=type(exc).__name__, reason='本次总结未生成')
            text = failed_periodic_report(facts, generated_at=ledger.clock())
        generation['ended_at'] = iso(ledger.clock())
        payload = {'text': text, 'generation': generation, 'facts': facts,
                   'facts_sha256': facts['facts_sha256'], 'window': facts['window']}
        queued = ledger.queue_notification('PERIODIC_SUMMARY', payload, command_id=command_id,
            source_event_id=facts['source_event_id'], run_id=facts['run_id'], role=role, notification_id=identifier)
        return {**queued, 'generation': generation}


def _structure_summary(text):
    """Give a role-written delivery summary the structure the group needs.

    A summary arrives as plain prose: no bold, key numbers buried mid-sentence.
    Feishu then renders one undifferentiated block that nobody scans. This adds
    only structure — bold labels, code spans, list markers, blank lines — and
    never rewrites wording, so the facts the role reported stay as written.
    """
    if not isinstance(text, str):
        return ''
    if not text.strip():
        return text
    out = []
    for raw in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        line = raw.rstrip()
        if not line.strip():
            if out and out[-1] != '':
                out.append('')
            continue
        out.append(_structure_line(line))
    return '\n'.join(out).strip('\n')


_SECTION_MARK = re.compile(r'^([一二三四五六七八九十]{1,3})、\s*')
_ITEM_MARK = re.compile(r'^(\d{1,2})\s*[)．]\s*')
_LABEL_HEAD = re.compile(r'^([^：:，。；、【\]\s][^：:，。；、\n]{0,19})[：:]\s*')
_BRACKET = re.compile(r'【[^】]{1,60}】')
_FINDING_ID = re.compile(r'(?<![\w`-])((?:[a-z]{1,3}\d{1,3})-\d{2,4})(?![\w`-])')
_HASH = re.compile(r'(?<![\w`])(?=[0-9a-f]*[a-f])[0-9a-f]{7,64}(?![\w`])')
_COUNT = re.compile(r'(?<![\w`])(\d+\s*(?:passed|failed|errors?|items?|新增|项))(?![a-zA-Z`])')


def _structure_line(line):
    indent = line[:len(line) - len(line.lstrip())]
    body = line.strip()
    if body.startswith('**'):
        return line
    match = _SECTION_MARK.match(body)
    if match:
        rest = body[match.end():].lstrip()
        # A section heading already carrying its own bold span must not be
        # double-wrapped, which renders as literal asterisks in Feishu.
        if rest.startswith('**'):
            return indent + '**' + match.group(1) + '、** ' + _emphasis(rest[2:].lstrip())
        return indent + '**' + match.group(1) + '、' + _heading_tail(rest)
    match = _ITEM_MARK.match(body)
    if match:
        return indent + '- **' + match.group(1) + '）** ' + _emphasis(body[match.end():].lstrip())
    match = _LABEL_HEAD.match(body)
    if match:
        return indent + '**' + match.group(1) + '：** ' + _emphasis(body[match.end():].lstrip())
    return indent + _emphasis(body)


def _heading_tail(text):
    """Bold the short title of a section heading, leaving prose outside it."""
    if not text:
        return '**'
    rest = text
    title = ''
    # A bracketed tag opens the heading: 【必修 b02-001】node_path_service...
    bracket = re.match(r'^【[^】]{1,60}】', rest)
    if bracket:
        title, rest = bracket.group(0), rest[bracket.end():].lstrip()
    else:
        cut = len(rest)
        for mark in ('：', ':', '。', '，'):
            index = rest.find(mark)
            if 0 < index < cut:
                cut = index
        if cut < len(rest) and cut <= 40:
            title, rest = rest[:cut], rest[cut:]
        elif len(rest) <= 40 and not rest.endswith(('：', ':')):
            title, rest = rest, ''
    if not title:
        return _emphasis(text) + '**'
    # A tail starting with punctuation glues to the closing bold, not to a space.
    glue = '' if rest[:1] in ('：', ':', '。', '，', '；', '、') else ' '
    tail = _emphasis(rest).strip()
    return title + '**' + (glue + tail if tail else '')


def _emphasis(text):
    def span(pattern, wrap):
        def replace(match):
            if text[match.start() - 1:match.start()] == wrap or text[match.end():match.end() + 1] == wrap:
                return match.group(0)
            if wrap == '**' and text[match.start() - 2:match.start()] == '**':
                return match.group(0)
            return wrap + match.group(0) + wrap
        return pattern.sub(replace, text)
    text = span(_BRACKET, '**')
    text = span(_FINDING_ID, '`')
    text = span(_HASH, '`')
    text = span(_COUNT, '`')
    return text


def render_notification(ledger, item):
    payload, kind = item['payload'], item['kind']
    if isinstance(payload.get('text'), str):
        return payload['text']
    if kind=='VERIFICATION_EXCEPTION_REPAIR_QUEUED':
        return ('**补验异常已登记返修**\n\n**任务：** '+str(payload['task_id'])+' / attempt '+str(payload['attempt'])+
            '\n\n**责任人：** '+str(payload['owner'])+'；'+str(payload['reason'])+
            '\n\n**边界：** 原 scope、AC 与必要验证保持；旧结果仍为未验证，修复后按固定新候选独立复验。')
    if kind in ('INTEGRATION_VERIFICATION_QUEUED','INTEGRATION_VERIFICATION_REPORTED','FINAL_ACCEPTANCE_REWORK_QUEUED','REQUIREMENT_COMPLETED'):
        labels={'INTEGRATION_VERIFICATION_QUEUED':'最终集成验证已自动排队',
            'INTEGRATION_VERIFICATION_REPORTED':'最终集成验证已交付',
            'FINAL_ACCEPTANCE_REWORK_QUEUED':'最终验收问题已回派','REQUIREMENT_COMPLETED':'本地需求验收已收口'}
        return (f"**{labels[kind]}**\n\n**范围：** 已接受的本地开发；实际状态以账本及证据回执为准。\n\n"
            +str(payload.get('summary') or ('工作单：'+str(payload.get('work_id') or payload.get('task_id')))))
    if kind == 'WORK_CLAIMED':
        return f'{item["role"]} 已领取 {payload["task_id"]}，第 {payload["attempt"]} 次开发。'
    if kind in ('SERVICE_ARTIFACT_RECORDED','EXTERNAL_ACTION_REJECTED'):
        candidate=next(row for row in ledger.snapshot()['candidates']
                       if row['candidate_id']==payload['candidate_id'])
        observed=datetime.fromtimestamp(item['created_at'],ZONE)
        state=payload.get('state','REJECTED')
        return (f'**协调者 · 审查阻塞 · {observed:%Y-%m-%d %H:%M} +08:00**\n\n'
                f'{candidate["task_id"]} / attempt {candidate["attempt"]}：'
                f'审查 {payload["action_id"]} 为 {state}。\n\n'
                '完整审查门禁保留；协调者须核实失败证据与已有发现，'
                '已确认问题按原授权返修，不能把补审失败视为通过或静默等待。')
    if kind in ('CANDIDATE_DELIVERED', 'CANDIDATE_DECIDED'):
        candidates = [value for value in ledger.snapshot()['candidates']
                      if value['candidate_id'] == payload.get('candidate_id')]
        if len(candidates) != 1:
            raise ValueError('NOTIFICATION_CANDIDATE_UNVERIFIED')
        candidate = candidates[0]
        status = '已完成' if payload.get('decision') == 'COMPLETE' else '待验收'
        blocks = [
            f'**【{candidate["role"]} 交付（{status}）】**',
            f'**{candidate["task_id"]}｜第 {candidate["attempt"]} 次开发**',
            _structure_summary(candidate['summary']),
            f'**候选提交：** `{candidate["head"][:12]}`　**AC：** {candidate["ac_version"]}',
        ]
        return '\n\n'.join(block for block in blocks if block)
    raise ValueError('NOTIFICATION_RENDERER_REQUIRED')


def completion_notification(ledger, candidate_id, *, command_id):
    """Reuse the original event outbox entry and its role-written summary."""
    items = [item for item in ledger.outbox_items(include_terminal=True)
             if item['kind'] in ('CANDIDATE_DELIVERED', 'CANDIDATE_DECIDED') and
                item['payload'].get('candidate_id') == candidate_id]
    if not items:
        raise ValueError('COMPLETION_EVENT_NOTIFICATION_REQUIRED')
    item = items[-1]
    return {'notification_id': item['notification_id'], 'state': item['state'],
            'text': render_notification(ledger, item), 'generation': {'state': 'REUSED_ROLE_SUMMARY'}}


def deliver_notification(ledger, notification_id, send, *, command_id):
    """One delivery claim; ambiguous delivery never invokes the callback again."""
    item = _existing(ledger, notification_id)
    if not item:
        raise ValueError('UNKNOWN_NOTIFICATION')
    if item['state'] != 'PENDING':
        return {'status': 'DUPLICATE', 'notification_id': notification_id, 'state': item['state']}
    text = render_notification(ledger, item)
    claimed = ledger.claim_delivery(notification_id, command_id=command_id + ':claim')
    if claimed.get('status') != 'COMMITTED' or claimed.get('state') != 'SENDING':
        return claimed
    try:
        receipt = send(text)
        if not isinstance(receipt, dict) or not receipt.get('message_id'):
            receipt, state = {'reason': 'DELIVERY_ACK_UNVERIFIED'}, 'UNKNOWN'
        else:
            state = 'SENT'
    except Exception as exc:
        receipt, state = {'error': type(exc).__name__, 'reason': 'DELIVERY_OUTCOME_UNKNOWN'}, 'UNKNOWN'
    return ledger.ack_delivery(notification_id, state, receipt=receipt, command_id=command_id + ':ack')


def render_status_brief(facts):
    """The fixed status-only brief is entirely mechanical and has no model hook."""
    wanted = ('A', 'B', 'C', 'TESTER')
    by_role = {item['role']: item for item in facts['role_states']}
    return '\n'.join(role + '：' + by_role.get(role, {}).get('label', '待核实') for role in wanted)


def _coordinator_progress_format_error(text):
    """Fail closed on unstructured or mixed-language coordinator progress replies."""
    if not isinstance(text, str):
        return 'REPORT_TEXT_REQUIRED'
    lines = [line.rstrip() for line in text.strip().splitlines()]
    headings = ['**' + title + '**' for title in COORDINATOR_SECTIONS]
    positions = [index for index, line in enumerate(lines) if line in headings]
    if [lines[index] for index in positions] != headings:
        return 'COORDINATOR_SEVEN_SECTIONS_REQUIRED'
    if not lines or not lines[0].startswith('**'):
        return 'COORDINATOR_REPORT_TITLE_REQUIRED'
    if '[SILENT]' in text or '[NO_REPLY]' in text:
        return 'SILENT_MARKER_MIXED_WITH_REPORT'
    if re.search(r'QUERY_[A-Z_]*BYTE_BUDGET|role_live_progress.{0,80}(?:超预算|超支|exceeds.{0,20}budget)',
                 text, re.IGNORECASE | re.DOTALL):
        return 'INTERNAL_QUERY_DIAGNOSTIC_IN_USER_REPORT'
    for index, line in enumerate(lines):
        if index == 0 or not line.strip():
            continue
        if line in headings:
            continue
        if not line.startswith('- ') or not _BOLD_REPORT_FIELD.match(line[2:]):
            return 'BOLD_SINGLE_LEVEL_BULLETS_REQUIRED'
    for section_index, start in enumerate(positions):
        end = positions[section_index + 1] if section_index + 1 < len(positions) else len(lines)
        bullets = [line[2:] for line in lines[start + 1:end] if line.startswith('- ')]
        if not bullets or len(bullets) > 8:
            return 'COORDINATOR_SECTION_CONTENT_REQUIRED'
        for bullet in bullets:
            body = re.sub(r'^\*\*[^*]+\*\*\s*', '', bullet)
            body = re.sub(r'`[^`]*`', '', body)
            chinese = len(re.findall(r'[\u3400-\u9fff]', body))
            english_words = len(re.findall(r'\b[A-Za-z]{3,}\b', body))
            if (len(bullet) > 240 or (not chinese and english_words)
                    or english_words > max(10, chinese * 2)
                    or len(re.findall(r'[。！？]', body)) > 4):
                return 'SIMPLIFIED_CHINESE_REPORT_REQUIRED'
    return None


def _progress_report_format_error(text, role):
    """Validate manual progress output at the final reply seam."""
    if role == 'COORDINATOR':
        return _coordinator_progress_format_error(text)
    if role not in ('A', 'B', 'C', 'TESTER'):
        return 'ROLE_PROGRESS_FORMAT_UNSUPPORTED'
    if not isinstance(text, str):
        return 'REPORT_TEXT_REQUIRED'
    lines = [line.rstrip() for line in text.strip().splitlines()]
    headings = ['**' + title + '**' for title in PERSONAL_SECTIONS]
    positions = [index for index, line in enumerate(lines) if line in headings]
    if [lines[index] for index in positions] != headings:
        return 'PERSONAL_FOUR_SECTIONS_REQUIRED'
    if not lines or not lines[0].startswith('**') or not lines[0].endswith('**'):
        return 'PERSONAL_REPORT_TITLE_REQUIRED'
    if '[SILENT]' in text or '[NO_REPLY]' in text:
        return 'SILENT_MARKER_MIXED_WITH_REPORT'
    for index, line in enumerate(lines):
        if index == 0 or not line.strip() or line in headings:
            continue
        if not line.startswith('- ') or not _BOLD_REPORT_FIELD.match(line[2:]):
            return 'BOLD_SINGLE_LEVEL_BULLETS_REQUIRED'
    for section_index, start in enumerate(positions):
        end = positions[section_index + 1] if section_index + 1 < len(positions) else len(lines)
        bullets = [line[2:] for line in lines[start + 1:end] if line.startswith('- ')]
        if not 1 <= len(bullets) <= 8:
            return 'PERSONAL_SECTION_CONTENT_REQUIRED'
        for bullet in bullets:
            body = re.sub(r'^\*\*[^*]+\*\*\s*', '', bullet)
            body = re.sub(r'`[^`]*`', '', body)
            chinese = len(re.findall(r'[\u3400-\u9fff]', body))
            english_words = len(re.findall(r'\b[A-Za-z]{3,}\b', body))
            if (len(bullet) > 240 or not chinese or english_words > max(10, chinese * 2)
                    or len(re.findall(r'[。！？]', body)) > 1):
                return 'PERSONAL_REPORT_BULLET_INVALID'
    return None


_LIVE_PHASE_LABELS = {
    'PENDING': '待领取', 'READY': '待启动', 'PLANNED': '已规划', 'DISPATCHED': '已派发',
    'DEVELOPING': '开发中', 'TESTING': '验证中', 'REVIEW': '等待审查', 'INTEGRATING': '集成中',
    'COMPLETED': '已登记完成', 'CANCELLED': '已取消', 'BLOCKED': '阻塞', 'WAITING': '等待前置条件',
}


def _live_role_label(row, tasks):
    control = str(row.get('control') or 'ACTIVE').upper()
    if control == 'PAUSED':
        return '暂停'
    if control not in ('ACTIVE', 'RUNNING'):
        return '阻塞'
    if row.get('writer_execution_id'):
        return '工作中'
    if row.get('current_work_id'):
        return '等待'
    unfinished = [task for task in tasks if task.get('phase') not in ('COMPLETED', 'CANCELLED')]
    return '等待' if unfinished else '空闲'


def _live_bullet(label, value, *, limit=205):
    text = re.sub(r'\s+', ' ', str(value or '')).strip()
    text = text.replace('**', '').replace('`', '')
    if len(text) > limit:
        text = text[:limit - 1].rstrip() + '…'
    return '- **' + label + '：** ' + (text or '待核实')


def _read_progress_context(packet):
    """Verify report evidence against the exact query waterline before using it."""
    def load(ref):
        if not isinstance(ref, dict) or not ref.get('path'):
            return {}
        path = Path(ref['path'])
        if path.stat().st_size > 262144:
            raise ValueError('REPORT_CONTEXT_TOO_LARGE')
        raw = path.read_bytes()
        if len(raw) != ref.get('bytes') or hashlib.sha256(raw).hexdigest() != ref.get('sha256'):
            raise ValueError('REPORT_CONTEXT_HASH_MISMATCH')
        return json.loads(raw)
    try:
        refs = packet.get('artifacts') or {}
        if refs.get('manifest'):
            refs = load(refs['manifest'])
        context = load(refs.get('report_context'))
        if (context.get('run_id') != (packet.get('run') or {}).get('run_id')
                or context.get('event_high_water') != packet.get('event_high_water')):
            return {}
        return context
    except (OSError, ValueError, TypeError, KeyError):
        return {}


def render_live_progress_fallback(packet, role):
    """Render a concise evidence-only fallback when a manual progress answer fails format checks."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    checked = packet.get('checked_at')
    try:
        stamp = datetime.fromisoformat(str(checked)).astimezone(ZoneInfo('Asia/Shanghai'))
        time_text = stamp.strftime('%Y-%m-%d %H:%M') + ' +08:00'
    except (ValueError, TypeError):
        time_text = '时间待核实'
    role_names = {'COORDINATOR': '协调者', 'A': '执行者 A', 'B': '执行者 B',
                  'C': '执行者 C', 'TESTER': '测试者'}
    tasks = packet.get('tasks') if isinstance(packet.get('tasks'), list) else []
    roles = {row.get('role'): row for row in packet.get('roles', []) if isinstance(row, dict)}
    context = _read_progress_context(packet)
    descriptions = context.get('tasks') or {}
    histories = context.get('roles') or {}
    completion = context.get('completion') or {}
    def history_bullet(who, field='summary'):
        saved = histories.get(who) or {}
        text = saved.get(field)
        if not text:
            return []
        date = str(saved.get('recorded_at') or '时间未知')
        current_work_id=(roles.get(who) or {}).get('current_work_id')
        if saved.get('work_id') and saved['work_id']!=current_work_id:
            return [_live_bullet('前一工作单记录',
                f'{who} · {date} · event:{saved.get("event_id")}；属于已退出或已切换工作单，原文保留在历史附件，不作为当前进展。')]
        return [_live_bullet('历史检查点' if field == 'summary' else '已记录的接续计划',
            f'{who} · {date} · event:{saved.get("event_id")}：{text}（仅反映保存时判断）')]
    title_role = role_names.get(role, '角色')
    if role == 'COORDINATOR':
        lines = [f'**{title_role}｜当前进展核验 · {time_text}**']
        run = packet.get('run') or {}
        completed = sum(task.get('phase') == 'COMPLETED' for task in tasks)
        lines += ['', '**整体目标与进度**',
                  _live_bullet('需求', f"{run.get('requirement_id') or '当前需求待核实'}；已登记完成 {completed}/{len(tasks)} 项，按任务回执确认验收。")]
        final=packet.get('completion') or {}
        final_labels={'NOT_PLANNED':'尚未登记最终验收计划','WAITING_DEVELOPMENT':'等待开发集成',
            'VERIFICATION_QUEUED':'最终集成验证已自动排队','VERIFYING':'最终集成验证进行中',
            'READY_FOR_CLOSURE':'最终验证通过，待协调者证据复核收口','REPAIR_REQUIRED':'最终验证未通过，按问题回派',
            'REPAIRING':'最终验收返修中','SNAPSHOT_STALE':'验证快照已变化，需重新冻结',
            'WAITING_ENVIRONMENT':'最终验证环境待修复，保留未验证状态',
            'COMPLETED':'本地需求验收已收口'}
        if final:
            lines.append(_live_bullet('需求验收', final_labels.get(final.get('state'),final.get('state') or '待核实')+'；开发叶任务完成与需求验收分别记账。'))
        if completion.get('head'):
            lines.append(_live_bullet('固定验证版本', str(completion['head'])[:12]+'；结果及返修按本次账本快照核对。'))
        lines += ['', '**执行者进度**', '']
        for who in ('A', 'B', 'C'):
            assigned = [task for task in tasks if task.get('role') == who and task.get('phase') not in ('COMPLETED', 'CANCELLED')]
            current = roles.get(who, {})
            state = _live_role_label(current, [task for task in tasks if task.get('role') == who])
            detail = '；'.join(f"{task.get('task_id')} attempt {task.get('attempt')} { _LIVE_PHASE_LABELS.get(task.get('phase'), task.get('phase', '状态待核实')) }" for task in assigned[:2])
            if len(assigned) > 2:
                detail += f'；另有 {len(assigned)-2} 项'
            lines.append(_live_bullet(f'{role_names[who]}（{state}）', detail or '当前没有未完成任务记录。'))
            described = [t for t in tasks if t.get('role') == who and descriptions.get(t.get('task_id'), {}).get('description')]
            if described:
                task = (next((t for t in described if t in assigned), None) or described[-1])
                lines.append(_live_bullet(who+' 交付说明', task['task_id']+'（开发者记录）：'+descriptions[task['task_id']]['description']))
            else:
                lines += history_bullet(who)
        lines += ['', '**测试者进度**', '']
        tester_tasks = [task for task in tasks if task.get('role') == 'TESTER' and task.get('phase') not in ('COMPLETED', 'CANCELLED')]
        tester_state = _live_role_label(roles.get('TESTER', {}), [task for task in tasks if task.get('role') == 'TESTER'])
        tester_detail='；'.join(f"{task.get('task_id')} attempt {task.get('attempt')} 验证记录 {task.get('recorded_checks') or '待核实'}" for task in tester_tasks[:2])
        if final.get('state') in ('VERIFICATION_QUEUED','VERIFYING'):
            tester_detail=f"{final.get('validation_task_id') or '最终集成验证'}：{final_labels[final['state']]}；固定版本与逐项证据见最终验收记录。"
        verification_rows=packet.get('supplemental_verifications') or []
        if not tester_detail and verification_rows:
            work_states={'ENDED':'已结束','RUNNING':'验证中','QUEUED':'排队','CANCELLED':'已取消'}
            results={'PASS':'通过','FAIL':'失败','NOT_VERIFIED':'未验证','UNVERIFIED':'未验证'}
            tester_detail='补验记录：'+'；'.join(
                f"{row.get('task_id')} {work_states.get(row.get('state'),row.get('state') or '状态待核实')} / "
                f"{results.get(row.get('result'),row.get('result') or '结果待核实')}"
                for row in verification_rows[:2])
        lines.append(_live_bullet(f'测试者（{tester_state}）', tester_detail or '当前没有未完成测试派单记录。'))
        if completion.get('summary'):
            lines.append(_live_bullet('最近最终验证回执',
                f'event:{completion.get("event_id")} · {completion.get("result")}：{completion["summary"]}'))
        lines += history_bullet('TESTER')
        checks = completion.get('checks') or []
        if checks:
            results = {result: sum(c.get('result') == result for c in checks) for result in ('PASS', 'FAIL', 'UNVERIFIED')}
            lines.append(_live_bullet('逐项验证', f"最近回执通过 {results['PASS']} 项、失败 {results['FAIL']} 项、未验证 {results['UNVERIFIED']} 项；问题及影响见风险区。"))
        serial = packet.get('ocr_serial_gate') or {}
        version = packet.get('ocr_version_gate') or {}
        lines += ['', '**OCR情况和后续计划**', '',
                  _live_bullet('OCR串行门', f"{'占用或状态未知' if serial.get('blocked') else '当前未见活动占用'}；在途计数 {serial.get('blocking_count', '未知')}。"),
                  _live_bullet('OCR版本门', f"状态 {version.get('state', '待核实')}；以当前安装版本准入记录为准。")]
        activity=context.get('review_activity') or {}
        own_actions = [action for action in (activity.get('attention',packet.get('external_actions') or []))
                       if action.get('kind') == 'ocr' and action.get('run_id') == run.get('run_id')
                       and (action.get('current_candidate') is not False or action.get('state') in ('SUBMITTED','UNKNOWN'))]
        for action in own_actions[:4]:
            state=action.get('state')
            if action.get('current_candidate') is False and state not in ('SUBMITTED','UNKNOWN'):continue
            instructions={'RESULT_READY':('审查待裁决','结果已落账；协调者核原报告后裁决'),
                'INCOMPLETE':('审查未完成','缺少可裁决的完整结果；协调者核原因并决定恢复、重审或其他处置，不直接判PASS/FAIL'),
                'FAILED':('审查执行失败','保留执行失败事实；协调者核证处置，不等同代码FAIL'),
                'CANCELLED':('审查已取消','保留取消事实；当前检查仍按有效合同核验'),
                'SUBMITTED':('当前审查','原生执行尚在途，等待原回执，不重复提交'),
                'UNKNOWN':('审查状态待核验','原生结果未知，保留串行围栏，不自动重试')}
            label,next_step=instructions.get(state,('审查状态待核验','核对原动作与固定候选'))
            if 'current_candidate' not in action and state not in ('SUBMITTED','UNKNOWN'):
                label,next_step='审查对象待核验','本回包缺少候选版本绑定，不能断言属于当前待办'
            target=f"{action.get('task_id')}"
            if action.get('attempt') is not None:target+=f" a{action['attempt']}"
            lines.append(_live_bullet(label,f"{target}：{state}，动作 {action.get('action_id')}；{next_step}。"))
        if len(own_actions)>4:
            remaining={state:sum(a['state']==state for a in own_actions[4:])
                for state in dict.fromkeys(a['state'] for a in own_actions[4:])}
            lines.append(_live_bullet('其他审查待办',
                f'另有 {len(own_actions)-4} 项：'+ '、'.join(f'{state} {count} 项' for state,count in remaining.items())+
                '；完整对象和原始状态见本次查询附件，不表示已处理。'))
        if activity:
            lines.append(_live_bullet('当前候选 OCR 记录',
                f"{activity.get('current_ocr_pass_count',0)}/{activity.get('current_ocr_candidate_count',0)} 个当前候选已登记 PASS；不替代最终集成验收。"))
        historical=activity.get('historical_incomplete_count',(packet.get('external_action_summary') or {}).get('historical_incomplete_count',0))
        if historical:
            lines.append(_live_bullet('旧候选遗留记录',
                f'{historical} 条历史 INCOMPLETE 保留原记录与证据，不作为当前候选待裁决；历史在途/未知动作仍受原串行门约束。'))
        for task in tasks:
            missing = [name for name, values in (task.get('recorded_checks') or {}).items()
                       if name == 'ocr' and 'PASS' not in values]
            if missing:
                lines.append(_live_bullet('待满足的候选检查', task['task_id']+' 的当前候选尚无 OCR PASS；仍按原门禁取得审查与裁决。'))
        review = packet.get('strong_review') or {}
        authorization = review.get('authorization') or review.get('state') or '待核实'
        active_reviews = review.get('active_calls')
        review_status = f'授权状态 {authorization}'
        review_status += '；在途调用 ' + (str(len(active_reviews)) if isinstance(active_reviews, list) else '待核实')
        review_status += '；审查结论需以已落账结果为准。'
        if context and not review:
            actions = context.get('strong_review_actions') or []
            history_count=context.get('strong_review_history_count',0)
            review_status = (('当前没有待处置或在途强审动作；历史共 '+str(history_count)+' 项，按原合同核验。' if history_count else
                '本需求账本未登记强审动作；是否需要仍按有效合同，不新增审查要求。') if not actions
                else '本需求已登记 '+str(len(actions))+' 项审查动作；状态 '+ '、'.join(str(a['state']) for a in actions[-3:]))
        lines += ['', '**强审查情况**', '', _live_bullet('强审查', review_status)]
        gates = packet.get('open_gates') or []
        lines += ['', '**风险**', '']
        gate_labels={'VERIFICATION_EXCEPTION_HANDOFF_REQUIRED':'补验异常待协调者核证处置',
            'VERIFICATION_EXCEPTION_REPAIR_PENDING':'补验异常返修/复验待落实',
            'OCR_SERIAL_ACTION_STILL_ACTIVE_OR_UNKNOWN':'OCR仍在途或状态未知',
            'OCR_VERSION_GATE_BLOCKED':'当前OCR版本尚未准入'}
        lines.append(_live_bullet('未关闭门禁', '当前门禁：'+'；'.join(gate_labels.get(gate.get('code'),str(gate.get('code') or gate)) for gate in gates[:3])
            + (f'；另有 {len(gates)-3} 项' if len(gates) > 3 else '') if gates
            else '本次账本快照未列出打开门禁；验收结果仍以各项回执为准。'))
        for issue in completion.get('issues', [])[:3]:
            lines.append(_live_bullet('验收问题 '+str(issue.get('id')),
                '最近回执 event:'+str(completion.get('event_id'))+' · '+str(issue.get('task_id'))+'：'+str(issue.get('summary'))))
        open_exceptions=[i for i in (context or {}).get('verification_exceptions',[]) if i['blocking']]
        for item in open_exceptions[:3]:
            lines.append(_live_bullet('补验异常处置',str(item['task_id'])+' 补验保留未验证结果；协调者负责核证处置并落实返修/调查，'
                '状态 '+item['state']+'；不等待用户重复授权，不能据执行退出判完成。'))
        pending = [task for task in tasks if task.get('phase') not in ('COMPLETED', 'CANCELLED')]
        if not pending and final.get('state') not in (None,'NOT_PLANNED','COMPLETED'):
            pending=[{'task_id':final.get('validation_task_id') or '最终验收','phase':final_labels.get(final['state'],final['state'])}]
        lines += ['', '**下一步计划**', '']
        for item in open_exceptions[:3]:
            steps={'AWAITING_COORDINATOR_DISPOSITION':'核补验原始证据，用异常处置入口登记返修或调查；不等最终集成先失败',
                'WAITING_SAFE_EXIT':'核原writer安全退出，再确认原生新attempt派单',
                'REPAIR_QUEUED':'跟进已落账返修及后续固定候选独立复验，取得真实PASS后显式关闭异常',
                'INVESTIGATING':'按已登记下一行动调查并到期复核，保留UNVERIFIED',
                'BLOCKED_REFRESH_REQUIRED':'核机制阻断原因、刷新前提后重新处置，不重复同因提交'}
            lines.append(_live_bullet('协调者异常处置',str(item['task_id'])+' · event:'+str(item['observation_event_id'])+'：'+
                steps.get(item['state'],'核对异常回执及后续责任')+'。'))
        lines.append(_live_bullet('接续', '；'.join(f"{task.get('task_id')}：{_LIVE_PHASE_LABELS.get(task.get('phase'), task.get('phase', '待核实'))}" for task in pending[:3])
            + (f'；另有 {len(pending)-3} 项' if len(pending) > 3 else '') if pending
            else '依据当前已接受计划核对收尾条件；没有未完成任务记录。'))
        for task in pending[:3]:
            if task.get('phase') == 'REVIEW':
                names = {'independent_test': '独立测试', 'ocr': 'OCR', 'strong_review': '强审查'}
                missing = [names.get(name, name) for name, values in (task.get('recorded_checks') or {}).items()
                           if 'PASS' not in values]
                next_step = '补齐'+ '、'.join(missing)+'结果后由协调者裁决' if missing else '由协调者核对验证证据并裁决'
                lines.append(_live_bullet(str(task.get('role'))+' 接续条件', str(task.get('task_id'))+' 当前候选已交付，'+next_step+'。'))
            else:
                lines += history_bullet(task.get('role'), 'next')
        if final.get('state') in ('REPAIR_REQUIRED', 'REPAIRING'):
            lines.append(_live_bullet('验收接续', '原 owner 按问题返修后，仍需固定新版本的独立验证；满足收口条件后由协调者复核。'))
        return '\n'.join(lines)

    task_rows = [task for task in tasks if task.get('role') == role]
    role_row = roles.get(role, {})
    state = _live_role_label(role_row, task_rows)
    lines = [f'**{title_role}（{state}）进度核验 · {time_text}**', '', '**当前工作**', '']
    active = [task for task in task_rows if task.get('phase') not in ('COMPLETED', 'CANCELLED')]
    lines.append(_live_bullet('当前任务', '，'.join(
        f"{task.get('task_id')} attempt {task.get('attempt')} {_LIVE_PHASE_LABELS.get(task.get('phase'), task.get('phase', '状态待核实'))}" for task in active[:3])
        or ('当前工作单已释放。' if not role_row.get('current_work_id') else '工作单仍在账本中，具体阶段待核实。')))
    lines += ['', '**本次完成**', '', _live_bullet('验证与交付', '本次快照缺少该报告时段的新增事件明细，新增成果待核实。')]
    lines += ['', '**阻塞与依赖**', '']
    gates = packet.get('open_gates') or []
    lines.append(_live_bullet('门禁状态', '当前门禁：'+'，'.join(str(gate.get('code') or gate) for gate in gates[:3])
        + (f'，另有 {len(gates)-3} 项' if len(gates) > 3 else '') if gates
        else '本次快照未列出打开门禁，任务验收仍按原检查项确认。'))
    admission = (packet.get('model_admission') or {}).get(role, {})
    reason = admission.get('reason')
    lines += ['', '**下一步计划**', '', _live_bullet('接续',
        f"按当前任务与有效派单继续，模型接续状态 {reason or '待核实'}。" if active else '等待新的有效派单，当前没有未完成任务记录。')]
    return '\n'.join(lines)


def transform_coordinator_notice(scope, execution_id, text, home, *, kind):
    """协调者实质进展走同源发件箱；定时短报保留原 cron 投递与回执。"""
    from workflow_runtime import open_ledger
    expected_kind='business' if kind=='COORDINATOR_PROGRESS' else 'report'
    if (kind not in ('COORDINATOR_PROGRESS','STATUS_BRIEF','REPORT_FAILURE') or not execution_id
            or scope.get('execution_id')!=execution_id or scope.get('role')!='COORDINATOR'
            or scope.get('kind')!=expected_kind or scope.get('workflow_version')!=1):
        return {'content':'','suppress':True,'metadata':{'reason':'REPORT_EXECUTION_MISMATCH'}}
    if not isinstance(text,str) or not text.strip() or text.strip() in ('[SILENT]','[NO_REPLY]'):
        return {'content':'','suppress':True,'metadata':{'reason':'NO_MEANINGFUL_CHANGE'}}
    if kind == 'COORDINATOR_PROGRESS':
        if re.search(r'(?m)^\s*\[SILENT\]\s*$', text):
            return {'content':'','suppress':True,'metadata':{'reason':'SILENT_MARKER_MIXED_WITH_TEXT'}}
        invalid = _coordinator_progress_format_error(text)
        if invalid:
            try:
                from workflow_query import live_progress
                text=render_live_progress_fallback(live_progress(scope['pool_path'],'COORDINATOR'),'COORDINATOR')
            except Exception:
                return {'content':'','suppress':True,'metadata':{'reason':'REPORT_FORMAT_INVALID'}}
            if _coordinator_progress_format_error(text):
                return {'content':'','suppress':True,'metadata':{'reason':'REPORT_FORMAT_INVALID'}}
    ledger=open_ledger(scope['pool_path'],readonly=False)
    identifier=kind.lower()+'-'+hashlib.sha256(execution_id.encode()).hexdigest()[:32]
    with locked(ledger.path.with_suffix('.reports.lock')):
        item=_existing(ledger,identifier)
        if item is None:
            with ledger._connection() as db:
                run=db.execute('SELECT requirement_id FROM runs WHERE run_id=?',(scope['run_id'],)).fetchone()
                source=db.execute('SELECT MAX(event_id) FROM events WHERE run_id=? OR run_id IS NULL',
                                  (scope['run_id'],)).fetchone()[0]
            if not run or source is None:
                return {'content':'','suppress':True,'metadata':{'reason':'REPORT_SOURCE_UNVERIFIED'}}
            if kind in ('STATUS_BRIEF','REPORT_FAILURE'):
                generated=datetime.fromtimestamp(ledger.clock(),ZONE)
                label='报告失败' if kind=='REPORT_FAILURE' else '定时状态'
                text=(f'**协调者 · {label} · {generated:%Y-%m-%d %H:%M} +08:00**\n\n'
                      f'当前需求：{run[0]}\n\n'+text.strip())
            digest=hashlib.sha256(text.encode()).hexdigest()
            if kind=='COORDINATOR_PROGRESS':
                with ledger._connection() as db:
                    duplicate=db.execute(
                        "SELECT notification_id,state FROM outbox WHERE run_id=? AND kind=? "
                        "AND state!='SUPPRESSED' AND json_extract(payload,'$.generation.rendered_sha256')=? LIMIT 1",
                        (scope['run_id'],kind,digest)).fetchone()
                if duplicate:
                    return {'content':'','suppress':True,'metadata':{
                        'reason':'DUPLICATE_COORDINATOR_PROGRESS','notification_id':duplicate[0],
                        'state':duplicate[1]}}
            queued=ledger.queue_notification(kind,{'text':text,'execution_id':execution_id,
                'generation':{'state':'FAILED' if kind=='REPORT_FAILURE' else 'SCRIPT_GENERATED' if kind=='STATUS_BRIEF' else 'MODEL_FINAL',
                              'rendered_sha256':digest}},command_id='coordinator-notice:'+execution_id,
                source_event_id=source,run_id=scope['run_id'],role='COORDINATOR',notification_id=identifier)
            if queued.get('status') not in ('COMMITTED','DUPLICATE'):
                raise ValueError('COORDINATOR_NOTIFICATION_NOT_QUEUED')
            item=_existing(ledger,identifier)
        metadata={'pool_path':str(scope['pool_path']),'notification_id':identifier,
            'execution_id':execution_id,'native_home':str(Path(home).resolve()),
            'rendered_sha256':item['payload']['generation']['rendered_sha256']}
        if kind=='COORDINATOR_PROGRESS':
            return {'content':'','suppress':True,
                    'metadata':{**metadata,'reason':'BUSINESS_PROGRESS_QUEUED'}}
        if item['state']!='PENDING':
            return {'content':'','suppress':True,'metadata':metadata}
        claim=ledger.claim_delivery(identifier,command_id='native-report-claim:'+execution_id)
        if claim.get('status')!='COMMITTED' or claim.get('state')!='SENDING':
            return {'content':'','suppress':True,'metadata':metadata}
        return {'content':item['payload']['text'],'suppress':False,'metadata':{**metadata,'claimed':True}}


def transform_native_report(scope, execution_id, final_response, agent_identity):
    """Render and claim the sole outbox item before the native channel sends it."""
    from report_summary import complete_native_summary
    from runtime_common import read_json, save_json
    from workflow_runtime import open_ledger
    if not scope or scope.get('kind') != 'report' or scope.get('workflow_version') != 1:
        return None
    if scope.get('execution_id') != execution_id or not execution_id:
        return {'content': '', 'suppress': True, 'metadata': {'reason': 'REPORT_EXECUTION_MISMATCH'}}
    ledger = open_ledger(scope['pool_path'], readonly=False)
    folder = ledger.path.parent/'reports'
    try:
        matches = []
        for artifact in folder.glob('*.input.json'):
            packet = read_json(artifact)
            reference = packet.get('generation', {}).get('execution', {})
            if (reference.get('execution_id') == execution_id and reference.get('job_id') == scope['job_id']
                    and packet.get('role') == scope['role'] and packet.get('facts', {}).get('run_id') == scope['run_id']):
                matches.append(artifact)
        if len(matches) != 1:
            raise ValueError('REPORT_FROZEN_INPUT_NOT_UNIQUELY_MATCHED')
        route = agent_identity.get('route', 'unknown')
        if route == 'unknown':
            import sqlite3
            reference = read_json(matches[0])['generation']['execution']
            usage_path = Path(reference['database']).parent.parent/'runtime/role-usage.db'
            if usage_path.is_file():
                try:
                    with sqlite3.connect(usage_path.resolve().as_uri()+'?mode=ro', uri=True, timeout=1) as db:
                        latest = db.execute('SELECT route FROM role_usage WHERE session_id=? AND logical_request_id=? '
                            'AND ended_at IS NOT NULL ORDER BY ended_at DESC LIMIT 1',
                            (agent_identity.get('session_id'), agent_identity.get('model_request_id'))).fetchone()
                        route = latest[0] if latest else 'unknown'
                except (OSError, sqlite3.Error):
                    route = 'unknown'
        result = complete_native_summary(matches[0], final_response,
            model_request_id=agent_identity.get('model_request_id'), model=agent_identity.get('model'),
            route=route)
        identifier = result['notification_id']
        item = _existing(ledger, identifier)
        metadata = {'pool_path': str(scope['pool_path']), 'notification_id': identifier,
                    'execution_id': execution_id, 'input_artifact': str(matches[0]),
                    'native_home': str(Path(read_json(matches[0])['generation']['execution']['database']).resolve().parent.parent),
                    'rendered_sha256': hashlib.sha256(result['text'].encode()).hexdigest()}
        if not item or item['state'] != 'PENDING':
            return {'content': '', 'suppress': True, 'metadata': metadata}
        claim = ledger.claim_delivery(identifier, command_id='native-report-claim:' + execution_id)
        if claim.get('status') != 'COMMITTED' or claim.get('state') != 'SENDING':
            return {'content': '', 'suppress': True, 'metadata': metadata}
        metadata['claimed'] = True
        return {'content': result['text'], 'suppress': False, 'metadata': metadata}
    except Exception as exc:
        # A malformed/missing frozen input never leaks raw JSON or creates a
        # second sender. Keep the exact native execution's bounded failure.
        save_json(folder/(hashlib.sha256(execution_id.encode()).hexdigest()+'.native-failure.json'),
            {'execution_id': execution_id, 'role': scope['role'], 'run_id': scope['run_id'],
             'state': 'FAILED', 'reason': '本次总结未生成', 'error': type(exc).__name__,
             'recorded_at': iso(ledger.clock())})
        return {'content': '', 'suppress': True, 'metadata': {'reason': 'REPORT_INPUT_UNVERIFIED'}}


def acknowledge_native_report(metadata, *, execution_id, outcome, attempted,
                              delivery_error=False, unverified_targets=None, queued=None,
                              content_sha256=None, stage='native_final', home=None, **kwargs):
    """Use confirmed native delivery facts, never manufacture a channel message ID."""
    if not metadata.get('claimed') or metadata.get('execution_id') != execution_id:
        return None
    if home is None:
        from hermes_constants import get_hermes_home
        home = get_hermes_home()
    if not metadata.get('native_home') or Path(metadata['native_home']).resolve() != Path(home).resolve():
        return {'status': 'REJECTED', 'code': 'REPORT_NATIVE_HOME_MISMATCH'}
    from workflow_runtime import open_ledger
    ledger = open_ledger(metadata['pool_path'], readonly=False)
    item = _existing(ledger, metadata['notification_id'])
    expected = hashlib.sha256((item or {}).get('payload', {}).get('text', '').encode()).hexdigest()
    renderer = (item or {}).get('payload', {}).get('generation', {}).get('rendered_sha256')
    content_verified = bool(item and expected == renderer == metadata.get('rendered_sha256') == content_sha256)
    receipt = {'source': 'native_cron', 'execution_id': execution_id, 'outcome': outcome,
               'attempted': bool(attempted), 'acknowledgement_verified': False, 'stage': stage,
               'native_home': str(Path(home).resolve()),
               'rendered_sha256': metadata.get('rendered_sha256'), 'content_sha256': content_sha256}
    state = 'UNKNOWN'
    if (outcome == 'delivered' and attempted and content_verified
        and stage in ('native_final', 'native_queue_terminal')
        and not (delivery_error or unverified_targets or queued)):
        state = 'SENT'
        receipt['acknowledgement_verified'] = True
    elif outcome in ('suppressed', 'suppressed_acked'):
        state = 'SUPPRESSED'
    else:
        receipt['reason'] = 'DELIVERY_CONTENT_UNVERIFIED' if not content_verified else 'DELIVERY_OUTCOME_UNVERIFIED'
    if item and item['state'] == 'SENT':
        # A gateway may finish while the original worker is still waiting for
        # its queue result. That later worker receipt cannot downgrade/replay it.
        return {'status': 'DUPLICATE', 'notification_id': item['notification_id'], 'state': 'SENT'}
    return ledger.ack_delivery(metadata['notification_id'], state, receipt=receipt,
                              command_id='native-report-ack:' + execution_id + ':' + stage)
