"""隔离交付合同回归：真实Git/bare remote，显式合成已验收输入及本地栈替身。"""
import json
from pathlib import Path
import subprocess
from unittest import mock

from test_business_ledger import LedgerFixture
import workflow_final_delivery as delivery


class FinalGitDeliveryTests(LedgerFixture):
    def setUp(self):
        super().setUp()
        self.cp,self.coord=self.bind('COORDINATOR')
        self.git('branch','-M','main')
        scripts=self.repo/'scripts';scripts.mkdir()
        for name in ('local_stack.sh','local_quality_gate.sh'):
            p=scripts/name
            p.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> .local/isolated-stages\n')
            p.chmod(0o755)
        (self.repo/'.gitignore').write_text('.local/\n')
        self.git('add','scripts','.gitignore');self.git('commit','-qm','isolated local profile fixture')
        self.git('checkout','-qb','feat/delivery')
        (self.repo/'feature.txt').write_text('accepted isolated feature\n')
        self.git('add','feature.txt');self.git('commit','-qm','feature')
        self.feature=self.git('rev-parse','HEAD')
        self.git('checkout','main')
        (self.repo/'main-note.txt').write_text('independent main doc\n')
        self.git('add','main-note.txt');self.git('commit','-qm','main doc')
        self.main=self.git('rev-parse','HEAD')
        self.remote=self.root/'github.git'
        subprocess.run(['git','init','--bare','-q',str(self.remote)],check=True)
        self.git('remote','add','github',str(self.remote));self.git('push','-q','github','main')
        (self.repo/'.local').mkdir()
        # Synthetic acceptance is isolated to this test. Real completion gate
        # tests live in test_workflow_completion; no production records are made.
        self.final={'state':'COMPLETED','completion_event_id':123,
                    'target':{'head':self.feature,'branch':'feat/delivery'}}
        self.accepted=mock.patch('workflow_completion.facts',return_value=self.final)
        self.accepted.start();self.addCleanup(self.accepted.stop)
        self.target={'run_id':'run-one','source_branch':'feat/delivery','target_branch':'main','remote':'github'}
        self.provenance={'platform':'feishu','authenticated':True,'chat_id':'isolated','user_id':'fixture',
            'message_id':'message-1','primary_text':'合入 feat/delivery 到 main 并 push github，然后更新本地测试环境'}

    def request(self,identifier='request'):
        return self.assertCommitted(delivery.request(self.ledger,self.target,self.provenance,command_id=identifier))

    def test_diverged_real_merge_push_local_steps_and_idempotence(self):
        r=self.request();self.assertEqual('DIVERGED',r['frozen']['relation']);self.assertEqual([],r['frozen']['overlap'])
        self.assertEqual('DUPLICATE',delivery.request(self.ledger,self.target,self.provenance,command_id='request')['status'])
        actions=delivery.next_actions(self.ledger.snapshot(),'run-one')
        self.assertEqual('finalize_delivery',actions[0]['action'])
        result=delivery.execute(self.ledger,self.coord,{'request_id':r['request_id']},command_id='execute')
        self.assertEqual('COMPLETED',result['status'],result)
        head=self.git('rev-parse','main')
        self.assertEqual(2,len(self.git('rev-list','--parents','-n','1',head).split())-1)
        self.assertEqual(head,self.git('rev-parse','refs/heads/main',cwd=self.remote))
        self.assertEqual(['--mode mock up','--mode mock test',''],(self.repo/'.local/isolated-stages').read_text().splitlines())
        again=delivery.execute(self.ledger,self.coord,{'request_id':r['request_id']},command_id='changed-command')
        self.assertEqual('COMPLETED',again['status']);self.assertFalse(again['replay_allowed'])
        self.assertEqual([],delivery.next_actions(self.ledger.snapshot(),'run-one'))
        compact=delivery.summary(self.ledger.snapshot(),'run-one')[0]
        self.assertEqual(6,compact['stage_count'])
        self.assertEqual(list(delivery.STAGES),compact['completed_stages'])
        self.assertLess(len(json.dumps(compact).encode()),1000)
        self.assertNotIn('evidence',compact['stages'][0])
        from workflow_query import live_progress
        with mock.patch('workflow_runtime.open_ledger',return_value=self.ledger):
            live=live_progress(self.root/'isolated-pool.json','COORDINATOR')
        self.assertLess(len(json.dumps(live,ensure_ascii=False).encode()),8192)
        self.assertEqual('COMPLETED',live['final_deliveries'][0]['state'])
        self.assertEqual(head,live['final_deliveries'][0]['head'])
        self.assertNotIn('stages',live['final_deliveries'][0])
        self.assertTrue(live['final_deliveries'][0]['push_verified'])
        self.assertEqual(6,live['final_deliveries'][0]['completed_stage_count'])
        self.assertEqual(head,self.git('rev-parse','main'))

    def test_remote_advance_stops_before_merge_and_push(self):
        r=self.request();other=self.root/'other'
        subprocess.run(['git','clone','-q','--branch','main',str(self.remote),str(other)],check=True)
        self.git('config','user.email','isolated@example.invalid',cwd=other);self.git('config','user.name','isolated',cwd=other)
        (other/'new.txt').write_text('new remote owner commit\n')
        self.git('add','new.txt',cwd=other);self.git('commit','-qm','advance',cwd=other);self.git('push','-q','origin','main',cwd=other)
        result=delivery.execute(self.ledger,self.coord,{'request_id':r['request_id']},command_id='execute')
        self.assertEqual('FINAL_DELIVERY_REMOTE_MAIN_CHANGED',result['code'])
        self.assertEqual(self.main,self.git('rev-parse','main'))
        self.assertEqual('BLOCKED',delivery.states(self.ledger.snapshot(),'run-one')[0]['state'])

    def test_failed_quality_preserves_push_and_blocks_success(self):
        # Fixture failure is committed before consent; never disguise it as a
        # passing test, and never rewrite a real result.
        self.git('checkout','feat/delivery')
        (self.repo/'scripts/local_quality_gate.sh').write_text('#!/bin/sh\nexit 7\n')
        self.git('add','scripts');self.git('commit','-qm','isolated failing quality gate')
        self.final['target']['head']=self.git('rev-parse','HEAD')
        self.git('checkout','main')
        # Scripts differ between branches; request from accepted source checkout.
        self.git('checkout','feat/delivery')
        r=self.request()
        result=delivery.execute(self.ledger,self.coord,{'request_id':r['request_id']},command_id='execute')
        self.assertEqual('BLOCKED',result['state']);self.assertEqual('FINAL_DELIVERY_QUALITY_GATE_FAILED',result['code'])
        self.assertEqual(7,result['exit_code']);self.assertIn('sha256',result['evidence'])
        self.assertEqual(self.git('rev-parse','main'),self.git('rev-parse','refs/heads/main',cwd=self.remote))
        self.assertEqual('BLOCKED',delivery.execute(self.ledger,self.coord,{'request_id':r['request_id']},command_id='again')['status'])

    def test_interruption_does_not_replay_side_effect(self):
        r=self.request()
        delivery._record(self.ledger,self.coord,r['request_id'],'fetch',{'state':'RUNNING'},started=True)
        result=delivery.execute(self.ledger,self.coord,{'request_id':r['request_id']},command_id='execute')
        self.assertEqual('UNVERIFIED',result['state']);self.assertFalse(result['replay_allowed'])
        self.assertEqual(self.main,self.git('rev-parse','main'))

    def test_authentication_completion_and_conflict_guards(self):
        for text in ('为什么不能合入 feat/delivery 到 main 并 push github 然后更新本地测试环境？',
                     '他说合入 feat/delivery 到 main 并 push github 然后更新本地测试环境',
                     '合入 feat/delivery 到 main',
                     '合入 feat/delivery 到 main 并 push origin，然后更新本地测试环境'):
            with self.assertRaises(ValueError):delivery.request(self.ledger,self.target,{**self.provenance,'primary_text':text},command_id='bad')
        with self.assertRaises(ValueError):delivery.request(self.ledger,self.target,{**self.provenance,'authenticated':False},command_id='bad-auth')
        with mock.patch('workflow_completion.facts',return_value={'state':'READY_FOR_CLOSURE'}):
            with self.assertRaisesRegex(ValueError,'FINAL_DELIVERY_REQUIREMENT_NOT_COMPLETED'):
                delivery.request(self.ledger,self.target,self.provenance,command_id='not-completed')
        self.git('checkout','feat/delivery')
        (self.repo/'main-note.txt').write_text('conflicting feature\n')
        self.git('add','main-note.txt');self.git('commit','-qm','conflicting fixture')
        self.final['target']['head']=self.git('rev-parse','HEAD')
        with self.assertRaisesRegex(ValueError,'FINAL_DELIVERY_MERGE_CONFLICT_OR_PREVIEW_FAILED'):
            delivery.request(self.ledger,self.target,self.provenance,command_id='conflict')
        self.assertEqual(self.main,self.git('rev-parse','main'))

    def test_native_control_has_separate_message_slot_and_role_contract(self):
        from workflow_runtime import submit_control
        from workflow_contracts import control_target_schema,validate_payload
        from workflow_decision_context import _contract
        from jsonschema import Draft202012Validator
        self.assertFalse(list(Draft202012Validator(control_target_schema()).iter_errors(self.target)))
        validate_payload('finalize_delivery',{'request_id':'test'})
        self.assertTrue(_contract('COORDINATOR','finalize_delivery')['executable'])
        with self.assertRaises(ValueError):_contract('TESTER','finalize_delivery')
        pool=self.root/'pool.json';pool.write_text(json.dumps({'business_ledger':str(self.path)}))
        with mock.patch('workflow_runtime.open_ledger',return_value=self.ledger):
            self.ledger._command('isolated-previous-rejected-action','message-control:isolated:message-1',[],lambda db: {'isolated':True})
            result=submit_control(pool,'COORDINATOR','request_final_delivery',self.target,self.provenance)
        self.assertEqual('COMMITTED',result['status'],result)

    def test_ff_dirty_and_source_change_guards(self):
        self.git('checkout','-qb','feat/ff')
        (self.repo/'ff.txt').write_text('isolated fast-forward\n')
        self.git('add','ff.txt');self.git('commit','-qm','ff fixture')
        self.final['target']={'head':self.git('rev-parse','HEAD'),'branch':'feat/ff'}
        self.target['source_branch']='feat/ff'
        self.provenance['primary_text']='合入 feat/ff 到 main 并 push github，然后更新本地测试环境'
        dirty=self.repo/'dirty.txt';dirty.write_text('pre-existing owner change\n')
        with self.assertRaisesRegex(ValueError,'DIRTY_REPOSITORY'):self.request()
        dirty.unlink()
        r=self.request();self.assertEqual('FF',r['frozen']['relation'])
        (self.repo/'ff.txt').write_text('new unaccepted head\n')
        self.git('add','ff.txt');self.git('commit','-qm','source changed after consent')
        result=delivery.execute(self.ledger,self.coord,{'request_id':r['request_id']},command_id='execute')
        self.assertEqual('FINAL_DELIVERY_SOURCE_CHANGED',result['code'])
        self.assertEqual(self.main,self.git('rev-parse','main'))

    def test_succeeded_operation_receipt_reconciles_without_reexecution(self):
        from runtime_common import save_json
        r=self.request();rid=r['request_id']
        delivery._record(self.ledger,self.coord,rid,'fetch',{'state':'RUNNING'},started=True)
        path=self.ledger.path.parent/'final-deliveries'/rid/'fetch.json'
        save_json(path,{'status':'SUCCEEDED','exit_code':0,'isolated_synthetic_receipt':True})
        result=delivery.execute(self.ledger,self.coord,{'request_id':rid},command_id='recover')
        self.assertEqual('COMPLETED',result['status'],result)
        self.assertTrue(json.loads(path.read_text())['isolated_synthetic_receipt'])
