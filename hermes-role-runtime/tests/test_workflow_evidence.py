"""Real candidate checkouts and command results, without provider or production I/O."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import socket
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from business_ledger import BusinessLedger, _process
from delivery_evidence import execute_candidate_tests, parse_test_results


RUNNER = '''import os,socket,sqlite3,sys,time,unittest,xml.etree.ElementTree as ET
from pathlib import Path
path,mode=sys.argv[1:]
if mode.startswith('isolation|'):
    host_file,ledger,port=mode.split('|')[1:]
    try: Path(host_file).write_text('host escaped')
    except OSError: pass
    else: raise AssertionError('host repository write escaped')
    try:
        db=sqlite3.connect(ledger)
        db.execute("UPDATE runs SET requirement_id='escaped'")
        db.commit()
    except sqlite3.Error: pass
    else: raise AssertionError('business ledger write escaped')
    if 'ROLE_TEST_PRIVATE_SENTINEL' in os.environ: raise AssertionError('undeclared environment escaped')
    client=socket.socket(); client.settimeout(.5)
    try: client.connect(('127.0.0.1',int(port)))
    except OSError: pass
    else: raise AssertionError('host network escaped')
    finally: client.close()
if mode=='missing': raise SystemExit(0)
if mode=='timeout': time.sleep(1)
if mode=='mutate': Path('value.py').write_text('value=99\\n')
class Check(unittest.TestCase):
    def test_value(self): self.assertNotEqual(mode,'fail','actual assertion failure')
suite=unittest.TestSuite() if mode=='zero' else unittest.defaultTestLoader.loadTestsFromTestCase(Check)
result=unittest.TestResult(); suite.run(result)
root=ET.Element('testsuite',tests=str(result.testsRun),failures=str(len(result.failures)),errors=str(len(result.errors)),skipped='0')
if result.testsRun:
    case=ET.SubElement(root,'testcase',name='test_value',classname='Check')
    for _,text in result.failures: ET.SubElement(case,'failure',message=text)
    for _,text in result.errors: ET.SubElement(case,'error',message=text)
ET.ElementTree(root).write(path,encoding='utf-8',xml_declaration=True)
raise SystemExit(0 if result.wasSuccessful() else 1)
'''


class WorkflowEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root/'repo'
        self.repo.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.name', 'Isolated test')
        self.git('config', 'user.email', 'isolated@localhost')
        (self.repo/'test_runner.py').write_text(RUNNER)
        (self.repo/'value.py').write_text('value=1\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'frozen candidate')
        self.head = self.git('rev-parse', 'HEAD').strip()
        self.ledger = BusinessLedger(self.root/'runtime/business.sqlite3')
        self.run = self.ledger.create_run('req', str(self.root), str(self.repo), ['TASK-1'], command_id='run')['run_id']
        self.assertEqual('COMMITTED', self.ledger.publish_plan(self.run, 1,
            [{'task_id': 'TASK-1', 'role': 'A', 'scope': ['*.py'], 'base': self.head, 'ac_version': 'ac1'}],
            command_id='plan')['status'])
        self.ledger.select_run(self.run, command_id='select')
        self.ledger.set_mode('ACTIVE', command_id='activate')
        self.dev = self.bind('A')
        receipt = self.root/'development.txt'
        receipt.write_text('actual development self-test receipt')
        base=self.head
        (self.repo/'value.py').write_text('value=1\n# frozen development delta\n')
        self.git('add','value.py');self.git('commit','-qm','actual development')
        self.head=self.git('rev-parse','HEAD').strip()
        delivered = self.ledger.deliver(self.dev, {'repo': str(self.repo), 'base': base,
            'head': self.head, 'ac_version': 'ac1', 'evidence': [{'path': str(receipt)}]}, 'frozen', command_id='deliver')
        self.assertEqual('COMMITTED', delivered['status'])
        self.candidate = delivered['candidate_id']
        self.tester = self.bind('TESTER')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True, stderr=subprocess.DEVNULL)

    def bind(self, role):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
        def cleanup():
            child.terminate()
            child.wait(timeout=5)
        self.addCleanup(cleanup)
        actual = _process(child.pid)
        bound = self.ledger.bind_execution(role, {'execution_id': uuid.uuid4().hex, 'pid': child.pid,
            'process_start': actual['process_start'], 'scope': actual['cgroup'],
            'generation': self.ledger.mode()['generation']}, command_id='bind-'+role)
        self.assertEqual('COMMITTED', bound['status'])
        return bound['identity']

    def execute(self, mode='pass', command_id='test', **kwargs):
        return execute_candidate_tests(self.ledger, self.tester, self.candidate,
            [sys.executable, 'test_runner.py', '{junit_xml}', mode], command_id=command_id,
            report_format='junit', **kwargs)

    def test_real_frozen_checkout_publishes_once_and_tamper_closes_gate(self):
        result = self.execute()
        self.assertEqual('COMMITTED', result['status'])
        self.assertEqual((1, 1, 0), tuple(result['test_stats'][k] for k in ('tests', 'passed', 'failures')))
        self.assertEqual('PASS', result['test_stats']['mechanical_result'])
        self.assertTrue(self.ledger.acceptance(self.candidate)['ready'])
        runner = Path(result['runner_evidence']['path'])
        frozen = json.loads(runner.read_text())
        self.assertNotEqual(str(self.repo), frozen['cwd'])
        self.assertFalse(frozen['before']['identity']['dirty'])
        self.assertEqual(self.head, frozen['before']['identity']['commit'])
        self.assertEqual(1, result['candidate']['attempt'])
        original_log = Path(frozen['log_path']).read_bytes()
        replay = self.execute()
        self.assertEqual('DUPLICATE', replay['status'])
        self.assertEqual(original_log, Path(frozen['log_path']).read_bytes())
        self.assertEqual(1, len(self.ledger.snapshot()['verifications']))
        Path(frozen['log_path']).write_text('tampered command log')
        self.assertFalse(self.ledger.acceptance(self.candidate)['ready'])

    def test_actual_failure_publishes_fail_and_preserves_failure_identity(self):
        result = self.execute('fail')
        self.assertEqual('COMMITTED', result['status'])
        self.assertEqual('FAIL', result['test_stats']['mechanical_result'])
        self.assertEqual(1, result['test_stats']['failures'])
        self.assertIn('actual assertion failure', result['test_stats']['first_failure']['message'])
        gate = self.ledger.acceptance(self.candidate)
        self.assertFalse(gate['ready'])
        self.assertEqual([result['publication']['verification_id']], gate['open_failure_ids'])

    def test_arbitrary_test_command_cannot_write_host_repo_ledger_or_connect_host_network(self):
        from unittest import mock
        original = (self.repo/'value.py').read_bytes()
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(('127.0.0.1', 0)); listener.listen()
        mode = '|'.join(['isolation', str(self.repo/'value.py'), str(self.ledger.path), str(listener.getsockname()[1])])
        with mock.patch.dict(os.environ, {'ROLE_TEST_PRIVATE_SENTINEL': 'private-fixture'}):
            result = self.execute(mode)
        self.assertEqual('COMMITTED', result['status'], result)
        self.assertEqual('PASS', result['test_stats']['mechanical_result'])
        self.assertEqual(original, (self.repo/'value.py').read_bytes())
        self.assertEqual('req', self.ledger.snapshot()['runs'][0]['requirement_id'])
        self.assertEqual(1, len(self.ledger.snapshot()['execution_tools']))
        self.assertTrue(self.ledger.acceptance(self.candidate)['ready'])

    def test_missing_zero_timeout_and_changed_content_never_publish_pass(self):
        for mode in ('missing', 'zero', 'timeout', 'mutate'):
            with self.subTest(mode=mode):
                result = self.execute(mode, command_id='test-'+mode, timeout_seconds=.1 if mode=='timeout' else 5)
                self.assertEqual('UNVERIFIED', result['status'])
                self.assertNotIn('publication', result)
        self.assertEqual([], self.ledger.snapshot()['verifications'])

    def test_parser_checks_cases_and_nested_counts_and_explicit_skip(self):
        report = self.root/'parser.xml'
        report.write_text('<testsuites tests="1"><testsuite tests="1" failures="0" errors="0" skipped="0"><testcase name="x"/></testsuite></testsuites>')
        self.assertEqual(1, parse_test_results(report)['tests'])
        report.write_text('<testsuite tests="12" failures="0" errors="0" skipped="0"/>')
        self.assertEqual('UNVERIFIED', parse_test_results(report)['mechanical_result'])
        report.write_text('<testsuite tests="1" skipped="1"><testcase name="x"><skipped/></testcase></testsuite>')
        self.assertEqual('UNVERIFIED', parse_test_results(report)['mechanical_result'])
        report.write_text('<testsuite tests="1"><testcase name="x"/></testsuite>')
        self.assertEqual('UNVERIFIED', parse_test_results(report, execution_status='CANCELLED')['mechanical_result'])


if __name__ == '__main__':
    unittest.main()
