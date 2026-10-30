"""Read-only usage comparison with explicit windows, nulls and field coverage."""
import argparse
import json
from pathlib import Path
import sqlite3

from time_accounting import epoch, iso
from usage_accounting import ALL_TOKEN_FIELDS, known_usage, business_attributed


def _empty():
    return {'physical_requests':0,'logical_requests':0,'known_tokens':{k:None for k in ALL_TOKEN_FIELDS},
        'field_coverage_calls':{k:0 for k in ALL_TOKEN_FIELDS},'statuses':{},'provider_dispatch':{},
        'normalization_versions':{},'business_attributed_calls':0,'usage_present_calls':0,
        'recorded_json':{'tokens':{k:None for k in ALL_TOKEN_FIELDS},
            'field_calls':{k:0 for k in ALL_TOKEN_FIELDS},
            'unverified_tokens':{k:None for k in ALL_TOKEN_FIELDS},
            'unverified_field_calls':{k:0 for k in ALL_TOKEN_FIELDS},
            'cache_read_evidence':{
                'field_presence':{'calls':0,'tokens':None},
                'legacy_nonzero':{'calls':0,'tokens':None},
                'legacy_zero_unresolved':{'calls':0,'tokens':None},
                'other_presence_unknown':{'calls':0,'tokens':None},
                'numerically_supported':{'calls':0,'tokens':None}}}}


def _add(bucket,value):
    bucket['calls']+=1
    bucket['tokens']=(bucket['tokens'] or 0)+value


def audit(path,start,end,*,time_basis='started_at',role=None,project=None,task_kind=None):
    if time_basis not in ('started_at','ended_at'):
        raise ValueError('EXPLICIT_USAGE_TIME_BASIS_REQUIRED')
    left,right=epoch(start),epoch(end)
    if right<=left:
        raise ValueError('INVALID_USAGE_WINDOW')
    path=Path(path).resolve()
    result={'schema_version':1,'source':str(path),'start':iso(left),'end':iso(right),
        'time_basis':time_basis,'interval':'[start,end)','filters':{'role':role,'project':project,'task_kind':task_kind},
        'field_coverage_denominator':'physical_requests',
        'recorded_json_semantics':'Stored normalized JSON values, including historical defaults; not proof of provider field presence.',
        'cache_read_evidence_semantics':{
            'field_presence':'Field presence was captured by the hook.',
            'legacy_nonzero':'Historical nonzero normalized value; the old missing-field zero fallback cannot explain it. Original wire field is not retained.',
            'legacy_zero_unresolved':'Historical zero without field presence; absent and explicit zero cannot be distinguished.',
            'other_presence_unknown':'Value lacks presence evidence and the legacy normalization provenance.',
            'numerically_supported':'Field-presence values plus legacy nonzero values. This does not increase strict field coverage.'},
        'state':'UNKNOWN','total':_empty(),'groups':[],
        'limitations':['Recorded application physical attempts; not a billing estimate.',
            'Unknown token/cache fields remain null; a short execution is not evidence of idle work.',
            'Unattributed API traffic is unknown, not zero; five-minute recovery polls remain enabled.']}
    if not path.is_file():
        return result
    clauses=[time_basis+'>=?',time_basis+'<?']; values=[left,right]
    for field,value in (('role',role),('project',str(Path(project).resolve()) if project else None),('task_kind',task_kind)):
        if value is not None:
            clauses.append(field+'=?'); values.append(value)
    groups={}; logical={}
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        for row in db.execute('SELECT * FROM role_usage WHERE '+' AND '.join(clauses),values):
            key=(row['project'],row['role'],row['task_kind'])
            group=groups.setdefault(key,{**_empty(),'project':key[0],'role':key[1],'task_kind':key[2]})
            measured,presence=known_usage(row)
            recorded=json.loads(row['usage_json']) if row['usage_present'] else {}
            for group_key,group_value in ((None,result['total']),(key,group)):
                group_value['physical_requests']+=1
                group_value['usage_present_calls']+=bool(row['usage_present'])
                logical.setdefault(group_key,set()).add((row['session_id'],row['logical_request_id']))
                group_value['business_attributed_calls']+=business_attributed(row)
                for field in ALL_TOKEN_FIELDS:
                    value=recorded.get(field)
                    if type(value) is int and value>=0:
                        raw=group_value['recorded_json']
                        raw['tokens'][field]=(raw['tokens'][field] or 0)+value
                        raw['field_calls'][field]+=1
                        if not presence[field]:
                            raw['unverified_tokens'][field]=(raw['unverified_tokens'][field] or 0)+value
                            raw['unverified_field_calls'][field]+=1
                        if field == 'cache_read_tokens':
                            evidence=raw['cache_read_evidence']
                            legacy=row['normalization_version']=='legacy-presence-unknown'
                            tier=('field_presence' if presence[field] else
                                  'legacy_nonzero' if legacy and value>0 else
                                  'legacy_zero_unresolved' if legacy else 'other_presence_unknown')
                            _add(evidence[tier],value)
                            if tier in ('field_presence','legacy_nonzero'):
                                _add(evidence['numerically_supported'],value)
                    if presence[field]:
                        group_value['field_coverage_calls'][field]+=1
                        group_value['known_tokens'][field]=(group_value['known_tokens'][field] or 0)+measured[field]
                for name,value in (('statuses',row['status']),('provider_dispatch',
                        'UNKNOWN' if row['provider_dispatched'] is None else 'DISPATCHED' if row['provider_dispatched'] else 'NOT_DISPATCHED'),
                        ('normalization_versions',row['normalization_version'])):
                    group_value[name][value]=group_value[name].get(value,0)+1
    for key,value in [(None,result['total']),*groups.items()]:
        value['logical_requests']=len(logical.get(key,set()))
    result.update(state='OBSERVED',groups=[groups[k] for k in sorted(groups)])
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,required=True)
    parser.add_argument('--start',required=True);parser.add_argument('--end',required=True)
    parser.add_argument('--time-basis',choices=['started_at','ended_at'],required=True)
    parser.add_argument('--role');parser.add_argument('--project');parser.add_argument('--task-kind')
    args=parser.parse_args()
    print(json.dumps(audit(args.database,args.start,args.end,time_basis=args.time_basis,
        role=args.role,project=args.project,task_kind=args.task_kind),ensure_ascii=False,indent=2))
