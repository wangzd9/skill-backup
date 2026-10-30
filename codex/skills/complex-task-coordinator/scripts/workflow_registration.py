"""Register document packages without selecting a run or granting development.

The maintenance API is deliberately not a role tool. Native intake can accept a
prepared run later, using the same authenticated development-source checks as a
new requirement. Registration records are ordinary audited ledger events.
"""
import hashlib
from pathlib import Path
import re


def freeze_sources(ledger, root, names):
    root = Path(root).resolve()
    if not isinstance(names, list) or not names or len(names) > 20 or len(set(names)) != len(names):
        raise ValueError('BOUNDED_REGISTRATION_CONTRACT_REQUIRED')
    sources = []
    for name in names:
        if not isinstance(name, str):
            raise ValueError('REGISTRATION_CONTRACT_PATH_REQUIRED')
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size > 1024 * 1024:
            raise ValueError('REGISTRATION_CONTRACT_OUTSIDE_ROOT_OR_TOO_LARGE')
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        frozen = ledger.path.parent / 'requirements' / digest
        frozen.parent.mkdir(exist_ok=True)
        if not frozen.exists():
            try:
                with frozen.open('xb') as stream:
                    stream.write(raw)
                frozen.chmod(0o400)
            except FileExistsError:
                pass
        if frozen.read_bytes() != raw:
            raise ValueError('FROZEN_REQUIREMENT_SOURCE_CHANGED')
        sources.append({'path': str(path), 'sha256': digest, 'frozen_path': str(frozen)})
    return sources


def packages(ledger):
    """Read the latest registration per package, including reference-only ones."""
    from business_ledger import _row
    with ledger._connection() as db:
        result = {}
        for row in db.execute("SELECT * FROM events WHERE kind='REQUIREMENT_PACKAGE_REGISTERED' ORDER BY event_id"):
            event = _row(row)
            item = dict(event['payload']['package'])
            item.update(registration_event_id=event['event_id'], registered_at=event['created_at'],
                        sources=event['payload']['sources'])
            if item.get('run_id'):
                run = _row(db.execute('SELECT * FROM runs WHERE run_id=?', (item['run_id'],)).fetchone())
                item['run_control'] = run['control'] if run else 'UNVERIFIED'
                item['plan_version'] = run['plan_version'] if run else None
            result[item['requirement_id']] = item
        return result


def register_packages(ledger, entries, provenance, *, command_id):
    """Atomically register a reviewed batch; existing runs/owners stay untouched."""
    from business_ledger import _deny, _json, _row
    if (not isinstance(entries, list) or not entries or len(entries) > 100
            or not isinstance(provenance, dict) or provenance.get('purpose') != 'registration_only'
            or not isinstance(provenance.get('primary_text'), str) or not provenance['primary_text'].strip()):
        raise ValueError('REGISTRATION_ONLY_SOURCE_REQUIRED')
    prepared = []
    seen = set()
    for entry in entries:
        item = dict(entry)
        identifier = item.get('requirement_id')
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,100}', identifier) or identifier in seen:
            raise ValueError('UNIQUE_REGISTRATION_ID_REQUIRED')
        seen.add(identifier)
        root = Path(item['requirement_root']).resolve()
        repo = Path(item['repo']).resolve()
        if not root.is_dir() or not (repo / '.git').exists():
            raise ValueError('REGISTRATION_ROOT_AND_REPOSITORY_REQUIRED')
        item.update(requirement_root=str(root), repo=str(repo))
        tasks = item.get('task_ids', [])
        if (not isinstance(tasks, list) or len(set(tasks)) != len(tasks)
                or any(not isinstance(t, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}', t) for t in tasks)
                or item.get('kind') not in ('development', 'reference')):
            raise ValueError('EXACT_REGISTRATION_TASK_SCOPE_REQUIRED')
        if item['kind'] == 'reference' and tasks or item['kind'] == 'development' and not tasks:
            raise ValueError('REFERENCE_IS_NOT_A_DEVELOPMENT_TASK')
        sources = freeze_sources(ledger, root, item['contract_files'])
        texts = '\n'.join(Path(s['frozen_path']).read_text() for s in sources)
        if any(not re.search(r'(?<![A-Za-z0-9_-])' + re.escape(t) + r'(?![A-Za-z0-9_-])', texts) for t in tasks):
            raise ValueError('REGISTRATION_TASK_NOT_IN_CONTRACT')
        prepared.append((item, sources))

    def operation(db):
        registered = []
        for item, sources in prepared:
            item = dict(item)
            existing = [_row(row) for row in db.execute('SELECT * FROM runs WHERE requirement_id=?', (item['requirement_id'],))]
            if len(existing) > 1:
                _deny('AMBIGUOUS_EXISTING_REQUIREMENT_RUN', requirement_id=item['requirement_id'])
            if existing:
                run = existing[0]
                if (run['requirement_root'], run['repo']) != (item['requirement_root'], item['repo']):
                    _deny('EXISTING_REQUIREMENT_SCOPE_MISMATCH')
                item.update(run_id=run['run_id'], prepared_run=not bool(run['plan_version']),
                            existing_run_preserved=True)
                if run['plan_version']:
                    item['task_ids'] = run['task_ids']
            elif item['kind'] == 'development':
                run_id = 'prepared-' + item['requirement_id']
                if db.execute('SELECT 1 FROM runs WHERE run_id=?', (run_id,)).fetchone():
                    _deny('REGISTERED_RUN_ID_CONFLICT')
                db.execute("INSERT INTO runs(run_id,requirement_id,requirement_root,repo,task_ids,control,created_at) VALUES(?,?,?,?,?,'PAUSED',?)",
                    (run_id, item['requirement_id'], item['requirement_root'], item['repo'], _json(item['task_ids']), ledger.clock()))
                ledger._event(db, 'RUN_REGISTERED', {'requirement_id': item['requirement_id'],
                    'task_ids': item['task_ids'], 'registration_only': True}, run_id=run_id)
                item.update(run_id=run_id, prepared_run=True, existing_run_preserved=False)
            else:
                item.update(run_id=None, prepared_run=False, existing_run_preserved=False)
            event = ledger._event(db, 'REQUIREMENT_PACKAGE_REGISTERED', {'package': item,
                'sources': sources, 'provenance': provenance, 'development_permission': False,
                'production_permission': False}, run_id=item['run_id'])
            registered.append({'requirement_id': item['requirement_id'], 'run_id': item['run_id'],
                               'registration_event_id': event, 'kind': item['kind']})
        return {'packages': registered, 'selection_changed': False, 'development_permission': False}
    return ledger._command('register_requirement_packages', command_id, [entries, provenance, prepared], operation)


def prepared_package(ledger, target):
    """Locate a registered placeholder; never borrow a previously running run."""
    item = packages(ledger).get(target['requirement_id'])
    if not item:
        return None
    if target.get('run_id') and target['run_id'] != item.get('run_id'):
        raise ValueError('REGISTERED_RUN_ID_MISMATCH')
    if item['kind'] == 'reference':
        raise ValueError('REFERENCE_REQUIREMENT_NEEDS_EXPLICIT_DELIVERY_CONTRACT')
    if not item.get('prepared_run'):
        raise ValueError('REGISTERED_REQUIREMENT_ALREADY_RUNNING_USE_EXISTING_RUN_CONTROL')
    if (item['requirement_root'], item['repo']) != (str(Path(target['requirement_root']).resolve()), str(Path(target['repo']).resolve())):
        raise ValueError('REGISTERED_REQUIREMENT_SCOPE_MISMATCH')
    if target.get('use_native_requirement') or not set(item['contract_files']) <= set(target.get('contract_files', [])):
        raise ValueError('REGISTERED_CONTRACT_FILES_REQUIRED')
    if not set(target['task_ids']) <= set(item['task_ids']):
        raise ValueError('TASK_OUTSIDE_REGISTERED_REQUIREMENT')
    for source in item['sources']:
        if hashlib.sha256(Path(source['path']).read_bytes()).hexdigest() != source['sha256']:
            raise ValueError('REGISTERED_CONTRACT_CHANGED_RE_REGISTER_REQUIRED')
    return item
