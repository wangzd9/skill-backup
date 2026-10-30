"""R1 observation v2: immutable payloads plus every real sampling boundary.

This is a rebuildable observation projection, never a business authority. The
existing report interval algorithm consumes reconstructed original rows. Format
selection is exclusively by a pinned manifest, not file/content detection.
Legacy installations must explicitly pass legacy_mode='LEGACY_ONLY'. Publishing
v2 creates a durable requirement marker: losing its manifest then fails closed.
No installation is migrated merely by importing or calling this module.
"""
from contextlib import contextmanager, ExitStack
from copy import deepcopy
from datetime import datetime, timedelta
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import resource
import sqlite3
import time
import uuid
from zoneinfo import ZoneInfo

ROLES = ('COORDINATOR', 'A', 'B', 'C', 'TESTER')
MAX_GAP = 600
CLASSIFIER_VERSION = 'activity-report-v1'
BASE = 'inbox/control/runtime'
MANIFEST = BASE+'/activity-routing.json'
REQUIRED = BASE+'/activity-v2-required.json'
STORE = BASE+'/activity-v2.sqlite3'
TIMES = ('at', 'observed_at', 'recorded_at')
SEMANTIC = ('category', 'role', 'task', 'attempt', 'summary', 'raw_status',
            'classification_reason', 'reason_codes', 'confirmed_idle', 'basis',
            'legacy_status_evidence', 'occurred_at', 'event_id', 'source_refs',
            'identity', 'owner_process_state', 'classification_reasons',
            'business_authority_state', 'heartbeat_state', 'activity_source_state',
            'reason', 'dispatch_source', 'dispatch_mtime')
IDENTITY_FIELDS = ('pool', 'generation', 'run_id', 'role', 'work_id', 'task_id',
                   'attempt', 'execution_id', 'execution_generation')


class ObservationGap(ValueError):
    """A routing/reference gap. Trying another parser would hide this error."""


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else _json(value).encode()).hexdigest()


def _epoch(value):
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        number = float(value)
    else:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ObservationGap('OBSERVATION_TIME_TIMEZONE_REQUIRED')
        number = parsed.timestamp()
    if not math.isfinite(number):
        raise ObservationGap('OBSERVATION_TIME_NOT_FINITE')
    return number


def _path(root, locator):
    if not isinstance(locator, str) or not locator or Path(locator).is_absolute():
        raise ObservationGap('OBSERVATION_LOCATOR_NOT_RELATIVE')
    root = Path(root).resolve()
    path = root/locator
    if not path.resolve().is_relative_to(root) or not path.resolve().is_relative_to((root/BASE).resolve()):
        raise ObservationGap('OBSERVATION_LOCATOR_ESCAPES_RUNTIME')
    return path


def _routing_signature(root):
    """Exact routing bytes, including a durable partially published epoch."""
    values = []
    for locator in (MANIFEST, REQUIRED):
        try:
            values.append(_path(root, locator).read_bytes())
        except FileNotFoundError:
            values.append(None)
        except OSError as exc:
            raise ObservationGap('OBSERVATION_ROUTING_UNREADABLE') from exc
    return tuple(values)


@contextmanager
def routing_lock(root, *, readonly=False):
    """Writers serialize publication; readers never create target artifacts.

    An older installation can have no routing lock yet. Its reader pins the
    routing bytes optimistically and verifies both them and continued lock
    absence after reading. A concurrent upgraded writer/publication makes that
    read fail closed, so missing locks never enable a routing fallback race.
    """
    path = _path(root, BASE+'/activity-storage.lock')
    if readonly:
        try:
            stream = path.open('r')
        except FileNotFoundError:
            before = _routing_signature(root)
            yield
            if path.exists() or _routing_signature(root) != before:
                raise ObservationGap('OBSERVATION_ROUTING_CHANGED_DURING_READ')
            return
        except OSError as exc:
            raise ObservationGap('OBSERVATION_ROUTING_LOCK_UNREADABLE') from exc
        with stream:
            fcntl.flock(stream, fcntl.LOCK_SH)
            try:
                opened = os.fstat(stream.fileno())
                before = _routing_signature(root)
                yield
                try:
                    current = path.stat()
                except OSError as exc:
                    raise ObservationGap('OBSERVATION_ROUTING_LOCK_CHANGED_DURING_READ') from exc
                if ((opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
                        or _routing_signature(root) != before):
                    raise ObservationGap('OBSERVATION_ROUTING_CHANGED_DURING_READ')
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with temp.open('x') as stream:
            os.chmod(temp, 0o600)
            stream.write(_json(value)+'\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temp.unlink(missing_ok=True)


def _legacy_manifest():
    return {'manifest_version': 1, 'mode': 'LEGACY_ONLY', 'manifest_id': 'explicit-legacy-installation-v1',
            'writer_epoch': 'legacy', 'classifier_version': CLASSIFIER_VERSION,
            'source_mapping': {'legacy_activity': 'explicit legacy installation configuration'},
            'streams': [{'stream_id': 'legacy-'+role, 'source_id': 'legacy_activity', 'role': role,
                'locator': BASE+'/activity/{date}/'+role+'.jsonl', 'storage_format': 'activity_jsonl_v1',
                'schema_version': 1, 'parser_id': 'activity_v1', 'read_range': 'LIVE', 'writer': True}
                for role in ROLES]}


def _range(value):
    if (not isinstance(value, list) or len(value) != 2 or type(value[0]) is not int or
            value[0] < 1 or (value[1] is not None and (type(value[1]) is not int or value[1] < value[0]-1))):
        raise ObservationGap('OBSERVATION_READ_RANGE_INVALID')
    return value


def validate_manifest(root, supplied):
    """Validate registration before ever inspecting stream data."""
    manifest = deepcopy(supplied)
    if (not isinstance(manifest, dict) or type(manifest.get('manifest_version')) is not int or manifest.get('manifest_version') != 1 or
            manifest.get('mode') not in {'LEGACY_ONLY', 'DUAL_READ_V2'} or
            not isinstance(manifest.get('manifest_id'), str) or not manifest['manifest_id'] or
            not isinstance(manifest.get('writer_epoch'), str) or not manifest['writer_epoch'] or
            not isinstance(manifest.get('classifier_version'), str) or not manifest['classifier_version'] or
            not isinstance(manifest.get('source_mapping'), dict) or not isinstance(manifest.get('streams'), list)):
        raise ObservationGap('OBSERVATION_MANIFEST_INVALID')
    ids, ranges, writers, formats = set(), {}, set(), {}
    for stream in manifest['streams']:
        if (not isinstance(stream, dict) or not isinstance(stream.get('stream_id'), str) or
                not stream['stream_id'] or stream['stream_id'] in ids or stream.get('role') not in ROLES or
                stream.get('source_id') not in manifest['source_mapping']):
            raise ObservationGap('OBSERVATION_STREAM_OR_SOURCE_INVALID')
        ids.add(stream['stream_id'])
        if type(stream.get('schema_version')) is not int or ('writer' in stream and type(stream['writer']) is not bool):
            raise ObservationGap('OBSERVATION_STREAM_SCHEMA_INVALID')
        fmt = (stream.get('storage_format'), stream.get('schema_version'), stream.get('parser_id'))
        if fmt not in {('activity_jsonl_v1', 1, 'activity_v1'), ('observation_store_v2', 2, 'activity_v2')}:
            raise ObservationGap('OBSERVATION_FORMAT_UNSUPPORTED')
        locator = stream.get('locator')
        if not isinstance(locator, str):
            raise ObservationGap('OBSERVATION_LOCATOR_NOT_RELATIVE')
        if locator in formats and formats[locator] != fmt[0]:
            raise ObservationGap('OBSERVATION_CONFLICTING_FORMATS')
        formats[locator] = fmt[0]
        template = '{date}' in locator
        _path(root, locator.replace('{date}', '2000-01-01'))
        if '{' in locator.replace('{date}', '') or '}' in locator.replace('{date}', ''):
            raise ObservationGap('OBSERVATION_TEMPLATE_UNSUPPORTED')
        span = stream.get('read_range')
        if span == 'LIVE':
            if manifest['mode'] != 'LEGACY_ONLY' or fmt[0] != 'activity_jsonl_v1' or not template:
                raise ObservationGap('OBSERVATION_LIVE_RANGE_NOT_LEGACY')
        else:
            _range(span)
            if template or (fmt[0] == 'activity_jsonl_v1' and
                    (not isinstance(stream.get('sha256'), str) or len(stream['sha256']) != 64 or span[1] is None)):
                raise ObservationGap('OBSERVATION_FROZEN_SOURCE_INVALID')
            if fmt[0] == 'activity_jsonl_v1' and stream.get('cutover_high_water') != span[1]:
                raise ObservationGap('OBSERVATION_CUTOVER_HIGH_WATER_INVALID')
        # Same physical source cannot silently change parser, even for disjoint ranges.
        key = (locator, stream['role'] if fmt[0] == 'observation_store_v2' else None)
        for previous in ranges.get(key, []):
            if previous['storage_format'] != fmt[0]:
                raise ObservationGap('OBSERVATION_CONFLICTING_FORMATS')
            one, two = previous['read_range'], span
            if one == 'LIVE' or two == 'LIVE' or max(one[0], two[0]) <= min(one[1] or math.inf, two[1] or math.inf):
                raise ObservationGap('OBSERVATION_OVERLAPPING_RANGES')
        ranges.setdefault(key, []).append(stream)
        if stream.get('writer'):
            if stream['role'] in writers:
                raise ObservationGap('OBSERVATION_MULTIPLE_WRITERS')
            writers.add(stream['role'])
            if manifest['mode'] == 'DUAL_READ_V2' and fmt[0] != 'observation_store_v2':
                raise ObservationGap('OBSERVATION_OLD_WRITER_STILL_ENABLED')
    if writers != set(ROLES):
        raise ObservationGap('OBSERVATION_WRITER_ROLES_MISSING')
    if manifest['manifest_id'] != 'explicit-legacy-installation-v1' and manifest['manifest_id'] != _sha(
            {key:value for key,value in manifest.items() if key!='manifest_id'}):
        raise ObservationGap('OBSERVATION_MANIFEST_CONTENT_HASH_MISMATCH')
    return manifest


def load_manifest(root, *, legacy_mode=None):
    """Pin one routing version; explicit legacy config is required if uninstalled."""
    path, marker = _path(root, MANIFEST), _path(root, REQUIRED)
    if path.exists():
        try:
            value = validate_manifest(root, json.loads(path.read_bytes()))
        except (OSError, ValueError, TypeError) as exc:
            raise ObservationGap('OBSERVATION_MANIFEST_UNREADABLE:'+str(exc)) from exc
        if marker.exists():
            try:
                required = json.loads(marker.read_bytes())
                if any(required.get(key) != value.get(key) for key in ('manifest_id', 'writer_epoch')):
                    raise ObservationGap('OBSERVATION_MANIFEST_EPOCH_MISMATCH')
            except (OSError, ValueError, TypeError) as exc:
                raise ObservationGap('OBSERVATION_REQUIRED_EPOCH_UNREADABLE:'+str(exc)) from exc
        elif value['mode'] != 'LEGACY_ONLY':
            raise ObservationGap('OBSERVATION_V2_EPOCH_MARKER_MISSING')
        return value
    if marker.exists() or legacy_mode != 'LEGACY_ONLY':
        raise ObservationGap('OBSERVATION_MANIFEST_REQUIRED')
    return validate_manifest(root, _legacy_manifest())


@contextmanager
def _db(path, *, readonly=False):
    path = Path(path)
    if readonly:
        if not path.is_file():
            raise ObservationGap('OBSERVATION_STORE_MISSING')
        db = sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=1)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path, timeout=1)
        os.chmod(path, 0o600)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA foreign_keys=ON')
        if not readonly:
            db.execute('PRAGMA synchronous=FULL')
            db.executescript('''
              CREATE TABLE IF NOT EXISTS store_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS payloads (payload_id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL,
                classifier_version TEXT NOT NULL, body TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS observations (seq INTEGER PRIMARY KEY AUTOINCREMENT,
                stream_id TEXT NOT NULL, observation_id TEXT NOT NULL, observed_at REAL NOT NULL,
                original_times TEXT NOT NULL, source TEXT NOT NULL, row_extras TEXT NOT NULL,
                payload_id TEXT NOT NULL REFERENCES payloads, collector_epoch TEXT NOT NULL,
                source_checks_ref TEXT NOT NULL, row_sha256 TEXT NOT NULL, sampling_sha256 TEXT NOT NULL,
                UNIQUE(stream_id,observation_id));
              CREATE TABLE IF NOT EXISTS source_checks (seq INTEGER NOT NULL REFERENCES observations,
                source_id TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(seq,source_id));
              CREATE INDEX IF NOT EXISTS observation_window ON observations(stream_id,observed_at,seq);
            ''')
            old = db.execute("SELECT value FROM store_meta WHERE key='schema_version'").fetchone()
            if old is not None and old[0] != '2':
                raise ObservationGap('OBSERVATION_STORE_SCHEMA_UNSUPPORTED')
            db.execute("INSERT OR IGNORE INTO store_meta VALUES ('schema_version','2')")
            db.commit()  # schema initialization precedes the observation transaction
        else:
            version = db.execute("SELECT value FROM store_meta WHERE key='schema_version'").fetchone()
            if not version or version[0] != '2':
                raise ObservationGap('OBSERVATION_STORE_SCHEMA_UNSUPPORTED')
        yield db
        if not readonly:
            db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _audit(db):
    missing = list(db.execute('PRAGMA foreign_key_check'))
    orphan = [row[0] for row in db.execute('SELECT payload_id FROM payloads WHERE payload_id NOT IN (SELECT payload_id FROM observations)')]
    if missing or orphan:
        raise ObservationGap('OBSERVATION_STORE_REFERENCE_GAP:'+_json({'missing_references': len(missing), 'orphan_payloads': orphan}))
    for stream, high in db.execute("SELECT substr(key,19),value FROM store_meta WHERE key LIKE 'source_high_water:%'"):
        actual = db.execute('SELECT MAX(seq) FROM observations WHERE stream_id=?',(stream,)).fetchone()[0] or 0
        if str(actual) != high:
            raise ObservationGap('OBSERVATION_STORE_INCOMPLETE_HIGH_WATER')


def _payload(row, classifier):
    fields = {key: row[key] for key in SEMANTIC if key in row}
    original_identity = fields.get('identity') if isinstance(fields.get('identity'),dict) else {}
    identity = {key:original_identity.get(key,row.get('role') if key=='role' else None) for key in IDENTITY_FIELDS}
    identity['not_applicable'] = {key:'legacy_observation_identity_not_recorded' for key in IDENTITY_FIELDS
                                  if identity[key] is None}
    value = {'schema_version': 2, 'classifier_version': classifier, 'identity': identity,
             'fields': fields, 'present_fields': list(fields)}
    # The mandatory logical fields stay inspectable while present_fields keeps
    # missing vs null/type distinctions when the original row is reconstructed.
    for key in SEMANTIC:
        if key != 'identity':
            value[key] = fields.get(key)
    return value


def _normalized_checks(checks, observed, db, stream):
    result, seen = [], set()
    for value in checks or []:
        if not isinstance(value, dict) or not isinstance(value.get('source_id'), str) or not value['source_id'] or value['source_id'] in seen:
            raise ObservationGap('OBSERVATION_SOURCE_CHECK_INVALID')
        seen.add(value['source_id'])
        item = {key: value.get(key) for key in ('source_id', 'source_version', 'observed_at', 'check_status',
                'last_verified_at', 'max_gap_seconds', 'reason_codes', 'evidence_ref')}
        if item['observed_at'] is None:
            item['observed_at'] = observed
        checked = _epoch(item['observed_at'])
        if checked > observed:
            raise ObservationGap('OBSERVATION_SOURCE_CHECK_FUTURE')
        if item['check_status'] not in {'VERIFIED', 'FAILED', 'UNVERIFIED', 'STALE', 'INCOMPLETE'}:
            raise ObservationGap('OBSERVATION_SOURCE_CHECK_STATUS_INVALID')
        gap = item['max_gap_seconds']
        if not isinstance(gap, (int, float)) or isinstance(gap, bool) or not math.isfinite(gap) or gap < 0:
            raise ObservationGap('OBSERVATION_SOURCE_MAX_GAP_INVALID')
        if item['check_status'] == 'VERIFIED':
            verified = _epoch(item['last_verified_at']) if item['last_verified_at'] is not None else checked
            if verified > checked:
                raise ObservationGap('OBSERVATION_SOURCE_VERIFICATION_FUTURE')
            item['last_verified_at'] = verified
        else:
            # A failure is never renewed by another successful source.
            previous = db.execute('''SELECT c.body FROM source_checks c JOIN observations o ON o.seq=c.seq
                WHERE o.stream_id=? AND c.source_id=? AND o.observed_at<=?
                ORDER BY o.observed_at DESC,o.seq DESC LIMIT 1''',
                (stream['stream_id'], item['source_id'], checked)).fetchone()
            item['last_verified_at'] = json.loads(previous[0]).get('last_verified_at') if previous else None
        result.append(item)
    return result


def _append_v2(root, manifest, stream, row, *, observation_id=None, source_checks=()):
    path = _path(root, stream['locator'])
    instant = _epoch(row['at'])
    payload = _payload(row, manifest['classifier_version'])
    body = _json(payload)
    payload_id = _sha(body.encode())
    identity = observation_id or uuid.uuid4().hex
    if not isinstance(identity, str) or not identity:
        raise ObservationGap('OBSERVATION_ID_REQUIRED')
    with _db(path) as db:
        db.execute('BEGIN IMMEDIATE')
        _audit(db)
        previous = db.execute('SELECT seq,sampling_sha256 FROM observations WHERE stream_id=? AND observation_id=?',
                              (stream['stream_id'],identity)).fetchone()
        row_sha = _sha(row)
        sampling_sha = _sha({'row':row,'source_checks':source_checks})
        if previous:
            if previous['sampling_sha256'] != sampling_sha:
                raise ObservationGap('OBSERVATION_ID_CONFLICT')
            return {'observation_id': identity, 'seq': previous['seq'], 'duplicate': True, 'payload_id': payload_id}
        checks = _normalized_checks(source_checks, instant, db, stream)
        db.execute('INSERT OR IGNORE INTO payloads VALUES (?,?,?,?)', (payload_id, 2, manifest['classifier_version'], body))
        times = {key: row[key] for key in TIMES if key in row}
        extras = {key: value for key, value in row.items() if key not in (*TIMES, *SEMANTIC, 'source')}
        source = {'present': 'source' in row, 'value': row.get('source')}
        cursor = db.execute('''INSERT INTO observations (stream_id,observation_id,observed_at,original_times,
            source,row_extras,payload_id,collector_epoch,source_checks_ref,row_sha256,sampling_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
            (stream['stream_id'],identity,instant,_json(times),_json(source),_json(extras),payload_id,
             manifest['writer_epoch'],_json([item['source_id'] for item in checks]),row_sha,sampling_sha))
        seq = cursor.lastrowid
        for item in checks:
            db.execute('INSERT INTO source_checks VALUES (?,?,?)', (seq,item['source_id'],_json(item)))
        db.execute('INSERT OR REPLACE INTO store_meta VALUES (?,?)', ('source_high_water:'+stream['stream_id'],str(seq)))
    return {'observation_id':identity, 'seq':seq, 'duplicate':False, 'payload_id':payload_id}


def write_sample(root, row, *, legacy_mode=None, observation_id=None, source_checks=()):
    """Exactly one writer/parser selected from a fixed routing manifest."""
    if row.get('role') not in ROLES:
        raise ObservationGap('OBSERVATION_ROLE_INVALID')
    with routing_lock(root):
        manifest = load_manifest(root, legacy_mode=legacy_mode)
        matches = [item for item in manifest['streams'] if item['role'] == row['role'] and item.get('writer')]
        if len(matches) != 1:
            raise ObservationGap('OBSERVATION_WRITER_UNVERIFIABLE')
        stream = matches[0]
        if stream['parser_id'] == 'activity_v2':
            return _append_v2(root, manifest, stream, row, observation_id=observation_id, source_checks=source_checks)
        if stream.get('read_range') != 'LIVE':
            raise ObservationGap('OBSERVATION_LEGACY_WRITER_FROZEN')
        date = datetime.fromisoformat(str(row['at']).replace('Z', '+00:00')).astimezone(ZoneInfo('Asia/Shanghai')).date()
        _epoch(row['at'])
        path = _path(root, stream['locator'].replace('{date}', str(date)))
        with _file_lock(path.with_suffix('.lock')):
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('a') as output:
                output.write(json.dumps(row, ensure_ascii=False, allow_nan=False)+'\n')
        return {'storage_format':'activity_jsonl_v1', 'source': str(path.relative_to(Path(root).resolve()))}


@contextmanager
def _file_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _read_v1(root, stream, start, end, metrics):
    locators = []
    if stream['read_range'] == 'LIVE':
        date = datetime.fromtimestamp(start, ZoneInfo('Asia/Shanghai')).date()
        last = datetime.fromtimestamp(end, ZoneInfo('Asia/Shanghai')).date()
        while date <= last:
            locators.append(stream['locator'].replace('{date}', str(date)))
            date += timedelta(days=1)
    else:
        locators = [stream['locator']]
    rows = []
    for locator in locators:
        path = _path(root, locator)
        if not path.exists() and stream['read_range'] == 'LIVE':
            continue
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ObservationGap('OBSERVATION_SOURCE_MISSING:'+locator) from exc
        metrics['bytes_read'] += len(raw)
        lines = raw.splitlines()
        if stream['read_range'] != 'LIVE':
            if _sha(raw) != stream['sha256'] or len(lines) != stream['cutover_high_water']:
                raise ObservationGap('OBSERVATION_LEGACY_HIGH_WATER_DRIFT:'+locator)
            first, last = stream['read_range']
        else:
            first, last = 1, len(lines)
        for line_number in range(first,last+1):
            try:
                row = json.loads(lines[line_number-1])
                if not isinstance(row, dict) or row.get('role', stream['role']) != stream['role']:
                    raise ValueError('legacy row role mismatch')
                instant = _epoch(row['at'])
            except (ValueError, TypeError, KeyError, IndexError) as exc:
                raise ObservationGap('OBSERVATION_V1_ROW_INVALID:'+locator+':'+str(line_number)) from exc
            if start <= instant <= end:
                rows.append(row)
    return rows


def _restore(record, manifest, stream, *, payload_cache=None):
    cache = payload_cache if payload_cache is not None else {}
    body = cache.get(record['payload_id'])
    if body is None:
        body = json.loads(record['body'])
        cache[record['payload_id']] = body
    if (record['schema_version'] != 2 or body.get('schema_version') != 2 or
            body.get('classifier_version') != manifest['classifier_version'] or
            record['classifier_version'] != manifest['classifier_version'] or
            _sha(record['body'].encode()) != record['payload_id'] or
            record['collector_epoch'] != manifest['writer_epoch']):
        raise ObservationGap('OBSERVATION_PAYLOAD_VERSION_OR_HASH_INVALID')
    row = {**body['fields'], **json.loads(record['row_extras']), **json.loads(record['original_times'])}
    source = json.loads(record['source'])
    if source['present']:
        row['source'] = source['value']
    if row.get('role', stream['role']) != stream['role'] or _sha(row) != record['row_sha256']:
        raise ObservationGap('OBSERVATION_RECONSTRUCTED_ROW_MISMATCH')
    return row


def read_samples(root, role, start, end, *, legacy_mode=None):
    """Dual read, one fixed manifest and committed high water per query.

    Caller retains the original points merge, ordering and interval semantics.
    No virtual sample is generated from a coverage run. Returned metrics include
    the pinned source high waters and actual serialized bytes read.
    """
    if role not in ROLES:
        raise ObservationGap('OBSERVATION_ROLE_INVALID')
    left, right = _epoch(start)-MAX_GAP, _epoch(end)
    rows, metrics = [], {'bytes_read':0, 'observations_read':0, 'source_high_water':{}}
    wall, cpu, io = time.monotonic(), time.process_time(), resource.getrusage(resource.RUSAGE_SELF)
    with routing_lock(root, readonly=True):
        manifest = load_manifest(root, legacy_mode=legacy_mode)
        metrics.update(manifest_id=manifest['manifest_id'], writer_epoch=manifest['writer_epoch'], mode=manifest['mode'])
        for stream in manifest['streams']:
            if stream['role'] != role:
                continue
            if stream['parser_id'] == 'activity_v1':
                rows.extend(_read_v1(root, stream, left, right, metrics))
                metrics['source_high_water'][stream['stream_id']] = stream['read_range']
                continue
            with _db(_path(root, stream['locator']), readonly=True) as db:
                db.execute('BEGIN')
                _audit(db)
                first, supplied_high = stream['read_range']
                actual = db.execute('SELECT MAX(seq) FROM observations WHERE stream_id=?', (stream['stream_id'],)).fetchone()[0] or 0
                high = actual if supplied_high is None else supplied_high
                if high > actual:
                    raise ObservationGap('OBSERVATION_READ_HIGH_WATER_MISSING')
                metrics['source_high_water'][stream['stream_id']] = high
                records = db.execute('''SELECT o.* FROM observations o
                    WHERE o.stream_id=? AND o.seq>=? AND o.seq<=?
                    AND o.observed_at>=? AND o.observed_at<=? ORDER BY o.seq''',
                    (stream['stream_id'],first,high,left,right))
                payload_records, payload_cache = {}, {}
                for record in records:
                    metrics['bytes_read'] += sum(len(str(record[key]).encode()) for key in record.keys())
                    payload = payload_records.get(record['payload_id'])
                    if payload is None:
                        payload = db.execute('SELECT body,schema_version,classifier_version FROM payloads WHERE payload_id=?',
                                             (record['payload_id'],)).fetchone()
                        if payload is None:
                            raise ObservationGap('OBSERVATION_PAYLOAD_MISSING')
                        payload_records[record['payload_id']] = payload
                        metrics['bytes_read'] += sum(len(str(payload[key]).encode()) for key in payload.keys())
                    checks = [row[0] for row in db.execute('SELECT source_id FROM source_checks WHERE seq=? ORDER BY source_id', (record['seq'],))]
                    if checks != sorted(json.loads(record['source_checks_ref'])):
                        raise ObservationGap('OBSERVATION_SOURCE_CHECK_REFERENCE_MISSING')
                    row = _restore({**dict(record),**dict(payload)}, manifest, stream, payload_cache=payload_cache)
                    rows.append(row)
        metrics['observations_read'] = len(rows)
        metrics['read_high_water'] = deepcopy(metrics['source_high_water'])
        metrics['classifier_version'] = manifest['classifier_version']
    after = resource.getrusage(resource.RUSAGE_SELF)
    metrics.update(wall_seconds=time.monotonic()-wall, cpu_seconds=time.process_time()-cpu,
        process_input_blocks=after.ru_inblock-io.ru_inblock, process_output_blocks=after.ru_oublock-io.ru_oublock,
        bytes_read_basis='serialized window rows and distinct payloads returned by reader; excludes SQLite integrity-index pages')
    return rows, metrics


def prepare_cutover(root, *, writer_epoch, writer_stopped_ref):
    """Freeze exact v1 sources, then prepare a reviewable manifest (no publish).

    The caller must first stop ALL old writers, including already-loaded code.
    File locks alone cannot stop such a writer from appending after cutover.
    writer_stopped_ref records that external prerequisite; this module cannot
    manufacture it. Publication rechecks every source under its original lock.
    """
    if (not isinstance(writer_epoch, str) or not writer_epoch or writer_epoch == 'legacy' or
            not isinstance(writer_stopped_ref,str) or not writer_stopped_ref.strip()):
        raise ObservationGap('OBSERVATION_CUTOVER_STOP_RECEIPT_AND_NEW_EPOCH_REQUIRED')
    root = Path(root).resolve()
    with routing_lock(root):
        current = load_manifest(root, legacy_mode='LEGACY_ONLY')
        if current['mode'] != 'LEGACY_ONLY':
            raise ObservationGap('OBSERVATION_ALREADY_SWITCHED')
        streams = []
        for role in ROLES:
            role_streams = [item for item in current['streams'] if item['role'] == role]
            for prior in role_streams:
                if prior['read_range'] != 'LIVE':
                    frozen = deepcopy(prior)
                    frozen.pop('writer',None)
                    _read_v1(root,frozen,float('-inf'),float('inf'),{'bytes_read':0})
                    streams.append(frozen)
                    continue
                pattern = prior['locator'].replace('{date}','*')
                for path in sorted(root.glob(pattern)):
                    path = _path(root,str(path.relative_to(root)))
                    with _file_lock(path.with_suffix('.lock')):
                        raw = path.read_bytes()
                    if raw and not raw.endswith(b'\n'):
                        raise ObservationGap('OBSERVATION_INCOMPLETE_LEGACY_TAIL:'+str(path))
                    count = len(raw.splitlines())
                    if count:
                        locator = str(path.relative_to(root))
                        streams.append({'stream_id':'frozen-'+_sha(locator.encode())[:20], 'source_id':prior['source_id'],
                            'role':role,'locator':locator,'storage_format':'activity_jsonl_v1','schema_version':1,
                            'parser_id':'activity_v1','read_range':[1,count], 'cutover_high_water':count,'sha256':_sha(raw)})
            streams.append({'stream_id':'v2-'+_sha(writer_epoch.encode())[:12]+'-'+role,'source_id':'observations_v2','role':role,'locator':STORE,
                'storage_format':'observation_store_v2','schema_version':2,'parser_id':'activity_v2',
                'read_range':[1,None],'cutover_high_water':0,'writer':True})
        value = {'manifest_version':1,'mode':'DUAL_READ_V2','writer_epoch':writer_epoch,
            'classifier_version':CLASSIFIER_VERSION,'source_mapping':{**current['source_mapping'],
                'observations_v2':'committed v2 real observation skeletons'},'streams':streams,
            'writer_stopped_ref':writer_stopped_ref,'previous_manifest_id':current['manifest_id']}
        value['manifest_id'] = _sha(value)
        return validate_manifest(root,value)


def publish_manifest(root, manifest, *, expected_manifest_id):
    """Explicit cutover/recovery only; never starts or stops a business worker."""
    value = validate_manifest(root,manifest)
    if not isinstance(value.get('writer_stopped_ref'),str) or not value['writer_stopped_ref'].strip():
        raise ObservationGap('OBSERVATION_WRITER_STOP_RECEIPT_REQUIRED')
    with routing_lock(root), ExitStack() as locks:
        try:
            previous = load_manifest(root,legacy_mode='LEGACY_ONLY')
        except ObservationGap:
            # Crash after durable requirement marker but before manifest publish:
            # only the exact prepared publication is allowed to finish.
            marker = json.loads(_path(root,REQUIRED).read_bytes())
            if marker.get('manifest_id') != value['manifest_id'] or marker.get('writer_epoch') != value['writer_epoch']:
                raise
            previous = {'manifest_id':value.get('previous_manifest_id')}
        if previous['manifest_id'] != expected_manifest_id:
            raise ObservationGap('OBSERVATION_MANIFEST_COMPARE_AND_SWAP_FAILED')
        for stream in value['streams']:
            if stream['parser_id'] == 'activity_v1' and stream['read_range'] != 'LIVE':
                path = _path(root,stream['locator'])
                locks.enter_context(_file_lock(path.with_suffix('.lock')))
                _read_v1(root,stream,float('-inf'),float('inf'),{'bytes_read':0})
        for locator in {item['locator'] for item in value['streams'] if item['parser_id']=='activity_v2'}:
            with _db(_path(root,locator)) as db:
                _audit(db)
        _atomic(_path(root,REQUIRED),{'manifest_id':value['manifest_id'],'writer_epoch':value['writer_epoch']})
        _atomic(_path(root,MANIFEST),value)
    return {'manifest_id':value['manifest_id'],'writer_epoch':value['writer_epoch'],'mode':value['mode']}


def coverage_runs(root, role):
    """Rebuild a per-source index; failures truncate only the failed source.

    These intervals never replace real skeletons or the original report reader.
    Recovery starts at its real check, with no historical backfill.
    """
    runs = []
    with routing_lock(root, readonly=True):
        manifest = load_manifest(root)
        for stream in manifest['streams']:
            if stream['role'] != role or stream['parser_id'] != 'activity_v2':
                continue
            with _db(_path(root,stream['locator']),readonly=True) as db:
                db.execute('BEGIN')
                _audit(db)
                first,last = stream['read_range']
                high = db.execute('SELECT MAX(seq) FROM observations WHERE stream_id=?',(stream['stream_id'],)).fetchone()[0] or 0
                last = high if last is None else last
                active = {}
                records = db.execute('''SELECT o.*,p.body,p.schema_version,p.classifier_version FROM observations o
                    JOIN payloads p ON p.payload_id=o.payload_id WHERE o.stream_id=? AND o.seq>=? AND o.seq<=?
                    ORDER BY o.observed_at,o.seq''',(stream['stream_id'],first,last))
                for row in records:
                    payload = json.loads(row['body'])
                    checks = [json.loads(raw) for (raw,) in db.execute(
                        'SELECT body FROM source_checks WHERE seq=? ORDER BY source_id',(row['seq'],))]
                    by_source = {check['source_id']:check for check in checks}
                    # A recorded state change bounds every prior source-derived
                    # component, even if this row lacks a check for that source.
                    for source,old in list(active.items()):
                        if old['payload_id'] != row['payload_id']:
                            check = by_source.get(source,{})
                            failed_at = (_epoch(check['observed_at']) if check and check['check_status']!='VERIFIED'
                                         else row['observed_at'])
                            cutoff = min(row['observed_at'],failed_at)
                            old.update(coverage_until=min(old['coverage_until'],cutoff),
                                       closed_at=cutoff,gap_reason='state_changed')
                            active.pop(source,None)
                    for check in checks:
                        source = check['source_id']; at = _epoch(check['observed_at'])
                        if check['check_status']=='VERIFIED':
                            at = max(row['observed_at'],at)  # no backdating a new derived projection
                        old = active.get(source)
                        reasons_changed = old and (old.get('reason_codes') != check.get('reason_codes') or
                            old.get('source_version') != check.get('source_version'))
                        if old and (reasons_changed or check['check_status'] != 'VERIFIED'):
                            old['coverage_until'] = min(old['coverage_until'],at)
                            old.update(last_observed_at=at,closed_at=at,
                                gap_reason='source_reason_or_version_changed' if check['check_status']=='VERIFIED' else check['check_status'])
                            active.pop(source,None); old = None
                        if check['check_status'] != 'VERIFIED':
                            continue
                        until = check['last_verified_at']+check['max_gap_seconds']
                        if until <= at:
                            continue
                        if old and old['coverage_until'] >= at:
                            old.update(last_observed_at=at,last_verified_at=check['last_verified_at'],
                                       coverage_until=max(old['coverage_until'],until),through_observation_id=row['observation_id'])
                        else:
                            run = {'identity':payload['identity'],'payload_id':row['payload_id'],'source_id':source,
                                'source_version':check.get('source_version'),'reason_codes':check.get('reason_codes'),
                                'state_since':at,'last_observed_at':at,'last_verified_at':check['last_verified_at'],
                                'coverage_until':until,'closed_at':None,'gap_reason':None,
                                'through_observation_id':row['observation_id']}
                            if old:
                                old.update(closed_at=old['coverage_until'],gap_reason='validity_expired')
                            runs.append(run);active[source]=run
    return runs


def export_real_rows(root, role, destination):
    """Export real rows for an explicit rollback; no synthetic cadence or writer."""
    destination = Path(destination)
    if destination.exists():
        raise ObservationGap('OBSERVATION_EXPORT_DESTINATION_EXISTS')
    with routing_lock(root, readonly=True):
        if load_manifest(root)['mode'] != 'DUAL_READ_V2':
            raise ObservationGap('OBSERVATION_EXPORT_REQUIRES_V2_ROUTING')
    # The old interval reader only accepts timestamped rows. An unrepresentable
    # row/reference causes read_samples to stop, leaving both writers untouched.
    rows,_ = read_samples(root,role,-10**12,10**12)
    with destination.open('x') as stream:
        for row in sorted(enumerate(rows),key=lambda pair:(_epoch(pair[1]['at']),pair[0])):
            stream.write(_json(row[1])+'\n')
        stream.flush();os.fsync(stream.fileno())
    return {'role':role,'rows':len(rows),'sha256':_sha(destination.read_bytes()),'writer_enabled':False}


def prepare_rollback(root, *, writer_epoch, writer_stopped_ref):
    """Export a stopped v2 sequence and prepare a single v1 writer manifest.

    The export is new immutable evidence. Its source high waters are retained;
    no old frozen file is reopened for appending. publish_manifest is still an
    explicit, separate operation and verifies every exported byte. Failure or
    a concurrent routing change leaves the active routing unchanged.
    """
    if (not isinstance(writer_epoch,str) or not writer_epoch or writer_epoch == 'legacy'
            or not isinstance(writer_stopped_ref,str) or not writer_stopped_ref.strip()):
        raise ObservationGap('OBSERVATION_ROLLBACK_STOP_RECEIPT_AND_NEW_EPOCH_REQUIRED')
    root = Path(root).resolve()
    with routing_lock(root):
        current = load_manifest(root)
        if current['mode'] != 'DUAL_READ_V2' or current['writer_epoch'] == writer_epoch:
            raise ObservationGap('OBSERVATION_ROLLBACK_SOURCE_OR_EPOCH_INVALID')
    target = BASE+'/activity-rollback/'+_sha(writer_epoch.encode())[:24]
    folder = _path(root,target)
    folder.mkdir(parents=True,exist_ok=False)
    streams, high_waters = [], {}
    for role in ROLES:
        rows, metrics = read_samples(root,role,-10**12,10**12)
        if metrics['manifest_id'] != current['manifest_id']:
            raise ObservationGap('OBSERVATION_ROUTING_CHANGED_DURING_ROLLBACK')
        high_waters.update(metrics['source_high_water'])
        if rows:
            locator = target+'/frozen-'+role+'.jsonl'
            destination = _path(root,locator)
            with destination.open('x') as output:
                os.chmod(destination,0o600)
                for _,row in sorted(enumerate(rows),key=lambda pair:(_epoch(pair[1]['at']),pair[0])):
                    output.write(_json(row)+'\n')
                output.flush();os.fsync(output.fileno())
            streams.append({'stream_id':'rollback-export-'+role,'source_id':'rollback_export',
                'role':role,'locator':locator,'storage_format':'activity_jsonl_v1','schema_version':1,
                'parser_id':'activity_v1','read_range':[1,len(rows)],'cutover_high_water':len(rows),
                'sha256':_sha(destination.read_bytes())})
        streams.append({'stream_id':'rollback-live-'+role,'source_id':'rollback_writer','role':role,
            'locator':target+'/{date}/'+role+'.jsonl','storage_format':'activity_jsonl_v1','schema_version':1,
            'parser_id':'activity_v1','read_range':'LIVE','writer':True})
    value = {'manifest_version':1,'mode':'LEGACY_ONLY','writer_epoch':writer_epoch,
        'classifier_version':current['classifier_version'],'source_mapping':{'rollback_export':
            {'manifest_id':current['manifest_id'],'source_high_water':high_waters},
            'rollback_writer':'new legacy writer after explicit v2 rollback'},'streams':streams,
        'writer_stopped_ref':writer_stopped_ref,'previous_manifest_id':current['manifest_id']}
    value['manifest_id'] = _sha(value)
    return validate_manifest(root,value)


def storage_metrics(root):
    """Measure physical and logical costs without implying model/token savings."""
    path = _path(root,STORE)
    with _db(path,readonly=True) as db:
        payload_bytes = db.execute('SELECT COALESCE(SUM(length(CAST(body AS BLOB))),0) FROM payloads').fetchone()[0]
        skeleton_bytes = db.execute('''SELECT COALESCE(SUM(length(CAST(original_times AS BLOB))+
            length(CAST(source AS BLOB))+length(CAST(row_extras AS BLOB))+length(payload_id)+
            length(observation_id)+length(stream_id)+length(collector_epoch)+length(row_sha256)+
            length(CAST(source_checks_ref AS BLOB))+16),0) FROM observations''').fetchone()[0]
        count = db.execute('SELECT COUNT(*) FROM observations').fetchone()[0]
        payloads = db.execute('SELECT COUNT(*) FROM payloads').fetchone()[0]
        check_bytes = db.execute('SELECT COALESCE(SUM(length(CAST(body AS BLOB))),0) FROM source_checks').fetchone()[0]
    return {'schema_version':2,'payload_count':payloads,'observation_count':count,
        'payload_bytes':payload_bytes,'skeleton_bytes':skeleton_bytes,'source_check_bytes':check_bytes,
        'database_bytes':path.stat().st_size,'sample_transactions':count,'model_token_savings':None}
