"""Target-relative acceptance bindings and migration source semantics."""
from pathlib import Path
import hashlib
import json

GATE_FIELDS = {'P0': ('home', 'pool_path', 'database', 'run_id'),
               'T1': ('database', 'run_id'), 'T2': ('spec_path', 'unit', 'service_id'),
               'M': ('home', 'controlled_project', 'run_id')}
ENTRY_CONTRACTS = {'P0':'role-observer/schema-2', 'T1':'frozen-native-test/v1',
                   'T2':'independent-systemd/v1', 'M':'native-provider-report-delivery/v1'}
PATH_FIELDS = frozenset({'home', 'pool_path', 'database', 'controlled_project', 'spec_path'})
DERIVED_FOLDERS = ('inbox/control/runtime/activity/', 'inbox/control/runtime/dispatch-index/',
                   'inbox/control/runtime/observer/', 'inbox/control/runtime/snapshots/',
                   'inbox/control/runtime/ticks/')
JOB_RUNTIME_FIELDS = frozenset({'last_run_at', 'last_status', 'last_error', 'last_delivery_error',
    'last_fire_error', 'last_run_status', 'last_finished_at', 'last_delivery_status', 'next_run_at',
    'pending_slot', 'run_count', 'last_executed_at'})


def canonical(value):
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError('ABSOLUTE_ACCEPTANCE_PATH_REQUIRED')
    return str(Path(value).resolve())


def declared_environments(environments, version_digest):
    result = {}
    if not isinstance(environments,dict):
        raise ValueError('ACCEPTANCE_ENVIRONMENT_MAP_REQUIRED')
    for gate, value in environments.items():
        if gate not in GATE_FIELDS or not isinstance(value,dict):
            raise ValueError('UNKNOWN_ACCEPTANCE_ENVIRONMENT')
        env = dict(value)
        for key in GATE_FIELDS[gate]:
            if key not in env or not isinstance(env[key],str) or not env[key]:
                raise ValueError('INCOMPLETE_ACCEPTANCE_ENVIRONMENT:'+key)
            if key in PATH_FIELDS:
                env[key] = canonical(env[key])
        sources = []
        if gate == 'P0': sources.append(Path(env['pool_path']))
        if gate == 'T2': sources.append(Path(env['spec_path']))
        if gate == 'M': sources.append(Path(env['home'])/'config.yaml')
        env.update(runtime_version_digest=version_digest,entry_contract=ENTRY_CONTRACTS[gate],
                   configuration_sources={str(p):hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None for p in sources})
        result[gate] = env
    return result


def output_usage(row):
    try:
        usage=json.loads(row.get('usage_json') or '{}')
        count=usage.get('output_tokens')
        if row.get('usage_present') != 1 or type(count) is not int or count < 0:
            raise ValueError('M_OUTPUT_USAGE_UNVERIFIED')
    except (ValueError,TypeError) as exc:
        raise ValueError('M_OUTPUT_USAGE_UNVERIFIED') from exc
    if count > 1024:
        raise ValueError('M_OUTPUT_BUDGET_EXCEEDED')
    return count


def bind_environment(gate, proof, manifest):
    if gate == 'R':
        return None
    env = manifest.get('acceptance_environments', {}).get(gate)
    if not isinstance(env, dict) or env.get('purpose') != 'isolated_acceptance':
        raise ValueError('DECLARED_ACCEPTANCE_ENVIRONMENT_REQUIRED:'+gate)
    version=manifest.get('version',{}).get('digest')
    if version and (env.get('runtime_version_digest') != version or env.get('entry_contract') != ENTRY_CONTRACTS[gate]):
        raise ValueError('ACCEPTANCE_ENTRY_VERSION_MISMATCH')
    for path, expected in env.get('configuration_sources',{}).items():
        source=Path(canonical(path))
        actual=hashlib.sha256(source.read_bytes()).hexdigest() if source.is_file() else None
        if actual != expected:
            raise ValueError('ACCEPTANCE_CONFIGURATION_CHANGED:'+path)
    for key in GATE_FIELDS[gate]:
        value = env.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError('INCOMPLETE_ACCEPTANCE_ENVIRONMENT:'+key)
        expected = canonical(value) if key in PATH_FIELDS else value
        if key == 'database' and gate == 'P0':
            actual = str(Path(proof['pool_path']).resolve().with_suffix('.runtime')/'business.sqlite3')
        elif key == 'run_id' and gate in ('P0', 'M'):
            continue  # Bound against the observed authority below, not model text.
        else:
            actual = canonical(proof.get(key)) if key in PATH_FIELDS else proof.get(key)
        if actual != expected:
            raise ValueError('ACCEPTANCE_ENVIRONMENT_MISMATCH:'+key)
    if proof.get('database') is not None and env.get('database') is not None:
        if canonical(proof['database']) != canonical(env['database']):
            raise ValueError('ACCEPTANCE_ENVIRONMENT_MISMATCH:database')
    return env


def source_class(relative):
    if relative.startswith(DERIVED_FOLDERS):
        return 'derived_observation'
    if relative.startswith('reviews/') and relative.endswith('/state.json'):
        return 'service_live_state'
    return 'legacy_metadata'


def job_policy(jobs, configured):
    return sorted(({key: value for key, value in job.items() if key not in JOB_RUNTIME_FIELDS}
                   for job in jobs if job.get('id') in configured), key=lambda job: job['id'])


# Only known sampling fields are volatile. Identity, status/terminal result,
# runner binding and unknown fields remain stable business/service facts.
def service_policy(value):
    return {key: item for key, item in value.items() if key not in
            {'updated_at', 'heartbeat_at', 'last_seen', 'elapsed_seconds', 'progress', 'poll_count'}}
