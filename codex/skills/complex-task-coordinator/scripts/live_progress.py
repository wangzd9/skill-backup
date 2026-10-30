"""On-demand read-only runtime observations, separate from recorded business progress."""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
from zoneinfo import ZoneInfo

from operation_jobs import operation_states
from role_status import ROLES, confined, role_status, runtime_module

TZ = ZoneInfo('Asia/Shanghai')


def _record(root, path, at, reads):
    path = confined(root, str(path))
    source = str(path.relative_to(root))
    if not path.exists():
        reads.append((path, None))
        return {}, {'source': source, 'checked_at': at.isoformat(), 'state': 'MISSING', 'age_seconds': None}
    before = path.stat()
    if before.st_size > 1024*1024:
        raise ValueError('metadata_exceeds_1MiB: '+source)
    raw = path.read_bytes(); after = path.stat()
    signature = (after.st_ino, after.st_mtime_ns, after.st_size)
    if signature != (before.st_ino, before.st_mtime_ns, before.st_size):
        raise ValueError('source_changed_during_read: '+source)
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('metadata_not_object: '+source)
    reads.append((path, signature))
    return value, {'source': source, 'checked_at': at.isoformat(), 'state': 'READ',
        'sha256': hashlib.sha256(raw).hexdigest(), 'age_seconds': at.timestamp()-after.st_mtime}


def _git(worktree):
    path = Path(worktree)
    if not path.is_absolute() or not path.is_dir() or path.resolve() in {Path('/'), Path.home()}:
        raise ValueError('checkpoint_worktree_invalid')
    environment = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    environment['GIT_OPTIONAL_LOCKS'] = '0'
    command = ['git', '--no-optional-locks', '-c', 'core.fsmonitor=false',
               '-c', 'core.untrackedCache=false', '-C', str(path.resolve())]
    def run(*args):
        result = subprocess.run([*command, *args], capture_output=True, text=True, timeout=3,
                                env=environment, stdin=subprocess.DEVNULL)
        if result.returncode:
            raise ValueError('fixed_git_read_failed')
        return result.stdout.strip()
    head = run('rev-parse', '--verify', 'HEAD')
    if not re.fullmatch(r'[a-f0-9]{40,64}', head):
        raise ValueError('git_head_unrecognized')
    if Path(run('rev-parse', '--show-toplevel')).resolve() != path.resolve():
        raise ValueError('checkpoint_worktree_is_not_repository_root')
    dirty = bool(run('status', '--porcelain=v1', '--untracked-files=normal'))
    if run('rev-parse', '--verify', 'HEAD') != head:
        raise ValueError('worktree_head_changed_during_read')
    return {'path': str(path.resolve()), 'head': head, 'dirty': dirty,
            'basis': 'fixed local git reads; dirty is not test/acceptance evidence'}


def _finish_observation(result, root, reads):
    for path, signature in reads:
        try:
            current = path.stat() if path.exists() else None
            actual = (current.st_ino, current.st_mtime_ns, current.st_size) if current else None
            if signature != actual:
                result['errors'].append('source_changed_during_collection: '+str(path.relative_to(root)))
        except OSError:
            result['errors'].append('source_unavailable_after_collection: '+str(path.relative_to(root)))
    if result['errors'] or any(row.get('state') == 'UNVERIFIED' for row in
            [*result['roles'].values(), *result['ocr'], result['strong_review'], result.get('task_observation', {})]):
        result['state'] = 'UNVERIFIED'
    return result


def collect_live_runtime(project, *, at=None, ocr_candidates=None, selected_roles=None, include_services=True):
    """Never acquires leases, refreshes indexes, ACKs, recovers or starts any work."""
    at = at or datetime.now(TZ)
    if at.tzinfo is None:
        raise ValueError('live observation requires timezone')
    runtime = runtime_module(); root, binding = runtime.binding(project)
    import role_observer
    import strong_review
    checked = at.isoformat()
    result = {'schema_version': 1, 'kind': 'live-runtime-observation', 'project': str(root),
        'checked_at': checked, 'state': 'VERIFIED', 'roles': {}, 'ocr': [], 'strong_review': {}, 'errors': [],
        'scope': 'bound runtime/worktrees and explicitly configured read-only task probes only; no tests or task-state writes'}
    reads = []
    current_binding, _ = _record(root, root/'ROLE-BINDINGS.json', at, reads)
    if current_binding != binding:
        raise ValueError('binding_changed_during_collection')
    selected = list(selected_roles) if selected_roles is not None else [role for role in ROLES if role in binding['roles']]
    if any(role not in ROLES or role not in binding['roles'] for role in selected):
        raise ValueError('selected live roles must be explicitly bound workers')
    for role in selected:
        item = {'role': role, 'checked_at': checked, 'state': 'VERIFIED', 'errors': []}
        result['roles'][role] = item
        try:
            if role not in binding['roles']:
                raise ValueError('role_not_bound')
            checkpoint, metadata = _record(root, root/f'inbox/control/runtime/checkpoints/{role}.json', at, reads)
            if checkpoint and (checkpoint.get('schema_version') != 1 or checkpoint.get('role') != role):
                raise ValueError('checkpoint_identity_mismatch')
            item['checkpoint'] = {**metadata, 'value': {key: checkpoint.get(key) for key in
                ('task', 'attempt', 'status', 'recorded_at', 'event_id', 'dispatch', 'evidence', 'next',
                 'head', 'base', 'worktree', 'pending_actions', 'open_gates', 'slot_released') if key in checkpoint}}
            if checkpoint.get('recorded_at'):
                recorded_at = datetime.fromisoformat(checkpoint['recorded_at'].replace('Z', '+00:00'))
                item['checkpoint']['recorded_age_seconds'] = (at-recorded_at).total_seconds() if recorded_at.tzinfo else None
            lease, lease_meta = _record(root, runtime.lease_path(root, role), at, reads)
            owner = runtime.owner_state(lease) if lease.get('owner') else 'none'
            item['owner'] = {**lease_meta, 'state': owner, 'pid': lease.get('pid'),
                'process_start': lease.get('process_start'), 'basis': 'process start identity; liveness alone proves no business progress'}
            if lease.get('owner') and owner != 'alive':
                item['errors'].append('owner_process_exited_reused_or_unverifiable')
            item['availability'] = role_status(root, role, at=at)
            operation_meta = {str(path): _record(root, path, at, reads)[1] for path in
                              sorted((root/f'inbox/control/runtime/operations/{role}').glob('*/result.json'))}
            item['operations'] = [{key: operation.get(key) for key in
                ('operation_id', 'task', 'attempt', 'status', 'source', 'pid', 'process_start', 'boot_id',
                 'unit', 'started_at', 'ended_at', 'exit_code', 'reason')} for operation in operation_states(root, role)]
            for operation in item['operations']:
                metadata = operation_meta.get(operation['source'])
                if metadata is None:
                    raise ValueError('new_operation_arrived_during_collection')
                operation.update(checked_at=checked, age_seconds=metadata['age_seconds'])
                if operation['status'] == 'UNKNOWN':
                    item['errors'].append('operation_unverifiable: '+str(operation['operation_id']))
                if operation['status'] in {'RUNNING', 'PENDING'} and any(
                        operation.get(key) and operation[key] != checkpoint.get(key) for key in ('task', 'attempt')):
                    item['errors'].append('active_operation_checkpoint_identity_conflict: '+str(operation['operation_id']))
            if checkpoint.get('worktree'):
                item['worktree'] = {**_git(checkpoint['worktree']), 'checked_at': checked,
                                    'source': item['checkpoint']['source']}
                expected = checkpoint.get('head')
                if expected:
                    matches = (isinstance(expected, str) and re.fullmatch(r'[a-f0-9]{7,64}', expected)
                               and item['worktree']['head'].startswith(expected))
                    item['worktree']['checkpoint_head_match'] = bool(matches)
                    if not matches:
                        item['errors'].append('worktree_HEAD_differs_from_recorded_checkpoint')
            else:
                item['worktree'] = {'state': 'NOT_CONFIGURED', 'source': item['checkpoint']['source'],
                    'checked_at': checked, 'reason': 'checkpoint_worktree_missing'}
            if item['availability']['status'] == 'UNKNOWN':
                item['errors'].append('availability_unverifiable')
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            item['errors'].append(type(exc).__name__+': '+str(exc)[:200])
        if item['errors']:
            item['state'] = 'UNVERIFIED'
    if not include_services:
        result['scope'] = 'explicitly selected worker runtime only; no target writes, recursion or independent service collection'
        return _finish_observation(result, root, reads)
    import ocr_runtime
    discovery_failure = None
    if ocr_candidates is None:
        try:
            ocr_candidates, _ = role_observer.ocr_observations(root)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            ocr_candidates = []
            discovery_failure = exc
    candidates = {}
    def discovery_error(source, exc):
        result['ocr'].append({'source': str(source), 'checked_at': checked, 'state': 'UNVERIFIED',
            'errors': ['OCR_candidate_unavailable: '+type(exc).__name__+': '+str(exc)[:200]]})
    if discovery_failure is not None:
        discovery_error('OCR发现索引', discovery_failure)
        # The business observer rejects an ambiguous index as a whole. A read
        # query still independently verifies other supported legacy runs.
        for spec_path in sorted((root/'reviews').rglob('spec.json')):
            try:
                spec, _ = _record(root, spec_path, at, reads)
                if spec.get('schema') == 'coordinator.ocr-run/v1':
                    directory = confined(root, str(spec_path.parent))
                    candidates[directory] = (spec_path.parent/'state.json', {})
            except (OSError, ValueError, KeyError, TypeError) as exc:
                discovery_error(spec_path.relative_to(root), exc)
    for row in ocr_candidates:
        try:
            path = confined(root, row['source'])
            if path.name != 'state.json':
                raise ValueError('OCR observation must reference a state.json')
            candidates[path.parent] = (path, {})
        except (OSError, ValueError, KeyError, TypeError) as exc:
            discovery_error(row.get('source', 'OCR candidate') if isinstance(row, dict) else 'OCR candidate', exc)
    for index in sorted((root/'inbox/control/runtime/ocr').glob('*.json')):
        try:
            pointer, _ = _record(root, index, at, reads)
            directory = confined(root, pointer['run'])
            previous = candidates.get(directory)
            if previous and previous[1] and previous[1].get('unit') != pointer.get('unit'):
                raise ValueError('OCR_conflicting_runtime_index')
            candidates[directory] = (index, pointer)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            discovery_error(index.relative_to(root), exc)
    frozen = []
    for directory, (index, pointer) in sorted(candidates.items()):
        item = {'source': str(index.relative_to(root)), 'checked_at': checked, 'state': 'UNVERIFIED', 'errors': []}
        try:
            spec, _ = _record(root, directory/'spec.json', at, reads)
            recorded, metadata = _record(root, directory/'state.json', at, reads)
            if (spec.get('schema') != 'coordinator.ocr-run/v1'
                    or Path(spec.get('project', '')).resolve() != root or pointer.get('unit') not in (None, spec.get('unit'))
                    or any(recorded.get(key) not in (None, spec.get(key)) for key in ('run_id', 'unit'))
                    or not re.fullmatch(r'coordinator-ocr-[a-f0-9]{32}\.service', str(spec.get('unit', '')))):
                raise ValueError('OCR_bound_identity_conflict')
            item.update(run=str(directory.relative_to(root)), task=spec.get('task'), attempt=spec.get('attempt'),
                recorded_state=recorded.get('state'), recorded_at=recorded.get('updated_at'),
                ended_at=recorded.get('ended_at'), age_seconds=metadata['age_seconds'],
                state_source=metadata['source'])
            # Bound service reads to active/uncertain runs and one recent record
            # per task. An abandoned PREPARED directory is not an in-flight run.
            # File mtime is only a selection fallback, never business progress.
            recent = None
            for field in ('updated_at', 'ended_at'):
                try:
                    date = datetime.fromisoformat(str(recorded.get(field)).replace('Z', '+00:00'))
                    if date.tzinfo:
                        recent = date.timestamp()
                        item['selection_basis'] = 'recorded_'+field
                        break
                except (TypeError, ValueError):
                    pass
            if recent is None:
                recent = at.timestamp()-metadata['age_seconds'] if metadata['age_seconds'] is not None else float('-inf')
                item['selection_basis'] = 'state_file_mtime_only'
            frozen.append((recent, directory, spec, recorded, item))
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            item['errors'].append(type(exc).__name__+': '+str(exc)[:200])
            result['ocr'].append(item)
    latest = {}
    for recent, directory, spec, recorded, item in frozen:
        task = item.get('task') or str(directory)
        latest[task] = max(latest.get(task, float('-inf')), recent)
    for recent, directory, spec, recorded, item in frozen:
        task = item.get('task') or str(directory)
        if recorded.get('state') not in {'SUBMITTED', 'RUNNING', 'UNKNOWN'} and recent < latest[task]:
            continue
        result['ocr'].append(item)
        try:
            observed = ocr_runtime.status(directory)
            process = role_observer.ocr_process_state(recorded, spec)
            service = observed['service']
            service_active = service.get('ActiveState') in {'active', 'activating', 'deactivating'}
            item.update(observed_state=observed['state'], service=service, worker_process_state=process,
                        service_active=service_active, in_flight=None)
            if observed['state'] in {'UNKNOWN', 'INTERRUPTED'} or process in {'unbound', 'unverified', 'exited_or_reused'}:
                item['errors'].append('OCR_service_or_process_unverifiable_or_interrupted')
            if service.get('ActiveState') in (None, 'unknown'):
                item['errors'].append('OCR_service_query_unverifiable')
            if recorded.get('state') == 'PREPARED' and service_active:
                item['errors'].append('OCR_prepared_record_service_active_conflict')
            if not item['errors']:
                item['in_flight'] = observed['state'] in {'SUBMITTED', 'RUNNING'} and service_active
            item['state'] = 'UNVERIFIED' if item['errors'] else 'VERIFIED'
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            item['errors'].append(type(exc).__name__+': '+str(exc)[:200])
    ledger = strong_review.Ledger()
    if not ledger.database.exists():
        result['strong_review'] = {'source': str(ledger.database), 'checked_at': checked, 'state': 'NOT_CONFIGURED', 'errors': []}
    else:
        try:
            status = ledger.status(str(root))
            errors = ['strong_service_identity_unverifiable: '+str(call.get('id'))
                      for call in status.get('active_calls', []) if call.get('service_identity') != 'VERIFIED']
            result['strong_review'] = {'source': str(ledger.database), 'checked_at': checked,
                'state': 'UNVERIFIED' if errors else 'VERIFIED', 'value': status, 'errors': errors,
                'active_calls_scope': 'global_serial_review_ledger_across_projects',
                'basis': 'read-only ledger/service/PID/cgroup; terminal status is not an accepted review finding'}
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            result['strong_review'] = {'source': str(ledger.database), 'checked_at': checked,
                'state': 'UNVERIFIED', 'errors': [type(exc).__name__+': '+str(exc)[:200]]}
    probe_path = root/'inbox/control/runtime/task-observation-spec.json'
    observation = {'source': str(probe_path.relative_to(root)), 'checked_at': checked,
                   'state': 'NOT_CONFIGURED', 'errors': []}
    result['task_observation'] = observation
    try:
        spec, metadata = _record(root, probe_path, at, reads)
        observation.update({key: metadata[key] for key in ('sha256', 'age_seconds') if key in metadata})
        if metadata['state'] != 'MISSING':
            from task_observation import sample, validate
            validate(spec)
            value = sample(spec, now=at)
            observation.update(state='UNVERIFIED' if value.get('errors') else 'VERIFIED', value=value,
                errors=[str(key)+': '+str(error.get('type', 'unverified'))
                        for key, error in value.get('errors', {}).items()])
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        observation.update(state='UNVERIFIED', errors=[type(exc).__name__+': '+str(exc)[:200]])
    return _finish_observation(result, root, reads)
