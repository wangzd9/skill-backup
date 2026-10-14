"""Offline checks; no real Codex call, live authorization, project writes or network."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from strong_review import Ledger, parse_time, project_binding, run_review


class ReviewGuardTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix='review-auth-check-')
        self.root = Path(self.scratch.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.binding = {'project_root': str(self.project), 'control': 'ACTIVE',
                        'migration': {'status': 'ACTIVE'}, 'roles': {'COORDINATOR': {}}}
        self.save_binding()
        self.evidence = self.root / 'user-message.txt'
        self.evidence.write_text('Offline fixture: allow the selected project for 90 minutes.')
        self.clock = 10000.0
        self.ledger = Ledger(self.root / 'state', lambda: self.clock)

    def tearDown(self):
        self.scratch.cleanup()

    def save_binding(self):
        (self.project / 'ROLE-BINDINGS.json').write_text(json.dumps(self.binding))

    def grant(self, source='message-1', duration=5400, starts=None, tasks=()):
        start = self.clock if starts is None else starts
        end = None if duration is None else start + duration
        return self.ledger.grant(str(self.project), source, self.evidence, 'explicit offline fixture', start, end, tasks)

    def reserve(self, name='R1', **kwargs):
        return self.ledger.reserve(str(self.project), name, 'TASK-01', 'fixed-target', name, **kwargs)

    def test_read_only_status_does_not_create_files(self):
        self.assertEqual(self.ledger.status(str(self.project))['authorization'], 'MANUAL_ONLY')
        self.assertFalse((self.root / 'state').exists())

    def test_missing_authorization_blocks(self):
        with self.assertRaisesRegex(ValueError, 'NO_ACTIVE_AUTHORIZATION'):
            self.reserve()

    def test_custom_interval_and_no_implicit_renewal(self):
        original = self.grant()
        self.clock += 60
        duplicate = self.grant()
        self.assertEqual(original['id'], duplicate['id'])
        self.assertEqual(original['ends'], duplicate['ends'])
        self.clock = original['ends']
        with self.assertRaisesRegex(ValueError, 'EXPIRED'):
            self.reserve()

    def test_future_start_and_scoped_task(self):
        self.grant(starts=self.clock + 60, tasks=['TASK-02'])
        with self.assertRaisesRegex(ValueError, 'NOT_STARTED'):
            self.reserve()
        self.clock += 60
        with self.assertRaisesRegex(ValueError, 'TASK_OUTSIDE'):
            self.reserve()

    def test_revocation_before_launch_prevents_subprocess(self):
        self.grant()
        call = self.reserve()
        self.ledger.revoke(str(self.project), 'user revoked')
        started = []
        with self.assertRaisesRegex(ValueError, 'NO_ACTIVE'):
            self.ledger.launch(call['id'], lambda: started.append(True))
        self.assertEqual(started, [])

    def test_expiration_before_launch_prevents_subprocess(self):
        self.grant(duration=60)
        call = self.reserve()
        self.clock += 60
        with self.assertRaisesRegex(ValueError, 'EXPIRED'):
            self.ledger.launch(call['id'], lambda: self.fail('must not start'))

    def test_global_cap_includes_failed_attempts_and_new_authorizations(self):
        self.grant(duration=20000)
        for number in range(5):
            call = self.reserve(str(number))
            self.ledger.finish(call['id'], 'FAILED', 'offline failure')
        self.grant(source='message-2', duration=20000)
        with self.assertRaisesRegex(ValueError, 'ROLLING_HOUR_LIMIT'):
            self.reserve('sixth')
        other = self.root / 'other-project'
        other.mkdir()
        other_binding = {**self.binding, 'project_root': str(other)}
        (other / 'ROLE-BINDINGS.json').write_text(json.dumps(other_binding))
        self.ledger.grant(str(other), 'other-message', self.evidence, 'offline fixture', self.clock, self.clock + 20000)
        with self.assertRaisesRegex(ValueError, 'ROLLING_HOUR_LIMIT'):
            self.ledger.reserve(str(other), 'other', 'TASK-01', 'target', 'package')
        self.clock += 3600
        self.assertFalse(self.reserve('next-window')['reused'])

    def test_parallel_reservation_does_not_exceed_five(self):
        self.grant()
        barrier = threading.Barrier(10)

        def request(number):
            barrier.wait()
            try:
                call = self.reserve(str(number))
                self.ledger.finish(call['id'], 'FAILED', 'offline fixture')
                return True
            except ValueError as error:
                self.assertTrue('BUSY' in str(error) or 'LIMIT' in str(error))
                return False

        with ThreadPoolExecutor(max_workers=10) as pool:
            accepted = list(pool.map(request, range(10)))
        self.assertGreater(sum(accepted), 0)
        self.assertLessEqual(sum(accepted), 5)
        self.assertEqual(self.ledger.status(str(self.project))['used_last_60_minutes'], sum(accepted))

    def test_duplicate_failed_call_does_not_retry_without_explicit_reference(self):
        self.grant()
        first = self.reserve()
        self.ledger.finish(first['id'], 'FAILED', 'fixture')
        with self.assertRaisesRegex(ValueError, 'EXPLICIT_RETRY_OF'):
            self.reserve()
        retry = self.reserve(retry_of=first['id'])
        self.assertNotEqual(first['id'], retry['id'])
        self.assertEqual(self.ledger.status(str(self.project))['used_last_60_minutes'], 2)

    def test_global_inflight_and_recovery_checks_live_owner(self):
        self.grant()
        call = self.reserve()
        with self.assertRaisesRegex(ValueError, 'BUSY'):
            self.reserve('another')
        with self.assertRaisesRegex(ValueError, 'STILL_ALIVE'):
            self.ledger.recover(call['id'], self.evidence)

    def test_complete_and_unbound_projects_cannot_launch(self):
        self.grant(duration=None)
        self.binding['control'] = 'COMPLETE'
        self.save_binding()
        self.assertEqual(self.ledger.status(str(self.project))['authorization'], 'SCOPE_COMPLETE')
        with self.assertRaisesRegex(ValueError, 'PROJECT_NOT_RUNNING'):
            project_binding(self.project, for_run=True)
        self.binding['control'] = 'ACTIVE'
        self.binding['migration']['status'] = 'UNBOUND'
        self.save_binding()
        with self.assertRaisesRegex(ValueError, 'PROJECT_NOT_RUNNING'):
            project_binding(self.project, for_run=True)

    def test_repository_scope_is_enforced(self):
        self.grant()
        with self.assertRaisesRegex(ValueError, 'REPOSITORY_OUTSIDE'):
            self.reserve(repo=self.root / 'unauthorized')

    def test_arbitrary_deadline_uses_shanghai_timezone(self):
        self.assertEqual(parse_time('2030-10-13T18:30:00'), parse_time('2030-10-13T18:30:00+08:00'))
        self.assertNotEqual(parse_time('2030-10-13T18:30:00+08:00'), parse_time('2030-10-13T18:30:00+00:00'))

    def test_recovery_of_dead_owner_keeps_quota_and_requires_explicit_retry(self):
        self.grant()
        call = self.reserve()
        with self.ledger.transaction() as con:
            con.execute('UPDATE calls SET wrapper_pid=NULL,wrapper_start=NULL WHERE id=?', (call['id'],))
        recovered = self.ledger.recover(call['id'], self.evidence)
        self.assertFalse(recovered['quota_refunded'])
        self.assertEqual(self.ledger.status(str(self.project))['used_last_60_minutes'], 1)
        with self.assertRaisesRegex(ValueError, 'EXPLICIT_RETRY_OF'):
            self.reserve()

    def test_real_git_snapshot_with_fake_codex_and_idempotent_result(self):
        repo = self.project
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        subprocess.run(['git', '-C', str(repo), 'add', 'ROLE-BINDINGS.json'], check=True)
        subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(repo), '-c', 'user.name=fixture',
                        '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture'], check=True)
        target = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
        package = self.project / 'review.md'
        package.write_text('Review TASK-01, existing AC only. This is an offline fixture.')
        self.grant()
        binaries = self.root / 'bin'
        binaries.mkdir()
        stub = binaries / 'codex'
        stub.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
args=sys.argv[1:]
assert args[0]=='exec'
assert args[args.index('--sandbox')+1]=='read-only'
assert 'approval_policy="never"' in args
assert '--json' in args
prompt=sys.stdin.read()
assert 'CORRECTION' in prompt and 'grant_id' in prompt and 'fixed review fixture' in prompt
if os.environ.get('REVIEW_STUB_FAIL')=='1':
    print('offline simulated CLI failure')
    sys.exit(3)
pathlib.Path(args[args.index('--output-last-message')+1]).write_text('# R-offline\\nPASS\\nOnly the specified fixed fixture was inspected.\\n')
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':5}}))
''')
        stub.chmod(0o700)
        args = argparse.Namespace(project=str(self.project), repo=str(repo), package=str(package),
                                  review_id='R-offline', task_id='TASK-01', base=target, target=target,
                                  risk='fixed review fixture', question='Does existing behavior hold?',
                                  ac='AC-01', gate='module merge', retry_of=None, timeout_seconds=10)
        with patch.dict(os.environ, {'PATH': str(binaries) + os.pathsep + os.environ['PATH']}):
            self.assertEqual(shutil.which('codex'), str(stub))  # Prevent accidental paid calls.
            result = run_review(args, self.ledger)
            repeated = run_review(args, self.ledger)
            args.review_id = 'R-failure'
            with patch.dict(os.environ, {'REVIEW_STUB_FAIL': '1'}):
                with self.assertRaisesRegex(ValueError, 'REVIEW_FAILED'):
                    run_review(args, self.ledger)
            failed = self.ledger.status(str(self.project))['last_call']
            self.assertEqual(failed['status'], 'FAILED')
            self.assertEqual(self.ledger.status(str(self.project))['active_calls'], [])
            with self.assertRaisesRegex(ValueError, 'EXPLICIT_RETRY_OF'):
                run_review(args, self.ledger)
            self.assertEqual(self.ledger.status(str(self.project))['used_last_60_minutes'], 2)
        self.assertEqual(result['status'], 'SUCCEEDED')
        self.assertEqual(result['approval'], 'NOT_DECIDED')
        self.assertTrue(repeated['reused'])
        self.assertEqual(self.ledger.status(str(self.project))['used_last_60_minutes'], 2)
        self.assertTrue(Path(result['result_path']).is_file())
        self.assertFalse(Path(result['result_path']).parent.joinpath('snapshot').exists())
        self.assertEqual(subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip(), target)


if __name__ == '__main__':
    unittest.main()
