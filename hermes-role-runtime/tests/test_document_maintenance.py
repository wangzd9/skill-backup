import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from document_maintenance import replace_semantic_document
from runtime_common import save_json


class SemanticArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.active = '| TASK-01 数据处理 | RUNNING / A | TASK-02 回执门禁 | AC-01 独立验证 | A 唯一 owner，整理证据 |\n'
        self.closed = '| TASK-02 旧任务 | DONE | 无 | 已验证 | CLOSED |\n'
        self.header = '# TODO\n| 任务 | 状态 | 依赖 | 进展 | 当前动作 |\n|---|---|---|---|---|\n'
        self.todo = self.root/'TODO.md'; self.todo.write_text(self.header+self.active+self.closed)
        self.handoff = self.root/'HANDOFF.md'; self.handoff.write_text('# 交接\n旧交接\n')
        self.base = self.root/'inbox/control/runtime'
        for role, task, attempt in [('COORDINATOR', 'TASK-01', 'attempt-1'), ('A', 'TASK-01', 'attempt-2')]:
            save_json(self.base/f'checkpoints/{role}.json', {'schema_version': 1, 'role': role,
                'task': task, 'attempt': attempt, 'status': 'RUNNING', 'dispatch': f'inbox/{role}/dispatch.md'})

    def replace(self, source, body):
        return replace_semantic_document(self.root, source, body, hashlib.sha256(source.read_bytes()).hexdigest())

    def handoff_body(self):
        return ('# 当前交接\nTODO.md\nTASK-01 attempt-1 attempt-2\n'
                'inbox/control/runtime/checkpoints/COORDINATOR.json\n'
                'inbox/control/runtime/checkpoints/A.json\ninbox/COORDINATOR/dispatch.md\ninbox/A/dispatch.md\n')

    def test_completed_rows_can_archive_while_unfinished_contract_stays_active(self):
        result = self.replace(self.todo, self.header+self.active)
        self.assertTrue(result['archived'])
        self.assertNotIn(self.closed, self.todo.read_text())
        self.assertIn(self.closed, (self.root/result['archive']).read_text())

    def test_same_state_does_not_allow_dropping_owner_dependency_or_acceptance(self):
        for old, new in [('TASK-02 回执门禁', '无'), ('A 唯一 owner，整理证据', '待处理'),
                         ('AC-01 独立验证', '进展中')]:
            with self.subTest(field=old):
                before = self.todo.read_bytes()
                with self.assertRaisesRegex(ValueError, 'preserve'):
                    self.replace(self.todo, self.header+self.active.replace(old, new))
                self.assertEqual(self.todo.read_bytes(), before)

    def test_unfinished_task_detail_section_cannot_disappear(self):
        self.todo.write_text(self.todo.read_text()+'\n## TASK-01 验收契约\n必须验证独立工作树与固定快照。\n')
        with self.assertRaisesRegex(ValueError, 'preserve'):
            self.replace(self.todo, self.header+self.active)

    def test_reviewed_current_obligations_allow_archiving_large_closed_progress_history(self):
        header = '| 任务 | 标志 | 交付行为 | REQ / AC | 依赖 | 阶段与状态 | owner / attempt |\n'
        history = '已闭环的历史回执与测试记录。'*3000
        before = header+'| TASK-01 | RUNNING | 数据处理 | REQ-01 / AC-01 | TASK-02 | '+history+'尚待强审；当前回执 receipts/current.md | A / attempt-2 |\n'
        replacement = header+'| TASK-01 | RUNNING | 数据处理 | REQ-01 / AC-01 | TASK-02 | 尚待强审；当前回执 receipts/current.md | A / attempt-2 |\n'
        self.todo.write_text(before)
        self.assertGreater(len(before.encode()), 64*1024)
        obligations = {'TASK-01': ['尚待强审', '当前回执 receipts/current.md']}
        expected = hashlib.sha256(self.todo.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, 'obligations'):
            replace_semantic_document(self.root, self.todo, replacement, expected)
        with self.assertRaisesRegex(ValueError, 'obligation'):
            replace_semantic_document(self.root, self.todo, replacement.replace('尚待强审', '进展中'), expected,
                                      obligations=obligations)
        with self.assertRaisesRegex(ValueError, 'preserve'):
            replace_semantic_document(self.root, self.todo, replacement.replace('REQ-01 / AC-01', '无'), expected,
                                      obligations=obligations)
        result = replace_semantic_document(self.root, self.todo, replacement, expected, obligations=obligations)
        self.assertLess(len(self.todo.read_bytes()), 48*1024)
        self.assertEqual((self.root/result['archive']).read_text(), before)
        manifest = json.loads((self.root/result['archive']).with_suffix('.json').read_text())
        self.assertEqual(manifest['semantic_obligations'], obligations)

    def test_reviewed_obligations_can_shorten_closed_history_in_an_unfinished_task_section(self):
        before = self.header+self.active+'\n## TASK-01 历史与当前门禁\n'+('历史问题已闭环。'*4000)+'\n当前门禁：AC-01 固定快照独立验证；证据 receipts/open.md\n'
        self.todo.write_text(before)
        replacement = self.header+self.active+'\n## TASK-01 当前恢复\n当前门禁：AC-01 固定快照独立验证；证据 receipts/open.md\n'
        obligations = {'TASK-01': ['AC-01 固定快照独立验证', 'receipts/open.md']}
        expected = hashlib.sha256(self.todo.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, 'obligations'):
            replace_semantic_document(self.root, self.todo, replacement, expected)
        result = replace_semantic_document(self.root, self.todo, replacement, expected, obligations=obligations)
        self.assertLess(len(self.todo.read_bytes()), 48*1024)
        self.assertEqual((self.root/result['archive']).read_text(), before)

    def test_legacy_status_narrative_can_shorten_without_changing_state_owner_or_acceptance(self):
        header = '| 任务 | 交付物 | 依赖 | 验收 | 状态 / owner |\n'
        before = header+'| TASK-01 | 数据处理 | TASK-02 | AC-01 固定快照 | RUNNING / A；'+('旧阶段完成。'*4000)+'当前待强审 |\n'
        self.todo.write_text(before)
        replacement = header+'| TASK-01 | 数据处理 | TASK-02 | AC-01 固定快照 | RUNNING / A；当前待强审 |\n'
        expected = hashlib.sha256(self.todo.read_bytes()).hexdigest()
        obligations = {'TASK-01': ['当前待强审', 'AC-01 固定快照']}
        result = replace_semantic_document(self.root, self.todo, replacement, expected, obligations=obligations)
        self.assertTrue(result['archived'])
        self.assertEqual((self.root/result['archive']).read_text(), before)

    def test_semantic_progress_cannot_discard_task_state_or_current_owner(self):
        replacement = self.header+self.active.replace('AC-01 独立验证', 'AC-01 独立验证已备，等待强审')
        obligations = {'TASK-01': ['AC-01 独立验证已备', '等待强审']}
        for old, new in [('RUNNING / A', 'DONE / A'), ('A 唯一 owner', 'B 唯一 owner')]:
            with self.subTest(field=old):
                with self.assertRaisesRegex(ValueError, 'preserve'):
                    replace_semantic_document(self.root, self.todo, replacement.replace(old, new),
                        hashlib.sha256(self.todo.read_bytes()).hexdigest(), obligations=obligations)

    def test_handoff_requires_current_role_checkpoints_and_obligations(self):
        body = self.handoff_body()
        for missing in ('checkpoints/A.json', 'attempt-2', 'inbox/A/dispatch.md'):
            with self.subTest(missing=missing):
                with self.assertRaises(ValueError): self.replace(self.handoff, body.replace(missing, ''))
        self.assertTrue(self.replace(self.handoff, body)['archived'])

    def test_handoff_without_verified_coordinator_checkpoint_is_rejected(self):
        (self.base/'checkpoints/COORDINATOR.json').unlink()
        with self.assertRaises(ValueError): self.replace(self.handoff, self.handoff_body())

    def test_reviewed_replacement_rejects_changed_source(self):
        expected = hashlib.sha256(self.todo.read_bytes()).hexdigest()
        self.todo.write_text(self.todo.read_text()+'外部更新\n')
        before = self.todo.read_bytes()
        with self.assertRaisesRegex(ValueError, 'CONFLICT'):
            replace_semantic_document(self.root, self.todo, self.header+self.active, expected)
        self.assertEqual(self.todo.read_bytes(), before)


if __name__ == '__main__': unittest.main()
