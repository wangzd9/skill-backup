"""Model-facing projections only; immutable originals are never business ACKs.

Every omitted body has a verified JSON artifact and an explicit coverage count.
Candidate identities, all check results/supersedes and open action states stay
inline. Consumers must not interpret an unexpanded reference as read evidence.
"""
from collections import Counter
import json


def size(value):
    return len(json.dumps(value,ensure_ascii=False,separators=(',',':')).encode())


def archive(ledger, packet, name, value):
    from workflow_runtime import verified_input_view
    ref=verified_input_view(ledger.path.parent/'model-views',name,value,pretty=True)
    packet.setdefault('detail_artifacts',{})[name]=ref
    return ref


def rows_view(ledger, packet, name, rows, fields):
    """Replace verbose named fields, never result/identity/closure metadata."""
    if not any(size(row.get(field))>512 for row in rows for field in fields):
        return rows
    archive(ledger,packet,name,rows)
    output=[]
    for index,row in enumerate(rows):
        projected=dict(row)
        for field in fields:
            if field in row and size(row[field])>512:
                projected[field]={'detail_source':name,'index':index,'field':field,
                                  'bytes':size(row[field]),'expanded':False}
        output.append(projected)
    return output


def business_rows(ledger, packet):
    for name,fields in (('candidates',('summary','evidence')),
                        ('verifications',('evidence',)),
                        ('external_actions',('evidence','receipt'))):
        if name in packet:
            packet[name]=rows_view(ledger,packet,name,packet[name],fields)
    packet['detail_policy']='detail_source 指向 detail_artifacts 中的完整 JSON；index/field 为原始位置。未展开不代表已阅读或已通过。'


def work_packet(ledger, packet):
    history=packet.get('execution_history',[])
    packet['execution_history_total']=len(history)
    packet['execution_history_counts']=dict(Counter(r['state'] for r in history))
    packet['execution_history_omitted']=max(0,len(history)-10)
    if len(history)>10:
        archive(ledger,packet,'execution_history',history)
        packet['execution_history']=history[-10:]
    business_rows(ledger,packet)
    if packet.get('work') and packet.get('checkpoint')==packet['work'].get('checkpoint'):
        packet['checkpoint']={'same_as':'/work/checkpoint'}
    from workflow_model_input import project
    return project(ledger,packet)


def report_packet(ledger, facts):
    if facts.get('input_coverage',{}).get('history_is_not_current_verification'):
        return facts
    checkpoints=facts['work_checkpoint_events']
    facts['checkpoint_events_total']=len(checkpoints)
    facts['checkpoint_events_omitted']=max(0,len(checkpoints)-20)
    recent=checkpoints[-20:]
    if len(checkpoints)>20:
        archive(ledger,facts,'checkpoint_history',checkpoints)
    # Keep small original events byte-equivalent for the migration proof
    # verifier. Their retained index points back to the event; large bodies
    # instead live in the retained record/archive. Each body appears once.
    indices={row['event_id']:index for index,row in enumerate(recent)}
    history_indices={row['event_id']:index for index,row in enumerate(checkpoints)}
    events=[]
    inline={}
    for event in facts['events']:
        if event['kind']=='CHECKPOINT_SAVED' and 'checkpoint' in event.get('payload',{}):
            if event['event_id'] in indices and size(event['payload']['checkpoint'])<=512:
                inline[event['event_id']]=len(events)
            else:
                event={**event,'payload':{**event['payload'],'checkpoint':
                    ({'same_as':'/work_checkpoint_events/'+str(indices[event['event_id']])+'/checkpoint'}
                     if event['event_id'] in indices else
                     {'detail_source':'checkpoint_history','index':history_indices[event['event_id']],
                      'field':'checkpoint','expanded':False})}}
        events.append(event)
    facts['events']=events
    facts['work_checkpoint_events']=rows_view(ledger,facts,'recent_checkpoints',recent,('checkpoint',))
    facts['work_checkpoint_events']=[
        {**row,'checkpoint':{'same_as':'/events/'+str(inline[row['event_id']])+'/payload/checkpoint'}}
        if row['event_id'] in inline else row for row in facts['work_checkpoint_events']]
    business_rows(ledger,facts)
    facts['input_coverage']={'checkpoint_events_total':len(checkpoints),
        'checkpoint_events_retained':len(recent),'checkpoint_events_omitted':max(0,len(checkpoints)-20),
        'history_is_not_current_verification':True,'unexpanded_details_are_not_analysed':True}
    return facts


def fit_report(ledger, facts, limit):
    """Priority view for an unusually large window; no silent JSON truncation.

    The normal projection keeps every event envelope. On overflow those
    historical envelopes move to numbered evidence pages; current task/check
    state remains inline. No additional provider call or notification is made.
    """
    if size(facts)<=limit:
        return facts
    events=facts['events']
    pages=[]; batch=[]
    for event in events:
        if batch and size(batch+[event])>16*1024:
            name='report_events_page_'+str(len(pages)+1)
            pages.append(archive(ledger,facts,name,batch));batch=[]
        batch.append(event)
    if batch:
        pages.append(archive(ledger,facts,'report_events_page_'+str(len(pages)+1),batch))
    # Small retained checkpoints may reference an inline event. Keep their
    # body in the retained index when that event moves out of the model view.
    retained=[]
    for row in facts.get('work_checkpoint_events',[]):
        pointer=row.get('checkpoint',{}).get('same_as') if isinstance(row.get('checkpoint'),dict) else None
        if pointer and pointer.startswith('/events/'):
            row={**row,'checkpoint':events[int(pointer.split('/')[2])]['payload']['checkpoint']}
        retained.append(row)
    if 'work_checkpoint_events' in facts:
        facts['work_checkpoint_events']=retained
    evidence_ids=facts.get('evidence_ids',[])
    facts['events']=[]
    facts['evidence_ids']=[]
    facts['input_coverage'].update(events_total=len(events),event_pages=pages,
        omitted_sections=[{'section':'events','bytes':size(events),'items':len(events)},
                          {'section':'evidence_ids','items':len(evidence_ids),'bytes':size(evidence_ids)}],
        event_kind_counts=dict(Counter(e['kind'] for e in events)),
        notice='因输入预算，历史事件正文改为证据分页引用；本轮模型未分析这些页，不据此推断历史成果或消除失败。')
    if size(facts)>limit and facts.get('work_checkpoint_events'):
        history=facts['work_checkpoint_events']
        archive(ledger,facts,'report_checkpoint_history',history)
        facts['work_checkpoint_events']=[]
        facts['input_coverage']['omitted_sections'].append(
            {'section':'work_checkpoint_events','items':len(history),'bytes':size(history)})
        facts['input_coverage']['notice']+=(
            ' 历史检查点正文另存完整证据；current_works、角色当前状态、全部验证结果和未解除门禁仍保留。')
    if size(facts)>limit and facts.get('candidates'):
        archive(ledger,facts,'report_candidate_history',facts['candidates'])
        fields=('candidate_id','run_id','task_id','attempt','base','head','ac_version','state')
        facts['candidates']=[{k:row[k] for k in fields if k in row}
            if row.get('state')=='SUPERSEDED' else row for row in facts['candidates']]
        facts['input_coverage']['omitted_sections'].append(
            {'section':'superseded_candidate_details','items':sum(
                r.get('state')=='SUPERSEDED' for r in facts['candidates'])})
    if size(facts)>limit and facts.get('external_actions'):
        # 终态历史授权完整归档；当前动作的权限及所有结果/闭合状态保持原文。
        rows=facts['external_actions']
        archive(ledger,facts,'report_terminal_action_history',rows)
        facts['external_actions']=[
            {**row,'authorization':{'detail_source':'report_terminal_action_history',
                'index':index,'field':'authorization','expanded':False}}
            if row.get('state') in ('ENDED','REJECTED') and row.get('authorization') else row
            for index,row in enumerate(rows)]
        facts['input_coverage']['omitted_sections'].append(
            {'section':'terminal_action_authorization_details',
             'items':sum(row.get('state') in ('ENDED','REJECTED') for row in rows)})
    if size(facts)>limit:
        raise ValueError('REPORT_CURRENT_FACTS_EXCEED_BUDGET: bytes='+str(size(facts))+
                         ', limit='+str(limit)+'；当前门禁不可静默丢弃，需进一步分批分析')
    return facts
