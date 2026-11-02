"""Read-only wait revalidation. Rechecking never grants business permission."""
import os
import subprocess


def _condition_value(db, work, kind, identifier):
    from business_ledger import _row
    if kind=='task':
        row=_row(db.execute('SELECT * FROM tasks WHERE run_id=? AND task_id=?',(work['run_id'],identifier)).fetchone())
    elif kind in ('candidate','action','run'):
        table,key={'candidate':('candidates','candidate_id'),'action':('external_actions','action_id'),
                   'run':('runs','run_id')}[kind]
        row=_row(db.execute('SELECT * FROM '+table+' WHERE '+key+'=?',(identifier,)).fetchone())
        if row and kind=='action':
            owner=db.execute('SELECT run_id FROM candidates WHERE candidate_id=?',(row['candidate_id'],)).fetchone()
            if not owner or owner['run_id']!=work['run_id']:row=None
        elif row and row['run_id']!=work['run_id']:row=None
    elif kind=='authorizations':
        import json
        return [_row(r) for r in db.execute('SELECT * FROM authorizations ORDER BY authorization_id')
                if json.loads(r['spec']).get('run_id') in (None,work['run_id'])]
    else:raise ValueError('INVALID_WAIT_CONDITION_KIND')
    if row is None:raise ValueError('WAIT_CONDITION_OBJECT_NOT_IN_RUN')
    return row


def freeze_conditions(db, work, supplied, at):
    """Freeze real object identities, never parse a semantic wait from prose."""
    from business_ledger import _row,_hash
    if supplied is None:
        objects=[]
        tasks=[_row(r) for r in db.execute('SELECT * FROM tasks WHERE run_id=?',(work['run_id'],))]
        own=next((t for t in tasks if t['task_id']==work['task_id']),None)
        selected=tasks if work['role']=='COORDINATOR' else [t for t in tasks if own and
            t['task_id'] in [own['task_id'],*own['dependencies']]]
        objects.extend({'kind':'task','id':t['task_id']} for t in selected)
        candidates=[_row(r) for r in db.execute('SELECT * FROM candidates WHERE run_id=?',(work['run_id'],))]
        candidates=[c for c in candidates if c['state']=='CURRENT' and
            (work['role']=='COORDINATOR' or c['task_id'] in {t['task_id'] for t in selected})]
        objects.extend({'kind':'candidate','id':c['candidate_id']} for c in candidates)
        ids={c['candidate_id'] for c in candidates}
        objects.extend({'kind':'action','id':r['action_id']} for r in db.execute('SELECT * FROM external_actions')
                       if r['candidate_id'] in ids and r['state'] not in ('ENDED','REJECTED'))
        if len(objects)>126:return None  # Fall back to broad legacy event visibility, never omit a dependency.
        supplied={'version':1,'objects':objects,'recheck_at':at+900,'origin':'runtime-object-observation'}
    if (not isinstance(supplied,dict) or supplied.get('version')!=1 or
            not isinstance(supplied.get('objects'),list) or len(supplied['objects'])>128):
        raise ValueError('INVALID_WAIT_CONDITIONS')
    objects=[];seen=set()
    for item in [*supplied['objects'],{'kind':'run','id':work['run_id']},{'kind':'authorizations','id':work['run_id']}]:
        if not isinstance(item,dict) or not isinstance(item.get('id'),str):raise ValueError('INVALID_WAIT_CONDITION_OBJECT')
        key=(item.get('kind'),item['id'])
        if key in seen:continue
        seen.add(key)
        value=_condition_value(db,work,*key)
        objects.append({'kind':key[0],'id':key[1],'fingerprint':_hash(value)})
    deadline=supplied.get('recheck_at',at+900)
    import math
    if type(deadline) not in (int,float) or not math.isfinite(deadline):raise ValueError('INVALID_WAIT_RECHECK_AT')
    return {'version':1,'objects':objects,'recheck_at':min(max(at,deadline),at+900),
        'frozen_at':at,'event_after':db.execute('SELECT COALESCE(MAX(event_id),0) FROM events').fetchone()[0],
        'origin':supplied.get('origin','explicit-object-observation'),
        'policy':'对象变化只触发重核；不表示所需前置已满足，不确认输入或授权。'}


def condition_review(db, work, events, at):
    from business_ledger import _hash
    conditions=work['checkpoint']['wait_conditions'];changes=[]
    for item in conditions['objects']:
        try:changed=_hash(_condition_value(db,work,item['kind'],item['id']))!=item['fingerprint']
        except ValueError:changed=True
        if changed:changes.append({'kind':item['kind'],'id':item['id']})
    urgent=[e['event_id'] for e in events if e['event_id']>conditions['event_after'] and
        (e.get('run_id') is None or e['kind'].startswith(('CONTROL_','USER_DECISION_',
            'VERIFICATION_EXCEPTION_','VERIFICATION_RETRY_','REQUIREMENT_PREPARATION_')))]
    targets={item['id'] for item in conditions['objects'] if item['kind'] in ('task','candidate','action')}
    mechanical={'EXECUTION_BOUND','EXECUTION_ENDED','EXECUTION_OUTCOME_OBSERVED','TOOL_EXECUTION_REGISTERED',
                'CHECKPOINT_SAVED','WORK_CLAIMED','COORDINATION_ATTENTION_REQUIRED'}
    urgent.extend(e['event_id'] for e in events if e['event_id']>conditions['event_after'] and
        e['kind'] not in mechanical and any(isinstance(e.get('payload',{}).get(k),str) and e['payload'][k] in targets
            for k in ('task_id','candidate_id','action_id')))
    return {'changed':bool(changes or urgent),'objects_changed':changes,'attention_event_ids':urgent,
        'due':at>=conditions['recheck_at'],'recheck_at':conditions['recheck_at'],'gates_unchanged':True}


def repo_observation(repo):
    try:
        def git(*args):
            return subprocess.check_output(['git', '--no-optional-locks', '-c', 'core.fsmonitor=false',
                '-c', 'core.untrackedCache=false', '-C', str(repo), *args],
                env={k:v for k,v in os.environ.items() if not k.startswith('GIT_')},
                stderr=subprocess.DEVNULL, text=True, timeout=3).strip()
        head = git('rev-parse', 'HEAD')
        branch = git('symbolic-ref', '--short', 'HEAD')
        dirty = bool(git('status', '--porcelain', '--untracked-files=normal'))
        if git('rev-parse', 'HEAD') != head:
            return {'state': 'UNKNOWN'}
        return {'state': 'OBSERVED', 'head': head, 'branch': branch, 'dirty': dirty}
    except (OSError, subprocess.SubprocessError):
        return {'state': 'UNKNOWN'}


def review(ledger, db, work, run):
    from business_ledger import _row
    event = _row(db.execute("SELECT * FROM events WHERE work_id=? AND kind='CHECKPOINT_SAVED' "
        'ORDER BY event_id DESC LIMIT 1', (work['work_id'],)).fetchone())
    # A checkpoint timestamp bounds even old string-only wait contracts. No
    # synthetic events or ACKs are needed to escape an incorrectly saved wait.
    saved_at = event['created_at'] if event else work['created_at']
    deadline = saved_at + 900
    deadline=min(deadline,work['checkpoint'].get('wait_conditions',{}).get('recheck_at',deadline))
    result = {'due': ledger.clock() >= deadline, 'recheck_at': deadline,
              'reason': 'WAIT_RECHECK_DUE', 'gates_unchanged': True}
    previous = (event or {}).get('payload', {}).get('wait_observation')
    if work['role'] == 'COORDINATOR' and previous and not result['due']:
        current = repo_observation(run['repo'])
        # Content changes inside an already dirty tree are not progress. Observe
        # branch/base changes and dirty -> clean without polling file mtimes.
        if (previous.get('state') == current.get('state') == 'OBSERVED'
                and (previous['head'] != current['head'] or previous['branch'] != current['branch']
                     or (previous['dirty'] and not current['dirty']))):
            result.update(due=True, reason='WAIT_REPOSITORY_CHANGED')
        result['repository'] = current
    return result


def capabilities(role):
    developer = role in {'A', 'B', 'C'}
    return {'ordinary_workspace_write': developer, 'ordinary_terminal': developer,
            'coordinator_shell_available': False,
            'supplemental_mysql': '原生归属指令持久排队给TESTER；verify_supplemental提供隔离MySQL8.4与固定测试，无需生产URL、shell或重授；当前状态看supplemental_verifications。',
            'merge_execution': '有效授权spec.repo同仓；共享dirty时受管integration-worktrees隔离合入，回执记录实际位置，operations仅存日志',
            'coordination_actions': ['publish_plan','decide','checkpoint','safe_release','service_submit',
                'service_collect','service_decide','service_resume','service_reject','extend_task_scope',
                'refresh_task_base','refresh_development_base','merge','complete','complete_requirement','finalize_delivery','append_development_log','commit_documentation','prepare_requirement','activate_requirement','resolve_user_decision','resolve_verification_exception'] if role == 'COORDINATOR' else [],
            'final_delivery': '群用request_final_delivery持久登记精确分支/remote及本地栈授权；后台finalize_delivery固定执行main合入、push、mock up/test与quality_gate，分步留证据，不开放shell；失败停止不隐式重试。',
            'final_acceptance': '原生completion计划自动冻结集成版本并排队TESTER；verify_integration逐AC取证，deliver_integration交付，代码FAIL回原owner新attempt；最终PASS后协调者complete_requirement复核本地收口；无需用户另发派单裁决，规划者仍手动。',
            'documentation_policy': '协调者先用role_repo_state的documentation_review读真实diff与SHA，核对事实和范围；'
                '可确认的普通文档用commit_documentation单独提交，不能因归属未知一律等待用户；代码、指令与AC不走此入口。',
            'local_automation': '已接受需求内派单/开发/测试/返修/基线刷新/安全收口/本地合入自动接续，无需a/b或逐任务授权；OCR按原预算期限，生产/push边界单独核验。',
            'startup_authority': 'business_ledger.admission_hint (ACTIVE); pool.current is legacy metadata',
            'writer_fence_meaning': '该角色已有写执行，禁止并发第二个；不等于死锁',
            'wait_policy': '仅余真实外部条件才保存 waiting_external；15分钟或仓库状态变化后重新核验，门禁不解除'}
