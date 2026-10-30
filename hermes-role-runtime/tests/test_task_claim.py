import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import role_runtime
from runtime_common import save_json
from role_records import record_role
from task_claim import claim_task, release_slot, pending_claim_notifications, drain_claim_notifications

DAY = datetime.fromisoformat('2026-10-16T10:00:00+08:00')
NIGHT = datetime.fromisoformat('2026-10-16T22:00:00+08:00')


class TaskClaimTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        save_json(self.root/'ROLE-BINDINGS.json', {'schema_version': 1, 'project_root': str(self.root),
            'control': 'ACTIVE', 'roles': {'A': {'session_file': 'sessions/A.md', 'chat_id': 'original-group'},
                                         'COORDINATOR': {'session_file': 'sessions/COORDINATOR.md'}}})
        self.token = self.make_lease('A'); self.coordinator_token = self.make_lease('COORDINATOR')
        self.wt = self.root/'worktree'; self.wt.mkdir()
        self.git('init', '-b', 'task-01')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '--allow-empty', '-m', 'base')
        self.base = self.git('rev-parse', 'HEAD')
        self.dispatch = self.root/'inbox/1号/TASK-01/attempt-1-dispatch.md'
        self.dispatch.parent.mkdir(parents=True)
        self.dispatch.write_text(f'# 派单 TASK-01\n- status: DISPATCHED\n- task: TASK-01\n'
                                 f'- attempt: attempt-1\n- role: A\n- base: {self.base}\n')
        self.spec = {'dispatch': str(self.dispatch.relative_to(self.root)), 'task': 'TASK-01',
                     'attempt': 'attempt-1', 'base': self.base, 'branch': 'task-01', 'worktree': str(self.wt)}

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.wt), *args], text=True, stderr=subprocess.DEVNULL).strip()

    def make_lease(self, role):
        lease = role_runtime.execute(argparse.Namespace(action='claim', project=str(self.root), role=role,
                                                         owner='isolated-'+role, pid=None, ttl=300))
        path = self.root/(role+'-token.json'); save_json(path, {'token': lease['token']}); return path

    def claim(self, **kwargs):
        return claim_task(self.root, 'A', self.spec, self.token, at=kwargs.pop('at', DAY), **kwargs)

    def test_claim_writes_ack_event_and_one_durable_notification(self):
        result = self.claim()
        self.assertTrue(result['claimed'])
        ack = Path(result['ack']); self.assertTrue(ack.exists())
        self.assertIn(self.base, ack.read_text()); self.assertIn('task-01', ack.read_text())
        again = self.claim(); self.assertFalse(again['claimed'])
        self.assertEqual(len(list((self.root/'events/A').glob('*.json'))), 1)
        rows = pending_claim_notifications(self.root, 'A', at=DAY)
        self.assertEqual(len(rows), 1); self.assertEqual(rows[0]['chat_id'], 'original-group')
        sent = []
        drain_claim_notifications(self.root, 'A', lambda row: sent.append(row['text']) or {'delivered': True, 'message_id': 'native-id'}, at=DAY)
        drain_claim_notifications(self.root, 'A', lambda row: self.fail('duplicate send'), at=DAY)
        self.assertEqual(len(sent), 1); self.assertIn('TASK-01', sent[0])

    def test_night_claim_is_suppressed_and_available_to_morning_summary(self):
        self.claim(at=NIGHT)
        self.assertEqual(pending_claim_notifications(self.root, 'A', at=DAY), [])
        rows = pending_claim_notifications(self.root, 'A', at=DAY, include_suppressed=True)
        self.assertEqual(len(rows), 1); self.assertEqual(rows[0]['status'], 'SUPPRESSED')

    def test_delivery_queue_rechecks_wall_clock_before_each_notice(self):
        result = self.claim()
        row = json.loads(Path(result['notification']).read_text())
        save_json(Path(result['notification']).with_name('second-notice.json'),
                  {**row, 'notification_id': 'second-notice', 'task': 'TASK-02', 'text': '领取 TASK-02'})
        sent = []
        with patch('task_claim._instant', side_effect=[DAY, NIGHT]):
            outcomes = drain_claim_notifications(self.root, 'A', lambda item: sent.append(item) or True)
        self.assertEqual(len(sent), 1)
        self.assertEqual([item['status'] for item in outcomes], ['SENT', 'SUPPRESSED'])
        morning = pending_claim_notifications(self.root, 'A', include_suppressed=True)
        self.assertEqual(len(morning), 1)
        self.assertEqual(morning[0]['status'], 'SUPPRESSED')

    def test_repeat_claim_after_development_does_not_require_old_head_or_reset_checkpoint(self):
        self.claim()
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '--allow-empty', '-m', 'development')
        path = self.root/'inbox/control/runtime/checkpoints/A.json'
        row = json.loads(path.read_text()); row.update(status='REVIEW_PENDING', event_id='A:later-delivery')
        save_json(path, row); before = path.read_bytes()
        self.assertFalse(self.claim()['claimed'])
        self.assertEqual(path.read_bytes(), before)

    def test_same_task_attempt_cannot_be_claimed_twice_by_renaming_dispatch_after_release(self):
        first = self.claim()
        receipt = self.root/'receipts/attempt-1.md'; receipt.parent.mkdir()
        receipt.write_text('TASK-01 attempt-1 已冻结交付')
        record_role(self.root, 'A', {'event_id': 'A:mirror-test-delivery', 'kind': 'business',
            'task': 'TASK-01', 'attempt': 'attempt-1', 'status': 'REVIEW_PENDING',
            'summary': '已交付', 'evidence': str(receipt.relative_to(self.root)),
            'next': '待审查', 'occurrence': {'kind': 'now'}}, self.token)
        release_slot(self.root, 'A', {'task': 'TASK-01', 'attempt': 'attempt-1',
            'evidence': str(receipt.relative_to(self.root))}, self.coordinator_token, at=DAY)
        mirror = self.dispatch.with_name('attempt-1-mirror-dispatch.md')
        mirror.write_bytes(self.dispatch.read_bytes())
        with self.assertRaisesRegex(ValueError, '领取身份'):
            claim_task(self.root, 'A', {**self.spec, 'dispatch': str(mirror.relative_to(self.root))}, self.token, at=DAY)
        self.assertEqual(len(pending_claim_notifications(self.root, 'A', at=DAY)), 1)
        self.assertFalse(mirror.with_name('attempt-1-mirror-ack.md').exists())
        self.assertFalse(self.claim()['claimed'])

    def test_existing_ack_freezes_same_task_attempt_against_a_new_dispatch_path(self):
        ack = self.dispatch.parent/'attempt-1-ack.md'
        ack.write_text('- status: ACK\n- task: TASK-01\n- attempt: attempt-1\n- dispatch: '+self.spec['dispatch'])
        self.assertTrue(self.claim()['already_acknowledged'])
        mirror = self.dispatch.with_name('attempt-1-mirror-dispatch.md')
        mirror.write_bytes(self.dispatch.read_bytes())
        with self.assertRaisesRegex(ValueError, '领取身份'):
            claim_task(self.root, 'A', {**self.spec, 'dispatch': str(mirror.relative_to(self.root))}, self.token, at=DAY)
        self.assertEqual(pending_claim_notifications(self.root, 'A', at=DAY), [])

    def test_legacy_dispatch_keyed_journal_keeps_its_outbox_identity_without_reclaim(self):
        first = self.claim()
        old_id = hashlib.sha256(json.dumps({'role': 'A', 'dispatch': self.spec['dispatch'],
            'task': 'TASK-01', 'attempt': 'attempt-1'}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        folder = self.root/'inbox/control/runtime/task-claims/A'
        journal = folder/(first['claim_id']+'.json')
        row = json.loads(journal.read_text()); row['claim_id'] = old_id
        row['record']['event_id'] = 'A:claim:'+old_id
        save_json(folder/(old_id+'.json'), row); journal.unlink()
        outbox = Path(first['notification'])
        notice = json.loads(outbox.read_text()); notice['notification_id'] = old_id
        notice['event_id'] = row['record']['event_id']; notice['status'] = 'SENT'
        save_json(outbox.with_name(old_id+'.json'), notice); outbox.unlink()
        checkpoint = self.root/'inbox/control/runtime/checkpoints/A.json'
        before = checkpoint.read_bytes()
        restored = self.claim()
        self.assertFalse(restored['claimed'])
        self.assertEqual(restored['claim_id'], old_id)
        self.assertEqual(checkpoint.read_bytes(), before)
        self.assertEqual(pending_claim_notifications(self.root, 'A', at=DAY), [])
        self.assertEqual(len(list(folder.glob('*.json'))), 1)
        mirror = self.dispatch.with_name('attempt-1-mirror-dispatch.md'); mirror.write_bytes(self.dispatch.read_bytes())
        with self.assertRaisesRegex(ValueError, '领取身份'):
            claim_task(self.root, 'A', {**self.spec, 'dispatch': str(mirror.relative_to(self.root))}, self.token, at=DAY)

    def test_unreleased_role_cannot_claim_a_second_dispatch(self):
        self.claim()
        other = self.dispatch.with_name('attempt-2-dispatch.md')
        other.write_text(self.dispatch.read_text().replace('attempt-1', 'attempt-2'))
        with self.assertRaisesRegex(ValueError, '未释放'):
            claim_task(self.root, 'A', {**self.spec, 'dispatch': str(other.relative_to(self.root)), 'attempt': 'attempt-2'}, self.token, at=DAY)
        checkpoint = self.root/'inbox/control/runtime/checkpoints/A.json'
        row = json.loads(checkpoint.read_text()); row.update(status='IDLE', slot_released=True)
        save_json(checkpoint, row)
        with self.assertRaisesRegex(ValueError, '未释放'):
            claim_task(self.root, 'A', {**self.spec, 'dispatch': str(other.relative_to(self.root)), 'attempt': 'attempt-2'}, self.token, at=DAY)

    def test_cross_role_stale_token_wrong_base_and_branch_reject_before_ack(self):
        for change in ({'dispatch': '../outside.md'}, {'base': '1'*40}, {'branch': 'other'}, {'task': 'TASK-02'}):
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    claim_task(self.root, 'A', {**self.spec, **change}, self.token, at=DAY)
        save_json(self.token, {'token': 'stale'})
        with self.assertRaises(ValueError): self.claim()
        self.assertFalse((self.dispatch.parent/'attempt-1-ack.md').exists())

    def test_cancelled_dispatch_and_existing_ack_cannot_restart_work(self):
        self.dispatch.write_text(self.dispatch.read_text().replace('DISPATCHED', 'CANCELLED'))
        with self.assertRaises(ValueError): self.claim()
        self.dispatch.write_text(self.dispatch.read_text().replace('CANCELLED', 'DISPATCHED'))
        ack = self.dispatch.parent/'attempt-1-ack.md'
        ack.write_text('- status: ACK\n- task: TASK-01\n- attempt: attempt-1\n- dispatch: '+self.spec['dispatch'])
        result = self.claim(); self.assertTrue(result['already_acknowledged'])
        self.assertFalse((self.root/'events/A').exists())
        self.assertEqual(pending_claim_notifications(self.root, 'A', at=DAY), [])

    def test_crash_after_ack_recovers_without_second_event_or_notification(self):
        with patch('task_claim.record_role', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError): self.claim()
        self.claim(); self.claim()
        self.assertEqual(len(pending_claim_notifications(self.root, 'A', at=DAY)), 1)
        self.assertEqual(len(list((self.root/'events/A').glob('*.json'))), 1)

    def test_uncertain_send_is_not_automatically_retried(self):
        self.claim()
        def failed(row): raise TimeoutError('delivery outcome unknown')
        drain_claim_notifications(self.root, 'A', failed, at=DAY)
        drain_claim_notifications(self.root, 'A', lambda row: self.fail('uncertain delivery repeated'), at=DAY)

    def test_slot_release_requires_current_real_delivery_and_coordinator_lease(self):
        self.claim()
        receipt = self.root/'receipts/TASK-01/attempt-1.md'; receipt.parent.mkdir(parents=True)
        receipt.write_text('TASK-01 attempt-1 已交付 '+self.base)
        spec = {'task': 'TASK-01', 'attempt': 'attempt-1', 'evidence': str(receipt.relative_to(self.root))}
        with self.assertRaises(ValueError): release_slot(self.root, 'A', spec, self.coordinator_token, at=DAY)
        delivery = {'event_id': 'A:TASK-01:attempt-1:delivery', 'kind': 'business', 'task': 'TASK-01',
                    'attempt': 'attempt-1', 'status': 'REVIEW_PENDING', 'summary': '已冻结交付',
                    'evidence': spec['evidence'], 'next': '等待审查', 'occurrence': {'kind': 'now'}}
        result = record_role(self.root, 'A', delivery, self.token)
        checkpoint = {'schema_version': 1, 'role': 'A', **delivery, 'recorded_at': DAY.isoformat()}
        save_json(self.root/'inbox/control/runtime/checkpoints/A.json', checkpoint)
        event_before = Path(result['event']).read_bytes()
        release_slot(self.root, 'A', spec, self.coordinator_token, at=DAY)
        stored = json.loads((self.root/'inbox/control/runtime/checkpoints/A.json').read_text())
        self.assertTrue(stored['slot_released']); self.assertEqual(Path(result['event']).read_bytes(), event_before)
        with self.assertRaises(ValueError):
            release_slot(self.root, 'A', {**spec, 'attempt': 'attempt-2'}, self.coordinator_token, at=DAY)
        receipt.write_text(receipt.read_text()+'\n交付原件已变化\n')
        other = self.dispatch.with_name('attempt-2-dispatch.md')
        other.write_text(self.dispatch.read_text().replace('attempt-1', 'attempt-2'))
        with self.assertRaisesRegex(ValueError, '证据发生变化'):
            claim_task(self.root, 'A', {**self.spec, 'dispatch': str(other.relative_to(self.root)),
                                      'attempt': 'attempt-2'}, self.token, at=DAY)
        self.assertFalse(other.with_name('attempt-2-ack.md').exists())

    def test_crlf_delivery_receipt_can_release_slot_for_next_attempt(self):
        self.claim()
        receipt = self.root/'receipts/crlf.md'; receipt.parent.mkdir()
        receipt.write_bytes(b'TASK-01 attempt-1 delivered\r\n')
        record_role(self.root, 'A', {'event_id': 'A:CRLF-delivery', 'kind': 'business',
            'task': 'TASK-01', 'attempt': 'attempt-1', 'status': 'REVIEW_PENDING',
            'summary': 'delivered', 'evidence': 'receipts/crlf.md', 'next': 'review',
            'occurrence': {'kind': 'now'}}, self.token)
        release_slot(self.root, 'A', {'task': 'TASK-01', 'attempt': 'attempt-1',
            'evidence': 'receipts/crlf.md'}, self.coordinator_token, at=DAY)
        other = self.dispatch.with_name('attempt-2-dispatch.md')
        other.write_text(self.dispatch.read_text().replace('attempt-1', 'attempt-2'))
        next_attempt = claim_task(self.root, 'A', {**self.spec,
            'dispatch': str(other.relative_to(self.root)), 'attempt': 'attempt-2'}, self.token, at=DAY)
        self.assertTrue(next_attempt['claimed'])


if __name__ == '__main__': unittest.main()
