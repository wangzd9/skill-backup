#!/usr/bin/env python3
"""Read-only identity checks using isolated scheduler-shaped process trees."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import role_runtime as runtime


class WorkerIdentityProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cli = str(Path(runtime.__file__).resolve())
        package = self.root / 'cron'
        package.mkdir()
        (package / '__init__.py').write_text('')
        driver = '''import json, os, shlex, subprocess, sys, time
if sys.argv[1] == 'sleep':
    print(os.getpid(), flush=True)
    time.sleep(30)
else:
    cli = sys.argv[-1]
    start = open('/proc/self/stat').read().rsplit(') ', 1)[1].split()[19]
    command = shlex.join([sys.executable, cli, 'worker-identity'])
    # Keep bash alive around the CLI; its inline text contains a decoy module.
    wrapper = '# python -m cron.scheduler --token PRIVATE-SENTINEL\\n' + command + '; result=$?; exit "$result"'
    run = subprocess.run(['/bin/bash', '-c', wrapper], capture_output=True, text=True, timeout=15)
    print(json.dumps({'worker_pid': os.getpid(), 'worker_start': start,
                      'returncode': run.returncode, 'stdout': run.stdout, 'stderr': run.stderr}))
'''
        (package / 'scheduler.py').write_text(driver)
        (package / 'scheduler_extra.py').write_text(driver)
        (self.root / 'driver.py').write_text(driver)
        (self.root / 'hermes_cli.py').write_text(driver)

    def run_driver(self, argv):
        run = subprocess.run([sys.executable, *argv, 'run', self.cli], cwd=self.root,
                             capture_output=True, text=True, timeout=20)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)

    def assert_unproved(self, result):
        self.assertEqual(result['returncode'], 2, result)
        self.assertFalse(result['stdout'])
        error = json.loads(result['stderr'])
        self.assertIn('无法证明', error['error'])
        self.assertNotIn('PRIVATE-SENTINEL', result['stderr'])

    def test_exact_scheduler_ancestor_skips_inline_shell_decoy(self):
        before = set(self.root.rglob('*'))
        result = self.run_driver(['-m', 'cron.scheduler'])
        self.assertEqual(result['returncode'], 0, result)
        identity = json.loads(result['stdout'])
        self.assertEqual(identity['pid'], result['worker_pid'])
        self.assertEqual(identity['process_start'], result['worker_start'])
        self.assertEqual(identity['source'], 'current_process_ancestor_chain')
        self.assertTrue(identity['verified'])
        self.assertEqual(set(identity), {'pid', 'process_start', 'source', 'verified'})
        self.assertNotIn('PRIVATE-SENTINEL', result['stdout'])
        added = set(self.root.rglob('*')) - before
        self.assertTrue(all('__pycache__' in str(path) for path in added), added)

    def test_script_argument_containing_module_name_is_not_a_worker(self):
        self.assert_unproved(self.run_driver(['driver.py', 'cron.scheduler']))

    def test_similarly_named_module_is_not_a_worker(self):
        self.assert_unproved(self.run_driver(['-m', 'cron.scheduler_extra']))

    def test_gateway_module_cannot_be_selected(self):
        self.assert_unproved(self.run_driver(['-m', 'hermes_cli']))

    def test_foreign_scheduler_is_not_scanned_or_borrowed(self):
        worker = subprocess.Popen([sys.executable, '-m', 'cron.scheduler', 'sleep'],
                                  cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True)
        self.addCleanup(lambda: worker.poll() is None and (worker.terminate(), worker.wait()))
        self.addCleanup(worker.stdout.close)
        self.addCleanup(worker.stderr.close)
        self.assertEqual(int(worker.stdout.readline()), worker.pid)
        result = self.run_driver(['driver.py'])
        self.assert_unproved(result)
        self.assertIsNone(worker.poll())

    def test_caller_cannot_supply_another_pid(self):
        run = subprocess.run([sys.executable, self.cli, 'worker-identity', '--pid', str(os.getpid())],
                             cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 2)
        self.assertIn('--pid is not accepted', run.stderr)
        self.assertFalse(run.stdout)

    def test_execute_needs_no_project_binding(self):
        expected = {'pid': 42, 'process_start': '17', 'verified': True,
                    'source': 'current_process_ancestor_chain'}
        with mock.patch.object(runtime, 'worker_identity', return_value=expected):
            self.assertEqual(runtime.execute(argparse.Namespace(action='worker-identity')), expected)


class WorkerIdentityRaceTests(unittest.TestCase):
    def worker(self, **changes):
        return {'pid': 42, 'process_start': '17', 'parent_pid': 1, 'is_worker': True, **changes}

    def test_pid_reuse_between_confirmations_is_rejected(self):
        before = self.worker()
        after = self.worker(process_start='18')
        with mock.patch.object(runtime, '_calling_worker_chain', side_effect=[
                (before, [(10, '9', 42), (42, '17', 1)]),
                (after, [(10, '9', 42), (42, '18', 1)])]), \
             mock.patch.object(runtime, 'refuse_shared_gateway_pid'):
            with self.assertRaisesRegex(ValueError, 'PID/start'):
                runtime.worker_identity()

    def test_reparented_caller_is_not_accepted_on_old_chain(self):
        worker = self.worker()
        with mock.patch.object(runtime, '_calling_worker_chain', side_effect=[
                (worker, [(10, '9', 11), (11, '8', 42), (42, '17', 1)]),
                (worker, [(10, '9', 42), (42, '17', 1)])]), \
             mock.patch.object(runtime, 'refuse_shared_gateway_pid'):
            with self.assertRaisesRegex(ValueError, '父进程'):
                runtime.worker_identity()

    def test_unreadable_ancestor_fails_without_trying_an_unrelated_worker(self):
        with mock.patch.object(runtime, '_worker_process_snapshot', side_effect=ValueError('无法核实')) as read:
            with self.assertRaisesRegex(ValueError, '无法核实'):
                runtime.worker_identity()
        self.assertEqual(read.call_count, 1)

    def test_start_changes_during_single_process_read_are_rejected(self):
        stat = Path('/proc/self/stat').read_text()
        prefix, rest = stat.rsplit(') ', 1)
        fields = rest.split()
        fields[19] = str(int(fields[19]) + 1)
        changed = prefix + ') ' + ' '.join(fields)
        with mock.patch.object(Path, 'read_text', side_effect=[stat, changed]), \
             mock.patch.object(Path, 'read_bytes', return_value=b'python3\0-m\0cron.scheduler\0'), \
             mock.patch.object(runtime.os, 'readlink', return_value='/usr/bin/python3.10'):
            with self.assertRaisesRegex(ValueError, 'PID/start'):
                runtime._worker_process_snapshot(os.getpid())

    def test_shell_executable_cannot_impersonate_scheduler_argv(self):
        with mock.patch.object(Path, 'read_bytes', return_value=b'bash\0-m\0cron.scheduler\0'), \
             mock.patch.object(runtime.os, 'readlink', return_value='/usr/bin/bash'):
            self.assertFalse(runtime._worker_process_snapshot(os.getpid())['is_worker'])

    def test_shared_gateway_is_refused_even_if_scheduler_shaped(self):
        worker = self.worker()
        with mock.patch.object(runtime, '_calling_worker_chain', return_value=(worker, [(42, '17', 1)])), \
             mock.patch.object(runtime, 'gateway_pid', return_value=42):
            with self.assertRaisesRegex(ValueError, 'shared gateway PID'):
                runtime.worker_identity()


if __name__ == '__main__':
    unittest.main()
