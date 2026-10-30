"""Read current authoritative records for a human-facing query, without business writes."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
from zoneinfo import ZoneInfo

from coord_queue import pending_requests
if __package__:
    from .progress_display import render_progress, enrich_records
    from .workflow_query import report_presentation_contract
else:
    from progress_display import render_progress, enrich_records
    from workflow_query import report_presentation_contract

def progress_query(text):
    # Only explicit opt-in. Natural-language progress always stays in the model conversation.
    return text.strip() == '/progress'


def collect_live(root):
    root = Path(root).resolve()
    from role_status import runtime_module
    # Invalid top-level identity is still fatal. Only independent evidence
    # sources may degrade; querying never changes the business poll contract.
    root, binding = runtime_module().binding(root)
    from role_observer import collect
    failures = (OSError, ValueError, KeyError, TypeError, RuntimeError,
                sqlite3.Error, subprocess.SubprocessError)
    gaps = []
    def gap(source, exc):
        gaps.append(source + ': ' + type(exc).__name__ + ': ' + str(exc)[:240])
    try:
        snapshot,_=collect(root,'COORDINATOR')
        ocr_candidates = snapshot.get('ocr_runs', [])
    except failures as exc:
        gap('业务观察快照未取得', exc)
        snapshot = {'role': 'COORDINATOR', 'project': str(root),
                    'observed_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()}
        ocr_candidates = None  # Independently discover valid OCR records below.
    from runtime_tools import refresh_progress_facts
    try:
        packet=refresh_progress_facts(root,snapshot=snapshot)
    except failures as exc:
        gap('业务文档投影未取得', exc)
        # A fresh empty projection explicitly means unavailable. Never return
        # a previous report merely because an OCR index/document is malformed.
        packet = {'project': str(root), 'observed_at': snapshot['observed_at'],
                  'tasks': {}, 'roles': {}, 'ocr': {'runs': [], 'declared': []},
                  'strong_review': {}, 'risks': [], 'current_conflicts': []}
        from runtime_common import save_json
        digest = hashlib.sha256(json.dumps(packet, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        path = root/'inbox/control/runtime/progress/evidence'/('unavailable-'+digest+'.json')
        save_json(path, packet)
        packet = {**packet, 'fingerprint': digest, 'evidence_reference': str(path)}
    if __package__:
        from .live_progress import collect_live_runtime
    else:
        from live_progress import collect_live_runtime
    from role_status import collect_role_statuses
    observation = collect_live_runtime(root, ocr_candidates=ocr_candidates)
    pool = binding.get('role_pool') or {}
    if pool.get('path'):
        import os
        from workflow_observation import saved_health
        home = Path(os.environ.get('HERMES_HOME') or Path.home()/'.hermes')
        observation['monitor'] = saved_health(home, pool['path'], now=datetime.now().timestamp())
    if binding.get('roles', {}).get('COORDINATOR', {}).get('coordinated_projects'):
        from role_status import report_role_scope
        from coordination_inputs import collect_coordinated_inputs, context_view
        scope = report_role_scope(root)
        observation['coordinated_scope'] = scope
        coordinated = snapshot.get('coordinated_inputs')
        if coordinated is None and not scope['errors']:
            try:
                coordinated = context_view(collect_coordinated_inputs(root, binding, collect))
            except failures as exc:
                gap('显式订阅业务事实未取得', exc)
        observation['coordinated_inputs'] = coordinated or {'projects': [], 'gates': [], 'errors': scope['errors']}
        if scope['errors']:
            gap('显式订阅报告范围待核实', ValueError(json.dumps(scope['errors'], ensure_ascii=False)))
            for who in scope['roles']:
                observation['roles'][who] = {'role': who, 'state': 'UNVERIFIED',
                    'checked_at': observation['checked_at'], 'errors': ['role_report_source_conflict'],
                    'report_scope_conflict': scope['roles'][who]}
        else:
            selected = {}
            for who, origins in scope['roles'].items():
                source = origins[0]['project_root']
                if Path(source) != root:
                    selected.setdefault(source, []).append(who)
            business = {item['project_root']: item for item in (coordinated or {}).get('projects', [])}
            for source, roles in selected.items():
                try:
                    remote = collect_live_runtime(source, selected_roles=roles, include_services=False)
                    for who in roles:
                        item = remote['roles'][who]
                        item.update(project_root=source,
                            coordinated_business=business.get(source, {}).get('roles_state', {}).get(who, {}))
                        for field in ('checkpoint', 'owner'):
                            metadata = item.get(field, {})
                            if metadata.get('source') and not Path(metadata['source']).is_absolute():
                                metadata['source'] = str(Path(source)/metadata['source'])
                        if remote.get('errors'):
                            item['errors'] = [*item.get('errors', []), *remote['errors']]
                            item['state'] = 'UNVERIFIED'
                        observation['roles'][who] = item
                except failures as exc:
                    gap('订阅角色运行核验未取得 '+source, exc)
                    for who in roles:
                        observation['roles'][who] = {'role': who, 'project_root': source, 'state': 'UNVERIFIED',
                            'checked_at': observation['checked_at'], 'errors': [type(exc).__name__+': '+str(exc)[:240]],
                            'coordinated_business': business.get(source, {}).get('roles_state', {}).get(who, {})}
        if any(item.get('state') == 'UNVERIFIED' for item in observation['roles'].values()):
            observation['state'] = 'UNVERIFIED'
    try:
        statuses = collect_role_statuses(root)
    except failures as exc:
        gap('角色状态汇集未取得', exc)
        statuses = {role: item['availability'] for role, item in observation.get('roles', {}).items()
                    if isinstance(item.get('availability'), dict)}
    if gaps:
        observation = {**observation, 'state': 'UNVERIFIED',
                       'collection_gaps': [*observation.get('collection_gaps', []), *gaps]}
    from runtime_common import save_json
    identity=hashlib.sha256(json.dumps(observation,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    observation_path=Path(root)/'inbox/control/runtime/progress/evidence'/('runtime-'+identity+'.json')
    save_json(observation_path,observation)
    observation={**observation,'evidence_reference':str(observation_path)}
    packet = {**packet, 'runtime_observation': observation, 'role_statuses': statuses}
    return {**snapshot, 'projection': packet}


def live_progress_report(project, role='COORDINATOR'):
    """On-demand observation and latest receipts, never an ACK or task action."""
    root = Path(project).resolve()
    snapshot = collect_live(root)
    if snapshot.get('role') != 'COORDINATOR' or Path(snapshot.get('project', '')).resolve() != root:
        raise ValueError('live progress identity mismatch')
    packet = snapshot['projection']
    if __package__:
        from .report_contract import build_report, render_report
    else:
        from report_contract import build_report, render_report
    report = build_report(packet, role)
    report['report_presentation'] = report_presentation_contract(role)
    report['markdown'] = render_report(report)
    report['analysis_instructions'] = ('先依据本次runtime_observation解释实际进程、工程操作、审查服务和代码快照；'
        '业务阶段与验收成果按各自检查点/回执时间核对。核验缺口或冲突保留待核实，进程存活、HEAD或dirty变化不等于新增完成。'
        '结合有效派单和回执分析当前做到哪、具体等待原因与下一步，不机械复述旧记录；必要时按来源补读。'
        '如果用户询问生产任务，使用已配置只读观察器；未配置或失联明确具体缺口。查询不得领取租约、ACK pending、启动测试或审查。'
        '答复分区契约：' + '、'.join(report_presentation_contract(role)['sections']) +
        '。每区保留加粗标题及单层短bullet；字段名、状态/结论、阻塞/风险和下一步加粗。工具或来源报错时仍保留全部分区，'
        '写明具体错误、当前可用证据来源和待核实范围；不以checkpoint自由摘要替代完整结构。')
    return report


def live_progress_tool_packet(project, role='COORDINATOR'):
    """Bounded tool view; the full report is a replaceable observation artifact."""
    report=live_progress_report(project,role)
    from runtime_common import save_json
    path=Path(project)/'inbox/control/runtime/progress'/('live-'+role+'.json')
    save_json(path,report)
    raw=(json.dumps(report,ensure_ascii=False,indent=2)+'\n').encode()
    # Do not replay original narratives, duplicate source objects and service
    # payloads into the conversation. All reported facts and gaps stay in text.
    observation=report.get('runtime_observation',{})
    return {key:report[key] for key in ('kind','role','project','report_basis','checked_at','markdown',
        'analysis_instructions','report_presentation') if key in report} | {
        'runtime_state':observation.get('state'),
        'runtime_errors':[*observation.get('errors',[]), *observation.get('collection_gaps',[])],
        'artifact':{'path':str(path),'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw),
                    'scope':'replaceable current observation; verify hash before supplemental reads'},
        'evidence_reference':report.get('evidence_reference'),
        'runtime_evidence_reference':observation.get('evidence_reference')}


def progress_answer(project,job,at=None):
    root=Path(project).resolve()
    at=at or datetime.now(ZoneInfo('Asia/Shanghai'))
    try:
        snapshot=collect_live(root)
        if snapshot.get('role')!='COORDINATOR' or Path(snapshot.get('project','')).resolve()!=root:
            raise ValueError('progress identity mismatch')
        pending=pending_requests(root)
    except Exception:
        poll='已启用' if job.get('enabled') is True else '**暂停或未配置**'
        return ('**协调者｜当前进展无法核实**\n\n'
                '- **原因：** 本次运行核验或当前证据读取失败；不能确认此刻的实际状态。\n'
                '- **调度：** 后台巡检'+poll+'；本次不改变调度。\n'
                '- **下一步：** 请协调者核对来源后说明进展；可直接发送“请核对当前进展并解释”。')
    return render_progress(snapshot,at=at,job=job,pending_count=len(pending))
