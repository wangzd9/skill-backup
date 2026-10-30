"""Small, native-bound read views; never execute business controls or shell text."""
from collections import Counter
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
from zoneinfo import ZoneInfo


HISTORY_NOTICE = '未经本轮核验，不得作为当前结论'
COORDINATOR_REPORT_SECTIONS = ('整体目标与进度', '执行者进度', '测试者进度',
                               'OCR情况和后续计划', '强审查情况', '风险', '下一步计划')
PERSONAL_REPORT_SECTIONS = ('当前工作', '本次完成', '阻塞与依赖', '下一步计划')


def report_presentation_contract(role):
    """Compact format contract attached to every live query result.

    This is presentation metadata only. It does not add facts, gates, or
    permissions, and gives the native model a stable fallback when the live
    query has to be supplemented by a bounded checkpoint artifact.
    """
    sections = (COORDINATOR_REPORT_SECTIONS if role == 'COORDINATOR'
                else PERSONAL_REPORT_SECTIONS)
    return {'role': role, 'sections': list(sections),
            'heading_style': '每个分区使用 **加粗标题**',
            'body_style': '每区单层短bullet；字段、状态/结论、阻塞/风险、下一步加粗',
            'missing_fact': '保留分区并写明待核实原因，不省略或把历史checkpoint冒充当前状态'}


def history(recorded_at=None, *, event_id=None):
    if isinstance(recorded_at, (int, float)):
        recorded_at = datetime.fromtimestamp(recorded_at, ZoneInfo('Asia/Shanghai')).isoformat()
    stamp = recorded_at or '时间未记录'
    return {'kind': 'historical_narrative', 'recorded_at': recorded_at, 'event_id': event_id,
            'current_verified': False,
            'notice': f'【历史叙述 · 写于 {stamp} · {HISTORY_NOTICE}】'}


def checkpoint_history(work, events):
    matches = [e for e in events if e['kind'] == 'CHECKPOINT_SAVED'
               and e.get('work_id') == work.get('work_id')
               and e.get('payload', {}).get('checkpoint') == work.get('checkpoint')]
    event = max(matches, key=lambda e: e['event_id']) if matches else {}
    return history(event.get('created_at'), event_id=event.get('event_id'))


def progress_context(snapshot, run_id, role):
    """Business descriptions for reports; saved narratives keep their own dates."""
    def excerpt(value, limit=800):
        text = str(value or '').strip()
        return text if len(text) <= limit else text[:limit] + '…（节选）'
    roles = ('A', 'B', 'C', 'TESTER') if role == 'COORDINATOR' else (role,)
    result = {'run_id': run_id, 'event_high_water': snapshot['event_high_water'],
              'roles': {}, 'tasks': {}, 'history_policy': HISTORY_NOTICE}
    for who in roles:
        works = [w for w in snapshot['works'] if w['run_id'] == run_id and w['role'] == who]
        records = [(checkpoint_history(w, snapshot['events']), w) for w in works if w.get('checkpoint')]
        if records:
            stamp, work = max(records, key=lambda row: row[0].get('event_id') or 0)
            checkpoint = work['checkpoint']
            if isinstance(checkpoint, dict):
                waiting = checkpoint.get('waiting_external') or []
                if not isinstance(waiting, list):
                    waiting = [waiting]
                result['roles'][who] = {**stamp, 'work_id': work['work_id'],
                    'task_id': work.get('task_id'), 'work_state': work['state'],
                    'summary': excerpt(checkpoint.get('summary') or checkpoint.get('done')),
                    'next': excerpt(checkpoint.get('next')),
                    'waiting_external': [excerpt(v, 240) for v in waiting[:3]]}
    for task in snapshot['tasks']:
        if task['run_id'] != run_id or task['role'] not in roles:
            continue
        candidate = next((c for c in reversed(snapshot['candidates'])
            if c['run_id'] == run_id and c['task_id'] == task['task_id']
            and c['attempt'] == task['attempt'] and c['state'] == 'CURRENT'), None)
        result['tasks'][task['task_id']] = {'role': task['role'], 'phase': task['phase'],
            'required_checks': task['required_checks'],
            'description': excerpt(str(candidate.get('summary') or '').split('\n\n')[0], 500) if candidate else '',
            'source': 'candidate_delivery' if candidate else 'task_contract'}
    from workflow_completion import facts
    final = facts(snapshot, run_id)
    report = final.get('report') or {}
    payload = report.get('payload') or {}
    result['completion'] = {'state': final['state'],
        'head': (final.get('target') or {}).get('head'),
        'event_id': report.get('event_id'), 'recorded_at': report.get('created_at'),
        'result': payload.get('result'), 'summary': excerpt(payload.get('summary')),
        'checks': [{key: check[key] for key in ('id', 'result') if key in check}
                   for check in payload.get('checks', [])],
        'issues': [{key: excerpt(issue[key]) for key in ('id', 'task_id', 'kind', 'summary') if key in issue}
                   for issue in payload.get('issues', [])]}
    candidate_ids = {c['candidate_id'] for c in snapshot['candidates'] if c['run_id'] == run_id}
    result['strong_review_actions'] = [{key: action[key] for key in ('action_id', 'state')}
        for action in snapshot['external_actions']
        if action['candidate_id'] in candidate_ids and action['kind'] != 'ocr']
    return result


def _artifact(ledger, name, value):
    from workflow_runtime import verified_input_view
    return verified_input_view(ledger.path.parent/'query-views', name, value, pretty=True)


def _paged_artifact(ledger, name, value, *, max_bytes=6144):
    """Persist large JSON as bounded UTF-8 pages and a small index hierarchy."""
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
    page_target = 2000  # Data is JSON-escaped inside a page envelope.
    chunks, current, current_bytes = [], [], 0
    for character in serialized:
        width = len(character.encode('utf-8'))
        if current and current_bytes + width > page_target:
            chunks.append(''.join(current))
            current, current_bytes = [], 0
        current.append(character)
        current_bytes += width
    if current or not chunks:
        chunks.append(''.join(current))

    page_refs = []
    for index, chunk in enumerate(chunks, 1):
        page_value = {'schema_version': 1, 'kind': name+'-page',
                      'page': index, 'data': chunk}
        encoded = json.dumps(page_value, ensure_ascii=False, sort_keys=True, indent=2).encode()
        if len(encoded) > max_bytes:
            raise ValueError('QUERY_ARTIFACT_PAGE_EXCEEDS_BYTE_BUDGET')
        page_refs.append(_artifact(ledger, f'{name}-page-{index:03d}', page_value))

    index_refs = []
    for start in range(0, len(page_refs), 16):
        page_group = page_refs[start:start+16]
        index_value = {'schema_version': 1, 'kind': name+'-index',
                       'first_page': start+1, 'pages': page_group}
        index_ref = _artifact(ledger, f'{name}-index-{len(index_refs)+1:03d}', index_value)
        if index_ref['bytes'] > max_bytes:
            raise ValueError('QUERY_ARTIFACT_INDEX_EXCEEDS_BYTE_BUDGET')
        index_refs.append(index_ref)
    return _artifact(ledger, name+'-manifest', {
        'schema_version': 1, 'kind': name+'-manifest',
        'document_bytes': len(serialized.encode('utf-8')),
        'page_count': len(page_refs), 'index_count': len(index_refs),
        'indexes': index_refs, 'page_limit_bytes': max_bytes,
        'reassembly': '按index与page顺序连接每页data，再JSON解析；内容完整保留。'})


def _history_index(works, details_ref):
    latest = {}
    for work in works:
        row = {'work_id': work.get('work_id'), 'role': work.get('role'),
               'run_id': work.get('run_id'), 'state': work.get('state'),
               'checkpoint_event_id': work.get('checkpoint_history', {}).get('event_id'),
               'checkpoint_recorded_at': work.get('checkpoint_history', {}).get('recorded_at'),
               'checkpoint_current_verified': False}
        previous = latest.get(work['role'])
        if previous is None or (row.get('checkpoint_event_id') or 0) > (previous.get('checkpoint_event_id') or 0):
            latest[work['role']] = row
    return {'history_policy': history()['notice'], 'work_count': len(works),
            'latest_checkpoint_by_role': latest, 'detail_manifest': details_ref,
            'detail_policy': '完整历史按分页artifact读取；checkpoint只反映保存时判断，不是当前核验。'}


def _task_summary(row):
    """Keep every task and its gate state; full contracts remain pageable."""
    result = {key: row[key] for key in ('task_id', 'role', 'phase', 'attempt',
        'candidate_id', 'head') if key in row}
    if row.get('phase') not in ('COMPLETED', 'CANCELLED') and 'recorded_checks' in row:
        result['recorded_checks'] = row['recorded_checks']
    stages = row.get('dependency_stages') or {}
    readiness = {name: bool(stage.get('ready')) for name, stage in stages.items()}
    if row.get('phase') not in ('COMPLETED', 'CANCELLED') and readiness:
        if len(set(readiness.values())) == 1:
            result['dependency_ready'] = next(iter(readiness.values()))
        else:
            result['dependency_readiness'] = readiness
    details = {name: {'missing': stage.get('missing') or [],
                      'contract_mismatches': stage.get('contract_mismatches') or []}
               for name, stage in stages.items()
               if stage.get('missing') or stage.get('contract_mismatches')}
    if details:
        result['dependency_details'] = details
    contract = row.get('contract_summary') or {}
    for key in ('dependency_contracts','provided_contracts','minimum_reading_set',
                'risk_scenarios','test_environment','escalation_conditions'):
        value = contract.get(key)
        if value:
            result[key] = value
    return result


def _role_summary(row, selected_run_id):
    result = {key: row[key] for key in ('role', 'control', 'current_work_id',
        'writer_execution_id') if row.get(key) is not None}
    work_run_id = row.get('work_run_id')
    if work_run_id and work_run_id != selected_run_id:
        result['work_run_id'] = work_run_id
    return result


def _bounded(packet, fields, *, limit=8192, preserve=()):
    """Trim only optional rows; critical status, tasks and gates stay intact."""
    packet['omitted_counts'] = {}
    while len(json.dumps(packet, ensure_ascii=False).encode()) >= limit:
        available = [key for key in fields if key not in preserve and packet.get(key)]
        if not available:
            raise ValueError('QUERY_CORE_METADATA_EXCEEDS_BYTE_BUDGET')
        field = max(available, key=lambda key: len(json.dumps(packet.get(key, []), ensure_ascii=False)))
        packet[field].pop()
        packet['omitted_counts'][field] = packet['omitted_counts'].get(field, 0) + 1
    if not any(packet['omitted_counts'].values()):
        del packet['omitted_counts']
    return packet


def ocr_gate_state():
    """Read-only OCR version admission state, never a business grant or dispatch.

    A role that only sees an old REJECTED action cannot tell whether the version
    gate has since opened. Report the registry verdict plus the installed
    identity so the answer is evidence, not inference. Any failure degrades to
    UNKNOWN instead of claiming a gate is closed.
    """
    try:
        import json as _json
        import ocr_compat
        root = Path.home() / '.hermes'
        registry = root / 'ocr-version-admissions.json'
        records = []
        if registry.is_file():
            payload = _json.loads(registry.read_text())
            for key, record in (payload.get('admissions') or {}).items():
                records.append({'key': key[:16], 'version': (record.get('version') or '').splitlines()[0],
                                'state': record.get('state'),
                                'validated_at': record.get('validated_at'),
                                'native_binary_sha256': record.get('native_binary_sha256'),
                                'launcher_sha256': record.get('launcher_sha256')})
        installed = None
        try:
            import ocr_runtime
            identity = ocr_runtime.execution_identity()
            installed = {'version': (identity.get('version') or '').splitlines()[0],
                         'native_binary_sha256': identity.get('native_binary_sha256'),
                         'launcher_sha256': identity.get('launcher_sha256')}
        except Exception:
            installed = None
        # Compare the installed identity with the admitted records.
        verdict, reason = 'BLOCKED', 'NO_ADMISSION_RECORD_FOR_INSTALLED_IDENTITY'
        if isinstance(installed, dict):
            for record in records:
                if (record.get('native_binary_sha256') == installed.get('native_binary_sha256')
                        and record.get('launcher_sha256') == installed.get('launcher_sha256')):
                    verdict, reason = 'OPEN', 'INSTALLED_IDENTITY_ADMITTED'
                    break
        return {'state': verdict, 'reason': reason, 'registry_path': str(registry),
                'admissions': records, 'installed': installed,
                'note': '放行记录是兼容性自检结果，不代表业务授权或已派发；'
                        'BLOCKED 表示当前执行体尚未自检，下一次 preflight/prepare 会自动校验。'}
    except Exception as exc:
        return {'state': 'UNKNOWN', 'reason': type(exc).__name__,
                'note': '无法读取放行注册表；不要据此断言门禁关闭。'}


def live_progress(pool_path, role):
    """One ledger snapshot: present facts first, historical prose only by reference."""
    from workflow_runtime import ROLES, open_ledger
    if role not in ROLES:
        raise ValueError('ROLE_NOT_IN_RUNTIME')
    ledger = open_ledger(pool_path)
    snapshot = ledger.snapshot()
    checked = datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()
    slot = next(r for r in snapshot['roles'] if r['role'] == role)
    # Queries describe the selected run; draining work is identified separately.
    run_id = snapshot['current_run_id']
    run = next((r for r in snapshot['runs'] if r['run_id'] == run_id), None)
    tasks = [t for t in snapshot['tasks'] if t['run_id'] == run_id
             and (role == 'COORDINATOR' or t['role'] == role)]
    candidates = {c['candidate_id']: c for c in snapshot['candidates']}
    global_ocr = [a for a in snapshot['external_actions'] if a['kind'] == 'ocr']
    active_ocr = [a for a in global_ocr if a['state'] in ('SUBMITTED', 'UNKNOWN')]
    actions = [{**{key: a[key] for key in ('action_id', 'kind', 'state')},
                **{key: candidates[a['candidate_id']][key] for key in ('run_id', 'task_id')}}
               for a in snapshot['external_actions']
               if a['kind'] == 'ocr' or candidates[a['candidate_id']]['run_id'] == run_id]
    actions.sort(key=lambda a: (a['state'] not in ('SUBMITTED', 'UNKNOWN'), a['action_id']))
    gates = []
    if snapshot['mode'] != 'ACTIVE' or not snapshot['admission_open']:
        gates.append({'code': 'MECHANISM_ADMISSION_CLOSED'})
    if run and run['control'] != 'ACTIVE':
        gates.append({'code': 'RUN_' + run['control'], 'run_id': run_id})
    if slot['control'] != 'ACTIVE':
        gates.append({'code': 'ROLE_' + slot['control'], 'role': role})
    if active_ocr:
        gates.append({'code': 'OCR_SERIAL_ACTION_STILL_ACTIVE_OR_UNKNOWN', 'count': len(active_ocr)})
    # An open gate is not a diagnosis. Report what the OCR version gate actually
    # resolved to, so a role never has to infer it from a stale REJECTED action.
    ocr_version_gate = ocr_gate_state()
    # Surface a blocked version gate as a gate with its reason, so a stale
    # REJECTED action is never the only evidence a role can see.
    if ocr_version_gate.get('state') == 'BLOCKED':
        gates.append({'code': 'OCR_VERSION_GATE_BLOCKED',
                      'reason': ocr_version_gate.get('reason'),
                      'installed_version': (ocr_version_gate.get('installed') or {}).get('version')})
    task_rows = []
    for task in tasks:
        current = next((c for c in snapshot['candidates'] if c['run_id'] == run_id
                        and c['task_id'] == task['task_id'] and c['attempt'] == task['attempt']
                        and c['state'] == 'CURRENT'), None)
        row = {key: task[key] for key in ('task_id', 'role', 'phase', 'attempt')}
        row['dependency_stages']={stage:ledger.dependency_readiness(run_id,task['task_id'],stage)
            for stage in ('development_start','integration','acceptance')}
        row['contract_summary']=task.get('contract') or {}
        if current:
            row.update(candidate_id=current['candidate_id'], head=current['head'])
        # A retained CURRENT candidate does not reactivate a cancelled task.
        if task['phase'] not in ('CANCELLED', 'COMPLETED'):
            if task.get('unresolved_reason'):
                gates.append({'code': 'TASK_UNRESOLVED', 'task_id': task['task_id']})
            checks = [v for v in snapshot['verifications'] if current and v['candidate_id'] == current['candidate_id']]
            row['recorded_checks'] = {name: [v['result'] for v in checks if v['check_name'] == name]
                                      for name in task['required_checks']}
        task_rows.append(row)
    roles = [{key: r[key] for key in ('role', 'control', 'current_work_id', 'writer_execution_id')}
             for r in snapshot['roles'] if role == 'COORDINATOR' or r['role'] == role]
    for row in roles:
        work = next((w for w in snapshot['works'] if w['work_id'] == row['current_work_id']), None)
        row['work_run_id'] = work['run_id'] if work else None
        if work and work['kind'] == 'coordination' and work['run_id'] != run_id:
            gates.append({'code': 'RUN_SWITCH_HANDOFF_REQUIRED', 'work_id': work['work_id'],
                          'work_run_id': work['run_id'], 'selected_run_id': run_id,
                          'next': 'safe_release 或等待已核验的原生执行退出；不得清除在途 writer'})
    works = [{**{key: w.get(key) for key in ('work_id', 'role', 'run_id', 'state', 'checkpoint')},
              'checkpoint_history': checkpoint_history(w, snapshot['events'])}
             for w in snapshot['works'] if w['run_id'] == run_id
             and (role == 'COORDINATOR' or w['role'] == role)]
    history_details = _paged_artifact(ledger, role+'-history-detail', {
        'history_policy': history()['notice'], 'records': works})
    artifacts = {
        'history': _artifact(ledger, role+'-history',
            _history_index(works, history_details)),
        'history_detail': history_details,
        'facts': _artifact(ledger, role+'-facts', {'checked_at': checked,
            'event_high_water': snapshot['event_high_water'], 'tasks': tasks, 'roles': roles,
            'external_actions': actions, 'open_gates': gates}),
    }
    # Saved health is supplementary, timestamped evidence, never live business authority.
    from workflow_observation import saved_health
    observation = saved_health(Path(pool_path).parent, pool_path, now=datetime.now().timestamp())
    artifacts['observation'] = _artifact(ledger, role+'-observation', observation)
    packet = {'kind': 'readonly-live-progress', 'checked_at': checked,
        'event_high_water': snapshot['event_high_water'], 'source': str(ledger.path),
        'authority': '当前状态以本次业务账本为准；invocation.json 是提交记录；checkpoint 是历史叙述。',
        'role': role, 'mode': snapshot['mode'],
        'run': {k: run[k] for k in ('run_id', 'requirement_id', 'control')} if run else None,
        'ocr_serial_gate': {'blocked': bool(active_ocr), 'blocking_count': len(active_ocr),
                            'state_counts': dict(Counter(a['state'] for a in global_ocr)), 'scope': 'global'},
        'ocr_version_gate': {'state': ocr_version_gate.get('state'),
            'reason': ocr_version_gate.get('reason'),
            'installed': {'version': (ocr_version_gate.get('installed') or {}).get('version')},
            'detail_artifact': _artifact(ledger, role+'-ocr-version-gate', ocr_version_gate)},
        'external_actions': [a for a in actions if a['state'] in ('SUBMITTED', 'UNKNOWN', 'INCOMPLETE')],
        'external_action_summary': {'total': len(actions),
            'state_counts': dict(Counter(a['state'] for a in actions)),
            'serial_blocking_count': len(active_ocr), 'detail_artifact': 'artifacts.manifest → facts'},
        'open_gates': gates.copy(), 'tasks': [_task_summary(row) for row in task_rows],
        'roles': [_role_summary(row, run_id) for row in roles],
        'gate_coverage': '运行/角色控制、全局OCR串行占用、任务未解决原因；候选检查列为账本记录，不表示验收已通过。',
        'artifacts': artifacts,
        'limits': '检查结果仅为账本记录，不代替证据复核或批准。测试能力未实测则待验证；历史 FAIL 不证明当前环境不可用。'}
    from workflow_supplemental import summary
    supplemental_rows=summary(snapshot,run_id)
    packet['artifacts']['supplemental_verifications']=_artifact(
        ledger,role+'-supplemental-verifications',supplemental_rows)
    packet['supplemental_verifications']=[{key:row[key] for key in (
        'work_id','task_id','candidate_id','state','owner','profile',
        'supersedes','result') if key in row}
        for row in supplemental_rows]
    from workflow_development_policy import facts as automation_facts
    from workflow_verification_lifecycle import facts as retirement_facts
    automation=automation_facts(snapshot,run_id)
    packet['artifacts']['local_development_automation']=_artifact(
        ledger,role+'-local-development-automation',automation)
    packet['local_development_automation']=[{key:row[key] for key in (
        'authorization_id','branch','task_ids','revoked','lifetime') if key in row}
        for row in automation]
    retirements=retirement_facts(snapshot,run_id)
    packet['artifacts']['verification_retirements']=_artifact(
        ledger,role+'-verification-retirements',retirements)
    packet['verification_retirements']=[{key:row[key] for key in (
        'work_id','task_id','candidate_id','state','reason','requires_user_approval')
        if key in row} for row in retirements]
    from workflow_user_decisions import pending as pending_decisions
    from workflow_completion import facts as completion_facts
    completion=completion_facts(snapshot,run_id)
    packet['completion']={key:completion[key] for key in ('state','work_id','completion_event_id') if key in completion}
    if completion.get('plan'):
        packet['completion'].update(validation_task_id=completion['plan']['validation_task_id'],
            closure_task_id=completion['plan']['closure_task_id'],scope='accepted_local_development')
        packet['artifacts']['completion']=_artifact(ledger,role+'-completion',completion)
    decisions=pending_decisions(snapshot,run_id)
    packet['user_decisions']={'pending_count':len(decisions)}
    if decisions:
        packet['user_decisions']['artifact']=_artifact(ledger,role+'-user-decisions',decisions)
        packet['user_decisions']['notice']='用户答复待核对原提问及当前实现；登记不等于关闭门禁或授予新权限。'
    from workflow_wait import capabilities
    runtime_capabilities=capabilities(role)
    packet['artifacts']['runtime_capabilities']=_artifact(
        ledger,role+'-runtime-capabilities',runtime_capabilities)
    packet['runtime_capabilities']={key:runtime_capabilities[key] for key in (
        'ordinary_workspace_write','ordinary_terminal','coordinator_shell_available')}
    # Business bodies stay reachable without growing the 8 KiB core envelope.
    packet['artifacts']['report_context'] = _artifact(
        ledger, role+'-report-context', progress_context(snapshot, run_id, role))
    admission = {name: ledger.admission_hint(name)
                 for name in ('COORDINATOR','A','B','C','TESTER')}
    packet['artifacts']['model_admission'] = _artifact(ledger, role+'-admission', admission)
    packet['model_admission'] = {name: {k:v for k,v in hint.items()
        if k in ('can_start_model','reason','work_id','drain_only')} for name,hint in admission.items()}
    for name, hint in admission.items():
        if 'wait_recheck' in hint:
            packet['model_admission'][name]['wait_recheck'] = {k:v for k,v in hint['wait_recheck'].items()
                if k in ('due','reason','recheck_at','gates_unchanged')}
        if hint.get('waiting_external'):
            packet['model_admission'][name]['waiting_count'] = len(hint['waiting_external'])
    if run:
        # Existing conversations already know role_live_progress. Keep the
        # essential Git query usable before a new tool catalog is assembled.
        repository = repo_state(pool_path, role)
        if repository.get('run_id') == run_id:
            packet['repository'] = {k: repository[k] for k in ('checked_at', 'repo', 'state',
                'branch', 'head', 'dirty', 'dirty_count', 'tracked_content_change_count',
                'content_evidence_state', 'artifact') if k in repository}
        else:
            packet['repository'] = {'state': 'UNVERIFIED', 'dirty': None,
                'reason': repository.get('reason') or 'SELECTED_RUN_CHANGED_DURING_QUERY'}
    artifact_refs = packet['artifacts']
    artifact_manifest = _artifact(ledger, role+'-live-progress-artifacts', artifact_refs)
    packet['artifacts'] = {'manifest': artifact_manifest}
    # Core rows are already reduced to bounded summaries. Keep them intact and
    # trim only optional action history; this guarantees the tool never drops a
    # task or role merely because repository metadata grew.
    return _bounded(packet, ('external_actions',), limit=8192,
                    preserve=('open_gates', 'tasks', 'roles'))


def repo_state(pool_path, role):
    """Fixed Git argv against the selected run's repository; no model-supplied paths."""
    from workflow_runtime import ROLES, open_ledger
    if role not in ROLES:
        raise ValueError('ROLE_NOT_IN_RUNTIME')
    ledger = open_ledger(pool_path)
    snapshot = ledger.snapshot()
    run = next((r for r in snapshot['runs'] if r['run_id'] == snapshot['current_run_id']), None)
    if not run:
        raise ValueError('SELECTED_RUN_REPOSITORY_REQUIRED')
    repo = Path(run['repo']).resolve()
    def git(*args):
        return subprocess.check_output(['git', '--no-optional-locks', '-c', 'core.fsmonitor=false',
            '-c', 'core.untrackedCache=false', '-C', str(repo), *args],
            env={k: v for k, v in os.environ.items() if not k.startswith('GIT_')},
            stderr=subprocess.DEVNULL, timeout=15).decode('utf-8', errors='replace')
    checked = datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()
    try:
        before = git('rev-parse', 'HEAD').strip()
        status = git('status', '--porcelain=v2', '--branch', '-z', '--untracked-files=normal')
        worktrees_format = 'porcelain_z'
        try:
            worktrees = git('worktree', 'list', '--porcelain', '-z')
        except subprocess.CalledProcessError as exc:
            # Cron can resolve the system Git 2.34 while an interactive shell
            # resolves Git 2.49. Preserve older Git's quoted records verbatim.
            if exc.returncode != 129:
                raise
            worktrees = '\0'.join(git('worktree', 'list', '--porcelain').splitlines())
            worktrees_format = 'porcelain_quoted_lines'
        after = git('rev-parse', 'HEAD').strip()
        repeat = git('status', '--porcelain=v2', '--branch', '-z', '--untracked-files=normal')
    except (OSError, subprocess.SubprocessError):
        return {'kind': 'readonly-repo-state', 'checked_at': checked, 'repo': str(repo),
                'event_high_water': snapshot['event_high_water'], 'state': 'UNVERIFIED',
                'dirty': None, 'reason': 'REPOSITORY_QUERY_UNAVAILABLE'}
    headers, files = {}, []
    records = iter(status.split('\0'))
    for record in records:
        if record.startswith('# '):
            key, _, value = record[2:].partition(' '); headers[key] = value
        elif record:
            if record.startswith('2 '):
                files.append({'record': record, 'original_path': next(records, '')})
            else:
                files.append({'record': record})
    ahead = behind = None
    if 'branch.ab' in headers:
        a, b = headers['branch.ab'].split(); ahead, behind = int(a[1:]), int(b[1:])
    stable = before == after and status == repeat
    content_changes = []
    content_state = 'UNVERIFIED'
    if stable:
        try:
            # Porcelain v2 has HEAD/index object IDs, never a worktree hash.
            # Read the real unstaged diff; suppress external diff/textconv hooks.
            raw = git('diff', '--numstat', '--no-renames', '--no-ext-diff', '--no-textconv', '-z', '--')
            for record in raw.split('\0'):
                if record:
                    added, deleted, path = record.split('\t', 2)
                    content_changes.append({'path': path,
                        'added': int(added) if added.isdecimal() else None,
                        'deleted': int(deleted) if deleted.isdecimal() else None})
            final_status = git('status', '--porcelain=v2', '--branch', '-z', '--untracked-files=normal')
            stable = repeat == final_status and after == git('rev-parse', 'HEAD').strip()
            content_state = 'OBSERVED' if stable else 'UNVERIFIED'
        except (OSError, subprocess.SubprocessError, ValueError):
            content_state = 'UNVERIFIED'
    result = {'kind': 'readonly-repo-state', 'checked_at': checked,
        'event_high_water': snapshot['event_high_water'], 'run_id': run['run_id'],
        'repo': str(repo), 'scope': 'selected_run_repository', 'state': 'OBSERVED' if stable else 'UNVERIFIED',
        'branch': headers.get('branch.head'), 'head': before, 'dirty': bool(files) if stable else None,
        'dirty_count': len(files), 'dirty_files': files.copy(), 'upstream': headers.get('branch.upstream'),
        'ahead_behind': {'ahead': ahead, 'behind': behind, 'basis': 'local_refs_only_no_fetch'},
        'worktrees': [row for row in worktrees.split('\0') if row],
        'worktrees_format': worktrees_format,
        'content_evidence_state': content_state,
        'tracked_content_change_count': len(content_changes) if content_state == 'OBSERVED' else None,
        'unstaged_content_changes': content_changes,
        'interpretation': {
            'porcelain_object_ids': 'HEAD与暂存区的blob；二者相同不能证明工作区内容未变。真实内容差异看unstaged_content_changes。',
            'ownership': '未核实；Git状态、文件名和mtime不能证明归属哪个会话，也不证明文件可删除。',
            'merge_location': '有效授权spec.repo（grant.repo）同仓；共享dirty时支持受管integration-worktrees独立合入，具体位置看回执frozen.integration_worktree；operations仅存日志。'},
        'reason': None if stable else 'REPOSITORY_CHANGED_DURING_QUERY'}
    if role == 'COORDINATOR' and stable:
        from workflow_documentation import inspect
        try:
            documents = inspect(repo)
            result['documentation_review'] = {
                'document_count': len(documents['documents']),
                'other_change_count': len(documents['other_changes']),
                'artifact': _artifact(ledger, 'documentation-review', documents),
                'next': '读逐文件content/diff/sha256，协调者判断合理后commit_documentation；不能只写dirty等待。'}
        except (OSError, ValueError, subprocess.SubprocessError):
            result['documentation_review'] = {'state': 'UNVERIFIED', 'next': '复核失败，不能自动提交。'}
    result['artifact'] = _artifact(ledger, 'repo-state', result)
    return _bounded(result, ('dirty_files', 'worktrees', 'unstaged_content_changes'))
