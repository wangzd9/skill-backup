"""Actionable handoffs derived from facts, not a second business state machine."""
import collections


MECHANICAL=frozenset({'EXECUTION_BOUND','EXECUTION_ENDED','EXECUTION_OUTCOME_OBSERVED',
    'TOOL_EXECUTION_REGISTERED','WORK_CLAIMED'})


def approval_state(snapshot, candidate):
    """Recorded decisions are history, not a claim that today's gates pass."""
    records=[r for r in snapshot['decisions'] if r['candidate_id']==candidate['candidate_id']]
    approved=sorted((r for r in records if r['decision']=='APPROVE'),
                    key=lambda r:(r['created_at'],r['decision_id']))
    return {'approved':bool(approved),'approval_count':len(approved),
            'approval_id':approved[0]['decision_id'] if approved else None,
            'approved_at':approved[0]['created_at'] if approved else None,
            'completed':any(r['decision']=='COMPLETE' for r in records),
            'acceptance_revalidated':False}


def merge_permission(snapshot, candidate, now):
    """Read-only permit hint. The merge command still enforces every gate."""
    grants={r['authorization_id']:r for r in snapshot['authorizations']}
    def live(grant):
        from workflow_task_merge_policy import live as chain_live
        return grant is not None and chain_live(grants,grant['authorization_id'],now)
    for grant in grants.values():
        spec=grant['spec']
        policy_parent=grants.get(spec.get('parent_policy_id'))
        if policy_parent and policy_parent['kind']=='merge_task_policy':
            from workflow_task_merge_policy import policies
            if policy_parent['authorization_id'] not in {p['authorization_id'] for p in policies(snapshot,candidate,now)}:
                continue
        if (grant['kind']=='merge' and live(grant)
                and all(spec.get(k)==candidate[k] for k in ('run_id','task_id','attempt','base','ac_version'))
                and (not spec.get('parent_policy_id') or live(grants.get(spec['parent_policy_id'])))):
            return {'available':True,'authorization_id':grant['authorization_id'],
                    'branch':spec['branch']}
    from workflow_development_policy import policies as development_policies
    development=development_policies(snapshot,candidate,now)
    if development:
        grant=development[0]
        return {'available':True,'parent_policy_id':grant['authorization_id'],'branch':grant['spec']['branch'],
                'reason':'已接受本地自动开发；基线刷新、attempt变化、内部安全收口及本地合入无需用户逐项授权，门禁仍须真实通过。'}
    from workflow_task_merge_policy import policies
    task_policies=policies(snapshot,candidate,now)
    if task_policies:
        parent=task_policies[0]
        return {'available':True,'parent_policy_id':parent['authorization_id'],
                'branch':parent['spec']['branch'],'expires_at':parent['spec']['expires_at'],
                'reason':'任务及分支授权在原期限和冻结合同内持续；新attempt重新验收，合入自动派生精确凭据，无需重授。'}
    accepted=[r for r in snapshot['events'] if r['kind']=='REQUIREMENT_ACCEPTED'
              and r['run_id']==candidate['run_id']]
    accepted=accepted[-1]['payload'] if accepted else {}
    parent=grants.get(candidate['run_id']+'-merge-policy')
    policy=accepted.get('merge_policy') or {}
    if (live(parent) and policy.get('expires_at',0)>now
            and candidate['task_id'] in accepted.get('task_ids',[])):
        return {'available':True,'parent_policy_id':parent['authorization_id'],'branch':policy['branch']}
    return {'available':False,'code':'MERGE_AUTHORIZATION_REQUIRED'}


def event_view(events):
    # Keep the exact issued prefix and every ID; no implicit event ACK.
    output=[]; pending=[]
    def flush():
        if pending:
            output.append({'kind':'MECHANICAL_AUDIT_SUMMARY','event_id':pending[-1]['event_id'],
                'event_ids':[e['event_id'] for e in pending],
                'counts':dict(collections.Counter(e['kind'] for e in pending)),
                'full_events':'business.sqlite3/events; IDs preserved, not ACKed'})
            pending.clear()
    for event in events:
        if event['kind'] in MECHANICAL:
            pending.append(event)
        else:
            flush();output.append(event)
    flush()
    return output


def next_actions(packet, ledger, *, snapshot=None):
    role=packet['role']; result=[]
    work = packet.get('work') or {}
    selected = packet.get('selected_run_id') or ledger.mode()['current_run_id']
    if work and work.get('kind') == 'coordination' and work['run_id'] != selected:
        return [{'action': 'safe_release', 'work_id': work['work_id'],
            'selected_run_id': selected,
            'reason': '当前 work 属于已切换的旧 run；保存必要检查点后用本角色 safe_release 安全收口并结束回合。'
                      '等待原生 writer 核验清零后，新 tick 领取新 run 工作；不得继续发布旧 run 计划或只读空转等待。',
            'payload': {'reason': 'RUN_SWITCHED：安全释放旧 run 工作，保留历史与未决证据'}}]
    from workflow_completion import next_actions as completion_actions
    if role=='COORDINATOR':
        from workflow_verification_exceptions import facts as exception_facts, closure_passes
        snapshot=ledger.snapshot((packet.get('run') or {}).get('run_id')) if snapshot is None else snapshot
        exceptions=exception_facts(snapshot,work.get('run_id') or selected)
        for item in exceptions:
            if not item['blocking']:continue
            action='resolve_verification_exception'
            if item['state']=='WAITING_SAFE_EXIT':action='wait_for_verification_exit'
            pass_options=closure_passes(snapshot,item)
            if item['state']=='REPAIR_QUEUED':
                if not pass_options:
                    action='follow_verification_repair'
            result.append({'action':action,'observation_event_id':item['observation_event_id'],
                'candidate_id':item['candidate_id'],'state':item['state'],
                'later_pass_options':pass_options,
                'payload':{'observation_event_id':item['observation_event_id']},
                'reason':'核UNVERIFIED原始证据后显式选择repair/investigate/close。repair仅原范围已完成任务，落实真实新attempt；'
                    '已有处置则先核落账派单/安全核退/真实复验，不等待MN-07失败、不扩范围、不重复同因提交。'})
    final_actions=completion_actions(packet,ledger,snapshot=snapshot)
    if final_actions:
        return result+final_actions
    if role=='COORDINATOR':
        snapshot=ledger.snapshot((packet.get('run') or {}).get('run_id')) if snapshot is None else snapshot
        from workflow_user_decisions import pending
        for decision in pending(snapshot, selected):
            result.append({'action':'resolve_user_decision','request_id':decision['payload']['request_id'],
                'decision_event':decision,
                'reason':'用户裁决原文已持久登记。核对原提问、选项语义及当前实现，关联准确对象后记录处理依据并更新checkpoint；'
                         '已有明确答复不得要求用户重复发送，不将登记等同于测试通过或合入授权。'})
        import subprocess
        from workflow_wait import repo_observation
        if packet.get('run') and repo_observation(packet['run']['repo']).get('dirty'):
            result.append({'action':'review_documentation',
                'reason':'先用role_repo_state读取documentation_review完整正文/diff/hash；核对事实与范围后用commit_documentation单独提交合理文档。'
                    '不能仅因文档dirty或归属未知写waiting_external；代码/指令/契约变更另走对应流程，文件持续变化则保留并说明证据。'})
        destination_head=None
        if snapshot['merges'] and packet.get('run'):
            from workflow_integration_workspace import completed_head
            destination_head=completed_head(ledger,snapshot,packet['run']['run_id'])
        if destination_head:
            for task in packet['tasks']:
                if (task['phase']=='DEVELOPING' and task['base']!=destination_head
                        and ledger.dependency_readiness(task['run_id'],task['task_id'],'integration')['ready']
                        and not any(c['task_id']==task['task_id'] and c['state']=='CURRENT' for c in packet['candidates'])):
                    result.append({'action':'refresh_development_base','payload':{'task_id':task['task_id'],
                        'reason':'依赖已集成，未交付任务直接刷新基线，不生成空占位候选'},
                        'reason':'等待开发者checkpoint安全交出后受控刷新；保留旧私有工作区用于迁移成果，无需用户授权。'})
        if not (packet.get('run') or {}).get('plan_version'):
            result.append({'action':'publish_plan','reason':'已接受需求需要拆分计划；按冻结合同分配范围和门禁。需要提前并行时显式提供单调stage_dependencies、提供方provided_contracts及匹配的dependency_contracts、最小阅读集、风险场景、验证环境和升级条件；接口无法匹配则保持串行。开发提前开始不开放集成与最终验收门禁，风险场景和原AC均须在固定最终候选上验证。'})
        for candidate in packet['candidates']:
            if candidate['state']!='CURRENT':
                continue
            task=next(t for t in packet['tasks'] if t['task_id']==candidate['task_id'])
            if task['phase'] in ('CANCELLED','COMPLETED'):
                continue
            cid=candidate['candidate_id']
            completed={t['task_id'] for t in packet['tasks'] if t['phase']=='COMPLETED'}
            integrated=[m for m in snapshot['merges'] if any(c['candidate_id']==m['candidate_id']
                        and c['task_id'] in completed and c['task_id']!=candidate['task_id']
                        and c['head']==destination_head for c in packet['candidates'])]
            if (integrated and destination_head!=candidate['base']
                    and not any(m['candidate_id']==cid for m in snapshot['merges'])):
                result.append({'action':'refresh_task_base','candidate_id':cid,
                    'reason':'同需求已有其他任务合入并完成；核实际目标基线后建新 attempt，迁移本任务改动并重新验证，不能循环重试旧 merge'})
                continue
            actions=[a for a in packet['external_actions'] if a['candidate_id']==cid]
            for action in actions:
                if action['state']=='RESULT_READY':
                    closure_preview=[item for item in ledger.acceptance(cid).get('failure_closure_preview',[])
                                     if item['check_name']==action['kind']]
                    result.append({'action':'service_decide','action_id':action['action_id'],
                        'failure_closure_preview':closure_preview,
                        'reason':'读审查结果并按冻结AC裁决；完整覆盖不自动等于PASS。逐条核对列出的历史FAIL与本候选修复证据，只有确认已修复且证据覆盖时才在裁决中提交对应supersedes。'})
                elif action['state']=='INCOMPLETE':
                    from workflow_service_recovery import resume_readiness
                    readiness=resume_readiness(ledger,action['action_id'])
                    result.append({'action':'service_resume' if readiness['allowed'] else 'read_file',
                        'action_id':action['action_id'],'recovery':readiness,'evidence':action.get('evidence'),
                        'reason':'用read_file读取evidence.path中的终态及其sources原文，核对真实代码和发现；service_collect重复收集不能代替读证据。'
                                 '仅有原因文字不得原样重跑。已有发现须语义核实，成立则按原授权decide REWORK，'
                                 '不等待用户接受部分覆盖；新快照重新独立验证与完整审查，不能据未完成审查判代码通过。'})
            verifications=[v for v in packet['verifications'] if v['candidate_id']==cid]
            for kind in ('ocr','strong_review'):
                if (kind in task['required_checks'] and not any(v['check_name']==kind for v in verifications)
                    and not any(a['kind']==kind and a['state'] not in ('REJECTED','ENDED') for a in actions)):
                    try:
                        from workflow_autonomy import select_grant
                        auth=select_grant(ledger,candidate,kind)
                        result.append({'action':'service_submit','candidate_id':cid,'kind':kind,
                                       'authorization':auth,'reason':'使用已接受范围的剩余预算，无需逐候选再问用户'})
                    except ValueError:
                        result.append({'action':'authorization_required','candidate_id':cid,'kind':kind,
                                       'reason':'合并提出缺失的范围授权；不能循环等待或自行扩大预算'})
            gate=ledger.acceptance(cid)
            if gate.get('ready'):
                approved=approval_state(snapshot,candidate)
                if not approved['approved']:
                    result.append({'action':'decide','candidate_id':cid,'decision':'APPROVE',
                        'reason':'当前验收门已齐且尚无批准记录；首次批准仍由事务门重新核验'})
                    continue
                if any(row['candidate_id']==cid for row in snapshot['merges']):
                    result.append({'action':'complete','candidate_id':cid,
                        'reason':'提交真实交付摘要；服务落盘收据并复核合入、验证与批准，不需要 write_file'})
                    continue
                permit=merge_permission(snapshot,candidate,ledger.clock())
                if permit['available']:
                    result.append({'action':'merge','candidate_id':cid,
                        'reason':'已批准且有有效合入边界；调用merge让入口判定。共享主仓dirty时支持同仓受管隔离worktree，保留全部用户改动；不要仅因共享dirty继续等待。事务门仍复核固定候选、授权及验收门',
                        'permission':permit})
                else:
                    result.append({'action':'authorization_required','candidate_id':cid,'kind':'merge',
                        'approval_id':approved['approval_id'],'reason':'只在原文明确保留目标决定且仍未给出目标时汇总一次；已有本地自动化策略不得要求逐任务/attempt重授。缺配置先核本次automation事实。'})
            elif any(v['result']=='FAIL' for v in verifications):
                result.append({'action':'decide','candidate_id':cid,'decision':'REWORK',
                               'reason':'仅依据真实业务失败返修；环境或收集失败先修验证器'})
            elif 'ACCEPTANCE_DEPENDENCIES_NOT_COMPLETE' in gate.get('reasons',[]):
                result.append({'action':'wait_for_dependency','candidate_id':cid,
                    'dependency_gate':gate.get('dependency_gate'),
                    'reason':'当前候选的本地检查已完成，但最终验收仍等待计划明确列出的上游任务接受；保留当前证据，不自动批准或取消门禁。'})
    elif role=='TESTER':
        with ledger._connection() as db:
            from workflow_verification_lifecycle import pending
            retirement=pending(db,work.get('work_id'))
        if retirement:
            return [{'action':'safe_release','payload':{'reason':'原生退休请求：'+retirement['payload']['reason']},
                'reason':'旧候选验证已不适用；本角色自行safe_release并结束执行，真实writer退出仍由运行时核验。无需用户a/b裁决，不等待刷新先发生。'}]
        if work.get('kind')=='supplemental_verification':
            return [{'action':'verify_supplemental','payload':{},
                'reason':'已入账的固定候选MySQL补验，使用受控临时8.4实例与Unix socket；不需要shell、生产URL或再次授权；skip不能报MySQL通过。'}]
        candidate_id=work.get('candidate_id')
        if candidate_id:
            snapshot=ledger.snapshot(work.get('run_id')) if snapshot is None else snapshot
            previous_passes=[row for row in snapshot['verifications']
                if row['candidate_id']==candidate_id and row['check_name']=='independent_test'
                and row['result']=='PASS']
            preview=[row for row in ledger.acceptance(candidate_id).get('failure_closure_preview',[])
                if row['check_name']=='independent_test' and row['closure_eligible']]
            if previous_passes and preview:
                latest=max(previous_passes,key=lambda row:(row['created_at'],row['verification_id']))
                return [{'action':'close_verification','payload':{
                            'verification_id':latest['verification_id'],
                            'supersedes':[row['verification_id'] for row in preview]},
                    'failure_closure_preview':preview,
                    'reason':'当前固定候选已有独立测试PASS，且仅缺少历史FAIL关闭关联；先核对原失败与现有测试证据覆盖，再用close_verification复用同一冻结PASS，不重复跑测试。'}]
        result.append({'action':'verify','reason':'用固定候选和显式 pytest 环境；pytest 命令自动选需求仓库 .venv；'
            '超时用 timeout_seconds，不依赖未安装的 --timeout 插件。环境错误修正 argv 后重试，不发布FAIL'})
    else:
        pending_refresh=(snapshot['events'] if snapshot is not None else
                         ledger.events(run_id=work.get('run_id') or selected,limit=10000))
        if any(e['kind']=='DEVELOPMENT_BASE_REFRESH_REQUESTED' and e['work_id']==work.get('work_id') for e in pending_refresh):
            return [{'action':'checkpoint','payload':{'checkpoint':{'next':'保存真实成果commit/head和私有路径，协调者refresh_development_base后迁移交付；禁止空占位候选。'},'yield_execution':True},
                     'reason':'当前开发基线需安全刷新；先保存真实成果再提交此checkpoint并退出，不等待用户授权、不改在途基线。'}]
        result.append({'action':'write_file/patch','reason':'在本单私有仓库的冻结文件范围内编辑；'
                       '工具经 writer 校验和受限沙箱执行，正文走 stdin，无需拆小 heredoc'})
        result.append({'action':'terminal','reason':'自测、git commit 后 deliver；不要重试被 hardline 拦截的大段内联命令'})
        result.append({'action':'checkpoint','reason':'外部业务决策未到时 waiting_external 必须是 list[str]；内部机制故障不要写成永久业务等待'})
    if role=='COORDINATOR' and result and all(r['action']=='authorization_required' for r in result):
        result.append({'action':'checkpoint','reason':'仅余外部授权；一次汇总后结构化等待，避免无新输入反复启动',
            'payload':{'checkpoint':{'waiting_external':[
                r['kind']+':'+r['candidate_id'] for r in result]},'yield_execution':True}})
    return result
