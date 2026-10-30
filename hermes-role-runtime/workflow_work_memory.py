"""Optional checkpoint memory with explicit dependency status, never fresh proof."""
import re


def validate(value):
    if not isinstance(value,dict) or type(value.get('version')) is not int or value['version']!=1 or set(value)-{'version','entries'}:
        raise ValueError('INVALID_WORK_MEMORY_VERSION')
    rows=value.get('entries')
    if not isinstance(rows,list) or len(rows)>32:raise ValueError('INVALID_WORK_MEMORY_ENTRIES')
    ids=set()
    for row in rows:
        if (not isinstance(row,dict) or set(row)-{'id','conclusion','dependencies','source_event_ids','next'}
                or not all(isinstance(row.get(k),str) and row[k].strip() and len(row[k])<=8192 for k in ('id','conclusion'))
                or row['id'] in ids or not isinstance(row.get('dependencies'),dict)):
            raise ValueError('INVALID_WORK_MEMORY_ENTRY')
        deps=row['dependencies']
        if set(deps)-{'candidate_id','head','ac_version','task_id','authorization_fingerprint','environment_fingerprint','sources'}:
            raise ValueError('INVALID_WORK_MEMORY_DEPENDENCY')
        if any(not isinstance(v,str) or not v.strip() or len(v)>512 for k,v in deps.items() if k!='sources'):
            raise ValueError('INVALID_WORK_MEMORY_DEPENDENCY')
        if 'next' in row and (not isinstance(row['next'],str) or len(row['next'])>8192):
            raise ValueError('INVALID_WORK_MEMORY_NEXT')
        sources=deps.get('sources',[])
        if not isinstance(sources,list) or len(sources)>16 or any(not isinstance(r,dict)
                or set(r)!={'path','sha256'} or not isinstance(r['path'],str) or not isinstance(r['sha256'],str)
                or not r['path'].strip() or len(r['path'])>4096
                or re.fullmatch('[0-9a-f]{64}',r['sha256']) is None for r in sources):raise ValueError('INVALID_WORK_MEMORY_SOURCES')
        refs=row.get('source_event_ids',[])
        if (not isinstance(refs,list) or len(refs)>64 or any(type(e) is not int or e<1 for e in refs)
                or len(set(refs))!=len(refs)):
            raise ValueError('INVALID_WORK_MEMORY_EVENT_IDS')
        ids.add(row['id'])


def inspect(snapshot, work):
    value=(work or {}).get('checkpoint',{}).get('work_memory')
    if value is None:return None
    try:validate(value)
    except ValueError as exc:return {'status':'INVALID_LEGACY_MEMORY','reason':str(exc),'entries':[],
        'notice':'旧检查点原文保留；无效结构不作为当前事实，重新取证。'}
    from workflow_decision_context import digest,_source_checks
    candidates={c['candidate_id']:c for c in snapshot['candidates']}
    events={e['event_id']:e for e in snapshot['events']}
    run_id=work['run_id'];rows=[]
    grants=[g for g in snapshot.get('authorizations',[]) if g.get('spec',{}).get('run_id') in (None,run_id)]
    # A checkpoint may name a historical file, but that does not authorize new
    # arbitrary filesystem reads. Only hash references already in bound facts.
    registered=set()
    def collect(node):
        if isinstance(node,dict):
            if isinstance(node.get('path'),str) and isinstance(node.get('sha256'),str):
                path=node.get('frozen_path',node['path'])
                if isinstance(path,str):registered.add((path,node['sha256']))
            for child in node.values():collect(child)
        elif isinstance(node,list):
            for child in node:collect(child)
    permitted=[c for c in candidates.values() if c['run_id']==run_id and
        (work['role']=='COORDINATOR' or c['task_id']==work.get('task_id'))]
    ids={c['candidate_id'] for c in permitted}
    collect(permitted)
    collect([v for v in snapshot.get('verifications',[]) if v['candidate_id'] in ids])
    collect([a for a in snapshot.get('external_actions',[]) if a['candidate_id'] in ids])
    selected=[source for entry in value['entries'] for source in entry['dependencies'].get('sources',[])
        if (source['path'],source['sha256']) in registered]
    verified={(c['path'],c['expected_sha256']):c for c in _source_checks(selected)} if selected else {}
    for entry in value['entries']:
        deps=entry['dependencies'];reasons=[]
        candidate=candidates.get(deps.get('candidate_id'))
        if deps.get('candidate_id') and (not candidate or candidate['run_id']!=run_id or candidate['state']!='CURRENT'):
            reasons.append('CANDIDATE_NOT_CURRENT')
        task=next((t for t in snapshot.get('tasks',[]) if t['run_id']==run_id and
            t['task_id']==deps.get('task_id',work.get('task_id'))),None)
        missing=[]
        for key in ('head','ac_version','task_id'):
            source=candidate or (task if key!='head' else None)
            if key in deps:
                if source is None:missing.append(key.upper()+'_DEPENDENCY_UNVERIFIED')
                elif deps[key]!=source.get(key):reasons.append(key.upper()+'_CHANGED')
        checks=[]
        for source in deps.get('sources',[]):
            if (source['path'],source['sha256']) in verified:checks.append(verified[(source['path'],source['sha256'])])
            else:checks.append({**source,'status':'UNVERIFIED','reason':'NOT_REGISTERED_IMMUTABLE_SOURCE',
                'content_read_by_model':False})
        reasons.extend('SOURCE_'+c['status'] for c in checks if c['status']!='FOUND')
        if deps.get('authorization_fingerprint') and deps['authorization_fingerprint']!=digest(grants):
            reasons.append('AUTHORIZATION_CHANGED')
        refs=entry.get('source_event_ids',[])
        valid_refs=[e for e in refs if e in events and events[e].get('run_id')==run_id]
        if not refs or len(refs)!=len(valid_refs):missing.append('READ_OR_DECISION_RECEIPT_UNVERIFIED')
        # Merely naming any event cannot prove a read/semantic review. There is
        # no implicit attestation based on a copied hash or checkpoint body.
        if valid_refs and any(events[e]['kind'] not in ('CANDIDATE_DECIDED','SERVICE_ADJUDICATED',
                'USER_DECISION_RESOLVED','REQUIREMENT_COMPLETED') for e in valid_refs):
            missing.append('SEMANTIC_RECEIPT_UNVERIFIED')
        if not deps.get('environment_fingerprint'):missing.append('ENVIRONMENT_DEPENDENCY_UNVERIFIED')
        else:missing.append('ENVIRONMENT_REQUIRES_CURRENT_REVALIDATION')
        if not deps.get('authorization_fingerprint'):missing.append('AUTHORIZATION_DEPENDENCY_UNVERIFIED')
        rows.append({'id':entry['id'],'status':'STALE' if reasons else 'HISTORICAL_DEPENDENCIES_PARTIALLY_CHECKED',
            'invalidations':list(dict.fromkeys(reasons)),'unverified_dependencies':missing,
            'source_checks':checks,'original_conclusion':entry['conclusion'],'next':entry.get('next'),
            'current_verified':False})
    return {'version':1,'entries':rows,'notice':'历史结论及定位可用于接续；依赖未变不等于本轮核验，未覆盖环境/授权/读取证明须重查。'}
