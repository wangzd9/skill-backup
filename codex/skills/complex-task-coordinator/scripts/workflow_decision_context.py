"""Read-only decision preparation. No verdict, authorization, ACK or dispatch."""
import hashlib
import json


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def sampling(snapshot, packet):
    return {'generation':snapshot['generation'],'run_id':(packet.get('run') or {}).get('run_id'),
        'event_high_water':snapshot['event_high_water'],'ledger_facts':'one_read_transaction',
        'assembled_at':packet['checked_at'],
        'external_checks':'Git, evidence hashes and live gate previews are separately sampled; execution revalidates current authority.'}


def _basis(packet, snapshot, action):
    cid=action.get('candidate_id') or (action.get('payload') or {}).get('candidate_id')
    aid=action.get('action_id')
    candidate=next((c for c in packet['candidates'] if c['candidate_id']==cid),None)
    if aid:
        external=next((a for a in packet['external_actions'] if a['action_id']==aid),None)
        candidate=next((c for c in packet['candidates'] if external and c['candidate_id']==external['candidate_id']),candidate)
    task_id=(candidate or {}).get('task_id') or (action.get('payload') or {}).get('task_id')
    candidates=[c for c in packet['candidates'] if not task_id or c['task_id']==task_id]
    ids={c['candidate_id'] for c in candidates}
    return {'generation':packet['generation'],'run':packet.get('run'),
        'tasks':[t for t in packet['tasks'] if not task_id or t['task_id']==task_id],
        'candidates':candidates,'verifications':[v for v in packet['verifications'] if v['candidate_id'] in ids],
        'external_actions':[a for a in packet['external_actions'] if a['candidate_id'] in ids],
        'decisions':[d for d in snapshot.get('decisions',[]) if d['candidate_id'] in ids],
        'authorizations':[g for g in snapshot.get('authorizations',[]) if g.get('spec',{}).get('run_id') in (None,(packet.get('run') or {}).get('run_id'))
            and (not task_id or g.get('spec',{}).get('task_id') in (None,task_id))],
        'completion':packet.get('completion'),'controls':packet.get('controls'),
        'verification_exceptions':packet.get('verification_exceptions',[]),
        'request_id':action.get('request_id'),
        'user_answer':[e for e in snapshot['events'] if action.get('request_id') and
            e['kind'] in ('USER_DECISION_RECORDED','USER_DECISION_RESOLVED') and
            e['payload'].get('request_id')==action['request_id']],
        'proposed_action':action}


def _target(action):
    fields=('candidate_id','action_id','request_id','kind','task_id','verification_id','new_action_id','observation_event_id')
    return {k:action.get(k,(action.get('payload') or {}).get(k)) for k in fields
            if k in action or k in (action.get('payload') or {})}


def directory(packet, snapshot):
    work=packet.get('work') or {};run_id=(packet.get('run') or {}).get('run_id')
    items=[];seen=set()
    for action in packet.get('next_actions',[]):
        target=_target(action)
        key={'run_id':run_id,'work_id':work.get('work_id'),'action':action['action'],'target':target}
        decision_id='decision-'+digest(key)[:24]
        if decision_id in seen:
            # Identical suggestions may be coalesced; ambiguous targets may not.
            prior=next(i for i in items if i['decision_id']==decision_id)
            if prior['material_fingerprint']!=digest(_basis(packet,snapshot,action)):
                raise ValueError('AMBIGUOUS_DECISION_TARGET')
            continue
        seen.add(decision_id)
        items.append({'decision_id':decision_id,'action':action['action'],
            'target':target,'reason':action.get('reason'),
            'material_fingerprint':digest(_basis(packet,snapshot,action))})
    from workflow_user_decisions import pending
    # The directory lists all obligations, even if next_actions intentionally
    # prioritizes final closure or safe retirement. No pending user answer is lost.
    blockers={'nonterminal_actions':[{'action_id':a['action_id'],'candidate_id':a['candidate_id'],'state':a['state']}
            for a in packet['external_actions'] if a['state'] not in ('ENDED','REJECTED')],
        'verification_results':[{'verification_id':v['verification_id'],'candidate_id':v['candidate_id'],
            'result':v['result'],'supersedes':v.get('supersedes',[])} for v in packet['verifications'] if v['result']!='PASS'],
        'pending_user_answers':[{'request_id':e['payload']['request_id'],'event_id':e['event_id']}
            for e in pending(snapshot,run_id)] if packet['role']=='COORDINATOR' else [],
        'controls':packet.get('controls',[]),
        'unfinished_tasks':[{'task_id':t['task_id'],'attempt':t['attempt'],'phase':t['phase']}
            for t in packet['tasks'] if t['phase'] not in ('COMPLETED','CANCELLED')],
        'historical_failures':packet.get('historical_failures',[]),
        'verification_exceptions':packet.get('verification_exceptions',[]),
        'mechanism_blockers':packet.get('mechanism_blockers',[])}
    return {'version':1,'items':items,'blocker_index':blockers,
        'fingerprint':digest([items,blockers]),'authority':'preparation only; no verdict or permission',
        'sampling':sampling(snapshot,packet)}


def startup_index(ledger, packet, snapshot):
    from workflow_runtime import verified_input_view
    value=directory(packet,snapshot)
    ref=verified_input_view(ledger.path.parent/'decision-views','directory',value,pretty=True)
    scope=packet.get('scope') or {}
    if scope.get('kind')=='business':
        try:
            from workflow_decision_metrics import observe
            observe(ledger,scope,'startup',{},value['items'])
        except Exception:pass
    return {'tool':'role_decision_context','operation':'directory','items_total':len(value['items']),
        'fingerprint':value['fingerprint'],'full_directory':ref,
        'notice':'完整待决目录含未闭合结果、控制与用户答复；prepare按decision_id取原对象和合同，contract按action取完整schema。未展开不等于已读。'}


def _bounded(ledger, value, *, limit=32768):
    from workflow_views import size
    if size(value)<=limit:return value
    from workflow_runtime import verified_input_view
    ref=verified_input_view(ledger.path.parent/'decision-views','material',value,pretty=True)
    envelope={k:value[k] for k in ('status','decision_id','material_fingerprint','sampling','can_ack',
        'items_total','next_offset','fingerprint','refresh_required','source_status') if isinstance(value,dict) and k in value}
    if isinstance(value,dict) and isinstance(value.get('items'),list):
        envelope['items_index']=[{k:item[k] for k in ('kind','id','status','decision_id','action','material_fingerprint')
            if k in item} for item in value['items']]
    return {**envelope,'inline':False,'full_material':ref,'expanded':False,
        'item_count':len(value) if isinstance(value,list) else None,
        'notice':'完整结构超过内联预算；按read_file读取完整对象/行，未展开不得声称已核验。'}


def _contract(role, action):
    from workflow_runtime import COMMANDS
    from workflow_contracts import payload_schema
    extra={'COORDINATOR':{'service_resume','service_reject','extend_task_scope','refresh_task_base',
        'refresh_development_base','complete','complete_requirement','append_development_log',
        'commit_documentation','resolve_user_decision','environment_recovered','resolve_verification_exception'},
        'TESTER':{'verify_supplemental','verify_integration','deliver_integration'}}
    if action not in COMMANDS[role] | extra.get(role,set()) | {'ack_events'}:
        raise ValueError('COMMAND_NOT_ALLOWED_FOR_ROLE')
    return {'action':action,'payload_schema':payload_schema(action),
        'command_id_policy':'同一逻辑动作重试沿用；INVALID_NOT_STARTED可修正同ID；已冻结/启动不得改参重放。',
        'semantic_policy':'result/decision/reason/summary及supersedes均由角色阅读原证据后确认，不自动填PASS。'}


def _objects(packet, snapshot):
    result={}
    for kind,name,key in (('candidate','candidates','candidate_id'),('verification','verifications','verification_id'),
                          ('action','external_actions','action_id')):
        result[kind]={r[key]:r for r in packet[name]}
    run_id=(packet.get('run') or {}).get('run_id')
    result['event']={str(e['event_id']):e for e in snapshot['events'] if e.get('run_id') in (None,run_id)}
    plan=(packet.get('completion') or {}).get('plan') or {}
    result['obligation']={o['id']:o for o in plan.get('obligations',[])}
    report=(packet.get('completion') or {}).get('report') or {}
    result['issue']={o['id']:o for o in report.get('payload',{}).get('issues',[])}
    return result


def _source_checks(value):
    """Check explicit immutable references, without mistaking metadata for reading."""
    import os
    import re
    import stat
    result=[];seen=set();remaining=64*1024*1024
    def identity(info):
        return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
    def walk(node):
        nonlocal remaining
        if isinstance(node,dict):
            if isinstance(node.get('path'),str):
                path=node.get('frozen_path',node['path']);expected=node.get('sha256');key=(str(path),str(expected))
                if key in seen:
                    for child in node.values():walk(child)
                    return
                seen.add(key);status='UNVERIFIED';reason=None;actual=None
                try:
                    if not isinstance(path,str) or not isinstance(expected,str) or re.fullmatch('[0-9a-f]{64}',expected) is None:
                        reason='IMMUTABLE_HASH_REQUIRED'
                    else:
                        # Nonblocking open rejects FIFO/device content; stream
                        # only regular files and detect replacement/mutation.
                        fd=os.open(path,os.O_RDONLY|os.O_NONBLOCK)
                        with os.fdopen(fd,'rb') as stream:
                            before=os.fstat(stream.fileno())
                            if not stat.S_ISREG(before.st_mode):reason='SOURCE_NOT_REGULAR'
                            elif before.st_size>remaining:reason='HASH_BUDGET_EXCEEDED_READ_SOURCE_EXPLICITLY'
                            else:
                                hasher=hashlib.sha256();read=0
                                while remaining>0:
                                    chunk=stream.read(min(1024*1024,remaining))
                                    if not chunk:break
                                    remaining-=len(chunk);read+=len(chunk);hasher.update(chunk)
                                after=os.fstat(stream.fileno());named=os.stat(path)
                                if identity(before)!=identity(after) or identity(after)!=identity(named):
                                    status='CHANGED';reason='SOURCE_CHANGED_DURING_READ'
                                elif read!=after.st_size:reason='HASH_BUDGET_EXCEEDED_READ_SOURCE_EXPLICITLY'
                                else:
                                    actual=hasher.hexdigest();status='FOUND' if actual==expected else 'CHANGED'
                except FileNotFoundError:status='MISSING';reason='SOURCE_NOT_FOUND'
                except (OSError,ValueError):reason='SOURCE_UNREADABLE'
                result.append({'path':path,'expected_sha256':expected,'actual_sha256':actual,
                    'status':status,'reason':reason,'content_read_by_model':False})
            for child in node.values():walk(child)
        elif isinstance(node,list):
            for child in node:walk(child)
    walk(value)
    return result


def _source_status(checks):
    return next((s for s in ('CHANGED','MISSING','UNVERIFIED') if any(c['status']==s for c in checks)), 'FOUND')


def prepare(packet, snapshot, item):
    action=next(a for a in packet['next_actions'] if a['action']==item['action'] and _target(a)==item['target'])
    objects=_objects(packet,snapshot);facts={}
    cid=item['target'].get('candidate_id')
    aid=item['target'].get('action_id')
    if aid:
        facts['action']=objects['action'].get(aid)
        cid=(facts['action'] or {}).get('candidate_id',cid)
    if cid:
        candidate=objects['candidate'].get(cid);facts['candidate']=candidate
        facts['task']=next((t for t in packet['tasks'] if candidate and t['task_id']==candidate['task_id']),None)
        facts['verifications']=[v for v in packet['verifications'] if v['candidate_id']==cid]
        facts['actions']=[a for a in packet['external_actions'] if a['candidate_id']==cid]
    elif item['target'].get('task_id'):
        facts['task']=next((t for t in packet['tasks'] if t['task_id']==item['target']['task_id']),None)
    if action.get('request_id'):
        facts['user_answer']=next(e for e in snapshot['events'] if e['kind']=='USER_DECISION_RECORDED'
            and e['payload']['request_id']==action['request_id'])
        facts['question_link_state']='COORDINATOR_MUST_VERIFY_ORIGINAL_QUESTION_AND_OPTIONS'
    if item['target'].get('observation_event_id'):
        facts['verification_exception']=next((i for i in packet.get('verification_exceptions',[])
            if i['observation_event_id']==item['target']['observation_event_id']),None)
        facts['later_pass_options']=action.get('later_pass_options',[])
    if action['action'] in ('complete_requirement','environment_recovered','verify_integration','deliver_integration'):
        facts['completion']=packet.get('completion');facts['historical_failures']=packet.get('historical_failures')
    draft={k:v for k,v in (action.get('payload') or {}).items() if k not in ('result','decision','summary','supersedes','coverage_review','issue_dispositions')}
    for field in ('candidate_id','action_id','request_id'):
        if action.get(field):draft[field]=action[field]
    if action['action']=='service_submit':
        draft['kind']=action['kind']
        if action.get('authorization'):draft['authorization']=action['authorization']
        facts['background_sources']={'task':facts.get('task'),'candidate':facts.get('candidate'),
            'checks':facts.get('verifications'),'historical_failures':packet.get('historical_failures'),
            'risk_questions':[],'requires_coordinator_review':True}
    review=[]
    state=packet.get('completion') or {};report=state.get('report') or {}
    checks={c['id']:c for c in report.get('payload',{}).get('checks',[])}
    for row in (state.get('plan') or {}).get('obligations',[]):
        if action['action']=='complete_requirement' and row['phase']=='local_closure':
            check=checks.get(row['ac_id'],{})
            review.append({'obligation_id':row['id'],'validation_event_id':report.get('event_id'),
                'scenario':row.get('scenario'),'actual_evidence':check.get('coverage',[]),
                'result':None,'reason':None})
    try:contract=_contract(packet['role'],action['action'])
    except ValueError:contract={'read_only_guidance':action.get('reason')}
    checks=_source_checks(facts)
    return {'decision':item,'facts':facts,'source_checks':checks,'source_status':_source_status(checks),'contract':contract,
        'payload_draft':draft,'semantic_review_rows':review,
        'failure_closure_preview':action.get('failure_closure_preview',[]),
        'required_semantic_fields':'依据完整证据填写结论、理由、覆盖及失败关闭；不得照抄程序建议当作裁决。',
        'execution_policy':'按原command工具提交；执行入口重新校验，材料指纹不是授权。'}


def _query(scope, args):
    from workflow_runtime import guard,open_ledger,current_input
    if not isinstance(args,dict):raise ValueError('DECISION_QUERY_OBJECT_REQUIRED')
    operation=args.get('operation','directory')
    allowed={'directory':{'operation','offset','limit','expected_fingerprint'},
        'prepare':{'operation','decision_id','expected_fingerprint'},
        'contract':{'operation','action'},'evidence':{'operation','requests'}}
    if not isinstance(operation,str) or operation not in allowed or set(args)-allowed[operation]:raise ValueError('INVALID_DECISION_QUERY')
    if 'expected_fingerprint' in args and (not isinstance(args['expected_fingerprint'],str)
            or len(args['expected_fingerprint'])!=64):raise ValueError('INVALID_MATERIAL_FINGERPRINT')
    if scope.get('kind')!='business':raise ValueError('CURRENT_BOUND_WORK_REQUIRED')
    guard(scope,allow_drain=True)
    if operation=='contract':
        if not isinstance(args.get('action'),str):raise ValueError('ACTION_REQUIRED')
        return _contract(scope['role'],args['action'])
    ledger=open_ledger(scope['pool_path']);snapshot=ledger.snapshot(scope['run_id'])
    packet=current_input(scope['pool_path'],scope['role'],scope=scope,snapshot=snapshot,model_view=False)
    catalog=directory(packet,snapshot)
    expected=args.get('expected_fingerprint')
    if operation=='directory':
        if expected is not None and expected!=catalog['fingerprint']:
            return {'status':'CHANGED','reason':'DECISION_DIRECTORY_CHANGED','current_fingerprint':catalog['fingerprint'],'refresh_required':True}
        offset=args.get('offset',0);limit=args.get('limit',16)
        if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=32:raise ValueError('INVALID_DECISION_PAGE')
        result={'status':'FOUND','items_total':len(catalog['items']),'items':catalog['items'][offset:offset+limit],
            'next_offset':offset+limit if offset+limit<len(catalog['items']) else None,
            'fingerprint':catalog['fingerprint'],'blocker_index':_bounded(ledger,catalog['blocker_index']),
            'sampling':catalog['sampling'],'work_memory_validation':packet.get('work_memory_validation'),'can_ack':False}
    elif operation=='prepare':
        item=next((i for i in catalog['items'] if i['decision_id']==args.get('decision_id')),None)
        if item is None:return {'status':'MISSING','decision_id':args.get('decision_id'),'refresh_required':True}
        if expected is not None and expected!=item['material_fingerprint']:
            return {'status':'CHANGED','decision_id':item['decision_id'],'current_fingerprint':item['material_fingerprint'],'refresh_required':True}
        material=prepare(packet,snapshot,item)
        result={'status':material['source_status'],'source_status':material['source_status'],
            'decision_id':item['decision_id'],'material_fingerprint':item['material_fingerprint'],
            'sampling':catalog['sampling'],'material':_bounded(ledger,material),'can_ack':False,
            'refresh_required':material['source_status']!='FOUND'}
    else:
        requests=args.get('requests')
        if not isinstance(requests,list) or not 1<=len(requests)<=8:raise ValueError('EVIDENCE_BATCH_REQUIRES_1_TO_8_ITEMS')
        objects=_objects(packet,snapshot);items=[]
        for request in requests:
            if (not isinstance(request,dict) or set(request)!={'kind','id'} or not isinstance(request['kind'],str) or request['kind'] not in objects
                    or not isinstance(request['id'],str) or not 1<=len(request['id'])<=256):raise ValueError('INVALID_EVIDENCE_ID')
            value=objects[request['kind']].get(request['id'])
            checks=_source_checks(value) if value is not None else []
            status=_source_status(checks) if value is not None else 'MISSING'
            items.append({**request,'status':status,'source_checks':checks,'value':_bounded(ledger,value),
                'object_sha256':digest(value) if value is not None else None})
        result={'items':items,'sampling':catalog['sampling'],'can_ack':False,
            'notice':'逐项状态独立；FOUND只表示取到对象或哈希一致，不证明语义覆盖或已读原文件。'}
    from workflow_decision_metrics import observe
    try:observe(ledger,scope,operation,result,catalog['items'])
    except Exception:pass  # Telemetry cannot disable evidence access.
    return _bounded(ledger,result)


def query(scope, args):
    from workflow_runtime import GuardRejected
    try:return _query(scope,args)
    except GuardRejected:raise  # Identity receipts keep their native diagnostics.
    except ValueError as exc:
        return {'status':'REJECTED','code':str(exc),'phase':'INVALID_NOT_STARTED','can_ack':False,
            'side_effect_state':'NONE','next_action':'按role_decision_context参数更正查询；该查询不提交业务动作。'}
