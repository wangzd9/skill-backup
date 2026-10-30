"""Bounded mechanical notification delivery; UNKNOWN is never retried.

The transport lock is retained by an outstanding send, including after the
caller's deadline. A hung transport cannot create more sender threads.
"""
from datetime import datetime
from contextlib import contextmanager
import logging
import contextvars
import json
import fcntl
import math
from pathlib import Path
import threading
import time
from zoneinfo import ZoneInfo

import role_runtime as runtime
import activity_report
import monitor_queue as queue


@contextmanager
def delivery_lock(outbox, deadline):
    with (outbox/'delivery.lock').open('a+') as stream:
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('DELIVERY_LOCK_BUSY')
                time.sleep(min(.01,max(0,deadline-time.monotonic())))
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def drain(outbox, send, **kwargs):
    try:
        return _drain(outbox,send,**kwargs)
    except TimeoutError:
        return [{'state':'BACKPRESSURE','reason':'DELIVERY_LOCK_BUSY'}]


def prepare(outbox, *, at=None, max_seconds=30, max_scan=100000, delivery_policy=None):
    """Offline/adoption handoff; counts share one locked index transaction.

    summary_groups is the legacy count of groups touched by this compaction,
    not the number of pending summaries. See pending_summary/pending_ordinary.
    """
    if (type(max_seconds) not in (int,float) or not math.isfinite(max_seconds) or max_seconds<=0
            or type(max_scan) is not int or max_scan<1):
        raise ValueError('INVALID_PREPARATION_BUDGET')
    outbox = Path(outbox)
    moment = at if at is not None else time.time()
    now = moment.timestamp() if isinstance(moment, datetime) else float(moment)
    deadline = time.monotonic()+max_seconds
    outbox.mkdir(parents=True,exist_ok=True)
    config = queue.policy(outbox,delivery_policy)
    with delivery_lock(outbox,deadline), queue.database(outbox) as db:
        if not queue.inventory(outbox,db,deadline,max_items=max_scan):
            return {'state':'PREPARATION_REQUIRED','reason':'LEGACY_INVENTORY_BUDGET'}
        summaries = queue.compact(outbox,db,now,config,limit=max_scan,deadline=deadline)
        queue.put(db,'directory_signature',queue.signature(outbox))
        by_role={}
        for item in db.execute("""SELECT role,summary_key IS NOT NULL AS is_summary,COUNT(*) AS n
                FROM queue WHERE state IN ('PENDING','QUIET_DEFERRED') GROUP BY role,is_summary"""):
            counts=by_role.setdefault(item['role'],{'summary':0,'ordinary':0})
            counts['summary' if item['is_summary'] else 'ordinary']=item['n']
        summary_count=sum(c['summary'] for c in by_role.values())
        ordinary_count=sum(c['ordinary'] for c in by_role.values())
        return {'state':'PREPARATION_REQUIRED' if queue.get(db,'compaction_incomplete',False) else 'PREPARED',
                'prepared_at':now,'summary_groups':summaries,'compacted_summary_groups':summaries,'policy':config,
                'pending':summary_count+ordinary_count,'pending_summary':summary_count,
                'pending_ordinary':ordinary_count,'pending_by_role':by_role,
                'outbox_states':{r[0]:r[1] for r in db.execute('SELECT state,COUNT(*) FROM queue GROUP BY state')}}


def _drain(outbox, send, *, at=None, max_messages=10, max_seconds=2, max_scan=256, delivery_policy=None):
    if (type(max_messages) is not int or max_messages < 1 or type(max_scan) is not int or max_scan < 1
            or type(max_seconds) not in (int, float) or not math.isfinite(max_seconds) or max_seconds <= 0):
        raise ValueError('INVALID_NOTIFICATION_BUDGET')
    moment = datetime.now(ZoneInfo('Asia/Shanghai')) if at is None else at
    if type(moment) in (int, float):
        moment = datetime.fromtimestamp(moment, ZoneInfo('Asia/Shanghai'))
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        raise ValueError('AWARE_NOTIFICATION_TIME_REQUIRED')
    outbox = Path(outbox)
    if not outbox.is_dir():
        return []
    config = queue.policy(outbox,delivery_policy)
    deadline = time.monotonic()+max_seconds
    results = []
    with delivery_lock(outbox, deadline), queue.database(outbox) as db:
        if not queue.inventory(outbox,db,deadline):
            return [{'state':'BACKPRESSURE','reason':'INDEX_PREPARATION_REQUIRED'}]
        if activity_report.quiet(moment):
            return [{'state':'QUIET_DEFERRED','reason':'night_quiet_hours'}]
        queue.compact(outbox,db,moment.timestamp(),config,limit=max_scan,deadline=deadline)
        paths = queue.pending(db,max_scan)
        pending, groups = [], {}
        for filename in paths[:max_scan]:
            if time.monotonic() >= deadline:
                break
            path = outbox/filename
            row = runtime.load(path)
            receipt, event = row['delivery_receipt'], row['event']
            if receipt.get('state') not in queue.PENDING:
                continue
            key = queue.coalescing_key(event)
            if key and key in groups:
                receipt.update(state='SUPERSEDED',reason='same_episode_transition',superseded_by=groups[key])
                queue.write(db,path,row)
                continue
            if key:
                groups[key] = event['event_id']
            pending.append(path)
        queue.put(db,'directory_signature',queue.signature(outbox))
    for path in pending:
        if len([r for r in results if r.get('attempted_at')]) >= max_messages or time.monotonic() >= deadline:
            break
        transport = (outbox/'.transport.lock').open('a+')
        try:
            fcntl.flock(transport, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            transport.close()
            break
        done = threading.Event()
        try:
            with delivery_lock(outbox, deadline), queue.database(outbox) as db:
                row = runtime.load(path)
                receipt = row['delivery_receipt']
                if receipt.get('state') not in ('PENDING', 'QUIET_DEFERRED'):
                    transport.close()
                    continue
                reason=queue.summary_delivery_reason(row['event'],config)
                if reason:
                    receipt.update(reason=reason,deferred_at=moment.isoformat())
                    queue.write(db,path,row)
                    results.append({'event_id':row['event']['event_id'],'state':'BACKPRESSURE','reason':reason})
                    transport.close()
                    continue
                if queue.needs_handoff(outbox,row['event'],moment.timestamp(),config):
                    results.append({'event_id':row['event']['event_id'],'state':'BACKPRESSURE','reason':'DISPLAY_REVALIDATION_REQUIRED'})
                    transport.close()
                    continue
                queue.refresh_summary(outbox,row,moment.timestamp())
                attempt = time.time_ns()
                reason = queue.reserve(db,row['event'],moment.timestamp(),attempt,config)
                if reason:
                    receipt.update(reason=reason, deferred_at=moment.isoformat())
                    queue.write(db,path,row)
                    queue.put(db,'directory_signature',queue.signature(outbox))
                    results.append({'event_id':row['event']['event_id'],'state':'BACKPRESSURE','reason':reason})
                    transport.close()
                    continue
                if row['event'].get('summary_key'):
                    row['delivery_snapshot']={'event':json.loads(json.dumps(row['event'])),
                        'member_count':row['event']['member_count'],'at':moment.isoformat()}
                receipt.update(state='UNKNOWN', attempted_at=moment.isoformat(), attempt=attempt,
                               budget_group_key=queue.group_key(row['event']),
                               reason='transport_in_flight_or_uncertain')
                queue.write(db,path,row)
                queue.put(db,'directory_signature',queue.signature(outbox))
        except BaseException:
            transport.close()
            raise

        def deliver(path=path, event={**row['event'],'delivery_started_at':moment.isoformat(),
                    'delivery_group_key':receipt.get('budget_group_key')}, transport=transport, attempt=attempt, done=done):
            try:
                try:
                    value = send(event)
                    final = {'state': 'SENT' if isinstance(value, dict) and value.get('confirmed') is True else 'UNKNOWN',
                             'result': value, 'finished_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()}
                except Exception as exc:
                    final = {'state': 'UNKNOWN', 'error': type(exc).__name__}
                with delivery_lock(outbox, time.monotonic()+1), queue.database(outbox) as db:
                    current = runtime.load(path)
                    if current['delivery_receipt'].get('attempt') == attempt:
                        current['delivery_receipt'].update(final)
                        queue.write(db,path,current)
                        queue.put(db,'directory_signature',queue.signature(outbox))
            except (OSError,ValueError,TimeoutError,KeyError):
                # The pre-send UNKNOWN receipt stays authoritative. A failed
                # confirmation write is never grounds to repeat the send.
                logging.getLogger(__name__).warning('Notification confirmation unavailable; retain UNKNOWN')
            finally:
                transport.close()
                done.set()
        context = contextvars.copy_context()
        thread = threading.Thread(target=lambda: context.run(deliver), name='role-monitor-notification', daemon=True)
        try:
            thread.start()
        except BaseException:
            transport.close()
            # No transport was invoked; restoring pending is safe.
            with delivery_lock(outbox, time.monotonic()+1), queue.database(outbox) as db:
                current = runtime.load(path)
                current['delivery_receipt'].update(state='PENDING', reason='sender_not_started')
                queue.write(db,path,current)
                queue.put(db,'directory_signature',queue.signature(outbox))
            raise
        done.wait(max(0, deadline-time.monotonic()))
        current = runtime.load(path)
        results.append({'event_id': current['event']['event_id'], **current['delivery_receipt']})
        if not done.is_set():
            break
    return results


if __name__=='__main__':
    import argparse
    import json
    parser=argparse.ArgumentParser(description='Prepare mechanical notification history without sending messages.')
    parser.add_argument('action',choices=['prepare','status'])
    parser.add_argument('--outbox',required=True,type=Path)
    parser.add_argument('--max-seconds',type=float,default=30)
    parser.add_argument('--max-scan',type=int,default=100000)
    args=parser.parse_args()
    result=(queue.delivery_status(args.outbox) if args.action=='status' else
            prepare(args.outbox,max_seconds=args.max_seconds,max_scan=args.max_scan))
    print(json.dumps(result,ensure_ascii=False,indent=2))
    raise SystemExit(0 if args.action=='status' or result['state']=='PREPARED' else 2)
