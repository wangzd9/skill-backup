from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import sqlite3,json,time,statistics,collections,re
OUT=Path(__file__).resolve().parent
ledger=json.loads((OUT/'ledger.json').read_text());start=ledger['window']['start'];end=ledger['window']['end']
def db(p):
 c=sqlite3.connect(Path(p).as_uri()+'?mode=ro',uri=True,timeout=1);c.row_factory=sqlite3.Row;return c
def pct(v,p):return sorted(v)[min(len(v)-1,int((len(v)-1)*p))] if v else None
result={}
c=db('/home/wang/.hermes/runtime/role-time.db')
rows=[dict(r) for r in c.execute('select role,model,status,started_at,ended_at,first_chunk_at,timing_basis from api_requests where started_at>=? and started_at<=?',(start,end))]
result['model_latency']=[]
for (role,model,status),items in __import__('itertools').groupby(sorted(rows,key=lambda r:(str(r['role']),str(r['model']),str(r['status']))),lambda r:(r['role'],r['model'],r['status'])):
 a=list(items);duration=[r['ended_at']-r['started_at'] for r in a if r['ended_at'] is not None];first=[r['first_chunk_at']-r['started_at'] for r in a if r['first_chunk_at'] is not None]
 result['model_latency'].append({'role':role,'model':model,'status':status,'count':len(a),'duration_p50':pct(duration,.5),'duration_p95':pct(duration,.95),'duration_max':max(duration) if duration else None,'first_chunk_p95':pct(first,.95)})
result['api_errors']=dict(c.execute('select retry_pending,count(*) from api_errors where occurred_at>=? and occurred_at<=? group by retry_pending',(start,end)).fetchall())
c=db('/home/wang/.hermes/role-pool.runtime/business.sqlite3')
executions=[dict(r) for r in c.execute('select execution_id,work_id,role,bound_at,ended_at from executions where bound_at>=? and bound_at<=?',(start,end))]
trans=ledger['business_transitions']
result['execution_business_window']=[]
for role in sorted({r['role'] for r in executions}):
 a=[r for r in executions if r['role']==role]
 with_transition=[r for r in a if any(e['role']==role and e['work_id']==r['work_id'] and r['bound_at']<=e['created_at']<=(r['ended_at'] or end) for e in trans)]
 result['execution_business_window'].append({'role':role,'executions':len(a),'with_selected_business_transition':len(with_transition),'without_selected_business_transition':len(a)-len(with_transition),'caveat':'同角色工作单时间窗关联，未覆盖所有有用分析，不是模型调用因果或空转判定。'})
result['native_outcomes']=[dict(r) for r in c.execute("select role,json_extract(payload,'$.native_outcome') outcome,count(*) n from events where kind='EXECUTION_OUTCOME_OBSERVED' and created_at>=? and created_at<=? group by role,outcome",(start,end))]
result['top_execution_works']=[dict(r) for r in c.execute('select w.run_id,e.work_id,e.role,count(*) n,min(e.bound_at) first,max(e.bound_at) last from executions e join works w using(work_id) where e.bound_at>=? and e.bound_at<=? group by e.work_id,e.role order by n desc limit 12',(start,end))]
result['recent_rejections']=[dict(r) for r in c.execute("select created_at,verb,json_extract(result,'$.code') code from commands where created_at>=? and created_at<=? and json_extract(result,'$.status')='REJECTED' order by created_at desc limit 30",(start,end))]
result['budget_files']={'count':0,'checkpoint_required':0,'blocked_actions':collections.Counter(),'request_max':0,'input_token_max':0}
for p in Path('/home/wang/.hermes/runtime/role-session-budgets').glob('*.json'):
 try:r=json.loads(p.read_text())
 except (ValueError,OSError):continue
 if not start<=r.get('observed_at',0)<=end:continue
 a=result['budget_files'];a['count']+=1;a['checkpoint_required']+=bool(r.get('checkpoint_required'));a['request_max']=max(a['request_max'],r.get('requests',0));a['input_token_max']=max(a['input_token_max'],r.get('approx_input_tokens',0))
 blocked=r.get('last_blocked_action')
 if blocked:a['blocked_actions'][blocked.get('reason','UNKNOWN')]+=1
c=db('/home/wang/.hermes/runtime/workflow-activity/model_requests/attempts.sqlite3')
result['physical_models']=[dict(r) for r in c.execute("select json_extract(c.identity,'$.role') role,a.kind,a.outcome,count(*) n,max(coalesce(a.ended_at,?)-a.started_at) max_seconds from attempts a join coverage c using(identity_key) where a.started_at>=? and a.started_at<=? group by role,a.kind,a.outcome",(end,start,end))]
# 不保存会话正文、环境变量、密钥或原始提示；日志仅统计时间覆盖和错误类别。
result['log_coverage']=[]
for p in sorted(Path('/home/wang/.hermes/logs').glob('*.log*')):
 if p.name not in {'agent.log','agent.log.1','agent.log.2','agent.log.3','errors.log','gateway.log'}:continue
 ts=[];levels=collections.Counter();exception=collections.Counter();codes=collections.Counter();recent=False
 for line in p.open(errors='replace'):
  m=re.match(r'^(2026-\d\d-\d\d[ T]\d\d:\d\d:\d\d)',line)
  if m:
   try:t=datetime.fromisoformat(m.group(1)).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
   except ValueError:continue
   ts.append(t);recent=start<=t<=end
  if not recent:continue
  for level in ['ERROR','WARNING','CRITICAL']:
   if level in line[:150]:levels[level]+=1
  m=re.search(r'\b([A-Za-z_]+(?:Error|Exception)):',line)
  if m:exception[m.group(1)]+=1
  for code in re.findall(r'\b[A-Z][A-Z0-9_]{7,}\b',line):
   if '_' in code:codes[code]+=1
 result['log_coverage'].append({'file':p.name,'earliest':min(ts) if ts else None,'latest':max(ts) if ts else None,'levels':levels,'exception_types':exception,'codes':codes.most_common(15)})
(OUT/'telemetry.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
for k in ['execution_business_window','native_outcomes','budget_files','physical_models','log_coverage']:
 print(k,json.dumps(result[k],ensure_ascii=False))
print('latency',json.dumps([r for r in result['model_latency'] if r['role'] in ('COORDINATOR','A','B','C','TESTER')],ensure_ascii=False))
