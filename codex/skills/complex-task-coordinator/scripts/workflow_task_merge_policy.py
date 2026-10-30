"""Preserve native task consent across attempts; derive exact permits later.

Only a narrowly recognized task/branch grant qualifies. Explicit candidate,
attempt, base or command restrictions remain exact grants, never promoted.
"""
import re
import subprocess


def boundary(task):
    from business_ledger import _hash
    return _hash({key: task.get(key, {} if key == 'contract' else [])
        for key in ('role', 'dependencies', 'scope', 'ac_version', 'required_checks', 'contract')})


def broad_source(grant, run):
    from manual_access import full_permission_action
    spec = grant['spec']; source = grant['provenance']
    if (grant['kind'] != 'merge' or spec.get('parent_policy_id') or spec.get('postmerge_argv')
            or source.get('authenticated') is not True or source.get('current_full') is not True
            or not source.get('message_id') or not source.get('user_id')):
        return False
    text = source.get('primary_text', '')
    text = full_permission_action(text) or text.strip()
    name = r'(?:(?:本需求|当前需求)\s*)?(?:(?:' + '|'.join(re.escape(v) for v in
        (run['run_id'], run['requirement_id'])) + r')\s*)?'
    task = re.escape(spec['task_id']); branch = re.escape(spec['branch'])
    action = r'(?:' + task + r'\s*合(?:入|并)(?:到|至)?\s*' + branch + r'|合入\s*' + task + r'\s*(?:到|至)\s*' + branch + ')'
    return bool(re.fullmatch(r'(?:请\s*)?授权\s*' + name + action +
        r'(?:\s*[,，;；]\s*有效(?:期)?\s*\d+\s*(?:小时|分钟))?\s*[。！!]?\s*', text))


def register(ledger, db, grant, task):
    from business_ledger import _json
    spec = grant['spec']; run = ledger._one(db, 'runs', 'run_id', spec['run_id'])
    if not broad_source(grant, run):
        return None
    identifier = grant['authorization_id'] + '-task-policy'
    if db.execute('SELECT 1 FROM authorizations WHERE authorization_id=?', (identifier,)).fetchone():
        return None
    policy = {key: spec[key] for key in ('run_id', 'task_id', 'repo', 'branch', 'ac_version', 'expires_at')}
    policy.update(parent_policy_id=grant['authorization_id'], task_boundary_sha256=boundary(task),
                  origin_base=spec['base'], origin_attempt=spec['attempt'])
    db.execute('INSERT INTO authorizations VALUES(?,?,?,?,?,NULL)',
        (identifier, 'merge_task_policy', _json(policy), _json(grant['provenance']), ledger.clock()))
    event = ledger._event(db, 'TASK_MERGE_POLICY_RECORDED', {
        'authorization_id': identifier, 'source_authorization_id': grant['authorization_id'],
        'spec': policy, 'tests_and_approval_inherited': False}, run_id=spec['run_id'])
    return {'task_policy_id': identifier, 'task_policy_event_id': event}


def reconcile(ledger):
    """Reconstruct only consent proven by original grant and immutable plan.

    Never substitute today's expanded task scope for a historical grant.
    """
    from business_ledger import _row
    snapshot = ledger.snapshot(); existing = {g['authorization_id'] for g in snapshot['authorizations']}
    runs = {r['run_id']: r for r in snapshot['runs']}; results = []
    for grant in snapshot['authorizations']:
        spec = grant['spec']; run = runs.get(spec.get('run_id'))
        if (not run or grant['kind'] != 'merge' or grant['revoked_at'] is not None
                or spec.get('expires_at', 0) <= ledger.clock()
                or grant['authorization_id'] + '-task-policy' in existing or not broad_source(grant, run)):
            continue
        def operation(db, grant=grant):
            source = ledger._one(db, 'authorizations', 'authorization_id', grant['authorization_id'])
            ledger._trusted_grant(source['provenance'])
            if source['revoked_at'] is not None or source['spec']['expires_at'] <= ledger.clock():
                return {'policy_recorded': False}
            plans = [_row(p) for p in db.execute('SELECT * FROM plans WHERE run_id=? AND accepted_at<=? ORDER BY version DESC',
                (source['spec']['run_id'], source['created_at']))]
            item = next((t for p in plans[:1] for t in p['spec'] if t['task_id'] == source['spec']['task_id']), None)
            if not item or item['ac_version'] != source['spec']['ac_version']:
                return {'policy_recorded': False, 'reason': 'HISTORICAL_TASK_BOUNDARY_UNPROVEN'}
            # Scope amendments are separately audited; do not guess their
            # historical effective contract during legacy reconstruction.
            amendments = [_row(e) for e in db.execute("SELECT * FROM events WHERE run_id=? AND kind='TASK_SCOPE_EXTENDED' AND created_at<=?",
                (source['spec']['run_id'], source['created_at']))]
            if any(e['payload']['task_id'] == item['task_id'] for e in amendments):
                return {'policy_recorded': False, 'reason': 'HISTORICAL_AMENDED_SCOPE_REQUIRES_REVIEW'}
            task = {**item, 'dependencies': item.get('dependencies', []),
                    'required_checks': item.get('required_checks', ['independent_test']),
                    'contract': {k: item[k] for k in ('ac', 'acceptance', 'inputs', 'stop_conditions', 'worktree',
                        'stage_dependencies','dependency_contracts','provided_contracts','minimum_reading_set','risk_scenarios',
                        'test_environment','escalation_conditions') if k in item}}
            return register(ledger, db, source, task) or {'policy_recorded': False}
        results.append(ledger._command('reconcile_task_merge_policy',
            'task-policy-source:' + grant['authorization_id'], [grant['authorization_id']], operation))
    return results


def live(grants, identifier, now):
    seen = set()
    while identifier:
        if identifier in seen:
            return False
        seen.add(identifier); grant = grants.get(identifier)
        if grant is None or grant['revoked_at'] is not None or grant['spec'].get('expires_at', 0) <= now:
            return False
        identifier = grant['spec'].get('parent_policy_id')
    return True


def policies(snapshot, candidate, now):
    grants = {g['authorization_id']: g for g in snapshot['authorizations']}
    task = next((t for t in snapshot['tasks'] if t['run_id'] == candidate['run_id'] and t['task_id'] == candidate['task_id']), None)
    run = next((r for r in snapshot['runs'] if r['run_id'] == candidate['run_id']), None)
    if not task or not run:
        return []
    return [g for g in reversed(snapshot['authorizations']) if g['kind'] == 'merge_task_policy'
        and live(grants, g['authorization_id'], now)
        and all(g['spec'].get(key) == candidate[key] for key in ('run_id', 'task_id', 'ac_version'))
        and g['spec']['repo'] == run['repo']
        and g['spec']['task_boundary_sha256'] == boundary(task)]


def derive(ledger, db, work, candidate):
    from business_ledger import _row, _hash, _json, _deny
    snapshot = {'authorizations': [_row(r) for r in db.execute('SELECT * FROM authorizations ORDER BY created_at')],
                'tasks': [_row(r) for r in db.execute('SELECT * FROM tasks WHERE run_id=?', (candidate['run_id'],))],
                'runs': [ledger._one(db, 'runs', 'run_id', candidate['run_id'])]}
    for parent in policies(snapshot, candidate, ledger.clock()):
        policy = parent['spec']
        # A routine forward base refresh is allowed; an unrelated branch is not.
        if subprocess.run(['git', '-C', policy['repo'], 'merge-base', '--is-ancestor',
            policy['origin_base'], candidate['base']], capture_output=True, timeout=15).returncode:
            continue
        identifier = 'task-merge-' + _hash([candidate['candidate_id'], parent['authorization_id']])[:40]
        existing = db.execute('SELECT * FROM authorizations WHERE authorization_id=?', (identifier,)).fetchone()
        if existing:
            ledger._authorization(db, identifier, 'merge')
            return {'authorization_id': identifier, 'parent_policy_id': parent['authorization_id']}
        spec = {key: candidate[key] for key in ('run_id', 'task_id', 'attempt', 'base', 'ac_version')}
        spec.update(repo=policy['repo'], branch=policy['branch'], expires_at=policy['expires_at'],
                    parent_policy_id=parent['authorization_id'])
        db.execute('INSERT INTO authorizations VALUES(?,?,?,?,?,NULL)',
            (identifier, 'merge', _json(spec), _json(parent['provenance']), ledger.clock()))
        event = ledger._event(db, 'TASK_MERGE_PERMIT_DERIVED', {
            'authorization_id': identifier, 'parent_policy_id': parent['authorization_id'],
            'candidate_id': candidate['candidate_id'], 'spec': spec, 'tests_and_approval_inherited': False},
            run_id=work['run_id'], role=work['role'], work_id=work['work_id'])
        return {'authorization_id': identifier, 'parent_policy_id': parent['authorization_id'], 'event_id': event}
    return None
