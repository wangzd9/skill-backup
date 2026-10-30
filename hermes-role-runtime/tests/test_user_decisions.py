"""Lost short replies become durable input, without opening any business gate."""
import json
import sqlite3
from unittest import mock

from test_business_ledger import LedgerFixture
import workflow_runtime as workflow
from workflow_user_decisions import answer_source, pending, record, recover_native_answer


FULL_RULING = ('裁决：请对 work-224df688 执行 close_verification（或运行时对应的交付收口），'
    '结果记录为 UNVERIFIED / TEST_RESULT_NONDETERMINISTIC，保留 run1–run10 全部证据，不再重跑。'
    '释放 TESTER 后，先对 26cc19d（MN-05 a3）完成 independent_test；结果落账前不要 APPROVE 或合入。'
    '若当前运行时不支持 close_verification，请将工作单重绑到该独立测试任务，并保留原工作单的证据与审计记录。')


class UserDecisionTests(LedgerFixture):
    def setUp(self):
        super().setUp()
        self.pool = self.root/'isolated.json'
        self.pool.write_text(json.dumps({'roles':{'COORDINATOR':{'chat_id':'chat'}}}))
        patch = mock.patch('workflow_runtime.open_ledger', return_value=self.ledger)
        patch.start(); self.addCleanup(patch.stop)
        self.source = {'platform':'feishu','chat_id':'chat','user_id':'user','message_id':'answer-1',
                       'authenticated':True,'current_full':False,'primary_text':'按解读b'}

    def test_short_answer_is_durable_idempotent_and_does_not_approve_or_authorize(self):
        before = self.ledger.snapshot()
        first = workflow.submit_control(self.pool,'COORDINATOR','record_user_decision',{},self.source)
        self.assertCommitted(first)
        self.assertEqual('DUPLICATE',workflow.submit_control(
            self.pool,'COORDINATOR','record_user_decision',{},self.source)['status'])
        after = self.ledger.snapshot()
        self.assertEqual(before['authorizations'],after['authorizations'])
        self.assertEqual(before['decisions'],after['decisions'])
        self.assertEqual(before['tasks'],after['tasks'])
        self.assertEqual(1,len(pending(after,'run-one')))
        self.assertEqual('b',pending(after,'run-one')[0]['payload']['choice'])
        self.assertIsNone(first['decision_id'])
        self.assertIsNone(self.ledger.current_work('COORDINATOR'))

    def test_only_original_affirmative_answer_is_accepted(self):
        for text in ('为什么按解读b？','不要按解读b','如果按解读b','他说按解读b','"按解读b"',
                     '引用：按解读b','按解读b\n授权OCR','请解释按解读b'):
            self.assertIsNone(answer_source(text),text)
        with self.assertRaisesRegex(ValueError,'DECISION_REFERENCE_NOT_IN_PRIMARY_SOURCE'):
            workflow.submit_control(self.pool,'COORDINATOR','record_user_decision',
                                    {'decision_id':'b08-005'},self.source)
        with self.assertRaisesRegex(ValueError,'COORDINATOR_REQUIRED'):
            workflow.submit_control(self.pool,'A','record_user_decision',{},self.source)
        source={**self.source,'primary_text':'b08-005 按逐节点禁止新准入处置，其他节点照常。'}
        result=workflow.submit_control(self.pool,'COORDINATOR','record_user_decision',{},source)
        self.assertCommitted(result)
        self.assertEqual('b08-005',result['decision_id'])

    def test_complete_explicit_ruling_is_recognized_but_provenance_is_still_required(self):
        parsed = answer_source(FULL_RULING)
        self.assertEqual('close_verification', parsed['decision_id'])
        self.assertEqual(FULL_RULING, parsed['answer'])
        self.assertIsNone(parsed['choice'])
        unprefixed = FULL_RULING.removeprefix('裁决：')
        self.assertEqual('close_verification', answer_source(unprefixed)['decision_id'])
        self.assertIsNone(answer_source(unprefixed.replace('请对 work-', '为什么对 work-')))
        unbound = {'primary_text': FULL_RULING, 'authenticated': True, 'current_full': True,
                   'user_id': 'user', 'message_id': 'answer-2'}
        with self.assertRaisesRegex(ValueError, 'EXPLICIT_NATIVE_USER_DECISION_REQUIRED'):
            record(self.ledger, {}, unbound, command_id='missing-native-chat')
        complete = {**self.source, 'primary_text': FULL_RULING, 'message_id': 'answer-2'}
        result = workflow.submit_control(self.pool, 'COORDINATOR', 'record_user_decision', {}, complete)
        self.assertCommitted(result)
        event = pending(self.ledger.snapshot(), 'run-one')[0]['payload']
        self.assertEqual(FULL_RULING, event['answer'])
        self.assertEqual('close_verification', event['decision_id'])

    def test_unprocessed_reply_remains_next_action_after_event_ack(self):
        reply=self.assertCommitted(workflow.submit_control(self.pool,'COORDINATOR','record_user_decision',{},self.source))
        _,coord=self.bind('COORDINATOR')
        self.assertCommitted(self.ledger.work_inputs(coord,command_id='issue-events'))
        self.assertCommitted(self.ledger.checkpoint(coord,{'next':'review answer'},
            processed_through=reply['event_id'],command_id='ack-events'))
        packet=workflow.current_input(self.pool,'COORDINATOR')
        self.assertTrue(any(a['action']=='resolve_user_decision' for a in packet['next_actions']))
        scope={**coord,'kind':'business','workflow_version':1,'pool_path':str(self.pool)}
        with mock.patch('workflow_runtime.request_business_wakeups',return_value={}):
            result=workflow.command(scope,'resolve_user_decision',{
                'request_id':reply['request_id'],'decision_id':'b08-005','summary':'对照原提问和当前实现确认逐节点语义',
                'evidence':{'question':'original question reference','implementation':'frozen code reference'}},
                command_id='resolve')
        self.assertCommitted(result)
        self.assertEqual([],pending(self.ledger.snapshot(),'run-one'))
        self.assertEqual([],self.ledger.snapshot()['authorizations'])
        self.assertEqual([],self.ledger.snapshot()['decisions'])

    def test_recover_original_native_message_and_redelivery_deduplicates(self):
        state=self.root/'state.sqlite3'
        with sqlite3.connect(state) as db:
            db.execute('CREATE TABLE sessions(id,source,user_id,chat_id)')
            db.execute('CREATE TABLE messages(id,session_id,role,content,platform_message_id,timestamp)')
            db.execute('INSERT INTO sessions VALUES(?,?,?,?)',('s','feishu','user','chat'))
            db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?)',(7,'s','user',
                '[角色群只读/控制请求] 角色：COORDINATOR\n权限说明\n用户原文及附件/引用如下：\n'+FULL_RULING,
                'answer-1',123))
        result=recover_native_answer(self.pool,state,7,expected_run_id='run-one')
        self.assertCommitted(result)
        recorded = pending(self.ledger.snapshot(), 'run-one')[0]['payload']
        self.assertEqual(FULL_RULING, recorded['answer'])
        self.assertEqual('native_history_recovery', recorded['provenance']['source_kind'])
        self.assertEqual('answer-1', recorded['provenance']['message_id'])
        duplicate=recover_native_answer(self.pool,state,7,expected_run_id='run-one')
        self.assertEqual('DUPLICATE',duplicate['status'])
        self.assertEqual(result['event_id'],duplicate['event_id'])
        self.assertEqual(1,len(pending(self.ledger.snapshot(),'run-one')))
        with self.assertRaisesRegex(ValueError,'HISTORICAL_ANSWER_RUN_CHANGED'):
            recover_native_answer(self.pool,state,7,expected_run_id='different-run')
