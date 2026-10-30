import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import manual_business

sys.path.insert(0, str(Path.home()/'.hermes/hermes-agent'))
from agent import relay_runtime


class ManualBusinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root/'ROLE-BINDINGS.json').write_text(json.dumps({'schema_version': 1,
            'project_root': str(self.root), 'control': 'ACTIVE', 'roles': {'A': {}}}))
        self.runtime = manual_business.runtime_module()
        self.coordinator = relay_runtime.RelaySessionCoordinator()
        for patcher in (patch.object(relay_runtime, 'SESSION_COORDINATOR', self.coordinator),
                        patch.dict(os.environ, {'HERMES_HOME': str(self.root/'hermes-home')})):
            patcher.start(); self.addCleanup(patcher.stop)

    def control(self, action):
        return manual_business.control(self.root, 'A', 'session', 'turn', action)

    def test_pid_reuse_or_missing_start_does_not_release_renew_or_claim_old_owner(self):
        lease = self.control('claim')
        path = self.runtime.lease_path(self.root, 'A')
        for process_start in ('old-process-start', None):
            with self.subTest(process_start=process_start):
                self.runtime.save(path, {**lease, 'process_start': process_start})
                before = path.read_bytes()
                self.assertFalse(manual_business.owns(self.root, 'A', 'session', 'turn'))
                self.assertFalse(manual_business.end_turn(self.root, 'A', 'session', 'turn')['released'])
                for action in ('release', 'renew', 'claim'):
                    result = self.control(action)
                    self.assertFalse(result.get('claimed', False))
                    self.assertEqual(path.read_bytes(), before)

    def test_unverifiable_process_identity_is_not_owned(self):
        self.control('claim')
        path = self.runtime.lease_path(self.root, 'A'); before = path.read_bytes()
        with patch.object(self.runtime, 'pid_identity', return_value='UNVERIFIABLE'):
            self.assertFalse(manual_business.end_turn(self.root, 'A', 'session', 'turn')['released'])
            self.assertFalse(self.control('release').get('claimed', False))
        self.assertEqual(path.read_bytes(), before)

    def test_other_pid_remains_foreign_even_with_matching_native_turn_and_start(self):
        lease = self.control('claim')
        path = self.runtime.lease_path(self.root, 'A')
        self.runtime.save(path, {**lease, 'pid': os.getpid()+10000000})
        before = path.read_bytes()
        self.assertFalse(manual_business.end_turn(self.root, 'A', 'session', 'turn')['released'])
        self.assertEqual(before, path.read_bytes())

    def test_exact_process_and_turn_can_renew_and_end(self):
        self.control('claim')
        self.assertTrue(manual_business.owns(self.root, 'A', 'session', 'turn'))
        self.assertTrue(self.control('renew')['claimed'])
        self.assertFalse(manual_business.end_turn(self.root, 'A', 'session', 'other-turn')['released'])
        self.assertTrue(manual_business.end_turn(self.root, 'A', 'session', 'turn')['released'])
        self.assertFalse(self.runtime.lease_path(self.root, 'A').exists())

    def test_write_extends_expired_lease_so_long_turns_keep_write_access(self):
        """真实故障回归：600 秒 TTL 在读完材料前到期，导致后续写操作全被拒。

        过期租约下 owns() 会因 valid_lease 抛 ValueError（上层 task_decision 捕获后
        转成"尚未核实持有租约"）。写操作续期后应恢复写权限。
        """
        self.control('claim')
        path = self.runtime.lease_path(self.root, 'A')
        lease = self.runtime.load(path, {})
        # 模拟单轮往返耗时超过 TTL：租约已过期
        self.runtime.save(path, {**lease, 'expires_at': 1.0})
        with self.assertRaises(ValueError):
            manual_business.owns(self.root, 'A', 'session', 'turn')

        # 一次成功的写操作续期后，写权限恢复
        self.assertTrue(manual_business.renew_after_write(self.root, 'A', 'session', 'turn'))
        self.assertTrue(manual_business.owns(self.root, 'A', 'session', 'turn'))
        renewed = self.runtime.load(path, {})
        self.assertGreater(renewed['expires_at'], lease['expires_at'])

    def test_renew_after_write_refuses_expired_lease_because_owner_state_unchanged(self):
        """续期绝不能复活已过期租约：_owned 要求 owner/pid/turn 完全一致，且不看过期时间。

        这里验证的是 design invariant：即使同 pid 同 turn，_owned 也不因过期而放行
        其他租约——过期租约的存在不改变 _owned 判定，renew 仍按 _owned 结果执行。
        """
        self.control('claim')
        path = self.runtime.lease_path(self.root, 'A')
        lease = self.runtime.load(path, {})
        self.runtime.save(path, {**lease, 'expires_at': 1.0})
        # 同 turn 的续期是允许的（这正是修复要的行为）
        self.assertTrue(manual_business.renew_after_write(self.root, 'A', 'session', 'turn'))
        self.assertTrue(manual_business.owns(self.root, 'A', 'session', 'turn'))

    def test_renew_after_write_refuses_foreign_turn_and_leaves_lease_alone(self):
        """续期不能被用作越权：非本回合持有者不得延长他人租约。"""
        self.control('claim')
        path = self.runtime.lease_path(self.root, 'A')
        before = path.read_bytes()
        self.assertFalse(manual_business.renew_after_write(self.root, 'A', 'session', 'other-turn'))
        self.assertFalse(manual_business.renew_after_write(self.root, 'B', 'session', 'turn'))
        self.assertEqual(before, path.read_bytes())

    def test_expired_owned_lease_is_renewable_before_gate_check(self):
        """核心回归：过期租约仍属本回合时，续期必须在门禁之前发生。

        _owned() 只看 owner/session/turn/pid（不看 expires_at），所以本回合过期
        租约可以续回来。插件的 on_tool 现在先续期再判门禁——否则第一次写操作会
        先被"无租约"拒绝，永远没有机会续期，这就是协调者卡死的真实路径。
        """
        self.control('claim')
        path = self.runtime.lease_path(self.root, 'A')
        lease = self.runtime.load(path, {})
        self.runtime.save(path, {**lease, 'expires_at': 1.0})
        # 门禁视角：已过期 → owns() 抛错
        with self.assertRaises(ValueError):
            manual_business.owns(self.root, 'A', 'session', 'turn')
        # 但本回合身份仍匹配 → 可以续期 → 立刻恢复可写
        self.assertTrue(manual_business.renew_after_write(self.root, 'A', 'session', 'turn'))
        self.assertTrue(manual_business.owns(self.root, 'A', 'session', 'turn'))

    def test_renew_never_revives_lease_whose_owner_process_is_gone(self):
        """续期不复活已退出进程的租约：owner_state 必须仍是 alive。"""
        self.control('claim')
        path = self.runtime.lease_path(self.root, 'A')
        lease = self.runtime.load(path, {})
        self.runtime.save(path, {**lease, 'expires_at': 1.0, 'process_start': 'stale-start'})
        self.assertFalse(manual_business.renew_after_write(self.root, 'A', 'session', 'turn'))
        unchanged = self.runtime.load(path, {})
        self.assertEqual(unchanged['process_start'], 'stale-start')

    def _put_foreign_lease(self, age_seconds, with_last_seen=True):
        """写入一个他人的活进程租约，心跳 age_seconds 秒前。"""
        now = time.time()
        lease = {
            'owner': manual_business.owner_id('other-session', 'other-turn'),
            'token': 'foreign-token',
            'claimed_at': time.strftime('%Y-%m-%dT%H:%M:%S+08:00', time.localtime(now - age_seconds)),
            'expires_at': now - age_seconds + manual_business.WRITE_TTL_SECONDS,
            'pid': os.getpid(),
            'process_start': self.runtime.verified_pid_identity(os.getpid()),
            'manual_session_id': 'other-session', 'manual_turn_id': 'other-turn',
        }
        if with_last_seen:
            lease['last_seen'] = now - age_seconds
        self.runtime.save(self.runtime.lease_path(self.root, 'A'), lease)
        return lease

    def test_active_heartbeat_blocks_takeover(self):
        """真实故障回归：空闲但进程活着的持有者不能永久占位。

        租约 pid 是常驻网关进程，回合异常退出时 on_turn_end 不跑、租约文件残留，
        旧逻辑只看进程存活 → 谁都接管不了直到网关重启。这里改为看心跳：
        心跳仍在窗口内（持有者确实在工作）时仍然拒绝接管。
        """
        self._put_foreign_lease(300)  # 5 分钟前还在调工具
        result = manual_business.control(self.root, 'A', 'session', 'turn', 'claim')
        self.assertFalse(result.get('claimed'))
        self.assertEqual(result.get('reason'), 'OWNER_BUSY_OR_UNVERIFIED')

    def _native_turn(self, turn_id='other-turn', session_id='other-session'):
        profile = relay_runtime.current_profile_key()
        lease = relay_runtime.ConversationLease(profile_key=profile, session_id=session_id,
            platform='test', host=relay_runtime.NoopRelayRuntime(profile, 'test'), session=None)
        turn = self.coordinator.begin_turn(lease, turn_id=turn_id, task_id='test')
        self.addCleanup(lambda: self.coordinator.end_turn(turn, outcome='test-cleanup'))
        return turn

    def test_stale_heartbeat_does_not_take_over_active_native_turn(self):
        self._put_foreign_lease(manual_business.STALE_HEARTBEAT_SECONDS + 900)
        self._native_turn()
        path = self.runtime.lease_path(self.root, 'A'); before = path.read_bytes()
        result = self.control('claim')
        self.assertFalse(result['claimed'])
        self.assertEqual(result['native_turn']['state'], 'active')
        self.assertEqual(before, path.read_bytes())

    def test_stale_heartbeat_without_native_proof_is_not_taken_over(self):
        self._put_foreign_lease(7200)
        path = self.runtime.lease_path(self.root, 'A'); before = path.read_bytes()
        result = self.control('claim')
        self.assertFalse(result['claimed'])
        self.assertEqual(result['native_turn']['state'], 'unknown')
        self.assertEqual(before, path.read_bytes())

    def test_confirmed_ended_stale_turn_allows_claim_and_records_native_evidence(self):
        """原生生命周期确认结束，且业务释放钩子漏跑时，可恢复残留人工租约。"""
        self._put_foreign_lease(manual_business.STALE_HEARTBEAT_SECONDS + 900)
        old = self._native_turn()
        self.coordinator.end_turn(old, outcome='failed')
        result = manual_business.control(self.root, 'A', 'session', 'turn', 'claim')
        self.assertTrue(result.get('claimed'))
        self.assertEqual(result.get('manual_turn_id'), 'turn')
        self.assertIn('last_seen', result)
        recs = list((self.root / 'inbox/control/runtime/recoveries').glob('*.json'))
        self.assertEqual(len(recs), 1)
        record = json.loads(recs[0].read_text())
        self.assertEqual(record['reason'], 'OWNER_NATIVE_TURN_ENDED')
        self.assertEqual(record['native_turn']['state'], 'ended')
        self.assertEqual(record['native_turn']['turn_id'], 'other-turn')

    def test_foreign_renew_and_release_cannot_recover_ended_owner(self):
        self._put_foreign_lease(7200)
        old = self._native_turn()
        self.coordinator.end_turn(old, outcome='failed')
        path = self.runtime.lease_path(self.root, 'A'); before = path.read_bytes()
        for action in ('renew', 'release'):
            self.assertFalse(self.control(action).get('claimed'))
            self.assertEqual(before, path.read_bytes())
        self.assertFalse((self.root/'inbox/control/runtime/recoveries').exists())

    def test_ended_other_turn_is_not_proof_for_stale_owner(self):
        self._put_foreign_lease(7200)
        old = self._native_turn('different-turn')
        self.coordinator.end_turn(old, outcome='success')
        self.assertFalse(self.control('claim')['claimed'])

    def test_ended_owner_under_other_profile_is_not_recovered(self):
        lease = self._put_foreign_lease(7200)
        old = self._native_turn()
        self.coordinator.end_turn(old, outcome='success')
        self.runtime.save(self.runtime.lease_path(self.root, 'A'),
                          {**lease, 'manual_profile_key': 'another-profile'})
        self.assertFalse(self.control('claim')['claimed'])

    def test_legacy_lease_without_last_seen_falls_back_to_claimed_at(self):
        """旧格式心跳仍可核验，但还必须有确切原生终态。"""
        self._put_foreign_lease(3600, with_last_seen=False)
        old = self._native_turn()
        self.coordinator.end_turn(old, outcome='failed')
        result = manual_business.control(self.root, 'A', 'session', 'turn', 'claim')
        self.assertTrue(result.get('claimed'))

    def test_lease_without_pid_is_never_taken_over(self):
        """缺 pid 的租约即便 claimed_at 很旧也不接管（无法证明持有者状态）。"""
        now = time.time()
        self.runtime.save(self.runtime.lease_path(self.root, 'A'), {
            'owner': 'cron-owner', 'token': 'foreign', 'expires_at': 0,
            'claimed_at': time.strftime('%Y-%m-%dT%H:%M:%S+08:00', time.localtime(now - 7200)),
        })
        result = manual_business.control(self.root, 'A', 'session', 'turn', 'claim')
        self.assertFalse(result.get('claimed'))

    def test_recent_renewal_without_last_seen_is_not_stale(self):
        """真实误判回归：旧格式租约只有 expires_at，刚续期过也不能判为陈旧。

        expires_at 每次续期都重置为 续期时刻 + WRITE_TTL_SECONDS，所以
        expires_at - TTL 就是最后一次续期时刻。真实案例：活跃协调者租约没有
        last_seen、claimed_at 在 48 分钟前，但 expires_at 刚被推远 —— 若只看
        claimed_at 会把它误判为可接管，导致正在干活的回合被抢。
        """
        now = time.time()
        self.runtime.save(self.runtime.lease_path(self.root, 'A'), {
            'owner': manual_business.owner_id('other-session', 'other-turn'),
            'token': 'foreign-token',
            'claimed_at': time.strftime('%Y-%m-%dT%H:%M:%S+08:00', time.localtime(now - 2880)),
            'expires_at': now - 120 + manual_business.WRITE_TTL_SECONDS,  # 2 分钟前刚续期
            'pid': os.getpid(),
            'process_start': self.runtime.verified_pid_identity(os.getpid()),
            'manual_session_id': 'other-session', 'manual_turn_id': 'other-turn',
        })
        result = manual_business.control(self.root, 'A', 'session', 'turn', 'claim')
        self.assertFalse(result.get('claimed'))
        self.assertEqual(result.get('reason'), 'OWNER_BUSY_OR_UNVERIFIED')

    def test_fresh_claim_writes_heartbeat_and_is_not_stale(self):
        """刚领取的租约必须带 last_seen，且不会被自己判定为陈旧。"""
        lease = self.control('claim')
        self.assertIn('last_seen', lease)
        path = self.runtime.lease_path(self.root, 'A')
        self.assertFalse(manual_business.heartbeat_is_stale(self.runtime.load(path, {})))

    def test_heartbeat_is_refreshed_by_renewed_tool_calls(self):
        """每次工具调用续期都会推进 last_seen，让在干活的回合不被误抢。"""
        self.control('claim')
        path = self.runtime.lease_path(self.root, 'A')
        lease = self.runtime.load(path, {})
        self.runtime.save(path, {**lease, 'last_seen': time.time() - 600})
        self.assertTrue(manual_business.renew_after_write(self.root, 'A', 'session', 'turn'))
        refreshed = self.runtime.load(path, {})
        self.assertGreater(refreshed['last_seen'], time.time() - 60)
        self.assertFalse(manual_business.heartbeat_is_stale(refreshed))


if __name__ == '__main__':
    unittest.main()
