"""ACTIVE reporting uses the admitted native execution and sole outbox/channel."""
from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
CORE = Path('/home/wang/.hermes/hermes-agent')
for folder in (ROOT, CORE):
    sys.path.insert(0, str(folder))

from business_ledger import BusinessLedger
import report_summary
import workflow_runtime as workflow
from workflow_reports import (transform_native_report, acknowledge_native_report,
    COORDINATOR_SECTIONS, PERSONAL_SECTIONS)


class NativeWorkflowReports(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root/'home'
        (self.home/'cron').mkdir(parents=True)
        home_patch = mock.patch.dict(os.environ, {'HERMES_HOME': str(self.home)})
        home_patch.start(); self.addCleanup(home_patch.stop)
        self.pool = self.root/'pool.json'
        self.pool.write_text(json.dumps({'roles': {role: {'chat_id': role, 'poll_job_id': role+'-poll',
            'report_job_id': role+'-report', 'morning_report_job_id': role+'-morning'} for role in workflow.ROLES}}))
        self.at = datetime(2026, 10, 19, 15, tzinfo=ZoneInfo('Asia/Shanghai'))
        self.ledger = BusinessLedger(workflow.ledger_path(self.pool), clock=lambda: self.at.timestamp())
        self.run = self.ledger.create_run('requirement', self.root, self.root, ['T1'], command_id='run')['run_id']
        self.ledger.select_run(self.run, command_id='select')
        self.ledger.set_mode('ACTIVE', command_id='activate')
        self.connection = sqlite3.connect(self.home/'cron/executions.db')
        self.addCleanup(self.connection.close)
        self.connection.execute('CREATE TABLE executions(id TEXT,job_id TEXT,pid INTEGER,status TEXT)')
        self.execution = 'native-report-1'
        self.connection.execute('INSERT INTO executions VALUES(?,?,?,?)',
            (self.execution, 'A-report', os.getppid(), 'running'))
        self.connection.commit()
        self.patch = mock.patch('workflow_runtime.open_ledger', return_value=self.ledger)
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.scope = {'kind': 'report', 'workflow_version': 1, 'pool_path': str(self.pool), 'role': 'A',
            'execution_id': self.execution, 'job_id': 'A-report', 'run_id': self.run}
        self.identity = {'session_id': 'native-session', 'model': 'offline', 'model_request_id': 'request-real'}

    def build(self, role='A', kind='phase'):
        with mock.patch.dict(os.environ, {'HERMES_ROLE_RUN_ID': self.run}):
            return report_summary.build_summary_input(self.root, role, kind, at=self.at,
                home=self.home, pool_path=self.pool)

    def output(self, packet, *, coordinator=False):
        value = {'evidence_ids': packet['facts']['facts']['evidence_ids']}
        if coordinator:
            value.update(accomplishments='本时段已核对需求登记，尚无业务交付。',
                issues='尚无独立测试或审查结果，需要保留当前门禁。',
                next_steps='按已接受需求继续，取得结果后再裁决。')
            value['sections'] = {name: '- **状态：** 当前仅已核对需求登记，其他结果待核实。'
                                 for name in COORDINATOR_SECTIONS}
        else:
            value['sections'] = {name: ['**状态：** 当前无新增事实，待核实。'] for name in
                ('当前工作', '本次完成', '阻塞与依赖', '下一步计划')}
        return json.dumps(value, ensure_ascii=False)

    def test_native_result_claims_one_outbox_and_unknown_delivery_never_resends(self):
        before = self.ledger.snapshot()
        packet = self.build()
        self.assertFalse((self.root/'ROLE-BINDINGS.json').exists())
        self.assertEqual(self.execution, packet['facts']['generation']['execution']['execution_id'])
        first = transform_native_report(self.scope, self.execution, self.output(packet), self.identity)
        self.assertFalse(first['suppress'])
        self.assertIn('**当前工作**', first['content'])
        self.assertIn('**本次完成**', first['content'])
        self.assertIn('usage覆盖 未知', first['content'])
        self.assertNotIn('**执行者进度**', first['content'])
        self.assertEqual('GENERATED', report_summary.summary_status(packet['artifact'])['state'])
        receipt = acknowledge_native_report(first['metadata'], execution_id=self.execution,
            outcome='delivered', attempted=True, unverified_targets=['feishu:chat'])
        self.assertEqual('UNKNOWN', receipt['state'])
        second = transform_native_report(self.scope, self.execution, self.output(packet), self.identity)
        self.assertTrue(second['suppress'])
        after = self.ledger.snapshot()
        for name in ('works', 'executions', 'tasks', 'event_high_water'):
            self.assertEqual(before[name], after[name])
        self.assertEqual(1, len(after['outbox']))

    def test_same_window_artifacts_are_native_attempt_scoped_but_notification_is_shared(self):
        first = self.build()
        frozen = Path(first['artifact']).read_bytes()
        self.ledger.create_run('unrelated', self.root/'other', self.root, ['X'], command_id='other')
        self.assertEqual(frozen, Path(self.build()['artifact']).read_bytes())
        self.connection.execute('UPDATE executions SET id=?', ('native-report-2',)); self.connection.commit()
        second = self.build()
        self.assertNotEqual(first['artifact'], second['artifact'])
        one = report_summary.complete_native_summary(first['artifact'], self.output(first), **{
            'model_request_id': 'one', 'model': 'offline'})
        two = report_summary.complete_native_summary(second['artifact'], self.output(second), **{
            'model_request_id': 'two', 'model': 'offline'})
        self.assertEqual(one['notification_id'], two['notification_id'])
        self.assertEqual('DUPLICATE', two['status'])

    def test_coordinator_keeps_seven_sections_and_model_failure_is_explicit(self):
        self.connection.execute('UPDATE executions SET job_id=?', ('COORDINATOR-morning',)); self.connection.commit()
        packet = self.build('COORDINATOR', 'morning')
        result = report_summary.complete_native_summary(packet['artifact'], self.output(packet, coordinator=True),
            model_request_id='morning-real', model='offline')
        for heading in COORDINATOR_SECTIONS:
            self.assertIn('**'+heading+'**', result['text'])
        self.assertIn('执行者A（空闲）', result['text'])
        self.assertIn('测试者（空闲）', result['text'])
        self.connection.execute('UPDATE executions SET job_id=?', ('A-report',)); self.connection.commit()
        bad = self.build()
        result = report_summary.complete_native_summary(bad['artifact'], '[SILENT]', model_request_id='bad', model='offline')
        self.assertEqual('FAILED', result['generation']['state'])
        self.assertEqual('REPORT_MODEL_OUTPUT_INVALID',result['generation']['reason_code'])
        self.assertIn('本次总结未生成', result['text'])
        for heading in PERSONAL_SECTIONS:
            self.assertIn('**'+heading+'**', result['text'])
        self.assertIn('- **调用分布：**', result['text'])
        self.assertNotIn('[SILENT]', result['text'])
        self.assertNotIn('REPORT_MODEL_OUTPUT_INVALID', result['text'])

    def test_output_contract_forbids_fact_metadata_and_exposes_precise_rejection(self):
        packet=self.build()
        self.assertIn('顶层键必须且只能是',packet['prompt'])
        value=json.loads(self.output(packet));value['schema_version']=1
        result=report_summary.complete_native_summary(packet['artifact'],json.dumps(value,ensure_ascii=False),
            model_request_id='invalid-metadata',model='offline')
        self.assertEqual('FAILED',result['generation']['state'])
        self.assertEqual('UNKNOWN_REPORT_ANALYSIS_FIELD',result['generation']['reason_code'])

    def test_oversized_legacy_frozen_input_is_preserved_and_reprojected(self):
        from workflow_reports import _hash
        packet=self.build()
        artifact=Path(packet['artifact'])
        frozen=json.loads(artifact.read_text())
        facts=frozen['facts'];facts.pop('facts_sha256',None)
        facts.pop('input_coverage',None)
        records=[{'event_id':1000+i,'work_id':'historical-fixture','role':'A',
                  'saved_at':self.at.isoformat(),'checkpoint':{'done':'历史叙述'*1000}}
                 for i in range(30)]
        facts['work_checkpoint_events']=records
        facts['events']=[{'event_id':r['event_id'],'kind':'CHECKPOINT_SAVED',
            'payload':{'checkpoint':r['checkpoint']}} for r in records]
        facts['evidence_ids']=['event:'+str(r['event_id']) for r in records]
        facts['facts_sha256']=_hash(facts)
        artifact.write_text(json.dumps(frozen,ensure_ascii=False))
        original=json.loads(artifact.read_text())['facts']
        rebuilt=self.build()
        view=rebuilt['facts']['facts']
        self.assertEqual(30,view['checkpoint_events_total'])
        self.assertEqual(20,len(view['work_checkpoint_events']))
        self.assertLess(len(json.dumps(view,ensure_ascii=False,separators=(',',':')).encode()),report_summary.MAX_INPUT_BYTES)
        ref=view['detail_artifacts']['original_report_facts']
        self.assertEqual(original,json.loads(Path(ref['path']).read_text()))
        result=report_summary.complete_native_summary(rebuilt['artifact'],self.output(rebuilt),
                    model_request_id='reprojected',model='offline')
        self.assertEqual('GENERATED',result['generation']['state'])

    def test_plugin_registered_callbacks_use_frozen_report_scope_and_fail_closed(self):
        spec = importlib.util.spec_from_file_location('native_report_plugin_test', ROOT/'__init__.py')
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module; spec.loader.exec_module(module)
        plugin = module.RuntimePlugin(registry=self.root/'registry', home=self.home, metrics_url='http://unused', pool_path=self.pool)
        packet = self.build()
        with mock.patch.object(plugin, '_frozen_scope', return_value=self.scope):
            result = plugin.transform_cron_report(job={'role_pool': {'kind': 'report'}}, execution_id=self.execution,
                final_response=self.output(packet), agent_identity=self.identity)
        self.assertFalse(result['suppress'])
        with mock.patch.object(plugin, '_frozen_scope', side_effect=OSError('offline')):
            blocked = plugin.transform_cron_report(job={'role_pool': {'kind': 'report'}}, execution_id=self.execution,
                final_response='raw model must not escape', agent_identity=self.identity)
        self.assertTrue(blocked['suppress'])
        with mock.patch.object(plugin, '_frozen_scope', side_effect=AssertionError('status brief has no model scope')):
            self.assertIsNone(plugin.transform_cron_report(job={'role_pool': {'kind': 'report'}, 'no_agent': True},
                execution_id=self.execution, final_response='A：空闲'))
        ack = plugin.acknowledge_cron_report(metadata=result['metadata'], execution_id=self.execution,
            outcome='delivered', attempted=True, content_sha256=result['metadata']['rendered_sha256'])
        self.assertEqual('SENT', ack['state'])

    def test_queued_report_stays_unknown_until_original_queue_terminal_receipt(self):
        packet = self.build()
        transformed = transform_native_report(self.scope, self.execution, self.output(packet), self.identity)
        metadata = transformed['metadata']
        queued = acknowledge_native_report(metadata, execution_id=self.execution,
            outcome='queued', attempted=True, queued=['feishu:fixture'])
        self.assertEqual(queued['state'], 'UNKNOWN')
        delivered = acknowledge_native_report(metadata, execution_id=self.execution,
            outcome='delivered', attempted=True, stage='native_queue_terminal',
            content_sha256=metadata['rendered_sha256'])
        self.assertEqual(delivered['state'], 'SENT')
        notice = self.ledger.outbox_items(include_terminal=True)[0]
        self.assertTrue(notice['receipt']['acknowledgement_verified'])
        self.assertEqual(notice['receipt']['content_sha256'], notice['receipt']['rendered_sha256'])
        late = acknowledge_native_report(metadata, execution_id=self.execution,
            outcome='queued', attempted=True, queued=['feishu:fixture'])
        self.assertEqual(late['state'], 'SENT')

    def test_changed_native_send_text_never_becomes_verified_sent(self):
        packet = self.build()
        transformed = transform_native_report(self.scope, self.execution, self.output(packet), self.identity)
        ack = acknowledge_native_report(transformed['metadata'], execution_id=self.execution,
            outcome='delivered', attempted=True, content_sha256='0'*64)
        self.assertEqual(ack['state'], 'UNKNOWN')
        self.assertEqual(self.ledger.outbox_items()[0]['receipt']['reason'], 'DELIVERY_CONTENT_UNVERIFIED')

    def test_native_receipt_from_another_home_cannot_ack_original_report(self):
        packet = self.build()
        transformed = transform_native_report(self.scope, self.execution, self.output(packet), self.identity)
        ack = acknowledge_native_report(transformed['metadata'], execution_id=self.execution,
            outcome='delivered', attempted=True, content_sha256=transformed['metadata']['rendered_sha256'],
            home=self.root/'unrelated-home')
        self.assertEqual(ack['code'], 'REPORT_NATIVE_HOME_MISMATCH')
        self.assertEqual(self.ledger.outbox_items()[0]['state'], 'SENDING')

    def test_original_gateway_queue_late_receipt_confirms_report_once_after_unknown(self):
        from cron import delivery_queue as queue, scheduler
        from hermes_cli import plugins
        import hashlib
        packet = self.build()
        transformed = transform_native_report(self.scope, self.execution, self.output(packet), self.identity)
        metadata = transformed['metadata']
        first = acknowledge_native_report(metadata, execution_id=self.execution,
            outcome='queued', attempted=True, queued=['feishu:fixture'])
        self.assertEqual(first['state'], 'UNKNOWN')
        manager = plugins.PluginManager(scope_key=str(self.home))
        manager._hooks['post_cron_delivery'] = [lambda **kwargs: acknowledge_native_report(kwargs.pop('metadata'), **kwargs)]
        sends = []

        def send(job, text, **kwargs):
            sends.append(text)
            job['_cron_delivery_content_sha256'] = hashlib.sha256(text.encode()).hexdigest()
            return None

        with mock.patch.dict(os.environ, {'HERMES_HOME': str(self.home)}), \
                mock.patch.object(queue, 'DELIVERY_DB', self.home/'cron/deliveries.db'), \
                mock.patch.object(plugins, '_delivery_manager', return_value=manager), \
                mock.patch.object(scheduler, '_deliver_result', side_effect=send):
            queue.enqueue(self.execution, {'id': 'A-report', 'execution_id': self.execution,
                '_cron_result_hook_metadata': metadata}, transformed['content'])
            self.assertEqual(scheduler.drain_delivery_queue({}, None), 1)
            self.assertEqual(scheduler.drain_delivery_queue({}, None), 0)
            terminal = queue.get_status(self.execution)
        self.assertEqual(len(sends), 1)
        self.assertEqual(terminal['job_json'], '{}')
        self.assertEqual(terminal['content'], '')
        notice = self.ledger.outbox_items(include_terminal=True)[0]
        self.assertEqual(notice['state'], 'SENT')
        self.assertEqual(notice['receipt']['stage'], 'native_queue_terminal')


if __name__ == '__main__':
    unittest.main()
