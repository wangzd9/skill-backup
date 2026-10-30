"""Reports use fake analysis and delivery callbacks with the real sole ledger."""
from datetime import datetime
from pathlib import Path
import sys
import tempfile
import unittest
import subprocess
import uuid
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from business_ledger import BusinessLedger, _process
import usage_accounting as usage
from workflow_reports import build_facts, generate_periodic, deliver_notification, render_status_brief

PERSONAL_SECTIONS = ('当前工作', '本次完成', '阻塞与依赖', '下一步计划')


class WorkflowReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.at = datetime(2026, 10, 19, 15, 0, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        self.ledger = BusinessLedger(self.root/'business.sqlite3', clock=lambda: self.at)
        self.run = self.ledger.create_run('req', str(self.root), str(self.root), ['TASK-1'], command_id='run')['run_id']
        self.ledger.select_run(self.run, command_id='select')
        self.index = self.root/'usage.sqlite3'
        self.calls = 0

    def analyze(self, facts):
        self.calls += 1
        # The report's request finishes after its input window and is charged
        # to the subsequent one rather than modifying its own frozen facts.
        usage.record(self.index, 'end', session_id='report', api_request_id='summary', physical_attempt_id='summary',
            role='A', project=self.root, session_kind='summary', started_at=self.at,
            ended_at=self.at+2, usage={'input_tokens': 8, 'output_tokens': 2, 'prompt_tokens': 8})
        sections = {key: ['**状态：** 当前没有新增事实，待核实。'] for key in PERSONAL_SECTIONS}
        return {'model_request_id': 'fake-summary', 'model': 'offline-fixture', 'route': 'unknown',
            'analysis': {'sections': sections,
                'evidence_ids': facts['evidence_ids']}}

    def generate(self, analyze=None, command_id='summary'):
        return generate_periodic(self.ledger, 'A', self.at-60, self.at, usage_index=self.index,
            project=self.root, analyze=analyze or self.analyze, command_id=command_id)

    def test_summary_analysis_dedup_delivery_and_own_completion_window(self):
        before = self.ledger.snapshot()
        result = self.generate()
        self.assertEqual('GENERATED', result['generation']['state'])
        self.assertEqual(1, self.calls)
        replay = self.generate(command_id='another-command-same-window')
        self.assertEqual('DUPLICATE', replay['status'])
        self.assertEqual(1, self.calls)
        after = self.ledger.snapshot()
        for key in ('works', 'roles', 'executions', 'tasks', 'event_high_water'):
            self.assertEqual(before[key], after[key])
        item = next(row for row in self.ledger.outbox_items() if row['notification_id'] == result['notification_id'])
        self.assertEqual(0, item['payload']['facts']['usage']['calls'])
        self.assertEqual(1, usage.summarize(self.index, 'A', self.at, self.at+10, project=self.root)['calls'])
        sent = []
        def send(text):
            sent.append(text)
            return {'message_id': 'offline-receipt'}
        delivered = deliver_notification(self.ledger, result['notification_id'], send, command_id='send')
        self.assertEqual('SENT', delivered['state'])
        deliver_notification(self.ledger, result['notification_id'], send, command_id='send-new')
        self.assertEqual(1, len(sent))
        self.assertIn('- **状态：** 当前没有新增事实，待核实。', sent[0])
        self.assertIn('usage覆盖 未知', sent[0])
        self.assertIn('北京时间', sent[0])
        self.assertIn('**阻塞与依赖**', sent[0])
        self.assertIn('- **状态：** 当前没有新增事实，待核实。', sent[0])
        self.assertNotIn('观察来源：', sent[0])

    def test_personal_report_rejects_flat_wall_of_text_or_missing_bold_labels(self):
        def unstructured(facts):
            return {'model_request_id':'bad-summary','model':'offline','route':'local',
                'analysis':{'evidence_ids':facts['evidence_ids']}}
        result=self.generate(unstructured)
        self.assertEqual('FAILED',result['generation']['state'])
        item=next(row for row in self.ledger.outbox_items() if row['notification_id']==result['notification_id'])
        self.assertIn('本次总结未生成',item['payload']['text'])

    def test_personal_report_rejects_long_multi_fact_wall_instead_of_delivering_it(self):
        def long_bullet(facts):
            sections={key:['**状态：** 当前无新增事实，待核实。'] for key in PERSONAL_SECTIONS}
            sections['本次完成']=['**成果：** '+'同一事实'*130]
            return {'model_request_id':'long-summary','model':'offline','route':'local',
                'analysis':{'sections':sections,'evidence_ids':facts['evidence_ids']}}
        result=self.generate(long_bullet)
        self.assertEqual('FAILED',result['generation']['state'])
        item=next(row for row in self.ledger.outbox_items() if row['notification_id']==result['notification_id'])
        self.assertIn('本次总结未生成',item['payload']['text'])
        self.assertNotIn('同一事实'*130,item['payload']['text'])

    def test_generation_failure_is_explicit_and_unknown_delivery_never_retries(self):
        def failure(_facts):
            raise RuntimeError('secret provider exception')
        result = self.generate(failure)
        self.assertEqual('FAILED', result['generation']['state'])
        item = next(row for row in self.ledger.outbox_items() if row['notification_id'] == result['notification_id'])
        self.assertIn('本次总结未生成', item['payload']['text'])
        self.assertNotIn('secret provider', str(item))
        attempted = []
        def send(text):
            attempted.append(text)
            raise TimeoutError('ambiguous after send')
        sent = deliver_notification(self.ledger, result['notification_id'], send, command_id='send')
        self.assertEqual('UNKNOWN', sent['state'])
        deliver_notification(self.ledger, result['notification_id'], send, command_id='new-send')
        self.assertEqual(1, len(attempted))

    def test_successful_generation_keeps_model_report_and_failure_fallback_is_structured(self):
        result = self.generate()
        item = next(row for row in self.ledger.outbox_items() if row['notification_id'] == result['notification_id'])
        self.assertEqual('GENERATED', result['generation']['state'])
        self.assertIn('**当前工作**', item['payload']['text'])
        self.assertNotIn('本次总结未生成', item['payload']['text'])

        other = self.ledger.create_run('req-2', str(self.root), str(self.root), ['TASK-2'], command_id='run-2')['run_id']
        self.ledger.select_run(other, command_id='select-2')
        failed = self.generate(lambda _facts: (_ for _ in ()).throw(RuntimeError('provider detail')),
                               command_id='failed-structured')
        failed_item = next(row for row in self.ledger.outbox_items()
                           if row['notification_id'] == failed['notification_id'])
        self.assertEqual('FAILED', failed['generation']['state'])
        self.assertIn('**阻塞与依赖**', failed_item['payload']['text'])
        self.assertIn('- **调用分布：**', failed_item['payload']['text'])
        self.assertNotIn('provider detail', failed_item['payload']['text'])

    def test_night_suppresses_delivery_and_status_brief_has_no_analysis(self):
        self.at = datetime(2026, 10, 19, 22, 0, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        facts = build_facts(self.ledger, 'A', self.at-60, self.at, usage_index=self.index, project=self.root)
        brief = render_status_brief(facts)
        self.assertEqual('A：待核实\nB：待核实\nC：待核实\nTESTER：待核实', brief)
        self.assertEqual(0, self.calls)
        result = self.generate()
        self.assertEqual('SUPPRESSED', result['state'])
        self.assertEqual(1, self.calls)
        def forbidden(_text):
            self.fail('night notification must not call delivery adapter')
        outcome = deliver_notification(self.ledger, result['notification_id'], forbidden, command_id='night')
        self.assertEqual('SUPPRESSED', outcome['state'])

    def test_coordination_checkpoint_is_window_result_without_task_or_plan(self):
        self.ledger.set_mode('ACTIVE', command_id='active')
        worker = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                                  start_new_session=True)
        try:
            observed = _process(worker.pid)
            bound = self.ledger.bind_execution('COORDINATOR', {
                'execution_id': uuid.uuid4().hex, 'pid': worker.pid,
                'process_start': observed['process_start'], 'scope': observed['cgroup'],
                'generation': self.ledger.mode()['generation']}, command_id='coord-bind')
            self.assertEqual('COMMITTED', bound['status'], bound)
            saved = self.ledger.checkpoint(bound['identity'], {'result': '只读核查已保存'}, command_id='cp')
            self.assertEqual('COMMITTED', saved['status'], saved)
            # End this actual writer; its checkpoint must still be reportable.
            worker.terminate(); worker.wait(timeout=5)
            self.ledger.end_execution(bound['identity'], command_id='coord-end')
            facts = build_facts(self.ledger, 'COORDINATOR', self.at-1, self.at+1,
                               usage_index=self.index, project=self.root)
            self.assertEqual([], facts['tasks'])
            self.assertIsNone(facts['run']['plan_version'])
            records = facts['work_checkpoint_events']
            self.assertEqual(1, len(records))
            self.assertEqual(saved['event_id'], records[0]['event_id'])
            pointer=records[0]['checkpoint']['same_as']
            self.assertRegex(pointer,r'^/events/\d+/payload/checkpoint$')
            source=facts['events'][int(pointer.split('/')[2])]
            self.assertEqual(saved['event_id'],source['event_id'])
            self.assertEqual({'result': '只读核查已保存'}, source['payload']['checkpoint'])
            personal = build_facts(self.ledger, 'A', self.at-1, self.at+1,
                                  usage_index=self.index, project=self.root)
            self.assertEqual([], personal['work_checkpoint_events'])
            later = build_facts(self.ledger, 'COORDINATOR', self.at+1, self.at+2,
                               usage_index=self.index, project=self.root)
            self.assertEqual([], later['work_checkpoint_events'])
        finally:
            if worker.poll() is None:
                worker.kill(); worker.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
