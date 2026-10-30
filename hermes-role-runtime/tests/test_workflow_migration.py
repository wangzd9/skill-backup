"""Migration rehearsal against real temporary SQLite/Git/PID boundaries.

No production home, model, provider or group transport is used. Acceptance
receipts below explicitly describe fixture evidence; they exercise the gate
contract and never certify controlled real model acceptance for deployment.
"""
from datetime import datetime
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from business_ledger import BusinessLedger, _process
import workflow_migration as migration


class MigrationTests(unittest.TestCase):
    def test_version_routing_semantics_ignores_secrets_but_fences_policy_changes(self):
        config = self.home/'routing.yaml'
        config.write_text('api_key: never-export-this\nplugins:\n  entries:\n    role-workflow-runtime:\n      settings:\n        model_routing:\n          auto_cloud_profile: sub2api\n          cloud_profiles:\n            sub2api:\n              cloud_model: deepseek-v4.1-flash\n              cloud_key_env: SUB2API_API_KEY\n              api_key: another-private-value\n')
        sources = [self.version_file, {'path': str(config), 'kind': 'routing_semantics'}]
        original = migration.code_version(sources)
        config.write_text(config.read_text().replace('never-export-this', 'secret-rotated').replace('another-private-value', 'changed-secret'))
        self.assertEqual(migration.code_version(original['files']), original)
        self.assertNotIn('private', json.dumps(original))
        config.write_text(config.read_text().replace('deepseek-v4.1-flash', 'different-policy-model'))
        self.assertNotEqual(migration.code_version(original['files'])['digest'], original['digest'])

    def test_navigation_symlink_keeps_alias_canonical_identity_without_business_authority(self):
        target = self.home/'shared.md'; target.write_text('navigation only\n')
        first, second = self.root/'REFERENCES.md', self.root/'RAW-DATA-REQUIREMENTS.md'
        first.symlink_to(target); second.symlink_to(target)
        json_target = self.home/'references.json'; json_target.write_text('{"links": []}')
        references = self.root/'SPRAY-REFERENCES.json'; references.symlink_to(json_target)
        manifest = self.inspect()
        self.assertEqual(manifest['errors'], [])
        values = {row['path']: row for row in manifest['sources']}
        self.assertEqual(values[str(first)]['canonical_path'], str(target))
        self.assertEqual(values[str(second)]['canonical_path'], str(target))
        self.assertFalse(values[str(first)]['authority'])
        self.assertFalse(values[str(references)]['authority'])
        self.assertFalse(manifest['records'])
        target.write_text('changed navigation target\n')
        changed, _ = migration._sources_unchanged(manifest)
        self.assertIn(str(first), changed)
        self.assertIn(str(second), changed)
        (self.root/'TODO.md').symlink_to(target)
        (self.root/'OUTSIDE.md').symlink_to('/etc/hostname')
        result = self.inspect()
        self.assertTrue(any(row['source'].endswith('TODO.md') for row in result['errors']))
        self.assertTrue(any(row['source'].endswith('OUTSIDE.md') for row in result['errors']))

    def setUp(self):
        # These fixture manifests hash one fixture file. Production validation
        # requires full runtime coverage; no CLI bypass exists for this mock.
        self.real_version_coverage = migration._version_coverage
        coverage = patch.object(migration, '_version_coverage', return_value=[])
        coverage.start(); self.coverage_patch = coverage; self.addCleanup(coverage.stop)
        self.temp = tempfile.TemporaryDirectory(prefix='workflow-migration-fixture-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)/'home'; self.home.mkdir()
        self.root = self.home/'requirements/one'; self.root.mkdir(parents=True)
        self.repo = self.home/'repo'; self.repo.mkdir()
        self.git('init', '-q'); self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        (self.repo/'a.py').write_text('value = 1\n')
        self.git('add', '.'); self.git('commit', '-qm', 'base')
        self.base = self.git('rev-parse', 'HEAD')
        self.pool = self.home/'role-pool.json'
        self.now = datetime(2026, 10, 19, 12, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        roles = {role: {'skill': 'fixture', 'poll_job_id': role+'-poll', 'report_job_id': role+'-report',
                        'chat_id': 'chat-'+role} for role in migration.ROLES}
        self.write(self.pool, {'schema_version': 1, 'generation': 3, 'control': 'ACTIVE', 'roles': roles,
            'current': {'requirement_id': 'one', 'runtime_root': str(self.root),
                        'role_roots': {role: str(self.root) for role in roles}}})
        self.write(self.root/'ROLE-BINDINGS.json', {'project_root': str(self.root),
            'schema_version': 1, 'control': 'ACTIVE', 'roles': roles,
            'role_pool': {'path': str(self.pool), 'generation': 3}})
        self.jobs = [{'id': cfg[field], 'enabled': role != 'B', 'state': 'paused' if role == 'B' else 'active',
            'origin': {'chat_id': cfg['chat_id']}, 'paused': role == 'B'}
            for role, cfg in roles.items() for field in ('poll_job_id', 'report_job_id')]
        self.write(self.home/'cron/jobs.json', {'jobs': self.jobs})
        with sqlite3.connect(self.home/'cron/executions.db') as db:
            db.execute('CREATE TABLE executions (id TEXT PRIMARY KEY,job_id TEXT,pid INTEGER,process_started_at INTEGER,status TEXT)')
        self.version_file = self.home/'fixture-runtime.py'; self.version_file.write_text('# frozen fixture code\n')
        self.version_paths = [self.version_file]
        self.proposal = {'run_id': 'run-one', 'requirement_id': 'one', 'requirement_root': str(self.root),
                         'repo': str(self.repo), 'task_ids': ['TASK-01']}

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True,
                                       stderr=subprocess.DEVNULL).strip()

    def write(self, path, value):
        path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False))
        return path

    def inspect(self):
        return migration.inspect(self.home, self.pool, version_paths=self.version_paths, at=self.now)

    def staged(self, records=True):
        if records:
            self.write(self.root/'events/A/unknown.json', {'role': 'A', 'task': 'TASK-01',
                'attempt': 'attempt-1', 'status': 'DONE', 'event_id': 'old-event'})
        manifest = self.inspect()
        prepared = migration.prepare(manifest, self.proposal)
        self.assertEqual(prepared['result']['status'], 'COMMITTED', prepared)
        ledger = BusinessLedger(migration.ledger_path(self.pool), clock=lambda: self.now)
        return manifest, ledger

    def plan(self, ledger):
        result = ledger.publish_plan('run-one', 1, [{'task_id': 'TASK-01', 'role': 'A', 'dependencies': [],
            'scope': ['a.py'], 'base': self.base, 'ac_version': 'fixed-ac', 'required_checks': ['independent_test']}],
            command_id='fixture-published-plan')
        self.assertEqual(result['status'], 'COMMITTED', result)
        self.assertEqual(ledger.select_run('run-one', command_id='fixture-select')['status'], 'COMMITTED')

    def evidence(self, manifest, *, omit=(), schema=2):
        paths = []
        for gate in migration.GATES:
            if gate in omit:
                continue
            artifact = self.home/'evidence'/(gate+'-fixture-output.txt')
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text('Offline test fixture gate contract only; no deployment acceptance.\n')
            receipt = {'schema_version': 1, 'gate': gate, 'result': 'PASS',
                'version_digest': manifest['version']['digest'], 'artifacts': [
                    {'path': str(artifact), 'sha256': migration.file_sha(artifact)}]}
            if gate == 'P0':
                receipt.update(observation_schema=schema, real_execution_observed=True, observed_at=self.now)
            if gate == 'M':
                receipt.update(real_model=True, real_channel=True, controlled_scope=True)
            if gate == 'T1':
                receipt['real_business_lifecycle'] = True
            if gate == 'T2':
                receipt['real_systemd'] = True
            if gate == 'R':
                receipt.update(inventory_manifest_sha256=manifest['sha256'], imports_reconciled=True)
            paths.append(self.write(self.home/'evidence'/(gate+'.json'), receipt))
        return paths

    def contract(self):
        artifact = self.home/'legacy-adapter.py'; artifact.write_text('# verified fixture adapter\n')
        return migration.sealed({'schema_version': 1, 'kind': 'verified-legacy-adapter', 'tested': True,
            'capabilities': sorted(migration.LEGACY_CAPABILITIES),
            'artifacts': [{'path': str(artifact), 'sha256': migration.file_sha(artifact)}]})

    def worker(self, ledger, role='A'):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
        def cleanup():
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL); child.wait(timeout=5)
        self.addCleanup(cleanup)
        observed = _process(child.pid)
        native = {'execution_id': uuid.uuid4().hex, 'pid': child.pid, 'process_start': observed['process_start'],
                  'scope': observed['cgroup'], 'generation': ledger.mode()['generation']}
        result = ledger.bind_execution(role, native, command_id='fixture-bind-'+native['execution_id'])
        self.assertEqual(result['status'], 'COMMITTED', result)
        return child, result['identity']

    def test_inventory_is_readonly_and_preserves_exact_missing_null_numeric_identity(self):
        for index, raw in enumerate(({'task': 'TASK-01'}, {'task': 'TASK-01', 'attempt': None},
                                      {'task': 'TASK-01', 'attempt': 0}, {'task': 'TASK-01', 'attempt': ''})):
            self.write(self.root/f'events/A/{index}.json', raw)
        before = {str(path): path.stat().st_mtime_ns for path in self.home.rglob('*') if path.is_file()}
        manifest = self.inspect()
        self.assertEqual(len({migration.sha(row['identity']) for row in manifest['records']}), 4)
        after = {str(path): path.stat().st_mtime_ns for path in self.home.rglob('*') if path.is_file()}
        self.assertEqual(before, after)
        self.assertFalse(migration.ledger_path(self.pool).exists())
        self.assertTrue(any(job['id'] == 'B-poll' and job['paused'] for job in manifest['jobs']))

    def test_inventory_records_real_active_and_unknown_lease_owners(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.addCleanup(lambda: child.poll() is None and (child.terminate(), child.wait()))
        self.write(self.root/'inbox/control/runtime/leases/A.json', {'owner': 'old', 'pid': child.pid,
            'process_start': _process(child.pid)['process_start'], 'token': 'never-restored', 'expires_at': 0})
        self.write(self.root/'inbox/control/runtime/leases/B.json', {'owner': 'unbound', 'expires_at': 0})
        manifest = self.inspect()
        self.assertEqual({writer['state'] for writer in manifest['writers']}, {'alive', 'unknown'})
        self.assertNotIn('never-restored', json.dumps(manifest))

    def test_prepare_only_stages_lossless_proposals_and_never_imports_done_as_pass(self):
        manifest, ledger = self.staged()
        state = ledger.export_state()['state']
        self.assertEqual(state['mode'], 'STAGED')
        self.assertEqual(state['admission_open'], 0)
        self.assertEqual(state['tasks'], [])
        self.assertEqual(state['verifications'], [])
        proposal = state['imports'][0]['manifest']['proposals'][0]
        self.assertEqual(proposal['raw_source']['status'], 'DONE')
        self.assertEqual(proposal['identity_fields']['attempt']['value'], 'attempt-1')
        duplicate = migration.prepare(manifest, self.proposal)
        self.assertEqual(duplicate['result']['status'], 'DUPLICATE')
        self.assertEqual(len(ledger.snapshot()['imports']), 1)

    def test_missing_m_and_real_schema2_gate_refuse_activation(self):
        manifest, ledger = self.staged(); self.plan(ledger)
        result = migration.activate(manifest, self.evidence(manifest, omit=('M',)), at=self.now)
        self.assertEqual(result['status'], 'REJECTED')
        self.assertIn('MISSING_GATE:M', result['readiness']['problems'])
        self.assertEqual(ledger.mode()['mode'], 'STAGED')
        result = migration.activate(manifest, self.evidence(manifest, schema=1), at=self.now)
        self.assertEqual(result['status'], 'REJECTED')
        self.assertTrue(any('schema-2' in value for value in result['readiness']['problems']))

    def test_boolean_pass_and_hashed_text_cannot_masquerade_as_acceptance(self):
        manifest, ledger = self.staged(); self.plan(ledger)
        result = migration.activate(manifest, self.evidence(manifest), at=self.now)
        self.assertEqual(result['status'], 'REJECTED')
        self.assertTrue(any('STRUCTURED_AUTHORITY_PROOF_REQUIRED' in value for value in result['readiness']['problems']))
        self.assertEqual(ledger.mode()['mode'], 'STAGED')

    def fixture_proofs(self):
        # Isolate activation's crash/locking algorithm from real acceptance.
        # This never writes a deployable receipt or invokes a real model.
        return patch.object(migration, '_verify_gate_proof', return_value={'offline_fixture_only': True})

    def test_explicit_version_source_cannot_drop_runtime_gate_sources(self):
        missing = self.real_version_coverage(migration.code_version(self.version_paths))
        self.assertIn(str(Path(migration.__file__).resolve()), missing)
        self.assertTrue(any(path.endswith('/cron/scheduler_tick.py') for path in missing))
        self.assertTrue(any(path.endswith('/agent/auxiliary_hooks.py') for path in missing))

    def test_p0_proof_reobserves_actual_schema2_writer_and_rejects_exit(self):
        manifest, ledger = self.staged(records=False); self.plan(ledger)
        self.assertEqual(ledger.set_mode('ACTIVE', command_id='isolated-authority')['status'], 'COMMITTED')
        self.assertEqual(ledger.open_admission(command_id='isolated-open', expected_generation=1,
            migration_manifest={'validated': True})['status'], 'COMMITTED')
        child, identity = self.worker(ledger)
        self.write(self.home/'runtime/role-pool-executions'/(identity['execution_id']+'.json'), {
            **identity, 'schema_version': 2, 'workflow_version': 1, 'runtime_version_digest': manifest['version']['digest']})
        item = {'version_digest': manifest['version']['digest'], 'proof': {'pool_path': str(self.pool), 'home': str(self.home),
            'generation': 1, 'execution_id': identity['execution_id']}}
        manifest['acceptance_environments'] = {'P0': {'purpose': 'isolated_acceptance',
            'pool_path': str(self.pool), 'home': str(self.home), 'database': str(ledger.path), 'run_id': 'run-one'}}
        from migration_contracts import declared_environments
        manifest['acceptance_environments']=declared_environments(manifest['acceptance_environments'],manifest['version']['digest'])
        result = migration._verify_gate_proof('P0', item, manifest, ledger.path, self.now)
        self.assertEqual(result['execution_id'], identity['execution_id'])
        child.terminate(); child.wait(timeout=5)
        with self.assertRaisesRegex(ValueError, 'CURRENT_SCHEMA2_NATIVE_WRITER'):
            migration._verify_gate_proof('P0', item, manifest, ledger.path, self.now)

    def test_r_proof_requires_every_raw_identity_and_explicit_integer_attempt(self):
        manifest, ledger = self.staged(); self.plan(ledger)
        record = manifest['records'][0]
        key = migration.sha([record['source'], record['source_sha256'], record['identity']])
        proof = {'reviewed_by': 'fixture-manual-review', 'resolutions': []}
        with self.assertRaisesRegex(ValueError, 'EVERY_RAW_IDENTITY'):
            migration._verify_gate_proof('R', {'proof': proof}, manifest, ledger.path, self.now)
        resolution = {'record_sha256': key, 'run_id': 'run-one', 'task_id': 'TASK-01', 'attempt': 'attempt-1'}
        proof['resolutions'] = [resolution]
        with self.assertRaisesRegex(ValueError, 'INTEGER_ATTEMPT'):
            migration._verify_gate_proof('R', {'proof': proof}, manifest, ledger.path, self.now)
        resolution['attempt'] = 1
        self.assertIn('resolutions_sha256', migration._verify_gate_proof('R', {'proof': proof}, manifest, ledger.path, self.now))

    def test_m_proof_rejects_record_without_actual_provider_dispatch(self):
        import usage_accounting
        usage_accounting.record(self.home/'runtime/role-usage.db', 'end', session_id='offline-session',
            api_request_id='logical', physical_attempt_id='physical', role='A', project=self.root,
            started_at=self.now-1, ended_at=self.now, session_kind='business_cron',
            route='cloud', run_id='run-one', work_id='work-one', execution_id='a'*32,
            provider_dispatched=False)
        item = {'proof': {'home': str(self.home), 'session_id': 'offline-session',
            'physical_attempt_id': 'physical', 'logical_request_id': 'logical', 'execution_id': 'a'*32,
            'controlled_project': str(self.root)}}
        with self.assertRaisesRegex(ValueError, 'REAL_CONTROLLED_MODEL_ATTEMPT'):
            migration._verify_gate_proof('M', item, {'acceptance_environments': {'M': {
                'purpose': 'isolated_acceptance', 'home': str(self.home),
                'controlled_project': str(self.root), 'run_id': 'run-one'}}}, None, self.now)

    def test_t2_proof_reads_real_systemd_completion_and_frozen_spec(self):
        identifier = uuid.uuid4().hex
        unit = 'migration-gate-fixture-'+identifier+'.service'
        spec = self.write(self.home/'service-spec.json', {'run_id': identifier, 'unit': unit,
            'runtime_version_digest': 'isolated-fixture-version'})
        self.addCleanup(lambda: subprocess.run(['systemctl', '--user', 'stop', unit], capture_output=True, timeout=10))
        launched = subprocess.run(['systemd-run', '--user', '--quiet', '--no-ask-password',
            '--unit='+unit, '--property=Type=oneshot', '--property=RemainAfterExit=yes', '/bin/true'], capture_output=True, text=True, timeout=10)
        self.assertEqual(launched.returncode, 0, launched.stderr)
        # RemainAfterExit keeps the native invocation/ExecMain metadata until
        # cleanup, even after the service executable has exited.
        item = {'version_digest': 'isolated-fixture-version', 'proof': {'spec_path': str(spec), 'spec_sha256': migration.file_sha(spec),
            'unit': unit, 'service_id': identifier}}
        manifest = {'acceptance_environments': {'T2': {'purpose': 'isolated_acceptance',
            'spec_path': str(spec), 'unit': unit, 'service_id': identifier}}}
        self.assertIn('service_sha256', migration._verify_gate_proof('T2', item, manifest, None, self.now))
        spec.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'FROZEN_INDEPENDENT_SERVICE_SPEC'):
            migration._verify_gate_proof('T2', item, manifest, None, self.now)

    def test_t1_proof_checks_real_completed_candidate_and_native_test_evidence(self):
        from tests.test_workflow_evidence import WorkflowEvidenceTests
        fixture = WorkflowEvidenceTests('test_real_frozen_checkout_publishes_once_and_tamper_closes_gate')
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        result = fixture.execute()
        self.assertEqual(result['status'], 'COMMITTED', result)
        execution = next(row for row in fixture.ledger.snapshot()['executions'] if row['execution_id'] == fixture.tester['execution_id'])
        os.kill(execution['pid'], signal.SIGTERM); os.waitpid(execution['pid'], 0)
        self.assertEqual(fixture.ledger.end_execution(fixture.tester, command_id='gate-test-ended')['status'], 'COMMITTED')
        coordinator = fixture.bind('COORDINATOR')
        self.assertEqual(fixture.ledger.decide(coordinator, fixture.candidate, 'APPROVE', command_id='gate-approved')['status'], 'COMMITTED')
        receipt = fixture.root/'delivery-receipt.txt'; receipt.write_text('fixture candidate delivery receipt')
        self.assertEqual(fixture.ledger.decide(coordinator, fixture.candidate, 'COMPLETE',
            evidence={'delivery': {'path': str(receipt)}}, command_id='gate-completed')['status'], 'COMMITTED')
        item = {'version_digest': migration.code_version()['digest'], 'proof': {
            'database': str(fixture.ledger.path), 'run_id': fixture.run, 'candidate_id': fixture.candidate}}
        manifest = {'acceptance_environments': {'T1': {'purpose': 'isolated_acceptance',
            'database': str(fixture.ledger.path), 'run_id': fixture.run}}}
        self.assertEqual(migration._verify_gate_proof('T1', item, manifest, None, self.now)['candidate_id'], fixture.candidate)
        frozen = json.loads(Path(result['runner_evidence']['path']).read_text())
        Path(frozen['log_path']).write_text('changed after completed')
        with self.assertRaisesRegex(ValueError, 'INDEPENDENT_TEST_SOURCE_CHANGED'):
            migration._verify_gate_proof('T1', item, manifest, None, self.now)

    def test_version_or_evidence_artifact_change_and_new_legacy_event_block_switch(self):
        manifest, ledger = self.staged(); self.plan(ledger)
        receipts = self.evidence(manifest)
        self.version_file.write_text('# changed\n')
        result = migration.validate(manifest, receipts, at=self.now)
        self.assertIn('CODE_VERSION_CHANGED', result['problems'])
        self.version_file.write_text('# frozen fixture code\n')
        (self.home/'evidence/M-fixture-output.txt').write_text('changed artifact')
        self.assertFalse(migration.validate(manifest, receipts, at=self.now)['ready'])
        self.evidence(manifest)
        self.write(self.root/'events/A/late.json', {'event_id': 'new-after-inventory', 'task': 'TASK-01', 'attempt': 1})
        result = migration.validate(manifest, receipts, at=self.now)
        self.assertIn('LEGACY_INCREMENT_REQUIRES_NEW_INVENTORY', result['problems'])
        self.assertTrue(any(path.endswith('late.json') for path in result['changed_sources']))

    def test_verified_fixture_activation_is_monotonic_idempotent_and_preserves_jobs(self):
        manifest, ledger = self.staged(); self.plan(ledger)
        jobs_before = (self.home/'cron/jobs.json').read_bytes()
        with self.fixture_proofs():
            result = migration.activate(manifest, self.evidence(manifest), at=self.now)
        self.assertEqual(result['status'], 'COMMITTED', result)
        self.assertEqual(ledger.mode()['mode'], 'ACTIVE')
        self.assertEqual(ledger.mode()['generation'], 1)
        self.assertEqual(ledger.mode()['admission_open'], 1)
        self.assertEqual((self.home/'cron/jobs.json').read_bytes(), jobs_before)
        with self.fixture_proofs():
            self.assertEqual(migration.activate(manifest, self.evidence(manifest), at=self.now)['status'], 'DUPLICATE')
        self.assertEqual(ledger.mode()['generation'], 1)

    def test_activation_crash_after_mode_change_remains_frozen_and_can_resume(self):
        manifest, ledger = self.staged(); self.plan(ledger)
        receipts = self.evidence(manifest)
        with self.fixture_proofs(), patch.object(BusinessLedger, 'open_admission', side_effect=RuntimeError('simulated crash')):
            with self.assertRaisesRegex(RuntimeError, 'simulated crash'):
                migration.activate(manifest, receipts, at=self.now)
        self.assertEqual(ledger.mode()['mode'], 'ACTIVE')
        self.assertEqual(ledger.mode()['admission_open'], 0)
        with self.fixture_proofs():
            result = migration.activate(manifest, receipts, at=self.now)
        self.assertIn(result['status'], ('COMMITTED', 'DUPLICATE'), result)
        self.assertEqual(ledger.mode()['admission_open'], 1)
        self.assertEqual(ledger.mode()['generation'], 1)

    def test_final_export_freezes_and_retains_high_water_increment_and_old_identity_fence(self):
        manifest, ledger = self.staged(); self.plan(ledger)
        with self.fixture_proofs():
            self.assertEqual(migration.activate(manifest, self.evidence(manifest), at=self.now)['status'], 'COMMITTED')
        baseline = ledger.snapshot()['event_high_water']
        child, identity = self.worker(ledger)
        checkpoint = ledger.checkpoint(identity, {'next': '继续核证据'}, command_id='actual-checkpoint')
        self.assertEqual(checkpoint['status'], 'COMMITTED')
        pending = migration.export(self.pool, after=baseline, freeze=True, reason='offline rollback rehearsal')
        self.assertFalse(pending['final'])
        self.assertEqual(ledger.mode()['admission_open'], 0)
        self.assertTrue(any(event['kind'] == 'EXECUTION_BOUND' for event in pending['increment']))
        child.terminate(); child.wait(timeout=5)
        result = ledger.end_execution(identity, command_id='actual-worker-exited')
        self.assertEqual(result['status'], 'COMMITTED', result)
        final = migration.export(self.pool, after=baseline)
        self.assertTrue(final['final'], final)
        preview = migration.rollback_preview(final, self.contract())
        self.assertTrue(preview['ready'], preview)
        self.assertEqual(preview['next_generation'], ledger.mode()['generation']+1)
        result = ledger.set_mode('LEGACY', expected_generation=ledger.mode()['generation'],
            migration_manifest={'validated': True, 'export_sha256': final['sha256'], 'preview': preview['sha256']},
            command_id='offline-commit-verified-rollback')
        self.assertEqual(result['status'], 'COMMITTED', result)
        self.assertEqual(ledger.mode()['generation'], 2)
        self.assertFalse(ledger.validate_identity(identity, allow_drain=True)['valid'])

    def test_new_events_after_export_require_increment_and_rollback_target_cannot_omit_identity(self):
        manifest, ledger = self.staged(); self.plan(ledger)
        final = migration.export(self.pool, freeze=True, reason='fixture export')
        self.assertTrue(final['final'])
        contract = self.contract()
        self.assertTrue(migration.rollback_preview(final, contract)['ready'])
        invalid = migration.sealed({**{key: value for key, value in contract.items() if key != 'sha256'},
                                    'capabilities': ['pause']})
        self.assertFalse(migration.rollback_preview(final, invalid)['ready'])
        ledger.freeze_admission('new persistent control after export', command_id='late-control')
        preview = migration.rollback_preview(final, contract)
        self.assertFalse(preview['ready'])
        self.assertTrue(any(row['code'] == 'NEW_EVENTS_REQUIRE_INCREMENTAL_EXPORT' for row in preview['conflicts']))
        newer = migration.export(self.pool, after=final['through_inclusive'])
        self.assertGreater(newer['through_inclusive'], final['through_inclusive'])
        self.assertTrue(newer['increment'])

    def test_cli_inspect_validate_and_export_have_real_exit_status_and_no_implicit_migration(self):
        path = self.home/'inventory.json'
        command = [sys.executable, str(Path(migration.__file__)), 'inspect', '--home', str(self.home),
            '--pool', str(self.pool), '--version-source', str(self.version_file), '--output', str(path)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(path.exists())
        self.assertFalse(migration.ledger_path(self.pool).exists())
        result = subprocess.run([sys.executable, str(Path(migration.__file__)), 'validate', '--manifest', str(path)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(json.loads(result.stdout)['ready'])
        self.assertFalse(migration.ledger_path(self.pool).exists())


if __name__ == '__main__':
    unittest.main()
