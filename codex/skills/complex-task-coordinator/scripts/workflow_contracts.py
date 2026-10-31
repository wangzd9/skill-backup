"""The same action schemas are advertised to models and checked before effects."""
from __future__ import annotations

import json


def obj(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required),
            'additionalProperties': False}


TEXT = {'type': 'string', 'minLength': 1}
STRINGS = {'type': 'array', 'items': TEXT}
EVIDENCE = obj({'path': {**TEXT,'description':'持久证据文件的绝对路径；不相对candidate.repo或工具cwd猜测。自测输出使用输入包delivery_evidence.directory，避免弄脏候选。'}, 'sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}}, ['path'])
TASK = obj({'task_id': TEXT, 'role': {'enum': ['A', 'B', 'C']},
            'dependencies':{**STRINGS,'description':'完整依赖任务集合；使用stage_dependencies时必须恰好等于三个阶段依赖的并集。'},
            'stage_dependencies': {**obj({'development_start':STRINGS,'integration':STRINGS,'acceptance':STRINGS},
                                      ['development_start','integration','acceptance']),
                'description':'仅需提前开发时使用；development_start ⊆ integration ⊆ acceptance。出现本字段时ac/acceptance/inputs/stop_conditions及四项风险环境字段均必填；晚于开发阶段才满足的每个依赖须有精确匹配提供方的dependency_contracts。'},
            'dependency_contracts': {'type':'array','items':obj({'task_id':TEXT,'version':TEXT,
                'contract_id':TEXT,
                'interface':{'type':'object','minProperties':1},
                'simulation':{'type':'object','minProperties':1}},['task_id','contract_id','version','interface','simulation'])},
            'provided_contracts': {'type':'array','items':obj({'contract_id':TEXT,'version':TEXT,
                'interface':{'type':'object','minProperties':1}},['contract_id','version','interface'])},
            'minimum_reading_set': {'type':'array','minItems':1,'items':TEXT},
            'risk_scenarios': {'type':'array','minItems':1,'items':obj({'id':TEXT,'scenario':TEXT,'invariant':TEXT,'verification':TEXT},
                ['id','scenario','invariant','verification'])},
            'test_environment': {'type':'object','minProperties':1},
            'escalation_conditions': {'type':'array','minItems':1,'items':TEXT},
            'scope': {'type': 'array', 'minItems': 1,
                'description':'唯一可写文件范围：填写仓库相对路径或glob（如app/services/source_service.py、tests/test_source_*.py），不要填写任务叙述、API行为或验收说明；实现目标填ac/inputs，范围过窄会阻止开发写入。',
                'items': TEXT},
            'base': {'type': 'string', 'pattern': '^[0-9a-f]{40}$'}, 'ac_version': TEXT,
            'required_checks': {'type': 'array', 'items': {'enum': ['independent_test', 'ocr', 'strong_review']}},
            'priority': {'type': 'integer'}, 'ac': {},
            'acceptance': {'description':'任务验收标准及逐项取证要求；不是PASS结论。使用stage_dependencies时必填且不得为null、空数组或空对象。'}, 'inputs': {},
            'stop_conditions': {}, 'worktree': TEXT}, ['task_id', 'role', 'scope', 'base', 'ac_version']) | {
    'allOf':[{'if':{'required':['stage_dependencies']},'then':{
    'required':['ac','acceptance','inputs','stop_conditions','minimum_reading_set',
                'risk_scenarios','test_environment','escalation_conditions'],
    'properties':{key:{'not':{'enum':[None,[],{}]}} for key in ('ac','acceptance','inputs','stop_conditions')}}}]}
AUTHORIZATION = obj({'authorization_id': TEXT}, ['authorization_id'])

PAYLOADS = {
    'checkpoint': obj({'checkpoint': {'type': 'object'}, 'processed_through': {'type': 'integer', 'minimum': 0},
                       'yield_execution': {'type': 'boolean'}}, ['checkpoint']),
    'safe_release': obj({'reason': TEXT}, ['reason']),
    'publish_plan': obj({'version': {'type': 'integer', 'minimum': 1},
                         'tasks': {'type': 'array', 'minItems': 1, 'items': TASK}}, ['version', 'tasks']),
    'deliver': obj({'candidate': obj({'repo': TEXT, 'base': TEXT, 'head': TEXT, 'ac_version': TEXT,
                                     'evidence': {'type': 'array', 'minItems': 1, 'items': EVIDENCE},
                                     'candidate_id': TEXT}, ['repo', 'base', 'head', 'ac_version', 'evidence']),
                    'summary': TEXT}, ['candidate', 'summary']),
    'decide': obj({'candidate_id': TEXT, 'decision': {'enum': ['REWORK', 'APPROVE', 'COMPLETE']},
                   'evidence': {'type': 'object'}}, ['candidate_id', 'decision']),
    'close_verification': obj({'verification_id': TEXT, 'supersedes': {'type': 'array', 'minItems': 1, 'items': TEXT}}, ['verification_id', 'supersedes']),
    'verify': obj({'candidate_id': TEXT, 'argv': {'type': 'array', 'minItems': 1, 'items': TEXT},
                   'timeout_seconds': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 86400},
                   'supersedes': STRINGS, 'evidence': {'type': 'object'},
                   'report_format': {'enum': ['pytest', 'junit']}}, ['candidate_id', 'argv']),
    'service_submit': obj({'candidate_id': TEXT, 'kind': {'enum': ['ocr', 'strong_review']},
                           'action_id': TEXT, 'authorization': AUTHORIZATION,
                           'request': {'oneOf': [
                               obj({'background_file': TEXT, 'batches_file': TEXT}, ['background_file']),
                               obj({'background_text': {**TEXT, 'maxLength': 65536}, 'batches_file': TEXT}, ['background_text']),
                               obj({'package': TEXT, 'risk': TEXT, 'question': TEXT, 'ac': TEXT, 'gate': TEXT,
                                    'timeout_seconds': {'type': 'number', 'exclusiveMinimum': 0}},
                                   ['package', 'risk', 'question', 'ac', 'gate'])]}},
                          ['candidate_id', 'kind', 'action_id', 'authorization', 'request']),
    'service_collect': obj({'action_id': TEXT}, ['action_id']),
    'service_decide': obj({'action_id': TEXT, 'result': {'enum': ['PASS', 'FAIL']}, 'summary': TEXT,
                           'supersedes': STRINGS}, ['action_id', 'result', 'summary']),
    'merge': obj({'candidate_id': TEXT, 'authorization_id': TEXT}, ['candidate_id', 'authorization_id']),
}

CONTROL_TARGET = {'oneOf': [
    obj({'requirement_id':TEXT,'requirement_root':TEXT,'repo':TEXT,'task_ids':STRINGS,
         'contract_files':STRINGS,'run_id':TEXT},
        ['requirement_id','requirement_root','repo','task_ids','contract_files']),
    obj({key: TEXT for key in ('run_id','role','task_id','candidate_id')}),
    obj({'authorization_id': TEXT}, ['authorization_id']),
    obj({'authorization_id':TEXT,'run_id':TEXT,'task_ids':{'type':'array','minItems':1,'items':TEXT},
         'service_kind':{'enum':['ocr','strong_review']},'max_calls':{'type':'integer','minimum':1,'maximum':1000},
         'expires_at':{'type':'number'},'execution_seconds':{'type':'integer','minimum':60,'maximum':7200}},
         ['run_id','task_ids','service_kind','max_calls','expires_at']),
    obj({'authorization_id': TEXT, 'candidate_id': TEXT, 'service_kind': {'enum':['ocr','strong_review']},
         'max_calls': {'type':'integer','minimum':1,'maximum':5}, 'expires_at': {'type':'number'}},
        ['candidate_id','service_kind','max_calls','expires_at']),
    obj({'authorization_id': TEXT, 'run_id': TEXT, 'task_id': TEXT, 'attempt': {'type':'integer','minimum':1},
         'repo': TEXT, 'branch': TEXT, 'base': TEXT, 'ac_version': TEXT, 'expires_at': {'type':'number'},
         'postmerge_argv': STRINGS, 'junit_format': {'enum':['pytest','junit']},
         'timeout_seconds': {'type':'number','exclusiveMinimum':0}},
        ['run_id','task_id','attempt','repo','branch','base','ac_version','expires_at'])]}


def payload_schema(action):
    if action=='activate_requirement':
        return obj({'request_id':TEXT,'reason':TEXT},['request_id','reason'])
    if action=='prepare_requirement':
        sha={'type':'string','pattern':'^[0-9a-f]{64}$'}
        return obj({'request_id':TEXT,'summary':{**TEXT,'maxLength':8000},
            'task_ids':{'type':'array','minItems':1,'maxItems':100,'uniqueItems':True,
                'items':{'type':'string','pattern':'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$'}},
            'additional_task_sources':{'type':'object','maxProperties':100,
                'description':'仅额外拆出的提供方任务必须逐项引用冻结proposal；原指令task_ids内的任务无需重复映射，若提供则同样核验。key只能是本次task_ids，不能复活历史任务。',
                'additionalProperties':obj({'path':{**TEXT,'description':'冻结sources中的proposal路径；可用需求根相对路径或该根内的绝对路径，不允许跨根或..。'},
                    'quote':{**TEXT,'minLength':8,'maxLength':4000,'description':'冻结proposal中连续的原文引用；不得使用改写或行号前缀。'}},['path','quote'])},
            'changes':{'type':'array','minItems':1,'maxItems':20,'items':obj({
                'path':TEXT,'sha256':sha,'content':{'type':'string','minLength':1,'maxLength':1048576},
                'append':{'type':'string','minLength':1,'maxLength':65536}},
                ['path','sha256']) | {'oneOf':[{'required':['content'],'not':{'required':['append']}},
                                              {'required':['append'],'not':{'required':['content']}}]}}},['request_id','summary','task_ids','changes'])
    if action=='verify':
        return {**PAYLOADS[action],'properties':{**PAYLOADS[action]['properties'],**test_execution_fields()}}
    if action=='resolve_verification_exception':
        schema=obj({'observation_event_id':{'type':'integer','minimum':1},
            'disposition':{'enum':['repair','investigate','close']},'reason':TEXT,
            'candidate_id':TEXT,'affected_paths':{'type':'array','minItems':1,'maxItems':32,'items':TEXT},
            'next_action':TEXT,'verification_id':TEXT},['observation_event_id','disposition','reason'])
        schema['allOf']=[{'if':{'properties':{'disposition':{'const':value}}},
            'then':{'required':fields}} for value,fields in (
                ('repair',['candidate_id','affected_paths']),('investigate',['next_action']),('close',['verification_id']))]
        return schema
    if action=='environment_recovered':
        return obj({'validation_event_id':{'type':'integer','minimum':1},'summary':TEXT,
            'evidence':{'type':'array','minItems':1,'items':EVIDENCE}},['validation_event_id','summary','evidence'])
    if action=='ack_events':
        return obj({'through':{'type':'integer','minimum':0}},['through'])
    if action in ('verify_integration','deliver_integration','complete_requirement'):
        from workflow_completion_contracts import payload_schema as completion_schema
        return completion_schema(action)
    if action=='finalize_delivery':
        return obj({'request_id':TEXT},['request_id'])
    if action=='resolve_user_decision':
        return obj({'request_id': TEXT, 'decision_id': TEXT,
                    'summary': TEXT, 'evidence': {'type':'object','minProperties':1}},
                   ['request_id','decision_id','summary','evidence'])
    if action=='verify_supplemental':
        return obj({})
    if action=='commit_documentation':
        return obj({'expected_head':{'type':'string','pattern':'^[0-9a-f]{40}$'},
            'expected_branch':TEXT, 'summary':{**TEXT,'maxLength':500},
            'files':{'type':'array','minItems':1,'maxItems':32,'items':obj({
                'path':TEXT,'sha256':{'type':'string','pattern':'^[0-9a-f]{64}$'},
                'review':{**TEXT,'maxLength':4000}},['path','sha256','review'])}},
            ['expected_head','expected_branch','summary','files'])
    if action=='append_development_log':
        return obj({'content':{**TEXT,'maxLength':65536},'source_event_ids':{'type':'array',
            'maxItems':32,'uniqueItems':True,'items':{'type':'integer','minimum':1}}},['content'])
    if action=='refresh_development_base':
        return obj({'task_id':TEXT,'reason':TEXT},['task_id','reason'])
    if action=='refresh_task_base':
        return obj({'candidate_id':TEXT,'reason':TEXT},['candidate_id','reason'])
    if action=='complete':
        return obj({'candidate_id':TEXT,'summary':TEXT},['candidate_id','summary'])
    if action=='merge':
        return obj({'candidate_id':TEXT,'authorization_id':TEXT},['candidate_id'])
    if action=='extend_task_scope':
        return obj({'task_id':TEXT,'additions':{'type':'array','minItems':1,'items':TEXT},'reason':TEXT},
                   ['task_id','additions','reason'])
    if action=='service_reject':
        return obj({'action_id':TEXT,'reason':TEXT},['action_id','reason'])
    if action=='service_resume':
        return obj({'action_id':TEXT,'new_action_id':TEXT,'reason':TEXT,'authorization':AUTHORIZATION},
                   ['action_id','new_action_id','reason'])
    schema=PAYLOADS[action]
    if action=='service_submit':
        # Late construction also covers a host which loaded the old contract
        # before a bounded function migration; no existing prompt is rebuilt.
        schema={**schema,'required':[v for v in schema['required'] if v!='authorization'],
          'properties':{**schema['properties'],'request':{'oneOf':[
            obj({'background_file':TEXT,'batches_file':TEXT},['background_file']),
            obj({'background_text':{**TEXT,'maxLength':65536},'batches_file':TEXT},['background_text']),
            obj({'package':TEXT,'risk':TEXT,'question':TEXT,'ac':TEXT,'gate':TEXT,
                 'timeout_seconds':{'type':'number','exclusiveMinimum':0}},
                ['package','risk','question','ac','gate'])]}}}
    return schema


def test_execution_rules():
    return {'pytest':{'argv':'pytest或python -m pytest；保留测试选择和项目插件。',
        'junit_output':'运行时注入；argv不得指定--junitxml或--junit-xml。',
        'example':{'argv':['pytest','-q','<原合同测试路径>'],'report_format':'pytest'}},
        'junit':{'argv':'argv中必须有字面占位{junit_xml}，由运行时替换为本次输出绝对路径。',
            'example':{'argv':['<既有JUnit程序>','--output','{junit_xml}'],'report_format':'junit'}},
        'replay':'同command_id只恢复既有执行；相同候选/命令/环境的失败不换ID盲目重跑，先核原证据并落实变更。',
        'stability':'pytest保留原两次稳定性运行，UNVERIFIED不能改写为PASS/FAIL。'}


def test_execution_fields():
    return {'argv':{'type':'array','minItems':1,'items':TEXT,
        'description':'pytest模式不传JUnit输出选项（运行时注入）；junit模式必须包含字面{junit_xml}占位。'},
        'report_format':{'enum':['pytest','junit'],'description':'默认pytest；pytest由运行时管理JUnit及稳定性复跑；junit由既有程序输出到{junit_xml}。'}}


def command_schema(actions):
    # Hermes strips top-level combinators for provider compatibility. Keep the
    # complete call envelope and payload alternatives discoverable without that
    # combinator; the original branches and validate_payload still enforce the
    # exact action/payload relationship before any effect.
    return {**obj({'action': {'type': 'string', 'enum': list(actions)},
        'payload': {'type': 'object', 'oneOf': [
            {**payload_schema(action), 'title': action} for action in actions]},
        'command_id': {**TEXT, 'description': '本动作稳定的唯一ID；相同重试保留，不同动作换ID。'}},
        ['action', 'payload', 'command_id']),
        'oneOf': [obj({'action': {'const': action},
                'payload': payload_schema(action), 'command_id': TEXT}, ['action', 'payload', 'command_id'])
            for action in actions]}


def control_target_schema():
    # Also works when the host imported the older module before hot activation.
    run_policy=obj({'authorization_id':TEXT,'run_id':TEXT,'task_ids':STRINGS,
        'service_kind':{'enum':['ocr','strong_review']},'max_calls':{'type':'integer','minimum':1,'maximum':1000},
        'expires_at':{'type':'number'},'execution_seconds':{'type':'integer','minimum':60,'maximum':7200}},
        ['run_id','task_ids','service_kind','max_calls','expires_at'])
    intake=obj({'requirement_id':TEXT,'requirement_root':TEXT,'repo':TEXT,'task_ids':STRINGS,'merge_branch':TEXT,
        'contract_files':STRINGS,'use_native_requirement':{'type':'boolean'},'run_id':TEXT},
        ['requirement_id','requirement_root','repo','task_ids'])
    base=[v for v in CONTROL_TARGET['oneOf'] if 'task_ids' not in v.get('properties',{})]
    supplemental=obj({'run_id':TEXT,'task_id':TEXT,'profile':{'const':'mysql_migration'}},['task_id','profile'])
    decision=obj({'run_id':TEXT,'decision_id':TEXT},['decision_id'])
    delivery=obj({'run_id':TEXT,'source_branch':TEXT,'target_branch':{'const':'main'},'remote':TEXT},
        ['source_branch','target_branch','remote'])
    recovery=obj({'request_id':TEXT,'expected_head':{'type':'string','pattern':'^[0-9a-f]{40}$'},
        'reason':TEXT},['request_id','expected_head','reason'])
    return {'oneOf':[intake,run_policy,supplemental,decision,delivery,recovery,*base]}


def plan_contract_errors(tasks):
    """All state-independent staged-contract errors; never choose dependencies."""
    errors=[]
    providers={t.get('task_id'):t for t in tasks if isinstance(t,dict) and isinstance(t.get('task_id'),str)}
    for index,task in enumerate(tasks):
        if not isinstance(task,dict):continue
        stages=task.get('stage_dependencies');dependencies=task.get('dependencies',[])
        names=('development_start','integration','acceptance')
        if (not isinstance(stages,dict) or set(stages)!=set(names)
                or not all(isinstance(stages[n],list) and all(isinstance(v,str) for v in stages[n]) for n in names)
                or not isinstance(dependencies,list) or not all(isinstance(v,str) for v in dependencies)):continue
        development,integration,acceptance=(set(stages[n]) for n in names)
        path='tasks.'+str(index)
        if not development<=integration<=acceptance:
            errors.append({'path':path+'.stage_dependencies','message':'STAGED_DEPENDENCY_GATES_NOT_MONOTONIC: development_start ⊆ integration ⊆ acceptance'})
        if (development|integration|acceptance)!=set(dependencies):
            errors.append({'path':path+'.dependencies','message':'STAGED_DEPENDENCIES_MUST_COVER_DEPENDENCIES: 必须等于三个阶段依赖的并集'})
        references=task.get('dependency_contracts',[])
        if not isinstance(references,list) or not all(isinstance(r,dict) and isinstance(r.get('task_id'),str) for r in references):continue
        expected=(set(dependencies)&(integration|acceptance))-development
        if {r['task_id'] for r in references}!=expected:
            errors.append({'path':path+'.dependency_contracts','message':'PRESTART_DEPENDENCY_CONTRACT_REQUIRED: 提前开发须逐项匹配晚期依赖 '+json.dumps(sorted(expected),ensure_ascii=False)})
        for reference in references:
            provided=providers.get(reference['task_id'],{}).get('provided_contracts',[])
            if not isinstance(provided,list) or not any(isinstance(p,dict) and all(p.get(k)==reference.get(k)
                    for k in ('contract_id','version','interface')) for p in provided):
                errors.append({'path':path+'.dependency_contracts','message':'DEPENDENCY_INTERFACE_CONTRACT_MISMATCH: '+reference['task_id']})
    return errors


def validate_payload(action, payload):
    from jsonschema import Draft202012Validator
    if action in ('ack_events','environment_recovered'):
        errors=list(Draft202012Validator(payload_schema(action)).iter_errors(payload))
        if errors:raise ValueError('INVALID_ACK_PAYLOAD: through 必须是已发行的连续前缀水位')
        return
    if action not in PAYLOADS and action not in ('prepare_requirement','activate_requirement','resolve_verification_exception','extend_task_scope','service_resume','service_reject','refresh_task_base','refresh_development_base','complete','finalize_delivery','append_development_log','commit_documentation','verify_supplemental','resolve_user_decision','verify_integration','deliver_integration','complete_requirement'):
        raise ValueError('COMMAND_UNSUPPORTED')
    errors = sorted(Draft202012Validator(payload_schema(action)).iter_errors(payload),
                    key=lambda error: list(map(str, error.absolute_path)))
    details = [{'path': '.'.join(map(str, error.absolute_path)), 'message': error.message}
               for error in errors]
    if action=='publish_plan' and isinstance(payload,dict) and isinstance(payload.get('tasks'),list):
        details.extend(plan_contract_errors(payload['tasks']))
    if details:
        raise ValueError('INVALID_COMMAND_PAYLOAD: ' + json.dumps(details, ensure_ascii=False))
