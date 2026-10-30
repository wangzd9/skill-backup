"""Native final validation payloads; no role/identity or permission overrides."""


def payload_schema(action):
    from workflow_contracts import obj, TEXT
    events={'type':'array','uniqueItems':True,'items':{'type':'integer','minimum':1}}
    if action=='verify_integration':
        return obj({'argv':{'type':'array','minItems':1,'items':TEXT},
            'profile':{'enum':['local','mysql']},'report_format':{'enum':['pytest','junit']},
            'timeout_seconds':{'type':'number','exclusiveMinimum':0,'maximum':3600},
            'evidence':{'type':'object'}},['argv'])
    if action=='deliver_integration':
        check=obj({'id':TEXT,'result':{'enum':['PASS','FAIL','UNVERIFIED']},
            'mode':{'enum':['local','substitute','isolated_mysql','not_executed']},
            'test_event_ids':events,'notes':TEXT},['id','result','mode','test_event_ids','notes'])
        issue=obj({'id':TEXT,'kind':{'enum':['code','environment']},'task_id':TEXT,
            'test_event_ids':events,'summary':TEXT},['id','kind','test_event_ids','summary'])
        issue['allOf']=[{'if':{'properties':{'kind':{'const':'code'}}},'then':{'required':['task_id']}}]
        return obj({'checks':{'type':'array','minItems':1,'items':check},
            'issues':{'type':'array','items':issue},'summary':TEXT},['checks','issues','summary'])
    if action=='complete_requirement':
        disposition=obj({'verification_id':TEXT,'test_event_ids':events,'reason':TEXT},
            ['verification_id','test_event_ids','reason'])
        return obj({'summary':TEXT,'review':TEXT,
            'historical_failures':{'type':'array','items':disposition}},['summary','review','historical_failures'])
    raise ValueError('COMPLETION_COMMAND_UNSUPPORTED')
