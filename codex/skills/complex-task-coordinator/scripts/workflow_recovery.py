"""Concise recovery guidance referencing existing diagnostics and full contracts."""


def guidance(scope, result, *, action=None, invalid_fields=None, not_started=False):
    import json
    from workflow_runtime import COMMAND_TOOLS
    decision=result.get('decision') or {}
    phase=result.get('phase') or decision.get('decision_phase') or 'UNVERIFIED'
    state=result.get('side_effect_state') or decision.get('side_effect_state') or 'UNKNOWN'
    known=not_started or phase=='INVALID_NOT_STARTED'
    fields=invalid_fields or result.get('invalid_fields') or result.get('fields',[])
    if not fields and str(result.get('code','')).startswith('INVALID_COMMAND_PAYLOAD: '):
        try:fields=json.loads(result['code'].split(': ',1)[1])
        except (ValueError,TypeError):pass
    return {'version':1,'code':result.get('code','UNVERIFIED'),'phase':phase,
        'invocation_started':False if known else None,
        'side_effect_state':'NONE' if known else state,
        'retryable':result.get('retryable',True if known else False),
        'new_command_id_required':result.get('new_command_id_required',False if known else None),
        'invalid_fields':fields,
        'available_entry':COMMAND_TOOLS.get(scope.get('role')),
        'contract_query':{'tool':'role_decision_context','operation':'contract','action':action} if action else None,
        'diagnostic_id':decision.get('decision_id'),
        'next_action':('修正指定字段后沿用同一command_id；尚未启动动作。' if known else
            '核对原回执及副作用状态；UNKNOWN不自动重放，业务门/版本变化先刷新材料。')}
