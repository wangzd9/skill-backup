"""Controlled lease for one explicitly authorized native manual turn."""
import hashlib
import os
import time

from role_status import runtime_module


def owner_id(session_id, turn_id):
    if not session_id or not turn_id:
        raise ValueError('native session and turn identity required')
    return 'manual:' + hashlib.sha256((session_id+'\0'+turn_id).encode()).hexdigest()


def _owned(lease, session_id, turn_id):
    return (lease.get('owner') == owner_id(session_id, turn_id)
            and lease.get('manual_session_id') == session_id
            and lease.get('manual_turn_id') == turn_id
            and lease.get('pid') == os.getpid()
            and runtime_module().owner_state(lease) == 'alive')


# 单次人工回合内的工具调用会连续发生数十次（读协议、核基线、派单、推送），
# 而一次模型往返实测中位数 56 秒、最长 17 分钟；固定 600 秒 TTL 会在读完材料
# 之前就到期，使真正的写操作全部被判为"无租约"。因此每次工具调用都顺带续期，
# 把 TTL 变成"空闲超时"而非"整轮预算"。
WRITE_TTL_SECONDS = 600

# 心跳陈旧只触发核验，不能证明持有者已退出。长时间模型请求、重试和工具操作
# 仍可能真实在途；只有 core 确认同一原生回合已结束，才能恢复残留人工租约。
STALE_HEARTBEAT_SECONDS = 1800


def heartbeat(lease):
    """最近一次可证明持有者仍在工作的时刻。

    优先用显式 last_seen；没有时从 expires_at 反推续期时刻
    （expires_at 每次续期都会重置为 续期时刻 + WRITE_TTL_SECONDS，所以
    expires_at - WRITE_TTL_SECONDS 就是最后一次续期时刻）。最后才回退到领取时刻。
    这样旧代码写入的租约（只有 expires_at、没有 last_seen）也不会被误判为陈旧。
    """
    if lease.get('last_seen'):
        return float(lease['last_seen'])
    expires = float(lease.get('expires_at') or 0)
    if expires:
        return expires - WRITE_TTL_SECONDS
    return _claimed_ts(lease) or 0.0


def _claimed_ts(lease):
    """把 claimed_at（ISO 字符串）转成时间戳；解析不了返回 0。"""
    import datetime as dt
    raw = lease.get('claimed_at')
    if not raw:
        return 0.0
    try:
        return dt.datetime.fromisoformat(str(raw)).timestamp()
    except (TypeError, ValueError):
        return 0.0


def heartbeat_is_stale(lease, window=STALE_HEARTBEAT_SECONDS):
    """心跳是否陈旧；此结果不表示持有者已经停止工作。"""
    last = heartbeat(lease)
    if not last:
        # 无时间证据也需要核验；接管另要求可解析时间和原生终态。
        return True
    return (time.time() - last) > window


def native_turn_evidence(lease):
    """只读查询同进程的确切原生回合；不从租约到期或工具心跳推断退出。"""
    evidence = {'source': 'core.native_turn_liveness', 'state': 'unknown'}
    if (lease.get('pid') != os.getpid() or not lease.get('process_start')
            or runtime_module().owner_state(lease) != 'alive'):
        return evidence
    session_id, turn_id = lease.get('manual_session_id'), lease.get('manual_turn_id')
    if (not session_id or not turn_id
            or lease.get('owner') != owner_id(session_id, turn_id)):
        return evidence
    try:
        from agent import relay_runtime
        profile_key = lease.get('manual_profile_key') or relay_runtime.current_profile_key()
        evidence.update(profile_key=profile_key, session_id=session_id, turn_id=turn_id)
        evidence['state'] = relay_runtime.SESSION_COORDINATOR.turn_state(
            profile_key=profile_key, session_id=session_id, turn_id=turn_id)
    except Exception:
        # Missing/deploying core support or unavailable scope is not completion.
        pass
    return evidence


def takeover_allowed_on_stale_heartbeat(lease):
    """仅允许恢复心跳陈旧、且 core 已确认原生回合结束的残留人工租约。"""
    return bool(heartbeat(lease) and heartbeat_is_stale(lease)
                and native_turn_evidence(lease)['state'] == 'ended')


def renew_after_write(project, role, session_id, turn_id):
    """延长本回合持有的租约；仅在确属本回合持有租约时执行，否则静默返回 False。

    必须先确认租约文件已存在再取锁：runtime.locked() 会 mkdir 并创建锁文件，
    在没有租约的只读回合里调用它会在项目下凭空产生 inbox/ 目录，破坏
    "只读回合不产生任何业务写入"的安全契约。
    """
    runtime = runtime_module()
    try:
        root, binding = runtime.binding(project)
        if role not in binding['roles']:
            return False
        path = runtime.lease_path(root, role)
        if not path.exists():
            return False
        if not _owned(runtime.load(path, {}), session_id, turn_id):
            return False
        with runtime.legacy_admission(project), runtime.locked(runtime.lease_lock(root, role)):
            _, current = runtime.binding(project)
            runtime.guard(current, role)
            lease = runtime.load(path, {})
            if not _owned(lease, session_id, turn_id):
                return False
            import time
            lease['expires_at'] = time.time() + WRITE_TTL_SECONDS
            lease['last_seen'] = time.time()
            runtime.save(path, lease)
        return True
    except Exception:
        return False


def owns(project, role, session_id, turn_id):
    runtime = runtime_module(); root, binding = runtime.binding(project)
    runtime.guard(binding, role)
    lease = runtime.load(runtime.lease_path(root, role), {})
    if not _owned(lease, session_id, turn_id):
        return False
    runtime.valid_lease(root, role, lease.get('token'))
    return True


def control(project, role, session_id, turn_id, action):
    runtime = runtime_module()
    with runtime.legacy_admission(project):
        return _control_unlocked(project, role, session_id, turn_id, action)


def _control_unlocked(project, role, session_id, turn_id, action):
    """Only claim may recover a confirmed ended native owner under the role lock."""
    if action not in {'claim', 'renew', 'release'}:
        raise ValueError('unsupported manual lease action')
    runtime = runtime_module(); root, binding = runtime.binding(project)
    if role not in binding['roles']:
        raise ValueError('role not bound')
    owner = owner_id(session_id, turn_id)
    with runtime.locked(runtime.lease_lock(root, role)):
        lease = runtime.load(runtime.lease_path(root, role), {})
        if action != 'release':
            runtime.guard(binding, role)
        if lease and not _owned(lease, session_id, turn_id):
            evidence = native_turn_evidence(lease)
            if (action != 'claim' or not heartbeat(lease) or not heartbeat_is_stale(lease)
                    or evidence['state'] != 'ended'):
                return {'claimed': False, 'reason': 'OWNER_BUSY_OR_UNVERIFIED',
                        'message': '业务owner仍占用或未核实；本轮不接管、不重复执行、不代写队列。',
                        'last_seen': heartbeat(lease), 'native_turn': evidence}
            stale_for = int(time.time() - heartbeat(lease))
            runtime.record_recovery(root, role, lease, reason='OWNER_NATIVE_TURN_ENDED',
                                    heartbeat_age_seconds=stale_for, native_turn=evidence)
            lease = {}
        if action == 'release':
            if lease:
                runtime.lease_path(root, role).unlink()
            return {'released': bool(lease)}
        if lease:
            runtime.valid_lease(root, role, lease.get('token'), allow_expired=True)
        elif action == 'renew':
            raise ValueError('no owned lease to renew')
        else:
            import uuid
            lease = {'owner': owner, 'token': uuid.uuid4().hex, 'claimed_at': runtime.now(),
                     'pid': os.getpid(), 'process_start': runtime.verified_pid_identity(os.getpid()),
                     'manual_session_id': session_id, 'manual_turn_id': turn_id}
            try:
                from agent.relay_runtime import current_profile_key
                lease['manual_profile_key'] = current_profile_key()
            except ImportError:
                pass
        lease['expires_at'] = time.time() + WRITE_TTL_SECONDS
        lease['last_seen'] = time.time()
        runtime.save(runtime.lease_path(root, role), lease)
        return {'claimed': True, 'role': role, **lease}


def end_turn(project, role, session_id, turn_id):
    # Only a matching controlled owner can be removed. No recovery of other work.
    runtime = runtime_module(); root, _ = runtime.binding(project)
    lease = runtime.load(runtime.lease_path(root, role), {})
    if not _owned(lease, session_id, turn_id):
        return {'released': False}
    return control(project, role, session_id, turn_id, 'release')
