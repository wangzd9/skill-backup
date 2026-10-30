import concurrent.futures
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import document_store as store


class DocumentStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root/'sessions/A.md'
        self.path.parent.mkdir()
        self.path.write_text('# A\n原有记录保留\n')
        self.checkpoint = {'role': 'A', 'task': 'TASK-1', 'attempt': 'attempt-1',
                           'status': 'RUNNING', 'next': '仍需独立验证', 'evidence': 'receipts/current.md'}
        self.policy = {'trigger_bytes': 1500, 'target_bytes': 1100, 'trigger_lines': 400, 'keep_records': 2}

    def append(self, n, **kwargs):
        marker = '<!-- role-record:' + hashlib.sha256(str(n).encode()).hexdigest() + ' -->'
        block = '\n'+marker+'\n- '+str(n)+'：'+'真实记录'*45+'\n'
        return store.append_record(self.root, self.path, marker, block,
                                   checkpoint=self.checkpoint, policy=self.policy, **kwargs)

    def test_rotation_preserves_history_checkpoint_and_replay_identity(self):
        for n in range(10): self.append(n)
        before = self.path.read_bytes()
        self.assertLessEqual(len(before), self.policy['target_bytes'])
        self.assertIn('TASK-1', before.decode())
        self.assertIn('仍需独立验证', before.decode())
        self.assertTrue(list((self.root/'archive/role-runtime').rglob('*.md')))
        self.assertFalse(self.append(0)['appended'])
        self.assertEqual(before, self.path.read_bytes())
        store.verify_archives(self.root, self.path)

    def test_rotation_crash_after_replace_recovers_without_duplicate(self):
        self.append(0)
        original = store._commit_rotation
        with patch.object(store, '_commit_rotation', side_effect=OSError('crash after replace')):
            with self.assertRaises(OSError):
                store.rotate_document(self.root, self.path, self.checkpoint, force=True, policy=self.policy)
        with patch.object(store, '_commit_rotation', wraps=original):
            self.append(0)
        self.assertLessEqual(self.path.read_text().count('role-record:'), 2)
        self.assertFalse(self.append(0)['appended'])
        store.verify_archives(self.root, self.path)

    def test_concurrent_append_and_rotation_keep_every_record_once(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(self.append, range(20)))
        for n in range(20): self.assertFalse(self.append(n)['appended'])
        self.assertLessEqual(self.path.stat().st_size, self.policy['target_bytes'])
        store.verify_archives(self.root, self.path)

    def test_tampered_active_record_or_archive_is_rejected(self):
        self.append(0)
        original = self.path.read_text()
        self.path.write_text(self.path.read_text().replace('真实记录', '伪造记录'))
        with self.assertRaisesRegex(ValueError, 'CONFLICT'): self.append(0)
        self.path.write_text('# A\n')
        with self.assertRaisesRegex(ValueError, 'CONFLICT'): self.append(1)
        self.path.write_text(original)
        for n in range(1, 4): self.append(n)
        archive = next(p for p in (self.root/'archive/role-runtime').rglob('*.md') if not p.name.endswith('.active.md'))
        archive.write_text('tampered')
        with self.assertRaisesRegex(ValueError, 'CONFLICT'): store.verify_archives(self.root, self.path)

    def test_single_large_record_has_complete_external_evidence(self):
        marker = '<!-- role-record:large -->'
        block = '\n'+marker+'\n'+'大记录'*4000+'\n'
        store.append_record(self.root, self.path, marker, block,
                            checkpoint=self.checkpoint, policy=self.policy)
        self.assertLessEqual(self.path.stat().st_size, self.policy['target_bytes'])
        raw = list((self.root/'archive/role-runtime').rglob('*.md'))
        self.assertTrue(any(block in p.read_text() for p in raw))
        self.assertFalse(store.append_record(self.root, self.path, marker, block,
                         checkpoint=self.checkpoint, policy=self.policy)['appended'])

    def test_indexed_marker_lookup_detects_tampered_archive(self):
        for n in range(4): self.append(n)
        archive = next(p for p in (self.root/'archive/role-runtime').rglob('*.md') if not p.name.endswith('.active.md'))
        archive.write_text('tampered archive')
        with self.assertRaisesRegex(ValueError, 'CONFLICT'):
            store.indexed_markers(self.root, self.path)

    def test_rotation_rejects_changed_indexed_payload_before_replacing_active_document(self):
        self.append(0)
        self.path.write_text(self.path.read_text().replace('真实记录', '伪造记录'))
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'CONFLICT'):
            store.rotate_document(self.root, self.path, self.checkpoint, force=True, policy=self.policy)
        self.assertEqual(self.path.read_bytes(), before)

    def test_recovery_cannot_redirect_material_outside_this_document_archive(self):
        self.append(0)
        with patch.object(store, '_commit_rotation', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):
                store.rotate_document(self.root, self.path, self.checkpoint, force=True, policy=self.policy)
        _, _, _, _, journal = store._paths(self.root, self.path)
        row = json.loads(journal.read_text()); row['source'] = 'sessions/OTHER.md'
        journal.write_text(json.dumps(row))
        with self.assertRaisesRegex(ValueError, 'CONFLICT'):
            self.append(0)

    def test_recovery_rejects_valid_bytes_under_an_unrelated_archive_location(self):
        self.append(0)
        with patch.object(store, '_commit_rotation', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):
                store.rotate_document(self.root, self.path, self.checkpoint, force=True, policy=self.policy)
        _, _, _, _, journal = store._paths(self.root, self.path)
        row = json.loads(journal.read_text())
        foreign = self.root/'unrelated.md'; foreign.write_bytes((self.root/row['archive']).read_bytes())
        row['archive'] = 'unrelated.md'; journal.write_text(json.dumps(row))
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'CONFLICT'): self.append(0)
        self.assertEqual(self.path.read_bytes(), before)

    def test_history_limit_does_not_remove_current_gate_or_recovery_identity(self):
        for n in range(36):
            marker = f'<!-- role-record:history-{n} -->'
            store.append_record(self.root, self.path, marker, '\n'+marker+f'\n历史记录 {n}\n',
                                checkpoint=self.checkpoint, policy={'trigger_bytes': 100000, 'trigger_lines': 10000})
        checkpoint = {**self.checkpoint, 'pending_actions': ['核验 TASK-1 的独立测试回执'],
                      'open_gates': ['固定快照未验证，禁止合入'], 'references': ['inbox/A/current-dispatch.md']}
        store.rotate_document(self.root, self.path, checkpoint, force=True,
                              policy={'target_bytes': 4096, 'keep_records': 2})
        body = self.path.read_text()
        self.assertNotIn('历史记录 0\n', body)
        self.assertIn('固定快照未验证，禁止合入', body)
        self.assertIn('核验 TASK-1 的独立测试回执', body)
        self.assertIn('inbox/control/runtime/continuations/A/', body)
        manifests = list((self.root/'archive/role-runtime').rglob('*.json'))
        self.assertEqual(json.loads(manifests[0].read_text())['checkpoint'], checkpoint)
        self.assertIn('<!-- role-record:history-0 -->', store.indexed_markers(self.root, self.path))


if __name__ == '__main__': unittest.main()
