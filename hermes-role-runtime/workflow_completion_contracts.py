"""Native final validation payloads; no role/identity or permission overrides."""


def payload_schema(action):
    from workflow_contracts import obj, TEXT, EVIDENCE
    events={'type':'array','uniqueItems':True,'items':{'type':'integer','minimum':1}}
    if action=='verify_integration':
        return obj({'argv':{'type':'array','minItems':1,'items':TEXT},
            'profile':{'enum':['local','mysql']},'report_format':{'enum':['pytest','junit']},
            'timeout_seconds':{'type':'number','exclusiveMinimum':0,'maximum':3600},
            'evidence':{'type':'object'}},['argv'])
    if action=='deliver_integration':
        coverage=obj({'obligation_id':TEXT,'phase':{'const':'local_validation'},
            'contract_sha256':{'type':'string','pattern':'^[0-9a-f]{64}$'},
            'scenario':TEXT,'assertion':TEXT,'environment':{'enum':['local','substitute','isolated_mysql']},
            'test_event_id':{'type':'integer','minimum':1},'case_id':TEXT,'event_ids':events},
            ['obligation_id','phase','contract_sha256','scenario','assertion','environment'])
        check=obj({'id':TEXT,'result':{'enum':['PASS','FAIL','UNVERIFIED']},
            'mode':{'enum':['local','substitute','isolated_mysql','not_executed']},
            'test_event_ids':events,'notes':TEXT,'coverage':{'type':'array','items':coverage}},['id','result','mode','test_event_ids','notes'])
        issue=obj({'id':TEXT,'kind':{'enum':['code','environment']},'task_id':TEXT,
            'test_event_ids':events,'summary':TEXT,'blocking':{'type':'boolean'},
            'confidence':{'enum':['confirmed','suspected']},'accepted_scope':{'enum':['local','out_of_scope','deferred_real_environment']},
            'ac_ids':{'type':'array','uniqueItems':True,'items':TEXT},'owner':TEXT,
            'disposition':{'enum':['repair','investigate','advisory','wait_environment','excluded_with_review']},
            'rationale':TEXT,'review_required':{'type':'boolean'}},['id','kind','test_event_ids','summary'])
        issue['allOf']=[{'if':{'properties':{'kind':{'const':'code'}}},'then':{'required':['task_id']}}]
        return obj({'checks':{'type':'array','minItems':1,'items':check},
            'issues':{'type':'array','items':issue},'summary':TEXT},['checks','issues','summary'])
    if action=='complete_requirement':
        disposition=obj({'verification_id':TEXT,'test_event_ids':events,'reason':TEXT},
            ['verification_id','test_event_ids','reason'])
        return obj({'summary':TEXT,'review':TEXT,
            'coverage_review':{'type':'array','items':obj({'obligation_id':TEXT,
                'validation_event_id':{'type':'integer','minimum':1},'result':{'enum':['PASS','FAIL','UNVERIFIED']},
                'reason':TEXT},['obligation_id','validation_event_id','result','reason'])},
            'issue_dispositions':{'type':'array','items':obj({'issue_id':TEXT,'reason':TEXT,
                'evidence':{'type':'array','minItems':1,'items':EVIDENCE}},['issue_id','reason','evidence'])},
            'historical_failures':{'type':'array','items':disposition}},['summary','review','historical_failures'])
    raise ValueError('COMPLETION_COMMAND_UNSUPPORTED')
