import asyncio
import copy
import gzip
import importlib.util
import json
import multiprocessing
from pathlib import Path
import sys
import subprocess
import time
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from coord_queue import enqueue, pending_requests, acknowledge_request
from load_metrics import parse_metrics, load_query
from operation_jobs import execute_plan, inspect_state, preflight, start_job
from runtime_common import save_json
from prefix_audit import PrefixAudit


def concurrent_enqueue(project):
    enqueue(project, message_id='same', chat_id='group', user_id='user', text='核对当前结果')


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_repeated_delivery_has_one_durable_request(self):
        processes = [multiprocessing.Process(target=concurrent_enqueue, args=(str(self.root),)) for _ in range(4)]
        for p in processes: p.start()
        for p in processes:
            p.join(5)
            self.assertEqual(p.exitcode, 0)
        self.assertEqual(len(pending_requests(self.root)), 1)

    def test_ack_requires_evidence_and_does_not_consume_new_request(self):
        first = enqueue(self.root, message_id='1', chat_id='group', user_id='user', text='第一项')
        second = enqueue(self.root, message_id='2', chat_id='group', user_id='user', text='第二项')
        with self.assertRaises(ValueError): acknowledge_request(self.root, first['request_id'], self.root/'missing')
        evidence = self.root/'decision.md'; evidence.write_text('实际处理结论')
        acknowledge_request(self.root, first['request_id'], evidence)
        self.assertEqual([r['request_id'] for r in pending_requests(self.root)], [second['request_id']])

    def test_duplicate_identity_cannot_replace_instruction(self):
        enqueue(self.root, message_id='1', chat_id='group', user_id='user', text='原指令')
        with self.assertRaises(ValueError):
            enqueue(self.root, message_id='1', chat_id='group', user_id='user', text='被替换的指令')


class MetricsTests(unittest.TestCase):
    def test_only_mechanical_query_matches(self):
        for text in ['当前大模型负载情况', '查看千问负载', '/llm-load', '模型负载怎么样？']:
            self.assertTrue(load_query(text), text)
        for text in ['为什么当前大模型负载高', '分析模型负载并修改并发', '/approve x', '将负载降下来']:
            self.assertFalse(load_query(text), text)

    def test_read_prometheus_metrics_and_keep_missing_unknown(self):
        data = parse_metrics('vllm:num_requests_running{model_name="x"} 4\nvllm:num_requests_waiting{reason="capacity"} 3\nvllm:kv_cache_usage_perc 0.12\n')
        self.assertEqual(data['running'], 4)
        self.assertEqual(data['waiting'], 3)
        self.assertEqual(data['kv_percent'], 12)
        self.assertIsNone(data['completed'])


class OperationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def run_plan(self, steps):
        state = self.root/('result-'+str(len(list(self.root.glob('result-*.json'))))+'.json')
        result = execute_plan({'operation_id':'isolated-test', 'cwd':str(self.root), 'steps':steps}, state)
        return result, state

    def test_failure_stops_next_step_and_keeps_nonzero_exit(self):
        result, state = self.run_plan([{'argv':[sys.executable,'-c','raise SystemExit(7)']},
                                      {'argv':[sys.executable,'-c','open("should-not-exist","w").write("bad")']}])
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(result['exit_code'], 7)
        self.assertFalse((self.root/'should-not-exist').exists())
        self.assertEqual(inspect_state(state)['status'], 'FAILED')

    def test_gzip_is_decoded_before_child_and_failure_propagates(self):
        archive = self.root/'input.sql.gz'
        with gzip.open(archive,'wb') as f: f.write(b'SELECT 1;\n')
        result, _ = self.run_plan([{'argv':[sys.executable,'-c','import sys; assert sys.stdin.buffer.read()==b"SELECT 1;\\n"'], 'stdin_gzip':str(archive)}])
        self.assertEqual(result['status'], 'SUCCEEDED')
        archive.write_bytes(b'not gzip')
        result, _ = self.run_plan([{'argv':[sys.executable,'-c','import sys;sys.stdin.buffer.read()'], 'stdin_gzip':str(archive)}])
        self.assertEqual(result['status'], 'FAILED')
        self.assertNotEqual(result['exit_code'], 0)

    def test_dead_owner_is_unknown_not_success(self):
        state=self.root/'result.json'
        state.write_text(json.dumps({'status':'RUNNING','pid':999999999,'process_start':'1','boot_id':'old'}))
        self.assertEqual(inspect_state(state)['status'], 'UNKNOWN')

    def test_preflight_checks_actual_filesystem_and_missing_program(self):
        result=preflight(str(self.root), need_bytes=10**30, programs=['definitely-no-such-program'])
        self.assertFalse(result['ok'])
        self.assertTrue(result['failures'])

    def test_start_is_idempotent_and_changed_input_cannot_restart(self):
        plan={'operation_id':'once','cwd':str(self.root),'steps':[{'argv':[sys.executable,'-V']}]}
        with patch('operation_jobs.subprocess.run',return_value=subprocess.CompletedProcess([],0,'','')) as run:
            result=start_job(self.root,'A',plan)
            self.assertEqual(result['status'],'PENDING')
            # systemctl inspect is read-only; separate it from the start count.
            run.return_value=subprocess.CompletedProcess([],0,'ActiveState=active\nLoadState=loaded\n','')
            self.assertFalse(start_job(self.root,'A',plan)['started'])
            self.assertEqual(sum(c.args[0][0]=='systemd-run' for c in run.call_args_list),1)
            with self.assertRaises(ValueError): start_job(self.root,'A',{**plan,'task':'changed'})
            with self.assertRaises(ValueError): start_job(self.root,'A',{**plan,'operation_id':'second'})

    def test_submission_timeout_cannot_overwrite_worker_terminal_result(self):
        plan={'operation_id':'race','cwd':str(self.root),'steps':[{'argv':[sys.executable,'-V']}]}
        def timed_out(command,**kwargs):
            state=self.root/'inbox/control/runtime/operations/A/race/result.json'
            save_json(state,{'status':'SUCCEEDED','exit_code':0,'operation_id':'race'})
            raise subprocess.TimeoutExpired(command,15)
        with patch('operation_jobs.subprocess.run',side_effect=timed_out):
            result=start_job(self.root,'A',plan)
        self.assertEqual(result['status'],'SUCCEEDED')

    def test_pending_without_service_is_unknown(self):
        state=self.root/'pending.json';save_json(state,{'status':'PENDING','unit':'gone'})
        with patch('operation_jobs.subprocess.run',return_value=subprocess.CompletedProcess([],0,'LoadState=not-found\n','')):
            self.assertEqual(inspect_state(state)['status'],'UNKNOWN')

    def test_invalid_lease_blocks_control_writes(self):
        from runtime_tools import owner_guard
        save_json(self.root/'ROLE-BINDINGS.json',{'schema_version':1,'project_root':str(self.root),'control':'ACTIVE','roles':{'A':{}}})
        token=self.root/'token.json';save_json(token,{'token':'stale'})
        with self.assertRaises(ValueError):
            with owner_guard(self.root,'A',token): self.fail('must not enter')

    def test_worker_rejects_changed_frozen_input(self):
        spec=self.root/'spec.json';state=self.root/'state.json'
        save_json(spec,{'operation_id':'tampered','cwd':str(self.root),'steps':[{'argv':[sys.executable,'-V']}]})
        save_json(state,{'status':'PENDING'})
        result=subprocess.run([sys.executable,str(ROOT/'runtime_tools.py'),'worker','--spec',str(spec),'--state',str(state),'--sha256','wrong'],capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(inspect_state(state)['status'],'FAILED')


class PrefixTests(unittest.TestCase):
    def test_audit_observes_drift_without_changing_messages_or_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit=PrefixAudit(Path(tmp))
            request={'messages':[{'role':'system','content':'保持原审核门禁'}], 'tools':[{'type':'function','function':{'name':'test','parameters':{}}}]}
            before=copy.deepcopy(request)
            a=audit.record(session_id='a', request=request, system_prompt='保持原审核门禁')
            b=audit.record(session_id='a', request=request, system_prompt='保持原审核门禁')
            self.assertEqual(request, before)
            self.assertFalse(b['system_changed'])
            c=audit.record(session_id='a', request=request, system_prompt='新契约仍须加载')
            self.assertTrue(c['system_changed'])
            self.assertNotIn('新契约', json.dumps(c,ensure_ascii=False))
            self.assertEqual(a['tools_hash'], b['tools_hash'])


if __name__=='__main__': unittest.main()
