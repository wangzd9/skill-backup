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

import strong_review
from strong_review import Ledger, parse_time, project_binding, run_review


class ReviewGuardTests(unittest.TestCase):
    def setUp(self):
        self.original_start_service = strong_review.start_service
        self.original_manager_check = strong_review.check_user_manager
        self.original_model_resolver = strong_review.resolve_default_model
        self.model_patch = patch.object(strong_review, 'resolve_default_model', return_value='offline-fixture-model')
        self.model_patch.start()
        self.addCleanup(self.model_patch.stop)
        self.service_patch = patch.object(strong_review, 'start_service', side_effect=AssertionError('offline test must fake systemd dispatch'))
        self.service_patch.start()
        self.addCleanup(self.service_patch.stop)
        self.manager_patch = patch.object(strong_review, 'check_user_manager')
        self.manager_patch.start()
        self.addCleanup(self.manager_patch.stop)
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

    def test_same_service_call_inflight_is_reused_without_second_charge(self):
        self.grant()
        first = self.reserve()
        self.ledger.bind_service(first['id'], 'review-fixture.service', '/fixture/spec.json', 'frozen-hash')
        repeated = self.reserve()
        self.assertTrue(repeated['reused'])
        self.assertEqual(first['id'], repeated['id'])
        self.assertEqual(self.ledger.status(str(self.project))['used_last_60_minutes'], 1)

    def test_recovery_refuses_active_or_unverifiable_service_even_with_dead_pids(self):
        self.grant()
        call = self.reserve()
        self.ledger.bind_service(call['id'], 'review-fixture.service', '/fixture/spec.json', 'frozen-hash')
        with self.ledger.transaction() as con:
            con.execute('UPDATE calls SET wrapper_pid=NULL,wrapper_start=NULL WHERE id=?', (call['id'],))
        for service in ({'ActiveState': 'active'}, {'ActiveState': 'activating'}, {'error': 'unreachable'}):
            with self.subTest(service=service), patch.object(strong_review, 'service_info', return_value=service):
                with self.assertRaisesRegex(ValueError, 'SERVICE_STILL_ALIVE_OR_UNVERIFIABLE'):
                    self.ledger.recover(call['id'], self.evidence)

    def test_worker_rejects_direct_terminal_execution(self):
        with patch.object(strong_review, 'own_cgroup', return_value='/user.slice/hermes-worker-cron-fixture.scope'):
            with self.assertRaisesRegex(ValueError, 'NOT_IN_DEDICATED_SERVICE'):
                strong_review.require_service('review-fixture.service')

    def frozen_fixture(self, codex='/never-execute-offline-fixture', timeout=10):
        subprocess.run(['git', 'init', '-q', str(self.project)], check=True)
        subprocess.run(['git', '-C', str(self.project), 'add', 'ROLE-BINDINGS.json'], check=True)
        subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(self.project),
                        '-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid',
                        'commit', '-qm', 'fixture'], check=True)
        target = subprocess.check_output(['git', '-C', str(self.project), 'rev-parse', 'HEAD'], text=True).strip()
        package = self.project / 'review.md'
        package.write_text('Frozen offline review fixture; no network.')
        self.grant()
        args = argparse.Namespace(project=str(self.project), repo=str(self.project), package=str(package),
                                  review_id='R-frozen', task_id='TASK-01', base=target, target=target,
                                  risk='fixture risk', question='Does existing AC hold?', ac='AC-01', gate='merge',
                                  retry_of=None, timeout_seconds=timeout)
        with patch.object(strong_review.shutil, 'which', return_value=str(codex)), \
                patch.object(strong_review, 'start_service') as service:
            call = run_review(args, self.ledger)
        self.assertEqual(service.call_count, 1)
        self.assertEqual(call['status'], 'RESERVED')
        return args, call

    def test_worker_timeout_stops_fake_child_and_keeps_quota(self):
        stub = self.root / 'fake-codex'
        stub.write_text('#!/usr/bin/python3\nimport sys,time\nsys.stdin.read()\ntime.sleep(5)\n')
        stub.chmod(0o700)
        _, call = self.frozen_fixture(stub, timeout=1)
        with patch.object(strong_review, 'own_cgroup', return_value='/fixture/' + call['unit']):
            with self.assertRaisesRegex(ValueError, 'REVIEW_TIMEOUT'):
                strong_review.worker(Path(call['spec_path']).parent, self.ledger)
        current = self.ledger.call(call['id'])
        self.assertEqual(current['status'], 'FAILED')
        self.assertFalse(strong_review.alive(current['child_pid'], current['child_start']))
        self.assertEqual(self.ledger.status(str(self.project))['used_last_60_minutes'], 1)

    def test_short_submit_freezes_original_input_and_inflight_is_not_resubmitted(self):
        args, call = self.frozen_fixture()
        frozen = Path(call['spec_path']).parent
        self.assertTrue((frozen / 'worker.py').is_file())
        self.assertEqual((frozen / 'spec.json').stat().st_mode & 0o777, 0o600)
        self.assertFalse((frozen / 'snapshot').exists())
        original = (frozen / 'input.md').read_bytes()
        with patch.object(strong_review, 'start_service') as launch:
            repeated = run_review(args, self.ledger)
        self.assertEqual(repeated['id'], call['id'])
        self.assertTrue(repeated['reused'])
        self.assertEqual(launch.call_count, 0)
        Path(args.package).write_text('Changed external package after submit')
        self.assertEqual((frozen / 'input.md').read_bytes(), original)
        self.assertEqual((frozen / 'package.md').read_text(), 'Frozen offline review fixture; no network.')

    def test_reuse_requires_every_frozen_request_field_to_match(self):
        args, call = self.frozen_fixture()
        subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(self.project),
                        '-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid',
                        'commit', '--allow-empty', '-qm', 'second fixture base'], check=True)
        other_base = subprocess.check_output(['git', '-C', str(self.project), 'rev-parse', 'HEAD'], text=True).strip()
        changes = {'task_id': 'TASK-02', 'base': other_base, 'timeout_seconds': 11,
                   'risk': 'different risk', 'question': 'A different review question?',
                   'ac': 'AC-02', 'gate': 'different merge gate'}
        for status in ('RESERVED', 'SUCCEEDED'):
            if status == 'SUCCEEDED':
                Path(call['result_path']).write_text('Offline result, not a real review.')
                self.ledger.finish(call['id'], status, 'fixture')
            for field, value in changes.items():
                changed = argparse.Namespace(**{**vars(args), field: value})
                with self.subTest(status=status, field=field), patch.object(strong_review, 'start_service') as launch:
                    with self.assertRaisesRegex(ValueError, 'REVIEW_INPUT_IDENTITY_CONFLICT'):
                        run_review(changed, self.ledger)
                    self.assertEqual(launch.call_count, 0)
            self.assertEqual(self.ledger.status(str(self.project))['used_last_60_minutes'], 1)

    def test_changed_authorized_model_or_repo_cannot_reuse_another_request(self):
        args, _ = self.frozen_fixture()
        self.ledger.grant(str(self.project), 'model-change', self.evidence, 'explicit fixture',
                          self.clock, self.clock + 5400, model='different-fixture-model')
        with self.assertRaisesRegex(ValueError, 'REVIEW_INPUT_IDENTITY_CONFLICT'):
            run_review(args, self.ledger)
        repo = self.root / 'alternate-repo'
        subprocess.run(['git', 'clone', '-q', '--local', str(self.project), str(repo)], check=True)
        self.ledger.grant(str(self.project), 'repo-change', self.evidence, 'explicit fixture',
                          self.clock, self.clock + 5400, repo=str(repo))
        changed = argparse.Namespace(**{**vars(args), 'repo': str(repo)})
        with self.assertRaisesRegex(ValueError, 'REVIEW_INPUT_IDENTITY_CONFLICT'):
            run_review(changed, self.ledger)
        self.assertEqual(self.ledger.status(str(self.project))['used_last_60_minutes'], 1)

    def test_default_model_is_frozen_and_later_default_changes_conflict(self):
        args, call = self.frozen_fixture()
        spec = json.loads(Path(call['spec_path']).read_text())
        self.assertEqual(spec['model'], 'offline-fixture-model')
        self.assertEqual(spec['request_hash'], call['request_hash'])
        with patch.object(strong_review, 'resolve_default_model', return_value='changed-default-model'):
            with self.assertRaisesRegex(ValueError, 'REVIEW_INPUT_IDENTITY_CONFLICT'):
                run_review(args, self.ledger)
        self.assertEqual(self.ledger.status(str(self.project))['used_last_60_minutes'], 1)

    def test_explicit_authorized_model_does_not_read_default_config(self):
        self.ledger.grant(str(self.project), 'explicit-model', self.evidence, 'explicit fixture',
                          self.clock, self.clock + 5400, model='authorized-fixture-model')
        with patch.object(strong_review, 'resolve_default_model', side_effect=AssertionError('must use authorized model')):
            call = self.reserve()
        self.assertEqual(call['model'], 'authorized-fixture-model')

    def test_unresolved_default_model_does_not_reserve_or_launch(self):
        self.grant()
        with patch.object(strong_review, 'resolve_default_model',
                          side_effect=ValueError('CODEX_DEFAULT_MODEL_UNDETERMINED')):
            with self.assertRaisesRegex(ValueError, 'CODEX_DEFAULT_MODEL_UNDETERMINED'):
                self.reserve()
        status = self.ledger.status(str(self.project))
        self.assertEqual(status['used_last_60_minutes'], 0)
        self.assertEqual(status['active_calls'], [])

    def test_default_model_config_and_profiles_are_read_without_sensitive_output(self):
        codex_home = self.root / 'codex-config-fixture'
        codex_home.mkdir()
        config = codex_home / 'config.toml'
        cases = [
            ('model = "base-fixture"\nsecret = "do-not-return-this"\n', 'base-fixture'),
            ('model = "base-fixture"\nprofile = "review"\n[profiles.review]\nmodel = "profile-fixture"\n', 'profile-fixture'),
            ('model = "base-fixture"\nprofile = "overlay"\n', 'overlay-fixture')]
        (codex_home / 'overlay.config.toml').write_text('model = "overlay-fixture"\nsecret = "do-not-return-this"\n')
        settings = strong_review.model_settings
        def fixture_settings(path):
            # The fixture owns only this synthetic configuration hierarchy.
            # Host ancestor files must not affect a test based on TMPDIR.
            return settings(path) if Path(path).is_relative_to(self.root) else {}
        with patch.dict(os.environ, {'CODEX_HOME': str(codex_home)}), patch.object(strong_review, 'git', return_value=''), \
                patch.object(strong_review,'model_settings',side_effect=fixture_settings):
            for body, expected in cases:
                with self.subTest(expected=expected):
                    config.write_text(body)
                    self.assertEqual(self.original_model_resolver(self.project, 'fixture-target', self.project), expected)
            for body in ('', 'profile = "missing"\n', 'model = 42\n', 'model = "broken'):
                with self.subTest(invalid=body):
                    config.write_text(body)
                    with self.assertRaisesRegex(ValueError, 'CODEX_DEFAULT_MODEL') as caught:
                        self.original_model_resolver(self.project, 'fixture-target', self.project)
                    self.assertNotIn('do-not-return-this', str(caught.exception))

    def test_ambiguous_project_default_model_is_not_guessed(self):
        codex_home = self.root / 'codex-config-fixture'
        codex_home.mkdir()
        (codex_home / 'config.toml').write_text('model = "base-fixture"\n')
        project_config = self.project / '.codex/config.toml'
        project_config.parent.mkdir()
        project_config.write_text('model = "project-model"\n')
        settings = strong_review.model_settings
        def fixture_settings(path):
            return settings(path) if Path(path).is_relative_to(self.root) else {}
        with patch.dict(os.environ, {'CODEX_HOME': str(codex_home)}), patch.object(strong_review, 'git', return_value=''), \
                patch.object(strong_review,'model_settings',side_effect=fixture_settings):
            with self.assertRaisesRegex(ValueError, 'CODEX_DEFAULT_MODEL_SCOPE_AMBIGUOUS'):
                self.original_model_resolver(self.project, 'fixture-target', self.project)

    def test_fixture_ancestor_model_override_remains_ambiguous(self):
        codex_home=self.root/'explicit-home'; codex_home.mkdir()
        (codex_home/'config.toml').write_text('model="base"\n')
        ancestor=self.root/'.codex/config.toml'; ancestor.parent.mkdir()
        ancestor.write_text('model="ancestor"\n')
        settings=strong_review.model_settings
        with patch.dict(os.environ,{'CODEX_HOME':str(codex_home)}), patch.object(strong_review,'git',return_value=''), \
                patch.object(strong_review,'model_settings',side_effect=lambda p:settings(p) if Path(p).is_relative_to(self.root) else {}):
            with self.assertRaisesRegex(ValueError,'SCOPE_AMBIGUOUS'):
                self.original_model_resolver(self.project,'fixture',self.project)

    def test_legacy_call_without_complete_identity_is_not_silently_reused(self):
        args, call = self.frozen_fixture()
        Path(call['result_path']).write_text('Legacy offline result.')
        self.ledger.finish(call['id'], 'SUCCEEDED', 'fixture')
        with self.ledger.transaction() as con:
            con.execute('UPDATE calls SET request_hash=NULL WHERE id=?', (call['id'],))
        with patch.object(strong_review, 'start_service') as launch:
            with self.assertRaisesRegex(ValueError, 'LEGACY_REQUEST_IDENTITY_UNVERIFIABLE'):
                run_review(args, self.ledger)
        self.assertEqual(launch.call_count, 0)
        self.assertEqual(self.ledger.status(str(self.project))['used_last_60_minutes'], 1)

    def test_worker_rechecks_authorization_before_any_codex_request(self):
        _, call = self.frozen_fixture()
        self.ledger.revoke(str(self.project), 'fixture revoked after submission')
        with patch.object(strong_review, 'own_cgroup', return_value='/fixture/' + call['unit']), \
                patch.object(strong_review.subprocess, 'Popen', wraps=subprocess.Popen) as request:
            with self.assertRaisesRegex(ValueError, 'NO_ACTIVE_AUTHORIZATION'):
                strong_review.worker(Path(call['spec_path']).parent, self.ledger)
        self.assertFalse(any(item.args[0][0] == '/never-execute-offline-fixture' for item in request.call_args_list))
        current = self.ledger.call(call['id'])
        self.assertEqual(current['status'], 'FAILED')
        self.assertIsNotNone(current['wrapper_cgroup'])
        self.assertEqual(self.ledger.status(str(self.project))['used_last_60_minutes'], 1)

    def test_tampered_frozen_prompt_is_not_sent_to_codex(self):
        _, call = self.frozen_fixture()
        directory = Path(call['spec_path']).parent
        (directory / 'input.md').write_text('tampered')
        with patch.object(strong_review, 'own_cgroup', return_value='/fixture/' + call['unit']), \
                patch.object(strong_review.subprocess, 'Popen') as request:
            with self.assertRaisesRegex(ValueError, 'FROZEN_INPUT_CHANGED'):
                strong_review.worker(directory, self.ledger)
        self.assertEqual(request.call_count, 0)
        self.assertEqual(self.ledger.call(call['id'])['status'], 'FAILED')

    def test_submission_timeout_remains_unresolved_without_automatic_retry(self):
        args, original = self.frozen_fixture()
        self.ledger.finish(original['id'], 'FAILED', 'fixture completed setup')
        args.retry_of = original['id']
        with patch.object(strong_review.shutil, 'which', return_value='/offline-fixture'), \
                patch.object(strong_review, 'start_service', side_effect=subprocess.TimeoutExpired('systemd-run', 20)), \
                patch.object(strong_review, 'service_info', return_value={'error': 'unreachable'}):
            with self.assertRaises(subprocess.TimeoutExpired):
                run_review(args, self.ledger)
        current = self.ledger.status(str(self.project))['last_call']
        self.assertEqual(current['status'], 'RESERVED')
        with patch.object(strong_review, 'start_service') as launch:
            repeated = run_review(args, self.ledger)
        self.assertTrue(repeated['reused'])
        self.assertEqual(launch.call_count, 0)
        self.assertEqual(self.ledger.status(str(self.project))['used_last_60_minutes'], 2)

    def test_manager_preflight_fails_without_exposing_diagnostics(self):
        failures = [FileNotFoundError('private path'), PermissionError('private path'),
                    subprocess.TimeoutExpired('systemctl', 5),
                    subprocess.CompletedProcess(['systemctl'], 1, 'private output', 'private error')]
        for failure in failures:
            settings = {'side_effect': failure} if isinstance(failure, Exception) else {'return_value': failure}
            with self.subTest(failure=type(failure).__name__), \
                    patch.object(strong_review.subprocess, 'run', **settings):
                with self.assertRaisesRegex(ValueError, '^USER_SYSTEMD_UNAVAILABLE_BEFORE_SUBMISSION$'):
                    self.original_manager_check()

    def test_unavailable_manager_before_submission_releases_active_slot_but_retains_quota(self):
        args, original = self.frozen_fixture()
        self.ledger.finish(original['id'], 'FAILED', 'fixture completed setup')
        args.retry_of = original['id']
        with patch.object(strong_review.shutil, 'which', return_value='/offline-fixture'), \
                patch.object(strong_review, 'check_user_manager',
                             side_effect=ValueError('USER_SYSTEMD_UNAVAILABLE_BEFORE_SUBMISSION')), \
                patch.object(strong_review, 'start_service') as launch:
            with self.assertRaisesRegex(ValueError, 'USER_SYSTEMD_UNAVAILABLE_BEFORE_SUBMISSION'):
                run_review(args, self.ledger)
        current = self.ledger.call(self.ledger.status(str(self.project))['last_call']['id'])
        self.assertEqual(current['status'], 'FAILED')
        self.assertIsNone(current['unit'])
        self.assertEqual(launch.call_count, 0)
        self.assertEqual(self.ledger.status(str(self.project))['active_calls'], [])
        self.assertEqual(self.ledger.status(str(self.project))['used_last_60_minutes'], 2)

    def test_launcher_exec_failure_is_failed_even_when_manager_is_unavailable(self):
        args, original = self.frozen_fixture()
        self.ledger.finish(original['id'], 'FAILED', 'fixture completed setup')
        args.retry_of = original['id']
        real_run = subprocess.run
        for exception in (FileNotFoundError('systemd-run absent'), PermissionError('cannot execute systemd-run')):
            def fail_only_launcher(command, *positional, **kwargs):
                if command[0] == 'systemd-run':
                    raise exception
                return real_run(command, *positional, **kwargs)
            with self.subTest(exception=type(exception).__name__), \
                    patch.object(strong_review.shutil, 'which', return_value='/offline-fixture'), \
                    patch.object(strong_review, 'start_service', self.original_start_service), \
                    patch.object(strong_review.subprocess, 'run', side_effect=fail_only_launcher), \
                    patch.object(strong_review, 'service_info', return_value={'error': 'unreachable'}):
                with self.assertRaises((OSError, ValueError)):
                    run_review(args, self.ledger)
                current = self.ledger.call(self.ledger.status(str(self.project))['last_call']['id'])
                self.assertEqual(current['status'], 'FAILED')
                self.assertEqual(self.ledger.status(str(self.project))['active_calls'], [])
                args.retry_of = current['id']
        self.assertEqual(self.ledger.status(str(self.project))['used_last_60_minutes'], 3)

    def test_failed_submission_cannot_overwrite_an_adopted_worker(self):
        _, call = self.frozen_fixture()
        with patch.object(strong_review, 'own_cgroup', return_value='/fixture/' + call['unit']):
            self.ledger.adopt_worker(call['id'], call['spec_hash'])
        self.ledger.fail_unsubmitted(call['id'], 'late launcher failure')
        current = self.ledger.call(call['id'])
        self.assertEqual(current['status'], 'RESERVED')
        self.assertIsNotNone(current['wrapper_cgroup'])
        self.assertIsNone(current['finished'])

    def test_old_ledger_status_remains_read_only_until_next_mutation(self):
        self.grant()
        with self.ledger.transaction() as con:
            for name in ('unit', 'spec_path', 'spec_hash', 'wrapper_cgroup', 'request_hash'):
                con.execute('ALTER TABLE calls DROP COLUMN ' + name)
        self.assertEqual(self.ledger.status(str(self.project))['used_last_60_minutes'], 0)
        with self.ledger.transaction() as con:
            self.assertIn('unit', {row['name'] for row in con.execute('PRAGMA table_info(calls)')})

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
assert args[args.index('--model')+1]=='offline-fixture-model'
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
        def fake_service(spec):
            with patch.object(strong_review, 'own_cgroup', return_value='/fixture/' + spec['unit']):
                strong_review.worker(spec['run_directory'], self.ledger)

        with patch.dict(os.environ, {'PATH': str(binaries) + os.pathsep + os.environ['PATH']}), \
                patch.object(strong_review, 'start_service', side_effect=fake_service):
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
