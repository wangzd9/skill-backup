"""Indexed mechanical outbox, evidence-preserving summaries and rolling budgets.

JSON records remain the recoverable evidence. SQLite is a rebuildable delivery
index, never business authority. Legacy inventory is explicit and bounded;
steady-state drains query pending rows without glob/sort of terminal history.
"""
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import time

import role_runtime as runtime

PENDING = ('PENDING', 'QUIET_DEFERRED')
DEFAULT_POLICY = {'schema_version': 1, 'window_seconds': 3600, 'pool_limit': 12,
                  'group_limit': 3, 'group_gap_seconds': 300,
                  # Display handoff age only: SUPERSEDED sources remain in the
                  # evidence/member index. This is never an incident expiry.
                  'history_age_seconds': 6*3600, 'backlog_threshold': 50, 'min_summary_members': 5}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def coalescing_key(event):
    if not event.get('episode'):
        return None
    return repr(tuple(event.get(k) for k in ('pool_path','generation','run_id','role','work_id',
        'execution_id','identity','episode','transition','severity')))


def policy(outbox, override=None):
    path = Path(outbox).parent/'delivery-policy.json'
    value = {**DEFAULT_POLICY, **(runtime.load(path) if path.exists() else {}), **(override or {})}
    if set(value)!=set(DEFAULT_POLICY) or type(value['schema_version']) is not int or value['schema_version'] != 1 or any(type(value[k]) is not int or value[k] < 1 for k in (
            'window_seconds', 'pool_limit', 'group_limit', 'history_age_seconds', 'backlog_threshold', 'min_summary_members')):
        raise ValueError('INVALID_PERSISTENT_NOTIFICATION_POLICY')
    if type(value['group_gap_seconds']) not in (int, float) or not math.isfinite(value['group_gap_seconds']) or value['group_gap_seconds'] < 0:
        raise ValueError('INVALID_PERSISTENT_NOTIFICATION_POLICY')
    return value


@contextmanager
def database(outbox):
    db = sqlite3.connect(Path(outbox)/'.delivery-index.sqlite3', timeout=.1)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA journal_mode=PERSIST')
        db.executescript('''
          CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS queue(name TEXT PRIMARY KEY,event_id TEXT NOT NULL,
            role TEXT NOT NULL,state TEXT NOT NULL,priority INTEGER NOT NULL,generated REAL,
            summary_key TEXT,body TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS pending ON queue(state,role,priority,name);
          CREATE INDEX IF NOT EXISTS summary_pending ON queue(summary_key,state);
          CREATE TABLE IF NOT EXISTS summary_members(summary_id TEXT NOT NULL,name TEXT NOT NULL,
            event_id TEXT NOT NULL,source_sha256 TEXT NOT NULL,disposition TEXT NOT NULL,
            PRIMARY KEY(summary_id,name));
          CREATE TABLE IF NOT EXISTS attempts(attempt TEXT PRIMARY KEY,at REAL NOT NULL,
            group_key TEXT NOT NULL,event_id TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS attempts_window ON attempts(at,group_key);
        ''')
        yield db
        db.commit()
    finally:
        db.close()


def get(db, key, default=None):
    row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def put(db, key, value):
    db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, json.dumps(value)))


def _time(value):
    if type(value) in (int, float) and math.isfinite(value):
        return float(value)
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo else None
    except (TypeError, ValueError, OverflowError):
        return None


def upsert(db, path, row):
    event, receipt = row['event'], row['delivery_receipt']
    if not isinstance(event, dict) or not isinstance(receipt, dict) or not event.get('event_id'):
        raise ValueError('INVALID_OUTBOX_RECORD')
    transition = event.get('transition')
    priority = 0 if transition == 'escalated' else 1 if transition == 'first' else 2
    db.execute('INSERT OR REPLACE INTO queue VALUES (?,?,?,?,?,?,?,?)', (
        Path(path).name, str(event['event_id']), event.get('role') if event.get('role') in runtime.ROLES else 'COORDINATOR', receipt['state'],
        priority, _time(event.get('generated_at', event.get('occurred_at'))),
        event.get('summary_key'), json.dumps(row, ensure_ascii=False, sort_keys=True)))
    for member in row.get('summary_members', []):
        db.execute('INSERT OR REPLACE INTO summary_members VALUES (?,?,?,?,?)',
            (str(event['event_id']), member['name'], member['event_id'], member['source_sha256'], member['disposition']))
    attempted = _time(receipt.get('attempted_at'))
    if attempted is not None and receipt.get('attempt') is not None:
        db.execute('INSERT OR IGNORE INTO attempts VALUES (?,?,?,?)',
            (str(receipt['attempt']),attempted,receipt.get('budget_group_key') or group_key(event),str(event['event_id'])))
        put(db,'last_budget_time',max(attempted,get(db,'last_budget_time',attempted)))


def signature(outbox):
    stat = Path(outbox).stat()
    return [stat.st_mtime_ns, stat.st_ino]


def register(path, row):
    """Producer registration; interrupted writes are found by inventory check."""
    path = Path(path)
    with database(path.parent) as db:
        upsert(db, path, row)
    # Do not certify the whole inventory from a single producer append.


def enqueue(path, row):
    from monitor_notifications import delivery_lock
    path = Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    with delivery_lock(path.parent,time.monotonic()+2), database(path.parent) as db:
        before = signature(path.parent)
        ready = get(db,'directory_signature') == before
        if path.exists():
            return
        write(db,path,row)
        if ready:
            put(db,'directory_signature',signature(path.parent))


def inventory(outbox, db, deadline, *, max_items=100000):
    """One-time/recovery import; fail closed if interrupted or over budget.

    Files produced outside register() invalidate the directory signature. No
    sending occurs against a partially imported legacy inventory.
    """
    if get(db, 'directory_signature') == signature(outbox):
        return True
    before = signature(outbox)
    count = 0
    with os.scandir(outbox) as entries:
        for entry in entries:
            if not entry.name.endswith('.json'):
                continue
            if time.monotonic() >= deadline or count >= max_items:
                return False
            if not entry.is_file(follow_symlinks=False):
                raise ValueError('INVALID_OUTBOX_FILE')
            path = Path(entry.path)
            upsert(db, path, runtime.load(path))
            count += 1
    if signature(outbox) != before:
        return False
    put(db, 'directory_signature', before)
    return True


def write(db, path, row):
    runtime.save(path, row)
    upsert(db, path, row)


def _state(outbox, now):
    try:
        state = runtime.load(Path(outbox).parent/'state.json')
        snapshot = runtime.load(Path(outbox).parent/'snapshot.json')
        observed = state['observed_at']
        if (not isinstance(state.get('active'), dict) or type(observed) not in (int,float)
                or observed != snapshot['at'] or not 0 <= now-observed <= 600):
            return {}, False, None
        return state['active'], True, observed
    except (OSError, ValueError, KeyError, TypeError):
        return {}, False, None


def disposition(member, active, fresh):
    current=active.get(member.get('identity'))
    if fresh and current and current.get('episode')==member.get('episode'):
        return 'CURRENT_OPEN'
    if member.get('transition')=='recovered':
        return 'HISTORICAL_RECOVERY'
    return 'NOT_IN_ACTIVE_SCOPE' if fresh else 'SNAPSHOT_STALE'


def summary_delivery_reason(event, config):
    if not event.get('summary_key'):
        return None
    if event.get('type')!='observer_history_summary' or event.get('lifecycle')!='GENERATION_SCOPE':
        return 'SUMMARY_LIFECYCLE_UNVERIFIED'
    if event.get('member_count',0)<config['min_summary_members']:
        return 'MIN_SUMMARY_MEMBERS'
    return None


def logical_summary(db, outbox, key):
    """One lifetime per (pool,generation,role), including SENT/UNKNOWN.

    Delivery is at most once, membership continues until that generation scope
    stops producing events. SENT is a receipt, never the end of this lifetime.
    Historic duplicate receipts remain evidence; unsent duplicates are replaced.
    """
    rows=db.execute("""SELECT name,body FROM queue WHERE summary_key=?
        ORDER BY CASE WHEN state IN ('SENT','UNKNOWN','ARCHIVED') THEN 0 ELSE 1 END,
        generated,name""",(key,)).fetchall()
    if not rows:
        return None
    canonical=json.loads(rows[0]['body']);path=Path(outbox)/rows[0]['name']
    members={m['name']:m for m in canonical.get('summary_members',[])}
    for item in rows:
        row=json.loads(item['body'])
        for member in row.get('summary_members',[]):
            previous=members.get(member['name'])
            if previous and any(previous[k]!=member[k] for k in ('event_id','source_sha256')):
                raise ValueError('SUMMARY_MEMBER_EVIDENCE_CONFLICT')
            members.setdefault(member['name'],member)
        # Preserve the exact historical transport payload/receipt before
        # appending members to a logical summary (also covers legacy sends).
        refs=canonical.setdefault('previous_delivery_records',[])
        if (row['delivery_receipt']['state'] in ('SENT','UNKNOWN')
                and not any(r['event_id']==row['event']['event_id'] for r in refs)):
            raw=item['body'].encode();sha=hashlib.sha256(raw).hexdigest()
            folder=Path(outbox)/'evidence'/sha[:2];folder.mkdir(parents=True,exist_ok=True)
            frozen=folder/(sha+'.json')
            if frozen.exists() and frozen.read_bytes()!=raw:
                raise ValueError('SOURCE_EVIDENCE_HASH_CONFLICT')
            if not frozen.exists():
                temporary=frozen.with_suffix('.tmp');temporary.write_bytes(raw);temporary.replace(frozen)
            refs.append({'path':str(frozen),'sha256':sha,'event_id':row['event']['event_id']})
        if item['name']!=path.name and row['delivery_receipt']['state'] in PENDING:
            row['delivery_receipt'].update(state='SUPERSEDED',reason='logical_summary_reuse',
                superseded_by=canonical['event']['event_id'])
            write(db,Path(outbox)/item['name'],row)
    canonical['summary_members']=list(members.values())
    event=canonical['event'];event['lifecycle']='GENERATION_SCOPE'
    event['member_count']=len(members)
    event['counts']={}
    for member in members.values():
        label=member['disposition'];event['counts'][label]=event['counts'].get(label,0)+1
    event['member_chain_sha256']=digest(list(members.values()))
    return path,canonical


def compact(outbox, db, now, config, *, limit=256, deadline=None):
    """Replace stale display intents, retaining every source and membership hash.

    This never closes an incident. Unknown/absent current facts are described
    as unverified, rather than silently expired or asserted recovered.
    """
    # Adopt pre-upgrade pending summaries even without a new ordinary source.
    # A historical SENT/UNKNOWN sibling suppresses all unsent duplicates.
    pending_keys=[r[0] for r in db.execute("SELECT DISTINCT summary_key FROM queue WHERE summary_key IS NOT NULL AND state IN ('PENDING','QUIET_DEFERRED')")]
    for key in pending_keys:
        existing=logical_summary(db,outbox,key)
        if existing:
            write(db,*existing)
    total = db.execute("SELECT COUNT(*) FROM queue WHERE state IN ('PENDING','QUIET_DEFERRED') AND summary_key IS NULL").fetchone()[0]
    overloaded_roles=set()
    for row in db.execute("""SELECT role,COUNT(*) AS n FROM queue
            WHERE state IN ('PENDING','QUIET_DEFERRED') GROUP BY role HAVING n>?""",(config['group_limit'],)):
        distinct=set()
        for item in db.execute("SELECT body FROM queue WHERE role=? AND state IN ('PENDING','QUIET_DEFERRED')",(row['role'],)):
            event=json.loads(item['body'])['event']
            distinct.add(coalescing_key(event) or str(event['event_id']))
            if len(distinct)>config['group_limit']:
                overloaded_roles.add(row['role']); break
    active, fresh, observed_at = _state(outbox, now)
    after = get(db,'compaction_after','')
    candidates = db.execute("""SELECT * FROM queue WHERE state IN ('PENDING','QUIET_DEFERRED')
        AND summary_key IS NULL AND name>? ORDER BY name LIMIT ?""", (after,limit)).fetchall()
    if not candidates and after:
        candidates = db.execute("""SELECT * FROM queue WHERE state IN ('PENDING','QUIET_DEFERRED')
            AND summary_key IS NULL ORDER BY name LIMIT ?""", (limit,)).fetchall()
    summaries = {}
    replacements = []
    incomplete = False
    for item in candidates:
        if deadline is not None and time.monotonic() >= deadline:
            incomplete = True
            break
        row = json.loads(item['body']); event = row['event']
        generated = item['generated']
        # Modern events are checked against the latest saved projection even
        # before the age limit. Missing evidence remains visible in a summary.
        current = active.get(event.get('identity'))
        matching = fresh and current and current.get('episode') == event.get('episode')
        changed = (fresh and event.get('episode') and
                   ((event.get('transition') in ('first','escalated') and not matching) or
                    (event.get('transition') == 'recovered' and current)))
        blind = bool(event.get('type') and event.get('episode') and not fresh)
        if not (item['role'] in overloaded_roles or total >= config['backlog_threshold'] or generated is not None and now-generated > config['history_age_seconds'] or changed or blind):
            continue
        group = {k: event.get(k) for k in ('pool_path','generation','role')}
        key = digest(['history-handoff/v1', group])
        if key not in summaries:
            existing = logical_summary(db,outbox,key)
            if existing:
                summary_path, summary = existing
            else:
                sid = digest(['logical-history-summary/v2',key])
                payload = {**group, 'event_id': sid, 'type': 'observer_history_summary', 'transition': 'history_summary',
                    'severity': 1, 'summary_key': key, 'lifecycle': 'GENERATION_SCOPE', 'generated_at': now, 'observed_at': now,
                    'source': str(Path(outbox)/'.delivery-index.sqlite3'),
                    'member_count': 0, 'member_chain_sha256': digest([]),
                    'counts': {}, 'current_state': 'UNVERIFIED', 'oldest_generated_at': None,
                    'instruction': '历史观察摘要不代表当前已恢复，不授权接管或重试；源事件及替代关系保留。'}
                summary = {'schema_version': 2, 'event': payload, 'summary_members': [],
                    'generation_receipt': {'state': 'GENERATED', 'at': now},
                    'delivery_receipt': {'state': 'PENDING', 'reason': 'history_handoff_summary'}}
                summary_path = Path(outbox)/(sid+'.json')
            summaries[key] = summary_path, summary
        summary_path, summary = summaries[key]
        payload = summary['event']
        member_disposition = disposition(event,active,fresh)
        raw = (Path(outbox)/item['name']).read_bytes()
        source_hash = hashlib.sha256(raw).hexdigest()
        frozen = Path(outbox)/'evidence'/source_hash[:2]/(source_hash+'.json')
        frozen.parent.mkdir(parents=True,exist_ok=True)
        if frozen.exists() and frozen.read_bytes()!=raw:
            raise ValueError('SOURCE_EVIDENCE_HASH_CONFLICT')
        if not frozen.exists():
            temporary=frozen.with_suffix('.tmp'); temporary.write_bytes(raw); temporary.replace(frozen)
        members = summary.setdefault('summary_members', [])
        if not any(member['name']==item['name'] for member in members):
            members.append({'name':item['name'],'event_id':item['event_id'],'source_sha256':source_hash,
                'disposition':member_disposition,'identity':event.get('identity'),'episode':event.get('episode'),
                'transition':event.get('transition'),'generated_at':generated})
            payload['member_count'] += 1
            payload['counts'][member_disposition] = payload['counts'].get(member_disposition,0)+1
            payload['member_chain_sha256'] = digest([payload['member_chain_sha256'], item['name'], source_hash, member_disposition])
        if generated is not None:
            previous = payload['oldest_generated_at']
            payload['oldest_generated_at'] = generated if previous is None else min(generated,previous)
        payload['current_state'] = 'OBSERVED_AT_PREPARATION' if fresh else 'SNAPSHOT_STALE'
        payload['observed_at'] = observed_at
        summary['generation_receipt'].update(sha256=digest(payload), at=now)
        row['delivery_receipt'].update(state='SUPERSEDED', reason='history_summary_replacement',
            superseded_by=payload['event_id'], source_sha256=source_hash, replaced_at=now)
        replacements.append((Path(outbox)/item['name'],row))
    # Commit replacement files first, then source pointers. Rebuilding the SQL
    # index from either side of a crash preserves all membership and evidence.
    for path,summary in summaries.values():
        write(db,path,summary)
    for path,row in replacements:
        write(db,path,row)
    put(db,'compaction_after',candidates[-1]['name'] if candidates else '')
    put(db,'compaction_incomplete',incomplete)
    return len(summaries)


def needs_handoff(outbox, event, now, config):
    if event.get('summary_key'):
        # A summary is already a display handoff; only a validated logical
        # lifetime with enough retained members may bypass source re-handoff.
        return summary_delivery_reason(event,config) is not None
    generated = _time(event.get('generated_at',event.get('occurred_at')))
    if generated is not None and now-generated > config['history_age_seconds']:
        return True
    active,fresh,_ = _state(outbox,now)
    if event.get('type') and event.get('episode') and not fresh:
        return True
    if fresh and event.get('episode'):
        current = active.get(event.get('identity'))
        if event.get('transition') in ('first','escalated'):
            return not current or current.get('episode') != event['episode']
        if event.get('transition') == 'recovered':
            return bool(current)
    return False


def refresh_summary(outbox, row, now):
    event=row['event']
    if not event.get('summary_key'):
        return
    active,fresh,observed_at=_state(outbox,now)
    counts={}
    incidents=set()
    for member in row.get('summary_members',[]):
        key=disposition(member,active,fresh)
        counts[key]=counts.get(key,0)+1
        if key=='CURRENT_OPEN':
            incidents.add((member.get('identity'),member.get('episode')))
    event.update(current_counts=counts,current_state='OBSERVED_AT_SEND' if fresh else 'SNAPSHOT_STALE',
        current_incident_count=len(incidents),rechecked_at=now,observed_at=observed_at)
    row['generation_receipt'].update(sha256=digest(event))


def group_key(event):
    role = event.get('role') or 'COORDINATOR'
    try:
        pool = runtime.load(Path(event['pool_path']))
        chat = pool['roles'][role]['chat_id']
        if not isinstance(chat, str) or not chat:
            raise ValueError('missing chat')
        return digest(['chat',chat])
    except (OSError,ValueError,KeyError,TypeError):
        return digest(['role',role])


def reserve(db, event, now, attempt, config):
    previous = get(db, 'last_budget_time', now)
    if now < previous:
        return 'CLOCK_ROLLBACK'
    put(db, 'last_budget_time', now)
    group = group_key(event)
    rows = db.execute('SELECT group_key,at FROM attempts WHERE at>?', (now-config['window_seconds'],)).fetchall()
    same = [row['at'] for row in rows if row['group_key']==group]
    if len(rows) >= config['pool_limit'] or len(same) >= config['group_limit']:
        return 'ROLLING_WINDOW_BUDGET'
    if same and now-max(same) < config['group_gap_seconds']:
        return 'GROUP_MINIMUM_GAP'
    db.execute('INSERT INTO attempts VALUES (?,?,?,?)', (str(attempt),now,group,str(event['event_id'])))
    put(db, 'last_role', event.get('role') or 'COORDINATOR')
    return None


def delivery_status(outbox, *, at=None):
    """Read the existing index without sending, reserving quota or creating it."""
    outbox = Path(outbox)
    now = time.time() if at is None else float(at)
    path = outbox/'.delivery-index.sqlite3'
    config = policy(outbox)
    with sqlite3.connect(path.as_uri()+'?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        attempts = db.execute('SELECT group_key,at FROM attempts WHERE at>? ORDER BY at',
                              (now-config['window_seconds'],)).fetchall()
        rows = db.execute("SELECT role,body,generated FROM queue WHERE state IN ('PENDING','QUIET_DEFERRED')").fetchall()
        groups = {}
        for row in rows:
            event = json.loads(row['body'])['event']
            key = group_key(event)
            item = groups.setdefault(key, {'roles':set(), 'pending':0, 'oldest_generated_at':None})
            item['roles'].add(row['role']); item['pending'] += 1
            if row['generated'] is not None:
                item['oldest_generated_at'] = min(item['oldest_generated_at'] or row['generated'], row['generated'])
        for key,item in groups.items():
            same = [r['at'] for r in attempts if r['group_key']==key]
            available = now
            reasons = []
            for values,limit,label in (([r['at'] for r in attempts],config['pool_limit'],'POOL_WINDOW'),
                                       (same,config['group_limit'],'GROUP_WINDOW')):
                if len(values)>=limit:
                    available=max(available,values[len(values)-limit]+config['window_seconds'])
                    reasons.append(label)
            if same and now-same[-1]<config['group_gap_seconds']:
                available=max(available,same[-1]+config['group_gap_seconds']); reasons.append('GROUP_GAP')
            item.update(group_key=key,roles=sorted(item['roles']),attempts_in_window=len(same),
                        next_eligible_at=available,blocked_by=reasons)
        return {'observed_at':now,'policy':config,'attempts_in_window':len(attempts),
                'pending':len(rows),'groups':list(groups.values()),
                'source':str(path),'delivery_confirmed':False}


def pending(db, limit):
    roles = [r[0] for r in db.execute("SELECT DISTINCT role FROM queue WHERE state IN ('PENDING','QUIET_DEFERRED') ORDER BY role")]
    last = get(db, 'last_role', '')
    roles = [r for r in roles if r>last]+[r for r in roles if r<=last]
    # Round-robin by role first; severity/transition prioritizes within a role.
    pages = []
    for role in roles:
        pages.append(db.execute("SELECT name FROM queue WHERE role=? AND state IN ('PENDING','QUIET_DEFERRED') ORDER BY priority,name LIMIT ?", (role,limit)).fetchall())
    result=[]
    for index in range(limit):
        for page in pages:
            if index<len(page):
                result.append(Path(page[index]['name']))
                if len(result)>=limit:
                    return result
    return result


def archive_terminal(outbox, *, before, max_items=100, at=None):
    """Archive JSON terminal receipts only; UNKNOWN always remains protected."""
    from monitor_notifications import delivery_lock
    outbox = Path(outbox); archived = []
    now=time.time() if at is None else at
    config=policy(outbox)
    with delivery_lock(outbox,time.monotonic()+2), database(outbox) as db:
        if not inventory(outbox,db,time.monotonic()+1):
            return {'state':'PREPARATION_REQUIRED','items':[]}
        rows = db.execute("SELECT name,body FROM queue WHERE state IN ('SENT','SUPERSEDED') AND summary_key IS NULL AND generated<? ORDER BY name LIMIT ?", (before,max_items)).fetchall()
        for item in rows:
            record=json.loads(item['body'])
            attempted=_time(record['delivery_receipt'].get('attempted_at'))
            if attempted is not None and attempted > now-config['window_seconds']:
                continue
            source = outbox/item['name']; raw=source.read_bytes()
            sha=hashlib.sha256(raw).hexdigest()
            folder=outbox/'archive'/sha[:2]; folder.mkdir(parents=True,exist_ok=True)
            target=folder/(sha+'.json')
            if target.exists() and target.read_bytes()!=raw:
                raise ValueError('ARCHIVE_HASH_CONFLICT')
            if not target.exists():
                temporary=target.with_suffix('.tmp'); temporary.write_bytes(raw); temporary.replace(target)
            db.execute("UPDATE queue SET state='ARCHIVED' WHERE name=?",(item['name'],))
            db.commit()
            source.unlink()
            archived.append({'name':item['name'],'path':str(target),'sha256':sha})
        put(db,'directory_signature',signature(outbox))
    return {'state':'COMMITTED','items':archived}
