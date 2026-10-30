"""Business event outbox, consumed by the existing native scheduler hook.

The transport is the existing Hermes sender. Periodic model reports retain
their native cron outlet; no event is sent by both paths.
"""
import json
import contextvars
import fcntl
import math
import threading
import time
from pathlib import Path


EVENT_KINDS = frozenset({'WORK_CLAIMED', 'CANDIDATE_DELIVERED', 'CANDIDATE_DECIDED'})


def drain_business_notifications(pool_path, *, transport=None, max_messages=10, max_seconds=2, max_scan=256):
    if (type(max_messages) is not int or max_messages<1 or type(max_scan) is not int or max_scan<1
            or type(max_seconds) not in (int,float) or not math.isfinite(max_seconds) or max_seconds<=0):
        raise ValueError('INVALID_NOTIFICATION_BUDGET')
    deadline=time.monotonic()+max_seconds
    from workflow_runtime import active, open_ledger
    from workflow_reports import deliver_notification
    if not active(pool_path):
        return []
    pool = json.loads(Path(pool_path).read_text())
    if transport is None:
        from runtime_tools import send_claim_notice
        transport = send_claim_notice
    ledger = open_ledger(pool_path, readonly=False)
    results = []
    attempts=0
    from workflow_notification_registry import REGISTRY,suppress_stale
    kinds=frozenset(REGISTRY)
    for item in ledger.outbox_items(kinds=tuple(kinds),states=('PENDING',),limit=max_scan):
        if attempts>=max_messages or time.monotonic()>=deadline:
            break
        if item['kind'] not in kinds or item['state'] != 'PENDING':
            continue
        stale=suppress_stale(ledger,item)
        if stale:
            results.append(stale)
            continue
        role = item['role']
        chat = pool.get('roles', {}).get(role, {}).get('chat_id')
        if not isinstance(chat, str) or not chat or any(ord(c) < 32 for c in chat):
            results.append({'notification_id': item['notification_id'], 'state': 'DEFERRED',
                            'reason': 'ROLE_CHAT_UNVERIFIED'})
            continue

        def send(text, *, current=item, target=chat):
            receipt = transport({'chat_id': target, 'text': text, 'role': current['role'],
                                 'notification_id': current['notification_id']})
            if not isinstance(receipt, dict) or receipt.get('delivered') is not True or not receipt.get('message_id'):
                raise ValueError('NATIVE_DELIVERY_UNVERIFIED')
            return {**receipt, 'source': 'native_hermes_send', 'role': current['role']}

        lock=(ledger.path.parent/'business-notification.transport.lock').open('a+')
        try:
            fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            break
        done=threading.Event(); result={}
        def deliver(current=item,send=send,lock=lock,done=done,result=result):
            try:
                result.update(deliver_notification(ledger,current['notification_id'],send,
                    command_id='business-notice:'+current['notification_id']))
            except Exception as exc:
                result.update(state='UNVERIFIED',reason=type(exc).__name__)
            finally:
                lock.close(); done.set()
        context=contextvars.copy_context()
        worker=threading.Thread(target=lambda:context.run(deliver),name='role-business-notification',daemon=True)
        try:
            worker.start()
        except BaseException:
            lock.close()
            raise
        attempts+=1
        done.wait(max(0,deadline-time.monotonic()))
        results.append(dict(result) if done.is_set() else {
            'notification_id':item['notification_id'],'state':'IN_FLIGHT','reason':'BOUNDED_DELIVERY_PENDING'})
        if not done.is_set(): break
    return results
