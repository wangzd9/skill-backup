"""Local development is authorized once per accepted requirement.

This policy covers local integration and internal coordination, never remote
push/deployment, paid reviews, production databases or destructive operations.
Candidate evidence and all acceptance gates remain independent.
"""
import re
import subprocess


def record(db, ledger, run, task_ids, branch, provenance, *, accepted_event_id=None):
    from business_ledger import _json, _hash, _deny
    if (not isinstance(provenance,dict) or provenance.get('authenticated') is not True
            or not provenance.get('message_id') or not provenance.get('primary_text')):
        _deny('AUTHENTICATED_DEVELOPMENT_REQUEST_REQUIRED')
    if subprocess.run(['git','check-ref-format','--branch',branch],capture_output=True).returncode:
        _deny('LOCAL_INTEGRATION_BRANCH_INVALID')
    if branch in ('main','master') and provenance.get('explicit_primary_branch')!=branch:
        _deny('PRIMARY_BRANCH_REQUIRES_EXPLICIT_DESTINATION')
    if not task_ids or not set(task_ids)<=set(run['task_ids']):
        _deny('DEVELOPMENT_POLICY_OUTSIDE_ACCEPTED_TASKS')
    identifier='development-policy-'+_hash([run['run_id'],provenance['message_id']])[:40]
    prior=db.execute('SELECT 1 FROM authorizations WHERE authorization_id=?',(identifier,)).fetchone()
    if prior:
        return {'authorization_id':identifier,'already_recorded':True}
    # No artificial 72-hour deadline for local development. The accepted run,
    # its pause/cancel controls and explicit revocation define the lifetime.
    spec={'run_id':run['run_id'],'repo':run['repo'],'branch':branch,'task_ids':list(task_ids),
          'expires_at':253402300799,'lifetime':'accepted_run_until_completion_or_revocation',
          'accepted_event_id':accepted_event_id,'managed_integration':branch not in ('main','master'),
          'paid_review_permission':False,'production_permission':False,'push_permission':False}
    db.execute('INSERT INTO authorizations VALUES(?,?,?,?,?,NULL)',
               (identifier,'development_policy',_json(spec),_json(provenance),ledger.clock()))
    event=ledger._event(db,'LOCAL_DEVELOPMENT_AUTOMATION_ACCEPTED',{
        'authorization_id':identifier,'spec':spec,'provenance':provenance,
        'internal_coordination_requires_user_approval':False,'candidate_gates_inherited':False},
        run_id=run['run_id'],role='COORDINATOR')
    return {'authorization_id':identifier,'event_id':event,'branch':branch,'lifetime':spec['lifetime']}


def enable(ledger, run_id, branch, provenance, *, command_id):
    """Maintenance ingress for the actual Codex user's workflow instruction.

    Not exposed to business models. Preserve the real channel and exact user
    text; never forge a Feishu message or impersonate a coordinator writer.
    """
    def operation(db):
        from business_ledger import _deny
        if provenance.get('platform')!='codex' or provenance.get('source')!='current_user_workflow_instruction':
            _deny('CURRENT_MAINTENANCE_USER_SOURCE_REQUIRED')
        run=ledger._one(db,'runs','run_id',run_id)
        return record(db,ledger,run,run['task_ids'],branch,provenance)
    return ledger._command('enable_local_development_automation',command_id,[run_id,branch,provenance],operation)


def default_for_request(db, ledger, run_id, task_ids, primary, provenance, *, branch=None, accepted_event_id=None):
    # An explicit candidate-only or later-destination instruction wins. The
    # caller can accept the requirement without silently choosing a branch.
    if re.search(r'(?:不要|不得|禁止|暂不|不允许)[^,，;；。\n]{0,40}(?:合入|合并)|合入分支由我|先交付候选',primary):
        return None
    run=ledger._one(db,'runs','run_id',run_id)
    if not branch:
        slug=re.sub(r'[^A-Za-z0-9_.-]+','-',run['requirement_id']).strip('-.')[:90] or 'requirement'
        from business_ledger import _hash
        branch='workflow/'+slug+'-'+_hash(run_id)[:8]
    source={**provenance}
    if branch in ('main','master') and branch in primary:
        source['explicit_primary_branch']=branch
    return record(db,ledger,run,task_ids,branch,source,accepted_event_id=accepted_event_id)


def policies(snapshot, candidate, now):
    from workflow_task_merge_policy import live
    grants={g['authorization_id']:g for g in snapshot['authorizations']}
    run=next((r for r in snapshot['runs'] if r['run_id']==candidate['run_id']),None)
    return [g for g in reversed(snapshot['authorizations']) if g['kind']=='development_policy'
            and live(grants,g['authorization_id'],now) and g['spec']['run_id']==candidate['run_id']
            and run and g['spec']['repo']==run['repo'] and candidate['task_id'] in g['spec']['task_ids']]


def derive(ledger, db, candidate):
    from business_ledger import _row, _hash, _json
    snapshot={'authorizations':[_row(r) for r in db.execute('SELECT * FROM authorizations ORDER BY created_at')],
              'runs':[ledger._one(db,'runs','run_id',candidate['run_id'])]}
    for parent in policies(snapshot,candidate,ledger.clock()):
        identifier='development-merge-'+_hash([candidate['candidate_id'],parent['authorization_id']])[:40]
        prior=db.execute('SELECT * FROM authorizations WHERE authorization_id=?',(identifier,)).fetchone()
        if prior:
            ledger._authorization(db,identifier,'merge')
            return {'authorization_id':identifier,'parent_policy_id':parent['authorization_id']}
        spec={key:candidate[key] for key in ('run_id','task_id','attempt','base','ac_version')}
        spec.update(repo=parent['spec']['repo'],branch=parent['spec']['branch'],
                    expires_at=parent['spec']['expires_at'],parent_policy_id=parent['authorization_id'],
                    managed_integration=parent['spec']['managed_integration'])
        db.execute('INSERT INTO authorizations VALUES(?,?,?,?,?,NULL)',
                   (identifier,'merge',_json(spec),_json(parent['provenance']),ledger.clock()))
        event=ledger._event(db,'LOCAL_DEVELOPMENT_MERGE_PERMIT_DERIVED',{
            'authorization_id':identifier,'parent_policy_id':parent['authorization_id'],
            'candidate_id':candidate['candidate_id'],'spec':spec,'candidate_gates_inherited':False},
            run_id=candidate['run_id'],role='COORDINATOR')
        return {'authorization_id':identifier,'parent_policy_id':parent['authorization_id'],'event_id':event}
    return None


def facts(snapshot, run_id):
    return [{'authorization_id':g['authorization_id'],'branch':g['spec']['branch'],
             'task_ids':g['spec']['task_ids'],'revoked':g['revoked_at'] is not None,
             'local_operations':'开发/派单/验证/返修/安全收口/基线刷新/本地合入，无需逐项授权',
             'separate_permissions':'OCR和强审遵守原预算期限；push/生产/真实远端副作用按独立边界',
             'lifetime':g['spec']['lifetime']}
            for g in snapshot['authorizations'] if g['kind']=='development_policy' and g['spec']['run_id']==run_id]
