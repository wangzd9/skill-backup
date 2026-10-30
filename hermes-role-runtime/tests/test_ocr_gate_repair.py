"""Real ledger refusal/uncertainty boundaries; fake only the native child."""
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import ocr_runtime as runtime
from workflow_services import NativeServices
from tests import test_workflow_services as service_tests
from tests.test_business_ledger import LedgerFixture
ExternalService=service_tests.ExternalService
from workflow_service_recovery import reject


class OcrGateRepairTests(LedgerFixture):
    def setUp(self):
        super().setUp()
        process,identity=self.bind('A')
        self.candidate_id,self.candidate_spec=self.candidate(identity)
        self.close(process,identity)
        self.coord_process,self.coord=self.bind('COORDINATOR')
        self.authorization={'authorization_id':'explicit-isolated-ocr'}
        self.assertCommitted(self.ledger.grant_external_authorization({'authorization_id':'explicit-isolated-ocr',
            'candidate_id':self.candidate_id,'service_kind':'ocr','max_calls':5,'expires_at':self.now+3600},
            {'authenticated':True,'current_full':True,'message_id':'native-service-grant','user_id':'u',
             'primary_text':'本轮授予全部权限：对此候选授权OCR最多五次'},command_id='grant-ocr'))
    submit=service_tests.WorkflowServiceTests.submit
    def native(self, operation, *, intent=False, partial=False):
        class Native(ExternalService):
            def submit(adapter,kind,frozen,request,directory):
                if operation=='start' or partial:
                    native=directory/'native';native.mkdir()
                    if not partial:(native/'state.json').write_text(json.dumps({'state':'PREPARED'}))
                    if intent:(native/'dispatch-intent.json').write_text('{}')
                code='EXECUTION_IDENTITY_MISMATCH' if operation=='start' else 'OCR_VERSION_UNVALIDATED'
                refusal=runtime.preflight_receipt(RuntimeError(code+': fixture'),operation)
                run=subprocess.CompletedProcess([],78,json.dumps({'success':False,'preflight_rejection':refusal}),'')
                with patch('workflow_services.subprocess.run',return_value=run):
                    NativeServices()._cli('ocr_runtime.py',[operation])
        return Native(self.root)

    def test_structured_prepare_refusal_rejected_and_next_action_admitted(self):
        for operation,partial in [('prepare',False),('start',False),('prepare',True)]:
            action_id='reject-'+operation+str(partial)
            with self.subTest(operation=operation,partial=partial):
                result=self.submit(self.native(operation,partial=partial),action=action_id)
                self.assertEqual(result['status'],'REJECTED',result)
                action=next(a for a in self.ledger.snapshot()['external_actions'] if a['action_id']==action_id)
                self.assertEqual(action['state'],'REJECTED')
        self.assertCommitted(self.submit(ExternalService(self.root),action='after-refusals'))

    def test_receipt_cannot_clear_action_with_durable_dispatch_intent(self):
        result=self.submit(self.native('start',intent=True))
        self.assertEqual(result['code'],'PRE_DISPATCH_REJECTION_NOT_RECORDED')
        action=next(a for a in self.ledger.snapshot()['external_actions'] if a['action_id']=='ocr-one')
        self.assertEqual(action['state'],'UNKNOWN')

    def test_no_receipt_after_dispatch_remains_unknown_and_blocks(self):
        self.submit(ExternalService(self.root,fail_submit=True))
        self.assertEqual(next(a for a in self.ledger.snapshot()['external_actions'] if a['action_id']=='ocr-one')['state'],'UNKNOWN')
        result=self.submit(ExternalService(self.root),action='must-block')
        self.assertEqual(result['code'],'OCR_SERIAL_ACTION_STILL_ACTIVE_OR_UNKNOWN')

    def test_legacy_prepare_version_refusal_reconciled_only_by_coordinator(self):
        self.submit(ExternalService(self.root,fail_submit=True))
        from workflow_services import _directory
        p=_directory(self.ledger,'ocr-one')/'invocation.json';value=json.loads(p.read_text())
        value['native_receipt']={'script':'ocr_runtime.py','operation':'prepare','returncode':2,'state':'BLOCKED','error_code':'OCR_VERSION_UNVALIDATED'}
        p.write_text(json.dumps(value))
        self.assertCommitted(reject(self.ledger,self.coord,'ocr-one','audited zero dispatch',command_id='reconcile'))

    def test_structured_protocol_not_error_string_controls_classification(self):
        for rc,receipt in [(2,{}),(0,{}),(78,{'stage':'preflight','side_effects':0})]:
            with self.subTest(rc=rc,receipt=receipt):
                run=subprocess.CompletedProcess([],rc,json.dumps({'success':False,'error':'BACKGROUND_REQUIRED','preflight_rejection':receipt}),'')
                with patch('workflow_services.subprocess.run',return_value=run):
                    with self.assertRaises(RuntimeError):NativeServices()._cli('ocr_runtime.py',['prepare'])

    def test_identity_drift_in_each_frozen_component_is_pre_dispatch(self):
        expected={'version':'v1.13.2','launcher_sha256':'a'*64,'manifest_schema':'ocr.run-manifest/v1'}
        for key in expected:
            with self.subTest(key=key),patch.object(runtime,'execution_identity',return_value={**expected,key:'changed'}):
                with self.assertRaisesRegex(runtime.PreflightRejected,'EXECUTION_IDENTITY_MISMATCH'):
                    runtime.validate_execution_identity({'execution_identity':expected})


class PreflightProtocolTests(unittest.TestCase):
    def test_prepare_cli_emits_fixed_machine_protocol_for_readonly_failures(self):
        import contextlib,io,sys
        argv=['ocr_runtime.py','prepare','--project','/unused','--repo','/unused',
              '--background-file','/unused','--output','/unused','--base','x','--head','y',
              '--task','t','--attempt','1','--authorization','test']
        for error in [RuntimeError('OCR_VERSION_UNVALIDATED: installed v1.13.3'),
                      RuntimeError('BACKGROUND_REQUIRED'),RuntimeError('NO_REVIEWABLE_FILES_OR_DUPLICATES'),
                      PermissionError('readonly')]:
            with self.subTest(error=type(error).__name__),patch.object(runtime,'prepare',side_effect=error),patch.object(sys,'argv',argv):
                out=io.StringIO()
                with contextlib.redirect_stdout(out):rc=runtime.main()
                value=json.loads(out.getvalue());receipt=value['preflight_rejection']
                self.assertEqual(rc,78);self.assertEqual(receipt['stage'],'preflight')
                self.assertEqual(receipt['side_effects'],0);self.assertFalse(receipt['dispatch_performed'])

    def test_start_drift_refuses_before_intent_and_any_native_dispatch(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp);(directory/'state.json').write_text('{"state":"PREPARED"}')
            spec={'unit':'isolated.service','project_lock':str(directory/'project.lock'),
                  'execution_identity':{'version':'v1.13.2','launcher_sha256':'old','manifest_schema':'ocr.run-manifest/v1'}}
            with patch.object(runtime,'load_spec',return_value=spec),patch.object(runtime,'GLOBAL_LOCK',directory/'global.lock'),patch.object(runtime,'service_info',return_value={}),patch.object(runtime,'execution_identity',return_value={**spec['execution_identity'],'version':'v1.13.3'}),patch.object(runtime,'run_command',side_effect=AssertionError('no native dispatch')):
                with self.assertRaisesRegex(runtime.PreflightRejected,'EXECUTION_IDENTITY_MISMATCH'):runtime.launch(directory)
            self.assertFalse((directory/'dispatch-intent.json').exists())
            self.assertEqual(json.loads((directory/'state.json').read_text())['state'],'PREPARED')
