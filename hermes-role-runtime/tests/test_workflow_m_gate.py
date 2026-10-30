"""M verifier contract fixtures only; no real model/channel acceptance is claimed."""
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import sys
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT/'tests', Path('/home/wang/.hermes/hermes-agent')):
    sys.path.insert(0, str(directory))
from test_business_ledger import LedgerFixture
import report_summary
import usage_accounting
import workflow_migration as migration
import workflow_runtime as workflow
from workflow_reports import transform_native_report, acknowledge_native_report


class MGateContractTests(LedgerFixture):
    def setUp(self):
        super().setUp()
        self.home = self.root/'home'
        (self.home/'cron').mkdir(parents=True)
        home_patch = mock.patch.dict(os.environ, {'HERMES_HOME': str(self.home)})
        home_patch.start(); self.addCleanup(home_patch.stop)
        self.pool = self.root/'isolated.json'
        self.pool.write_text(json.dumps({'roles': {role: {'chat_id': 'chat-'+role,
            'poll_job_id': role+'-poll', 'report_job_id': role+'-report'} for role in workflow.ROLES}}))
        patch = mock.patch('workflow_runtime.open_ledger', return_value=self.ledger)
        patch.start(); self.addCleanup(patch.stop)
        self.version = getattr(self, 'acceptance_version', 'isolated-verifier-fixture-not-real-model-evidence')
        process, identity = self.bind('A')
        self.scope = {**identity, 'schema_version': 2, 'workflow_version': 1, 'kind': 'business',
            'pool_path': str(self.pool), 'runtime_version_digest': self.version,
            'session_id': 'fixture-business-session', 'job_id': 'A-poll'}
        self.save_scope(self.scope)
        self.record('fixture-business-session', 'business-physical', 'business-logical',
            identity['execution_id'], 'business_cron', work_id=identity['work_id'])
        event = self.assertCommitted(self.ledger.checkpoint(identity,
            {'next': '本单真实工作结果尚待进一步验收'}, command_id='business-result'))
        self.close(process, identity)
        self.business_event = event['event_id']
        self.report = {**self.scope, 'kind': 'report', 'session_id': 'fixture-report-session',
            'execution_id': 'f'*32, 'job_id': 'A-report'}
        for key in ('work_id', 'execution_generation', 'task_id', 'attempt'):
            self.report.pop(key, None)
        self.save_scope(self.report)
        with sqlite3.connect(self.home/'cron/executions.db') as db:
            db.execute('CREATE TABLE executions(id TEXT,job_id TEXT,pid INTEGER,status TEXT,delivery_outcome TEXT)')
            db.execute('INSERT INTO executions VALUES(?,?,?,?,?)',
                (identity['execution_id'], 'A-poll', process.pid, 'completed', 'suppressed'))
            db.execute('INSERT INTO executions VALUES(?,?,?,?,?)',
                (self.report['execution_id'], 'A-report', os.getppid(), 'running', None))
        self.record(self.report['session_id'], 'report-physical', 'report-logical',
            self.report['execution_id'], 'summary')
        with mock.patch.dict(os.environ, {'HERMES_ROLE_RUN_ID': 'run-one'}):
            prepared = report_summary.build_summary_input(self.root, 'A', 'phase',
                at=datetime.fromtimestamp(self.now+3*3600, ZoneInfo('Asia/Shanghai')),
                home=self.home, pool_path=self.pool)
        output = json.dumps({'accomplishments': '已保存本单工作检查点，后续结果待验收。',
            'issues': '尚未完成生产验收，不将本夹具称为真实模型回执。',
            'next_steps': '继续核对真实结果及验收证据。',
            'sections':{'当前工作':['**工作：** 本单仍待验收。'],
                '本次完成':['**检查点：** 已保存真实接续状态。'],
                '阻塞与依赖':['**验收：** 尚未完成生产验收，隔离夹具不代表真实模型回执。'],
                '下一步计划':['**接续：** 继续核对真实结果及验收证据。']},
            'evidence_ids': ['event:'+str(self.business_event)]}, ensure_ascii=False)
        transformed = transform_native_report(self.report, self.report['execution_id'], output,
            {'session_id': self.report['session_id'], 'model_request_id': 'report-logical',
             'model': 'offline-contract-fixture', 'route': 'cloud'})
        self.assertFalse(transformed['suppress'], transformed)
        acknowledged = acknowledge_native_report(transformed['metadata'], execution_id=self.report['execution_id'],
            outcome='delivered', attempted=True, content_sha256=transformed['metadata']['rendered_sha256'])
        self.assertEqual(acknowledged['state'], 'SENT')
        with sqlite3.connect(self.home/'cron/executions.db') as db:
            db.execute("UPDATE executions SET status='completed',delivery_outcome='delivered' WHERE id=?",
                (self.report['execution_id'],))
        with sqlite3.connect(self.home/'cron/deliveries.db') as db:
            db.execute('CREATE TABLE deliveries(execution_id TEXT,status TEXT,finished_at TEXT,content TEXT,job_json TEXT)')
            db.execute('INSERT INTO deliveries VALUES(?,?,?,?,?)', (self.report['execution_id'],
                'delivered', '2026-10-19T05:00:00Z', '', '{}'))
        self.item = {'version_digest': self.version, 'proof': {'home': str(self.home),
            'session_id': self.scope['session_id'], 'physical_attempt_id': 'business-physical',
            'logical_request_id': 'business-logical', 'execution_id': identity['execution_id'],
            'controlled_project': str(self.root), 'business_event_id': self.business_event,
            'channel': {'session_id': self.report['session_id'], 'physical_attempt_id': 'report-physical',
                'logical_request_id': 'report-logical', 'execution_id': self.report['execution_id'],
                'notification_id': transformed['metadata']['notification_id'],
                'input_artifact': prepared['artifact'], 'input_sha256': migration.file_sha(prepared['artifact'])}}}

    def save_scope(self, scope):
        path = self.home/'runtime/role-pool-executions'/(scope['execution_id']+'.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(scope))

    def record(self, session, physical, logical, execution, kind, *, work_id=''):
        usage_accounting.record(self.home/'runtime/role-usage.db', 'end', session_id=session,
            api_request_id=logical, physical_attempt_id=physical, role='A', project=self.root,
            started_at=self.now-2, ended_at=self.now-1, session_kind=kind, route='cloud',
            run_id='run-one', work_id=work_id, execution_id=execution, provider_dispatched=True,
            model='offline-contract-fixture', provider='offline-fixture',
            usage={'input_tokens':11,'output_tokens':7})

    def verify(self):
        return migration._verify_gate_proof('M', self.item, {'acceptance_environments': {'M': {
            'purpose': 'isolated_acceptance', 'home': str(self.home),
            'controlled_project': str(self.root), 'run_id': 'run-one'}}}, None, self.now)

    def test_contract_accepts_explicit_two_execution_chain_with_frozen_business_event(self):
        result = self.verify()
        self.assertIn('business_event_sha256', result)
        self.assertIn('report_notification_sha256', result)

    def test_contract_rejects_summary_without_provider_dispatch(self):
        with sqlite3.connect(self.home/'runtime/role-usage.db') as db:
            db.execute('UPDATE role_usage SET provider_dispatched=0 WHERE session_id=?', (self.report['session_id'],))
        with self.assertRaisesRegex(ValueError, 'REAL_RELATED_SUMMARY_MODEL'):
            self.verify()

    def test_contract_rejects_unrelated_report_scope_and_self_delivery_shortcut(self):
        self.item['proof']['channel']['execution_id'] = self.scope['execution_id']
        with self.assertRaisesRegex(ValueError, 'RELATED_NATIVE_REPORT_PROOF'):
            self.verify()
        self.item['proof']['channel']['execution_id'] = self.report['execution_id']
        self.save_scope({**self.report, 'run_id': 'unrelated-run'})
        with self.assertRaisesRegex(ValueError, 'EXACT_RELATED_REPORT_SCOPE'):
            self.verify()

    def test_contract_rejects_unknown_channel_and_changed_native_send_hash(self):
        with sqlite3.connect(self.home/'cron/deliveries.db') as db:
            db.execute("UPDATE deliveries SET status='unknown'")
        with self.assertRaisesRegex(ValueError, 'ACTUAL_GATEWAY_DELIVERY'):
            self.verify()
        with sqlite3.connect(self.home/'cron/deliveries.db') as db:
            db.execute("UPDATE deliveries SET status='delivered'")
        with sqlite3.connect(self.path) as db:
            notice = next(row for row in self.ledger.outbox_items(include_terminal=True) if row['kind'] == 'PERIODIC_SUMMARY')
            receipt = {**notice['receipt'], 'content_sha256': '0'*64}
            db.execute('UPDATE outbox SET receipt=? WHERE notification_id=?',
                (json.dumps(receipt), notice['notification_id']))
        with self.assertRaisesRegex(ValueError, 'CONFIRMED_NATIVE_REPORT_OUTBOX'):
            self.verify()

    def test_contract_rejects_over_budget_output_and_missing_usage(self):
        with sqlite3.connect(self.home/'runtime/role-usage.db') as db:
            db.execute("UPDATE role_usage SET usage_json=? WHERE physical_attempt_id='report-physical'",
                       (json.dumps({'input_tokens':11,'output_tokens':1683}),))
        with self.assertRaisesRegex(ValueError,'M_OUTPUT_BUDGET_EXCEEDED'):
            self.verify()
        with sqlite3.connect(self.home/'runtime/role-usage.db') as db:
            db.execute("UPDATE role_usage SET usage_json=NULL,usage_present=0 WHERE physical_attempt_id='report-physical'")
        with self.assertRaisesRegex(ValueError,'M_OUTPUT_USAGE_UNVERIFIED'):
            self.verify()

    def test_contract_rejects_business_event_from_another_work(self):
        self.item['proof']['business_event_id'] = 1
        with self.assertRaisesRegex(ValueError, 'ACTUAL_CONTROLLED_BUSINESS_WORK'):
            self.verify()
