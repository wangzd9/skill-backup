"""Local evidence for orthogonal business, client API and role dispatch time.

No model calls or business writes. API metadata is allowlisted; prompts, response
bodies, tools, URLs and credentials are never persisted. Missing boundaries remain
missing. This module cannot measure pure server queue or pure coding time.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
from zoneinfo import ZoneInfo

ROLES = {'COORDINATOR', 'A', 'B', 'C', 'TESTER'}
LEGACY_API = re.compile(
    r'^(?P<at>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}) .*?'
    r'\[(?P<session>cron_(?P<job>[A-Za-z0-9_-]+)_\d{8}_\d{6})\] '
    r'.*?API call #(?P<count>\d+): .*?latency=(?P<latency>\d+(?:\.\d+)?)s(?:\s|$)')
API_LIMIT = ('Client elapsed time includes request preparation, network, SDK retries/backoff, '
             'server queue and inference; TTFB also includes network/prefill. '
             'Neither value is pure server queue time. Open or absent requests are not idle.')


def epoch(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
    else:
        at = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if at.tzinfo is None:
            raise ValueError('timestamps require an explicit timezone')
        number = at.timestamp()
    if not math.isfinite(number):
        raise ValueError('timestamp must be finite')
    return number


def iso(value):
    return datetime.fromtimestamp(epoch(value), timezone.utc).isoformat()


def _role(value):
    if value is not None and value not in ROLES:
        raise ValueError('unknown role')
    return value


@contextmanager
def _db(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=1)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA synchronous=FULL')
        db.execute('''CREATE TABLE IF NOT EXISTS api_requests (
            session_id TEXT NOT NULL, request_id TEXT NOT NULL, role TEXT, project TEXT,
            model TEXT, provider TEXT, started_at REAL, ended_at REAL, first_chunk_at REAL,
            status TEXT NOT NULL, timing_basis TEXT NOT NULL, source TEXT NOT NULL,
            timing_uncertainty_seconds REAL NOT NULL DEFAULT 0,
            PRIMARY KEY(session_id,request_id))''')
        db.execute('''CREATE TABLE IF NOT EXISTS api_errors (
            session_id TEXT NOT NULL, request_id TEXT NOT NULL, occurred_at REAL NOT NULL,
            retry_pending INTEGER NOT NULL, source TEXT NOT NULL,
            PRIMARY KEY(session_id,request_id,occurred_at))''')
        db.execute('''CREATE TABLE IF NOT EXISTS log_imports (
            path TEXT PRIMARY KEY, device INTEGER, inode INTEGER, offset INTEGER NOT NULL)''')
        db.execute('CREATE TABLE IF NOT EXISTS timing_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        db.execute('CREATE INDEX IF NOT EXISTS api_role_time ON api_requests(role,started_at,ended_at)')
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def record_api_start(index_path, *, session_id, api_request_id, started_at,
                     role=None, project=None, model='', provider='', **_ignored):
    """Idempotent pre hook. Repeated retry starts retain the native logical start."""
    _role(role)
    if not session_id or not api_request_id:
        raise ValueError('session_id and api_request_id are required')
    at = epoch(started_at)
    with _db(index_path) as db:
        db.execute('''INSERT INTO api_requests
            (session_id,request_id,role,project,model,provider,started_at,status,timing_basis,source)
            VALUES (?,?,?,?,?,?,?,'open','native_hook','pre_api_request')
            ON CONFLICT(session_id,request_id) DO UPDATE SET
            role=COALESCE(excluded.role,api_requests.role),
            project=COALESCE(excluded.project,api_requests.project),
            started_at=CASE WHEN api_requests.started_at IS NULL THEN excluded.started_at
                ELSE MIN(api_requests.started_at,excluded.started_at) END,
            timing_basis='native_hook' ''',
            (str(session_id), str(api_request_id), role, str(project) if project else None,
             str(model), str(provider), at))
    return {'session_id': session_id, 'api_request_id': api_request_id,
            'started_at': at, 'status': 'open'}


def record_api_end(index_path, *, session_id, api_request_id, ended_at,
                   started_at=None, first_chunk_at=None, status='completed',
                   retry_pending=False, role=None, project=None, model='', provider='', **_ignored):
    """Post/error hook; retryable attempt errors do not end the logical request."""
    _role(role)
    if status not in {'completed', 'error'}:
        raise ValueError('status must be completed or error')
    if not session_id or not api_request_id:
        raise ValueError('session_id and api_request_id are required')
    end = epoch(ended_at)
    start = epoch(started_at) if started_at is not None else None
    first = epoch(first_chunk_at) if first_chunk_at is not None else None
    with _db(index_path) as db:
        old = db.execute('SELECT * FROM api_requests WHERE session_id=? AND request_id=?',
                         (session_id, api_request_id)).fetchone()
        start = start if start is not None else (old['started_at'] if old else None)
        if old and old['started_at'] is not None and start is not None:
            start = min(old['started_at'], start)
        if start is not None and end < start:
            raise ValueError('request end precedes start')
        if first is not None and (start is None or not start <= first <= end):
            raise ValueError('first chunk is outside request boundaries')
        if status == 'error':
            db.execute('INSERT OR IGNORE INTO api_errors VALUES (?,?,?,?,?)',
                       (session_id, api_request_id, end, int(bool(retry_pending)), 'api_request_error'))
        if old and old['ended_at'] is not None and (old['status'] == 'completed' or status == 'error'):
            # A delayed error or replayed post cannot reopen/regress a final record.
            return {'session_id':session_id,'api_request_id':api_request_id,
                    'started_at':old['started_at'],'ended_at':old['ended_at'],'status':old['status']}
        final_end, final_status = (None, 'open') if retry_pending else (end, status)
        db.execute('''INSERT INTO api_requests
            (session_id,request_id,role,project,model,provider,started_at,ended_at,first_chunk_at,
             status,timing_basis,source) VALUES (?,?,?,?,?,?,?,?,?,?,'native_hook',?)
            ON CONFLICT(session_id,request_id) DO UPDATE SET
            role=COALESCE(excluded.role,api_requests.role),
            project=COALESCE(excluded.project,api_requests.project),
            started_at=COALESCE(excluded.started_at,api_requests.started_at),
            ended_at=excluded.ended_at,first_chunk_at=excluded.first_chunk_at,
            status=excluded.status,timing_basis='native_hook',source=excluded.source''',
            (session_id, api_request_id, role, str(project) if project else None, str(model),
             str(provider), start, final_end, first, final_status,
             'api_request_error' if status == 'error' else 'post_api_request'))
    return {'session_id': session_id, 'api_request_id': api_request_id,
            'started_at': start, 'ended_at': final_end, 'status': final_status}


def record_api_error(index_path, *, retryable=None, retry_count=None, max_retries=None,
                     retry_pending=None, **metadata):
    """An unspecified retry decision is conservatively kept open."""
    if retry_pending is None:
        retry_pending = retryable is not False
        if retry_count is not None and max_retries is not None and retry_count >= max_retries:
            retry_pending = False
    return record_api_end(index_path, status='error', retry_pending=retry_pending, **metadata)


def import_agent_logs(index_path, paths, job_roles, *, log_timezone='Asia/Shanghai', project=None, before=None):
    """Incremental local import of completed cron API latency lines only.

    Legacy start = log completion timestamp - rounded latency; it is approximate
    (latency rounding >= 0.05s plus unmeasured delay before the log was written).
    File offsets make repeat reports O(new bytes). Rotations restart that path's
    cursor; the request primary key deduplicates previously imported lines.
    A persisted import cutover excludes completions at/after native capture starts,
    avoiding duplicate legacy/native identities for the same logical request.
    """
    result = {'inserted': 0, 'matched': 0, 'bytes_read': 0, 'errors': [], 'skipped_after_cutover':0}
    for job_role in job_roles.values():
        _role(job_role)
    with _db(index_path) as db:
        policy = db.execute("SELECT value FROM timing_meta WHERE key='legacy_cutover_at'").fetchone()
        native = db.execute("SELECT MIN(started_at) FROM api_requests WHERE timing_basis='native_hook'").fetchone()[0]
        cutoff = epoch(before) if before is not None else (float(policy[0]) if policy else datetime.now(timezone.utc).timestamp())
        if native is not None:
            cutoff = min(cutoff,native)
        if policy:
            cutoff = min(cutoff,float(policy[0]))
        db.execute("INSERT OR REPLACE INTO timing_meta VALUES ('legacy_cutover_at',?)",(str(cutoff),))
        result['cutover_at'] = iso(cutoff)
        for supplied in paths:
            path = Path(supplied).resolve()
            try:
                with path.open('rb') as stream:
                    stat = path.stat()
                    previous = db.execute('SELECT * FROM log_imports WHERE path=?', (str(path),)).fetchone()
                    offset = previous['offset'] if previous and (previous['device'], previous['inode']) == (stat.st_dev, stat.st_ino) and previous['offset'] <= stat.st_size else 0
                    stream.seek(offset)
                    while True:
                        line_start = stream.tell()
                        raw = stream.readline()
                        if not raw or not raw.endswith(b'\n'):
                            stream.seek(line_start)
                            break
                        result['bytes_read'] += len(raw)
                        match = LEGACY_API.search(raw.decode('utf-8', errors='replace'))
                        if not match or match['job'] not in job_roles:
                            continue
                        result['matched'] += 1
                        end = datetime.strptime(match['at'], '%Y-%m-%d %H:%M:%S,%f').replace(tzinfo=ZoneInfo(log_timezone)).timestamp()
                        if end >= cutoff:
                            result['skipped_after_cutover'] += 1
                            continue
                        latency = float(match['latency'])
                        cursor = db.execute('''INSERT OR IGNORE INTO api_requests
                            (session_id,request_id,role,project,started_at,ended_at,status,timing_basis,source,
                             timing_uncertainty_seconds) VALUES (?,?,?,?,?,?,'completed','log_completion_backfill',?,?)''',
                            (match['session'], 'legacy:'+match['count']+':'+match['at'],
                             job_roles[match['job']], str(project) if project else None, end-latency, end,
                             str(path)+':byte:'+str(line_start), .05))
                        result['inserted'] += cursor.rowcount
                    db.execute('INSERT OR REPLACE INTO log_imports VALUES (?,?,?,?)',
                               (str(path), stat.st_dev, stat.st_ino, stream.tell()))
            except (OSError, ValueError) as exc:
                result['errors'].append({'source': str(path), 'reason': type(exc).__name__})
    return result


def read_cron_records(executions_db, job_roles, start, end):
    """Read only native execution metadata. claimed_at is not a server queue event."""
    a, b = epoch(start), epoch(end)
    if b <= a:
        raise ValueError('end must follow start')
    path = Path(executions_db).resolve()
    db = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=1)
    db.row_factory = sqlite3.Row
    try:
        # The native ledger is bounded (currently <=1000 terminal records). Select
        # metadata only; never read errors, prompts, deliveries or job_json.
        cols = {r[1] for r in db.execute('PRAGMA table_info(executions)')}
        required = {'id','job_id','claimed_at','started_at','finished_at','status'}
        if not required <= cols:
            raise ValueError('execution ledger schema is missing time boundaries')
        fields = sorted(required | ({'scheduled_instant'} & cols))
        jobs = tuple(job_roles)
        if not jobs:
            return []
        rows = db.execute('SELECT '+','.join(fields)+' FROM executions WHERE job_id IN ('+','.join('?' for _ in jobs)+')', jobs)
        records = []
        for row in rows:
            item = dict(row)
            claimed = epoch(item['claimed_at'])
            finished = epoch(item['finished_at']) if item['finished_at'] else None
            scheduled = epoch(item['scheduled_instant']) if item.get('scheduled_instant') else claimed
            if min(claimed, scheduled) < b and (finished is None or finished > a):
                item.update(role=_role(job_roles[item['job_id']]), execution_id=item.pop('id'),
                            source=str(path)+'#execution:')
                item['source'] += item['execution_id']
                records.append(item)
        return records
    finally:
        db.close()


def _clipped(start, end, a, b):
    left, right = max(epoch(start), a), min(epoch(end), b)
    return (left, right) if right > left else None


def _union(spans):
    merged = []
    for left, right in sorted(spans):
        if merged and left <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(right, merged[-1][1]))
        else:
            merged.append((left, right))
    return merged


def _gap(start, end, reason, sources):
    return {'start': iso(start), 'end': iso(end), 'seconds': end-start,
            'reason': reason, 'recovery_sources': list(dict.fromkeys(str(s) for s in sources if s))}


def summarize(index_path, role, start, end, *, business_segments=(), cron_records=(), project=None):
    """JSON-ready report; dimensions overlap and must never be added together.

    Input business segments stay intact, including evidence summaries and sources.
    Their task-stage durations include model responses; subtracting API latency
    cannot produce development time. No-model/no-cron evidence does not prove idle.
    """
    _role(role)
    a, b = epoch(start), epoch(end)
    if b <= a:
        raise ValueError('end must follow start')
    business = {'segments': [], 'observed_seconds': 0, 'totals_seconds': {}, 'gaps': [],
                'semantics': 'Observed task-stage time, including model response time; not exact development labor.'}
    spans, known_business_spans = [], []
    for original in business_segments:
        clipped = _clipped(original['start'], original['end'], a, b)
        if not clipped:
            continue
        left, right = clipped
        item = {**original, 'start': iso(left), 'end': iso(right), 'seconds': right-left}
        business['segments'].append(item)
        category = original.get('category', 'unknown')
        business['totals_seconds'][category] = business['totals_seconds'].get(category, 0)+right-left
        spans.append(clipped)
        if category == 'unknown':
            reason = original.get('reason') or original.get('gap_reason') or ('unclassified_business_evidence' if any(original.get('sources', [])) else 'business_observation_missing')
            business['gaps'].append(_gap(left, right, reason,
                original.get('recovery_sources') or [*original.get('sources', []), 'project events/<role>/', 'project inbox/events/<role>/', 'project runtime/activity/<date>/<role>.jsonl']))
        else:
            known_business_spans.append(clipped)
    business['observed_seconds'] = sum(right-left for left,right in _union(known_business_spans))
    cursor = a
    for left, right in _union(spans)+[(b,b)]:
        if left > cursor:
            business['gaps'].append(_gap(cursor,left,'business_observation_missing',
                ['project runtime/activity/<date>/<role>.jsonl','project events/<role>/','project inbox/events/<role>/']))
        cursor = max(cursor,right)

    model = {'requests': [], 'open_requests': [], 'gaps': [], 'observed_seconds': 0,
             'request_sum_seconds': 0, 'limitations': API_LIMIT,
             'coverage': 'Recorded request occupancy only. Its complement can contain tools or no API request; it does not by itself prove missing data or idle.'}
    path = Path(index_path)
    requests = []
    if path.exists():
        db = sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=1)
        db.row_factory = sqlite3.Row
        try:
            sql = '''SELECT * FROM api_requests WHERE role=?
                AND (started_at IS NULL OR started_at<?) AND (ended_at IS NULL OR ended_at>?)
                '''
            args = [role,b,a]
            if project is not None:
                sql += ' AND project=?'
                args.append(str(project))
            requests = list(db.execute(sql+' ORDER BY started_at', args))
        finally:
            db.close()
    api_spans = []
    for row in requests:
        item = dict(row)
        if row['started_at'] is None or row['ended_at'] is None:
            model['open_requests'].append(item)
            continue
        clipped = _clipped(row['started_at'], row['ended_at'], a, b)
        if clipped:
            left,right = clipped
            item.update(start=iso(left), end=iso(right), seconds=right-left,
                        ttfb_seconds=(row['first_chunk_at']-row['started_at']) if row['first_chunk_at'] is not None else None)
            model['requests'].append(item)
            api_spans.append(clipped)
            model['request_sum_seconds'] += right-left
    merged_api = _union(api_spans)
    model['observed_seconds'] = sum(right-left for left,right in merged_api)
    # Account for every unobserved subinterval with an explicit recovery path.
    boundaries = sorted({a,b,*[at for pair in merged_api for at in pair],
        *[max(a,r['started_at']) for r in model['open_requests'] if r['started_at'] is not None]})
    for left,right in zip(boundaries,boundaries[1:]):
        if right <= left or any(x <= left and right <= y for x,y in merged_api):
            continue
        open_rows = [r for r in model['open_requests'] if r['started_at'] is None or r['started_at'] <= left]
        reason = ('request_still_open' if any(r['started_at'] is not None for r in open_rows) else 'request_start_missing') if open_rows else ('request_timing_not_collected' if not requests else 'no_request_timing_evidence_for_interval')
        model['gaps'].append(_gap(left,right,reason,[*[r['source'] for r in open_rows],str(path),
            'Hermes logs/agent.log* (completion + rounded latency)',
            'Hermes native pre/post/error API hooks (future collection)',
            'Hermes state.db (session/message identity only, no per-request latency)']))
    model['unobserved_seconds'] = b-a-model['observed_seconds']
    model['not_observed_api_occupancy_seconds'] = model['unobserved_seconds']
    model['missing_boundary_note'] = ('Legacy logs recover completed calls within retained log files only; '
        'errors/inflight calls without completion lines and rotated-away logs cannot be reconstructed exactly. '
        'No timing evidence for remaining time; it can contain tools, reasoning orchestration or idle.')

    dispatch = {'records': [], 'gaps': [], 'schedule_delay_seconds': 0,
                'dispatch_wait_seconds': 0, 'execution_seconds': 0,
                'limitations': 'scheduled→claimed is scheduler lateness; claimed→started is role task dispatch waiting. '
                               'Execution span includes scripts/models/tools/delivery. No queued_at or server queue boundary is present.'}
    cron_spans = {'schedule_delay_seconds': [],'dispatch_wait_seconds': [],'execution_seconds': []}
    for raw in cron_records:
        if raw.get('role') != role:
            continue
        item = dict(raw)
        dispatch['records'].append(item)
        claimed = epoch(raw['claimed_at'])
        started = epoch(raw['started_at']) if raw.get('started_at') is not None else None
        finished = epoch(raw['finished_at']) if raw.get('finished_at') is not None else None
        scheduled = epoch(raw['scheduled_instant']) if raw.get('scheduled_instant') is not None else None
        pairs = [('schedule_delay_seconds',scheduled,claimed), ('dispatch_wait_seconds',claimed,started),
                 ('execution_seconds',started,finished)]
        for key,left,right in pairs:
            if left is not None and right is not None and right >= left:
                clipped = _clipped(left,right,a,b)
                if clipped:
                    cron_spans[key].append(clipped)
        if started is None:
            dispatch['gaps'].append(_gap(max(a,claimed), min(b,finished or b),
                'execution_never_started' if finished else 'execution_start_not_recorded', [raw.get('source'),'Hermes cron/executions.db','Hermes cron worker logs']))
        elif finished is None:
            dispatch['gaps'].append(_gap(max(a,started),b,'execution_finish_not_recorded',
                [raw.get('source'),'Hermes cron/executions.db','Hermes state.db session ended_at (corroboration only)']))
    for key, values in cron_spans.items():
        dispatch[key] = sum(right-left for left,right in _union(values))
    if not dispatch['records']:
        dispatch['gaps'].append(_gap(a,b,'cron_execution_history_unavailable_for_window',
            ['Hermes cron/executions.db (bounded terminal retention)','Hermes cron/usage_audit.jsonl','Hermes state.db cron session start/end','Hermes cron/output/<job_id>/ (corroboration)']))
    return {'schema_version':1,'role':role,'start':iso(a),'end':iso(b),'window_seconds':b-a,
            'dimensions_are_additive':False,'dimensions':{'business_phase':business,
                'client_model_wait':model,'role_task_dispatch':dispatch},
            'unmeasurable':{'pure_server_queue': 'Needs server-side per-request accepted/dequeued/inference events; client latency or TTFB cannot isolate it.',
                            'pure_development_labor':'Needs explicit development/tool activity boundaries; task status minus API time is insufficient.'}}
