"""Durable, native MySQL follow-up requests for the existing TESTER role.

The profile uses a new networkless local MySQL 8.4 instance and a Unix socket.
It never accepts a database URL, Docker options or production credentials.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
from contextlib import contextmanager

from runtime_common import locked, save_json, read_json


def _context_sha256(context):
    return hashlib.sha256(json.dumps(context, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def _context_from_event(db, work):
    """Read immutable supplemental bindings, recovering only from trusted events."""
    from business_ledger import _deny, _row
    try:
        stored = db.execute('SELECT context,context_sha256 FROM work_contexts WHERE work_id=?',
                            (work['work_id'],)).fetchone()
    except sqlite3.OperationalError as exc:
        if 'no such table' not in str(exc).lower():
            raise
        stored = None

    events = [_row(row) for row in db.execute(
        "SELECT * FROM events WHERE run_id=? AND kind IN "
        "('SUPPLEMENTAL_VERIFICATION_REQUESTED','SUPPLEMENTAL_REVALIDATION_QUEUED') ORDER BY event_id",
        (work['run_id'],))]
    requests = [event for event in events if event['kind']=='SUPPLEMENTAL_VERIFICATION_REQUESTED'
                and event['payload'].get('work_id')==work['work_id']]
    repairs = [event for event in events if event['kind']=='SUPPLEMENTAL_REVALIDATION_QUEUED'
               and event['work_id']==work['work_id']]
    if len(requests)+len(repairs)!=1:
        _deny('SUPPLEMENTAL_CONTEXT_EVENT_MISSING_OR_AMBIGUOUS', unknown=True)

    if requests:
        event=requests[0];payload=event['payload']
        provenance=payload.get('provenance') or {}
        candidate=_row(db.execute('SELECT * FROM candidates WHERE candidate_id=?',(work.get('candidate_id'),)).fetchone())
        if (payload.get('profile')!='mysql_migration' or payload.get('candidate_id')!=work.get('candidate_id')
                or payload.get('task_id')!=work.get('task_id') or event['run_id']!=work.get('run_id')
                or payload.get('owner')!='TESTER' or payload.get('production_permission') is not False
                or provenance.get('authenticated') is not True or not provenance.get('message_id')
                or not provenance.get('user_id') or not provenance.get('chat_id')
                or not candidate or candidate['run_id']!=work.get('run_id')
                or candidate['task_id']!=work.get('task_id') or candidate['attempt']!=work.get('attempt')
                or candidate['head']!=payload.get('head')):
            _deny('SUPPLEMENTAL_CONTEXT_BINDING_MISMATCH', unknown=True)
        context={'schema_version':1,'profile':'mysql_migration','request_event_id':event['event_id'],
                 'origin_work_id':None,'origin_task_id':None,'supersedes':[],
                 'candidate_id':work['candidate_id'],'run_id':work['run_id'],'task_id':work['task_id'],
                 'attempt':work['attempt'],'head':payload.get('head')}
    else:
        event=repairs[0];payload=event['payload']
        origin=_row(db.execute('SELECT * FROM works WHERE work_id=?',(payload.get('origin_work_id'),)).fetchone())
        origin_events=[item for item in events if item['kind']=='SUPPLEMENTAL_VERIFICATION_REQUESTED'
                       and item['payload'].get('work_id')==payload.get('origin_work_id')]
        candidate=_row(db.execute('SELECT * FROM candidates WHERE candidate_id=?',(work.get('candidate_id'),)).fetchone())
        failures=payload.get('supersedes');exception_ids=payload.get('exception_event_ids',[])
        exception_valid=True
        if exception_ids:
            from workflow_verification_exceptions import revalidation_ids
            exception_events=[_row(r) for r in db.execute('SELECT * FROM events WHERE run_id=? ORDER BY event_id',(work['run_id'],))]
            previous_ids={w['work_id'] for w in [_row(r) for r in db.execute(
                "SELECT * FROM works WHERE run_id=? AND kind='supplemental_verification'",(work['run_id'],))]
                if w['work_id']==payload.get('origin_work_id') or any(e['kind']=='SUPPLEMENTAL_REVALIDATION_QUEUED'
                    and e['work_id']==w['work_id'] and e['payload'].get('origin_work_id')==payload.get('origin_work_id') for e in exception_events)}
            exception_valid=bool(candidate and isinstance(exception_ids,list) and len(exception_ids)<=32
                and all(type(e) is int for e in exception_ids) and len(set(exception_ids))==len(exception_ids)
                and set(exception_ids)<=set(revalidation_ids({'events':exception_events},previous_ids,candidate)))
        source_payload=origin_events[0]['payload'] if len(origin_events)==1 else {}
        source_provenance=source_payload.get('provenance') or {}
        if (not origin or len(origin_events)!=1 or event['run_id']!=work.get('run_id')
                or payload.get('work_id')!=work.get('work_id')
                or payload.get('profile') not in (None,'mysql_migration')
                or payload.get('candidate_id')!=work.get('candidate_id')
                or not candidate or candidate['run_id']!=work.get('run_id')
                or candidate['task_id']!=work.get('task_id') or candidate['attempt']!=work.get('attempt')
                or candidate['head']!=payload.get('head')
                or origin['run_id']!=work.get('run_id') or origin['task_id']!='MN-01'
                or origin['candidate_id']!=origin_events[0]['payload'].get('candidate_id')
                or payload.get('original_request_event_id')!=origin_events[0]['event_id']
                or source_payload.get('profile')!='mysql_migration'
                or source_payload.get('owner')!='TESTER' or source_payload.get('production_permission') is not False
                or source_provenance.get('authenticated') is not True
                or not source_provenance.get('message_id') or not source_provenance.get('user_id')
                or not source_provenance.get('chat_id')
                or not exception_valid or not isinstance(failures,list) or (not failures and not exception_ids)
                or any(not isinstance(value,str) or not value for value in failures)
                or len(set(failures))!=len(failures)):
            _deny('SUPPLEMENTAL_CONTEXT_BINDING_MISMATCH', unknown=True)
        request=origin_events[0]
        if request['payload'].get('profile')!='mysql_migration':
            _deny('SUPPLEMENTAL_CONTEXT_ORIGIN_PROFILE_MISMATCH', unknown=True)
        context={'schema_version':1,'profile':'mysql_migration','request_event_id':request['event_id'],
                 'origin_work_id':origin['work_id'],'origin_task_id':origin['task_id'],
                 'supersedes':failures,'candidate_id':work['candidate_id'],'run_id':work['run_id'],
                 'task_id':work['task_id'],'attempt':work['attempt'],'head':payload.get('head')}
        if exception_ids:context['exception_event_ids']=exception_ids

    if stored:
        saved=json.loads(stored['context'])
        if (not isinstance(saved,dict) or saved.get('schema_version')!=1
                or _context_sha256(saved)!=stored['context_sha256']):
            _deny('SUPPLEMENTAL_CONTEXT_INTEGRITY_FAILURE', unknown=True)
        if saved!=context:
            _deny('SUPPLEMENTAL_CONTEXT_EVENT_MISMATCH', unknown=True)
    return context


def _save_context(db, work_id, context, clock):
    from business_ledger import _json
    if not isinstance(context, dict) or context.get('profile') != 'mysql_migration':
        raise ValueError('INVALID_SUPPLEMENTAL_CONTEXT')
    digest = _context_sha256(context)
    db.execute('INSERT INTO work_contexts(work_id,context,context_sha256,created_at) VALUES(?,?,?,?)',
               (work_id, _json(context), digest, clock()))


def get_context(ledger, work):
    with ledger._connection() as db:
        return _context_from_event(db, work)


def source_task(text):
    from manual_access import full_permission_action
    if not isinstance(text, str):
        return None
    text=full_permission_action(text) or text.strip()
    match=re.fullmatch(r'([A-Za-z][A-Za-z0-9_-]{0,80})\s*迁移\s*(?:MySQL\s*)?双向补验归属\s*TESTER\s*隔离\s*MySQL\s*会话[。！!]?\s*',text)
    return match[1] if match else None


def request(ledger, target, provenance, *, command_id):
    from business_ledger import _deny, _row, _id
    def operation(db):
        if (provenance.get('authenticated') is not True or not provenance.get('message_id')
                or not provenance.get('user_id') or not provenance.get('chat_id')):
            _deny('NATIVE_SUPPLEMENTAL_REQUEST_REQUIRED')
        task_id=source_task(provenance.get('primary_text'))
        if (not task_id or target.get('task_id')!=task_id or set(target)-{'run_id','task_id','profile'}
                or target.get('profile','mysql_migration')!='mysql_migration'):
            _deny('EXPLICIT_SUPPLEMENTAL_TASK_AND_PROFILE_REQUIRED')
        authority=ledger._authority(db)
        run_id=target.get('run_id') or authority['current_run_id']
        if authority['mode']!='ACTIVE' or run_id!=authority['current_run_id']:
            _deny('SUPPLEMENTAL_REQUEST_CURRENT_RUN_REQUIRED')
        task=_row(db.execute('SELECT * FROM tasks WHERE run_id=? AND task_id=?',(run_id,task_id)).fetchone())
        # This first supported profile runs the existing MN-01 migration test.
        # Other tasks/profiles need their own executable contract.
        if not task or task_id!='MN-01' or task['phase']!='COMPLETED':
            _deny('COMPLETED_MN01_TASK_REQUIRED')
        candidate=_row(db.execute("SELECT * FROM candidates WHERE run_id=? AND task_id=? AND attempt=? AND state='CURRENT'",
            (run_id,task_id,task['attempt'])).fetchone())
        if not candidate or not db.execute('SELECT 1 FROM merges WHERE candidate_id=?',(candidate['candidate_id'],)).fetchone():
            _deny('SUPPLEMENTAL_MERGED_CANDIDATE_REQUIRED')
        existing=_row(db.execute("SELECT * FROM works WHERE run_id=? AND candidate_id=? AND kind='supplemental_verification' ORDER BY rowid DESC LIMIT 1",
            (run_id,candidate['candidate_id'])).fetchone())
        if existing:
            context=_context_from_event(db,existing)
            return {'work_id':existing['work_id'],'state':existing['state'],'already_requested':True,
                    'request_event_id':context['request_event_id']}
        work={'work_id':_id('work')}
        # Queue without rebinding role_current: an existing TESTER writer keeps
        # its own work until normal release. Native admission binds this later.
        db.execute('INSERT INTO works(work_id,run_id,task_id,attempt,role,kind,state,candidate_id,created_at) '
            'VALUES(?,?,?,?,?,?,?,?,?)',(work['work_id'],run_id,task_id,task['attempt'],'TESTER',
            'supplemental_verification','QUEUED',candidate['candidate_id'],ledger.clock()))
        event=ledger._event(db,'SUPPLEMENTAL_VERIFICATION_REQUESTED',{
            'work_id':work['work_id'],'candidate_id':candidate['candidate_id'],'head':candidate['head'],
            'task_id':task_id,'profile':'mysql_migration','owner':'TESTER',
            'primary_text':provenance['primary_text'],'provenance':provenance,
            'production_permission':False},run_id=run_id,role='TESTER')
        from business_ledger import _json
        context={'schema_version':1,'profile':'mysql_migration','request_event_id':event,
            'origin_work_id':None,'origin_task_id':None,'supersedes':[],
            'candidate_id':candidate['candidate_id'],'run_id':run_id,'task_id':task_id,
            'attempt':task['attempt'],'head':candidate['head']}
        _save_context(db,work['work_id'],context,ledger.clock)
        checkpoint={'next':'调用role_test_command action=verify_supplemental，payload={}；受控入口提供临时MySQL 8.4与固定候选测试，不接受外部URL。'}
        db.execute('UPDATE works SET checkpoint=? WHERE work_id=?',(_json(checkpoint),work['work_id']))
        return {'work_id':work['work_id'],'state':'QUEUED','event_id':event,
                'candidate_id':candidate['candidate_id'],'owner':'TESTER','production_permission':False}
    return ledger._command('request_supplemental_verification',command_id,[target,provenance],operation)


def queue_repairs(ledger):
    """Continue the original local verification on an audited merged repair.

    Only a descendant candidate whose declared scope changes the exact test
    can qualify. No branch tip, live writer or unrelated code is tested.
    """
    from business_ledger import _row, _json, _id, _Denied
    def operation(db):
        authority=ledger._authority(db);run_id=authority['current_run_id']
        if authority['mode']!='ACTIVE' or not authority['admission_open']:
            return {'queued':[]}
        snapshot=ledger.snapshot(run_id)
        # snapshot() opens another read connection only; this command has no
        # mutations before freezing the source rows.
        supplemental=[w for w in snapshot['works'] if w['run_id']==run_id and w['kind']=='supplemental_verification']
        contexts={}
        for work in supplemental:
            try:
                contexts[work['work_id']]=_context_from_event(db,work)
            except _Denied:
                contexts[work['work_id']]=None
        roots=[w for w in supplemental if contexts.get(w['work_id'])
               and not contexts[w['work_id']].get('origin_work_id')]
        queued=[]
        for origin in roots:
            previous=[w for w in supplemental if w['work_id']==origin['work_id']
                      or (contexts.get(w['work_id']) or {}).get('origin_work_id')==origin['work_id']]
            ids={w['candidate_id'] for w in previous}
            checks=[v for v in snapshot['verifications'] if v['candidate_id'] in ids and v['check_name']=='supplemental_mysql']
            closed={key for v in checks if v['result']=='PASS' for key in v['supersedes']}
            failures=[v['verification_id'] for v in checks if v['result']=='FAIL' and v['verification_id'] not in closed]
            from workflow_verification_exceptions import facts as exception_facts, revalidation_ids
            exceptions=[item for item in exception_facts(snapshot,run_id) if item['work_id'] in {w['work_id'] for w in previous}
                and item['state']=='REPAIR_QUEUED']
            if (not failures and not exceptions) or any(w['state']!='ENDED' or w['active_execution_id']
                    or any(e['work_id']==w['work_id'] and e['state']!='ENDED' for e in snapshot['executions']) for w in previous):
                continue
            source=ledger._one(db,'candidates','candidate_id',origin['candidate_id'])
            candidates=[c for c in snapshot['candidates'] if c['run_id']==run_id and c['state']=='CURRENT' and c['candidate_id'] not in ids
                        and any(m['candidate_id']==c['candidate_id'] for m in snapshot['merges'])]
            for candidate in reversed(candidates):
                exception_ids=revalidation_ids(snapshot,{w['work_id'] for w in previous},candidate)
                if not failures and not exception_ids:continue
                task=next(t for t in snapshot['tasks'] if t['run_id']==run_id and t['task_id']==candidate['task_id'])
                path='tests/test_mn01_migration.py'
                if task['phase']!='COMPLETED' or path not in task['scope']:
                    continue
                if subprocess.run(['git','-C',candidate['repo'],'merge-base','--is-ancestor',source['head'],candidate['head']],
                                  capture_output=True,timeout=10).returncode:
                    continue
                if not subprocess.check_output(['git','-C',candidate['repo'],'diff','--name-only',source['head'],candidate['head'],'--',path],timeout=10):
                    continue
                identifier=_id('work')
                db.execute('INSERT INTO works(work_id,run_id,task_id,attempt,role,kind,state,candidate_id,checkpoint,created_at) '
                    "VALUES(?,?,?,?,'TESTER','supplemental_verification','QUEUED',?,'{}',?)",
                    (identifier,run_id,candidate['task_id'],candidate['attempt'],candidate['candidate_id'],ledger.clock()))
                context={'schema_version':1,'profile':'mysql_migration',
                         'request_event_id':contexts[origin['work_id']]['request_event_id'],
                         'origin_work_id':origin['work_id'],'origin_task_id':origin['task_id'],
                         'supersedes':failures,'candidate_id':candidate['candidate_id'],
                         'run_id':run_id,'task_id':candidate['task_id'],'attempt':candidate['attempt'],
                         'head':candidate['head']}
                if exception_ids:context['exception_event_ids']=exception_ids
                _save_context(db,identifier,context,ledger.clock)
                event=ledger._event(db,'SUPPLEMENTAL_REVALIDATION_QUEUED',{
                    'work_id':identifier,'origin_work_id':origin['work_id'],
                    'original_request_event_id':context['request_event_id'],
                    'candidate_id':candidate['candidate_id'],'head':candidate['head'],'supersedes':failures,
                    **({'exception_event_ids':exception_ids} if exception_ids else {}),
                    'reason':'原隔离补验范围内、已合入的后代候选修复固定测试，自动重验；不需要用户重授'},run_id=run_id,role='TESTER',work_id=identifier)
                queued.append({'work_id':identifier,'event_id':event});break
        return {'queued':queued}
    snapshot=ledger.snapshot()
    key=hashlib.sha256(json.dumps([(w['work_id'],w['state']) for w in snapshot['works']]+[
        (c['candidate_id'],c['state']) for c in snapshot['candidates']]+[
        (t['task_id'],t['phase']) for t in snapshot['tasks']]+[
        v['verification_id'] for v in snapshot['verifications']],sort_keys=True).encode()).hexdigest()
    # Avoid writing no-op audit commands on every cron tick.
    if not any(w['kind']=='supplemental_verification' for w in snapshot['works']):
        return {'queued':[]}
    return ledger._command('queue_supplemental_repair','supplemental-repair:'+key,[],operation)


def permits_closure(db, work, prior, earlier, candidate):
    if work['kind']!='supplemental_verification' or prior['check_name']!='supplemental_mysql':
        return False
    context=_context_from_event(db,work)
    origin_id=context.get('origin_work_id')
    if not origin_id or prior['verification_id'] not in context.get('supersedes',[]):
        return False
    from business_ledger import _row
    origin=_row(db.execute('SELECT * FROM works WHERE work_id=?',(origin_id,)).fetchone())
    record=_row(db.execute("SELECT * FROM events WHERE work_id=? AND kind='SUPPLEMENTAL_REVALIDATION_QUEUED' ORDER BY event_id DESC LIMIT 1",
                           (work['work_id'],)).fetchone())
    if not record:
        return False
    proof=record['payload']
    return bool(origin and origin['run_id']==candidate['run_id'] and origin['task_id']=='MN-01'
                and context['request_event_id']==proof['original_request_event_id']
                and proof['candidate_id']==candidate['candidate_id'] and proof['head']==candidate['head']
                and proof['origin_work_id']==origin_id and prior['verification_id'] in proof['supersedes'])


def summary(snapshot, run_id):
    rows=[]
    contexts={c['work_id']:c['context'] for c in snapshot.get('work_contexts',[])}
    work_by_id={w['work_id']:w for w in snapshot.get('works',[])}
    candidate_by_id={c['candidate_id']:c for c in snapshot.get('candidates',[])}
    events_by_run={}
    for event in snapshot.get('events',[]):
        if event.get('run_id')==run_id:
            events_by_run.setdefault(event['run_id'],[]).append(event)
    for work in snapshot['works']:
        if work['run_id']!=run_id or work['kind']!='supplemental_verification':
            continue
        checks=[v for v in snapshot['verifications'] if v['candidate_id']==work['candidate_id']
                and v['check_name']=='supplemental_mysql']
        context=contexts.get(work['work_id'])
        work_events=events_by_run.get(run_id,[])
        requests=[e for e in work_events if e['kind']=='SUPPLEMENTAL_VERIFICATION_REQUESTED'
                  and e['payload'].get('work_id')==work['work_id']]
        repairs=[e for e in work_events if e['kind']=='SUPPLEMENTAL_REVALIDATION_QUEUED'
                 and e.get('work_id')==work['work_id']]
        derived=None
        if len(requests)+len(repairs)==1:
            if requests:
                event=requests[0]
                candidate=candidate_by_id.get(work['candidate_id'])
                if (event['payload'].get('candidate_id')==work['candidate_id'] and candidate
                        and event['payload'].get('profile')=='mysql_migration'
                        and candidate['head']==event['payload'].get('head')
                        and event['payload'].get('task_id')==work['task_id']
                        and event['payload'].get('owner')=='TESTER'
                        and event['payload'].get('production_permission') is False
                        and (event['payload'].get('provenance') or {}).get('authenticated') is True):
                    derived={'schema_version':1,'profile':event['payload'].get('profile'),
                             'request_event_id':event['event_id'],'origin_work_id':None,
                             'origin_task_id':None,'supersedes':[],'candidate_id':work['candidate_id'],
                             'run_id':work['run_id'],'task_id':work['task_id'],'attempt':work['attempt'],
                             'head':event['payload'].get('head')}
            else:
                event=repairs[0];origin=work_by_id.get(event['payload'].get('origin_work_id'))
                candidate=candidate_by_id.get(work['candidate_id'])
                if (origin and event['payload'].get('candidate_id')==work['candidate_id'] and candidate
                        and candidate['head']==event['payload'].get('head')
                        and event['payload'].get('profile') in (None,'mysql_migration')
                        and any(e['kind']=='SUPPLEMENTAL_VERIFICATION_REQUESTED'
                                and e['event_id']==event['payload'].get('original_request_event_id')
                                and e['payload'].get('work_id')==origin['work_id']
                                and e['payload'].get('profile')=='mysql_migration'
                                for e in work_events)):
                    derived={'schema_version':1,'profile':'mysql_migration',
                             'request_event_id':event['payload'].get('original_request_event_id'),
                             'origin_work_id':origin['work_id'],'origin_task_id':origin['task_id'],
                             'supersedes':event['payload'].get('supersedes',[]),
                             'candidate_id':work['candidate_id'],'run_id':work['run_id'],
                             'task_id':work['task_id'],'attempt':work['attempt'],
                             'head':event['payload'].get('head')}
                    if event['payload'].get('exception_event_ids'):derived['exception_event_ids']=event['payload']['exception_event_ids']
        context_state='VERIFIED' if derived else 'UNVERIFIED'
        if context is not None:
            if (not isinstance(context,dict) or context.get('schema_version')!=1
                    or _context_sha256(context)!=next((c['context_sha256'] for c in snapshot.get('work_contexts',[])
                                                        if c['work_id']==work['work_id']),None)
                    or context!=derived):
                context=None;context_state='MISMATCH'
        else:
            context=derived
        exceptions=[e for e in work_events if e['kind']=='VERIFICATION_EXCEPTION_RECORDED' and e['work_id']==work['work_id']]
        rows.append({key:work[key] for key in ('work_id','task_id','candidate_id','state')} |
                    {'owner':'TESTER','profile':(context or {}).get('profile'),
                     'origin_task_id':(context or {}).get('origin_task_id') or work['task_id'],
                     'supersedes':checks[-1]['supersedes'] if checks else (context or {}).get('supersedes',[]),
                     'request_event_id':(context or {}).get('request_event_id'),
                     'context_state':context_state,
                     'result':'UNVERIFIED' if exceptions else checks[-1]['result'] if checks else 'NOT_VERIFIED',
                     'exception_event_ids':[e['event_id'] for e in exceptions],
                     'next':'按补验结果处理真实缺陷；不得仍称待用户指定或把排队当作通过。'})
    return rows


def docker(*args, timeout=20):
    return subprocess.check_output(['docker',*args],stderr=subprocess.PIPE,timeout=timeout).decode().strip()


@contextmanager
def mysql_session(directory):
    directory=Path(directory).resolve();directory.mkdir(parents=True,exist_ok=True)
    sockets=directory/'socket';sockets.mkdir(exist_ok=True);sockets.chmod(0o777)
    socket=sockets/'mysql.sock'
    # MYSQL's socket path limit is small. Bind a short container path and use
    # the host path only in the SQLAlchemy URL supplied by this helper.
    name='role-mysql-'+hashlib.sha256(str(directory).encode()).hexdigest()[:24]
    marker=directory/'session.json'
    with locked(directory/'session.lock'):
        if marker.exists():
            old=read_json(marker)
            if old.get('state') not in ('CLOSED','START_FAILED'):
                raise ValueError('MYSQL_SESSION_REQUIRES_RECONCILIATION:'+str(marker))
        image=json.loads(docker('image','inspect','mysql:8.4'))[0]['Id']
        # A private disposable instance: no network, no published port, no
        # existing volume. Empty password applies solely inside this instance.
        command=['run','-d','--pull=never','--name',name,'--network=none',
            '--label','role-workflow=mysql-supplemental','--memory=1g','--pids-limit=256',
            '--tmpfs','/var/lib/mysql:rw,size=768m',
            '--mount','type=bind,src='+str(sockets)+',dst=/test-socket',
            '-e','MYSQL_ALLOW_EMPTY_PASSWORD=yes','-e','MYSQL_DATABASE=mn01_migration_test',
            image,'--socket=/test-socket/mysql.sock','--skip-networking','--mysqlx=0']
        save_json(marker,{'state':'STARTING','name':name,'image':image,'network':'none','database':'mn01_migration_test'})
        container=None
        try:
            container=docker(*command)
            save_json(marker,{**read_json(marker),'state':'STARTED','container_id':container})
            deadline=time.monotonic()+90
            while time.monotonic()<deadline:
                status=subprocess.run(['docker','exec',container,'mysqladmin','--protocol=socket',
                    '--socket=/test-socket/mysql.sock','-uroot','ping'],capture_output=True,timeout=8)
                log=subprocess.run(['docker','logs',container],capture_output=True,timeout=8)
                initialized=b'MySQL init process done. Ready for start up.' in log.stdout+log.stderr
                if status.returncode==0 and socket.exists() and initialized:
                    break
                time.sleep(1)
            else:
                raise ValueError('ISOLATED_MYSQL_START_TIMEOUT')
            actual=json.loads(docker('inspect',container))[0]
            if (actual['HostConfig']['NetworkMode']!='none' or actual['HostConfig'].get('PortBindings')
                    or actual['Config']['Image']!=image):
                raise ValueError('ISOLATED_MYSQL_IDENTITY_MISMATCH')
            version=docker('exec',container,'mysql','--protocol=socket','--socket=/test-socket/mysql.sock',
                          '-uroot','-Nse','SELECT VERSION()')
            if not version.startswith('8.4.'):
                raise ValueError('MYSQL_84_REQUIRED')
            save_json(marker,{**read_json(marker),'state':'READY','version':version})
            ready=directory/'ready.json';save_json(ready,read_json(marker));ready.chmod(0o444)
            yield 'mysql+pymysql://root@localhost/mn01_migration_test?unix_socket='+str(socket),ready
        finally:
            if container:
                with (directory/'mysql.log').open('wb') as log:
                    subprocess.run(['docker','logs',container],stdout=log,stderr=subprocess.STDOUT,timeout=10)
                docker('rm','-f','-v',container)
            else:
                # A timed-out docker run can still have created the named
                # resource. Preserve uncertainty for reconciliation.
                present=subprocess.run(['docker','inspect',name],capture_output=True,timeout=10)
                if present.returncode==0:
                    raise ValueError('MYSQL_START_SIDE_EFFECT_REQUIRES_RECONCILIATION:'+str(marker))
            save_json(marker,{**read_json(marker),'state':'CLOSED' if container else 'START_FAILED'})


def verify(ledger, identity, *, command_id):
    checked=ledger.validate_identity(identity)
    if not checked.get('valid'):
        return checked
    work=checked['work']
    if work['role']!='TESTER' or work['kind']!='supplemental_verification':
        raise ValueError('MYSQL_SUPPLEMENTAL_TESTER_WORK_REQUIRED')
    context=get_context(ledger,work)
    if (context.get('profile')!='mysql_migration' or context.get('candidate_id')!=work['candidate_id']
            or context.get('run_id')!=work['run_id'] or context.get('task_id')!=work['task_id']
            or context.get('attempt')!=work['attempt']):
        raise ValueError('MYSQL_SUPPLEMENTAL_CONTEXT_REQUIRED')
    directory=ledger.path.parent/'mysql-sessions'/hashlib.sha256((work['work_id']+command_id).encode()).hexdigest()[:16]
    from delivery_evidence import execute_candidate_tests
    # Let the existing runner pick the accepted repo's test interpreter. The
    # profile chooses the test, environment and check name, never the model.
    with mysql_session(directory) as (url,marker):
        result=execute_candidate_tests(ledger,identity,work['candidate_id'],
            ['pytest','-q','tests/test_mn01_migration.py::test_mysql_upgrade_downgrade_roundtrip_real'],
            command_id=command_id,timeout_seconds=300,report_format='pytest',
            supersedes=context.get('supersedes',[]),
            verification_profile='mysql_migration',environment_overrides={'MN01_MYSQL_URL':url},
            evidence={'inputs':[str(marker)]})
    return result
