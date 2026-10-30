import json
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from context_input import (build_context, publish_continuation, acknowledge_continuation,
                           pending_continuations, recover_continuation_input)
from runtime_common import save_json
import role_runtime
import argparse


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        binding = {'schema_version': 1, 'project_root': str(self.root), 'control': 'ACTIVE',
                   'roles': {'A': {'session_file': 'sessions/A.md'}}}
        (self.root/'ROLE-BINDINGS.json').write_text(json.dumps(binding))
        (self.root/'TODO.md').write_text('# TODO\n')
        (self.root/'sessions').mkdir(); (self.root/'sessions/A.md').write_text('不应注入历史'*20000)
        (self.root/'receipt.md').write_text('TASK-1/attempt-1 尚需验证')
        claim = role_runtime.execute(argparse.Namespace(action='claim', project=str(self.root), role='A',
                                                        owner='test', pid=None, ttl=300))
        self.token = self.root/'token.json'; self.token.write_text(json.dumps({'token': claim['token']}))
        self.spec = {'continuation_id': 'TASK-1-attempt-1-verify', 'task': 'TASK-1', 'attempt': 'attempt-1',
                     'checkpoint': 'receipt.md', 'next': '核对独立验证回执', 'runnable': True}
        save_json(self.root/'inbox/control/runtime/checkpoints/A.json',
                  {'schema_version': 1, 'role': 'A', 'task': 'TASK-1', 'attempt': 'attempt-1',
                   'status': 'RUNNING', 'event_id': 'A:current'})
        (self.root/'completion.md').write_text('TASK-1 attempt-1 已处理本次核对；证据 actual-result.json')

    def test_context_is_bounded_and_contains_versions_not_history(self):
        value = build_context(self.root, 'A')
        rendered = json.dumps(value, ensure_ascii=False)
        self.assertNotIn('不应注入历史', rendered)
        self.assertIn('sha256', rendered)
        self.assertLess(len(rendered.encode()), 16000)

    def test_continuation_replays_once_and_ack_preserves_new_inputs(self):
        first = publish_continuation(self.root, 'A', self.spec, self.token)
        self.assertEqual(first, publish_continuation(self.root, 'A', self.spec, self.token))
        self.assertEqual(len(pending_continuations(self.root, 'A')), 1)
        second = {**self.spec, 'continuation_id': 'TASK-1-attempt-1-delivery', 'next': '整理交付'}
        publish_continuation(self.root, 'A', second, self.token)
        acknowledge_continuation(self.root, 'A', first['continuation_id'], 'completion.md', self.token,
                                 expected_sha256=first['input_sha256'])
        self.assertEqual([r['continuation_id'] for r in pending_continuations(self.root, 'A')], [second['continuation_id']])
        self.assertEqual('HANDLED', publish_continuation(self.root, 'A', self.spec, self.token)['status'])

    def test_conflicting_checkpoint_and_wait_only_continuation_are_rejected(self):
        publish_continuation(self.root, 'A', self.spec, self.token)
        with self.assertRaisesRegex(ValueError, 'CONFLICT'):
            publish_continuation(self.root, 'A', {**self.spec, 'next': '不同动作'}, self.token)
        with self.assertRaises(ValueError):
            publish_continuation(self.root, 'A', {**self.spec, 'runnable': False}, self.token)
        (self.root/'receipt.md').write_text('输入变了')
        original = publish_continuation(self.root, 'A', self.spec, self.token)
        self.assertNotEqual(original['checkpoint_sha256'], hashlib.sha256((self.root/'receipt.md').read_bytes()).hexdigest())

    def test_large_dispatch_index_is_a_bounded_preview_with_full_source_pointer(self):
        save_json(self.root/'inbox/control/runtime/dispatch-index/A.json', {'schema_version': 1, 'role': 'A',
            'entries': [{'source': f'inbox/1号/TASK-{n}/dispatch.md', 'summary': '很长的材料'*1000} for n in range(1000)]})
        value = build_context(self.root, 'A')
        self.assertLess(len(json.dumps(value, ensure_ascii=False).encode()), 32768)
        self.assertEqual(value['dispatch_index']['entry_count'], 1000)
        self.assertIn('full_source_index', value['dispatch_index'])

    def test_current_task_dispatch_is_not_hidden_behind_old_cards(self):
        entries = [{'source': f'inbox/1号/TASK-{n}/dispatch.md', 'task': f'TASK-{n}',
                    'attempt': 'attempt-1'} for n in range(20, 50)]
        entries.append({'source': 'inbox/1号/TASK-1/dispatch.md', 'task': 'TASK-1', 'attempt': 'attempt-1'})
        save_json(self.root/'inbox/control/runtime/dispatch-index/A.json', {'entries': entries})
        value = build_context(self.root, 'A')
        self.assertEqual(value['dispatch_index']['entries'][0]['task'], 'TASK-1')
        self.assertEqual(value['dispatch_index']['entry_count'], 31)

    def test_stale_fingerprint_wrong_task_and_original_checkpoint_do_not_ack_work(self):
        first = publish_continuation(self.root, 'A', self.spec, self.token)
        for evidence, fingerprint in (('completion.md', 'stale'), ('receipt.md', first['input_sha256'])):
            with self.subTest(evidence=evidence, fingerprint=fingerprint):
                with self.assertRaises(ValueError):
                    acknowledge_continuation(self.root, 'A', first['continuation_id'], evidence, self.token,
                                             expected_sha256=fingerprint)
        save_json(self.root/'inbox/control/runtime/checkpoints/A.json',
                  {'schema_version': 1, 'role': 'A', 'task': 'TASK-2', 'attempt': 'attempt-2', 'status': 'RUNNING'})
        with self.assertRaises(ValueError):
            acknowledge_continuation(self.root, 'A', first['continuation_id'], 'completion.md', self.token,
                                     expected_sha256=first['input_sha256'])
        self.assertEqual(len(pending_continuations(self.root, 'A')), 1)

    def test_tampered_pending_action_is_not_offered_as_a_new_runnable_input(self):
        publish_continuation(self.root, 'A', self.spec, self.token)
        path = next((self.root/'inbox/control/runtime/continuations/A').glob('*.json'))
        row = json.loads(path.read_text()); row['next'] = '偷偷更改的动作'; save_json(path, row)
        with self.assertRaisesRegex(ValueError, 'CONFLICT'): pending_continuations(self.root, 'A')

    def test_completion_proof_for_another_task_does_not_clear_pending_action(self):
        first = publish_continuation(self.root, 'A', self.spec, self.token)
        (self.root/'completion.md').write_text('TASK-11 attempt-10 完成')
        with self.assertRaises(ValueError):
            acknowledge_continuation(self.root, 'A', first['continuation_id'], 'completion.md', self.token,
                                     expected_sha256=first['input_sha256'])
        self.assertEqual(len(pending_continuations(self.root, 'A')), 1)

    def test_activity_checkpoint_can_change_without_destroying_frozen_input(self):
        first = publish_continuation(self.root, 'A', self.spec, self.token)
        (self.root/'receipt.md').write_text('TASK-1 attempt-1 消费动作导致活动文档追加')
        self.assertEqual(first, publish_continuation(self.root, 'A', self.spec, self.token))
        (self.root/'receipt.md').unlink()
        self.assertTrue(pending_continuations(self.root, 'A')[0]['runnable_now'])
        self.assertEqual(acknowledge_continuation(self.root, 'A', first['continuation_id'], 'completion.md', self.token,
                         expected_sha256=first['input_sha256'])['status'], 'HANDLED')

    def test_tampered_snapshot_requires_recovery_and_never_consumes_pending(self):
        first = publish_continuation(self.root, 'A', self.spec, self.token)
        (self.root/first['checkpoint_snapshot']).write_text('被篡改的输入')
        pending = pending_continuations(self.root, 'A')[0]
        self.assertFalse(pending['runnable_now'])
        self.assertEqual(pending['resume_state'], 'INPUT_RECOVERY_REQUIRED')
        self.assertEqual(pending['input_sha256'], first['input_sha256'])
        with self.assertRaisesRegex(ValueError, 'CONTINUATION_CONFLICT'):
            acknowledge_continuation(self.root, 'A', first['continuation_id'], 'completion.md', self.token,
                                     expected_sha256=first['input_sha256'])

    def test_legacy_changed_input_is_preserved_and_requires_exact_original_recovery(self):
        first = publish_continuation(self.root, 'A', self.spec, self.token)
        path = next((self.root/'inbox/control/runtime/continuations/A').glob('*.json'))
        del first['checkpoint_snapshot']; save_json(path, first)
        original = (self.root/'receipt.md').read_bytes()
        (self.root/'receipt.md').write_text('TASK-1 attempt-1 活动文档变更')
        before = path.read_bytes()
        self.assertFalse(pending_continuations(self.root, 'A')[0]['runnable_now'])
        self.assertEqual(path.read_bytes(), before)
        context = build_context(self.root, 'A')['continuations'][0]
        self.assertFalse(context['runnable_now'])
        self.assertEqual(context['resume_state'], 'INPUT_RECOVERY_REQUIRED')
        self.assertEqual(context['source'], str(path.relative_to(self.root)))
        for evidence, fingerprint in (('receipt.md', first['input_sha256']), ('completion.md', 'stale')):
            with self.assertRaisesRegex(ValueError, 'CONTINUATION_CONFLICT'):
                recover_continuation_input(self.root, 'A', first['continuation_id'], evidence, self.token,
                                           expected_sha256=fingerprint)
        (self.root/'recovered-original.md').write_bytes(original)
        repaired = recover_continuation_input(self.root, 'A', first['continuation_id'], 'recovered-original.md', self.token,
                                              expected_sha256=first['input_sha256'])
        self.assertEqual(repaired['status'], 'PENDING')
        self.assertEqual(repaired['input_sha256'], first['input_sha256'])
        self.assertTrue(pending_continuations(self.root, 'A')[0]['runnable_now'])
        acknowledge_continuation(self.root, 'A', first['continuation_id'], 'completion.md', self.token,
                                 expected_sha256=first['input_sha256'])

    def test_snapshot_survives_journal_publication_interruption(self):
        from unittest.mock import patch
        import context_input
        original_save = context_input.save_json
        with patch('context_input.save_json', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError): publish_continuation(self.root, 'A', self.spec, self.token)
        self.assertEqual(pending_continuations(self.root, 'A'), [])
        self.assertEqual(len(list((self.root/'inbox/control/runtime/continuation-inputs').glob('*.snapshot'))), 1)
        with patch('context_input.save_json', original_save):
            value = publish_continuation(self.root, 'A', self.spec, self.token)
        self.assertTrue((self.root/value['checkpoint_snapshot']).is_file())

    def test_snapshot_pointer_cannot_be_rebound_to_other_input(self):
        first = publish_continuation(self.root, 'A', self.spec, self.token)
        path = next((self.root/'inbox/control/runtime/continuations/A').glob('*.json'))
        first['checkpoint_snapshot'] = 'receipt.md'; save_json(path, first)
        with self.assertRaisesRegex(ValueError, 'CONTINUATION_CONFLICT'):
            pending_continuations(self.root, 'A')

    def test_unchanged_legacy_input_still_acknowledges_and_recovery_requires_owner_lease(self):
        first = publish_continuation(self.root, 'A', self.spec, self.token)
        path = next((self.root/'inbox/control/runtime/continuations/A').glob('*.json'))
        del first['checkpoint_snapshot']; save_json(path, first)
        wrong = self.root/'wrong-token.json'; wrong.write_text(json.dumps({'token':'foreign'}))
        with self.assertRaises(ValueError):
            recover_continuation_input(self.root, 'A', first['continuation_id'], 'receipt.md', wrong,
                                       expected_sha256=first['input_sha256'])
        self.assertTrue(pending_continuations(self.root, 'A')[0]['runnable_now'])
        acknowledge_continuation(self.root, 'A', first['continuation_id'], 'completion.md', self.token,
                                 expected_sha256=first['input_sha256'])

    def test_updated_current_business_checkpoint_can_ack_unchanged_frozen_evidence(self):
        first = publish_continuation(self.root, 'A', self.spec, self.token)
        save_json(self.root/'inbox/control/runtime/checkpoints/A.json',
                  {'schema_version': 1, 'role': 'A', 'task': 'TASK-1', 'attempt': 'attempt-1',
                   'status': 'DELIVERED', 'event_id': 'A:completed', 'head': 'new-business-head'})
        result = acknowledge_continuation(self.root, 'A', first['continuation_id'], 'completion.md', self.token,
                                          expected_sha256=first['input_sha256'])
        self.assertEqual(result['status'], 'HANDLED')
        self.assertEqual(pending_continuations(self.root, 'A'), [])

    def _legacy_recovery_candidate(self):
        row = publish_continuation(self.root, 'A', self.spec, self.token)
        snapshot = self.root/row.pop('checkpoint_snapshot')
        snapshot.unlink()
        path = next((self.root/'inbox/control/runtime/continuations/A').glob('*.json'))
        save_json(path, row)
        raw = (self.root/'receipt.md').read_bytes()
        (self.root/'receipt.md').write_text('TASK-1 attempt-1 后续业务追加')
        candidate = self.root/'inbox/control/recovery-candidates'/(row['checkpoint_sha256']+'.snapshot')
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(raw)
        return row, path, candidate

    def test_verified_recovery_candidate_has_concrete_argv_without_claim_ack_or_secret(self):
        row, path, candidate = self._legacy_recovery_candidate()
        before = {str(item.relative_to(self.root)):item.read_bytes() for item in self.root.rglob('*') if item.is_file()}
        value = build_context(self.root, 'A')
        instructions = value['recovery_instructions']
        item = instructions['items'][0]
        self.assertEqual('READY_FOR_OWNER_INPUT_RECOVERY', item['state'])
        self.assertEqual(row['input_sha256'], item['input_sha256'])
        self.assertEqual(row['checkpoint_sha256'], item['candidate_sha256'])
        argv = item['recovery_argv_template']
        self.assertEqual('continuation-recover-input', argv[2])
        for option, expected in (('--id', row['continuation_id']), ('--fingerprint', row['input_sha256']),
                                 ('--evidence', str(candidate.relative_to(self.root))), ('--role', 'A')):
            self.assertEqual(expected, argv[argv.index(option)+1])
        self.assertEqual('<PRIVATE_CLAIM_JSON_0600>', argv[argv.index('--token-file')+1])
        self.assertEqual('worker-identity', instructions['worker_identity_argv'][-1])
        self.assertIn('<VERIFIED_WORKER_PID>', instructions['lease']['claim_argv_template'])
        self.assertIn('<VERIFIED_WORKER_PID>', instructions['lease']['renew_argv_template'])
        self.assertNotIn(json.loads(self.token.read_text())['token'], json.dumps(instructions))
        after = {str(item.relative_to(self.root)):item.read_bytes() for item in self.root.rglob('*') if item.is_file()}
        self.assertEqual(before, after)
        self.assertEqual('PENDING', json.loads(path.read_text())['status'])
        self.assertFalse(value['continuations'][0]['runnable_now'])

    def test_missing_or_wrong_hash_candidate_never_offers_recovery_or_claim_argv(self):
        row, _, candidate = self._legacy_recovery_candidate()
        raw = candidate.read_bytes()
        candidate.unlink()
        # The old descriptive filename is not a deterministic approved input.
        (candidate.parent/'tester-prep-original.snapshot').write_bytes(raw)
        for raw_candidate, expected in ((None, 'CANDIDATE_UNAVAILABLE'), (b'wrong input', 'CANDIDATE_HASH_MISMATCH')):
            with self.subTest(expected=expected):
                if raw_candidate is not None: candidate.write_bytes(raw_candidate)
                value = build_context(self.root, 'A')['recovery_instructions']
                self.assertEqual(expected, value['items'][0]['state'])
                self.assertNotIn('recovery_argv_template', value['items'][0])
                self.assertNotIn('lease', value)
                self.assertNotIn('worker_identity_argv', value)

    def test_candidate_requires_exact_current_role_task_and_attempt(self):
        self._legacy_recovery_candidate()
        checkpoint = self.root/'inbox/control/runtime/checkpoints/A.json'
        original = json.loads(checkpoint.read_text())
        for difference in ({'task': 'TASK-2'}, {'attempt': 'attempt-2'}, {'role': 'TESTER'}, {'schema_version': 0}):
            with self.subTest(difference=difference):
                save_json(checkpoint, {**original, **difference})
                value = build_context(self.root, 'A')['recovery_instructions']
                self.assertEqual('CURRENT_TASK_ATTEMPT_CONFLICT', value['items'][0]['state'])
                self.assertNotIn('recovery_argv_template', value['items'][0])
                self.assertNotIn('lease', value)

    def test_paused_project_and_corrupted_immutable_snapshot_do_not_offer_execution(self):
        row, _, _ = self._legacy_recovery_candidate()
        binding_path = self.root/'ROLE-BINDINGS.json'
        binding = json.loads(binding_path.read_text())
        save_json(binding_path, {**binding, 'control': 'PAUSED'})
        value = build_context(self.root, 'A')['recovery_instructions']
        self.assertEqual('PROJECT_NOT_ACTIVE', value['items'][0]['state'])
        self.assertNotIn('lease', value)
        save_json(binding_path, binding)
        snapshot = self.root/'inbox/control/runtime/continuation-inputs'/(row['checkpoint_sha256']+'.snapshot')
        snapshot.write_bytes(b'corrupted immutable input')
        value = build_context(self.root, 'A')['recovery_instructions']
        self.assertEqual('IMMUTABLE_SNAPSHOT_CONFLICT', value['items'][0]['state'])
        self.assertNotIn('recovery_argv_template', value['items'][0])

    def test_recovery_candidate_symlink_is_rejected_even_when_bytes_match(self):
        _, _, candidate = self._legacy_recovery_candidate()
        original = candidate.parent/'original-evidence.md'
        original.write_bytes(candidate.read_bytes())
        candidate.unlink(); candidate.symlink_to(original)
        value = build_context(self.root, 'A')['recovery_instructions']
        self.assertEqual('CANDIDATE_UNAVAILABLE', value['items'][0]['state'])
        self.assertNotIn('lease', value)

    def test_healthy_continuation_has_no_recovery_execution_guidance(self):
        publish_continuation(self.root, 'A', self.spec, self.token)
        self.assertEqual({'state': 'NOT_REQUIRED'}, build_context(self.root, 'A')['recovery_instructions'])


if __name__ == '__main__': unittest.main()
