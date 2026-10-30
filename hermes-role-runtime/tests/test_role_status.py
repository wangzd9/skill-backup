import argparse
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import role_runtime
from runtime_common import save_json
from role_status import role_status, collect_role_statuses, render_status_line, refresh_dispatch_index

AT = datetime.fromisoformat('2026-10-16T10:00:00+08:00')


class RoleStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        save_json(self.root/'ROLE-BINDINGS.json', {'schema_version': 1, 'project_root': str(self.root),
            'control': 'ACTIVE', 'roles': {r: {'session_file': f'sessions/{r}.md'}
                                         for r in ('A', 'B', 'C', 'TESTER', 'COORDINATOR')}})
        self.base = self.root/'inbox/control/runtime'

    def checkpoint(self, **kwargs):
        row = {'schema_version': 1, 'role': 'A', 'task': 'TASK-01', 'attempt': 'attempt-1',
               'status': 'RUNNING', 'event_id': 'A:current', 'recorded_at': AT.isoformat(), **kwargs}
        save_json(self.base/'checkpoints/A.json', row)
        return row

    def index(self, entries=()):
        refresh_dispatch_index(self.root, 'A', list(entries))

    def released_checkpoint(self):
        receipt = self.root/'receipts/TASK-01/attempt-1.md'
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text('TASK-01 attempt-1 已冻结交付，等待审核。')
        event = {'event_id': 'A:delivery', 'role': 'A', 'task': 'TASK-01',
                 'attempt': 'attempt-1', 'status': 'REVIEW_PENDING',
                 'evidence': str(receipt.relative_to(self.root))}
        path = self.root/'events/A'/(hashlib.sha256(event['event_id'].encode()).hexdigest()+'.json')
        save_json(path, event)
        return self.checkpoint(status='REVIEW_PENDING', slot_released=True,
            slot_release={'task': 'TASK-01', 'attempt': 'attempt-1',
                'delivery_event_id': event['event_id'],
                'delivery_event_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                'evidence': str(receipt.relative_to(self.root)),
                'evidence_sha256': hashlib.sha256(receipt.read_bytes()).hexdigest()})

    def test_missing_material_is_unknown_without_reading_old_session(self):
        session = self.root/'sessions/A.md'; session.parent.mkdir(); session.write_text('IDLE\n'*100000)
        self.assertEqual(role_status(self.root, 'A')['status'], 'UNKNOWN')
        self.assertFalse(role_status(self.root, 'A')['available'])

    def test_delivered_and_released_is_available_while_review_remains_pending(self):
        self.released_checkpoint()
        self.index()
        self.assertEqual(role_status(self.root, 'A')['status'], 'IDLE')
        self.assertTrue(role_status(self.root, 'A')['available'])
        self.assertIn('执行者 A（空闲）', render_status_line(collect_role_statuses(self.root)))

    def test_missing_or_changed_release_evidence_is_unknown(self):
        for artifact, changed in (('receipt', False), ('receipt', True), ('event', False), ('event', True)):
            with self.subTest(artifact=artifact, changed=changed):
                row = self.released_checkpoint()
                self.index()
                release = row['slot_release']
                path = (self.root/release['evidence'] if artifact == 'receipt' else
                        self.root/'events/A'/(hashlib.sha256(release['delivery_event_id'].encode()).hexdigest()+'.json'))
                if changed:
                    path.write_text(path.read_text()+'\nchanged\n')
                else:
                    path.unlink()
                status = role_status(self.root, 'A')
                self.assertEqual(status['status'], 'UNKNOWN')
                self.assertFalse(status['available'])

    def test_free_idle_or_delivered_label_does_not_release_slot(self):
        for status in ('IDLE', 'REVIEW_PENDING'):
            with self.subTest(status=status):
                self.checkpoint(status=status)
                self.index()
                self.assertFalse(role_status(self.root, 'A')['available'])

    def test_bound_live_worker_is_working_and_unverified_owner_is_unknown(self):
        self.checkpoint()
        self.index()
        save_json(self.base/'leases/A.json', {'owner': 'legacy', 'expires_at': 0})
        self.assertEqual(role_status(self.root, 'A')['status'], 'UNKNOWN')
        save_json(self.base/'leases/A.json', {'owner': 'worker', 'pid': os.getpid(),
                                            'process_start': role_runtime.pid_identity(os.getpid())})
        self.assertEqual(role_status(self.root, 'A')['status'], 'WORKING')

    def test_waiting_blocked_paused_are_not_idle(self):
        self.index()
        for recorded, expected in (('WAITING', 'WAITING'), ('BLOCKED', 'BLOCKED'), ('PAUSED', 'PAUSED')):
            with self.subTest(recorded=recorded):
                self.checkpoint(status=recorded)
                row = role_status(self.root, 'A')
                self.assertEqual(row['status'], expected)
                self.assertFalse(row['available'])

    def test_new_dispatch_cancels_previous_availability(self):
        self.released_checkpoint()
        dispatch = self.root/'inbox/1号/TASK-02/attempt-1-dispatch.md'
        dispatch.parent.mkdir(parents=True); dispatch.write_text('# 派单 TASK-02\n- status: DISPATCHED\n')
        self.index([{'source': str(dispatch.relative_to(self.root)), 'status': 'DISPATCHED'}])
        row = role_status(self.root, 'A')
        self.assertEqual(row['status'], 'WAITING')
        self.assertFalse(row['available'])

    def test_changed_dispatch_is_unknown_until_scan_refreshes_index(self):
        dispatch = self.root/'inbox/1号/TASK-01/attempt-1-dispatch.md'
        dispatch.parent.mkdir(parents=True); dispatch.write_text('# 派单 TASK-01\n- status: DISPATCHED\n')
        self.index([{'source': str(dispatch.relative_to(self.root)), 'status': 'DISPATCHED'}])
        dispatch.write_text('# 派单 TASK-01\n- status: CANCELLED\n')
        self.assertEqual(role_status(self.root, 'A')['status'], 'UNKNOWN')

    def test_report_does_not_claim_lease_or_write_checkpoint(self):
        self.index()
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        collect_role_statuses(self.root)
        after = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_stale_index_never_claims_current_availability(self):
        self.released_checkpoint()
        self.index()
        path = self.base/'dispatch-index/A.json'
        row = json.loads(path.read_text()); row['observed_at'] = AT.isoformat(); save_json(path, row)
        self.assertEqual(role_status(self.root, 'A', at=AT+timedelta(minutes=11))['status'], 'UNKNOWN')

    def test_fresh_complete_empty_scan_can_initialize_a_new_role_as_idle(self):
        (self.root/'inbox/1号').mkdir(parents=True)
        self.index()
        self.assertTrue(role_status(self.root, 'A')['available'])
        refresh_dispatch_index(self.root, 'A', [], operations=[{'status': 'UNKNOWN', 'operation_id': 'unfinished'}])
        self.assertFalse(role_status(self.root, 'A')['available'])


if __name__ == '__main__': unittest.main()
