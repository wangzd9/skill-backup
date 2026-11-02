"""只读审计；不保存授权、身份令牌或消息正文。"""
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import sqlite3,json,time,collections
OUT=Path(__file__).resolve().parent
NOW=time.time();START=datetime(2026,10,31,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
c=sqlite3.connect('file:/home/wang/.hermes/role-pool.runtime/business.sqlite3?mode=ro',uri=True,timeout=2);c.row_factory=sqlite3.Row
c.execute('PRAGMA query_only=ON')
def rows(q,args=()):return [dict(r) for r in c.execute(q,args)]
def event_fields(kind,fields):
 return rows('select event_id,run_id,work_id,role,created_at,'+','.join("json_extract(payload,'$."+f+"') AS "+f.replace('.','_') for f in fields)+' from events where kind=? and created_at>=? and created_at<=? order by event_id',(kind,START,NOW))
result={'window':{'start':START,'end':NOW,'timezone':'Asia/Shanghai','note':'三自然日截至采集时点；读取不断运行的系统，各查询不是同一长事务。'},'high_watermark':c.execute('select max(event_id) from events').fetchone()[0]}
result['events_by_kind']=rows('select kind,count(*) n from events where created_at>=? and created_at<=? group by kind order by n desc',(START,NOW))
result['executions_by_role_state']=rows('select role,state,count(*) n,round(avg(coalesce(ended_at,?)-bound_at),2) avg_seconds,max(coalesce(ended_at,?)-bound_at) max_seconds from executions where bound_at>=? and bound_at<=? group by role,state',(NOW,NOW,START,NOW))
result['outcomes']=event_fields('EXECUTION_OUTCOME_OBSERVED',['execution_id','reason','classification','first_effective_action','next_action','status','summary'])
result['business_transitions']=rows("select event_id,kind,run_id,role,work_id,created_at,json_extract(payload,'$.task_id') task_id,json_extract(payload,'$.decision') decision,json_extract(payload,'$.result') result,json_extract(payload,'$.action_id') action_id from events where created_at>=? and created_at<=? and kind in ('CANDIDATE_DELIVERED','CANDIDATE_DECIDED','CANDIDATE_MERGED','VERIFICATION_PUBLISHED','SERVICE_ARTIFACT_RECORDED','SERVICE_ADJUDICATED','REQUIREMENT_COMPLETED','DEPENDENCY_RUN_SCHEDULED','USER_DECISION_RECORDED','USER_DECISION_RESOLVED') order by event_id",(START,NOW))
result['recent_command_outcomes']=rows("select verb,json_extract(result,'$.status') status,json_extract(result,'$.code') code,count(*) n from commands where created_at>=? and created_at<=? group by verb,status,code order by n desc",(START,NOW))
result['outbox_all']=rows('select kind,state,count(*) n,min(created_at) oldest from outbox group by kind,state order by n desc')
result['outbox_recent']=rows('select kind,state,count(*) n,min(created_at) oldest from outbox where created_at>=? and created_at<=? group by kind,state order by n desc',(START,NOW))
result['undelivered']=rows("select notification_id,event_id,kind,role,run_id,state,created_at,json_extract(receipt,'$.status') receipt_status,json_extract(receipt,'$.reason') receipt_reason from outbox where state not in ('SENT','SUPPRESSED') order by created_at")
result['current_works']=rows("select work_id,run_id,task_id,attempt,role,kind,state,processed_through,issued_through,created_at,ended_at,json_extract(checkpoint,'$.waiting_external') waiting_external,json_extract(checkpoint,'$.next') next,json_extract(checkpoint,'$.work_memory.version') memory_version from works where state!='ENDED'")
result['current_tasks']=rows("select run_id,task_id,role,phase,attempt,dependencies,unresolved_reason from tasks where run_id in ('prepared-rosbag-catalog-scene-foundation','prepared-rosbag-catalog-spray-pipeline')")
result['all_run_controls']=rows('select run_id,control,plan_version,created_at from runs')
result['external_actions']=rows("select a.action_id,a.kind,a.state,a.created_at,a.candidate_id,c.run_id,c.task_id,c.head,json_extract(a.evidence,'$.receipt') receipt,json_extract(a.evidence,'$.service') service from external_actions a join candidates c using(candidate_id) where a.created_at>=? or a.state in ('SUBMITTED','UNKNOWN','RESULT_READY') order by a.created_at",(START,))
result['work_memory_adoption']=rows("select role,count(*) n,sum(json_extract(checkpoint,'$.work_memory.version') is not null) with_memory from works where created_at>=? group by role",(START,))
result['checkpoint_activity']=rows("select role,work_id,count(*) n,min(created_at) first,max(created_at) last from events where kind='CHECKPOINT_SAVED' and created_at>=? group by role,work_id order by n desc limit 12",(START,))
result['command_storage']=rows("select verb,count(*) n,sum(length(result)) result_bytes,max(length(result)) max_result_bytes from commands where created_at>=? and created_at<=? group by verb order by result_bytes desc limit 12",(START,NOW))
(OUT/'ledger.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
for k in ['high_watermark','executions_by_role_state','outbox_recent','current_tasks','work_memory_adoption','command_storage']:
 print(k,json.dumps(result[k],ensure_ascii=False))
print('outcome_sample',json.dumps(result['outcomes'][-2:],ensure_ascii=False))
print('undelivered',len(result['undelivered']))
print('rejections',json.dumps([r for r in result['recent_command_outcomes'] if r['status'] not in ('COMMITTED','DUPLICATE')],ensure_ascii=False))
