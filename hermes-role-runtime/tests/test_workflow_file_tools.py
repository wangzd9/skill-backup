"""Real sandbox edits with native handlers and preserved host protections."""
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest import mock
import unittest

ROOT = Path(__file__).resolve().parents[1]
CORE = Path('/home/wang/.hermes/hermes-agent')
sys.path[:0] = [str(ROOT), str(ROOT / 'tests'), str(CORE)]
from test_business_ledger import LedgerFixture
import workflow_runtime as workflow
import workflow_file_tools as edits


class FileToolsTests(LedgerFixture):
    def setUp(self):
        super().setUp()
        self.pool = self.root / 'isolated.json'
        self.pool.write_text('{}')
        self.worker_process, identity = self.bind('A')
        self.workspace = self.path.parent / 'workspaces' / identity['work_id']
        self.workspace.parent.mkdir(parents=True)
        self.git('clone', '-q', '--shared', str(self.repo), str(self.workspace))
        self.scope = {**identity, 'kind': 'business', 'workflow_version': 1,
                      'pool_path': str(self.pool), 'worktree': str(self.workspace),
                      'write_scope': ['app.py']}
        self.handlers = {}
        def register(name, toolset, schema, handler, **kwargs):
            self.assertTrue(kwargs['override'])
            self.handlers[name] = handler
            return object()
        self.plugin = SimpleNamespace(pool_path=self.pool,
            _frozen_scope=lambda _: self.scope, api_attribution_details=lambda _: {'role': 'A'})
        edits.install(SimpleNamespace(register_tool=register), self.plugin)
        self.addCleanup(edits.REGISTERED.discard, str(self.pool.resolve()))

    def call(self, name, arguments):
        return json.loads(self.handlers[name](arguments, session_id='native-test', task_id=str(self.pool)))

    def test_large_native_write_uses_stdin_and_preserves_host(self):
        content = '# ' + ('中文 multiline content ' * 20000) + '\nVALUE = 1\n'
        self.scope['write_scope'].append('new.py')
        decision = workflow.tool_decision(self.scope, 'write_file', {'path': 'new.py', 'content': content})
        self.assertIsNone(decision)
        before = (self.repo / 'app.py').read_bytes()
        result = self.call('write_file', {'path': 'new.py', 'content': content})
        self.assertNotIn('error', result, result)
        self.assertTrue(result['verified'])
        self.assertEqual((self.workspace / 'new.py').read_text(), content)
        self.assertEqual((self.repo / 'app.py').read_bytes(), before)
        self.assertTrue(self.ledger.snapshot()['execution_tools'])
        self.assertIsNone(edits.BACKEND.get())

    def test_native_patch_then_unseen_overwrite_is_still_rejected(self):
        result = self.call('patch', {'path': 'app.py', 'old_string': 'return 0', 'new_string': 'return 42'})
        self.assertTrue(result['success'], result)
        result = self.call('write_file', {'path': 'app.py', 'content': 'value = 99\n'})
        self.assertTrue(result['stale_write_blocked'], result)
        self.assertIn('return 42', (self.workspace / 'app.py').read_text())

    def test_native_add_update_move_delete_patch(self):
        self.scope['write_scope'] += ['added.py', 'moved.py']
        for body in ('*** Add File: added.py\n+VALUE = 2',
                     '*** Update File: added.py\n@@\n-VALUE = 2\n+VALUE = 3',
                     '*** Move File: added.py -> moved.py',
                     '*** Delete File: moved.py'):
            result = self.call('patch', {'mode': 'patch', 'patch': '*** Begin Patch\n' + body + '\n*** End Patch'})
            self.assertTrue(result.get('success'), result)
        self.assertFalse((self.workspace / 'added.py').exists())
        self.assertFalse((self.workspace / 'moved.py').exists())

    def test_all_patch_paths_are_checked_before_any_write(self):
        patch = '*** Begin Patch\n*** Add File: okay.py\n+VALUE = 1\n*** Move File: app.py -> ../escape.py\n*** End Patch'
        self.scope['write_scope'].append('okay.py')
        with self.assertRaisesRegex(ValueError, 'OUTSIDE'):
            self.call('patch', {'mode': 'patch', 'patch': patch})
        self.assertFalse((self.workspace / 'okay.py').exists())

    def test_outside_scope_metadata_and_symlink_are_rejected(self):
        (self.workspace / 'alias.py').symlink_to(self.repo / 'app.py')
        (self.workspace / 'linked').symlink_to(self.repo, target_is_directory=True)
        self.scope['write_scope'] += ['alias.py', 'linked/*', '.git/*']
        for path, reason in ((str(self.repo / 'app.py'), 'OUTSIDE'), ('other.py', 'TASK_SCOPE'),
                             ('.git/config', 'METADATA'), ('alias.py', 'SYMLINK'), ('linked/app.py', 'SYMLINK')):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, reason):
                self.call('write_file', {'path': path, 'content': 'VALUE = 1'})

    def test_symlink_inserted_after_preflight_fails_inside_sandbox(self):
        backend = edits.RemoteOperations(self.scope, ['app.py'])
        (self.workspace / 'app.py').unlink()
        (self.workspace / 'app.py').symlink_to(self.repo / 'app.py')
        with self.assertRaisesRegex(ValueError, 'SYMLINK'):
            backend.write_file(str(self.workspace / 'app.py'), 'VALUE = 9')
        self.assertIn('return 0', (self.repo / 'app.py').read_text())

    def test_hardlink_is_replaced_without_modifying_source_inode(self):
        (self.workspace / 'app.py').unlink()
        os.link(self.repo / 'app.py', self.workspace / 'app.py')
        backend = edits.RemoteOperations(self.scope, ['app.py'])
        result = backend.write_file(str(self.workspace / 'app.py'), 'VALUE = 9\n')
        self.assertFalse(result.error, result)
        self.assertIn('return 0', (self.repo / 'app.py').read_text())

    def test_revoked_writer_cannot_call_backend_even_without_prehook(self):
        self.close(self.worker_process, self.scope)
        with self.assertRaises(ValueError):
            self.call('patch', {'path': 'app.py', 'old_string': 'return 0', 'new_string': 'return 9'})
        self.assertIn('return 0', (self.workspace / 'app.py').read_text())

    def test_report_coordinator_and_drain_are_rejected(self):
        for updates in ({'kind': 'report'}, {'role': 'COORDINATOR'}, {'drain_only': True}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                edits.validate_edit({**self.scope, **updates}, 'write_file', {'path': 'app.py', 'content': ''})

    def test_missing_registered_backend_stays_fail_closed(self):
        edits.REGISTERED.discard(str(self.pool.resolve()))
        with self.assertRaisesRegex(ValueError, 'HANDLER_NOT_READY'):
            workflow.tool_decision(self.scope, 'write_file', {'path': 'app.py', 'content': 'x=1'})

    def test_failed_reregistration_clears_readiness(self):
        with self.assertRaisesRegex(ValueError, 'REGISTRATION_FAILED'):
            edits.install(SimpleNamespace(register_tool=lambda *a, **k: None), self.plugin)
        self.assertFalse(edits.backend_ready(self.scope))

    def test_backend_receipt_identifies_current_source(self):
        receipt = edits.install(SimpleNamespace(register_tool=lambda *a, **k: object()), self.plugin)
        self.assertEqual(receipt['pid'], os.getpid())
        self.assertEqual(receipt['protocol'], 'sandbox-files/1')
        self.assertEqual(set(receipt['source_sha256']), {'workflow_file_tools.py', 'workflow_sandbox.py',
            'workflow_runtime.py', 'workflow_tool_bridge.py', 'workflow_progression.py'})

    def test_unrelated_native_session_uses_original_handler(self):
        self.plugin._frozen_scope = lambda _: None
        self.plugin.api_attribution_details = lambda _: None
        import tools.file_tools as native
        with mock.patch.object(native, 'write_file_tool', return_value='{"native": true}') as handler:
            self.assertEqual(self.call('write_file', {'path': '/unrelated', 'content': 'ok'}), {'native': True})
            handler.assert_called_once()

    def test_inprocess_lint_and_crlf_are_preserved(self):
        (self.workspace / 'app.py').write_bytes(b'VALUE = 0\r\n')
        backend = edits.RemoteOperations(self.scope, ['app.py'])
        result = backend.patch_replace(str(self.workspace / 'app.py'), 'VALUE = 0', 'VALUE = 1')
        self.assertTrue(result.success, result)
        self.assertEqual(result.lint.get('status'), 'ok', result.lint)
        self.assertEqual((self.workspace / 'app.py').read_bytes(), b'VALUE = 1\r\n')


if __name__ == '__main__':
    unittest.main()
