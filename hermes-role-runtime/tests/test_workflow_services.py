"""Detached service adapters with real ledger/Git; fake only external service."""
import json
from pathlib import Path
import signal
import os
import unittest

try:
    from test_business_ledger import LedgerFixture
except ModuleNotFoundError:
    from tests.test_business_ledger import LedgerFixture
from workflow_services import submit_service, collect_service_result, adjudicate_service


class ExternalService:
    def __init__(self, root, *, fail_submit=False, incomplete=False):
        self.root=root; self.calls=0; self.fail_submit=fail_submit; self.incomplete=incomplete

    def submit(self, kind, frozen, request, directory):
        self.calls+=1
        if self.fail_submit:
            raise TimeoutError('fake external dispatch outcome unknown')
        path=directory/'external-result.json'
        path.write_text(json.dumps({'findings':[{'id':'finding-one','message':'需要协调者明确判断'}],
                                   'review_complete':True,'head':frozen['candidate']['head']}))
        return {'kind':kind,'native_id':'isolated-service','source':str(path)}

    def collect(self, kind, reference):
        import hashlib
        path=Path(reference['source'])
        return {'state':'INCOMPLETE' if self.incomplete else 'READY_FOR_ADJUDICATION',
                'service_stopped':True,'facts':json.loads(path.read_text()),
                'sources':[{'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}]}


class WorkflowServiceTests(LedgerFixture):
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

    def submit(self,adapter,action='ocr-one',kind='ocr'):
        return submit_service(self.ledger,self.coord,self.candidate_id,kind,{'background_file':'isolated-background'},
            action_id=action,authorization=self.authorization,command_id='submit-'+action,adapter=adapter)

    def test_external_only_fake_service_complete_is_not_automatic_pass(self):
        adapter=ExternalService(self.root)
        result=self.assertCommitted(self.submit(adapter))
        self.assertEqual(result['approval'],'NOT_DECIDED')
        repeated=self.submit(adapter)
        self.assertEqual(repeated['status'],'DUPLICATE')
        self.assertEqual(adapter.calls,1)
        self.assertCommitted(self.ledger.checkpoint(self.coord,{'next':'等待独立服务'},yield_execution=True,command_id='service-yield'))
        self.close(self.coord_process,self.coord)
        collected=self.assertCommitted(collect_service_result(self.ledger,'ocr-one',command_id='collect',adapter=adapter))
        self.assertEqual(collected['state'],'RESULT_READY')
        self.assertFalse(collected['business_pass'])
        self.assertEqual(self.ledger.snapshot()['verifications'],[])
        new_process,new_coord=self.bind('COORDINATOR')
        adjudicated=self.assertCommitted(adjudicate_service(self.ledger,new_coord,'ocr-one','FAIL',
            'finding-one 与固定AC冲突，需要最小返修',command_id='adjudicate'))
        self.assertEqual(adjudicated['result'],'FAIL')
        self.assertEqual(self.ledger.snapshot()['verifications'][0]['result'],'FAIL')
        self.assertEqual(len(self.ledger.snapshot()['roles']),5)

    def test_unknown_launch_is_not_repeated_or_silently_freed(self):
        adapter=ExternalService(self.root,fail_submit=True)
        result=self.submit(adapter)
        self.assertEqual(result['status'],'UNVERIFIED')
        self.assertEqual(result['code'],'EXTERNAL_SUBMISSION_UNKNOWN')
        repeated=self.submit(adapter)
        self.assertEqual(repeated['status'],'DUPLICATE')
        self.assertEqual(repeated['state'],'UNKNOWN')
        self.assertFalse(repeated['replay_allowed'])
        self.assertEqual(adapter.calls,1)
        second=self.submit(ExternalService(self.root),action='ocr-two')
        self.assertEqual(second['code'],'OCR_SERIAL_ACTION_STILL_ACTIVE_OR_UNKNOWN')
        self.assertEqual(collect_service_result(self.ledger,'ocr-one',command_id='unknown-collect',adapter=adapter)['code'],
                         'EXTERNAL_DISPATCH_MAPPING_MISSING')

    def test_incomplete_service_cannot_be_adjudicated_pass_and_source_tamper_blocks(self):
        adapter=ExternalService(self.root,incomplete=True)
        result=self.submit(adapter)
        self.assertCommitted(result)
        self.assertCommitted(collect_service_result(self.ledger,'ocr-one',command_id='incomplete',adapter=adapter))
        self.assertEqual(adjudicate_service(self.ledger,self.coord,'ocr-one','PASS','不能跳过缺覆盖',command_id='bad-pass')['code'],
                         'COMPLETE_SERVICE_FACTS_AND_SEMANTIC_DECISION_REQUIRED')
        other=ExternalService(self.root)
        second=self.assertCommitted(self.submit(other,action='ocr-two'))
        self.assertCommitted(collect_service_result(self.ledger,'ocr-two',command_id='ready',adapter=other))
        Path(second['reference']['source']).write_text('tampered')
        self.assertEqual(adjudicate_service(self.ledger,self.coord,'ocr-two','PASS','原文已核',command_id='tampered')['code'],
                         'EVIDENCE_HASH_MISMATCH')

    def test_pre_dispatch_rejection_frees_serial_ocr_gate_without_unknown(self):
        # A pure argument error must not leave UNKNOWN behind: that row holds
        # the global serial OCR gate closed for every candidate. The native
        # adapter signals it with PreDispatchRejected.
        from workflow_services import PreDispatchRejected
        class BadPath(ExternalService):
            def submit(adapter,kind,frozen,request,directory):
                adapter.calls+=1
                raise PreDispatchRejected('OCR_INPUT_MUST_BE_REGULAR_FILE_IN_FROZEN_REQUIREMENT')
        adapter=BadPath(self.root)
        result=submit_service(self.ledger,self.coord,self.candidate_id,'ocr',
            {'background_file':str(self.root/'background.md')},
            action_id='bad-path',authorization=self.authorization,command_id='bad-path-submit',
            adapter=adapter)
        self.assertEqual('REJECTED',result['status'])
        self.assertEqual('OCR_INPUT_MUST_BE_REGULAR_FILE_IN_FROZEN_REQUIREMENT',result['code'])
        self.assertFalse(result['dispatch_performed'])
        self.assertFalse(result['authorization_consumed'])
        action=next(row for row in self.ledger.snapshot()['external_actions']
                    if row['action_id']=='bad-path')
        self.assertEqual('REJECTED',action['state'])
        second=self.assertCommitted(self.submit(ExternalService(self.root),action='ocr-two'))
        self.assertEqual('SUBMITTED',second['state'])

    def test_rejection_entry_refuses_action_with_native_reference(self):
        adapter=ExternalService(self.root)
        self.assertCommitted(self.submit(adapter))
        self.assertEqual('REJECTION_REQUIRES_NO_NATIVE_REFERENCE',
            self.ledger.reject_external_action('ocr-one',reason='不能作废已派发动作',
                command_id='refuse-void')['code'])
        self.assertEqual('SUBMITTED',next(row for row in self.ledger.snapshot()['external_actions']
                                          if row['action_id']=='ocr-one')['state'])

    def test_external_tool_wrapper_is_part_of_execution_fence(self):
        wrapper,spec=self.worker()
        registered=self.assertCommitted(self.ledger.register_tool_execution(self.coord,{
            'tool_id':'terminal-wrapper-one','pid':spec['pid'],'process_start':spec['process_start'],
            'cgroup':spec['scope']},command_id='register-tool'))
        self.assertEqual(registered['execution_id'],self.coord['execution_id'])
        self.assertCommitted(self.ledger.checkpoint(self.coord,{'next':'待wrapper退出'},yield_execution=True,command_id='wrapper-yield'))
        self.coord_process.terminate(); self.coord_process.wait(timeout=5)
        denied=self.ledger.end_execution(self.coord,command_id='wrapper-still-alive')
        self.assertEqual(denied['code'],'WRITER_STILL_ALIVE')
        wrapper.terminate(); wrapper.wait(timeout=5)
        self.assertCommitted(self.ledger.end_execution(self.coord,command_id='wrapper-ended'))


if __name__=='__main__':
    unittest.main()
