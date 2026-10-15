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


def _condition_category(text):
    """Classify an explicit statement, never turn a keyword into a release gate.

    These are report annotations only. Unclear prose remains unknown, and neither
    a resolved entry prerequisite nor nonblocking follow-up establishes acceptance.
    """
    text = plain(text)
    gate = ('entry' if re.search(r'版本门禁|工具链|(?:OCR|审查)入口', text, re.I) else
            'acceptance' if re.search(r'接受门禁|合入门禁|发布门禁|生产门禁', text) else
            'coverage' if re.search(r'覆盖|验收资料|真实样本|时间映射', text) else 'unknown')
    negative = bool(re.search(r'不(?:再|会)?阻塞|(?:已)?无阻塞|没有阻塞|非阻塞|未(?:形成|构成|造成).{0,6}阻塞|不作为.{0,12}(?:前置|阻塞)|低优先.{0,12}候选', text))
    resolved = bool(re.search(r'(?:版本)?门禁(?:已)?解除|(?:阻塞|障碍)(?:已)?(?:解除|消除)|已解除(?:阻塞|门禁)', text))
    blocking = bool(re.search(r'(?<!不)阻塞|受阻|门禁.{0,12}(?:关闭|未开放)|仍缺|等待用户|待用户', text))
    # Mixed claims are not resolved by picking whichever keyword appeared first.
    if (negative or resolved) and re.search(r'(?:但|仍|同时).{0,30}(?<!不)(?:阻塞|受阻)|(?:但|仍|同时).{0,30}门禁.{0,10}关闭', text):
        category, value = 'unknown', None
    elif negative:
        category, value = 'nonblocking', False
    elif resolved:
        category, value = 'resolved', False
    elif re.match(r'^(?:此前|前次|历史|原运行|原run|旧运行)', text, re.I) or re.search(r'已替代|后续替代|历史遗留', text):
        category, value = 'history', False
    elif blocking:
        category, value = 'blocked', True
    elif re.search(r'\b(?:RUNNING|SUBMITTED)\b|在途|正在(?:补审|审查)', text):
        category, value = 'current_activity', False
    elif re.search(r'排队|待\s*OCR|串行|后续.{0,12}(?:审查|补审)', text, re.I):
        category, value = 'queued', False
    else:
        category, value = 'unknown', None
    queued = bool(re.search(r'排队|待\s*OCR',text,re.I))
    return {'category':category, 'is_blocking':value, 'gate_kind':gate,
            'queue_state':'QUEUED' if queued else 'NOT_SPECIFIED',
            'unresolved_current_claim':category=='unknown' and blocking}


def _same_commit(left, right):
    return bool(left and right and len(str(left))>=7 and len(str(right))>=7
                and (str(left).startswith(str(right)) or str(right).startswith(str(left))))


def _delivery_snapshots(sources, tasks, events, phase_started):
    """An observed delivery identifies a version, never accepts that delivery."""
    chosen = {}
    for event in events:
        task = event.get('task')
        when = instant(event.get('occurred_at')) or instant(event.get('observed_at')) or instant(event.get('recorded_at'))
        if (task not in tasks or event.get('role') not in ROLES or
                event.get('status') not in DELIVERED | {'REVIEW_PENDING','LEAF_ACCEPTED'} or
                not when or phase_started and when<phase_started):
            continue
        evidence = str(event.get('evidence') or '')
        summary = str(event.get('summary') or '')
        commit = re.search(r'\bcommit\s+`?([0-9a-f]{7,40})\b',evidence,re.I)
        if not commit:
            commit = re.search(r'(?:\bcommit|冻结(?:\s+head)?|\bhead)\s*[:：=]?\s*`?([0-9a-f]{7,40})\b',summary,re.I)
        if not commit:
            continue
        old = chosen.get(task)
        if old is None or when>old[0]:
            chosen[task] = (when,event,commit[1])
    for task,(_,event,commit) in chosen.items():
        tasks[task]['delivery_snapshot'] = sources.fact(
            {'commit':commit,'status':event['status'],'role':event['role']},event['source'],
            occurred_at=event.get('occurred_at'),recorded_at=event.get('recorded_at'),observed_at=event.get('observed_at'))


def _validation_records(sources, tasks, roles, events, phase_started):
    """Read the current tester task's bounded receipts, retaining exact coverage."""
    paths = set()
    tester_tasks = roles.get('TESTER',{}).get('tasks',{}).get('value',[])
    # Existing projects use TEST-TASK16D rather than TASK-16D for receipt folders.
    # Only directories matching a current tester responsibility are considered.
    for task in tester_tasks:
        key = re.sub(r'[-_]','',task)
        for folder in (sources.root/'test-receipts').glob('*'):
            normalized = re.sub(r'[-_]','',folder.name)
            if folder.is_dir() and normalized in (key,'TEST'+key):
                paths.update(folder.glob('*.md'))
    for event in events:
        when = instant(event.get('occurred_at')) or instant(event.get('observed_at')) or instant(event.get('recorded_at'))
        if event.get('role')!='TESTER' or event.get('task') not in tester_tasks or phase_started and (not when or when<phase_started):
            continue
        for reference in re.findall(r'test-receipts/[^\s`;；，、）)]+\.md',str(event.get('evidence') or '')):
            path = (sources.root/reference).resolve()
            if path.is_relative_to(sources.root/'test-receipts') and path.is_file():
                paths.add(path)
    for path in sorted(paths):
        body = sources.read(path)
        source = str(path.relative_to(sources.root))
        headers = list(re.finditer(r'^##\s+(.+)$',body,re.M))
        for index,heading in enumerate(headers):
            section = body[heading.end():headers[index+1].start() if index+1<len(headers) else len(body)]
            tested = re.search(r'^被测[：:]\s*('+TASK_PATTERN+r')\s+`?([0-9a-f]{7,40})\b',section,re.M)
            if not tested or tested[1] not in tasks:
                continue
            # A PASS elsewhere in the receipt cannot stand in for this section's
            # conclusion. Tests marked RED may intentionally fail during proof.
            conclusion_lines = [line for line in section.splitlines() if re.search(r'结论[（(：:]|验证结论',line)]
            conclusion = 'PASS_RECORDED' if any(re.search(r'(?<![A-Z_])PASS(?![A-Z_])',line) for line in conclusion_lines) else 'NOT_CONFIRMED'
            scope = 'REWORK' if '返修' in heading[1] else 'INCREMENTAL' if '增量' in heading[1] else 'INTEGRATION' if '联合' in heading[1] or '集成' in heading[1] else 'UNKNOWN'
            task,commit = tested[1],tested[2]
            delivered = tasks[task].get('delivery_snapshot',{}).get('value',{}).get('commit')
            match = ('MATCH' if _same_commit(commit,delivered) else 'DIFFERENT') if delivered else 'UNKNOWN'
            # Uncertain historical times such as 13:0x remain unknown. The exact
            # source and section survive; mtime never fills the missing minute.
            stamp = re.search(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?\s*\+08:00',heading[1])
            fact = sources.fact({'commit':commit,'conclusion':conclusion,'scope':scope,
                                 'section':heading[1],'current_snapshot':match},source,
                                line=body.count('\n',0,heading.start())+1,
                                occurred_at=stamp[0] if stamp else None)
            tasks[task].setdefault('validation_records',[]).append(fact)


def _ocr_decisions(sources, task, directory, spec, all_run_ids):
    candidates = set()
    for folder in (sources.root/'inbox/coordinator'/task,directory,directory.parent):
        candidates.update(path for path in folder.glob('*.md')
                          if re.search(r'adjudication|decision|ruling|verdict',path.name,re.I)
                          and not re.search(r'acceptance|accept-decision',path.name,re.I))
    records = []
    run_id,head = str(spec.get('run_id') or ''),str(spec.get('head') or '')
    for path in sorted(candidates):
        body = sources.read(path)
        identity = None
        if run_id and re.search(r'(?<![A-Za-z0-9])'+re.escape(run_id)+r'(?![A-Za-z0-9])',body):
            identity = 'RUN_ID'
        elif len(run_id)>=8:
            short = run_id[:8]
            if (sum(candidate.startswith(short) for candidate in all_run_ids)==1 and
                    re.search(r'(?<![A-Za-z0-9])'+re.escape(short)+r'(?![A-Za-z0-9])',body)):
                identity = 'RUN_PREFIX'
        if not identity:
            run_folder = directory.parent if directory.name=='run' else directory
            # A run-local verdict can name its directory and abbreviated head.
            hashes = re.findall(r'(?<![0-9a-f])([0-9a-f]{7,40})(?![0-9a-f])',body)
            if (path.parent in (directory,directory.parent) and
                    run_folder.name in body and any(_same_commit(head,commit) for commit in hashes)):
                identity = 'RUN_DIRECTORY_AND_HEAD'
        if not identity:
            continue
        lines = body.splitlines()
        decision_lines = [(n,line) for n,line in enumerate(lines,1)
                          if re.search(r'门禁|裁决|返修|覆盖|最终',line)]
        scope = '\n'.join(line for _,line in decision_lines)
        final = bool(re.search(r'本\s*ruling.{0,35}最终合并裁决|本.{0,8}为最终.{0,8}裁决',body,re.I))
        followup = ('REWORK' if re.search(r'最小返修\s*[=＝]|最小返修[=＝]|最小返修=|回派.{0,15}最小返修',body) else
                    'REVIEW_REQUIRED' if re.search(r'最终\s*head.{0,35}增量\s*OCR|增量\s*OCR.{0,20}单文件',body,re.I) else
                    'NO_BLOCKING_RECORDED' if re.search(r'OCR\s*门禁满足',body,re.I) else 'UNCONFIRMED')
        explicit = re.search(r'^裁决时间[：:]\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2} \+08:00)',body,re.M)
        recorded = re.search(r'(?:记录|记录时间)[：:]\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2} \+08:00)',body)
        source = str(path.relative_to(sources.root))
        records.append(sources.fact({'run_id':run_id or None,'head':head or None,
                                     'record_type':'RULING' if 'ruling' in path.stem.lower() else
                                                   'VERDICT' if 'verdict' in path.stem.lower() else 'DECISION',
                                     'identity_basis':identity,'final_record':final,
                                     'followup_kind':followup,'scope':scope},source,
                                    line=decision_lines[0][0] if decision_lines else 1,
                                    occurred_at=explicit[1] if explicit else None,
                                    recorded_at=recorded[1] if recorded else None))
    return records


def _ocr(sources, tasks, conflicts):
    runs, seen, indexed = [], set(), []
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
        indexed.append((directory,spec,state))
    all_run_ids = [str(spec.get('run_id') or '') for _,spec,_ in indexed]
    for directory,spec,state in indexed:
        task = spec['task']
        source = str((directory/'state.json').relative_to(sources.root))
        spec_source = str((directory/'spec.json').relative_to(sources.root))
        run = {'task':sources.fact(task,spec_source), 'head':sources.fact(spec.get('head'),spec_source),
                     'state':sources.fact(state.get('state','UNKNOWN'),source,occurred_at=state.get('updated_at')),
                     'batches':sources.fact([{k:b.get(k) for k in ('id','state','returncode','started_at','ended_at')} for b in state.get('batches',[])],source),
                     'gate_decision':sources.fact(state.get('gate_decision','NOT_EVALUATED'),source)}
        # Current task decision cards are bounded, exact-run evidence. Reading
        # them does not rewrite the runtime state or infer task acceptance.
        records = _ocr_decisions(sources,task,directory,spec,all_run_ids)
        if records:
            run['adjudication_records'] = records
            finals = [record for record in records if record['value']['final_record']]
            # A declared final ruling may supersede intermediate records. Without
            # that explicit statement no file timestamp selects a semantic winner.
            selected = finals[0] if len(finals)==1 else records[0] if len(records)==1 else None
            # RECORDED establishes only that a decision document exists. Several
            # compatible records need not be treated as a conflict, nor does this
            # existence marker choose their business conclusion.
            run['adjudication'] = {**(selected or records[0]),'value':'RECORDED'}
            run['decision_selection'] = ('FINAL_RECORDED' if len(finals)==1 else
                                         'SINGLE_RECORD' if len(records)==1 else 'MULTIPLE_RECORDS_NO_SEMANTIC_SELECTION')
            # AWAITING_DECISION is the runner's terminal hand-off state. The
            # separate adjudication document is its intended next layer; both
            # records can coexist without manufacturing a state conflict.
        delivered = tasks[task].get('delivery_snapshot')
        if delivered:
            commit = delivered['value']['commit']
            run['current_snapshot'] = {**delivered,'value':'MATCH' if _same_commit(spec.get('head'),commit) else 'DIFFERENT'}
        runs.append(run)
    declared = []
    for task, row in tasks.items():
        if row['state']['value'] in TERMINAL | PAUSED:
            continue
        text = row['details']['value']
        for clause in re.split(r'[；;。→\n]', text):
            if re.search(r'(?<![A-Za-z])OCR(?![A-Za-z])|补审|审查遗留', clause, re.I):
                declared.append({'task':row['task'], 'detail':sources.fact(clause.strip(), 'TODO.md',line=row['details']['line']),
                                 **_condition_category(clause)})
    for run in runs:
        if run['state']['value'] not in ('RUNNING','SUBMITTED'):
            continue
        blocked = [row for row in declared if row['task']['value']==run['task']['value']
                   and row['is_blocking'] is True and row['gate_kind']=='entry']
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
                status = re.search(r'^\s*-?\s*(?:status|状态)\s*[:：]\s*([A-Z][A-Z_]+)',body,re.M|re.I)
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
        task_id = task[0] if task else None
        attempt = re.search(r'\battempt[-_ ]?(\d+)\b',path.stem+'\n'+body[:1800],re.I)
        attempt_id = 'attempt-'+attempt[1] if attempt else None
        ack_sources, other_attempts = [], []
        for ack_path in sorted(path.parent.glob('*ack*.md')):
            ack = sources.read(ack_path)
            ack_task = re.search(TASK_PATTERN, ack[:1800])
            ack_attempt = re.search(r'\battempt[-_ ]?(\d+)\b',ack_path.stem+'\n'+ack[:1800],re.I)
            ack_status = re.search(r'^\s*-?\s*status\s*[:：]\s*(\w+)',ack,re.M|re.I)
            accepted_ack = (ack_status[1].upper() in ('ACK','ACKED') if ack_status else
                            bool(re.search(r'^#.*\bACK(?:ED)?\b|^ACK\s',ack,re.M)))
            if (not ack_task or ack_task[0]!=task_id or not ack_attempt or
                    not accepted_ack):
                continue
            other_attempts.append('attempt-'+ack_attempt[1])
            # An ACK for the original card cannot acknowledge a later same-attempt
            # correction. Both the attempt and explicit dispatch reference must match.
            if attempt_id == 'attempt-'+ack_attempt[1] and source in ack:
                ack_sources.append(str(ack_path.relative_to(sources.root)))
        status_match = re.search(r'^\s*-?\s*(?:status|状态)\s*[:：]\s*([A-Z][A-Z_]+)',body,re.M)
        status = status_match[1] if status_match else row.get('status','UNKNOWN')
        closed = bool(re.search(r'^\s*-?\s*(?:status|状态)\s*[:：].{0,30}(?:已核销|已收口|已完成|闭环|CLOSED|SUPERSEDED)',body,re.M))
        issued_text = '\n'.join(body.splitlines()[:2])+'\n'+'\n'.join(
            line for line in body.splitlines() if re.match(r'^\s*-?\s*(?:issued_at|派单时间)\s*[:：]',line))
        issued = re.search(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?\s*(?:\+08:00|Z)',issued_text)
        result[role] = sources.fact({'task':task_id, 'attempt':attempt_id, 'status':status,
                                    'source_mtime':row.get('mtime',path.stat().st_mtime),
                                    'acknowledged':bool(ack_sources), 'ack_sources':ack_sources,
                                    'ack_evidence':[sources.fact({'task':task_id,'attempt':attempt_id,'dispatch':source},ack_source)
                                                    for ack_source in ack_sources],
                                    'known_ack_attempts':list(dict.fromkeys(other_attempts)),
                                    'explicit_work_order':status in ('PENDING','DISPATCHED','REWORK_DISPATCHED'),
                                    'closed':closed, 'issued_at':minute(issued[0]) if issued else None},source)
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
    goal = _goals(sources,todo)
    phase_started = instant(goal.get('occurred_at'))
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
    _delivery_snapshots(sources,tasks,events,phase_started)
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
        completed_events = {}
        candidates = [row for row in events if row.get('role')==role] if phase_started else [event]
        for candidate in candidates:
            candidate_task = candidate.get('task')
            when = instant(candidate.get('occurred_at')) or instant(candidate.get('observed_at')) or instant(candidate.get('recorded_at'))
            if (candidate_task not in tasks or tasks[candidate_task]['state']['value'] not in TERMINAL
                    or candidate.get('status')=='CONFLICT'
                    or phase_started and (not when or when<phase_started)):
                continue
            previous = completed_events.get(candidate_task)
            rank = when.timestamp() if when else float('-inf')
            if previous is None or rank>previous[0]:
                completed_events[candidate_task] = (rank,candidate)
        completed = sorted(completed_events,key=lambda task:(-completed_events[task][0],task))
        roles[role]['recent_completed'] = sources.fact(completed,'TODO.md',
                                     line=tasks[completed[0]]['state']['line'] if len(completed)==1 else None)
        roles[role]['completed_evidence'] = {
            task:sources.fact(candidate['status'],candidate['source'],occurred_at=candidate.get('occurred_at'),
                              observed_at=candidate.get('observed_at'),recorded_at=candidate.get('recorded_at'))
            for task,(_,candidate) in completed_events.items()}
        if event.get('variants'):
            roles[role]['variants'] = event_fact(event['variants'])
        if role in dispatches:
            dispatch = dispatches[role]
            activity_at = instant(event.get('occurred_at')) or instant(event.get('observed_at'))
            value = dispatch['value']
            dispatch_task, attempt = value.get('task'), value.get('attempt')
            task_state = tasks.get(dispatch_task,{}).get('state',{}).get('value')
            known_attempts = value.get('known_ack_attempts',[])
            issued_at = instant(value.get('issued_at'))
            # Retain the legacy boolean for callers, but it now means a proven
            # outstanding input. Editing a handled card does not produce a new order.
            pending, kind = False, 'UNCONFIRMED'
            if value.get('acknowledged'):
                kind = 'ACKNOWLEDGED'
            elif value.get('closed') or task_state in TERMINAL | PAUSED:
                kind = 'CLOSED_OR_INACTIVE'
            elif value.get('explicit_work_order'):
                numbers = [int(a.split('-')[-1]) for a in known_attempts]
                if attempt and numbers and int(attempt.split('-')[-1])>max(numbers):
                    pending, kind = True, 'NEW_ATTEMPT'
                elif value.get('status')=='REWORK_DISPATCHED' and issued_at and activity_at and issued_at>activity_at:
                    pending, kind = True, 'REWORK_PENDING'
                elif dispatch_task is None and value.get('status')=='PENDING':
                    pending, kind = True, 'UNIDENTIFIED_PENDING'
                elif dispatch_task not in tasks or task_state in ('READY','PLANNED','DISPATCHED'):
                    pending, kind = True, 'NEW_TASK_PENDING'
                else:
                    kind = 'EXISTING_ASSIGNMENT'
            value.update(newer_than_activity=pending, notification_kind=kind)
            roles[role]['dispatch'] = dispatch
        assigned = roles[role]['tasks']['value']
        activity_task = event.get('task')
        observing = (event.get('status') in ('OBSERVING','OBSERVE','OBSERVATION','OBSERVED') or
                     bool(re.search(r'只读(?:核实|观察|核查)|read.?only observation',str(event.get('summary') or ''),re.I)))
        observed = [activity_task] if (observing and activity_task in tasks and
                        tasks[activity_task]['state']['value'] not in TERMINAL | PAUSED) else []
        roles[role]['observed_tasks'] = event_fact(observed)
        roles[role]['activity_focus'] = event_fact({'task':activity_task,
                                    'kind':'OBSERVATION' if observed else 'RECORDED_ACTIVITY'})
        dispatch = roles[role].get('dispatch',{})
        dispatch_value = dispatch.get('value',{})
        if activity_task in assigned and not observed:
            primary, basis = activity_task, 'current_role_activity'
            primary_source = event.get('source','TODO.md')
        elif dispatch_value.get('acknowledged') and dispatch_value.get('task') in assigned:
            primary, basis = dispatch_value['task'], 'matched_dispatch_ack'
            primary_source = dispatch['source']
        elif observed and any(tasks[task].get('delivery_snapshot',{}).get('value',{}).get('role')==role for task in assigned):
            delivered = [task for task in assigned if tasks[task].get('delivery_snapshot',{}).get('value',{}).get('role')==role]
            delivered.sort(key=lambda task:tasks[task]['delivery_snapshot'].get('occurred_at') or '',reverse=True)
            primary, basis = delivered[0], 'current_phase_delivery_pending_acceptance'
            primary_source = tasks[primary]['delivery_snapshot']['source']
        elif len(assigned)==1:
            primary, basis, primary_source = assigned[0], 'sole_open_responsibility', 'TODO.md'
        else:
            primary, basis, primary_source = None, 'current_focus_not_confirmed', 'TODO.md'
        roles[role]['primary_task'] = sources.fact(primary,primary_source)
        roles[role]['primary_basis'] = sources.fact(basis,primary_source)
        roles[role]['related_tasks'] = sources.fact([task for task in assigned if task!=primary],'TODO.md')
    _validation_records(sources,tasks,roles,events,phase_started)
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
    conditions = []
    for task in open_ids:
        row = tasks[task]
        text = row['details']['value']
        for clause in re.split(r'[；;。→\n]',text):
            if (any(word in clause for word in ('阻塞','仍缺','待用户','等待用户','未覆盖'))
                    or re.search(r'门禁.{0,12}(?:关闭|未开放)',clause)):
                condition = sources.fact(clause.strip(),'TODO.md',line=row['details']['line'])
                condition.update(task=task, **_condition_category(clause))
                conditions.append(condition)
                if condition['is_blocking'] is True or condition['unresolved_current_claim']:
                    risks.append(condition)
    packet = {'schema_version':1, 'kind':'current-progress', 'project':str(root), 'observed_at':minute(at),
              'goal':goal, 'control':sources.fact(binding.get('control','UNKNOWN'),'ROLE-BINDINGS.json'),
              'overall':{'current_tasks':sources.fact(open_ids,'TODO.md'),
                         'accepted_count':sources.fact(sum(row['state']['value'] in TERMINAL for row in tasks.values()),'TODO.md'),
                         'open_count':sources.fact(len(open_ids),'TODO.md'),
                         'paused_count':sources.fact(sum(row['state']['value'] in PAUSED for row in tasks.values()),'TODO.md')},
              'tasks':tasks, 'roles':roles, 'ocr':ocr, 'strong_review':_strong_review(sources,snapshot,at),
              'risks':risks, 'conditions':conditions, 'conflicts':conflicts, 'current_conflicts':current_conflicts,
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
        primary = role.get('primary_task',{}).get('value')
        if primary in packet['tasks']:
            current.add(primary)
        current.update(task for task in role.get('related_tasks',{}).get('value',[]) if task in packet['tasks'])
        current.update(task for task in role.get('observed_tasks',{}).get('value',[]) if task in packet['tasks'])
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
