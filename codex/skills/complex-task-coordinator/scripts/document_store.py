"""Bounded active role documents, immutable archives and recoverable rotations.

All cooperating append/rotate writers take the same stable document lock. The
SQLite marker index survives rotation; Markdown is a view, never the dedup ledger.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid

from runtime_common import locked, now, read_json, save_json

DEFAULT_POLICY = dict(trigger_bytes=64*1024, target_bytes=48*1024,
                      trigger_lines=400, keep_records=30)
MARKER = re.compile(r'(?m)^<!-- (?:role-record|role-event):[^\n]+ -->[ \t]*$')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _text_hash(body):
    return sha(body.strip().encode())


def _archive_path(root, relative, value):
    if not isinstance(value, str):
        raise ValueError('DOCUMENT_CONFLICT: invalid archive location')
    path = (root/value).resolve()
    folder = (root/'archive/role-runtime'/sha(relative.encode())[:16]).resolve()
    if (not folder.is_relative_to(root) or path.parent != folder
            or not re.fullmatch(r'[a-f0-9]{32}\.md', path.name)):
        raise ValueError('DOCUMENT_CONFLICT: invalid archive location')
    return path


def _stub(marker, archive):
    return f'\n{marker}\n- 完整记录：`{archive}`（按标记精确定位）\n'


def _block_map(text):
    result = {}
    for marker, block in _blocks(text)[1]:
        if marker in result:
            raise ValueError('DOCUMENT_CONFLICT: duplicate marker in document')
        result[marker] = block
    return result


def _validate_records(root, relative, text, db, *, include_archives=False):
    """Validate authoritative indexed originals before a view is changed or skipped."""
    current = _block_map(text)
    archives = {}
    for record in db.execute('SELECT * FROM records WHERE source=?', (relative,)):
        marker = record['marker']
        if record['location'] == 'active':
            if marker not in current or _text_hash(current[marker]) != record['block_sha']:
                raise ValueError('DOCUMENT_CONFLICT: active marker payload changed or missing；禁止静默覆盖')
            continue
        if marker in current and _text_hash(current[marker]) not in {
                record['block_sha'], _text_hash(_stub(marker, record['location']))}:
            raise ValueError('DOCUMENT_CONFLICT: archived marker pointer changed')
        if not include_archives and marker not in current:
            continue
        location = record['location']
        if location not in archives:
            path = _archive_path(root, relative, location)
            raw = path.read_bytes()
            archives[location] = (sha(raw), _block_map(raw.decode()))
        digest, blocks = archives[location]
        if digest != record['archive_sha'] or marker not in blocks or _text_hash(blocks[marker]) != record['block_sha']:
            raise ValueError('DOCUMENT_CONFLICT: archived marker payload changed or missing')
    return current


def _validate_rotation(root, source, row):
    relative = str(source.relative_to(root))
    generation = row.get('generation', '')
    if (row.get('schema_version') != 1 or row.get('source') != relative
            or not isinstance(generation, str) or not re.fullmatch(r'[a-f0-9]{32}', generation)):
        raise ValueError('DOCUMENT_CONFLICT: rotation identity mismatch')
    archive = _archive_path(root, relative, row.get('archive', ''))
    replacement_value = row.get('replacement', '')
    if not isinstance(replacement_value, str):
        raise ValueError('DOCUMENT_CONFLICT: invalid replacement location')
    replacement = (root/replacement_value).resolve()
    if archive.name != generation+'.md' or replacement != archive.with_name(generation+'.active.md'):
        raise ValueError('DOCUMENT_CONFLICT: rotation material path mismatch')
    if any(not re.fullmatch(r'[a-f0-9]{64}', str(row.get(key, '')))
           for key in ('before_sha256', 'after_sha256')):
        raise ValueError('DOCUMENT_CONFLICT: invalid rotation fingerprint')
    records, retained = row.get('records'), row.get('retained')
    if (not isinstance(records, list) or not isinstance(retained, list)
            or any(not isinstance(item, dict) or not MARKER.fullmatch(str(item.get('marker', '')))
                   or not re.fullmatch(r'[a-f0-9]{64}', str(item.get('sha256', ''))) for item in records)):
        raise ValueError('DOCUMENT_CONFLICT: invalid rotation records')
    markers = [item['marker'] for item in records]
    if len(markers) != len(set(markers)) or len(retained) != len(set(retained)) or not set(retained) <= set(markers):
        raise ValueError('DOCUMENT_CONFLICT: conflicting rotation markers')
    return archive, replacement


def _preflight_rotation(db, row):
    for record in row['records']:
        old = db.execute('SELECT block_sha FROM records WHERE source=? AND marker=?',
                         (row['source'], record['marker'])).fetchone()
        if old and old['block_sha'] != record['sha256']:
            raise ValueError('DOCUMENT_CONFLICT: marker payload changed')


def _paths(project, path):
    root, source = Path(project).resolve(), Path(path).resolve()
    if not source.is_relative_to(root) or source == root:
        raise ValueError('document outside project')
    relative = str(source.relative_to(root))
    key = sha(relative.encode())
    runtime = root/'inbox/control/runtime'
    return root, source, relative, runtime/'document-locks'/f'{key}.lock', runtime/'document-rotations'/f'{key}.json'


def atomic_text(path, body):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.'+path.name+'.'+uuid.uuid4().hex)
    try:
        with temporary.open('w') as stream:
            stream.write(body); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _index(root):
    path = root/'inbox/control/runtime/document-history.db'
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA synchronous=FULL')
        db.execute('''CREATE TABLE IF NOT EXISTS records (
            source TEXT, marker TEXT, block_sha TEXT NOT NULL, location TEXT NOT NULL,
            archive_sha TEXT, PRIMARY KEY(source,marker))''')
        db.execute('''CREATE TABLE IF NOT EXISTS rotations (
            source TEXT, generation TEXT, archive TEXT, archive_sha TEXT,
            active_sha TEXT, recorded_at TEXT, PRIMARY KEY(source,generation))''')
        yield db
        db.commit()
    except BaseException:
        db.rollback(); raise
    finally:
        db.close()


def _blocks(text):
    matches = list(MARKER.finditer(text))
    if not matches:
        return text, []
    starts = [m.start()-1 if m.start() and text[m.start()-1] == '\n' else m.start() for m in matches]
    return text[:starts[0]], [(m.group().strip(), text[start:(starts[i+1] if i+1 < len(starts) else len(text))])
                             for i, (m, start) in enumerate(zip(matches, starts))]


def _header(relative, checkpoint, manifest=None):
    lines = ['# 最新活动记录', '', f'- 文档：`{relative}`',
             '- 当前任务以 TODO、有效派单、ACK、回执及 decision 为准；归档不是任务完成。']
    if checkpoint:
        for key in ('role', 'task', 'attempt', 'status', 'base', 'head', 'worktree', 'dispatch',
                    'evidence', 'next', 'pending_actions', 'open_gates', 'references', 'process_identity', 'recorded_at'):
            if checkpoint.get(key) is not None:
                item = checkpoint[key]
                value = (json.dumps(item, ensure_ascii=False) if isinstance(item, (list, dict)) else str(item)).replace('\n', ' ')
                if len(value) > 800:
                    value = value[:800]+'…（完整内容见检查点/事件原件）'
                lines.append(f'- {key}：{value}')
        role = checkpoint.get('role')
        if role:
            lines.append(f'- 完整检查点：`inbox/control/runtime/checkpoints/{role}.json`')
            lines.append(f'- 当前派单/续跑：`inbox/control/runtime/dispatch-index/{role}.json`、`inbox/control/runtime/continuations/{role}/`')
    if manifest:
        lines.append(f'- 本次归档完整恢复检查点：`{manifest}`（仅需追溯时读取）')
    lines += ['- 恢复入口：`runtime_tools.py context-input --project ROOT --role ROLE`',
              '- 未闭环任务与门禁：`TODO.md`、`HANDOFF.md`、本角色有效派单及回执。', '']
    return '\n'.join(lines)+'\n'


def _commit_rotation(db, row):
    source, archive = row['source'], row['archive']
    _preflight_rotation(db, row)
    for record in row['records']:
        db.execute('INSERT OR IGNORE INTO records VALUES (?,?,?,?,?)',
                   (source, record['marker'], record['sha256'], 'active', None))
    db.execute("UPDATE records SET location=?,archive_sha=? WHERE source=? AND location='active'",
               (archive, row['before_sha256'], source))
    for marker in row['retained']:
        db.execute("UPDATE records SET location='active',archive_sha=NULL WHERE source=? AND marker=?",
                   (source, marker))
    db.execute('INSERT OR IGNORE INTO rotations VALUES (?,?,?,?,?,?)',
               (source, row['generation'], archive, row['before_sha256'], row['after_sha256'], row['recorded_at']))
    db.commit()


def _recover(root, source, journal, db):
    row = read_json(journal)
    if not row or row.get('complete'):
        return
    archive, active = _validate_rotation(root, source, row)
    if sha(archive.read_bytes()) != row['before_sha256'] or sha(active.read_bytes()) != row['after_sha256']:
        raise ValueError('DOCUMENT_CONFLICT: rotation material changed')
    _validate_records(root, row['source'], archive.read_text(), db)
    originals = _block_map(archive.read_text())
    if any(record['marker'] not in originals or _text_hash(originals[record['marker']]) != record['sha256']
           for record in row['records']):
        raise ValueError('DOCUMENT_CONFLICT: rotation records differ from archive')
    _preflight_rotation(db, row)
    current = sha(source.read_bytes())
    if current == row['before_sha256']:
        atomic_text(source, active.read_text())
    elif current != row['after_sha256']:
        raise ValueError('DOCUMENT_CONFLICT: concurrent legacy writer during rotation')
    _commit_rotation(db, row)
    _remap_observations(root, row)
    row['complete'] = True
    save_json(journal, row)
    save_json(archive.with_suffix('.json'), row)


def _remap_observations(root, rotation):
    # Old exact byte spans must not silently point into different content. Preserve
    # their provenance, and conservatively re-check retained bytes after rotation.
    for path in (root/'inbox/control/runtime/observation-index').glob('*.json'):
        with locked(path.with_suffix('.lock')):
            value = read_json(path, {})
            affected = [e for e in value.get('entries', []) if e.get('kind') == 'session_block'
                        and e.get('source') == rotation['source']]
            if not affected:
                continue
            value['entries'] = [e for e in value['entries'] if e not in affected]
            value.setdefault('archived_entries', []).append({'generation': rotation['generation'],
                                                            'archive': rotation['archive'], 'entries': affected})
            save_json(path, value)


def _rotate(root, source, relative, journal, db, checkpoint, policy, force=False):
    raw = source.read_bytes()
    if not force and len(raw) < policy['trigger_bytes'] and raw.count(b'\n') < policy['trigger_lines']:
        return False
    if not checkpoint:
        raise ValueError('rotation requires current checkpoint; legacy history cannot be blindly tailed')
    blocks = list(_validate_records(root, relative, raw.decode(), db).items())
    generation = uuid.uuid4().hex
    folder = root/'archive/role-runtime'/sha(relative.encode())[:16]
    archive = folder/(generation+'.md')
    replacement = folder/(generation+'.active.md')
    manifest = folder/(generation+'.json')
    header = _header(relative, checkpoint, str(manifest.relative_to(root)))
    if len(header.encode()) > policy['target_bytes']:
        raise ValueError('checkpoint header exceeds active document budget')
    selected, retained = [], []
    budget = policy['target_bytes']-len(header.encode())
    for marker, block in reversed(blocks[-policy['keep_records']:]):
        if len(block.encode()) <= budget:
            selected.append(block); retained.append(marker); budget -= len(block.encode())
        else:
            old = db.execute('SELECT location FROM records WHERE source=? AND marker=?', (relative, marker)).fetchone()
            location = old['location'] if old and old['location'] != 'active' else str(archive.relative_to(root))
            stub = _stub(marker, location)
            if len(stub.encode()) <= budget:
                selected.append(stub); budget -= len(stub.encode())
    after = header+''.join(reversed(selected))
    atomic_text(archive, raw.decode())
    atomic_text(replacement, after)
    # When a prior rotation retained a stub, its marker remains indexed at the
    # original immutable archive. Only genuinely active originals are migrated.
    records = []
    for marker, block in blocks:
        old = db.execute('SELECT * FROM records WHERE source=? AND marker=?', (relative, marker)).fetchone()
        if old and old['location'] != 'active':
            if marker in retained: retained.remove(marker)
            continue
        records.append({'marker': marker, 'sha256': _text_hash(block)})
    row = dict(schema_version=1, source=relative, generation=generation,
               archive=str(archive.relative_to(root)), replacement=str(replacement.relative_to(root)),
               before_sha256=sha(raw), after_sha256=sha(after.encode()), recorded_at=now(),
               records=records, retained=retained, checkpoint=checkpoint, complete=False)
    _validate_rotation(root, source, row)
    _preflight_rotation(db, row)
    save_json(manifest, row)
    save_json(journal, row)
    if sha(source.read_bytes()) != row['before_sha256']:
        raise ValueError('DOCUMENT_CONFLICT: concurrent legacy writer during rotation')
    atomic_text(source, after)
    _commit_rotation(db, row)
    _remap_observations(root, row)
    row['complete'] = True
    save_json(journal, row)
    save_json(folder/(generation+'.json'), row)
    return True


def append_record(project, path, marker, block, *, checkpoint=None, policy=None, writer=None):
    root, source, relative, lock, journal = _paths(project, path)
    limits = {**DEFAULT_POLICY, **(policy or {})}
    with locked(lock), _index(root) as db:
        if source.exists(): _recover(root, source, journal, db)
        body = source.read_text() if source.exists() else '# 活动记录\n'
        _validate_records(root, relative, body, db)
        supplied = _block_map(block)
        if set(supplied) != {marker}:
            raise ValueError('DOCUMENT_CONFLICT: event block must contain exactly its marker')
        expected = _text_hash(block)
        old = db.execute('SELECT * FROM records WHERE source=? AND marker=?', (relative, marker)).fetchone()
        if old:
            if old['block_sha'] != expected:
                raise ValueError('DOCUMENT_CONFLICT: changed event payload')
            evidence = body if old['location'] == 'active' else _archive_path(root, relative, old['location']).read_text()
            if old['archive_sha'] and sha(evidence.encode()) != old['archive_sha']:
                raise ValueError('DOCUMENT_CONFLICT: archive changed')
            if block.strip() not in evidence:
                raise ValueError('DOCUMENT_CONFLICT: 已有会话记录与冻结内容不符，禁止静默覆盖')
            return {'appended': False, 'rotated': False, 'path': str(source)}
        if marker in body:
            if block.strip() not in body:
                raise ValueError('DOCUMENT_CONFLICT: 已有会话记录与冻结内容不符，禁止静默覆盖')
            appended = False
        else:
            (writer or atomic_text)(source, body+block)
            appended = True
        db.execute('INSERT INTO records VALUES (?,?,?,?,NULL)', (relative, marker, expected, 'active'))
        db.commit()
        rotated = _rotate(root, source, relative, journal, db, checkpoint, limits)
        return {'appended': appended, 'rotated': rotated, 'path': str(source)}


def rotate_document(project, path, checkpoint, *, force=False, policy=None):
    root, source, relative, lock, journal = _paths(project, path)
    with locked(lock), _index(root) as db:
        _recover(root, source, journal, db)
        rotated = _rotate(root, source, relative, journal, db, checkpoint,
                          {**DEFAULT_POLICY, **(policy or {})}, force)
        return {'rotated': rotated, 'path': str(source), 'bytes': source.stat().st_size}


def verify_archives(project, path):
    root, _, relative, lock, _ = _paths(project, path)
    with locked(lock), _index(root) as db:
        for row in db.execute('SELECT archive,archive_sha FROM rotations WHERE source=?', (relative,)):
            if sha(_archive_path(root, relative, row['archive']).read_bytes()) != row['archive_sha']:
                raise ValueError('DOCUMENT_CONFLICT: archive hash mismatch')
    return True


def archival_snapshot(project, path, body, *, stop_generations=()):
    """Resolve only byte-exact committed rotations for a read-only observer.

    A rotation changes the view, not the business input. This alias is valid
    only when the entire current snapshot is the frozen replacement; appends,
    edits and incomplete/corrupt material never acquire that equivalence. No
    observer cursor, ACK, journal, database or lock is created or repaired here.
    """
    root, source, relative, _, journal = _paths(project, path)
    def normalized(raw):
        return raw.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n')
    before = source.stat()
    raw = source.read_bytes()
    after = source.stat()
    if ((before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size)
            or normalized(raw) != body):
        raise ValueError('DOCUMENT_CONFLICT: session changed during archival lookup')
    ledger = root/'inbox/control/runtime/document-history.db'
    folder = root/'archive/role-runtime'/sha(relative.encode())[:16]
    if not ledger.exists():
        if journal.exists() or (folder.exists() and any(folder.iterdir())):
            raise ValueError('DOCUMENT_CONFLICT: rotation index missing')
        return body, []
    db = None
    try:
        db = sqlite3.connect(ledger.as_uri()+'?mode=ro', uri=True, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        generations = []
        while True:
            matches = db.execute('SELECT * FROM rotations WHERE source=? AND active_sha=?',
                                 (relative, sha(raw))).fetchall()
            if not matches:
                break
            if len(matches) != 1 or matches[0]['generation'] in generations:
                raise ValueError('DOCUMENT_CONFLICT: ambiguous rotation identity')
            record = matches[0]
            archive = _archive_path(root, relative, record['archive'])
            manifest = read_json(archive.with_suffix('.json'))
            if not isinstance(manifest, dict) or manifest.get('complete') is not True:
                raise ValueError('DOCUMENT_CONFLICT: rotation is not complete')
            checked_archive, replacement = _validate_rotation(root, source, manifest)
            if (checked_archive != archive or manifest['generation'] != record['generation']
                    or manifest['before_sha256'] != record['archive_sha']
                    or manifest['after_sha256'] != record['active_sha']):
                raise ValueError('DOCUMENT_CONFLICT: rotation index and material disagree')
            previous, active = archive.read_bytes(), replacement.read_bytes()
            if sha(previous) != record['archive_sha'] or active != raw:
                raise ValueError('DOCUMENT_CONFLICT: rotation material changed')
            raw = previous
            generations.append(record['generation'])
            if record['generation'] in stop_generations:
                break
        return normalized(raw), generations
    except (OSError, sqlite3.Error, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('DOCUMENT_CONFLICT: archival lookup failed: '+type(exc).__name__) from exc
    finally:
        if db is not None:
            db.close()


def indexed_markers(project, path):
    root, source, relative, lock, journal = _paths(project, path)
    with locked(lock), _index(root) as db:
        if source.exists(): _recover(root, source, journal, db)
        _validate_records(root, relative, source.read_text() if source.exists() else '', db, include_archives=True)
        indexed = {r[0] for r in db.execute('SELECT marker FROM records WHERE source=?', (relative,))}
        if source.exists(): indexed.update(marker for marker, _ in _blocks(source.read_text())[1])
        return indexed
