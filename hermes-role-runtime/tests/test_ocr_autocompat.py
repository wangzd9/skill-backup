"""Real CLI/namespace/parser admission tests; only the model executable is fake.

The trusted npm launcher bytes are vendored with their original license headers.
There is no mock admission record and no bypass in the production validator.
"""
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import ocr_compat as compat
import ocr_runtime as runtime
from test_ocr_runtime import FAKE_OCR
from tests.test_business_ledger import LedgerFixture
from tests.test_workflow_services import ExternalService
from workflow_services import submit_service, collect_service_result, NativeServices


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ocr-admission-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.package = self.root/'package'
        shutil.copytree(Path(__file__).parent/'fixtures/ocr-launcher', self.package)
        (self.package/'package.json').write_text('{"version":"9.8.7"}')
        self.launcher = self.package/'bin/ocr.js'
        self.native = self.package/'bin/opencodereview'
        source = FAKE_OCR.replace('OpenCodeReview v1.13.0 (fixture)', 'OpenCodeReview v9.8.7 (fixture)')
        source = source.replace("'ocr_version':'v1.13.0'", "'ocr_version':'v9.8.7','configured_concurrency':1")
        source = source.replace("files=['a.py','b.py','tests/test_a.py']", "files=['sample_a.py','sample_b.py']")
        source = source.replace("mode=(root/'mode').read_text().strip()", "mode='complete'")
        source = source.replace("with (root/'calls').open('a') as f:f.write('review\\n')", '')
        source = source.replace("path=root/'sessions'/", "path=Path.home()/'.opencodereview'/'sessions'/")
        source = source.replace('else:time.sleep(0.2)', 'else:time.sleep(0)')
        source = source.replace("manifest={'schema_version':", "manifest={'run_id':sid,'operation':'review','schema_version':")
        source = source.replace("event({'type':'session_end','run_manifest':manifest})",
            "event({'type':'llm_request','messages':[{'content':Path(arg('--background-file')).read_text()}]})\n"
            "event({'type':'llm_response','usage':{'prompt_tokens':10,'completion_tokens':2}})\n"
            "for item in items: event({'type':'review_item_done','filePath':item['path'],'fingerprint':item['fingerprint'],'comments':[]})\n"
            "event({'type':'session_end','run_manifest':manifest})")
        source = source.replace("print(json.dumps({'findings':[]}))",
            "print(json.dumps({'status':'complete','session_id':sid,'manifest':manifest,"
            "'llm':{'provider':arg('--provider'),'model':arg('--model')},"
            "'summary':{'comments':0,'files_reviewed':len(items),'input_tokens':10,'output_tokens':2,'total_tokens':12},'comments':[]}))")
        source = source.replace("if args[:2]==['delegate','rule']:",
            "if '--ocr-compat-invalid-flag' in args: sys.exit(2)\n"
            "if '--help' in args: print("+repr(compat.baseline()['review_help'])+");sys.exit(0)\n"
            "if args[:2]==['delegate','rule']:")
        self.native.write_text(source); self.native.chmod(0o755)
        self.config = self.root/'config.json'
        self.config.write_text(json.dumps({'custom_providers':{runtime.PROVIDER:{
            'url':runtime.ENDPOINT,'model':'fixture-model'}}}))
        for name, value in [('OCR', self.launcher), ('CONFIG', self.config)]:
            patcher = patch.object(runtime, name, value); patcher.start(); self.addCleanup(patcher.stop)
        self.state = self.root/'state'

    def admit(self):
        return compat.ensure_admission(runtime, state_dir=self.state)

    def mutate(self, old, new):
        self.native.write_text(self.native.read_text().replace(old, new))

    def test_new_version_runs_two_batches_and_real_results_parser(self):
        result = self.admit()
        self.assertFalse(result['cache_hit'])
        record = result['record']
        self.assertIn('v9.8.7', record['version'])
        self.assertEqual(set(compat.CHECKS), set(record['checks']))
        self.assertTrue(all(v['passed'] for v in record['checks'].values()))
        parsed = runtime.inspect_results(Path(record['evidence_path']))
        self.assertEqual('READY_FOR_ADJUDICATION', parsed['evidence_state'], parsed)
        self.assertEqual(2, len(parsed['batches']))
        self.assertEqual(2, len(record['validation_run_id']))

    def test_cache_hit_runs_no_second_review(self):
        first = self.admit(); second = self.admit()
        self.assertTrue(second['cache_hit'])
        self.assertEqual(0, second['validation_model_calls'])
        self.assertEqual(first['record'], second['record'])
        self.assertEqual(1, len(list((self.state/'ocr-compat-evidence').iterdir())))

    def test_same_version_native_hash_drift_revalidates(self):
        first = self.admit()
        self.native.write_text(self.native.read_text()+'\n# changed native bytes\n')
        second = self.admit()
        self.assertFalse(second['cache_hit'])
        self.assertNotEqual(first['record']['native_binary_sha256'], second['record']['native_binary_sha256'])
        self.assertEqual(2, len(compat.read_registry(self.state/'ocr-version-admissions.json')['admissions']))

    def test_corrupt_registry_revalidates(self):
        first = self.admit()
        (self.state/'ocr-version-admissions.json').write_text('{truncated')
        second = self.admit()
        self.assertFalse(second['cache_hit'])
        self.assertNotEqual(first['record']['validation_run_id'], second['record']['validation_run_id'])

    def test_missing_evidence_invalidates_cache(self):
        first = self.admit()
        (Path(first['record']['evidence_path'])/'b01.log').unlink()
        self.assertFalse(self.admit()['cache_hit'])

    def test_malformed_cached_check_revalidates(self):
        self.admit()
        path = self.state/'ocr-version-admissions.json'
        data = json.loads(path.read_text())
        next(iter(data['admissions'].values()))['checks']['parser'] = 'damaged'
        path.write_text(json.dumps(data))
        self.assertFalse(self.admit()['cache_hit'])

    def test_concurrent_requests_share_one_validation(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: self.admit(), (1, 2)))
        self.assertEqual([False, True], sorted(r['cache_hit'] for r in results))
        self.assertEqual(1, len(list((self.state/'ocr-compat-evidence').iterdir())))

    def test_changed_launcher_rejected_before_execution(self):
        self.launcher.write_text(self.launcher.read_text()+'\nthrow Error("must never execute");\n')
        with self.assertRaisesRegex(compat.CompatibilityRejected, 'LAUNCHER_INCOMPATIBLE.*bin/ocr.js'):
            self.admit()
        self.assertFalse((self.state/'ocr-compat-evidence').exists())

    def test_changed_script_rejected_even_with_same_launcher_and_native(self):
        self.admit()
        script = self.package/'scripts/update.js'
        script.write_text(script.read_text()+'\n// changed\n')
        with self.assertRaisesRegex(compat.CompatibilityRejected, 'scripts/update.js'):
            self.admit()

    def test_unknown_manifest_schema_rejected(self):
        self.native.write_text(self.native.read_text()+'\n# ocr.run-manifest/v2\n')
        with self.assertRaisesRegex(compat.CompatibilityRejected, 'MANIFEST_SCHEMA_UNVERIFIABLE'):
            self.admit()

    def test_parameter_type_or_semantics_change_rejected(self):
        self.mutate('max concurrent subtasks (default 8)', 'max concurrent subtasks (default 9)')
        with self.assertRaisesRegex(compat.CompatibilityRejected, 'PARAMETER_SURFACE_INCOMPATIBLE.*concurrency'):
            self.admit()

    def test_refusal_exit_zero_rejected(self):
        self.mutate("if '--ocr-compat-invalid-flag' in args: sys.exit(2)",
                    "if '--ocr-compat-invalid-flag' in args: sys.exit(0)")
        with self.assertRaisesRegex(compat.CompatibilityRejected, 'EXIT_CODE_INCOMPATIBLE'):
            self.admit()

    def test_real_review_failure_is_rejected_and_alerted_once(self):
        self.mutate("mode='complete'", "sys.exit(7)\nmode='complete'")
        for first in (True, False):
            with self.assertRaises(compat.CompatibilityRejected) as caught:
                self.admit()
            self.assertEqual('COMPAT_RUN_FAILED', caught.exception.code)
            self.assertIs(first, caught.exception.details['alert'])

    def test_usage_mismatch_is_parser_incompatible(self):
        self.mutate("'total_tokens':12", "'total_tokens':13")
        with self.assertRaisesRegex(compat.CompatibilityRejected, 'PARSER_INCOMPATIBLE.*用量'):
            self.admit()

    def test_incomplete_session_rejected_by_real_parser(self):
        self.mutate("'operation':'review'", "'operation':'scan'")
        with self.assertRaisesRegex(compat.CompatibilityRejected, 'PARSER_INCOMPATIBLE'):
            self.admit()

    def test_native_adapter_preserves_actual_cli_refusal(self):
        self.native.write_text(self.native.read_text()+'\n# ocr.run-manifest/v2\n')
        scripts = self.root/'bridge'; scripts.mkdir()
        program = ("import sys\nfrom pathlib import Path\n"
            + "sys.path.insert(0, " + repr(str(Path(runtime.__file__).parent)) + ")\n"
            + "import ocr_runtime as runtime\n"
            + "runtime.OCR = Path("+repr(str(self.launcher))+")\n"
            + "runtime.CONFIG = Path("+repr(str(self.config))+")\n"
            + "admit = runtime.admit_ocr\n"
            + "runtime.admit_ocr = lambda: admit(Path("+repr(str(self.state))+"))\n"
            + "runtime.existing_ocr = lambda: []\n"
            + "sys.exit(runtime.main())\n")
        (scripts/'ocr_runtime.py').write_text(program)
        adapter = NativeServices(); adapter.scripts = scripts
        result = adapter.preflight('ocr')
        self.assertFalse(result['ready'])
        self.assertEqual('MANIFEST_SCHEMA_UNVERIFIABLE', result['code'])
        self.assertEqual(0, result['preflight_rejection']['side_effects'])

    def test_runtime_receipt_is_structured_preflight_rejection(self):
        self.native.write_text(self.native.read_text()+'\n# ocr.run-manifest/v2\n')
        with self.assertRaises(runtime.PreflightRejected) as caught:
            runtime.admit_ocr(self.state)
        receipt = runtime.preflight_receipt(caught.exception, 'preflight')
        self.assertEqual(0, receipt['side_effects'])
        self.assertFalse(receipt['dispatch_performed'])
        self.assertEqual('MANIFEST_SCHEMA_UNVERIFIABLE', receipt['code'])


class QuotaTests(LedgerFixture):
    def source(self, text='全部权限：授权当前需求全部任务 OCR max_calls=30'):
        return {'authenticated':True,'current_full':True,'message_id':'grant','user_id':'u',
                'primary_text':text}

    def candidate_and_grant(self, calls=1):
        process, dev = self.bind('A'); cid, _ = self.candidate(dev); self.close(process, dev)
        _, coord = self.bind('COORDINATOR')
        self.assertCommitted(self.ledger.grant_external_authorization({
            'authorization_id':'quota','run_id':'run-one','task_ids':['T1'], 'service_kind':'ocr',
            'max_calls':calls,'expires_at':self.now+3600},self.source(),command_id='quota'))
        return cid, coord

    def test_explicit_run_grant_accepts_thirty(self):
        self.candidate_and_grant(30)
        grant = next(g for g in self.ledger.snapshot()['authorizations'] if g['authorization_id']=='quota')
        self.assertEqual(30, grant['spec']['max_calls'])

    def test_default_policy_floor_growth_and_cap_via_real_intake(self):
        from workflow_intake import accept, requirement_review_policy
        for count, expected in ((1, 30), (6, 30), (15, 32), (100, 100)):
            tasks = ['NEW'+str(i) for i in range(count)]
            (self.root/'TODO.md').write_text('\n'.join(tasks))
            target = {'requirement_id':'new'+str(count),'requirement_root':str(self.root),
                      'repo':str(self.repo),'task_ids':tasks,'contract_files':['TODO.md']}
            source = self.source('全部权限：开发 new'+str(count))
            accepted = self.assertCommitted(accept(self.ledger,target,source,command_id='new'+str(count)))
            policy = self.assertCommitted(requirement_review_policy(self.ledger,accepted['run_id'],
                accepted['event_id'],source,command_id='review-policy'+str(count)))
            grant = next(g for g in self.ledger.snapshot()['authorizations']
                         if g['authorization_id']==policy['authorization_id'])
            self.assertEqual(expected, grant['spec']['max_calls'])

    def test_exhaustion_implicit_and_explicit_rejected_without_lock(self):
        cid, coord = self.candidate_and_grant()
        adapter = ExternalService(self.root)
        self.assertCommitted(submit_service(self.ledger,coord,cid,'ocr',{'background_text':'AC'},
            action_id='one',authorization={'authorization_id':'quota'},command_id='one',adapter=adapter))
        self.assertCommitted(collect_service_result(self.ledger,'one',command_id='collect',adapter=adapter))
        for index, auth in enumerate((None, {'authorization_id':'quota'})):
            result = submit_service(self.ledger,coord,cid,'ocr',{'background_text':'AC'},
                action_id='over'+str(index),authorization=auth,command_id='over'+str(index),adapter=adapter)
            self.assertEqual('REJECTED', result['status'], result)
            self.assertEqual('SERVICE_GRANT_CALL_LIMIT', result['code'])
            self.assertIn('额度用尽，需提额度', result['message'])
        actions = self.ledger.snapshot()['external_actions']
        self.assertEqual(1, len(actions))
        self.assertFalse(any(a['state'] in ('SUBMITTED','UNKNOWN') for a in actions))

    def test_selftest_does_not_consume_business_grant(self):
        self.candidate_and_grant(30)
        before = self.ledger.snapshot()
        fixture = AdmissionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.assertFalse(fixture.admit()['cache_hit'])
        after = self.ledger.snapshot()
        self.assertEqual(before['external_actions'], after['external_actions'])
        self.assertEqual(before['authorizations'], after['authorizations'])

    def test_compatibility_preflight_refusal_creates_no_external_action(self):
        cid, coord = self.candidate_and_grant()
        class Incompatible(ExternalService):
            def preflight(adapter, kind):
                return {'ready':False,'dispatch_performed':False,'code':'PARSER_INCOMPATIBLE'}
        result = submit_service(self.ledger,coord,cid,'ocr',{'background_text':'AC'},
            action_id='refused',authorization={'authorization_id':'quota'},command_id='refused',
            adapter=Incompatible(self.root))
        self.assertEqual('REJECTED', result['status'])
        self.assertFalse(self.ledger.snapshot()['external_actions'])
