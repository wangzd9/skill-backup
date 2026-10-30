"""Fixed five-role resources with authenticated, journalled requirement switches.

Business remains in its concrete requirement directory. The pool only owns
selection, stable transport identities and accepted selection receipts. The
scheduler adapter must fence admission as well as inspect unclaimed workers.
"""
import argparse
from contextlib import ExitStack
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from urllib.parse import unquote, urlsplit

from runtime_common import locked, now, read_json, save_json
from manual_access import full_permission_action

ROLES = ('COORDINATOR', 'A', 'B', 'C', 'TESTER')
JOB_FIELDS = ('poll_job_id', 'report_job_id', 'morning_report_job_id', 'idle_report_job_id')
PROTECTED_JOB_FIELDS = ('id', 'enabled', 'state', 'schedule', 'origin', 'deliver')
TERMINAL = frozenset({'SUCCEEDED', 'SUCCESS', 'FAILED', 'FAILURE', 'CANCELLED', 'CANCELED',
                      'INTERRUPTED', 'SKIPPED', 'FINISHED', 'COMPLETED', 'STOPPED', 'LOST', 'RECONCILED'})


def _bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2)+'\n').encode()


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _path(value):
    if not isinstance(value, (str, Path)) or not str(value):
        raise ValueError('绝对路径缺失')
    path = Path(value)
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError('路径必须为规范绝对路径')
    return path


def _state_dir(pool_path):
    path = _path(pool_path)
    return path.parent/(path.stem+'.runtime')


def _journal_path(pool_path):
    return _state_dir(pool_path)/'transition.json'


def _pool(pool_path):
    path = _path(pool_path)
    value = read_json(path)
    if (not isinstance(value, dict) or value.get('schema_version') != 1
            or not isinstance(value.get('pool_id'), str) or not value['pool_id']
            or type(value.get('generation')) is not int or value['generation'] < 0
            or value.get('control') not in ('ACTIVE', 'PAUSED')
            or not isinstance(value.get('roles'), dict) or set(value['roles']) != set(ROLES)):
        raise ValueError('INVALID_ROLE_POOL')
    _path(value.get('catalog_path')); _path(value.get('registry_path'))
    chats, jobs = set(), set()
    for role, cfg in value['roles'].items():
        if (not isinstance(cfg, dict) or not isinstance(cfg.get('chat_id'), str)
                or not cfg['chat_id'] or not cfg.get('poll_job_id') or not cfg.get('skill')):
            raise ValueError('INVALID_STABLE_ROLE_IDENTITY: '+role)
        if cfg['chat_id'] in chats:
            raise ValueError('DUPLICATE_POOL_CHAT')
        chats.add(cfg['chat_id'])
        for field in JOB_FIELDS:
            identifier = cfg.get(field)
            if identifier is not None:
                if not isinstance(identifier, str) or not identifier or identifier in jobs:
                    raise ValueError('DUPLICATE_OR_INVALID_POOL_JOB')
                jobs.add(identifier)
    current = value.get('current')
    if current is not None:
        if not isinstance(current, dict):
            raise ValueError('INVALID_POOL_CURRENT')
        roots = current.get('role_roots')
        if roots is not None and (not isinstance(roots, dict) or set(roots) != set(ROLES)):
            raise ValueError('INVALID_POOL_ROLE_ROOTS')
        for role in ROLES:
            _path((roots or {}).get(role) or current.get('runtime_root'))
    return value


def _catalog(pool):
    index = _path(pool['catalog_path'])
    raw = index.read_bytes()
    container = index.parent
    entries, referenced_roots = {}, set()
    text = raw.decode('utf-8')

    def root_of(link):
        parsed = urlsplit(link)
        if parsed.scheme or parsed.netloc or parsed.query:
            return None
        relative = Path(unquote(parsed.path))
        if relative.is_absolute() or len(relative.parts) != 2 or relative.name != 'README.md':
            return None
        root = container/relative.parts[0]
        if root.resolve() != root or not root.is_dir() or not (root/'README.md').is_file():
            raise ValueError('目录链接缺失、越界或非规范需求目录')
        return root

    for match in re.finditer(r'\[[^\]]+\]\(([^)]+)\)', text):
        root = root_of(match[1])
        if root is not None:
            referenced_roots.add(str(root))
    for line in text.splitlines():
        match = re.match(r'^\|\s*\[([^\]]+)\]\(([^)]+)\)\s*\|', line)
        if not match:
            continue
        root = root_of(match[2])
        if root is None or root.name in entries:
            raise ValueError('需求索引包含重复或非法入口')
        entries[root.name] = {'requirement_id': root.name, 'title': match[1],
                              'requirement_root': str(root), 'runtime_root': str(root),
                              'aliases': []}
    if not entries:
        raise ValueError('需求总索引中没有明确需求入口')
    mapping = pool.get('requirements', {})
    if not isinstance(mapping, dict) or any(key not in entries for key in mapping):
        raise ValueError('角色池映射必须精确对应总索引需求')
    selectors = {}
    for key, entry in entries.items():
        config = mapping.get(key, {})
        if not isinstance(config, dict) or set(config)-{'runtime_root', 'aliases'}:
            raise ValueError('INVALID_REQUIREMENT_MAPPING')
        runtime_root = str(_path(config.get('runtime_root', entry['runtime_root'])))
        if runtime_root not in referenced_roots:
            raise ValueError('运行兼容根必须被总索引明确引用')
        aliases = config.get('aliases', [])
        if not isinstance(aliases, list) or any(not isinstance(a, str) or not a.strip() for a in aliases):
            raise ValueError('INVALID_REQUIREMENT_ALIAS')
        entry.update(runtime_root=runtime_root, aliases=aliases,
                     path=entry['requirement_root'], package_root=entry['requirement_root'])
        for selector in [key, entry['title'], entry['requirement_root'],
                         entry['requirement_root']+'/README.md', *aliases]:
            if selector in selectors and selectors[selector] != key:
                raise ValueError('AMBIGUOUS_REQUIREMENT_ALIAS: '+selector)
            selectors[selector] = key
    return {'catalog_path': str(index), 'sha256': _sha(raw), 'entries': entries, 'selectors': selectors,
            'referenced_roots': referenced_roots}


def list_requirements(pool_path):
    catalog = _catalog(_pool(pool_path))
    from workflow_runtime import ledger_path,open_ledger
    from workflow_registration import packages
    registered=packages(open_ledger(pool_path)) if ledger_path(pool_path).is_file() else {}
    for identifier,item in catalog['entries'].items():
        registration=registered.get(identifier)
        if registration:
            item['registration']={key:registration.get(key) for key in
                ('registration_event_id','kind','run_id','run_control','plan_version','contract_files',
                 'requires_explicit_task_ids','task_ids','prepared_run')}
            item['legacy_runtime_root']=item['runtime_root']
            item['runtime_root']=registration['requirement_root']
            item['registration']['authority']='business.sqlite3'
        else:
            item['registration']={'state':'CATALOG_ONLY','run_id':None}
    return {'catalog_path': catalog['catalog_path'], 'catalog_sha256': catalog['sha256'],
            'requirements': list(catalog['entries'].values())}


def _requests(pool_path):
    rows = []
    for path in (_state_dir(pool_path)/'requests').glob('*.json'):
        row = read_json(path)
        if not isinstance(row, dict) or row.get('request_id') != path.stem:
            raise ValueError('INVALID_SELECTION_REQUEST')
        rows.append(row)
    return sorted(rows, key=lambda item: (item['received_at'], item['request_id']))


def _unfinished(pool_path):
    row = read_json(_journal_path(pool_path))
    return row if row and row.get('state') != 'COMMITTED' else None


def _superseded_by(rows):
    return {identifier: row['request_id'] for row in rows for identifier in row.get('supersedes', [])}


def _pending(pool_path):
    # The newer receipt is the sole durable supersession write. A crash cannot
    # leave an old BLOCKED request at the head of the queue after its replacement
    # was accepted. Prepared transactions always recover before new selection.
    rows = _requests(pool_path)
    superseded = _superseded_by(rows)
    active = _unfinished(pool_path)
    protected = active['request_id'] if active else None
    return [row for row in rows if row['status'] != 'APPLIED'
            and (row['request_id'] not in superseded or row['request_id'] == protected)]


def status(pool_path):
    pool = _pool(pool_path)
    journal = _unfinished(pool_path)
    pending = [{k: row[k] for k in ('request_id', 'requirement_id', 'runtime_root', 'received_at', 'status', 'last_blockers') if k in row}
               for row in _pending(pool_path)]
    return {'pool_path': str(_path(pool_path)), 'catalog_path': pool['catalog_path'],
            'state': 'SWITCHING' if journal else ('IDLE' if pool.get('current') is None else pool['control']),
            'generation': pool['generation'], 'current': copy.deepcopy(pool.get('current')),
            'pending': pending, 'transition_id': journal.get('transition_id') if journal else None}


def resolve_role(pool_path, role):
    if role not in ROLES:
        raise ValueError('UNKNOWN_POOL_ROLE')
    pool = _pool(pool_path)
    result = {'schema_version': 1, 'pool_path': str(_path(pool_path)), 'role': role,
              'job_id': pool['roles'][role]['poll_job_id'], 'generation': pool['generation'],
              'requirement_id': None, 'project_root': None}
    if _unfinished(pool_path):
        return {**result, 'state': 'SWITCHING'}
    current = pool.get('current')
    if current is None:
        return {**result, 'state': 'IDLE'}
    root = (current.get('role_roots') or {}).get(role) or current['runtime_root']
    binding = read_json(Path(root)/'ROLE-BINDINGS.json', {})
    if not isinstance(binding, dict) or not _same_identity(binding.get('roles', {}).get(role), pool['roles'][role]):
        return {**result, 'state': 'UNVERIFIED'}
    gate = binding.get('role_pool')
    if gate is not None and gate != {'path': str(_path(pool_path)), 'generation': pool['generation']}:
        return {**result, 'state': 'UNVERIFIED'}
    state = 'ACTIVE' if pool['control'] == 'ACTIVE' and binding.get('control') == 'ACTIVE' else 'PAUSED'
    legacy_assignment = (not current.get('authorization_request_id')
                         and len(set((current.get('role_roots') or {}).values())) > 1)
    selected = {} if legacy_assignment else _catalog(pool)['entries'].get(current.get('requirement_id'), {})
    requirement_root = root if legacy_assignment else current.get('requirement_root') or selected.get('requirement_root')
    missing = [name for name in ('PRD.md', 'TODO.md')
               if not (Path(requirement_root or root)/name).is_file()]
    if not any((Path(root)/name).is_file() for name in ('inbox/PROTOCOL.md', 'PROTOCOL.md')):
        missing.append('inbox/PROTOCOL.md')
    return {**result, 'state': state, 'requirement_id': current.get('requirement_id'), 'project_root': root,
            'requirement_root': requirement_root,
            'legacy_assignment': legacy_assignment,
            'preparation_required': bool(missing), 'missing_documents': missing}


def validate_role_scope(pool_path, role, project, generation):
    current = resolve_role(pool_path, role)
    if (current['state'] != 'ACTIVE' or type(generation) is not int
            or current['generation'] != generation or current['project_root'] != str(_path(project))):
        raise ValueError('STALE_OR_INACTIVE_ROLE_POOL_SCOPE')
    return current


def request(pool_path, target, provenance, *, trusted_ingress=False):
    """Only a trusted current full coordinator intake may enqueue a selection.

    ``trusted_ingress`` must be supplied by the native integration, never copied
    from model arguments, attachments, a queued document or a JSON request body.
    This control receipt does not claim a role lease or perform business work.
    """
    path = _path(pool_path)
    with locked(path.with_suffix('.lock')):
        pool = _pool(path); catalog = _catalog(pool)
        if (trusted_ingress is not True or not isinstance(provenance, dict)
                or provenance.get('authenticated') is not True or provenance.get('current_full') is not True
                or provenance.get('role') != 'COORDINATOR' or provenance.get('platform') != 'feishu'
                or provenance.get('chat_id') != pool['roles']['COORDINATOR']['chat_id']
                or any(not isinstance(provenance.get(k), str) or not provenance[k].strip()
                       for k in ('user_id', 'message_id', 'primary_text'))):
            raise ValueError('CURRENT_AUTHENTICATED_FULL_COORDINATOR_REQUIRED')
        if not isinstance(target, str) or target not in catalog['selectors']:
            raise ValueError('UNKNOWN_EXACT_REQUIREMENT_TARGET')
        key = catalog['selectors'][target]; entry = catalog['entries'][key]
        action = full_permission_action(provenance['primary_text'])
        from control_intents import selection_plan
        plan = selection_plan(action, catalog['selectors'])
        if not plan or catalog['selectors'][plan['target']] != key:
            raise ValueError('目标必须是可信全权原文中的明确需求执行或切换指令，不能来自引用、否定或旁提')
        source = {k: provenance[k] for k in ('platform', 'chat_id', 'user_id', 'message_id', 'primary_text')}
        request_id = _sha(('requirement-select\0'+pool['pool_id']+'\0'+source['platform']+'\0'+source['chat_id']+'\0'+source['message_id']).encode())
        dest = _state_dir(path)/'requests'/(request_id+'.json')
        old = read_json(dest)
        if old:
            if old.get('source') != source or old.get('requirement_id') != key or old.get('runtime_root') != entry['runtime_root']:
                raise ValueError('SELECTION_MESSAGE_ID_CONFLICT')
            replacement = _superseded_by(_requests(path)).get(request_id)
            return {**_public_request(old), **({'status': 'SUPERSEDED', 'superseded_by': replacement}
                                            if replacement else {}), 'created': False}
        active = _unfinished(path)
        protected = active['request_id'] if active else None
        supersedes = [item['request_id'] for item in _pending(path) if item['request_id'] != protected]
        row = {'schema_version': 1, 'request_id': request_id, 'pool_id': pool['pool_id'],
               'status': 'QUEUED', 'received_at': now(), 'source': source,
               'supersedes': supersedes, 'pre_controls': plan['pre_controls'],
               'source_sha256': _sha(_bytes(source)), 'catalog_sha256': catalog['sha256'],
               'requirement_id': key, 'requirement_root': entry['requirement_root'], 'runtime_root': entry['runtime_root'],
               'authorization': {'scope': 'existing_requirement_business', 'requirement_id': key,
                                 'requirement_root': entry['requirement_root'],
                                 'runtime_root': entry['runtime_root'], 'source_message_id': source['message_id'],
                                 'manual_grant_inherited': False, 'production_or_strong_review_granted': False}}
        save_json(dest, row)
        return {**_public_request(row), 'created': True}


def _public_request(row):
    return {k: copy.deepcopy(row[k]) for k in ('request_id', 'status', 'requirement_id', 'requirement_root',
            'runtime_root', 'received_at', 'generation', 'transition_id', 'last_blockers', 'supersedes') if k in row}


def _same_identity(actual, stable):
    return isinstance(actual, dict) and all(actual.get(k) == stable.get(k) for k in ('chat_id', *JOB_FIELDS))


def _job_ids(pool):
    return sorted({cfg[k] for cfg in pool['roles'].values() for k in JOB_FIELDS if cfg.get(k)})


def _process_state(pid, start):
    if type(pid) is not int or pid <= 0:
        return 'unverified'
    try:
        values = []
        for _ in range(2):
            fields = Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()
            values.append(None if fields[0] in ('Z', 'X') else fields[19])
    except (FileNotFoundError, ProcessLookupError):
        return 'dead'
    except (OSError, ValueError, IndexError):
        return 'unverified'
    if values[0] != values[1]:
        return 'unverified'
    if values[0] is None:
        return 'dead'
    if not start:
        return 'unverified'
    return 'alive' if values[0] == str(start) else 'dead'


def _record_process_state(row, unit=None):
    """Use the same legacy cgroup evidence as role_observer.ocr_process_state.

    Missing start ticks never prove liveness. PID disappearance (or a recorded
    unique service cgroup mismatch after PID reuse) independently proves exit.
    """
    pid = row.get('pid')
    if pid is None:
        return 'none'
    if row.get('boot_id'):
        try:
            if Path('/proc/sys/kernel/random/boot_id').read_text().strip() != row['boot_id']:
                return 'dead'
        except OSError:
            return 'unverified'
    start = row.get('process_start_ticks') or row.get('process_start')
    state = _process_state(pid, start)
    if state != 'unverified' or start:
        return state
    cgroup = row.get('cgroup')
    if isinstance(cgroup, str) and unit and '/'+unit in cgroup:
        try:
            actual = Path(f'/proc/{pid}/cgroup').read_text().strip()
            return 'alive' if actual == cgroup.strip() else 'dead'
        except (FileNotFoundError, ProcessLookupError):
            return 'dead'
        except OSError:
            pass
    return 'unverified'


def _roots(pool, target=None):
    result = set()
    if pool.get('current'):
        current = pool['current']
        result.update((current.get('role_roots') or {r: current['runtime_root'] for r in ROLES}).values())
    if target:
        result.add(target)
    return sorted(result)


def _lease_blockers(roots):
    blockers = []
    for root in roots:
        for role in ROLES:
            lease = read_json(Path(root)/f'inbox/control/runtime/leases/{role}.json', {})
            if lease and _process_state(lease.get('pid'), lease.get('process_start')) != 'dead':
                blockers.append({'code': 'ROLE_OWNER_ACTIVE_OR_UNKNOWN', 'project_root': root, 'role': role})
        binding = read_json(Path(root)/'ROLE-BINDINGS.json', {})
        for row in binding.get('legacy_runs', []):
            if not row.get('stopped_verified') and _process_state(row.get('pid'), row.get('process_start')) != 'dead':
                blockers.append({'code': 'LEGACY_OWNER_ACTIVE_OR_UNKNOWN', 'project_root': root, 'role': row.get('role')})
    return blockers


def _service_active_or_unknown(unit):
    if not isinstance(unit, str) or not re.fullmatch(r'[A-Za-z0-9_@:-][A-Za-z0-9_.@:-]*', unit):
        return True
    if not unit.endswith('.service'):
        if '.' in unit:
            return True
        unit += '.service'
    try:
        run = subprocess.run(['systemctl', '--user', 'show', unit, '-p', 'LoadState',
                              '-p', 'ActiveState', '-p', 'MainPID', '-p', 'ControlPID', '-p', 'ControlGroup'],
                             capture_output=True, text=True, timeout=5)
        fields = dict(line.split('=', 1) for line in run.stdout.splitlines() if '=' in line)
        stopped = (fields.get('LoadState') in ('loaded', 'not-found', 'masked')
                   and fields.get('ActiveState') in ('inactive', 'failed')
                   and fields.get('MainPID') == '0' and fields.get('ControlPID') == '0'
                   and 'ControlGroup' in fields)
        # Collected transient units can return a nonzero exit status with an
        # explicit, complete not-found state. Bus/probe errors are still unknown.
        if not (stopped and (run.returncode == 0 or fields['LoadState'] == 'not-found')):
            return True
        group = fields['ControlGroup']
        if group:
            mount = Path('/sys/fs/cgroup')
            path = (mount/group.lstrip('/')).resolve()
            if not group.startswith('/') or not path.is_relative_to(mount):
                return True
            try:
                events = dict(line.split() for line in (path/'cgroup.events').read_text().splitlines())
                return events.get('populated') != '0'
            except FileNotFoundError:
                pass  # An independently stopped unit may already have no cgroup.
        return False
    except (OSError, ValueError, subprocess.SubprocessError):
        return True


def _independent_blockers(roots):
    """No model calls or ledger writes; unresolved independent work stays put."""
    blockers = []
    for raw in roots:
        root = Path(raw)
        for result in (root/'inbox/control/runtime/operations').glob('*/*/result.json'):
            from operation_jobs import inspect_state
            row = inspect_state(result)  # Includes the separate submission receipt.
            unit = row.get('unit')
            if not unit and (result.parent/'spec.json').is_file():
                # execute_plan replaces PENDING with worker output without its
                # unit field. start_job derives this stable unit from the path.
                unit = 'role-operation-'+_sha(str(result.parent).encode())[:24]
            process = _record_process_state(row, unit)
            service_unknown = _service_active_or_unknown(unit) if unit else None
            if (process in ('alive', 'unverified') or service_unknown is True
                    or service_unknown is None and process != 'dead'):
                blockers.append({'code': 'INDEPENDENT_OPERATION_ACTIVE_OR_UNKNOWN', 'source': str(result)})
        directories = set()
        for index in (root/'inbox/control/runtime/ocr').glob('*.json'):
            row = read_json(index)
            directory = Path(row.get('run', ''))
            directory = (root/directory if not directory.is_absolute() else directory).resolve()
            if not directory.is_relative_to(root):
                raise ValueError('OCR_RUN_OUTSIDE_REQUIREMENT')
            directories.add(directory)
        for spec_path in (root/'reviews').rglob('spec.json'):
            spec = read_json(spec_path)
            if isinstance(spec, dict) and spec.get('schema') == 'coordinator.ocr-run/v1':
                directories.add(spec_path.parent)
        for directory in directories:
            row = read_json(directory/'state.json', {})
            spec = read_json(directory/'spec.json', {})
            # OCR has no machine adjudication ledger: successful workers keep
            # AWAITING_DECISION forever. A stopped run is a safe scope checkpoint;
            # its old acceptance gates remain intact, never implicitly PASS.
            processes = [_record_process_state(record, spec.get('unit'))
                         for record in [row, *row.get('batches', [])]]
            if (_service_active_or_unknown(spec.get('unit'))
                    or any(state in ('alive', 'unverified') for state in processes)):
                blockers.append({'code': 'OCR_ACTIVE_OR_UNRESOLVED', 'source': str(directory)})
    database = Path.home()/'.local/state/complex-task-reviews/ledger.sqlite3'
    if database.exists():
        with sqlite3.connect(database.as_uri()+'?mode=ro', uri=True) as con:
            con.row_factory = sqlite3.Row
            columns = {row[1] for row in con.execute('PRAGMA table_info(calls)')}
            select = ['project', 'status']+[name for name in ('unit', 'wrapper_pid', 'wrapper_start', 'child_pid', 'child_start') if name in columns]
            marks = ','.join('?' for _ in roots)
            rows = con.execute('SELECT '+','.join(select)+' FROM calls WHERE project IN ('+marks+')', roots).fetchall() if roots else []
            for record in rows:
                row = dict(record)
                active = (str(row['status']).upper() not in TERMINAL
                          or row.get('unit') and _service_active_or_unknown(row['unit'])
                          or any(row.get(p) and _process_state(row[p], row.get(s)) != 'dead'
                                 for p, s in (('wrapper_pid', 'wrapper_start'), ('child_pid', 'child_start'))))
                if active:
                    blockers.append({'code': 'STRONG_REVIEW_ACTIVE_OR_UNKNOWN', 'project_root': row['project']})
    return blockers


def _scheduler_view(adapter, pool):
    if adapter is None:
        return {}, [{'code': 'SCHEDULER_ADMISSION_ADAPTER_REQUIRED'}]
    view = adapter.inspect(_job_ids(pool))
    if not isinstance(view, dict) or not isinstance(view.get('jobs'), dict) or not isinstance(view.get('executions'), list):
        raise ValueError('INVALID_SCHEDULER_INSPECTION')
    blockers = list(view.get('blockers', []))
    protected = {}
    for identifier in _job_ids(pool):
        row = view['jobs'].get(identifier)
        if not isinstance(row, dict) or row.get('id') != identifier:
            blockers.append({'code': 'POOL_JOB_MISSING_OR_UNVERIFIED', 'job_id': identifier})
        else:
            protected[identifier] = {k: row.get(k) for k in PROTECTED_JOB_FIELDS}
    for row in view['executions']:
        if row.get('job_id') not in _job_ids(pool):
            raise ValueError('SCHEDULER_RETURNED_FOREIGN_EXECUTION')
        if row.get('job_id') not in {cfg['poll_job_id'] for cfg in pool['roles'].values()}:
            # Read-only report turns do not own or consume business inputs.
            continue
        if row.get('safe_checkpoint') is True and row.get('identity_verified') is True:
            continue
        if str(row.get('state', 'UNKNOWN')).upper() not in TERMINAL or row.get('process_alive') is True:
            blockers.append({'code': 'CRON_EXECUTION_ACTIVE_OR_UNKNOWN',
                             'job_id': row.get('job_id'), 'execution_id': row.get('execution_id')})
    return protected, blockers


def _file_change(path, after):
    raw = path.read_bytes() if path.exists() else None
    return {'path': str(path), 'before': raw.decode('utf-8') if raw is not None else None,
            'before_sha256': _sha(raw) if raw is not None else None,
            'after': after, 'after_sha256': _sha(_bytes(after))}


def _target_templates(value, root):
    if isinstance(value, str):
        return value.replace('{project_root}', root).replace('{root}', root)
    if isinstance(value, list):
        return [_target_templates(item, root) for item in value]
    if isinstance(value, dict):
        return {key: _target_templates(item, root) for key, item in value.items()}
    return value


def _plan(pool_path, pool, row):
    catalog = _catalog(pool)
    selected = catalog['entries'].get(row['requirement_id'])
    if (catalog['sha256'] != row['catalog_sha256'] or not selected
            or selected['runtime_root'] != row['runtime_root']):
        raise ValueError('SELECTION_CATALOG_OR_MAPPING_CHANGED')
    target = _path(row['runtime_root'])
    registry_path = _path(pool['registry_path'])
    registry = read_json(registry_path)
    if not isinstance(registry, dict) or not isinstance(registry.get('projects'), list):
        raise ValueError('INVALID_ROLE_REGISTRY')
    roots = _roots(pool, str(target)); documents = {}
    for entry in registry['projects']:
        registered_root = str(_path(entry.get('project_root')))
        if registered_root not in roots:
            existing = read_json(Path(registered_root)/'ROLE-BINDINGS.json', {})
            if any(_same_identity(existing.get('roles', {}).get(role), stable) for role, stable in pool['roles'].items()):
                raise ValueError('POOL_ROLE_BOUND_OUTSIDE_CURRENT_ROOTS')
    for root in roots:
        path = Path(root)/'ROLE-BINDINGS.json'
        value = read_json(path)
        if value is None and root == str(target):
            value = _target_templates(copy.deepcopy(pool.get('binding_defaults')), root)
            if not isinstance(value, dict):
                raise ValueError('NEW_REQUIREMENT_BINDING_DEFAULTS_REQUIRED')
            value.update(schema_version=1, project_root=root, roles={})
        if (not isinstance(value, dict) or value.get('schema_version') != 1
                or value.get('project_root') != root or not isinstance(value.get('roles'), dict)
                or value.get('control') not in ('ACTIVE', 'PAUSED')):
            raise ValueError('INVALID_CONCRETE_REQUIREMENT_BINDING: '+root)
        documents[root] = copy.deepcopy(value)
    current = pool.get('current')
    for role, stable in pool['roles'].items():
        old_root = ((current.get('role_roots') or {}).get(role) or current['runtime_root']) if current else None
        if old_root and not _same_identity(documents[old_root]['roles'].get(role), stable):
            raise ValueError('CURRENT_POOL_ROLE_BINDING_CHANGED: '+role)
        for root, value in documents.items():
            actual = value['roles'].get(role)
            if actual:
                if not _same_identity(actual, stable):
                    if root == str(target):
                        raise ValueError('TARGET_ROLE_BELONGS_TO_ANOTHER_POOL: '+role)
                else:
                    del value['roles'][role]
        existing = read_json(target/'ROLE-BINDINGS.json', {}).get('roles', {}).get(role, {})
        # Worktrees/task-specific metadata are local to the target, never copied
        # from the role's old business directory through the stable pool.
        transport_fields = {'skill', 'chat_id', 'chat_name', *JOB_FIELDS, 'session_file', 'report_policy', 'skill_version'}
        transport = {key: _target_templates(copy.deepcopy(value), str(target))
                     for key, value in stable.items() if key in transport_fields}
        dest = {**(existing if _same_identity(existing, stable) else {}), **transport}
        dest.setdefault('session_file', 'sessions/'+role+'.md')
        dest.setdefault('worktree', None)
        dest.pop('coordinated_projects', None)
        documents[str(target)]['roles'][role] = dest
    generation = pool['generation']+1
    target_binding = documents[str(target)]
    target_binding['role_pool'] = {'path': str(_path(pool_path)), 'generation': generation}
    target_binding['requirement_authorization'] = {**row['authorization'], 'request_id': row['request_id'],
                                                  'requirement_root': row['requirement_root'],
                                                  'source_sha256': row['source_sha256']}
    for root, value in documents.items():
        if root != str(target):
            value.pop('role_pool', None)
    final_registry = copy.deepcopy(registry)
    if not any(entry.get('project_root') == str(target) for entry in final_registry['projects']):
        final_registry['projects'].append({'project_root': str(target), 'bindings': str(target/'ROLE-BINDINGS.json')})
    final_registry.pop('role_pool_transition', None)
    new_pool = copy.deepcopy(pool)
    new_pool.update(generation=generation, current={'requirement_id': row['requirement_id'],
        'requirement_root': row['requirement_root'],
        'runtime_root': str(target), 'role_roots': {role: str(target) for role in ROLES},
        'authorization_request_id': row['request_id'], 'accepted_source_sha256': row['source_sha256']})
    transition_id = _sha((pool['pool_id']+'\0'+row['request_id']+'\0'+str(generation)).encode())
    quarantine = copy.deepcopy(registry)
    quarantine['projects'] = [entry for entry in registry['projects'] if entry.get('project_root') not in roots]
    quarantine['role_pool_transition'] = {'pool_path': str(_path(pool_path)), 'transition_id': transition_id}
    files = [_file_change(Path(root)/'ROLE-BINDINGS.json', value) for root, value in sorted(documents.items())]
    registry_change = _file_change(registry_path, final_registry)
    registry_change.update(quarantine=quarantine, quarantine_sha256=_sha(_bytes(quarantine)))
    files += [registry_change, _file_change(_path(pool_path), new_pool)]
    return {'schema_version': 1, 'transition_id': transition_id, 'state': 'PREPARED', 'created_at': now(),
            'pool_path': str(_path(pool_path)), 'request_id': row['request_id'], 'generation': generation,
            'affected_roots': roots, 'registry_path': str(registry_path), 'files': files}


def _check_files(journal):
    for entry in journal['files']:
        path = Path(entry['path']); raw = path.read_bytes() if path.exists() else None
        actual = _sha(raw) if raw is not None else None
        if actual not in {entry['before_sha256'], entry['after_sha256'], entry.get('quarantine_sha256')}:
            raise ValueError('TRANSITION_FILE_CONFLICT: '+entry['path'])


def _validate_journal(pool_path, pool, journal):
    if (journal.get('schema_version') != 1 or journal.get('pool_path') != str(pool_path)
            or journal.get('registry_path') != pool['registry_path']
            or type(journal.get('generation')) is not int
            or journal['generation'] not in (pool['generation'], pool['generation']+1)):
        raise ValueError('INVALID_POOL_TRANSITION_JOURNAL')
    roots = journal.get('affected_roots', [])
    catalog = _catalog(pool)
    known = catalog['referenced_roots'] | set(_roots(pool))
    if not roots or len(roots) != len(set(roots)) or any(str(_path(root)) not in known for root in roots):
        raise ValueError('TRANSITION_ROOT_NOT_IN_CATALOG')
    expected = {str(Path(root)/'ROLE-BINDINGS.json') for root in roots} | {str(pool_path), pool['registry_path']}
    if (len(journal.get('files', [])) != len(expected)
            or {row.get('path') for row in journal.get('files', [])} != expected):
        raise ValueError('INVALID_TRANSITION_FILE_SET')
    for entry in journal['files']:
        if (_sha(_bytes(entry['after'])) != entry.get('after_sha256')
                or (entry.get('before') is None) != (entry.get('before_sha256') is None)
                or entry.get('before') is not None and _sha(entry['before'].encode()) != entry.get('before_sha256')
                or 'quarantine' in entry and _sha(_bytes(entry['quarantine'])) != entry.get('quarantine_sha256')):
            raise ValueError('TRANSITION_SNAPSHOT_HASH_MISMATCH')
    _check_files(journal)


def _finish(pool_path, journal, adapter, pool, protected):
    _check_files(journal)
    registry = next(entry for entry in journal['files'] if entry['path'] == journal['registry_path'])
    save_json(registry['path'], registry['quarantine'])
    for entry in journal['files']:
        if entry is not registry:
            save_json(entry['path'], entry['after'])
    save_json(registry['path'], registry['after'])
    observed, blockers = _scheduler_view(adapter, pool)
    if observed != protected or blockers:
        raise ValueError('SCHEDULER_CHANGED_DURING_TRANSITION')
    request_path = _state_dir(pool_path)/'requests'/(journal['request_id']+'.json')
    row = read_json(request_path)
    if not row:
        raise ValueError('TRANSITION_REQUEST_MISSING')
    row.update(status='APPLIED', applied_at=now(), generation=journal['generation'], transition_id=journal['transition_id'])
    save_json(request_path, row)
    journal.update(state='COMMITTED', committed_at=now())
    save_json(_state_dir(pool_path)/'transitions'/(journal['transition_id']+'.json'), journal)
    save_json(_journal_path(pool_path), journal)
    return {'state': 'APPLIED', 'request_id': row['request_id'], 'generation': journal['generation'],
            'current': read_json(pool_path)['current'], 'transition_id': journal['transition_id']}


def reconcile(pool_path, scheduler_adapter=None, apply=False):
    """Plan or safely commit one queued selection, never reclaim live owners."""
    pool_path = _path(pool_path); initial = _pool(pool_path)
    pending = _pending(pool_path)
    journal = _unfinished(pool_path)
    if not journal and not pending:
        return {**status(pool_path), 'state': 'NO_PENDING_REQUEST'}
    if scheduler_adapter is None and apply:
        try:
            from cron.role_pool_scheduler import CronRolePoolAdapter
            scheduler_adapter = CronRolePoolAdapter(pool_path)
        except ImportError:
            pass
    txid = journal['transition_id'] if journal else pending[0]['request_id']
    if apply and scheduler_adapter is None:
        return {'state': 'BLOCKED', 'generation': initial['generation'],
                'blockers': [{'code': 'SCHEDULER_ADMISSION_ADAPTER_REQUIRED'}]}

    def check_and_apply():
        pool = _pool(pool_path)
        if _job_ids(pool) != _job_ids(initial):
            raise ValueError('POOL_IDENTITIES_CHANGED_DURING_SWITCH')
        active = _unfinished(pool_path)
        queued = _pending(pool_path)
        if not active and not queued:
            return {**status(pool_path), 'state': 'NO_PENDING_REQUEST'}
        def waiting(blockers, state='WAITING_SAFE_POINT'):
            request_id = active['request_id'] if active else queued[0]['request_id']
            if apply:
                dest = _state_dir(pool_path)/'requests'/(request_id+'.json')
                saved = read_json(dest)
                saved.update(status=state, last_blockers=blockers)
                save_json(dest, saved)
            return {'state': state, 'generation': pool['generation'], 'request_id': request_id,
                    'blockers': blockers, 'recovering': bool(active)}
        if apply and not active and queued[0].get('pre_controls'):
            saved_path = _state_dir(pool_path)/'requests'/(queued[0]['request_id']+'.json')
            saved = read_json(saved_path)
            if not saved.get('pre_controls_applied'):
                method = getattr(scheduler_adapter, 'pause_business_roles', None)
                if method is None:
                    return waiting([{'code': 'ORDERED_CONTROL_ADAPTER_REQUIRED'}], 'BLOCKED')
                result = method([item['role'] for item in saved['pre_controls']])
                saved.update(pre_controls_applied=True, pre_control_receipts=result)
                save_json(saved_path, saved)
        try:
            plan = active or _plan(pool_path, pool, queued[0])
            if active:
                _validate_journal(pool_path, pool, active)
        except (OSError, ValueError, KeyError, TypeError) as error:
            return waiting([{'code': 'SWITCH_INPUT_UNVERIFIED', 'reason': str(error)}], 'BLOCKED')
        try:
            protected, blockers = _scheduler_view(scheduler_adapter, pool)
            blockers += _lease_blockers(plan['affected_roots'])+_independent_blockers(plan['affected_roots'])
        except (OSError, ValueError, KeyError, TypeError, AttributeError, sqlite3.Error) as error:
            return waiting([{'code': 'SWITCH_RUNTIME_UNVERIFIED', 'reason': str(error)}], 'BLOCKED')
        if blockers:
            return waiting(blockers)
        if active and protected != active.get('protected_jobs'):
            raise ValueError('SCHEDULER_CONFIGURATION_CHANGED_SINCE_TRANSITION')
        if not apply:
            return {'state': 'READY', 'generation': pool['generation'], 'next_generation': plan['generation'],
                    'request_id': plan['request_id'], 'recovering': bool(active),
                    'affected_roots': plan['affected_roots']}
        with ExitStack() as stack:
            for root in plan['affected_roots']:
                for role in ROLES:
                    name = 'coordinator.lock' if role == 'COORDINATOR' else role+'.lock'
                    stack.enter_context(locked(Path(root)/'inbox'/name))
            # Claims use these same stable locks. Re-check after taking all of
            # them; scheduler admission remains fenced throughout.
            try:
                late = _lease_blockers(plan['affected_roots'])+_independent_blockers(plan['affected_roots'])
            except (OSError, ValueError, KeyError, TypeError, AttributeError, sqlite3.Error) as error:
                return waiting([{'code': 'SWITCH_RUNTIME_UNVERIFIED', 'reason': str(error)}], 'BLOCKED')
            if late:
                return {'state': 'WAITING_SAFE_POINT', 'generation': pool['generation'], 'blockers': late}
            if not active:
                plan['protected_jobs'] = protected
                save_json(_journal_path(pool_path), plan)
            return _finish(pool_path, plan, scheduler_adapter, pool, protected)

    if not apply:
        return check_and_apply()
    with scheduler_adapter.switch_guard(_job_ids(initial), txid):
        with locked(pool_path.with_suffix('.lock')):
            return check_and_apply()


def main():
    parser = argparse.ArgumentParser(description='固定角色池的只读需求目录与当前范围查询。')
    parser.add_argument('action', choices=('list', 'status', 'resolve'))
    parser.add_argument('--pool', required=True)
    parser.add_argument('--role', choices=ROLES)
    args = parser.parse_args()
    try:
        if args.action == 'resolve':
            if not args.role:
                parser.error('resolve requires --role')
            value = resolve_role(args.pool, args.role)
        else:
            value = {'list': list_requirements, 'status': status}[args.action](args.pool)
        print(json.dumps(value, ensure_ascii=False))
        return 0
    except (OSError, ValueError, TypeError, sqlite3.Error) as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
def workflow_enabled(pool_path):
    from workflow_runtime import active
    return active(pool_path)


def prepare_workflow_run(job, *, pool_path, execution, home, session_id):
    import workflow_runtime
    prepare = (workflow_runtime.prepare if job['role_pool'].get('kind', 'business') == 'business'
               else workflow_runtime.prepare_report)
    return prepare(job, pool_path=pool_path, execution=execution, home=home, session_id=session_id)


def legacy_lease_maintenance(scope, *, home):
    from role_status import runtime_module
    return runtime_module().LegacyLeaseHeartbeat(scope, home=home)
