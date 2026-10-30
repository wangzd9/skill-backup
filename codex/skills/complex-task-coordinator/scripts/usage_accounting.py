"""Role-session usage ledger. No prompts, credentials or external tool usage."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit, urlunsplit

from time_accounting import epoch, iso

ROLES = {'COORDINATOR', 'A', 'B', 'C', 'TESTER'}
TOKEN_FIELDS = ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens', 'reasoning_tokens')
ALL_TOKEN_FIELDS = (*TOKEN_FIELDS, 'prompt_tokens', 'total_tokens')
ATTRIBUTION_FIELDS = ('run_id', 'work_id', 'task_id', 'business_attempt', 'execution_id')


def known_usage(row):
    """Shared normalized amounts/presence; unknown legacy cache stays unknown."""
    keys=set(row.keys())
    measured=json.loads(row['usage_json']) if row['usage_present'] else {}
    presence=json.loads(row['field_presence_json']) if 'field_presence_json' in keys and row['field_presence_json'] else None
    if presence is None:
        presence={key:key in measured and key not in ('cache_read_tokens','cache_write_tokens','reasoning_tokens')
                  for key in ALL_TOKEN_FIELDS}
    values={key:measured.get(key) if presence.get(key) and type(measured.get(key)) is int
            and measured[key]>=0 else None for key in ALL_TOKEN_FIELDS}
    return values,{key:value is not None for key,value in values.items()}


def business_attributed(row):
    keys=set(row.keys())
    required=ATTRIBUTION_FIELDS
    # Coordination work is intentionally taskless. One supplied task field
    # without the other remains incomplete, rather than silently optional.
    if (row['role']=='COORDINATOR' and 'task_id' in keys and 'business_attempt' in keys
            and row['task_id'] in ('',None) and row['business_attempt'] is None):
        required=('run_id','work_id','execution_id')
    return all(key in keys and row[key] not in ('',None) for key in required)


def endpoint_key(value):
    """Strip credentials/query strings and compare actual configured endpoints."""
    parsed = urlsplit(str(value or ''))
    if not parsed.hostname:
        return ''
    host = parsed.hostname.lower()
    if ':' in host:
        host = '[' + host + ']'
    if parsed.port:
        host += ':' + str(parsed.port)
    return urlunsplit((parsed.scheme.lower(), host, parsed.path.rstrip('/'), '', ''))


def classify_route(base_url, model, config):
    """An exact endpoint+model match is required; provider names are not geography."""
    actual = (endpoint_key(base_url), str(model or ''))
    matches = set()
    for route, endpoint, configured_model in config:
        if actual[0] and actual == (endpoint_key(endpoint), str(configured_model or '')):
            matches.add(route)
    route = next(iter(matches)) if len(matches) == 1 else 'unknown'
    alias = hashlib.sha256(actual[0].encode()).hexdigest()[:16] if actual[0] else ''
    return route, alias


def request_identity(index_path, *, session_id, physical_attempt_id='', api_request_id=''):
    """Read the exact attempt's frozen attribution without creating a ledger.

    Rebinding a group while a request is in flight must not move its end event
    to another project. A physical ID is never matched by logical ID alone.
    Logical-ID disagreement remains an error rather than joining two requests.
    """
    path = Path(index_path).resolve()
    if not path.is_file() or not session_id or not api_request_id:
        return None
    physical = physical_attempt_id or ('legacy:' + str(api_request_id))
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1) as db:
        db.row_factory = sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='role_usage'").fetchone():
            return None
        row = db.execute('SELECT * FROM role_usage WHERE session_id=? AND physical_attempt_id=?',
                         (str(session_id), str(physical))).fetchone()
    if row is None:
        return None
    if row['logical_request_id'] != str(api_request_id):
        raise ValueError('physical attempt identity conflict: logical_request_id')
    keys = set(row.keys())
    task = row['task_kind']
    return {'role': row['role'], 'project': row['project'],
            'parent_session_id': row['parent_session_id'] if 'parent_session_id' in keys else '',
            'session_kind': row['session_kind'] if 'session_kind' in keys else 'unknown',
            'aux_task': task[4:] if task.startswith('aux:') else '',
            'route': row['route'], 'endpoint_alias': row['endpoint_alias'],
            **{key: row[key] if key in keys else None if key == 'business_attempt' else ''
               for key in ATTRIBUTION_FIELDS},
            'provider_dispatched': (bool(row['provider_dispatched']) if row['provider_dispatched'] is not None else None)
                if 'provider_dispatched' in keys else None}


@contextmanager
def _database(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=1)
    db.row_factory = sqlite3.Row
    try:
        # Serialize schema discovery and ALTER across natural concurrent hooks.
        db.execute('BEGIN IMMEDIATE')
        db.execute('''CREATE TABLE IF NOT EXISTS role_usage (
            session_id TEXT NOT NULL, physical_attempt_id TEXT NOT NULL,
            logical_request_id TEXT NOT NULL, role TEXT NOT NULL, project TEXT NOT NULL,
            task_kind TEXT NOT NULL, model TEXT NOT NULL, provider TEXT NOT NULL,
            endpoint_alias TEXT NOT NULL, route TEXT NOT NULL,
            started_at REAL NOT NULL, ended_at REAL, status TEXT NOT NULL,
            usage_present INTEGER NOT NULL, usage_json TEXT,
            identity_basis TEXT NOT NULL,
            parent_session_id TEXT NOT NULL DEFAULT '',
            session_kind TEXT NOT NULL DEFAULT 'unknown',
            PRIMARY KEY(session_id,physical_attempt_id))''')
        columns = {row['name'] for row in db.execute('PRAGMA table_info(role_usage)')}
        for name, declaration in [('parent_session_id', "TEXT NOT NULL DEFAULT ''"),
                                  ('session_kind', "TEXT NOT NULL DEFAULT 'unknown'"),
                                  ('run_id', "TEXT NOT NULL DEFAULT ''"),
                                  ('work_id', "TEXT NOT NULL DEFAULT ''"),
                                  ('task_id', "TEXT NOT NULL DEFAULT ''"),
                                  ('business_attempt', 'INTEGER'),
                                  ('execution_id', "TEXT NOT NULL DEFAULT ''"),
                                  ('provider_dispatched', 'INTEGER'),
                                  ('normalization_version', "TEXT NOT NULL DEFAULT 'legacy-presence-unknown'"),
                                  ('field_presence_json', 'TEXT')]:
            if name not in columns:
                db.execute('ALTER TABLE role_usage ADD COLUMN '+name+' '+declaration)
        db.execute('CREATE INDEX IF NOT EXISTS role_usage_window ON role_usage(project,role,ended_at)')
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _usage(value):
    if not isinstance(value, dict) or not all(k in value for k in ('input_tokens', 'output_tokens')):
        return None
    result = {}
    presence = value.get('field_presence')
    if presence is not None and (not isinstance(presence, dict) or
                                any(not isinstance(v, bool) for v in presence.values())):
        raise ValueError('invalid usage field presence')
    for key in TOKEN_FIELDS:
        number = value.get(key)
        if number is None or presence is not None and not presence.get(key, False):
            result[key] = None
            continue
        if isinstance(number, bool) or not isinstance(number, int) or number < 0:
            raise ValueError('invalid token bucket: ' + key)
        result[key] = number
    # Canonical input excludes cache buckets; reasoning is a subset of output.
    for key, parts in [('prompt_tokens', ('input_tokens', 'cache_read_tokens', 'cache_write_tokens')),
                       ('total_tokens', ('prompt_tokens', 'output_tokens'))]:
        explicit = value.get(key)
        if explicit is not None and (presence is None or presence.get(key, False)):
            if isinstance(explicit, bool) or not isinstance(explicit, int) or explicit < 0:
                raise ValueError('invalid token bucket: ' + key)
            result[key] = explicit
        else:
            if presence is None:
                # Pre-presence callers use CanonicalUsage's established numeric
                # defaults. Preserve their recorded normalized totals, while
                # raw cache observability remains unknown in field coverage.
                result[key] = sum(result[k] or 0 for k in parts)
            else:
                result[key] = sum(result[k] for k in parts) if all(result[k] is not None for k in parts) else None
    if all(value is None for value in result.values()):
        return None
    return result


def record(index_path, kind, *, session_id, api_request_id, role, project,
           physical_attempt_id='', attempt_started_at=None, started_at=None,
           ended_at=None, model='', provider='', route='unknown', endpoint_alias='',
           usage=None, aux_task='', retry_count=None, error=None,
           parent_session_id='', session_kind='unknown', run_id='', work_id='', task_id='',
           business_attempt=None, execution_id='', provider_dispatched=None, **_ignored):
    if role not in ROLES or not session_id or not api_request_id:
        raise ValueError('missing registered role/request identity')
    if kind not in {'start', 'end', 'error'} or route not in {'local', 'cloud', 'unknown'}:
        raise ValueError('invalid usage event')
    if session_kind not in {'group_chat', 'business_cron', 'summary', 'unknown'}:
        raise ValueError('invalid role session purpose')
    if business_attempt is not None and (isinstance(business_attempt, bool) or not isinstance(business_attempt, int) or business_attempt < 1):
        raise ValueError('invalid business attempt')
    if provider_dispatched is not None and not isinstance(provider_dispatched, bool):
        raise ValueError('invalid provider dispatch presence')
    # Old in-flight core workers lack physical IDs. Retain their logical record,
    # explicitly excluding it from claims of complete physical-attempt coverage.
    basis = 'physical_hook' if physical_attempt_id else 'legacy_logical_hook'
    physical_attempt_id = physical_attempt_id or ('legacy:' + str(api_request_id))
    start = epoch(attempt_started_at if attempt_started_at is not None else started_at)
    end = epoch(ended_at) if ended_at is not None and kind != 'start' else None
    if end is not None and end < start:
        raise ValueError('request end precedes physical start')
    measured = _usage(usage) if kind == 'end' else None
    status = 'open' if kind == 'start' else 'error' if kind == 'error' or error else 'completed'
    row = dict(session_id=str(session_id), physical_attempt_id=str(physical_attempt_id),
               logical_request_id=str(api_request_id), role=role, project=str(Path(project).resolve()),
               task_kind='aux:'+str(aux_task) if aux_task else session_kind if session_kind != 'unknown' else 'main',
               parent_session_id=str(parent_session_id or ''), session_kind=session_kind,
               run_id=str(run_id or ''), work_id=str(work_id or ''), task_id=str(task_id or ''),
               business_attempt=business_attempt, execution_id=str(execution_id or ''),
               provider_dispatched=None if provider_dispatched is None else int(provider_dispatched),
               model=str(model), provider=str(provider),
               endpoint_alias=str(endpoint_alias), route=route, started_at=start, ended_at=end,
               status=status, usage_present=int(measured is not None),
               usage_json=json.dumps(measured) if measured is not None else None, identity_basis=basis,
               normalization_version=str(usage.get('normalization_version', 'legacy-presence-unknown')) if isinstance(usage, dict) else 'unavailable',
               field_presence_json=json.dumps({key: measured[key] is not None for key in ALL_TOKEN_FIELDS})
                   if measured is not None and isinstance(usage.get('field_presence'), dict) else None)
    with _database(index_path) as db:
        old = db.execute('SELECT * FROM role_usage WHERE session_id=? AND physical_attempt_id=?',
                         (row['session_id'], row['physical_attempt_id'])).fetchone()
        if old:
            for key in ('logical_request_id', 'role', 'project', 'model', 'provider', 'endpoint_alias'):
                if old[key] != row[key]:
                    raise ValueError('physical attempt identity conflict: ' + key)
            legacy_purpose = old['session_kind'] == 'unknown' and old['task_kind'] in {'main', str(aux_task)}
            if old['task_kind'] != row['task_kind'] and not legacy_purpose:
                raise ValueError('physical attempt identity conflict: task_kind')
            if old['session_kind'] != 'unknown' and old['session_kind'] != row['session_kind']:
                raise ValueError('physical attempt identity conflict: session_kind')
            if old['parent_session_id'] and old['parent_session_id'] != row['parent_session_id']:
                raise ValueError('physical attempt identity conflict: parent_session_id')
            for key in ATTRIBUTION_FIELDS:
                if old[key] not in ('', None) and row[key] not in ('', None) and old[key] != row[key]:
                    raise ValueError('physical attempt identity conflict: ' + key)
                # Do not invent historical business attribution at completion.
                row[key] = old[key]
            if old['provider_dispatched'] is not None:
                if row['provider_dispatched'] is not None and row['provider_dispatched'] != old['provider_dispatched']:
                    raise ValueError('physical attempt identity conflict: provider_dispatched')
                row['provider_dispatched'] = old['provider_dispatched']
            if old['ended_at'] is not None:
                return dict(old)  # Replay cannot double charge or regress a terminal attempt.
            row['started_at'] = old['started_at']
            row['route'] = old['route']  # Freeze classification from request time, not later config.
        columns = ','.join(row)
        db.execute('INSERT OR REPLACE INTO role_usage (' + columns + ') VALUES (' + ','.join('?' for _ in row) + ')',
                   tuple(row.values()))
    return row


def summarize(index_path, role, start, end, *, project):
    """Completed attempts belong to [start,end); token amounts are never prorated."""
    if role not in ROLES:
        raise ValueError('unknown role')
    left, right = epoch(start), epoch(end)
    if right <= left:
        raise ValueError('empty or reversed usage window')
    path = Path(index_path)
    result = {'role': role, 'start': iso(left), 'end': iso(right), 'source': str(path),
              'scope': 'own role sessions and their auxiliary calls; excludes independent OCR/Codex tools',
              'window_basis': 'completed_at_half_open', 'source_available': path.is_file(),
              'coverage_basis': 'usage availability among observed application-level attempts; unobserved traffic cannot be inferred',
              'routes': {}, 'models': [], 'calls': 0, 'usage_calls': 0, 'missing_usage_calls': 0,
              'legacy_identity_calls': 0, 'open_calls': 0, 'collection_started_at': None,
              'logical_requests': 0, 'extra_physical_attempts': 0, 'error_calls': 0,
              'cache_read_calls': 0, 'cache_write_calls': 0, 'cache_complete_calls': 0,
              'cache_ratio_calls': 0, 'cache_ratio_coverage_ratio': None,
              'cache_known_prompt_tokens': None, 'cache_known_read_tokens': None,
              'cache_coverage_ratio': None, 'known_cache_read_ratio': None,
              'provider_dispatched_calls': 0, 'provider_not_dispatched_calls': 0, 'provider_dispatch_unknown_calls': 0,
              'business_attributed_calls': 0, 'business_attribution_missing_calls': 0,
              'normalization_versions': {}, 'field_coverage_calls': {key: 0 for key in ALL_TOKEN_FIELDS},
              'historical_gap': True,
              'limitations': ['Application-level physical attempts only; SDK-internal HTTP retries are not separately observed.',
                              'Unavailable usage is unknown, never zero; open attempts are listed separately.',
                              'Legacy normalized totals retain the old canonical arithmetic; their raw cache-field presence is unknown.',
                              'Session totals and cron audit totals are not added to this ledger.']}
    if not path.is_file():
        result['collection_error'] = 'usage_index_not_installed_or_no_requests_collected'
        return result
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1) as db:
        db.row_factory = sqlite3.Row
        args = (str(Path(project).resolve()), role)
        first = db.execute('SELECT MIN(started_at) FROM role_usage WHERE project=? AND role=?', args).fetchone()[0]
        rows = db.execute('SELECT * FROM role_usage WHERE project=? AND role=? AND ended_at>=? AND ended_at<?',
                          (*args, left, right)).fetchall()
        result['open_calls'] = db.execute('SELECT COUNT(*) FROM role_usage WHERE project=? AND role=? AND started_at<? AND ended_at IS NULL',
                                          (*args, right)).fetchone()[0]
    result['collection_started_at'] = iso(first) if first is not None else None
    result['historical_gap'] = first is None or first > left
    groups = {}
    logical_requests = set()
    cache_prompt = cache_read = 0
    for row in rows:
        row_keys = set(row.keys())
        key = (row['route'], row['model'], row['provider'], row['task_kind'])
        group = groups.setdefault(key, {'route': key[0], 'model': key[1], 'provider': key[2], 'task_kind': key[3],
                                        'calls': 0, 'usage_calls': 0, 'known_tokens': {k: None for k in ALL_TOKEN_FIELDS},
                                        'field_coverage_calls': {k: 0 for k in ALL_TOKEN_FIELDS}})
        group['calls'] += 1
        result['calls'] += 1
        logical_requests.add((row['session_id'], row['logical_request_id']))
        result['error_calls'] += row['status'] == 'error'
        dispatched = row['provider_dispatched'] if 'provider_dispatched' in row_keys else None
        result['provider_dispatch_unknown_calls' if dispatched is None else
               'provider_dispatched_calls' if dispatched else 'provider_not_dispatched_calls'] += 1
        attributed = business_attributed(row)
        result['business_attributed_calls' if attributed else 'business_attribution_missing_calls'] += 1
        version = row['normalization_version'] if 'normalization_version' in row_keys else 'legacy-presence-unknown'
        result['normalization_versions'][version] = result['normalization_versions'].get(version, 0) + 1
        result['legacy_identity_calls'] += row['identity_basis'] != 'physical_hook'
        if row['usage_present']:
            group['usage_calls'] += 1
            result['usage_calls'] += 1
            measured, presence = known_usage(row)
            for key in ALL_TOKEN_FIELDS:
                number = measured.get(key)
                if presence.get(key) and isinstance(number, int) and not isinstance(number, bool):
                    group['known_tokens'][key] = (group['known_tokens'][key] or 0) + number
                    group['field_coverage_calls'][key] += 1
                    result['field_coverage_calls'][key] += 1
            result['cache_read_calls'] += bool(presence.get('cache_read_tokens'))
            result['cache_write_calls'] += bool(presence.get('cache_write_tokens'))
            result['cache_complete_calls'] += bool(presence.get('cache_read_tokens') and presence.get('cache_write_tokens'))
            if presence.get('cache_read_tokens') and presence.get('prompt_tokens'):
                result['cache_ratio_calls'] += 1
                cache_prompt += measured['prompt_tokens']
                cache_read += measured['cache_read_tokens']
    result['logical_requests'] = len(logical_requests)
    result['extra_physical_attempts'] = result['calls'] - len(logical_requests)
    result['missing_usage_calls'] = result['calls'] - result['usage_calls']
    result['models'] = sorted(groups.values(), key=lambda g: (g['route'], g['model'], g['provider'], g['task_kind']))
    for route in ('local', 'cloud', 'unknown'):
        matching = [g for g in groups.values() if g['route'] == route]
        result['routes'][route] = {'calls': sum(g['calls'] for g in matching),
            'usage_calls': sum(g['usage_calls'] for g in matching),
            'field_coverage_calls': {k: sum(g['field_coverage_calls'][k] for g in matching) for k in ALL_TOKEN_FIELDS},
            'known_tokens': {k: sum(g['known_tokens'][k] for g in matching if g['known_tokens'][k] is not None)
                             if not matching or any(g['known_tokens'][k] is not None for g in matching) else None
                             for k in ALL_TOKEN_FIELDS}}
    known_total = sum(r['known_tokens']['total_tokens'] or 0 for r in result['routes'].values())
    for route in result['routes'].values():
        route['call_ratio'] = route['calls'] / result['calls'] if result['calls'] else None
        route['known_token_ratio'] = route['known_tokens']['total_tokens'] / known_total if known_total and route['known_tokens']['total_tokens'] is not None else None
    result['usage_coverage_ratio'] = result['usage_calls'] / result['calls'] if result['calls'] else None
    result['cache_coverage_ratio'] = result['cache_complete_calls'] / result['calls'] if result['calls'] else None
    result['cache_ratio_coverage_ratio'] = result['cache_ratio_calls'] / result['calls'] if result['calls'] else None
    if result['cache_ratio_calls']:
        result['cache_known_prompt_tokens'] = cache_prompt
        result['cache_known_read_tokens'] = cache_read
        result['known_cache_read_ratio'] = cache_read / cache_prompt if cache_prompt else None
    return result
