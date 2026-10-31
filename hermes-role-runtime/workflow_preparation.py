"""Durable, native-authorized preparation of existing paused requirement packages.

This is a narrow contract editor, not a coordinator shell. File effects have a
frozen, durable intent before writes; retries reconcile old/new hashes and never
silently overwrite an intervening edit. Acceptance and selection are separate.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile


PREPARABLE = frozenset({'TASK_OUTSIDE_REGISTERED_REQUIREMENT',
    'REGISTERED_CONTRACT_CHANGED_RE_REGISTER_REQUIRED', 'REGISTERED_CONTRACT_FILES_REQUIRED'})


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def committed(result):
    return result.get('status')=='COMMITTED' or (result.get('status')=='DUPLICATE' and result.get('original_status')=='COMMITTED')


def task_in_source(task, primary):
    if re.search(r'(?<![A-Za-z0-9_-])'+re.escape(task)+r'(?![A-Za-z0-9_-])', primary):
        return True
    for match in re.finditer(r'(?<![A-Za-z0-9_-])([A-Za-z][A-Za-z0-9_-]*?)(\d+)\s*[～~—–至]\s*(?:\1)?(\d+)(?![A-Za-z0-9_-])', primary):
        prefix, start, end = match.groups()
        if len(start) == len(end) and 0 <= int(end)-int(start) <= 1000:
            if any(task == prefix+str(n).zfill(len(start)) for n in range(int(start), int(end)+1)):
                return True
    return False


def requests(snapshot):
    result = {}
    for event in snapshot['events']:
        payload = event['payload']
        if event['kind'] == 'REQUIREMENT_PREPARATION_REQUESTED':
            result[payload['request_id']] = dict(payload, event_id=event['event_id'], state='QUEUED')
        elif event['kind'] in ('REQUIREMENT_PREPARATION_STAGED', 'REQUIREMENT_PREPARATION_COMPLETED',
                               'REQUIREMENT_PREPARATION_ACCEPTED', 'REQUIREMENT_PREPARATION_ACTIVATED') and payload['request_id'] in result:
            item = result[payload['request_id']]
            item.update(state={'REQUIREMENT_PREPARATION_STAGED':'STAGED',
                'REQUIREMENT_PREPARATION_COMPLETED':'PREPARED',
                'REQUIREMENT_PREPARATION_ACCEPTED':'ACCEPTED',
                'REQUIREMENT_PREPARATION_ACTIVATED':'ACTIVATED'}[event['kind']])
            item[event['kind']] = dict(payload, event_id=event['event_id'])
    return result


def _request(ledger, request_id):
    item = requests(ledger.snapshot()).get(request_id)
    if not item:
        raise ValueError('REQUIREMENT_PREPARATION_NOT_FOUND')
    return item


def _paused(db, ledger, item):
    from business_ledger import _deny
    run = ledger._one(db, 'runs', 'run_id', item['target']['run_id'])
    if (run['control'] != 'PAUSED' or run['plan_version'] is not None
            or db.execute('SELECT 1 FROM works WHERE run_id=?', (run['run_id'],)).fetchone()
            or db.execute("SELECT 1 FROM events WHERE run_id=? AND kind='REQUIREMENT_ACCEPTED'", (run['run_id'],)).fetchone()):
        _deny('PREPARATION_REQUIRES_UNSTARTED_PAUSED_RUN')
    latest = db.execute("SELECT event_id FROM events WHERE run_id=? AND kind='REQUIREMENT_PACKAGE_REGISTERED' ORDER BY event_id DESC LIMIT 1", (run['run_id'],)).fetchone()
    if not latest or latest['event_id'] != item['registration_event_id']:
        _deny('PREPARATION_REGISTRATION_CHANGED')
    return run


def enqueue(ledger, target, provenance, *, command_id, rejection):
    """Called only after native intake validated source, paths and task evidence."""
    from workflow_registration import packages, freeze_sources
    from business_ledger import _deny
    if rejection not in PREPARABLE:
        raise ValueError(rejection)
    registered = packages(ledger).get(target['requirement_id'])
    if (not registered or not registered.get('prepared_run') or registered['kind'] != 'development'
            or registered['requirement_root'] != str(Path(target['requirement_root']).resolve())
            or registered['repo'] != str(Path(target['repo']).resolve())
            or target.get('run_id', registered['run_id']) != registered['run_id']):
        raise ValueError('PREPARATION_REGISTERED_SCOPE_REQUIRED')
    primary = provenance['primary_text']
    if not re.search(r'合同|接纳|纳入|登记|冻结', primary):
        raise ValueError(rejection)
    if not all(task_in_source(task, primary) for task in target['task_ids']):
        raise ValueError('HISTORICAL_REQUIREMENT_NEEDS_EXPLICIT_CURRENT_TASK_SCOPE')
    exact = dict(target, run_id=registered['run_id'])
    exact['contract_files'] = list(dict.fromkeys(registered['contract_files']+target.get('contract_files', [])))
    sources = freeze_sources(ledger, registered['requirement_root'], exact['contract_files'])
    request_id = 'prepare-'+digest((provenance.get('chat_id','')+':'+provenance['message_id']+':'+target['requirement_id']).encode())[:32]
    item = {'request_id':request_id, 'target':exact, 'provenance':provenance, 'sources':sources,
        'registration_event_id':registered['registration_event_id'],
        'registered_task_ids':registered['task_ids'], 'editable_files':registered['contract_files'],
        'rejection':rejection}
    def operation(db):
        _paused(db, ledger, item)
        # A second message must not create a competing editor for the same run.
        for pending in requests(ledger.snapshot()).values():
            if pending['target']['run_id'] == exact['run_id'] and pending['state'] != 'ACTIVATED':
                _deny('REQUIREMENT_PREPARATION_ALREADY_QUEUED', request_id=pending['request_id'])
        event = ledger._event(db, 'REQUIREMENT_PREPARATION_REQUESTED', item, role='COORDINATOR')
        return {'event_id':event, 'request_id':request_id, 'run_id':exact['run_id'],
            'state':'QUEUED', 'requirement_accepted':False,
            'next_action':'后台prepare_requirement审阅原文与冻结提案，按CAS增补正式合同并重新登记；这张工作单不表示已接纳或已开工。'}
    return ledger._command('request_requirement_preparation', command_id+':prepare', [target, provenance], operation)


def _path(root, name):
    path = root / name
    if (not isinstance(name, str) or Path(name).is_absolute() or '..' in Path(name).parts
            or any(parent.is_symlink() for parent in (path, *path.parents) if parent != root.parent)
            or not path.resolve().is_relative_to(root) or not path.is_file()):
        raise ValueError('PREPARATION_FILE_OUTSIDE_FROZEN_SCOPE')
    return path


def _owner(ledger, identity):
    with ledger._connection() as db:
        work, _ = ledger._identity(db, identity)
        if work['role'] != 'COORDINATOR':
            raise ValueError('COORDINATOR_REQUIRED')


def prepare(ledger, identity, payload, *, command_id):
    """Stage an exact review, reconcile bounded edits, re-register, then accept."""
    from business_ledger import _deny, _json
    from workflow_registration import freeze_sources
    from workflow_intake import accept
    _owner(ledger, identity)
    item = _request(ledger, payload['request_id'])
    root = Path(item['target']['requirement_root'])
    tasks = payload['task_ids']
    if not set(item['target']['task_ids']) <= set(tasks) or len(set(tasks)) != len(tasks):
        raise ValueError('PREPARATION_MUST_PRESERVE_REQUESTED_TASK_SCOPE')
    # Additional provider tasks must be a new decomposition with an exact frozen
    # proposal citation. Historical task IDs cannot be silently reactivated.
    sources = {str(Path(s['path']).relative_to(root)):s for s in item['sources']}
    mappings = payload.get('additional_task_sources', {})
    extra = set(tasks)-set(item['target']['task_ids'])
    if not extra <= set(mappings) or set(mappings)-set(tasks) or extra & set(item['registered_task_ids']):
        raise ValueError('PREPARATION_NEW_TASK_SOURCE_MAPPING_REQUIRED')
    # Requested tasks already have native authority. Redundant citations are
    # welcome, but must pass exactly the same frozen-proposal check as provider
    # decompositions. Never ignore an invalid citation or read its live path.
    for task in mappings:
        citation = mappings[task]
        name = Path(citation['path'])
        if name.is_absolute():
            try:name = name.relative_to(root)
            except ValueError:raise ValueError('PREPARATION_TASK_NOT_GROUNDED_IN_FROZEN_PROPOSAL') from None
        source = sources.get(str(name)) if '..' not in name.parts else None
        if (not source or str(name) in item['editable_files'] or len(citation['quote']) < 8
                or citation['quote'] not in Path(source['frozen_path']).read_text()):
            raise ValueError('PREPARATION_TASK_NOT_GROUNDED_IN_FROZEN_PROPOSAL')
    changes=list(payload['changes'])
    # Keep the on-disk registration descriptor consistent with the ledger. This
    # deterministic metadata update preserves all existing fields and task IDs;
    # it is included in the same frozen intent as the reviewed document edits.
    if 'REGISTRATION.json' in item['editable_files']:
        existing=json.loads(Path(sources['REGISTRATION.json']['frozen_path']).read_text())
        expected=dict(existing,task_ids=list(dict.fromkeys(item['registered_task_ids']+tasks)),
                      contract_files=item['target']['contract_files'])
        supplied=next((c for c in changes if c['path']=='REGISTRATION.json'),None)
        if supplied:
            descriptor=json.loads(supplied['content'])
            if any(descriptor.get(key)!=value for key,value in expected.items()):
                raise ValueError('PREPARATION_REGISTRATION_DESCRIPTOR_MUST_PRESERVE_IDENTITY_AND_SCOPE')
        else:
            changes.append({'path':'REGISTRATION.json','sha256':sources['REGISTRATION.json']['sha256'],
                'content':json.dumps(expected,ensure_ascii=False,indent=2)+'\n'})
    effects = [];prospective={}
    for change in changes:
        if change['path'] not in item['editable_files']:
            raise ValueError('PREPARATION_FILE_OUTSIDE_FROZEN_SCOPE')
        if change['sha256'] != sources[change['path']]['sha256']:
            raise ValueError('PREPARATION_REVIEW_MUST_BIND_FROZEN_SOURCE')
        path = _path(root, change['path'])
        if ('content' in change)==('append' in change):
            raise ValueError('PREPARATION_EXACT_CONTENT_OR_APPEND_REQUIRED')
        raw=(change['content'].encode() if 'content' in change else
             Path(sources[change['path']]['frozen_path']).read_bytes()+change['append'].encode())
        prospective[change['path']]=raw.decode()
        if len(raw) > 1024*1024:
            raise ValueError('PREPARATION_FILE_TOO_LARGE')
        frozen = ledger.path.parent/'requirements'/digest(raw)
        frozen.parent.mkdir(exist_ok=True)
        if not frozen.exists():
            try:
                with frozen.open('xb') as stream:stream.write(raw)
                frozen.chmod(0o400)
            except FileExistsError:pass
        if frozen.read_bytes() != raw:
            raise ValueError('FROZEN_REQUIREMENT_SOURCE_CHANGED')
        effects.append({'path':change['path'], 'before':change['sha256'], 'after':digest(raw),
            'frozen_path':str(frozen)})
    if len({e['path'] for e in effects}) != len(effects):
        raise ValueError('PREPARATION_DUPLICATE_FILE')
    # Validate the prospective formal contract before committing any file effect.
    texts='\n'.join(prospective.get(name, _path(root,name).read_text()) for name in item['editable_files'])
    if any(not task_in_source(task,texts) for task in tasks):
        raise ValueError('PREPARATION_TASKS_MUST_BE_IN_FORMAL_CONTRACT')
    from workflow_query import _artifact
    review_artifact=_artifact(ledger,'requirement-preparation-review-'+item['request_id'],payload)
    def stage(db):
        ledger._identity(db, identity)
        _paused(db, ledger, item)
        if requests(ledger.snapshot())[item['request_id']]['state'] != 'QUEUED':
            _deny('PREPARATION_ALREADY_STAGED_USE_ORIGINAL_REVIEW')
        for effect in effects:
            if digest(_path(root, effect['path']).read_bytes()) != effect['before']:
                _deny('PREPARATION_FILE_CHANGED')
        event = ledger._event(db, 'REQUIREMENT_PREPARATION_STAGED', {
            'request_id':item['request_id'], 'effects':effects, 'review':payload,'review_artifact':review_artifact},
            role='COORDINATOR', work_id=identity['work_id'])
        return {'event_id':event}
    stable = item['request_id']+':review:'+digest(command_id.encode())[:16]
    journal=item.get('REQUIREMENT_PREPARATION_STAGED')
    if journal:
        if journal['review']!=payload or journal['effects']!=effects:
            raise ValueError('PREPARATION_REVIEW_IS_FROZEN_USE_ORIGINAL_REVIEW')
        staged={'status':'DUPLICATE','original_status':'COMMITTED','event_id':journal['event_id']}
    else:
        staged=ledger._command('stage_requirement_preparation',stable,[item['request_id'],payload],stage)
    if not committed(staged):return staged
    def apply(db):
        ledger._identity(db, identity)
        _paused(db, ledger, item)
        # Preflight the entire set before the first write. A crash can leave a
        # subset at AFTER; the committed stage journal makes that retry explicit.
        for source in item['sources']:
            name = str(Path(source['path']).relative_to(root))
            effect = next((e for e in effects if e['path'] == name), None)
            allowed = {effect['before'],effect['after']} if effect else {source['sha256']}
            if digest(_path(root,name).read_bytes()) not in allowed:
                _deny('PREPARATION_FILE_CHANGED_RECONCILE_JOURNAL')
        for effect in effects:
            path = _path(root, effect['path'])
            if digest(path.read_bytes()) == effect['after']:continue
            mode = path.stat().st_mode & 0o777
            fd, temp = tempfile.mkstemp(prefix='.requirement-preparation-', dir=path.parent)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(Path(effect['frozen_path']).read_bytes());stream.flush();os.fsync(stream.fileno())
                os.chmod(temp, mode);os.replace(temp, path)
            finally:
                if os.path.exists(temp):os.unlink(temp)
        names = item['target']['contract_files']
        frozen = freeze_sources(ledger, root, names)
        texts = '\n'.join(Path(s['frozen_path']).read_text() for s in frozen
                          if str(Path(s['path']).relative_to(root)) in item['editable_files'])
        if any(not task_in_source(task,texts) for task in tasks):
            _deny('PREPARATION_TASKS_MUST_BE_IN_FORMAL_CONTRACT')
        from workflow_registration import packages
        registered = dict(packages(ledger)[item['target']['requirement_id']])
        for key in ('registration_event_id','registered_at','sources','run_control','plan_version'):
            registered.pop(key,None)
        registered.update(task_ids=list(dict.fromkeys(item['registered_task_ids']+tasks)), contract_files=names)
        registration = ledger._event(db,'REQUIREMENT_PACKAGE_REGISTERED',{'package':registered,
            'sources':frozen, 'provenance':{'purpose':'registration_only', 'preparation_request_id':item['request_id'],
                'primary_text':payload['summary']}, 'development_permission':False,'production_permission':False},
            run_id=item['target']['run_id'], role='COORDINATOR')
        event = ledger._event(db,'REQUIREMENT_PREPARATION_COMPLETED',{'request_id':item['request_id'],
            'task_ids':tasks,'registration_event_id':registration,'review_event_id':staged['event_id']},
            role='COORDINATOR',work_id=identity['work_id'])
        return {'event_id':event,'registration_event_id':registration,'request_id':item['request_id']}
    completed=item.get('REQUIREMENT_PREPARATION_COMPLETED')
    if completed:
        result={'status':'DUPLICATE','original_status':'COMMITTED',**completed}
    else:
        result=ledger._command('apply_requirement_preparation',item['request_id']+':apply:'+digest(command_id.encode())[:16],
            [item['request_id'],staged['event_id']],apply)
    if not committed(result):return result
    target = dict(item['target'],task_ids=tasks)
    result['acceptance'] = accept(ledger,target,item['provenance'],command_id=item['request_id']+':accept',
        preparation_event_id=result['event_id'], select_after_accept=False)
    result['requirement_accepted'] = committed(result['acceptance'])
    result['state'] = 'ACCEPTED_AWAITING_SELECTION' if result['requirement_accepted'] else 'PREPARED'
    if result['requirement_accepted']:
        def record_accepted(db):
            ledger._identity(db, identity)
            event=ledger._event(db,'REQUIREMENT_PREPARATION_ACCEPTED',{
                'request_id':item['request_id'],'run_id':item['target']['run_id'],
                'acceptance_event_id':result['acceptance']['event_id']},role='COORDINATOR')
            return {'event_id':event}
        result['queue_receipt']=ledger._command('record_preparation_acceptance',item['request_id']+':accepted',
            [item['request_id'],result['acceptance']['event_id']],record_accepted)
    return result


def activate(ledger, identity, payload, *, command_id):
    from business_ledger import _deny
    _owner(ledger, identity)
    item = _request(ledger, payload['request_id'])
    target = item['target']['run_id']
    def operation(db):
        work, _ = ledger._identity(db, identity)
        if not db.execute("SELECT 1 FROM events WHERE run_id=? AND kind='REQUIREMENT_ACCEPTED'",(target,)).fetchone():
            _deny('REGISTERED_REQUIREMENT_NOT_ACCEPTED')
        # Selection uses the existing safe-drain protocol; no force clear,
        # duplicate run, or cross-run developer writer is permitted.
        current = ledger._authority(db)['current_run_id']
        if current != target and db.execute("SELECT 1 FROM works WHERE run_id=? AND role!='COORDINATOR' AND state!='ENDED'",(current,)).fetchone():
            _deny('REQUIREMENT_SWITCH_REQUIRES_DEVELOPER_SAFE_HANDOFF')
        result = ledger._select(db,target)
        event = ledger._event(db,'REQUIREMENT_PREPARATION_ACTIVATED',{
            'request_id':item['request_id'],'reason':payload['reason'],'run_id':target}, role='COORDINATOR')
        handoff = work['run_id'] != target
        return dict(result,event_id=event,request_id=item['request_id'],
            scope_handoff_required=handoff,
            next_action=({'action':'safe_release','payload':{'reason':'RUN_SWITCHED：保存必要检查点后释放旧run工作；原生新会话接续新run'}}
                if handoff else {'action':'publish_plan'}),
            continuation=('选中已成功，但当前原生会话仍绑定旧run。先保存必要检查点，再safe_release并结束本回合；'
                '等待旧writer退出与核验清零，原生新tick绑定目标run后才可publish_plan。不要在旧scope继续为新run查材料、派单或激活其他run。'
                if handoff else '当前原生scope已属于目标run；按冻结合同发布正式计划。'))
    return ledger._command('activate_prepared_requirement',command_id,[identity,payload],operation)


def next_actions(snapshot):
    result = []
    accepted = {e['run_id'] for e in snapshot['events'] if e['kind']=='REQUIREMENT_ACCEPTED'}
    for item in requests(snapshot).values():
        if item['state']=='ACTIVATED':continue
        action = 'activate_requirement' if item['state']=='ACCEPTED' or item['target']['run_id'] in accepted else 'prepare_requirement'
        result.append({'action':action, 'request_id':item['request_id'], 'state':item['state'],
            'requirement_root':item['target']['requirement_root'], 'run_id':item['target']['run_id'],
            'request_event_id':item['event_id'],
            'review_event_id':item.get('REQUIREMENT_PREPARATION_STAGED',{}).get('event_id'),
            'review_artifact':item.get('REQUIREMENT_PREPARATION_STAGED',{}).get('review_artifact'),
            'reason':'持久工作单：读取原始授权、冻结proposal与正式合同；prepare_requirement提交逐文件旧SHA和完整新正文、正式任务及来源映射。'
                '既有正文宜用changes[].append保留原文；元数据REGISTRATION.json自动与账本同步。STAGED重试须复用原review，不重新猜参数；接纳后activate_requirement按依赖选择并安全释放旧work，再publish_plan。'
                '本完整原生指令已授权合同准备，不依赖短答复B的历史选项关联，不为检索聊天日志暂停本请求。'
                '不开放shell；登记、接纳、派单须分别引用真实回执，不能宣称后台已执行未提交的动作。'})
    return result


def recover_native_request(pool_path, state_db, row_id, target, *, expected_primary_sha256):
    """Maintenance replay from the genuine stored native envelope, never a quote."""
    import sqlite3
    import workflow_runtime as workflow
    with sqlite3.connect('file:'+str(Path(state_db).resolve())+'?mode=ro', uri=True) as db:
        db.row_factory=sqlite3.Row
        row=db.execute('SELECT m.*,s.source,s.user_id,s.chat_id FROM messages m JOIN sessions s '
                       'ON m.session_id=s.id WHERE m.id=?',(row_id,)).fetchone()
    pool=json.loads(Path(pool_path).read_text())
    if (not row or row['role']!='user' or row['source']!='feishu'
            or row['chat_id']!=pool['roles']['COORDINATOR']['chat_id']
            or not row['platform_message_id'] or not row['user_id']):
        raise ValueError('NATIVE_REQUIREMENT_HISTORY_NOT_VERIFIED')
    prefix='[角色群只读/控制请求] 角色：COORDINATOR\n'
    marker='\n用户原文及附件/引用如下：\n'
    content=row['content']
    if not content.startswith(prefix) or content.count(marker)!=1:
        raise ValueError('NATIVE_PRIMARY_REQUIREMENT_ENVELOPE_REQUIRED')
    primary=content.split(marker,1)[1]
    if digest(primary.encode())!=expected_primary_sha256:
        raise ValueError('NATIVE_REQUIREMENT_HISTORY_HASH_CHANGED')
    provenance={'platform':'feishu','chat_id':row['chat_id'],'user_id':row['user_id'],
        'message_id':row['platform_message_id'],'primary_text':primary,'authenticated':True,
        'current_full':False,'role':'COORDINATOR','source_kind':'native_history_recovery',
        'recovery':{'message_db':str(Path(state_db).resolve()),'row_id':row_id,
            'session_id':row['session_id'],'original_at':row['timestamp'],
            'content_sha256':digest(content.encode())}}
    return workflow.submit_control(pool_path,'COORDINATOR','accept_requirement',target,provenance)
