"""Inspect, stage and gate the five-role migration without restoring old databases.

inspect/validate/rollback-preview are read-only. prepare writes only the STAGED
business ledger and its proposal. activate requires matching P0/T1/T2/M/R evidence
for the actual code digest, including real schema-2 observation and real model
acceptance. export --freeze closes new admission; it never kills an owner, clears
a lease, resumes a job, sends a message or launches an external action.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time

from business_ledger import BusinessLedger, ROLES
from runtime_common import locked, save_json

GATES = ('P0', 'T1', 'T2', 'M', 'R')
FIELDS = ('role', 'task', 'task_id', 'attempt', 'status', 'event_id', 'run_id', 'work_id', 'candidate_id')
LEGACY_CAPABILITIES = frozenset({'identity_sidecar', 'integer_attempts', 'candidate_ids',
    'verification_failures', 'event_cursor', 'notification_receipts', 'service_ids',
    'controls', 'pause', 'generation_fence'})
PHASE_MAP = {'PENDING': 'PENDING', 'DEVELOPING': 'RUNNING', 'REVIEW': 'DELIVERED_REVIEW_PENDING',
             'COMPLETED': 'ACCEPTED', 'CANCELLED': 'CANCELLED'}


def sha(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            digest.update(block)
    return digest.hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def sealed(value):
    return {**value, 'sha256': sha(value)}


def verify_seal(value):
    if not isinstance(value, dict) or value.get('sha256') != sha({k: v for k, v in value.items() if k != 'sha256'}):
        raise ValueError('MANIFEST_DIGEST_MISMATCH')
    return value


def routing_semantics(path):
    """Hash only policy values: never read .env or export credential material."""
    path = Path(path)
    if not path.exists():
        return {'present': False}
    import yaml
    try:
        config = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError:
        raise ValueError('ROUTING_POLICY_UNVERIFIED') from None
    if not isinstance(config, dict):
        raise ValueError('ROUTING_POLICY_UNVERIFIED')
    settings = config.get('plugins', {}).get('entries', {}).get('role-workflow-runtime', {}).get('settings', {})
    route = settings.get('model_routing', {})
    if not isinstance(route, dict):
        raise ValueError('ROUTING_POLICY_UNVERIFIED')
    allowed = {'auto_cloud_profile', 'default_cloud_profile', 'ocr_profile', 'cloud_provider',
        'cloud_model', 'cloud_key_env', 'local_provider', 'local_model', 'local_key_env',
        'metrics_url', 'local_ocr_enabled'}
    # Credential ENV VARIABLE NAMES are policy, their values are never resolved.
    result = {key: value for key, value in route.items() if key in allowed}
    profiles = route.get('cloud_profiles', {})
    if not isinstance(profiles, dict):
        raise ValueError('ROUTING_PROFILES_UNVERIFIED')
    result['cloud_profiles'] = {name: {key: value for key, value in profile.items()
        if key in {'cloud_provider', 'cloud_model', 'cloud_key_env'}}
        for name, profile in profiles.items() if isinstance(profile, dict)}
    from urllib.parse import urlsplit, urlunsplit
    def endpoint(value):
        parsed = urlsplit(str(value or ''))
        # Never include URL credentials, query strings or fragments in policy.
        host = parsed.hostname or ''
        if parsed.port:
            host += ':'+str(parsed.port)
        return urlunsplit((parsed.scheme, host, parsed.path, '', ''))
    provider_fields = {'name', 'default_model', 'api_mode', 'key_env', 'discover_models'}
    providers = config.get('providers', {})
    if not isinstance(providers, dict):
        raise ValueError('PROVIDER_POLICY_UNVERIFIED')
    provider_policy = {}
    for name, value in providers.items():
        if not isinstance(value, dict):
            raise ValueError('PROVIDER_POLICY_UNVERIFIED')
        provider_policy[name] = {key: item for key, item in value.items() if key in provider_fields}
        if 'base_url' in value:
            provider_policy[name]['base_url'] = endpoint(value['base_url'])
        # Arbitrary request overrides are not exported or parsed for secrets;
        # their full value is hashed so changing behavior still fences receipts.
        for key in ('models', 'extra_body', 'request_overrides', 'capabilities'):
            if key in value:
                provider_policy[name][key+'_sha256'] = sha(value[key])
    model = config.get('model', {})
    model_policy = {key: value for key, value in model.items() if key in ('provider', 'default', 'api_mode')} if isinstance(model, dict) else {}
    if isinstance(model, dict) and 'base_url' in model:
        model_policy['base_url'] = endpoint(model['base_url'])
    return {'present': True, 'model_routing': result, 'providers': provider_policy, 'model': model_policy}


def code_version(paths=None, *, profile_home=None):
    """The same byte digest must appear in all acceptance receipts.

    Paths are explicit and retained, so a receipt cannot silently refer to a
    different checkout. Tests pass their own fixture source paths.
    profile_home is an explicit offline release layout; production validators
    continue using the installed default and do not accept it as deployment proof.
    """
    if paths is not None and profile_home is not None:
        raise ValueError('EXPLICIT_SOURCES_OR_PROFILE_REQUIRED')
    profile = Path(profile_home).resolve() if profile_home is not None else Path.home()
    default_sources = paths is None
    if default_sources:
        directory = Path(__file__).resolve().parent
        paths = [p for p in directory.glob('*.py') if not p.name.startswith(('test_', 'verify_'))] + [directory/'plugin.yaml', directory/'SOURCE_LAYOUT.json']
        paths += list((directory/'scripts').glob('*.py'))
        paths += list((directory/'deployment/systemd').glob('*.service'))
        paths += [directory/'deployment/monitor-delivery-policy.json']
        pool_key=hashlib.sha256(json.dumps(str(profile/'.hermes/role-pool.json'),sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        monitor_base = profile/'.hermes/cache/role-lease-monitor'/pool_key
        paths += [{'path':str(monitor_base/'delivery-policy.json'),'kind':'monitor_delivery_semantics'}]
        skills = profile/'.codex/skills'
        for name in ('complex-task-planner', 'complex-task-coordinator', 'complex-task-executor',
                     'complex-task-tester', 'complex-task-reviewer', 'feishu-progress-style'):
            paths += [path for path in (skills/name).rglob('*') if path.is_file()
                and path.suffix in ('.py', '.sh', '.md', '.service', '.timer')
                and not path.name.startswith(('test_', 'verify_'))
                and not any(part in ('__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache', '.git')
                            for part in path.parts)]
            metadata = skills/name/'agents/openai.yaml'
            if metadata.is_file():
                paths.append(metadata)
        core = profile/'.hermes/hermes-agent'
        paths += [core/name for name in ('cron/role_pool_scheduler.py', 'cron/role_reporting_policy.py',
            'cron/scheduler.py', 'cron/scheduler_tick.py', 'cron/executions.py', 'cron/jobs.py',
            'cron/occurrences.py', 'cron/scheduler_delivery.py',
            'cron/delivery_queue.py', 'hermes_cli/plugins.py', 'hermes_cli/plugins_dispatch.py', 'hermes_cli/lifecycle.py',
            'agent/api_request_hooks.py', 'agent/turn_api_request.py', 'agent/turn_api_error.py',
            'agent/turn_usage.py', 'agent/aux_accounting.py', 'agent/auxiliary_hooks.py',
            'agent/auxiliary_wire.py', 'agent/conversation_loop.py', 'agent/chat_completion_helpers.py',
            'agent/turn_api_call.py', 'agent/turn_facade.py', 'agent/turn_context.py',
            'agent/turn_response_intake.py', 'agent/turn_input.py', 'agent/native_policy_activation.py',
            'agent/agent_runtime_helpers.py',
            'agent/tool_executor.py', 'agent/relay_runtime.py', 'gateway/run_busy.py',
            'gateway/run_inbound.py', 'gateway/run_turn.py', 'gateway/plugin_busy_control.py',
            'gateway/session_context.py', 'gateway/native_turn_input.py',
            'gateway/platforms/base.py', 'gateway/platforms/event.py',
            'plugins/platforms/feishu/adapter.py', 'model_tools.py', 'tools/cronjob_tools.py') if (core/name).is_file()]
        paths += list((directory/'fixtures/observation').glob('*.json'))
        paths += [profile/'.hermes/scripts/lease_observe.py']
        paths += list((profile/'.hermes/scripts/five-roles').glob('*.sh'))
        paths += [{'path': str(profile/'.hermes/config.yaml'), 'kind': 'routing_semantics'}]
    files = []
    for value in paths:
        item = value if isinstance(value, dict) else {'path': str(value), 'kind': 'bytes'}
        path, kind = Path(item['path']).resolve(), item.get('kind', 'bytes')
        if kind not in ('bytes', 'routing_semantics', 'monitor_delivery_semantics'):
            raise ValueError('UNKNOWN_VERSION_SOURCE_KIND')
        if kind == 'monitor_delivery_semantics':
            from monitor_queue import policy
            files.append({'path':str(path),'kind':kind,'sha256':sha(policy(path.parent/'outbox'))})
            continue
        files.append({'path': str(path), 'kind': kind, 'sha256':
            sha(routing_semantics(path)) if kind == 'routing_semantics' else file_sha(path)})
    if default_sources:
        files = list({(item['path'], item['kind']): item for item in files}.values())
    files.sort(key=lambda item: item['path'])
    if not files or len({item['path'] for item in files}) != len(files):
        raise ValueError('EXACT_VERSION_SOURCES_REQUIRED')
    return {'digest': sha(files), 'files': files}


def raw_identity(value):
    """Missing, null, empty, bool, numeric and string identities stay distinct."""
    return {key: {'present': key in value, **({'type': type(value[key]).__name__, 'value': value[key]}
                                            if key in value else {})} for key in FIELDS}


def process(pid, start):
    if type(pid) is not int or pid <= 1 or start is None:
        return {'state': 'unknown', 'pid': pid, 'process_start': start}
    try:
        fields = Path('/proc', str(pid), 'stat').read_text().rsplit(') ', 1)[1].split()
        actual = fields[19]
        state = 'exited' if fields[0] in ('Z', 'X') or actual != str(start) else 'alive'
        cgroup = Path('/proc', str(pid), 'cgroup').read_text()
        return {'state': state, 'pid': pid, 'process_start': start, 'actual_start': actual, 'cgroup': cgroup}
    except (FileNotFoundError, ProcessLookupError):
        return {'state': 'exited', 'pid': pid, 'process_start': start}
    except (OSError, ValueError, IndexError):
        return {'state': 'unknown', 'pid': pid, 'process_start': start}


def service(unit):
    if not isinstance(unit, str) or not re.fullmatch(r'[A-Za-z0-9_.@:-]+\.(?:service|scope)', unit):
        return {'state': 'unknown', 'unit': unit, 'reason': 'service_identity_unverified'}
    try:
        result = subprocess.run(['systemctl', '--user', 'show', unit, '--property=LoadState,ActiveState,SubState,MainPID,ExecMainPID,ExecMainCode,ExecMainStatus,InvocationID,ControlGroup,Result'],
                                capture_output=True, text=True, timeout=5)
        if result.returncode:
            return {'state': 'unknown', 'unit': unit, 'reason': 'service_source_unavailable'}
        values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        return {'unit': unit, 'state': 'active' if values.get('ActiveState') in
                ('active', 'activating', 'deactivating') else 'exited' if values.get('ActiveState') in
                ('inactive', 'failed') else 'unknown', 'properties': values}
    except (OSError, subprocess.SubprocessError):
        return {'state': 'unknown', 'unit': unit, 'reason': 'service_source_unavailable'}


def ledger_path(pool_path):
    return Path(pool_path).resolve().with_suffix('.runtime')/'business.sqlite3'


def inspect(home, pool_path, *, roots=(), version_paths=None, at=None, acceptance_environments=None):
    """A bounded metadata inventory, never a business status repair.

    Whole source bytes are hashed, including sessions. Only structured event,
    checkpoint and control metadata are parsed; historical prose is not read
    into role context and no source filename is used to invent a task identity.
    """
    home, pool_path = Path(home).resolve(), Path(pool_path).resolve()
    result = {'schema_version': 1, 'kind': 'legacy-migration-inventory',
        'observed_at': time.time() if at is None else at, 'home': str(home), 'pool_path': str(pool_path),
        'target_database': str(ledger_path(pool_path)),
        'version': code_version(version_paths), 'sources': [], 'records': [], 'writers': [],
        'services': [], 'jobs': [], 'roots': [], 'errors': [], 'high_water': {},
        'acceptance_environments': acceptance_environments or {}}
    from migration_contracts import declared_environments
    result['acceptance_environments'] = declared_environments(result['acceptance_environments'],result['version']['digest'])
    parsed, source_map = {}, {}
    def source(path, kind, *, parse=False, navigation=False):
        path = Path(path)
        try:
            if path.is_symlink():
                if not navigation or parse or path.suffix not in ('.md', '.json'):
                    raise ValueError('business source symlink requires explicit canonical root')
                alias = path.absolute()
                canonical = path.resolve(strict=True)
                # Navigation links carry no task/owner authority. Bound them
                # to the workspace containing the explicitly accepted roots.
                allowed = (Path('/home/wang/work').resolve(), home, *found)
                if not canonical.is_file() or not any(canonical.is_relative_to(root) for root in allowed):
                    raise ValueError('navigation target outside accepted source roots')
                before = path.lstat(); target = canonical.stat()
                digest = file_sha(canonical)
                after, target_after = path.lstat(), canonical.stat()
                if ((before.st_ino, before.st_size, before.st_mtime_ns) !=
                    (after.st_ino, after.st_size, after.st_mtime_ns)
                    or (target.st_ino, target.st_size, target.st_mtime_ns) !=
                    (target_after.st_ino, target_after.st_size, target_after.st_mtime_ns)):
                    raise ValueError('navigation changed during read')
                item = {'path': str(alias), 'canonical_path': str(canonical), 'kind': 'navigation',
                    'symlink_target': os.readlink(path), 'sha256': digest, 'size': target.st_size,
                    'mtime_ns': before.st_mtime_ns, 'target_mtime_ns': target.st_mtime_ns,
                    'authority': False}
                source_map[item['path']] = item
                return None
            before = path.stat()
            digest = file_sha(path)
            value = read(path) if parse else None
            after = path.stat()
            if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                raise ValueError('source changed during read')
            item = {'path': str(path.resolve()), 'kind': kind, 'sha256': digest,
                    'size': before.st_size, 'mtime_ns': before.st_mtime_ns}
            source_map[item['path']] = item
            if parse:
                parsed[item['path']] = value
            return value
        except (OSError, ValueError, TypeError) as exc:
            result['errors'].append({'source': str(path), 'kind': kind, 'error': type(exc).__name__})
            return None
    pool = source(pool_path, 'pool', parse=True)
    if not isinstance(pool, dict) or set(pool.get('roles', {})) != set(ROLES) or type(pool.get('generation')) is not int:
        result['errors'].append({'source': str(pool_path), 'error': 'INVALID_FIVE_ROLE_POOL'})
        pool = {}
    result['pool_generation'] = pool.get('generation')
    current = pool.get('current') or {}
    result['selection'] = current
    found = {Path(root).resolve() for root in roots}
    mapping = current.get('role_roots') or {}
    if isinstance(mapping, dict):
        found.update(Path(path).resolve() for path in mapping.values() if isinstance(path, str))
    if current.get('runtime_root'):
        found.add(Path(current['runtime_root']).resolve())
    jobs_file = home/'cron/jobs.json'
    configured = {identifier for cfg in pool.get('roles', {}).values() if isinstance(cfg, dict)
                  for key, identifier in cfg.items() if key.endswith('job_id') and identifier}
    jobs_data = source(jobs_file, 'native_jobs', parse=True)
    if isinstance(jobs_data, dict) and isinstance(jobs_data.get('jobs'), list):
        from migration_contracts import job_policy
        source_map[str(jobs_file.resolve())]['semantic_sha256'] = sha(job_policy(jobs_data['jobs'], configured))
        for job in jobs_data['jobs']:
            if job.get('id') in configured:
                keys = ('id', 'enabled', 'paused', 'state', 'origin', 'deliver', 'schedule', 'role_pool', 'workdir')
                result['jobs'].append({**{key: job[key] for key in keys if key in job},
                                       'present_fields': [key for key in keys if key in job]})
        if {job['id'] for job in result['jobs']} != configured:
            result['errors'].append({'source': str(jobs_file), 'error': 'CONFIGURED_JOB_MISSING'})
    else:
        result['errors'].append({'source': str(jobs_file), 'error': 'NATIVE_JOBS_UNVERIFIED'})
    for path in sorted((home/'runtime/role-pool-executions').glob('*.json')):
        value = source(path, 'frozen_execution', parse=True)
        if isinstance(value, dict) and value.get('pool_path') == str(pool_path) and value.get('project_root'):
            found.add(Path(value['project_root']).resolve())
    native = home/'cron/executions.db'
    try:
        with sqlite3.connect(native.as_uri()+'?mode=ro', uri=True, timeout=5) as db:
            db.row_factory = sqlite3.Row
            db.execute('BEGIN')
            rows = [dict(row) for row in db.execute('SELECT * FROM executions ORDER BY id') if row['job_id'] in configured]
            db.commit()
        result['native_executions'] = rows
        result['high_water']['native_executions_sha256'] = sha(rows)
        for row in rows:
            state = process(row.get('pid'), row.get('process_started_at'))
            if state['state'] != 'exited':
                result['writers'].append({'source': str(native), 'execution_id': row['id'],
                    'job_id': row['job_id'], 'native_status': row['status'], **state})
    except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
        result['errors'].append({'source': str(native), 'error': 'NATIVE_EXECUTIONS_UNVERIFIED', 'detail': type(exc).__name__})
    # A living descendant scope can outlast both the native row and its parent.
    try:
        for proc in Path('/proc').iterdir():
            if not proc.name.isdigit():
                continue
            try:
                cgroup = (proc/'cgroup').read_text()
                matches = re.findall(r'hermes-worker-cron-([A-Za-z0-9_-]+)-exec-([a-f0-9]{32})\.scope', cgroup)
                for job, execution in matches:
                    if job in configured:
                        result['writers'].append({'source': '/proc', 'job_id': job, 'execution_id': execution,
                            'pid': int(proc.name), 'state': 'scope_present', 'cgroup': cgroup})
            except (FileNotFoundError, ProcessLookupError):
                continue
            except OSError as exc:
                result['errors'].append({'source': str(proc), 'error': type(exc).__name__})
    except OSError as exc:
        result['errors'].append({'source': '/proc', 'error': type(exc).__name__})
    for root in sorted(found):
        result['roots'].append(str(root))
        if not root.is_dir():
            result['errors'].append({'source': str(root), 'error': 'ROOT_UNAVAILABLE'})
            continue
        binding = source(root/'ROLE-BINDINGS.json', 'binding', parse=True)
        if not isinstance(binding, dict) or binding.get('project_root') != str(root):
            result['errors'].append({'source': str(root), 'error': 'ROOT_BINDING_MISMATCH'})
        paths = {path for path in root.iterdir() if path.is_file() and path.suffix in ('.md', '.json')}
        for folder in ('events', 'inbox', 'sessions', 'decision', 'receipts', 'test-receipts', 'reviews'):
            directory = root/folder
            if directory.is_dir():
                paths.update(path for path in directory.rglob('*') if path.is_file() and
                             path.suffix in ('.md', '.json', '.jsonl'))
        for path in sorted(paths):
            if path == root/'ROLE-BINDINGS.json':
                continue
            relative = path.relative_to(root).as_posix()
            navigation = ('/' not in relative and ((path.suffix == '.md' and path.name not in
                {'TODO.md', 'HANDOFF.md', 'TIMELINE.md', 'PLAN.md', 'AC.md', 'ACCEPTANCE.md'})
                or bool(re.fullmatch(r'(?:[A-Za-z0-9_-]+-)?REFERENCES\.json', path.name))))
            from migration_contracts import source_class
            value = source(path, source_class(relative), parse=path.suffix == '.json' and not (path.is_symlink() and navigation), navigation=navigation)
            item = source_map.get(str(path.resolve()))
            if not isinstance(value, dict) or not item:
                continue
            if item['kind'] == 'service_live_state':
                from migration_contracts import service_policy
                item['semantic_sha256'] = sha(service_policy(value))
            if relative.startswith(('events/', 'inbox/events/', 'inbox/control/runtime/checkpoints/',
                                    'inbox/control/requests/')):
                result['records'].append({'source': str(path.resolve()), 'source_sha256': item['sha256'],
                    'root': str(root), 'identity': raw_identity(value), 'raw': value})
            if relative.startswith('inbox/control/runtime/leases/'):
                state = process(value.get('pid'), value.get('process_start'))
                if state['state'] != 'exited':
                    result['writers'].append({'source': str(path), 'role': path.stem,
                        'owner': value.get('owner'), 'expires_at': value.get('expires_at'),
                        'token_sha256': sha(value.get('token')), **state})
            if path.name == 'state.json' and relative.startswith('reviews/'):
                spec_path = path.with_name('spec.json')
                spec = parsed.get(str(spec_path.resolve()))
                if spec is None and spec_path.is_file():
                    spec = source(spec_path, 'service_specification', parse=True)
                spec = spec if isinstance(spec, dict) else {}
                unit = value.get('unit') or spec.get('unit')
                result['services'].append({'source': str(path), 'state': value, 'identity': spec,
                                            'observation': service(unit)})
    strong_db = home/'state/complex-task-reviews/ledger.sqlite3'
    # The production default is outside Hermes HOME; only an explicitly supplied
    # actual home uses that default. Isolated homes never wander into it.
    if home == (Path.home()/'.hermes').resolve():
        strong_db = Path.home()/'.local/state/complex-task-reviews/ledger.sqlite3'
    if strong_db.is_file():
        try:
            with sqlite3.connect(strong_db.as_uri()+'?mode=ro', uri=True) as db:
                db.row_factory = sqlite3.Row
                db.execute('BEGIN')
                calls = [dict(row) for row in db.execute('SELECT * FROM calls ORDER BY id') if row['project'] in result['roots']]
                grants = [dict(row) for row in db.execute('SELECT * FROM grants ORDER BY id') if row['project'] in result['roots']]
                db.commit()
            result['high_water']['strong_review_calls_sha256'] = sha(calls)
            result['high_water']['strong_review_grants_sha256'] = sha(grants)
            result['independent_authorizations'] = grants
            for row in calls:
                if row.get('status') in ('RESERVED', 'RUNNING'):
                    result['services'].append({'source': str(strong_db), 'state': row,
                        'identity': {'kind': 'strong_review', 'call_id': row['id'], 'unit': row.get('unit')},
                        'observation': service(row.get('unit'))})
        except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
            result['errors'].append({'source': str(strong_db), 'error': type(exc).__name__})
    result['sources'] = sorted(source_map.values(), key=lambda item: item['path'])
    result['high_water'].update(legacy_sources_sha256=sha(result['sources']),
        exact_record_identities_sha256=sha([row['identity'] for row in result['records']]),
        legacy_record_count=len(result['records']),
        delivered_event_ids=[row['identity']['event_id'] for row in result['records'] if
                             row['raw'].get('status') in ('SENT', 'DELIVERED')])
    result['ready_for_reconciliation'] = not result['errors']
    return sealed(result)


def _sources_unchanged(manifest):
    changed = []
    for item in manifest['sources']:
        if item['kind'] == 'derived_observation' or item.get('semantic_sha256'):
            continue
        try:
            if file_sha(item['path']) != item['sha256']:
                changed.append(item['path'])
        except OSError:
            changed.append(item['path'])
    fresh = inspect(manifest['home'], manifest['pool_path'], roots=manifest['roots'],
                    version_paths=manifest['version']['files'],
                    acceptance_environments=manifest.get('acceptance_environments'))
    def identity(item):
        return [item.get('semantic_sha256', item['sha256']), item.get('canonical_path'), item.get('symlink_target')]
    old = {item['path']: identity(item) for item in manifest['sources'] if item['kind'] != 'derived_observation'}
    new = {item['path']: identity(item) for item in fresh['sources'] if item['kind'] != 'derived_observation'}
    changed += [path for path in set(old)|set(new) if old.get(path) != new.get(path)]
    for key in ('native_executions_sha256', 'strong_review_calls_sha256', 'strong_review_grants_sha256'):
        if manifest['high_water'].get(key) != fresh['high_water'].get(key):
            changed.append(key)
    return sorted(set(changed)), fresh


def prepare(manifest, proposal, *, database=None):
    verify_seal(manifest)
    if manifest.get('kind') != 'legacy-migration-inventory' or manifest['errors']:
        raise ValueError('INVENTORY_UNVERIFIED')
    changed, _ = _sources_unchanged(manifest)
    if changed:
        raise ValueError('LEGACY_INCREMENT_REQUIRES_NEW_INVENTORY')
    needed = {'run_id', 'requirement_id', 'requirement_root', 'repo', 'task_ids'}
    if not isinstance(proposal, dict) or not needed <= proposal.keys():
        raise ValueError('EXPLICIT_RUN_PROPOSAL_REQUIRED')
    path = Path(database).resolve() if database else ledger_path(manifest['pool_path'])
    if str(path) != manifest.get('target_database', str(ledger_path(manifest['pool_path']))):
        raise ValueError('TARGET_DATABASE_MISMATCH')
    ledger = BusinessLedger(path)
    if ledger.mode()['mode'] != 'STAGED':
        raise ValueError('PREPARE_REQUIRES_STAGED_AUTHORITY')
    identity = sha([manifest['sha256'], proposal])
    created = ledger.create_run(proposal['requirement_id'], proposal['requirement_root'],
        proposal['repo'], proposal['task_ids'], run_id=proposal['run_id'], command_id='migration-run:'+identity)
    if created.get('status') not in ('COMMITTED', 'DUPLICATE'):
        return {'status': 'REJECTED', 'stage': 'create_run', 'result': created}
    records = []
    for record in manifest['records']:
        raw = record['raw']
        task = raw.get('task') if 'task' in raw else raw.get('task_id')
        records.append({'source': record['source'], 'source_sha256': record['source_sha256'],
            'raw_source': raw, 'identity_fields': record['identity'], 'task_id': task,
            'attempt': raw.get('attempt'), 'raw_status': raw.get('status'), 'role': raw.get('role'),
            'verified': False})
    imported = ledger.stage_import(manifest, records, command_id='migration-import:'+identity)
    freeze = ledger.freeze_admission('STAGED migration preparation '+identity,
                                    command_id='migration-stage-freeze:'+identity)
    return sealed({'schema_version': 1, 'kind': 'staged-migration-proposal',
        'manifest_sha256': manifest['sha256'], 'database': str(path), 'proposal': proposal,
        'version_digest': manifest['version']['digest'], 'result': imported,
        'automatic_ack_pass_or_completion': False, 'mode': ledger.mode(),
        'unverified_records': len(records), 'admission_freeze': freeze})


def _readonly_rows(path, query, parameters=()):
    with sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True, timeout=5) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(query, parameters)]


def _version_coverage(version):
    actual = {(row['path'], row.get('kind', 'bytes')): row['sha256'] for row in version['files']}
    return [row['path'] for row in code_version()['files']
            if actual.get((row['path'], row['kind'])) != row['sha256']]


def _verify_gate_proof(gate, item, manifest, database, instant):
    """Re-read real authorities; booleans and arbitrary hashed text prove nothing.

    This is local evidence integrity, not cryptographic protection from an
    operator who can rewrite the authority databases or this verifier itself.
    """
    proof = item.get('proof')
    if not isinstance(proof, dict):
        raise ValueError('STRUCTURED_AUTHORITY_PROOF_REQUIRED')
    from migration_contracts import bind_environment
    environment = bind_environment(gate, proof, manifest)
    if gate == 'P0':
        import role_observer
        value = role_observer.collect_pool_leases(proof['pool_path'], home=proof['home'], clock=lambda: instant)
        execution = next((row for values in value['roles'].values() for role in values
            for row in role.get('executions', []) if row['execution_id'] == proof['execution_id']), None)
        if (value.get('observation_schema') != 2 or value['health'] != 'observed'
            or value.get('generation') != proof['generation'] or not execution
            or not execution['writers_present'] or not execution['scope_identity_verified']
            or not execution['writer_scope_verified']):
            raise ValueError('CURRENT_SCHEMA2_NATIVE_WRITER_PROOF_REQUIRED')
        if execution.get('run_id') != environment['run_id']:
            raise ValueError('ACCEPTANCE_ENVIRONMENT_MISMATCH:run_id')
        frozen = read(Path(proof['home'])/'runtime/role-pool-executions'/(proof['execution_id']+'.json'))
        if (frozen.get('schema_version') != 2 or frozen.get('workflow_version') != 1
            or frozen.get('runtime_version_digest') != item['version_digest']
            or any(frozen.get(key) != execution.get(key) for key in ('execution_id', 'work_id', 'run_id', 'role'))):
            raise ValueError('CURRENT_VERSION_FROZEN_EXECUTION_PROOF_REQUIRED')
        return {'observation_sha256': sha(value), 'execution_id': execution['execution_id']}
    if gate == 'T1':
        ledger = BusinessLedger(proof['database'], readonly=True)
        state = ledger.snapshot(proof['run_id'])
        candidate = next(row for row in state['candidates'] if row['candidate_id'] == proof['candidate_id'])
        task = next(row for row in state['tasks'] if row['task_id'] == candidate['task_id'])
        results = [row for row in state['verifications'] if row['candidate_id'] == candidate['candidate_id']]
        decisions = {row['decision'] for row in state['decisions'] if row['candidate_id'] == candidate['candidate_id']}
        if (task['phase'] != 'COMPLETED' or task['attempt'] != candidate['attempt']
            or not {'APPROVE', 'COMPLETE'} <= decisions
            or not set(task['required_checks']) <= {row['check_name'] for row in results if row['result'] == 'PASS'}
            or not any(row['check_name'] == 'independent_test' and row['role'] == 'TESTER' and
                row['result'] == 'PASS' and 'runner_evidence' in row['evidence'] for row in results)):
            raise ValueError('REAL_COMPLETED_CANDIDATE_LIFECYCLE_REQUIRED')
        from delivery_evidence import check_candidate_test_evidence
        for row in results:
            if row['check_name'] == 'independent_test' and row['result'] == 'PASS':
                if not check_candidate_test_evidence(row['evidence'], candidate)['valid']:
                    raise ValueError('INDEPENDENT_TEST_SOURCE_CHANGED')
                runner = read(row['evidence']['runner_evidence']['path'])
                if (runner.get('runtime_version_unchanged') is not True or any(
                    (runner.get(key) or {}).get('runtime_version_digest') != item['version_digest']
                    for key in ('before', 'after'))):
                    raise ValueError('CURRENT_VERSION_TEST_EXECUTION_REQUIRED')
        for commit in (candidate['base'], candidate['head']):
            checked = subprocess.run(['git', '-C', candidate['repo'], 'cat-file', '-e', commit+'^{commit}'],
                capture_output=True, timeout=10)
            if checked.returncode:
                raise ValueError('CANDIDATE_GIT_OBJECT_UNVERIFIED')
        return {'state_sha256': sha(state), 'candidate_id': candidate['candidate_id']}
    if gate == 'T2':
        spec = read(proof['spec_path'])
        if file_sha(proof['spec_path']) != proof['spec_sha256'] or spec.get('unit') != proof['unit']:
            raise ValueError('FROZEN_INDEPENDENT_SERVICE_SPEC_REQUIRED')
        if spec.get('run_id', spec.get('call_id')) != proof['service_id']:
            raise ValueError('SERVICE_IDENTITY_MISMATCH')
        if spec.get('runtime_version_digest') != item['version_digest']:
            raise ValueError('CURRENT_VERSION_SERVICE_SPEC_REQUIRED')
        value = service(proof['unit']); properties = value.get('properties', {})
        if (properties.get('LoadState') != 'loaded' or not properties.get('InvocationID')
            or properties.get('Result') != 'success'
            or properties.get('ExecMainCode') != '1' or properties.get('ExecMainStatus') != '0'
            or not (value['state'] == 'exited' or (properties.get('ActiveState') == 'active' and properties.get('SubState') == 'exited'))
            or int(properties.get('ExecMainPID', '0')) <= 1):
            raise ValueError('REAL_SYSTEMD_COMPLETION_REQUIRED')
        # The frozen runner spec binds a service unit, not a worker scope.
        if not proof['unit'].endswith('.service') or 'hermes-worker-cron-' in proof['unit']:
            raise ValueError('INDEPENDENT_SERVICE_UNIT_REQUIRED')
        return {'service_sha256': sha(value), 'spec_sha256': proof['spec_sha256']}
    if gate == 'M':
        home = Path(proof['home']).resolve()
        rows = _readonly_rows(home/'runtime/role-usage.db',
            'SELECT * FROM role_usage WHERE session_id=? AND physical_attempt_id=?',
            (proof['session_id'], proof['physical_attempt_id']))
        if len(rows) != 1:
            raise ValueError('EXACT_PHYSICAL_MODEL_ATTEMPT_REQUIRED')
        usage = rows[0]
        if usage.get('run_id') != environment['run_id']:
            raise ValueError('ACCEPTANCE_ENVIRONMENT_MISMATCH:run_id')
        if (usage.get('logical_request_id') != proof['logical_request_id'] or usage.get('status') != 'completed'
            or usage.get('provider_dispatched') != 1 or usage.get('identity_basis') != 'physical_hook'
            or not usage.get('ended_at') or usage['ended_at'] < usage['started_at']
            or usage.get('execution_id') != proof['execution_id'] or usage.get('task_kind') != 'business_cron'
            or usage.get('session_kind') != 'business_cron' or not usage.get('work_id') or not usage.get('run_id')
            or not usage.get('model') or not usage.get('provider')
            or usage.get('role') not in ROLES or usage.get('route') not in ('local', 'cloud')
            or Path(usage['project']).resolve() != Path(proof['controlled_project']).resolve()):
            raise ValueError('REAL_CONTROLLED_MODEL_ATTEMPT_REQUIRED')
        from migration_contracts import output_usage
        business_output_tokens = output_usage(usage)
        scope = read(home/'runtime/role-pool-executions'/(proof['execution_id']+'.json'))
        if (scope.get('schema_version') != 2 or scope.get('workflow_version') != 1
            or scope.get('runtime_version_digest') != item['version_digest'] or any(
            scope.get(key) != usage.get(key) for key in ('execution_id', 'work_id', 'run_id', 'role'))):
            raise ValueError('EXACT_FROZEN_CONTROLLED_BUSINESS_SCOPE_REQUIRED')
        if scope.get('kind') != 'business' or scope.get('session_id') != proof['session_id']:
            raise ValueError('EXACT_FROZEN_CONTROLLED_BUSINESS_SCOPE_REQUIRED')
        pool_path = Path(scope['pool_path']).resolve()
        binding = read(pool_path).get('roles', {}).get(scope['role'], {})
        if binding.get('poll_job_id') != scope.get('job_id'):
            raise ValueError('CURRENT_NATIVE_BUSINESS_JOB_BINDING_REQUIRED')
        from workflow_runtime import ledger_path
        ledger = BusinessLedger(ledger_path(pool_path), readonly=True)
        state = ledger.snapshot(scope['run_id'])
        execution = next((row for row in state['executions'] if row['execution_id'] == proof['execution_id']), None)
        work = next((row for row in state['works'] if row['work_id'] == scope['work_id']), None)
        run = next((row for row in state['runs'] if row['run_id'] == scope['run_id']), None)
        event = next((row for row in state['events'] if row['event_id'] == proof.get('business_event_id')), None)
        business_kinds = {'CHECKPOINT_SAVED', 'PLAN_PUBLISHED', 'CANDIDATE_DELIVERED',
                          'VERIFICATION_PUBLISHED', 'CANDIDATE_DECIDED', 'SERVICE_ADJUDICATED'}
        if (not execution or not work or not run or execution['state'] != 'ENDED'
            or any(execution.get(key) != scope.get(key) for key in
                ('work_id', 'role', 'generation', 'execution_generation'))
            or any(work.get(key) != scope.get(key) for key in ('work_id', 'role', 'run_id'))
            or Path(run['requirement_root']).resolve() != Path(proof['controlled_project']).resolve()
            or not event or event['kind'] not in business_kinds
            or any(event.get(key) != scope.get(key) for key in ('work_id', 'role', 'run_id'))
            or not execution.get('ended_at') or not execution['bound_at'] <= event['created_at'] <= execution['ended_at']
            or event['created_at'] < usage['ended_at']):
            raise ValueError('ACTUAL_CONTROLLED_BUSINESS_WORK_AND_RESULT_REQUIRED')
        native = _readonly_rows(home/'cron/executions.db', 'SELECT * FROM executions WHERE id=?', (proof['execution_id'],))
        if (len(native) != 1 or native[0].get('status') != 'completed'
            or native[0].get('job_id') != scope.get('job_id')
            or native[0].get('delivery_outcome') != 'suppressed'):
            raise ValueError('CONTROLLED_BUSINESS_NATIVE_COMPLETION_REQUIRED')

        # Business final output is intentionally suppressed. The channel proof
        # is a different, explicitly related native report, not a fabricated
        # cron receipt for the standalone `hermes send` business-event outlet.
        channel = proof.get('channel')
        if not isinstance(channel, dict) or channel.get('execution_id') == proof['execution_id']:
            raise ValueError('RELATED_NATIVE_REPORT_PROOF_REQUIRED')
        reports = _readonly_rows(home/'runtime/role-usage.db',
            'SELECT * FROM role_usage WHERE session_id=? AND physical_attempt_id=?',
            (channel['session_id'], channel['physical_attempt_id']))
        report_usage = reports[0] if len(reports) == 1 else {}
        if (report_usage.get('logical_request_id') != channel['logical_request_id']
            or report_usage.get('status') != 'completed' or report_usage.get('provider_dispatched') != 1
            or report_usage.get('identity_basis') != 'physical_hook' or not report_usage.get('ended_at')
            or report_usage['ended_at'] < report_usage['started_at']
            or report_usage.get('task_kind') != 'summary' or report_usage.get('session_kind') != 'summary'
            or report_usage.get('execution_id') != channel['execution_id']
            or not report_usage.get('model') or not report_usage.get('provider')
            or report_usage.get('run_id') != scope['run_id'] or report_usage.get('role') != scope['role']
            or report_usage.get('route') not in ('local', 'cloud')
            or Path(report_usage.get('project', '')).resolve() != Path(proof['controlled_project']).resolve()):
            raise ValueError('REAL_RELATED_SUMMARY_MODEL_ATTEMPT_REQUIRED')
        report_output_tokens = output_usage(report_usage)
        report_scope = read(home/'runtime/role-pool-executions'/(channel['execution_id']+'.json'))
        if (report_scope.get('schema_version') != 2 or report_scope.get('workflow_version') != 1
            or report_scope.get('kind') != 'report' or report_scope.get('runtime_version_digest') != item['version_digest']
            or report_scope.get('session_id') != channel['session_id']
            or report_scope.get('job_id') not in {binding.get('report_job_id'), binding.get('morning_report_job_id')}
            or Path(report_scope.get('pool_path', '')).resolve() != pool_path
            or any(report_scope.get(key) != report_usage.get(key) for key in ('execution_id', 'run_id', 'role'))):
            raise ValueError('EXACT_RELATED_REPORT_SCOPE_REQUIRED')
        artifact = Path(channel['input_artifact']).resolve()
        if (not artifact.is_relative_to(ledger.path.parent/'reports') or not artifact.name.endswith('.input.json')
            or file_sha(artifact) != channel['input_sha256']):
            raise ValueError('FROZEN_RELATED_REPORT_ARTIFACT_REQUIRED')
        packet = read(artifact); facts = packet['facts']; generation = packet['generation']
        from workflow_reports import _hash
        if (packet.get('workflow_version') != 1 or Path(packet.get('pool_path', '')).resolve() != pool_path
            or packet.get('role') != scope['role'] or facts.get('run_id') != scope['run_id']
            or _hash({key: value for key, value in facts.items() if key != 'facts_sha256'}) != facts.get('facts_sha256')
            or not any(value == event for value in facts.get('events', []))
            or generation.get('state') != 'GENERATED'
            or generation.get('model_request_id') != channel['logical_request_id']
            or generation.get('model') != report_usage['model'] or generation.get('route') != report_usage['route']
            or generation.get('facts_sha256') != facts.get('facts_sha256')
            or generation.get('execution', {}).get('execution_id') != channel['execution_id']
            or generation.get('execution', {}).get('job_id') != report_scope.get('job_id')
            or Path(generation.get('execution', {}).get('database', '')).resolve() != home/'cron/executions.db'
            or generation.get('notification_id') != channel['notification_id']):
            raise ValueError('RELATED_REPORT_MODEL_AND_BUSINESS_FACTS_REQUIRED')
        notice = next((row for row in state['outbox'] if row['notification_id'] == channel['notification_id']), None)
        if (not notice or notice['kind'] != 'PERIODIC_SUMMARY' or notice['state'] != 'SENT'
            or notice['role'] != scope['role'] or notice['run_id'] != scope['run_id']
            or notice['payload'].get('facts_sha256') != facts['facts_sha256']
            or notice['payload'].get('generation') != {key: value for key, value in generation.items() if key != 'notification_id'}
            or hashlib.sha256(notice['payload'].get('text', '').encode()).hexdigest() != generation.get('rendered_sha256')
            or notice['payload'].get('generation', {}).get('execution', {}).get('execution_id') != channel['execution_id']
            or notice['payload'].get('generation', {}).get('model_request_id') != channel['logical_request_id']
            or notice['receipt'].get('source') != 'native_cron'
            or notice['receipt'].get('execution_id') != channel['execution_id']
            or Path(notice['receipt'].get('native_home', '')).resolve() != home
            or notice['receipt'].get('acknowledgement_verified') is not True
            or notice['receipt'].get('rendered_sha256') != generation.get('rendered_sha256')
            or notice['receipt'].get('content_sha256') != generation.get('rendered_sha256')):
            raise ValueError('CONFIRMED_NATIVE_REPORT_OUTBOX_REQUIRED')
        report_native = _readonly_rows(home/'cron/executions.db', 'SELECT * FROM executions WHERE id=?', (channel['execution_id'],))
        if (len(report_native) != 1
            or not (report_native[0].get('delivery_outcome') == 'delivered' or
                report_native[0].get('delivery_outcome') == 'queued' and notice['receipt'].get('stage') == 'native_queue_terminal')
            or report_native[0].get('status') != 'completed' or report_native[0].get('job_id') != report_scope.get('job_id')):
            raise ValueError('REAL_CONFIRMED_NATIVE_CHANNEL_DELIVERY_REQUIRED')
        delivery = _readonly_rows(home/'cron/deliveries.db', 'SELECT * FROM deliveries WHERE execution_id=?', (channel['execution_id'],))
        # The original queue privacy-prunes terminal content/job_json. The
        # actual sending boundary's hash above proves the body, while this
        # table independently proves the original queue's terminal outcome.
        if len(delivery) != 1 or delivery[0].get('status') != 'delivered' or not delivery[0].get('finished_at'):
            raise ValueError('ACTUAL_GATEWAY_DELIVERY_RECEIPT_REQUIRED')
        return {'usage_identity_sha256': sha(usage), 'native_execution_sha256': sha(native[0]),
                'business_execution_sha256': sha(execution), 'business_event_sha256': sha(event),
                'report_usage_sha256': sha(report_usage), 'report_scope_sha256': sha(report_scope),
                'report_native_sha256': sha(report_native[0]), 'report_artifact_sha256': channel['input_sha256'],
                'report_notification_sha256': sha(notice), 'delivery_sha256': sha(delivery[0])}
    if gate == 'R':
        if not isinstance(proof.get('reviewed_by'), str) or not proof['reviewed_by'].strip():
            raise ValueError('EXPLICIT_RECONCILIATION_REVIEWER_REQUIRED')
        resolutions = proof.get('resolutions')
        expected = {sha([row['source'], row['source_sha256'], row['identity']]) for row in manifest['records']}
        if not isinstance(resolutions, list) or {row.get('record_sha256') for row in resolutions} != expected or len(resolutions) != len(expected):
            raise ValueError('EVERY_RAW_IDENTITY_REQUIRES_EXPLICIT_RECONCILIATION')
        tasks = BusinessLedger(database, readonly=True).snapshot()['tasks']
        for row in resolutions:
            if row.get('excluded') is True and isinstance(row.get('reason'), str) and row['reason'].strip():
                continue
            task = next((task for task in tasks if task['run_id'] == row.get('run_id') and task['task_id'] == row.get('task_id')), None)
            if not task or type(row.get('attempt')) is not int or task['attempt'] != row['attempt']:
                raise ValueError('EXPLICIT_INTEGER_ATTEMPT_AND_TASK_MAPPING_REQUIRED')
        return {'resolutions_sha256': sha(resolutions), 'reviewed_by': proof['reviewed_by']}
    raise ValueError('UNKNOWN_GATE')


def validate(manifest, evidence, *, database=None, at=None, max_observation_age=180, _resuming=None):
    verify_seal(manifest)
    problems, receipts = [], {}
    target = Path(database).resolve() if database else ledger_path(manifest['pool_path'])
    if str(target) != manifest.get('target_database', str(ledger_path(manifest['pool_path']))):
        raise ValueError('TARGET_DATABASE_MISMATCH')
    paths = manifest['version']['files']
    version = code_version(paths)
    if version['digest'] != manifest['version']['digest']:
        problems.append('CODE_VERSION_CHANGED')
    missing_sources = _version_coverage(version)
    if missing_sources:
        problems.append('EXACT_RUNTIME_VERSION_COVERAGE_REQUIRED')
    changed, fresh = _sources_unchanged(manifest)
    if changed:
        problems.append('LEGACY_INCREMENT_REQUIRES_NEW_INVENTORY')
    if fresh['errors']:
        problems.append('CURRENT_INVENTORY_UNVERIFIED')
    if fresh['writers']:
        problems.append('LEGACY_WRITER_ALIVE_OR_UNKNOWN')
    if any(item['observation']['state'] == 'unknown' for item in fresh['services']):
        problems.append('INDEPENDENT_SERVICE_UNVERIFIED')
    instant = time.time() if at is None else at
    path = Path(database).resolve() if database else ledger_path(manifest['pool_path'])
    for path in evidence:
        try:
            item = read(path)
            gate = item.get('gate')
            if gate not in GATES or gate in receipts:
                raise ValueError('duplicate/unknown acceptance gate')
            if item.get('schema_version') != 1 or item.get('result') != 'PASS' or item.get('version_digest') != version['digest']:
                raise ValueError('acceptance result/version mismatch')
            artifacts = item.get('artifacts')
            if not isinstance(artifacts, list) or not artifacts:
                raise ValueError('hashed acceptance artifacts required')
            for artifact in artifacts:
                if file_sha(artifact['path']) != artifact['sha256']:
                    raise ValueError('acceptance artifact changed')
            if gate == 'P0' and (item.get('observation_schema') != 2 or item.get('real_execution_observed') is not True
                    or type(item.get('observed_at')) not in (int, float)
                    or not 0 <= instant-item['observed_at'] <= max_observation_age):
                raise ValueError('fresh real schema-2 observation required')
            if gate == 'M' and (item.get('real_model') is not True or item.get('real_channel') is not True
                               or item.get('controlled_scope') is not True):
                raise ValueError('controlled real model/channel acceptance required')
            if gate == 'T1' and item.get('real_business_lifecycle') is not True:
                raise ValueError('real business lifecycle acceptance required')
            if gate == 'T2' and item.get('real_systemd') is not True:
                raise ValueError('real independent service acceptance required')
            if gate == 'R' and (item.get('inventory_manifest_sha256') != manifest['sha256']
                               or item.get('imports_reconciled') is not True):
                raise ValueError('review of this exact inventory/import proposal required')
            checked = _verify_gate_proof(gate, item, manifest,
                Path(database).resolve() if database else ledger_path(manifest['pool_path']), instant)
            receipts[gate] = {'path': str(Path(path).resolve()), 'sha256': file_sha(path), 'authority_proof': checked}
        except (OSError, ValueError, TypeError, KeyError, StopIteration, sqlite3.Error, subprocess.SubprocessError) as exc:
            problems.append('ACCEPTANCE_EVIDENCE_UNVERIFIED:'+str(path)+':'+type(exc).__name__+':'+str(exc))
    problems += ['MISSING_GATE:'+gate for gate in GATES if gate not in receipts]
    path = Path(database).resolve() if database else ledger_path(manifest['pool_path'])
    try:
        ledger = BusinessLedger(path, readonly=True)
        state = ledger.export_state()['state']
        resumed = (_resuming and state['mode'] == 'ACTIVE' and state.get('admission_open') == 0
                   and state['generation'] == _resuming['generation']+1)
        if state['mode'] != 'STAGED' and not resumed:
            problems.append('AUTHORITY_NOT_STAGED')
        if any(item.get('state') in ('ACTIVE', 'DRAINING') for item in state['executions']):
            problems.append('NEW_WRITER_NOT_CLEARED')
        if any(item.get('phase') is None or item.get('unresolved_reason') for item in state['tasks']):
            problems.append('IMPORT_RECONCILIATION_INCOMPLETE')
        if not state['runs']:
            problems.append('ACCEPTED_RUN_MISSING')
        for run in state['runs']:
            tasks = {task['task_id'] for task in state['tasks'] if task['run_id'] == run['run_id']}
            if tasks != set(run['task_ids']) or run.get('plan_version') is None:
                problems.append('EXACT_ACCEPTED_PLAN_MISSING:'+run['run_id'])
        authority = {key: state.get(key) for key in ('mode', 'generation', 'current_run_id', 'admission_open')}
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        problems.append('STAGED_LEDGER_UNVERIFIED:'+type(exc).__name__)
        authority = None
    if code_version(paths)['digest'] != version['digest'] or _version_coverage(version):
        problems.append('CODE_CHANGED_DURING_ACCEPTANCE')
    return sealed({'schema_version': 1, 'kind': 'migration-readiness', 'ready': not problems,
        'checked_at': instant, 'version_digest': version['digest'], 'manifest_sha256': manifest['sha256'],
        'database': str(path), 'authority': authority, 'gates': receipts,
        'problems': problems, 'changed_sources': changed, 'writers': fresh['writers'],
        'current_services': fresh['services'],
        'live_source_observations': [item for item in fresh['sources']
            if item['kind'] in ('derived_observation', 'service_live_state')]})


def activate(manifest, evidence, *, database=None, at=None):
    """Repeat the entire readiness check under the existing pool admission lock."""
    pool = Path(manifest['pool_path']).resolve()
    with locked(pool.with_suffix('.admission.lock')):
        path = Path(database).resolve() if database else ledger_path(pool)
        journal_path = path.parent/'migration-activation.json'
        journal = read(journal_path) if journal_path.exists() else None
        if journal:
            verify_seal(journal)
            if journal['manifest_sha256'] != manifest['sha256']:
                return {'status': 'REJECTED', 'code': 'DIFFERENT_ACTIVATION_REQUIRES_RECONCILIATION'}
            if journal.get('state') == 'OPENED':
                actual = BusinessLedger(path, readonly=True).mode()
                return {'status': 'DUPLICATE' if actual['mode'] == 'ACTIVE' and
                        actual['generation'] == journal['generation']+1 else 'REJECTED', 'journal': journal}
        readiness = validate(manifest, evidence, database=database, at=at,
                             _resuming=journal['readiness']['authority'] if journal else None)
        if not readiness['ready']:
            return {'status': 'REJECTED', 'code': 'MIGRATION_GATES_NOT_CLEAR', 'readiness': readiness}
        ledger = BusinessLedger(readiness['database'])
        if not journal:
            journal = sealed({'schema_version': 1, 'kind': 'migration-activation-journal', 'state': 'PREPARED',
                'manifest_sha256': manifest['sha256'], 'generation': readiness['authority']['generation'],
                'readiness': readiness})
            save_json(journal_path, journal)
        frozen = ledger.freeze_admission('Validated migration activation '+manifest['sha256'],
            command_id='migration-activation-freeze:'+manifest['sha256'])
        if frozen['status'] not in ('COMMITTED', 'DUPLICATE'):
            return {'status': 'REJECTED', 'code': 'ACTIVATION_FREEZE_FAILED', 'result': frozen}
        accepted = {**journal['readiness'], 'validated': True}
        result = ledger.set_mode('ACTIVE', expected_generation=journal['generation'],
            migration_manifest=accepted, command_id='migration-activate:'+manifest['sha256'])
        if result['status'] not in ('COMMITTED', 'DUPLICATE'):
            return {'status': 'REJECTED', 'code': 'ACTIVATION_MODE_SWITCH_FAILED', 'result': result}
        opened = ledger.open_admission(expected_generation=journal['generation']+1,
            migration_manifest=accepted, command_id='migration-open:'+manifest['sha256'])
        if opened['status'] not in ('COMMITTED', 'DUPLICATE'):
            return {'status': 'REJECTED', 'code': 'ACTIVATION_REMAINS_FROZEN', 'result': opened}
        journal = sealed({**{key: value for key, value in journal.items() if key != 'sha256'},
                          'state': 'OPENED', 'activation': result, 'opened': opened})
        save_json(journal_path, journal)
        return {'status': result['status'], 'readiness': readiness, 'activation': result, 'admission': opened,
                'legacy_credentials_restored': False}


def export(pool_path, *, after=0, freeze=False, reason=None, at=None):
    """Preserve the final state AND the exact increment since a prior high water.

    --freeze closes new admission persistently. Existing results remain governed
    by allow_drain; active or unknown writers make the export provisional.
    """
    if type(after) is not int or after < 0:
        raise ValueError('INVALID_EVENT_HIGH_WATER')
    pool_path = Path(pool_path).resolve()
    ledger = BusinessLedger(ledger_path(pool_path), readonly=not freeze)
    freeze_result = None
    if freeze:
        if not reason:
            raise ValueError('FREEZE_REASON_REQUIRED')
        freeze_result = ledger.freeze_admission(reason, command_id='migration-freeze:'+sha([str(pool_path), reason]))
    value = ledger.export_state()
    state = value['state']
    if after > state['event_high_water']:
        raise ValueError('EXPORT_HIGH_WATER_AHEAD_OF_LEDGER')
    events = state.get('events')
    if events is None:
        raise ValueError('CONSISTENT_EVENT_EXPORT_UNAVAILABLE')
    conflicts = []
    for execution in state['executions']:
        if execution.get('state') in ('ACTIVE', 'DRAINING'):
            conflicts.append({'code': 'WRITER_NOT_CLEARED', 'execution_id': execution['execution_id'],
                              'observation': process(execution['pid'], execution['process_start'])})
    if state.get('admission_open') != 0:
        conflicts.append({'code': 'NEW_ADMISSION_NOT_FROZEN'})
    final_high_water = state['event_high_water']
    # Preserve later arrivals separately. They cannot be claimed to be part of
    # the frozen export, and a rollback preview will force a new final export.
    tail = ledger.events(after=final_high_water, limit=10000)
    if tail:
        conflicts.append({'code': 'EVENTS_AFTER_EXPORT_HIGH_WATER', 'through': tail[-1]['event_id']})
    return sealed({'schema_version': 1, 'kind': 'workflow-final-export',
        'pool_path': str(pool_path), 'database': str(ledger.path),
        'exported_at': time.time() if at is None else at, 'freeze': freeze_result,
        'generation': state['generation'], 'from_exclusive': after, 'through_inclusive': final_high_water,
        'increment': [event for event in events if after < event['event_id'] <= final_high_water],
        'state': state, 'state_sha256': value['sha256'], 'arrivals_after_high_water': tail,
        'final': not conflicts, 'conflicts': conflicts, 'never_restore_database_backup': True})


def rollback_preview(exported, target_contract):
    """Read-only adapter preview. No mode change, DB replacement or token reuse."""
    verify_seal(exported)
    verify_seal(target_contract)
    conflicts = list(exported.get('conflicts', []))
    if exported.get('kind') != 'workflow-final-export' or not exported.get('final'):
        conflicts.append({'code': 'FINAL_FROZEN_EXPORT_REQUIRED'})
    target = target_contract
    if (target.get('kind') != 'verified-legacy-adapter' or target.get('tested') is not True
            or not LEGACY_CAPABILITIES <= set(target.get('capabilities', []))):
        conflicts.append({'code': 'OLD_FORMAT_CANNOT_EXPRESS_CURRENT_FACTS'})
    for artifact in target.get('artifacts', []):
        try:
            if file_sha(artifact['path']) != artifact['sha256']:
                raise ValueError('adapter changed')
        except (OSError, ValueError, KeyError, TypeError):
            conflicts.append({'code': 'LEGACY_ADAPTER_ARTIFACT_UNVERIFIED'})
    if not target.get('artifacts'):
        conflicts.append({'code': 'LEGACY_ADAPTER_ARTIFACT_MISSING'})
    state = exported['state']
    documents, identities = [], []
    for task in state['tasks']:
        identity = {key: task.get(key) for key in ('run_id', 'task_id', 'attempt', 'role')}
        identities.append(identity)
        if task.get('phase') not in PHASE_MAP or task.get('unresolved_reason') or type(task.get('attempt')) is not int:
            conflicts.append({'code': 'TASK_RECONCILIATION_UNRESOLVED', **identity})
            continue
        documents.append({'identity': identity, 'legacy_status': PHASE_MAP[task['phase']],
            'scope': task['scope'], 'base': task['base'], 'ac_version': task['ac_version'],
            'dependencies': task['dependencies'], 'required_checks': task['required_checks'],
            'evidence_source': 'immutable migration identity sidecar'})
    for action in state.get('external_actions', []):
        if action.get('state') in ('UNKNOWN', 'SUBMITTING'):
            conflicts.append({'code': 'UNKNOWN_EXTERNAL_SIDE_EFFECT', 'action_id': action['action_id']})
    ledger = BusinessLedger(exported['database'], readonly=True)
    actual = ledger.mode()
    if actual['generation'] != exported['generation'] or actual.get('admission_open') != 0:
        conflicts.append({'code': 'AUTHORITY_CHANGED_AFTER_EXPORT'})
    late = ledger.events(after=exported['through_inclusive'], limit=10000)
    if late:
        conflicts.append({'code': 'NEW_EVENTS_REQUIRE_INCREMENTAL_EXPORT', 'through': late[-1]['event_id']})
    if ledger.export_state()['sha256'] != exported['state_sha256']:
        conflicts.append({'code': 'STATE_OR_RECEIPTS_CHANGED_AFTER_EXPORT'})
    return sealed({'schema_version': 1, 'kind': 'legacy-rollback-preview', 'ready': not conflicts,
        'export_sha256': exported['sha256'], 'target_contract_sha256': target['sha256'],
        'expected_generation': exported['generation'], 'next_generation': exported['generation']+1,
        'documents': documents, 'identity_sidecar': {'identities': identities,
            'works': state['works'], 'candidates': state['candidates'], 'verifications': state['verifications'],
            'controls': state['controls'], 'events': state.get('events', []), 'outbox': state['outbox'],
            'services': state.get('external_actions', []), 'roles': state['roles']},
        'conflicts': conflicts, 'writes_performed': False, 'old_tokens_restored': False,
        'next_step': '核验旧adapter后按当前generation切换；不得覆盖库、复用旧token或重放已送达通知。'})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    p = sub.add_parser('inspect'); p.add_argument('--home', required=True); p.add_argument('--pool', required=True)
    p.add_argument('--root', action='append', default=[]); p.add_argument('--version-source', action='append')
    p.add_argument('--acceptance-environments', help='JSON mapping of explicitly allowed isolated gate environments')
    p = sub.add_parser('prepare'); p.add_argument('--manifest', required=True); p.add_argument('--proposal', required=True)
    p.add_argument('--database')
    p = sub.add_parser('reconcile'); p.add_argument('--manifest', required=True); p.add_argument('--review', required=True)
    p.add_argument('--import-id', required=True); p.add_argument('--run', required=True); p.add_argument('--database')
    for action in ('validate', 'activate'):
        p = sub.add_parser(action); p.add_argument('--manifest', required=True); p.add_argument('--evidence', action='append', default=[])
        p.add_argument('--database')
    p = sub.add_parser('export'); p.add_argument('--pool', required=True); p.add_argument('--after', type=int, default=0)
    p.add_argument('--freeze', action='store_true'); p.add_argument('--reason')
    p = sub.add_parser('rollback-preview'); p.add_argument('--export', dest='export_path', required=True)
    p.add_argument('--target-contract', required=True)
    for p in sub.choices.values():
        p.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == 'inspect':
            result = inspect(args.home, args.pool, roots=args.root, version_paths=args.version_source,
                acceptance_environments=read(args.acceptance_environments) if args.acceptance_environments else None)
        elif args.action == 'prepare':
            result = prepare(read(args.manifest), read(args.proposal), database=args.database)
        elif args.action == 'reconcile':
            manifest = verify_seal(read(args.manifest))
            changed, fresh = _sources_unchanged(manifest)
            if changed or fresh['errors'] or fresh['writers']:
                raise ValueError('FINAL_INVENTORY_AND_WRITER_CLEARANCE_REQUIRED')
            target = Path(args.database).resolve() if args.database else ledger_path(manifest['pool_path'])
            if str(target) != manifest['target_database']:
                raise ValueError('TARGET_DATABASE_MISMATCH')
            from migration_reconciliation import accept
            review = read(args.review)
            result = accept(BusinessLedger(target), args.import_id, args.run, review,
                command_id='migration-reconcile:'+sha([manifest['sha256'], review]))
        elif args.action in ('validate', 'activate'):
            function = validate if args.action == 'validate' else activate
            result = function(read(args.manifest), args.evidence, database=args.database)
        elif args.action == 'export':
            result = export(args.pool, after=args.after, freeze=args.freeze, reason=args.reason)
        else:
            result = rollback_preview(read(args.export_path), read(args.target_contract))
        if args.output:
            save_json(args.output, result)
            os.chmod(args.output, 0o600)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result.get('ready') is False or result.get('status') == 'REJECTED' else 0
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        print(json.dumps({'status': 'REJECTED', 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
