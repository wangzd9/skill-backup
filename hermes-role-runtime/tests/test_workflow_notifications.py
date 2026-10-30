"""Real isolated ledger outbox with the original transport boundary injected."""
from datetime import datetime
import importlib.util
import json
from pathlib import Path
import sys
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT/'tests', Path('/home/wang/.hermes/hermes-agent')):
    sys.path.insert(0, str(directory))
from test_business_ledger import LedgerFixture
import workflow_runtime as workflow


class BusinessNotificationTests(LedgerFixture):
    def setUp(self):
        super().setUp()
        self.pool_path = self.root/'isolated.json'
        self.home = self.root/'home'
        (self.home/'cron').mkdir(parents=True)
        self.pool_path.write_text(json.dumps({'roles': {role: {
            'chat_id': 'chat-'+role, 'poll_job_id': role+'-poll',
            'report_job_id': role+'-report'} for role in workflow.ROLES}}))
        self.jobs = self.home/'cron/jobs.json'
        self.jobs.write_text(json.dumps({'jobs': [{'id': role+'-poll', 'paused': True,
            'enabled': True, 'role_pool': {'kind': 'business', 'role': role}}
            for role in workflow.ROLES]}))
        patch = mock.patch('workflow_runtime.open_ledger', return_value=self.ledger)
        patch.start(); self.addCleanup(patch.stop)

    def tick(self, transport):
        return workflow.event_tick(self.pool_path, home=self.home,
            dispatcher=lambda job: self.fail('paused poll must never dispatch a model'),
            notification_transport=transport)

    def test_tick_sends_one_claim_sentence_once_with_native_receipt_and_preserves_pause(self):
        self.bind('A')
        before = self.ledger.snapshot()
        jobs_before = self.jobs.read_bytes()
        calls = []

        def send(row):
            calls.append(row)
            return {'delivered': True, 'message_id': 'offline-native-message'}

        result = self.tick(send)
        self.tick(send)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['chat_id'], 'chat-A')
        self.assertEqual(calls[0]['text'], 'A 已领取 T1，第 1 次开发。')
        notice = self.ledger.outbox_items(include_terminal=True)[0]
        self.assertEqual(notice['state'], 'SENT')
        self.assertEqual(notice['receipt']['source'], 'native_hermes_send')
        self.assertEqual(notice['receipt']['message_id'], 'offline-native-message')
        self.assertEqual(before['event_high_water'], self.ledger.snapshot()['event_high_water'])
        self.assertEqual(jobs_before, self.jobs.read_bytes())
        self.assertEqual(result['dispatch']['COORDINATOR']['reason'], 'JOB_PAUSED_OR_UNAVAILABLE')

    def test_event_delivery_respects_quota_and_hung_transport_is_not_retried(self):
        import threading,time
        from workflow_notifications import drain_business_notifications
        self.bind('A')
        event=self.ledger.snapshot()['events'][-1]['event_id']
        for number in range(12):
            self.assertCommitted(self.ledger.queue_notification('WORK_CLAIMED',
                {'task_id':'T1','attempt':1},command_id='notice-'+str(number),source_event_id=event,
                run_id='run-one',role='A'))
        calls=[]
        result=drain_business_notifications(self.pool_path,transport=lambda e:calls.append(e) or
                {'delivered':True,'message_id':'fixture-'+str(len(calls))},max_messages=3,max_seconds=3)
        self.assertEqual(len(calls),3)
        self.assertEqual(len(result),3)
        release=threading.Event();entered=threading.Event()
        self.addCleanup(release.set)
        def hung(event):
            calls.append(event);entered.set();release.wait(5)
            return {'delivered':True,'message_id':'late-fixture'}
        result=drain_business_notifications(self.pool_path,transport=hung,max_seconds=.1)
        self.assertTrue(entered.wait(1));self.assertEqual(result[0]['state'],'IN_FLIGHT')
        for _ in range(3): drain_business_notifications(self.pool_path,transport=hung,max_seconds=.1)
        self.assertEqual(len(calls),4)
        release.set()
        deadline=time.monotonic()+3
        while time.monotonic()<deadline and self.ledger.outbox_items(states=('SENDING',)):
            time.sleep(.01)
        self.assertFalse(self.ledger.outbox_items(states=('SENDING',)))

    def test_unknown_delivery_never_calls_transport_twice(self):
        self.bind('A')
        calls = []

        def unknown(row):
            calls.append(row)
            return {'delivered': False}

        self.tick(unknown)
        self.tick(unknown)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.ledger.outbox_items()[0]['state'], 'UNKNOWN')

    def test_sending_notice_is_not_replayed_by_tick(self):
        self.bind('A')
        notice = self.ledger.outbox_items()[0]
        self.assertCommitted(self.ledger.claim_delivery(notice['notification_id'], command_id='native-inflight'))
        self.tick(lambda row: self.fail('SENDING outcome is unknown and cannot resend'))
        self.assertEqual(self.ledger.outbox_items()[0]['state'], 'SENDING')

    def test_periodic_outbox_is_only_for_its_native_report_outlet(self):
        before = self.ledger.snapshot()['event_high_water']
        queued = self.assertCommitted(self.ledger.queue_notification('PERIODIC_SUMMARY',
            {'text': '本轮模型分析时段总结'}, command_id='periodic', source_event_id=before,
            run_id='run-one', role='A', notification_id='periodic-fixture'))
        self.tick(lambda row: self.fail('tick must not consume the native periodic outlet'))
        notice = next(row for row in self.ledger.outbox_items() if row['notification_id'] == queued['notification_id'])
        self.assertEqual(notice['state'], 'PENDING')
        self.assertEqual(self.ledger.snapshot()['event_high_water'], before)

    def test_night_claim_is_suppressed_without_transport(self):
        self.now = datetime(2026, 10, 19, 22, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        self.bind('A')
        self.tick(lambda row: self.fail('night silence must not send'))
        self.assertEqual(self.ledger.outbox_items()[0]['state'], 'SUPPRESSED')

    def test_native_business_final_is_suppressed_while_periodic_model_path_remains(self):
        spec = importlib.util.spec_from_file_location('business_notification_plugin_test', ROOT/'__init__.py')
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module; spec.loader.exec_module(module)
        plugin = module.RuntimePlugin(registry=self.root/'registry', home=self.home,
            metrics_url='http://unused', pool_path=self.pool_path)
        with mock.patch.object(plugin, '_frozen_scope', return_value=None):
            result = plugin.transform_cron_report(job={'role_pool': {'kind': 'business'}},
                execution_id='isolated-execution', final_response='未经结构化的模型输出')
        self.assertEqual(result, {'content': '', 'suppress': True,
            'metadata': {'reason': 'BUSINESS_EVENT_OUTBOX_ONLY'}})
        self.assertIsNone(plugin.transform_cron_report(job={'role_pool': {'kind': 'report'}, 'no_agent': True},
            execution_id='status-brief', final_response='A：空闲'))
