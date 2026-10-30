"""Bounded cron conversations; durable work survives a technical session yield."""
import hashlib
from pathlib import Path
import time

from runtime_common import read_json, save_json

DEFAULT_POLICY={'checkpoint_requests':48,'request_limit':64,'checkpoint_input_tokens':128000}


def policy(home):
    import yaml
    path=Path(home)/'config.yaml'
    config=yaml.safe_load(path.read_text()) if path.is_file() else {}
    configured=(config or {}).get('plugins',{}).get('entries',{}).get(
        'role-workflow-runtime',{}).get('settings',{}).get('session_policy',{})
    if not isinstance(configured,dict) or set(configured)-set(DEFAULT_POLICY):
        raise ValueError('INVALID_ROLE_SESSION_POLICY')
    result={**DEFAULT_POLICY,**configured}
    if (any(type(v) is not int or v<1 for v in result.values())
            or result['checkpoint_requests']>=result['request_limit']):
        raise ValueError('INVALID_ROLE_SESSION_POLICY')
    return result


def _path(home,scope):
    key=hashlib.sha256(scope['execution_id'].encode()).hexdigest()
    return Path(home)/'runtime/role-session-budgets'/(key+'.json')


def observe(home,scope,*,api_call_count=0,approx_input_tokens=0,session_id=''):
    if not scope or scope.get('kind')!='business' or not scope.get('workflow_version'):
        return None
    path=_path(home,scope)
    previous=read_json(path) if path.is_file() else {}
    limits=scope.get('session_policy') or policy(home)
    requests=max(previous.get('requests',0),int(api_call_count or 0)+1)
    tokens=max(previous.get('approx_input_tokens',0),int(approx_input_tokens or 0))
    value={'execution_id':scope['execution_id'],'work_id':scope['work_id'],
           'session_id':session_id,'observed_at':time.time(),'requests':requests,
           'approx_input_tokens':tokens,'policy':limits,
           'checkpoint_required':requests>=limits['checkpoint_requests'] or tokens>=limits['checkpoint_input_tokens']}
    value['last_blocked_action']=previous.get('last_blocked_action')
    save_json(path,value)
    return value


def tool_decision(home,scope,tool_name,args):
    if not scope or scope.get('kind')!='business' or not scope.get('workflow_version'):
        return None
    path=_path(home,scope)
    value=read_json(path) if path.is_file() else {}
    if not value.get('checkpoint_required'):
        return None
    from workflow_runtime import COMMAND_TOOLS, READ_TOOLS
    if tool_name in READ_TOOLS:
        return None
    if tool_name==COMMAND_TOOLS.get(scope['role']) and isinstance(args,dict):
        if args.get('action')=='safe_release':
            return None
        if args.get('action')=='checkpoint' and (args.get('payload') or {}).get('yield_execution') is True:
            return None
    value['last_blocked_action']={'at':time.time(),'reason':'SESSION_CHECKPOINT_REQUIRED',
        'tool':tool_name,'action':args.get('action') if isinstance(args,dict) else None,
        'invocation_started':False,'owner':scope['role'],'work_retained':True}
    save_json(path,value)
    return {'action':'block','message':
            '本技术会话已到安全交接阈值。停止发起新业务动作；用本角色 checkpoint 工具保存真实进展、'
            '未决事项和下一步，payload.yield_execution=true，仅确认实际读过的事件前缀，然后结束本回合。'
            '原 work/attempt 和全部门禁保留，下一原生回合以新会话接续；不要宣称任务完成。',
            'reason':'SESSION_CHECKPOINT_REQUIRED'}
