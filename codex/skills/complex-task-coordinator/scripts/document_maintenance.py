"""Role-authorized archival; semantic documents require an explicit reviewed replacement."""
import hashlib
from pathlib import Path
import re
import uuid

from document_store import (_paths, _index, _recover, _blocks, _text_hash, _commit_rotation,
                            _remap_observations, _validate_records, _validate_rotation,
                            _preflight_rotation, atomic_text, rotate_document, sha)
from role_records import canonical, role_guard
from runtime_common import locked, now, read_json, save_json


def _normalized(text):
    return re.sub(r'\s+', ' ', text.replace('`', '').replace('**', '')).strip()


def _active_sections(text, tasks):
    lines = text.splitlines()
    headings = [(n, len(match[1]), match[2]) for n, line in enumerate(lines)
                if (match := re.match(r'^(#{1,6})\s+(.+)', line))]
    for index, (start, level, title) in enumerate(headings):
        if not any(re.search(r'(?<![A-Z0-9-])'+re.escape(task)+r'(?![A-Z0-9-])', title) for task in tasks):
            continue
        end = next((n for n, depth, _ in headings[index+1:] if depth <= level), len(lines))
        yield '\n'.join(lines[start:end])


def _verify_todo(before, replacement, obligations=None):
    from progress_projection import parse_task_rows, TERMINAL
    old, new = parse_task_rows(before), parse_task_rows(replacement)
    if not old:
        raise ValueError('unrecognized TODO; cannot verify nonterminal task preservation')
    active = {task: value for task, value in old.items() if value['state'] not in TERMINAL}
    obligations = {} if obligations is None else obligations
    if (not isinstance(obligations, dict) or not set(obligations) <= set(active)
            or any(not isinstance(items, list) or not items or len(items) > 100
                   or any(not isinstance(item, str) or not item.strip() or len(item.encode()) > 4096
                          for item in items) for items in obligations.values())):
        raise ValueError('semantic archive obligations must list reviewed current actions, open gates and recovery references per unfinished task')
    contexts = {task: _normalized(' '.join(' '.join(row['cells']) for row in [value, *value['variants']])
                                    +'\n'+'\n'.join(_active_sections(replacement, [task])))
                for task, value in new.items() if task in active}
    def require_obligations(task):
        items = obligations.get(task)
        if not items:
            raise ValueError('semantic archive must preserve task history unless explicit reviewed current obligations permit shortening: '+task)
        if any(_normalized(item) not in contexts.get(task, '') for item in items):
            raise ValueError('semantic archive must preserve reviewed current obligation in active task material: '+task)
    for task, value in active.items():
        if task not in new or new[task]['state'] != value['state']:
            raise ValueError('semantic archive must preserve unfinished task and state: '+task)
        old_rows, new_rows = [value, *value['variants']], [new[task], *new[task]['variants']]
        if len(old_rows) != len(new_rows):
            raise ValueError('semantic archive must preserve unfinished task variants: '+task)
        for original, current in zip(old_rows, new_rows):
            cells, newer = original['cells'], current['cells']
            # Seven-column tables have a dedicated state marker and a growing
            # narrative in the penultimate column. Five-column legacy tables
            # either have explicit progress or combine status/owner at the end.
            narrative = (len(cells)-2 if len(cells) > 5 else
                         3 if _normalized(original['raw_state']) == _normalized(cells[1]) else 4)
            if (len(cells) != len(newer) or original['owner'] != current['owner']
                    or original['title'] != current['title']
                    or any(_normalized(cell) != _normalized(newer[index])
                           for index, cell in enumerate(cells) if index != narrative)):
                raise ValueError('semantic archive must preserve unfinished owner, dependency, acceptance and actions: '+task)
            if _normalized(cells[narrative]) != _normalized(newer[narrative]):
                require_obligations(task)
    normalized = _normalized(replacement)
    for section in _active_sections(before, active):
        if _normalized(section) in normalized:
            continue
        title = section.splitlines()[0]
        for task in active:
            if re.search(r'(?<![A-Z0-9-])'+re.escape(task)+r'(?![A-Z0-9-])', title):
                require_obligations(task)
    for task in obligations:
        require_obligations(task)


def _verify_handoff(root, replacement):
    from context_input import pending_continuations
    from progress_projection import parse_task_rows, TERMINAL
    required = {'TODO.md'}
    coordinator = read_json(root/'inbox/control/runtime/checkpoints/COORDINATOR.json', {}) or {}
    if (coordinator.get('schema_version') != 1 or coordinator.get('role') != 'COORDINATOR'
            or not coordinator.get('task') or not coordinator.get('attempt')):
        raise ValueError('HANDOFF archive requires a verified current coordinator checkpoint')
    for role in ('COORDINATOR', 'A', 'B', 'C', 'TESTER'):
        path = root/f'inbox/control/runtime/checkpoints/{role}.json'
        if not path.exists(): continue
        current = read_json(path, {}) or {}
        if current.get('schema_version') != 1 or current.get('role') != role:
            raise ValueError('HANDOFF current role checkpoint identity invalid: '+role)
        required.add(str(path.relative_to(root)))
        required.update(str(current[key]) for key in ('task', 'attempt', 'dispatch') if current.get(key))
        evidence = current.get('evidence', [])
        required.update([evidence] if isinstance(evidence, str) else evidence)
        for continuation in pending_continuations(root, role):
            required.update(continuation[key] for key in ('source', 'continuation_id', 'task', 'attempt'))
    todo = root/'TODO.md'
    if not todo.is_file():
        raise ValueError('HANDOFF archive requires authoritative TODO')
    required.update(task for task, row in parse_task_rows(todo.read_text()).items() if row['state'] not in TERMINAL)
    for value in required:
        if not isinstance(value, str) or not re.search(r'(?<![A-Za-z0-9_.-])'+re.escape(value)+r'(?![A-Za-z0-9_.-])', replacement):
            raise ValueError('HANDOFF must preserve current obligation reference: '+str(value))


def replace_semantic_document(project, path, replacement, expected_sha256, *, obligations=None):
    root, source, relative, lock, journal = _paths(project, path)
    if relative not in ('TODO.md', 'HANDOFF.md'):
        raise ValueError('semantic replacement only supports TODO/HANDOFF')
    if relative != 'TODO.md' and obligations is not None:
        raise ValueError('reviewed per-task obligations apply only to TODO semantic history')
    with locked(lock), _index(root) as db:
        _recover(root, source, journal, db)
        before = source.read_bytes()
        if sha(before) != expected_sha256:
            raise ValueError('DOCUMENT_CONFLICT: source changed since semantic review')
        if not replacement.strip() or len(replacement.encode()) > 48*1024:
            raise ValueError('semantic active document must be nonempty and <=48KiB')
        blocks = _validate_records(root, relative, before.decode(), db)
        if relative == 'TODO.md':
            _verify_todo(before.decode(), replacement, obligations)
        else:
            _verify_handoff(root, replacement)
        generation = uuid.uuid4().hex
        folder = root/'archive/role-runtime'/sha(relative.encode())[:16]
        archive, after = folder/(generation+'.md'), folder/(generation+'.active.md')
        atomic_text(archive, before.decode()); atomic_text(after, replacement)
        records = []
        for marker, block in blocks.items():
            old = db.execute('SELECT location FROM records WHERE source=? AND marker=?', (relative, marker)).fetchone()
            if not old or old['location'] == 'active':
                records.append({'marker': marker, 'sha256': _text_hash(block)})
        row = dict(schema_version=1, source=relative, generation=generation,
                   archive=str(archive.relative_to(root)), replacement=str(after.relative_to(root)),
                   before_sha256=sha(before), after_sha256=sha(replacement.encode()), recorded_at=now(),
                   records=records,
                   retained=[item['marker'] for item in records if blocks[item['marker']].strip() in replacement],
                   semantic_obligations=obligations or {}, complete=False)
        _validate_rotation(root, source, row); _preflight_rotation(db, row)
        save_json(journal, row)
        if sha(source.read_bytes()) != row['before_sha256']:
            raise ValueError('DOCUMENT_CONFLICT: concurrent legacy writer during semantic archive')
        atomic_text(source, replacement)
        _commit_rotation(db, row); _remap_observations(root, row)
        row['complete'] = True; save_json(journal, row); save_json(folder/(generation+'.json'), row)
        return {'archived': True, 'source': relative, 'archive': row['archive'], 'bytes': len(replacement.encode())}


def archive_role_document(project, role, spec, token_file):
    if not isinstance(spec, dict) or not isinstance(spec.get('source'), str):
        raise ValueError('archive requires source')
    with role_guard(project, role, token_file) as (root, binding):
        source = canonical(root, spec['source'])
        own = binding['roles'][role]['session_file']
        if spec['source'] in ('TODO.md', 'HANDOFF.md'):
            required = {'source', 'replacement', 'expected_sha256'}
            if role != 'COORDINATOR' or not required <= set(spec) or set(spec)-required-{'obligations'}:
                raise ValueError('TODO/HANDOFF semantic archive requires coordinator and reviewed replacement')
            replacement = canonical(root, spec['replacement']).read_text()
            return replace_semantic_document(root, source, replacement, spec['expected_sha256'],
                                             obligations=spec.get('obligations'))
        if spec['source'] != own and not (role == 'COORDINATOR' and spec['source'] == 'TIMELINE.md'):
            raise ValueError('role may archive only its own canonical session; timeline is coordinator-only')
        if set(spec)-{'source', 'force'}:
            raise ValueError('unsupported archive fields')
        checkpoint = read_json(root/f'inbox/control/runtime/checkpoints/{role}.json', {})
        if not checkpoint:
            raise ValueError('publish a verified current role checkpoint before first legacy rotation')
        return rotate_document(root, source, checkpoint, force=spec.get('force') is True)
