"""Read-only current progress facts shared by scripts and model reports.

TODO is the sole business-state authority. Events describe recorded role activity;
OCR runtime state describes a review run, never task acceptance. Every fact keeps
its source and observation time; a source mtime is never an occurrence time.
"""
from collections import defaultdict
from copy import deepcopy
from datetime import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
TASK_PATTERN = r'(?:TASK|NC|MN)-\d+[A-Z]?(?:-[A-Z0-9]+)*'
TASK_RE = re.compile(r'^(' + TASK_PATTERN + r')(?:\s|$)')
ROLES = {'A':'1号', 'B':'2号', 'C':'3号', 'TESTER':'TESTER'}
TERMINAL = {'DONE', 'ACCEPTED', 'ACCEPT_MERGED', 'ACCEPTED_MERGED', 'CLOSED'}
PAUSED = {'PAUSED', 'DEFERRED', 'DEFERRED_AFTER_V04', 'CANCELLED', 'SUPERSEDED'}
DELIVERED = {'DELIVERED', 'DELIVERED_FROZEN', 'DELIVERED_REVIEW_PENDING', 'REVIEW_PENDING', 'NEEDS_REVIEW'}


def instant(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.astimezone(TZ) if parsed.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def minute(value):
    parsed = value if isinstance(value, datetime) else instant(value)
    return parsed.astimezone(TZ).strftime('%Y-%m-%d %H:%M +08:00') if parsed else None


def plain(value):
    return re.sub(r'[*`]', '', str(value or '')).strip()


def state_code(value):
    text = plain(value)
    match = re.match(r'^[^A-Za-z\u4e00-\u9fff]*([A-Z][A-Z0-9_]+)\b', text)
    if match:
        return match[1]
    for marker, state in (('✅','DONE'), ('🕓','DEFERRED'), ('🔁','REWORK'), ('🔧','RUNNING'),
                          ('⏸','READY'), ('⏳','WAITING'), ('🚫','BLOCKED')):
        if marker in text[:8]:
            return state
    return 'UNKNOWN'


def parse_task_rows(text):
    """Normalize both TODO table formats, retaining complete identities and conflicts.

    The returned mapping is also used by routing/observers. ``line`` is 1-based;
    conflicting duplicate states remain as ``variants`` and state CONFLICT.
    No acceptance is reconstructed from narrative phrases or event summaries.
    """
    tasks, headers, heading = {}, [], ''
    for number, line in enumerate(text.splitlines(), 1):
        if line.startswith('#'):
            heading = line.lstrip('# ').strip()
        if not line.lstrip().startswith('|'):
            continue
        cells = [c.strip() for c in re.split(r'(?<!\\)\|', line.strip().strip('|'))]
        key = plain(cells[0]) if cells else ''
        match = TASK_RE.match(key)
        if not match or len(cells) < 5:
            if cells and cells[0] in ('任务','Task','task'):
                headers = cells
            continue
        task = match[1]
        if len(cells) == 5:
            state_column = 1 if len(headers)==5 and headers[1] in ('状态','state','Status') else 4
            title = key[len(task):].strip() or re.sub(r'（\d{4}-\d{2}-\d{2}[^）]*）','',re.sub(r'^\d+[.、]\s*','',heading)) or '功能说明未登记'
            status, dependencies, progress = cells[state_column], cells[2], cells[3]
            owner_match = re.search(r'/\s*([^；;（(]+)', plain(status))
            owner = owner_match[1].strip() if owner_match else ''
            if state_column == 1:
                # The batch table has explicit owner prose under current actions.
                owner_match = re.search(r'\b([ABC])\s*唯一.*?owner', cells[4], re.I)
                owner = owner_match[1] if owner_match else owner
            state = state_code(status)
        else:
            title, status, dependencies, progress, owner = cells[2], cells[-2], cells[4], cells[-2], cells[-1]
            # The original table has a dedicated current-state marker column.
            state = state_code(cells[1])
            if state == 'UNKNOWN':
                state = state_code(status)
        row = {'task':task, 'title':plain(title) or task, 'owner':plain(owner), 'state':state,
               'progress':plain(progress), 'dependencies':plain(dependencies),
               'raw_state':plain(status), 'current_action':plain(cells[-1]),
               'line':number, 'cells':cells, 'variants':[]}
        if task in tasks:
            previous = tasks[task]
            previous['variants'].append({k:v for k,v in row.items() if k != 'variants'})
            if state != previous['state']:
                previous['state'] = 'CONFLICT'
        else:
            tasks[task] = row
    return tasks


class Sources:
    def __init__(self, root, at):
        self.root, self.at, self.entries, self.signatures = root, minute(at), {}, {}

    def read(self, path):
        path = Path(path).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError('progress source outside project')
        before = path.stat()
        body = path.read_text()
        after = path.stat()
        signature = (after.st_mtime_ns, after.st_size, after.st_ino)
        if (before.st_mtime_ns, before.st_size, before.st_ino) != signature:
            raise ValueError('progress source changed during read')
        source = str(path.relative_to(self.root))
        old = self.signatures.get(path)
        if old is not None and old != signature:
            raise ValueError('progress source changed between reads')
        self.signatures[path] = signature
        self.entries[source] = {'sha256':hashlib.sha256(body.encode()).hexdigest(),
                                'source_updated_at':minute(datetime.fromtimestamp(after.st_mtime, TZ)),
                                'observed_at':self.at}
        return body

    def json(self, path):
        value = json.loads(self.read(path))
        if not isinstance(value, dict):
            raise ValueError('invalid progress record')
        return value

    def fact(self, value, source, *, line=None, occurred_at=None, recorded_at=None, observed_at=None):
        return {'value':value, 'source':source, 'line':line, 'observed_at':minute(observed_at) or self.at,
                'occurred_at':minute(occurred_at), 'recorded_at':minute(recorded_at),
                'source_updated_at':self.entries.get(source, {}).get('source_updated_at')}

    def verify(self):
        for path, signature in self.signatures.items():
            stat = path.stat()
            if (stat.st_mtime_ns, stat.st_size, stat.st_ino) != signature:
                raise ValueError('progress source changed during collection')


def _owner_role(value):
    text = plain(value)
    if re.match(r'^[ABC](?:/[ABC])+',text):
        return None
    for role, markers in {'A':('1号','执行者 A','执行者A'), 'B':('2号','执行者 B','执行者B'),
                          'C':('3号','执行者 C','执行者C'), 'TESTER':('TESTER','测试者'),
                          'COORDINATOR':('协调者',)}.items():
        if any(marker in text for marker in markers) or re.match(r'^'+role+r'(?:\s|/|；|$)', text):
            return role
    return None


def _preamble(body):
    lines = body.splitlines()
    stop = next((n for n, line in enumerate(lines) if re.match(r'^#\s', line)), len(lines))
    return list(enumerate(lines[:stop], 1))


def _goals(sources, todo):
    current = []
    for line, text in _preamble(todo):
        match = re.search(r'当前执行决定[（(]([^）)]+)[）)][:：](.*?)(?:\*\*|$)', text)
        if match:
            current.append(sources.fact(plain(match[2]), 'TODO.md', line=line, occurred_at=match[1]))
    if not current:
        # The document title is an explicit goal label, not a synthesized business state.
        heading = next(((n,l.lstrip('# ').strip()) for n,l in enumerate(todo.splitlines(),1) if l.startswith('# ')), None)
        return sources.fact(heading[1] if heading else '当前目标未单独记录', 'TODO.md', line=heading[0] if heading else None)
    return max(current, key=lambda row:row['occurred_at'] or '')


def _events(sources, conflicts):
    grouped, latest = defaultdict(list), {}
    for role in ('COORDINATOR', *ROLES):
        for folder in (sources.root/'events'/role, sources.root/'inbox/events'/role):
            for path in sorted(folder.glob('*.json')):
                row = sources.json(path)
                if row.get('role', role) != role:
                    raise ValueError('progress event role mismatch')
                row = {**row, 'source':str(path.relative_to(sources.root)), 'role':role}
                grouped[(role, str(row.get('event_id') or row['source']))].append(row)
    rows = []
    for (role, identity), variants in grouped.items():
        payloads = {json.dumps({k:v for k,v in row.items() if k not in ('source','source_mtime','recorded_at')},
                              sort_keys=True, ensure_ascii=False) for row in variants}
        if len(payloads) > 1:
            conflicts.append(sources.fact('相同事件 ID 的内容不一致，待核对', variants[0]['source'],occurred_at=variants[0].get('occurred_at')))
            conflicts[-1]['event_id'] = identity
            conflicts[-1]['role'] = role
            conflicts[-1]['task'] = variants[0].get('task')
            conflicts[-1]['variants'] = variants
            # No arbitrary winner for conflicting mirrors.
            rows.append({**variants[0], 'status':'CONFLICT', 'summary':'相同事件 ID 的内容不一致，待核对', 'variants':variants})
        else:
            rows.append(variants[0])
    for row in rows:
        occurred = instant(row.get('occurred_at'))
        observed = instant(row.get('observed_at'))
        recorded = instant(row.get('recorded_at'))
        # File timestamps order undated evidence only; they never establish occurrence.
        order = (occurred or observed or recorded or instant(sources.entries[row['source']]['source_updated_at'])).timestamp()
        old = latest.get(row['role'])
        if old is None or order > old[0]:
            latest[row['role']] = (order,row)
        elif order == old[0] and old[1].get('event_id') != row.get('event_id'):
            conflicts.append(sources.fact('同一时点存在多条角色动作，保留各来源待核对', row['source'], occurred_at=occurred))
            latest[row['role']] = (order,{**row, 'status':'CONFLICT', 'variants':[old[1],row]})
    return {role:value[1] for role,value in latest.items()}, rows


def _ocr(sources, tasks, conflicts):
    runs, seen = [], set()
    for index in sorted((sources.root/'inbox/control/runtime/ocr').glob('*.json')):
        pointer = sources.json(index)
        directory = Path(pointer.get('run',''))
        if not directory.is_absolute():
            directory = sources.root/directory
        directory = directory.resolve()
        if not directory.is_relative_to(sources.root):
            raise ValueError('OCR progress source outside project')
        if directory in seen:
            continue
        seen.add(directory)
        if not (directory/'spec.json').exists() or not (directory/'state.json').exists():
            conflicts.append(sources.fact('OCR 索引指向缺失的状态记录', str(index.relative_to(sources.root))))
            continue
        spec, state = sources.json(directory/'spec.json'), sources.json(directory/'state.json')
        task = spec.get('task')
        if task not in tasks or tasks[task]['state']['value'] in TERMINAL | PAUSED:
            continue
        if spec.get('run_id') and state.get('run_id') and spec['run_id'] != state['run_id']:
            conflicts.append(sources.fact('OCR 运行身份与状态不一致', str((directory/'state.json').relative_to(sources.root))))
            continue
        source = str((directory/'state.json').relative_to(sources.root))
        spec_source = str((directory/'spec.json').relative_to(sources.root))
        run = {'task':sources.fact(task,spec_source), 'head':sources.fact(spec.get('head'),spec_source),
                     'state':sources.fact(state.get('state','UNKNOWN'),source,occurred_at=state.get('updated_at')),
                     'batches':sources.fact([{k:b.get(k) for k in ('id','state','returncode','started_at','ended_at')} for b in state.get('batches',[])],source),
                     'gate_decision':sources.fact(state.get('gate_decision','NOT_EVALUATED'),source)}
        # Current task decision cards are bounded, exact-run evidence. Reading
        # them does not rewrite the runtime state or infer task acceptance.
        for decision in sorted((sources.root/'inbox/coordinator'/task).glob('*.md')):
            if not re.search(r'adjudication|decision',decision.name,re.I):
                continue
            body = sources.read(decision)
            if not spec.get('run_id') or spec['run_id'] not in body[:1200]:
                continue
            when = re.search(r'^裁决时间[：:]\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2} \+08:00)',body,re.M)
            if when:
                decision_source = str(decision.relative_to(sources.root))
                run['adjudication'] = sources.fact('RECORDED',decision_source,occurred_at=when[1])
                if state.get('state') == 'AWAITING_DECISION':
                    conflict = sources.fact('OCR 运行记录仍待裁决，但已有对应裁决文档',source,occurred_at=state.get('updated_at'))
                    conflict.update(task=task, decision=run['adjudication'])
                    conflicts.append(conflict)
        runs.append(run)
    declared = []
    for task, row in tasks.items():
        if row['state']['value'] in TERMINAL | PAUSED:
            continue
        text = row['details']['value']
        for clause in re.split(r'[；;。→\n]', text):
            if re.search(r'\bOCR\b|补审|审查遗留', clause, re.I):
                declared.append({'task':row['task'], 'detail':sources.fact(clause.strip(), 'TODO.md',line=row['details']['line'])})
    for run in runs:
        if run['state']['value'] not in ('RUNNING','SUBMITTED'):
            continue
        blocked = [row for row in declared if row['task']['value']==run['task']['value']
                   and any(word in row['detail']['value'] for word in ('阻塞','版本门禁'))]
        if blocked:
            conflict = sources.fact('TODO 登记 OCR 受阻，但运行索引已有执行记录','TODO.md',line=blocked[0]['detail']['line'])
            conflict.update(task=run['task']['value'], runtime_claim=run['state'])
            conflicts.append(conflict)
    return {'runs':runs, 'declared':declared}


def _dispatches(sources, snapshot=None):
    """Current role activity can include a dispatch not yet entered in TODO."""
    found = dict((snapshot or {}).get('dispatches') or {})
    for role, folder in ROLES.items():
        if role in found:
            continue
        directory = sources.root/'inbox'/folder
        # Current role inbox cards are shallow. Read newest candidates until a
        # dispatch is identified, rather than recursively loading old attempts.
        paths = list(directory.glob('*.md')) + list(directory.glob('*/*.md'))
        for path in sorted(paths,key=lambda p:p.stat().st_mtime_ns,reverse=True):
            if re.search(r'ack',path.stem,re.I):
                continue
            body = sources.read(path)
            if re.search(r'dispatch|rework',path.stem,re.I) or re.search(r'^#.*(?:派单|返修)',body[:1800],re.M):
                status = re.search(r'^\s*-?\s*status\s*[:：]\s*(\w+)',body,re.M|re.I)
                found[role] = {'source':str(path.relative_to(sources.root)), 'status':status[1] if status else 'UNKNOWN',
                               'mtime':path.stat().st_mtime}
                break
    result = {}
    for role, row in found.items():
        source = row.get('source')
        if not source:
            continue
        path = sources.root/source
        body = sources.read(path)
        task = re.search(TASK_PATTERN,source+'\n'+body[:1800])
        result[role] = sources.fact({'task':task[0] if task else None, 'status':row.get('status','UNKNOWN'),
                                    'source_mtime':row.get('mtime',path.stat().st_mtime)},source)
    return result


def _strong_review(sources, snapshot, at):
    path = Path.home()/'.local/state/complex-task-reviews/ledger.sqlite3'
    source = str(path)
    try:
        if snapshot is not None and isinstance(snapshot.get('strong_review'),dict):
            result = snapshot['strong_review']
        else:
            script = Path.home()/'.codex/skills/complex-task-coordinator/scripts/strong_review.py'
            spec = importlib.util.spec_from_file_location('_progress_strong_review',script)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            result = module.Ledger(clock=at.timestamp).status(str(sources.root))
        return {key:sources.fact(value,source) for key,value in result.items() if key != 'project'}
    except (OSError, ValueError, KeyError, ImportError) as exc:
        return {'authorization':sources.fact('UNKNOWN',source), 'error':sources.fact(type(exc).__name__,source)}


def build_projection(project, at=None, snapshot=None):
    """Return one current fact packet; never write, ACK, call models, or alter gates."""
    root = Path(project).resolve()
    at = at or datetime.now(TZ)
    if at.tzinfo is None:
        raise ValueError('progress observation time requires timezone')
    sources = Sources(root,at)
    binding = sources.json(root/'ROLE-BINDINGS.json')
    if Path(binding.get('project_root','')).resolve() != root:
        raise ValueError('progress project binding mismatch')
    todo = sources.read(root/'TODO.md')
    parsed = parse_task_rows(todo)
    if not parsed:
        raise ValueError('unrecognized current task table')
    conflicts, tasks = [], {}
    role_tasks = defaultdict(list)
    for task, row in parsed.items():
        fact = lambda value:sources.fact(value,'TODO.md',line=row['line'])
        role, owner_source = _owner_role(row['owner']), 'TODO.md'
        if role == 'COORDINATOR' and row['state'] in DELIVERED and '机械核验' in row['owner']:
            # "Coordinator mechanical check passed" names a review actor in a
            # phase status, rather than reassigning the implementation owner.
            role = None
        # Current dispatch identity establishes role ownership when phase cards put
        # the coordinator's review status in their former owner column.
        assigned = []
        if row['state'] not in TERMINAL | PAUSED and role is None:
            for who, folder in ROLES.items():
                directory = root/'inbox'/folder/task
                for path in sorted(directory.glob('*.md')):
                    if re.search(r'dispatch|rework',path.stem,re.I):
                        assigned.append((who,path))
            if len({who for who,_ in assigned}) == 1:
                role, owner_path = assigned[0]
                sources.read(owner_path)
                owner_source = str(owner_path.relative_to(root))
            elif len({who for who,_ in assigned}) > 1:
                # A batch/aggregate may have several role dispatches. Retain each
                # assignment without pretending it is one implementation owner.
                for who in dict.fromkeys(who for who,_ in assigned):
                    role_tasks[who].append(task)
        if role:
            role_tasks[role].append(task)
        state = row['state']
        taskrow = {'task':fact(task), 'title':fact(row['title']), 'state':fact(state),
                   'owner':sources.fact(role or row['owner'] or 'NOT_RECORDED',owner_source,line=row['line'] if owner_source=='TODO.md' else None),
                   'details':fact(row['raw_state'] if state not in TERMINAL else state),
                   'deliverable':fact(row['progress'] if len(row['cells'])==5 else row['title']),
                   'current_action':fact(row['current_action'] if state not in TERMINAL else state),
                   'dependencies':fact(row['dependencies']),
                   'delivery':fact('ACCEPTED' if state in TERMINAL else 'DELIVERED' if state in DELIVERED else 'NOT_CONFIRMED'),
                   'testing':fact('RECORDED_PASSED' if re.search(r'(?:TESTER|独立验证|终验).*?PASS',row['raw_state']) else 'NOT_CONFIRMED'),
                   'merge':fact('NOT_MERGED' if re.search(r'未合入|尚未合入',row['raw_state']) else
                                'RECORDED_MERGED' if state in TERMINAL and re.search(r'已合入|合入\s*main|main\s*`?[0-9a-f]{7,}',row['raw_state']) else 'NOT_CONFIRMED')}
        if row['variants']:
            taskrow['variants'] = [fact(variant) for variant in row['variants']]
        if state == 'CONFLICT':
            conflicts.append(fact('TODO 同一任务有不同业务状态，待核对'))
            conflicts[-1]['task'] = task
        tasks[task] = taskrow
    latest, events = _events(sources,conflicts)
    for event in events:
        task = event.get('task')
        # Role DONE can mean only that role's assigned sub-work is finished.
        # Only an explicit business-state field competes with TODO business state.
        business_state = event.get('business_state')
        if task in tasks and business_state and business_state != tasks[task]['state']['value']:
            conflicts.append(sources.fact('事件的业务状态与 TODO 不一致',event['source'],occurred_at=event.get('occurred_at')))
            conflicts[-1]['task'] = task
            conflicts[-1]['todo_source'] = tasks[task]['state']
            conflicts[-1]['event_claim'] = business_state
    roles = {}
    dispatches = _dispatches(sources,snapshot)
    for role in ('COORDINATOR', *ROLES):
        event = latest.get(role,{})
        def event_fact(value):
            return sources.fact(value,event.get('source','ROLE-BINDINGS.json'),occurred_at=event.get('occurred_at'),
                                recorded_at=event.get('recorded_at'),observed_at=event.get('observed_at'))
        roles[role] = {'tasks':sources.fact(list(dict.fromkeys(task for task in role_tasks[role] if tasks[task]['state']['value'] not in TERMINAL | PAUSED)),'TODO.md'),
                       'activity':event_fact(event.get('status','NOT_RECORDED')),
                       'activity_task':event_fact(event.get('task')),
                       'last_action':event_fact(event.get('summary','未记录最近动作')),
                       'next':event_fact(event.get('next'))}
        # Completed work remains a role's result after leaving the active queue.
        # The event identifies the work; TODO alone establishes its completion.
        recent_task = event.get('task')
        completed = [recent_task] if recent_task in tasks and tasks[recent_task]['state']['value'] in TERMINAL else []
        roles[role]['recent_completed'] = sources.fact(completed,'TODO.md',
                                     line=tasks[recent_task]['state']['line'] if completed else None)
        if event.get('variants'):
            roles[role]['variants'] = event_fact(event['variants'])
        if role in dispatches:
            dispatch = dispatches[role]
            activity_at = instant(event.get('occurred_at')) or instant(event.get('observed_at'))
            dispatch['value']['newer_than_activity'] = not activity_at or dispatch['value']['source_mtime'] > activity_at.timestamp()
            dispatch_task = dispatch['value'].get('task')
            if (dispatch_task in tasks and tasks[dispatch_task]['state']['value'] in TERMINAL
                    and dispatch['value']['source_mtime'] <= sources.signatures[root/'TODO.md'][0]/1_000_000_000):
                # Current TODO acceptance supersedes its existing dispatch card;
                # the card's file time remains metadata, not acceptance time.
                dispatch['value']['newer_than_activity'] = False
            roles[role]['dispatch'] = dispatch
    context = []
    if (root/'HANDOFF.md').exists():
        for line, text in _preamble(sources.read(root/'HANDOFF.md')):
            if text.strip():
                context.append(sources.fact(text,'HANDOFF.md',line=line))
    ocr = _ocr(sources,tasks,conflicts)
    open_ids = [task for task,row in tasks.items() if row['state']['value'] not in TERMINAL | PAUSED]
    current_ids = {event.get('event_id') for event in latest.values()}
    current_conflicts = [row for row in conflicts if not row.get('event_id') or row['event_id'] in current_ids]
    risks = list(current_conflicts)
    for task in open_ids:
        row = tasks[task]
        text = row['details']['value']
        for clause in re.split(r'[；;。→\n]',text):
            if any(word in clause for word in ('阻塞','仍缺','待用户','等待用户','门禁关闭','未覆盖')):
                risks.append(sources.fact(clause.strip(),'TODO.md',line=row['details']['line']))
    packet = {'schema_version':1, 'kind':'current-progress', 'project':str(root), 'observed_at':minute(at),
              'goal':_goals(sources,todo), 'control':sources.fact(binding.get('control','UNKNOWN'),'ROLE-BINDINGS.json'),
              'overall':{'current_tasks':sources.fact(open_ids,'TODO.md'),
                         'accepted_count':sources.fact(sum(row['state']['value'] in TERMINAL for row in tasks.values()),'TODO.md'),
                         'open_count':sources.fact(len(open_ids),'TODO.md'),
                         'paused_count':sources.fact(sum(row['state']['value'] in PAUSED for row in tasks.values()),'TODO.md')},
              'tasks':tasks, 'roles':roles, 'ocr':ocr, 'strong_review':_strong_review(sources,snapshot,at),
              'risks':risks, 'conflicts':conflicts, 'current_conflicts':current_conflicts,
              'current_handoff':context, 'sources':sources.entries}
    sources.verify()
    # Reads needed to establish recency may inspect older events. The model fact
    # packet carries only sources actually cited by current facts or retained
    # conflicts, rather than flooding its context with unrelated history.
    cited = set()
    def citations(value):
        if isinstance(value,dict):
            if isinstance(value.get('source'),str):
                cited.add(value['source'])
            for key,item in value.items():
                if key != 'sources':
                    citations(item)
        elif isinstance(value,list):
            for item in value:
                citations(item)
    citations(packet)
    packet['sources'] = {source:meta for source,meta in sources.entries.items() if source in cited}
    packet['fingerprint'] = hashlib.sha256(json.dumps(packet,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    return packet


def reporting_packet(packet, evidence_reference='evidence.json'):
    """Compact the same facts for both model input and script presentation.

    The caller saves ``packet`` as the independent evidence artifact. This pure
    transformation preserves every current task, stage, risk, OCR record and
    authorization time. Summaries identify exactly where their original lives.
    """
    result = deepcopy(packet)
    current = set(packet['overall']['current_tasks']['value'])
    for role in packet['roles'].values():
        task = role.get('activity_task',{}).get('value')
        if task in packet['tasks'] and packet['tasks'][task]['state']['value'] in TERMINAL:
            current.add(task)
        current.update(role.get('recent_completed',{}).get('value') or [])
    result['kind'] = 'current-progress-reporting'
    result['evidence_fingerprint'] = packet['fingerprint']
    result['evidence_reference'] = evidence_reference
    def pointer(*parts):
        return evidence_reference+'#/'+ '/'.join(str(part).replace('~','~0').replace('/','~1') for part in parts)
    def summary(fact, value, *parts):
        return {**deepcopy(fact), 'value':value, 'summary_basis':'structured business/activity fields; original text in evidence',
                'evidence_reference':pointer(*parts)}
    result['tasks'] = {}
    for task, original in packet['tasks'].items():
        if task not in current:
            continue
        row = deepcopy(original)
        business = original['state']['value']+'；测试='+original['testing']['value']+'；合入='+original['merge']['value']
        for field in ('details','current_action'):
            row[field] = summary(original[field],business,'tasks',task,field)
        if 'variants' in row:
            # Current contradictory TODO claims remain complete, including all
            # original state variants; compacting cannot resolve their conflict.
            row['variants'] = deepcopy(original['variants'])
        result['tasks'][task] = row
    for role, original in packet['roles'].items():
        activity = original['activity']['value']
        task = original.get('activity_task',{}).get('value')
        title = packet['tasks'].get(task,{}).get('title',{}).get('value')
        result['roles'][role]['last_action'] = summary(original['last_action'],
                    (title+'：' if title else '')+str(activity),'roles',role,'last_action')
        if original.get('next'):
            result['roles'][role]['next'] = summary(original['next'],None,'roles',role,'next')
    current_conflicts = deepcopy(packet.get('current_conflicts',packet['conflicts']))
    historical = [(n,row) for n,row in enumerate(packet['conflicts']) if row not in current_conflicts]
    result['conflicts'] = current_conflicts
    result['current_conflicts'] = current_conflicts
    result['historical_conflicts'] = {
        'count':{'value':len(historical), 'source':evidence_reference, 'observed_at':packet['observed_at'], 'occurred_at':None},
        'sources':[{key:deepcopy(value) for key,value in row.items() if key in
                    ('value','source','line','observed_at','occurred_at','recorded_at','event_id','role','task')}
                   | {'evidence_reference':pointer('conflicts',n)} for n,row in historical]}
    result['current_handoff'] = [{key:deepcopy(value) for key,value in row.items() if key != 'value'}
                               | {'evidence_reference':pointer('current_handoff',n)}
                               for n,row in enumerate(packet.get('current_handoff',[]))]
    result['fingerprint'] = hashlib.sha256(json.dumps({k:v for k,v in result.items() if k!='fingerprint'},
                                                   ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    return result
