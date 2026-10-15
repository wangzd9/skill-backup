"""Compact Feishu progress presentation. Evidence states are never inferred from prose."""
from datetime import datetime
import re
import json
from pathlib import Path
from zoneinfo import ZoneInfo

if __package__:
    from .progress_projection import build_projection, parse_task_rows, TERMINAL, PAUSED
else:
    from progress_projection import build_projection, parse_task_rows, TERMINAL, PAUSED

LABELS = {'COORDINATOR': '协调者', 'A': '执行者 A', 'B': '执行者 B', 'C': '执行者 C', 'TESTER': '测试者'}
STATES = {
    'RUNNING': '处理中', 'IN_PROGRESS': '处理中', 'WORKING': '处理中',
    'OBSERVING': '观察运行进展', 'DEVELOPING': '开发中', 'TESTING': '验证中', 'BLOCKED': '阻塞',
    'WAITING': '等待前置条件', 'IDLE_WAITING': '等待新派单', 'IDLE': '等待新派单',
    'NO_ACTION': '巡检无新动作', 'NO_CHANGE': '巡检无新动作',
    'REVIEW_PENDING': '已交付，待审核', 'TEST_REPORT_PENDING': '验证报告待裁决',
    'DELIVERED': '已交付，待审核', 'ACK': '已领取', 'ACKED': '已领取',
    'ACCEPTED': '已接受', 'DONE': '已完成', 'FAILED': '失败', 'PAUSED': '已暂停',
}


def clean(value, limit=100):
    value=re.sub(r'\s+', ' ', str(value or '')).strip()
    value=re.sub(r'[*`<>\[\]]', '', value)
    # User-facing summaries never include raw paths or unbounded log fragments.
    value=re.sub(r'(?<![\w])(?:/[^\s；;，,]+|(?:inbox|receipts|reviews|scratch|sessions)/[^\s；;，,]+)', '证据文档', value)
    value=re.sub(r'(?<!\d)\d{1,2}:\d*[xX?？][\dxX?？]*', '时间未精确记录', value)
    return value if len(value)<=limit else value[:limit-1]+'…'


def instant(value):
    try:
        dt=datetime.fromisoformat(str(value))
        return dt if dt.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def clock_label(dt, at):
    # User-facing reports use reliable minutes; internal ISO timestamps retain precision.
    return dt.astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M')+' +08:00'


def event_time(event, at):
    """Return evidence freshness and a label without inventing occurrence from file time."""
    occurred=instant(event.get('occurred_at'))
    if occurred:
        return occurred, '发生 '+clock_label(occurred,at)
    observed=instant(event.get('observed_at'))
    recorded=instant(event.get('recorded_at'))
    if observed:
        return observed, '发生时间未知；观测 '+clock_label(observed,at)
    if recorded:
        return None, '发生时间未知；补录 '+clock_label(recorded,at)
    return None, '发生时间未知（时间待核对）'


def enrich_records(root, snapshot):
    """Include latest no-change confirmations for display, without waking/acking observers.

    Observer scheduling intentionally filters quiet events; reporting must not mistake that
    filter for stale role activity. Only structured dated records are eligible.
    """
    root=Path(root).resolve()
    roles=tuple(LABELS) if snapshot['role']=='COORDINATOR' else (snapshot['role'],)
    events=dict(snapshot.get('events') or {})
    for role in roles:
        candidates=[]
        for folder in (root/'events'/role, root/'inbox/events'/role):
            if not folder.exists(): continue
            for path in folder.glob('*.json'):
                if not path.resolve().is_relative_to(root): raise ValueError('event outside project')
                before=path.stat()
                row=json.loads(path.read_text())
                after=path.stat()
                if (before.st_mtime_ns,before.st_size)!=(after.st_mtime_ns,after.st_size):
                    raise ValueError('event changed during read')
                if not isinstance(row,dict): raise ValueError('invalid event')
                when=instant(row.get('occurred_at')) or instant(row.get('observed_at')) or instant(row.get('recorded_at'))
                if row.get('role',role)!=role: raise ValueError('event role mismatch')
                if when and row.get('status'):
                    candidates.append((when.timestamp(), after.st_mtime_ns, {**row,'source':str(path.relative_to(root))}))
        old=events.get(role) or {}
        old_at=instant(old.get('occurred_at')) or instant(old.get('observed_at')) or instant(old.get('recorded_at'))
        if candidates:
            latest=max(candidates,key=lambda c:c[:2])
            if not old_at or latest[0]>=old_at.timestamp(): events[role]=latest[2]
    result=task_records(root,{**snapshot,'events':events})
    if (root/'TODO.md').exists() and (root/'ROLE-BINDINGS.json').exists():
        result={**result,'projection':build_projection(root,snapshot=result)}
    return result


def task_records(root,snapshot):
    """Presentation only: include suffixed batch identities missed by legacy routing tables."""
    if snapshot.get('role')!='COORDINATOR': return snapshot
    path=root/'TODO.md'
    if not path.exists(): return snapshot
    before=path.stat()
    body=path.read_text()
    after=path.stat()
    if (before.st_mtime_ns,before.st_size)!=(after.st_mtime_ns,after.st_size):
        raise ValueError('TODO changed during read')
    rows=parse_task_rows(body)
    if not rows: raise ValueError('unrecognized current task table')
    def classification(row):
        state=row['state']
        if state in PAUSED: return 'paused'
        if state in TERMINAL: return 'done'
        return 'open'
    counts={key:sum(classification(r)==key for r in rows.values()) for key in ('done','open','paused')}
    return {**snapshot,'tasks':[r for r in rows.values() if classification(r)=='open'],'task_counts':counts}


def token_label(usage):
    if isinstance(usage,list):
        rows=[r for r in usage if isinstance(r,dict)]
        usage={k:sum(r.get(k,0) for r in rows if isinstance(r.get(k,0),int)) for k in ('input_tokens','output_tokens')} if rows else None
    if not isinstance(usage,dict): return clean(usage or '未知',30)
    values=[]
    for keys,label in ((('input_tokens','prompt_tokens'),'输入'),(('output_tokens','completion_tokens'),'输出')):
        value=next((usage[k] for k in keys if isinstance(usage.get(k),int)),None)
        if value is not None: values.append(label+format(value,',')+' tokens')
    return '、'.join(values) or '未知'


def role_line(snapshot, role, at):
    event=snapshot.get('events',{}).get(role) or {}
    dispatch=snapshot.get('dispatches',{}).get(role) or {}
    when,label=event_time(event,at)
    if dispatch.get('mtime') and (not when or dispatch['mtime']>when.timestamp()):
        return '**有新派单，领取情况待核实**'
    if not event:
        return '**缺少可核实进展**'
    state=STATES.get(event.get('status','').upper(), '状态待核对')
    task=clean(event.get('task'), 48)
    prefix=('`'+task+'`，') if task else ''
    stale=not when or not 0 <= (at-when).total_seconds() <= 600
    suffix='；**当前待核实**' if stale else ''
    return prefix+'记录为**'+state+'**（'+label+'）'+suffix


def render_progress(snapshot, *, at=None, job=None, pending_count=None):
    at=at or datetime.now(ZoneInfo('Asia/Shanghai'))
    if snapshot.get('projection'):
        return render_projection(snapshot['projection'],role=snapshot['role'],at=at,job=job,pending_count=pending_count)
    role=snapshot['role']
    tasks=snapshot.get('tasks',[])
    focus=(snapshot.get('events',{}).get('COORDINATOR') or {}).get('task') if role=='COORDINATOR' else None
    chosen=[t for t in tasks if t.get('task')==focus] or tasks[:2]
    current='、'.join('`'+clean(t['task'],48)+'`' for t in chosen)
    if snapshot.get('error'):
        conclusion='**当前进展无法核实**，需检查记录来源。'
    elif tasks:
        conclusion=(current+' **尚未收口**。')
    elif role!='COORDINATOR' and snapshot.get('confirmed_idle'):
        conclusion='**已核对：当前无待执行任务**。'
    else:
        conclusion='**未找到明确的在办任务记录**，不据此判定全部完成。'
    out=['**'+LABELS[role]+'｜进展汇总 '+clock_label(at,at)+'**', '', conclusion, '']
    counts=snapshot.get('task_counts')
    if counts:
        out.append('- **已完成：** TODO 登记 '+str(counts['done'])+' 项完成；另有 '+str(counts['open'])+' 项尚未收口、'+str(counts['paused'])+' 项暂缓（含父子任务条目，非完成率）。')
    roles=('A','B','C','TESTER') if role=='COORDINATOR' else (role,)
    for who in roles:
        out.append('- **'+LABELS[who]+'：** '+role_line(snapshot,who,at))
    if snapshot.get('event_conflicts'):
        out.append('- **记录冲突：** **相同事件 ID 的内容不一致，待核对**；保留各来源，不据此批准或判定完成。')
    blockers=[LABELS[r] for r in roles if (snapshot.get('events',{}).get(r) or {}).get('status','').upper() in ('BLOCKED','FAILED')]
    if blockers:
        out.append('- **阻塞：** **'+ '、'.join(blockers)+' 有阻塞或失败记录**；解除条件需核对对应证据。')
    else:
        out.append('- **待确认：** 新读取文档不等于生产重新实测；过旧或缺失记录不判为正常、空闲或完成。')
    if role=='COORDINATOR':
        s=snapshot.get('strong_review') or {}
        authorization={'ACTIVE':'有效','REVOKED':'已撤销','EXPIRED':'已到期','NONE':'未授权'}.get(s.get('authorization'), clean(s.get('authorization') or '待核对',24))
        scope=clean('、'.join(s['task_scope']) if isinstance(s.get('task_scope'),list) and s['task_scope'] else s.get('task_scope') or '见授权原文',60)
        deadline_at=instant(s.get('expires_at'))
        deadline=clock_label(deadline_at,at) if deadline_at else clean(s.get('expires_at') or '未指定/见授权原文',32)
        out.append('- **强审查：** '+authorization+'；期限 '+deadline+'；范围 '+scope+'；近60分钟 '+str(s.get('used_last_60_minutes','未知'))+'/5，本授权累计 '+str(s.get('calls_under_authorization','未知'))+'；在途 '+str(len(s.get('active_calls') or []))+'，最近 '+clean((s.get('last_call') or {}).get('status') or '无记录',24)+'；token/费用 '+token_label(s.get('tokens'))+' / '+clean(s.get('cost') if s.get('cost') is not None else '未知',20)+'。')
    own=snapshot.get('events',{}).get(role) or {}
    next_action=own.get('next')
    if next_action:
        next_action=re.split(r'[；;\n]',str(next_action))[0]
        next_action=re.sub(r'（[^）]*）|\([^)]*\)', '', next_action)
        next_action=next_action.replace('observer 接续核','核对').replace('observer 接续','继续核对')
    out.append('- **下一步：** '+clean(next_action or ('协调者核对最新交付和前置条件，按既有门禁接续。' if role=='COORDINATOR' else '按有效派单接续；未知状态先核对。'),110))
    if job is not None:
        poll='已启用' if job.get('enabled') is True else '**暂停或未配置**'
        out.append('- **调度：** 后台巡检'+poll+'；待处理指令 '+str(pending_count or 0)+' 条。查询不会恢复暂停或启动任务。')
    observed=instant(snapshot.get('observed_at'))
    observed_label=clock_label(observed,at) if observed else '时间未知'
    out.append('\n文档读取：'+observed_label+'（非生产实时探测）；各角色分别标注发生、观测或补录时间。需原因分析或调整方向，可直接发消息。')
    return '\n'.join(out)


BUSINESS_STATES = {
    'DONE':'已接受闭环', 'ACCEPTED':'已接受', 'ACCEPT_MERGED':'已接受合入',
    'ACCEPTED_MERGED':'已接受合入', 'CLOSED':'已收口',
    'DELIVERED':'已交付，等待审核和独立验证',
    'DELIVERED_FROZEN':'已交付，等待审查和独立验证',
    'DELIVERED_REVIEW_PENDING':'返修已交付，等待独立验证和裁决',
    'REVIEW_PENDING':'已交付，等待审核', 'NEEDS_REVIEW':'审查未完成',
    'DISPATCHED':'已派单，尚未接受', 'RUNNING':'推进中', 'REWORK':'返修中',
    'REWORK_DISPATCHED':'已派返修', 'ACKED':'已领取，验证准备中', 'ACK':'已领取',
    'PARTIAL':'部分通过，验收范围仍有缺口', 'BLOCKED':'受阻', 'WAITING':'等待前置条件',
    'READY':'待派单', 'PLANNED':'已规划', 'REOPENED':'已恢复推进',
    'CONFLICT':'来源冲突，需核对', 'UNKNOWN':'业务状态未明确记录',
}
OCR_STATES = {'PREPARED':'已准备，尚未启动', 'SUBMITTED':'已提交', 'RUNNING':'运行记录显示正在审查',
              'AWAITING_DECISION':'审查已结束，等待裁决', 'REVIEW_COMPLETE':'审查已完成',
              'FAILED':'审查失败', 'LAUNCH_FAILED':'启动失败', 'INTERRUPTED':'运行中断',
              'HARD_DEADLINE':'达到运行时限，审查未完成', 'REVIEW_INCOMPLETE':'审查未完整完成',
              'CANCELLED':'已取消，审查未完成', 'UNKNOWN':'状态待核实'}
OCR_INCOMPLETE = {'FAILED','LAUNCH_FAILED','INTERRUPTED','HARD_DEADLINE','REVIEW_INCOMPLETE','CANCELLED'}



def render_projection(packet, *, role='COORDINATOR', at=None, job=None, pending_count=None):
    """Use the same role report contract as natural-language model reports."""
    if __package__:
        from .report_contract import build_report, render_report
    else:
        from report_contract import build_report, render_report
    report = build_report(packet, role=role)
    return render_report(report)
