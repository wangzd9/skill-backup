"""Use actual queue CLI and lease script to verify the shared execution contract."""
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from progress_display import enrich_records, token_label
import manual_business

ROOT=Path(__file__).resolve().parents[1]
RUNTIME=Path(__file__).resolve().parents[1]/'role_runtime.py'

class InteractionContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.project=Path(self.tmp.name)
        (self.project/'ROLE-BINDINGS.json').write_text(json.dumps({'schema_version':1,'project_root':str(self.project),'control':'ACTIVE',
            'roles':{'COORDINATOR':{'chat_id':'coord','session_file':'sessions/COORDINATOR.md'}}}))

    def request(self,chat='coord',text='优先处理既有任务'):
        source=self.project/'input.json'
        source.write_text(json.dumps({'message_id':'native-id','chat_id':chat,'user_id':'native-user','text':text,
             'context':{'reply_to_message_id':'reply-id','media_urls':['artifact.png']}}))
        return subprocess.run([sys.executable,str(ROOT/'runtime_tools.py'),'request','--project',str(self.project),'--input',str(source)],capture_output=True,text=True)

    def test_explicit_action_request_is_durable_once_and_keeps_native_context(self):
        self.assertEqual(self.request().returncode,0)
        self.assertEqual(self.request().returncode,0)
        files=list((self.project/'inbox/coordinator/requests').glob('*.json'))
        self.assertEqual(len(files),1)
        row=json.loads(files[0].read_text())
        self.assertEqual(row['text'],'优先处理既有任务')
        self.assertEqual(row['context']['reply_to_message_id'],'reply-id')
        self.assertFalse((self.project/'inbox/control/runtime/leases/COORDINATOR.json').exists())
        self.assertNotEqual(self.request(text='修改原文').returncode,0)

    def test_wrong_group_cannot_submit_to_project(self):
        self.assertNotEqual(self.request(chat='unrelated').returncode,0)
        self.assertFalse((self.project/'inbox/coordinator/requests').exists())

    def test_manual_and_cron_owner_compete_for_same_lease(self):
        # An isolated scheduler-shaped parent starts the actual lease CLI and
        # stays alive until both competitors finish. The CLI verifies /proc
        # ancestry and PID/start identity; a unittest PID is not a cron worker.
        worker_script=self.project/'lease_worker.py'
        worker_script.write_text('''import json, os, subprocess, sys
sys.stdin.readline()
result = subprocess.run([sys.executable, sys.argv[2], 'claim', '--project', sys.argv[3],
    '--role', 'COORDINATOR', '--pid', str(os.getpid()), '--owner', 'cron'],
    capture_output=True, text=True, timeout=10)
print(json.dumps({'code': result.returncode, 'out': result.stdout, 'err': result.stderr}), flush=True)
sys.stdin.readline()
''')
        worker=subprocess.Popen([sys.executable,str(worker_script),'cron.scheduler',str(RUNTIME),str(self.project)],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            worker.stdin.write('claim\n'); worker.stdin.flush()
            manual=manual_business.control(self.project,'COORDINATOR','manual-session','native-turn','claim')
            with selectors.DefaultSelector() as selector:
                selector.register(worker.stdout,selectors.EVENT_READ)
                self.assertTrue(selector.select(timeout=15),'isolated worker did not return a lease receipt')
                result=json.loads(worker.stdout.readline())
            self.assertEqual(result['code'],0,result['err'])
            rows=[manual,json.loads(result['out'])]
        finally:
            if worker.poll() is None:
                worker.stdin.write('done\n'); worker.stdin.flush()
            try: worker.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                worker.kill(); worker.communicate(timeout=5)
        self.assertEqual(sum(r['claimed'] is True for r in rows),1)
        self.assertEqual(sum(r.get('reason')=='OWNER_BUSY_OR_UNVERIFIED' for r in rows),1)

    def test_report_uses_latest_confirmation_without_changing_observer_state(self):
        eventdir=self.project/'events/C';eventdir.mkdir(parents=True)
        event={'role':'C','occurred_at':'2026-10-14T09:55:00+08:00','status':'OBSERVING','event_id':'c-poll-nochange'}
        (eventdir/'latest.json').write_text(json.dumps(event))
        snapshot={'project':str(self.project),'role':'COORDINATOR','events':{'C':{'occurred_at':'2026-10-14T02:00:00+08:00','status':'RUNNING'}}}
        enriched=enrich_records(self.project,snapshot)
        self.assertEqual(enriched['events']['C']['status'],'OBSERVING')
        self.assertEqual(snapshot['events']['C']['status'],'RUNNING')
        self.assertFalse((self.project/'inbox/control/runtime/observer').exists())

    def test_token_usage_is_readable_instead_of_dumping_dict(self):
        self.assertEqual(token_label([{'input_tokens':1234,'output_tokens':56}]),'输入1,234 tokens、输出56 tokens')

if __name__=='__main__':unittest.main()
