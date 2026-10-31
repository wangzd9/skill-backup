"""隔离真实Git验证恢复授权、版本漂移、历史保留及原生报告；不写业务账本。"""
import copy
import hashlib
import json
import os
from unittest import mock

from test_final_git_delivery import FinalGitDeliveryTests as _Base
import workflow_final_delivery as delivery


class FinalDeliveryRecoveryTests(_Base):
    # 复用仓库夹具，原交付用例也在当前恢复模块环境下重跑。
    def failed_delivery(self):
        self.git('checkout','feat/delivery')
        (self.repo/'scripts/local_quality_gate.sh').write_text('#!/bin/sh\nexit 7\n')
        self.git('add','scripts');self.git('commit','-qm','isolated failed gate')
        self.final['target']['head']=self.git('rev-parse','HEAD')
        request=self.request()
        result=delivery.execute(self.ledger,self.coord,{'request_id':request['request_id']},command_id='initial')
        self.assertEqual('BLOCKED',result['state'])
        path=self.ledger.path.parent/'final-deliveries'/request['request_id']/'quality_gate.json'
        return request,path,path.read_bytes()

    def fix_and_push(self):
        (self.repo/'scripts/local_quality_gate.sh').write_text('#!/bin/sh\nprintf "gate\\n" >> .local/isolated-stages\n')
        self.git('add','scripts');self.git('commit','-qm','isolated gate fix')
        self.git('push','-q','github','main')
        return self.git('rev-parse','HEAD')

    def recovery(self,request,head,identifier='recovery',**changes):
        target={'request_id':request['request_id'],'expected_head':head,'reason':'已修复失败，本地验证重验，不重放Git写操作'}
        target.update(changes)
        provenance={**self.provenance,'message_id':'recovery-message',
            'primary_text':'恢复最终交付 '+request['request_id']+' main '+head+' 并验证本地栈'}
        return delivery.request(self.ledger,target,provenance,command_id=identifier)

    def test_recovery_revalidates_new_head_without_replaying_git_or_changing_history(self):
        request,path,raw=self.failed_delivery()
        actions=delivery.next_actions(self.ledger.snapshot(),'run-one')
        self.assertEqual('investigate_final_delivery',actions[0]['action'])
        from workflow_decision_context import _contract
        self.assertFalse(_contract('COORDINATOR',actions[0]['action'])['executable'])
        head=self.fix_and_push()
        r=self.assertCommitted(self.recovery(request,head))
        self.assertEqual('DUPLICATE',self.recovery(request,head)['status'])
        self.assertEqual('PENDING',delivery.states(self.ledger.snapshot(),'run-one')[0]['state'])
        self.assertFalse(delivery.summary(self.ledger.snapshot(),'run-one')[0]['push_verified'])
        result=delivery.execute(self.ledger,self.coord,{'request_id':request['request_id']},command_id='resume')
        self.assertEqual('COMPLETED',result['status'],result)
        self.assertEqual(head,self.git('rev-parse','HEAD'))
        self.assertEqual(head,self.git('rev-parse','main',cwd=self.remote))
        self.assertEqual(raw,path.read_bytes())
        row=delivery.states(self.ledger.snapshot(),'run-one')[0]
        self.assertEqual('BLOCKED',row['stages'][-1]['state'])
        self.assertEqual('COMPLETED',row['state'])
        self.assertEqual(4,len(row['recovery_stages']))
        self.assertEqual(hashlib.sha256(raw).hexdigest(),row['recovery']['previous_failure']['sha256'])
        summary=delivery.summary(self.ledger.snapshot(),'run-one')[0]
        self.assertEqual(6,len(summary['completed_stages']))
        self.assertTrue(summary['push_verified'])
        self.assertEqual(head,summary['stages'][0]['head'])
        calls=(self.repo/'.local/isolated-stages').read_bytes()
        again=delivery.execute(self.ledger,self.coord,{'request_id':request['request_id']},command_id='again')
        self.assertEqual('COMPLETED',again['status'])
        self.assertEqual(calls,(self.repo/'.local/isolated-stages').read_bytes())
        self.assertEqual([],delivery.next_actions(self.ledger.snapshot(),'run-one'))
        self.assertEqual(r['recovery_id'],row['recovery']['recovery_id'])

    def test_recovery_requires_clean_exact_descendant_and_matching_remote(self):
        request,_,_=self.failed_delivery()
        (self.repo/'dirty').write_text('user change')
        with self.assertRaisesRegex(ValueError,'DIRTY_REPOSITORY'):
            self.recovery(request,self.git('rev-parse','HEAD'))
        (self.repo/'dirty').unlink()
        head=self.fix_and_push()
        with self.assertRaisesRegex(ValueError,'TARGET_CHANGED'):
            self.recovery(request,self.main)
        self.git('update-ref','refs/heads/main',self.main,cwd=self.remote)
        with self.assertRaisesRegex(ValueError,'REMOTE_NOT_ALIGNED'):
            self.recovery(request,head,identifier='second-recovery')
        self.git('update-ref','refs/heads/main',head,cwd=self.remote)
        self.assertCommitted(self.recovery(request,head))
        self.git('commit','--allow-empty','-qm','unapproved target advance')
        result=delivery.execute(self.ledger,self.coord,{'request_id':request['request_id']},command_id='drift')
        self.assertEqual('FINAL_DELIVERY_TARGET_CHANGED',result['code'])
        self.assertEqual(2,len((self.repo/'.local/isolated-stages').read_text().splitlines()))
        # 未启动任何本地步骤的版本漂移，可用新的精确授权重绑；不形成新的永久死路。
        advanced=self.git('rev-parse','HEAD');self.git('push','-q','github','main')
        self.assertCommitted(self.recovery(request,advanced,identifier='advanced-recovery'))
        resumed=delivery.execute(self.ledger,self.coord,{'request_id':request['request_id']},command_id='resume-advanced')
        self.assertEqual('COMPLETED',resumed['status'],resumed)

    def test_unauthenticated_and_nonimperative_recovery_rejected(self):
        request,_,_=self.failed_delivery();head=self.fix_and_push()
        target={'request_id':request['request_id'],'expected_head':head,'reason':'fix'}
        for text in ('为什么恢复最终交付 '+request['request_id']+' main '+head+' 本地栈？',
                     '他说恢复最终交付 '+request['request_id']+' main '+head+' 本地栈'):
            with self.assertRaisesRegex(ValueError,'EXPLICIT_FINAL_DELIVERY_RECOVERY_SOURCE_REQUIRED'):
                delivery.request(self.ledger,target,{**self.provenance,'primary_text':text},command_id='bad')
        with self.assertRaisesRegex(ValueError,'NATIVE_FINAL_DELIVERY_SOURCE_REQUIRED'):
            delivery.request(self.ledger,target,{**self.provenance,'authenticated':False},command_id='bad-auth')
        from workflow_contracts import control_target_schema
        from jsonschema import Draft202012Validator
        self.assertFalse(list(Draft202012Validator(control_target_schema()).iter_errors(target)))

    def test_interrupted_recovery_stays_unverified_and_does_not_restart_local_step(self):
        from workflow_delivery_recovery import _record
        request,path,raw=self.failed_delivery();head=self.fix_and_push()
        self.assertCommitted(self.recovery(request,head))
        row=delivery.states(self.ledger.snapshot(),'run-one')[0]
        _record(self.ledger,self.coord,row,'reconcile',{'state':'READY','head':head})
        _record(self.ledger,self.coord,row,'local_up',{'state':'RUNNING'},started=True)
        result=delivery.execute(self.ledger,self.coord,{'request_id':request['request_id']},command_id='interrupted')
        self.assertEqual('UNVERIFIED',result['state'])
        self.assertEqual(raw,path.read_bytes())
        self.assertEqual(2,len((self.repo/'.local/isolated-stages').read_text().splitlines()))
        with self.assertRaisesRegex(ValueError,'TERMINAL_LOCAL_FAILURE_REQUIRED'):
            self.recovery(request,head,identifier='second-recovery')

    def test_failure_receipt_tampering_prevents_recovery(self):
        request,path,_=self.failed_delivery();head=self.fix_and_push()
        path.write_text(json.dumps({'status':'SUCCEEDED','exit_code':0}))
        with self.assertRaisesRegex(ValueError,'TERMINAL_FAILURE_RECEIPT_REQUIRED'):
            self.recovery(request,head)

    def test_native_recovery_control_uses_existing_action_and_exact_target(self):
        from workflow_runtime import submit_control
        request,_,_=self.failed_delivery();head=self.fix_and_push()
        target={'request_id':request['request_id'],'expected_head':head,'reason':'本地修复已冻结，重验'}
        source={**self.provenance,'message_id':'native-recovery',
            'primary_text':'恢复最终交付 '+request['request_id']+' main '+head+' 并验证本地栈'}
        pool=self.root/'pool.json';pool.write_text(json.dumps({'business_ledger':str(self.path)}))
        with mock.patch('workflow_runtime.open_ledger',return_value=self.ledger):
            result=submit_control(pool,'COORDINATOR','request_final_delivery',target,source)
        self.assertCommitted(result)
        self.assertEqual('finalize_delivery',delivery.next_actions(self.ledger.snapshot(),'run-one')[0]['action'])

    def test_same_head_recovery_failure_requires_new_facts_and_preserves_both_attempts(self):
        request,path,raw=self.failed_delivery();head=self.git('rev-parse','HEAD')
        self.assertCommitted(self.recovery(request,head))
        result=delivery.execute(self.ledger,self.coord,{'request_id':request['request_id']},command_id='recovery-fails')
        self.assertEqual('BLOCKED',result['state']);self.assertEqual(7,result['exit_code'])
        self.assertEqual(raw,path.read_bytes())
        with self.assertRaisesRegex(ValueError,'SAME_HEAD_ALREADY_REQUESTED'):
            self.recovery(request,head,identifier='repeat-without-new-facts')
        # 合成执行器版本变更，明确区分真实维护后的新授权与相同前提重试。
        with mock.patch('workflow_delivery_recovery.executor_digest',return_value='1'*64):
            second=self.assertCommitted(self.recovery(request,head,identifier='new-executor'))
        row=delivery.states(self.ledger.snapshot(),'run-one')[0]
        self.assertEqual(1,len(row['recovery_history']))
        self.assertEqual('BLOCKED',row['recovery_history'][0]['state'])
        self.assertNotEqual(row['recovery_history'][0]['recovery']['recovery_id'],second['recovery_id'])
        changed=delivery.execute(self.ledger,self.coord,{'request_id':request['request_id']},command_id='executor-drift')
        self.assertEqual('FINAL_DELIVERY_EXECUTOR_CHANGED',changed['code'])

    def test_native_delivery_strips_inherited_python_and_pytest_overrides(self):
        self.git('checkout','feat/delivery')
        gate=self.repo/'scripts/local_quality_gate.sh'
        gate.write_text('#!/bin/sh\ntest -z "${PYTHONPATH+x}" && test -z "${PYTHONHOME+x}" && '
            'test -z "${PYTEST_ADDOPTS+x}" && test "$PYTHONDONTWRITEBYTECODE" = 1\n')
        self.git('add','scripts');self.git('commit','-qm','isolated environment assertions')
        self.final['target']['head']=self.git('rev-parse','HEAD')
        request=self.request()
        with mock.patch.dict(os.environ,{'PYTHONPATH':'/isolated/wrong-tests','PYTHONHOME':'/wrong-python',
                'PYTEST_ADDOPTS':'-k no_test_selected'}):
            result=delivery.execute(self.ledger,self.coord,{'request_id':request['request_id']},command_id='native-env')
        self.assertEqual('COMPLETED',result['status'],result)

    def test_recovery_strips_inherited_python_and_pytest_overrides(self):
        request,_,_=self.failed_delivery()
        (self.repo/'scripts/local_quality_gate.sh').write_text('#!/bin/sh\ntest -z "${PYTHONPATH+x}" && '
            'test -z "${PYTHONHOME+x}" && test -z "${PYTEST_ADDOPTS+x}"\n')
        self.git('add','scripts');self.git('commit','-qm','isolated environment fix')
        self.git('push','-q','github','main');head=self.git('rev-parse','HEAD')
        self.assertCommitted(self.recovery(request,head))
        with mock.patch.dict(os.environ,{'PYTHONPATH':'/isolated/wrong-tests','PYTHONHOME':'/wrong-python',
                'PYTEST_ADDOPTS':'-k no_test_selected'}):
            result=delivery.execute(self.ledger,self.coord,{'request_id':request['request_id']},command_id='recovery-env')
        self.assertEqual('COMPLETED',result['status'],result)

    def test_current_report_shows_delivery_responsibility_and_recorded_issue_disposition(self):
        from workflow_reports import render_live_progress_fallback,_coordinator_progress_format_error
        from workflow_query import progress_context
        s=copy.deepcopy(self.ledger.snapshot())
        issue={'id':'i1','task_id':None,'summary':'historical failure'}
        f={**self.final,'report':{'event_id':900,'created_at':0,'payload':{'result':'PASS','issues':[issue]}}}
        s['events'].append({'event_id':123,'run_id':'run-one','kind':'REQUIREMENT_COMPLETED',
            'payload':{'issue_dispositions':[{'issue_id':'i1',
            'reason':'已核本地通过；真实MySQL并发与NAS实物仍未验证，待独立授权'}]}})
        with mock.patch('workflow_completion.facts',return_value=f):
            context=progress_context(s,'run-one','COORDINATOR')
        self.assertEqual(123,context['completion']['disposition_event_id'])
        packet={'run':{'requirement_id':'isolated'},'tasks':[],'roles':[],
            'repository':{'state':'OBSERVED','branch':'main','head':'a'*40,'dirty':False},
            'completion':{'state':'COMPLETED'},'final_deliveries':[{'request_id':'r1','state':'BLOCKED','stage':'quality_gate'}]}
        with mock.patch('workflow_reports._read_progress_context',return_value=context):
            text=render_live_progress_fallback(packet,'COORDINATOR')
        self.assertIn('最终交付接续',text)
        self.assertIn('当次仓库',text)
        self.assertIn('回执版本',text)
        self.assertIn('验收处置 i1',text)
        self.assertIn('真实MySQL并发与NAS实物仍未验证',text)
        self.assertNotIn('没有未完成任务记录',text.split('**下一步计划**',1)[1])
        self.assertNotIn('None：',text)
        self.assertIsNone(_coordinator_progress_format_error(text))
