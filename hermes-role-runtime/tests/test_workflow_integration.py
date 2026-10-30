"""Installed native admission, ledger and plugin against isolated real resources."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import threading
import time
import sys
import unittest
from unittest import mock
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parents[1]
CORE=Path('/home/wang/.hermes/hermes-agent')
for directory in (ROOT,ROOT/'tests',CORE):
    sys.path.insert(0,str(directory))

from test_business_ledger import LedgerFixture
import workflow_runtime as workflow
from cron import role_pool_scheduler as pools


class WorkflowIntegrationTests(LedgerFixture):
    def setUp(self):
        super().setUp()
        self.home=self.root/'hermes-home'
        (self.home/'plugins').mkdir(parents=True)
        (self.home/'plugins/role-workflow-runtime').symlink_to(ROOT,target_is_directory=True)
        (self.home/'cron').mkdir()
        self.pool_path=self.root/'isolated.json'
        roles={role:{'chat_id':'chat-'+role,'poll_job_id':role+'-poll','report_job_id':role+'-report'}
               for role in workflow.ROLES}
        self.pool={'schema_version':1,'roles':roles}
        self.pool_path.write_text(json.dumps(self.pool))
        jobs=[{'id':role+'-'+kind,'enabled':True,'paused':False,
               'role_pool':{'path':str(self.pool_path),'role':role,
                            'kind':'business' if kind=='poll' else 'report'}}
              for role in roles for kind in ('poll','report')]
        (self.home/'cron/jobs.json').write_text(json.dumps({'jobs':jobs}))
        with sqlite3.connect(self.home/'cron/executions.db') as db:
            db.execute('CREATE TABLE executions(id TEXT,job_id TEXT,pid INTEGER,process_started_at TEXT,status TEXT)')
        self.assertEqual(workflow.ledger_path(self.pool_path),self.path)
        patch=mock.patch.dict(os.environ,{'HERMES_HOME':str(self.home)})
        patch.start();self.addCleanup(patch.stop)
        # This suite verifies ledger commands; bounded native wakeups are
        # exercised separately with a blocking offline native run body.
        wakeups=mock.patch('workflow_runtime.request_business_wakeups',return_value={'state':'OFFLINE_FIXTURE'})
        wakeups.start();self.addCleanup(wakeups.stop)

    def tearDown(self):
        from workflow_dispatch import state
        deadline=time.monotonic()+5
        while state()['active'] and time.monotonic()<deadline:
            time.sleep(.01)
        self.assertEqual(state()['active'],0,state())
        super().tearDown()

    def test_current_facts_are_readonly_and_delivery_cannot_claim_idle_with_live_writer(self):
        worker,identity=self.bind('A')
        before=self.ledger.snapshot()['event_high_water']
        packet=workflow.current_input(self.pool_path,'A')
        self.assertEqual(packet['work']['work_id'],identity['work_id'])
        self.assertFalse(packet['archives_loaded'])
        self.assertEqual(workflow.role_state(self.pool_path,'A')['status'],'WORKING')
        self.assertEqual(self.ledger.snapshot()['event_high_water'],before)
        self.candidate(identity)
        self.assertEqual(workflow.role_state(self.pool_path,'A')['status'],'UNKNOWN')
        worker.terminate();worker.wait(timeout=5)
        self.assertEqual(workflow.recover_executions(self.pool_path)[0]['status'],'COMMITTED')
        self.assertEqual(workflow.role_state(self.pool_path,'A')['status'],'IDLE')

    def test_work_tool_is_bound_and_old_execution_cannot_write(self):
        worker,identity=self.bind('A')
        scope={**identity,'kind':'business','workflow_version':1,'pool_path':str(self.pool_path)}
        self.assertIsNone(workflow.tool_decision(scope,'role_development_command'))
        self.assertEqual(workflow.tool_decision(scope,'role_coordination_command')['action'],'block')
        response=workflow.command(scope,'checkpoint',{'checkpoint':{'next':'核对实际改动'}},command_id='cp')
        self.assertEqual(response['status'],'COMMITTED')
        with self.assertRaisesRegex(ValueError,'IDENTITY_IS_RUNTIME_BOUND'):
            workflow.command(scope,'checkpoint',{'role':'B'},command_id='forged')
        self.close(worker,identity)
        with self.assertRaises(ValueError):
            workflow.tool_decision(scope,'terminal')

    def test_native_worker_admission_injects_current_work_and_report_does_not_take_slot(self):
        execution_id=uuid.uuid4().hex
        fields=(Path('/proc')/str(os.getpid())/'stat').read_text().rsplit(')',1)[1].split()
        with sqlite3.connect(self.home/'cron/executions.db') as db:
            db.execute('INSERT INTO executions VALUES(?,?,?,?,?)',
                       (execution_id,'A-poll',os.getpid(),fields[19],'running'))
        job={'id':'A-poll','prompt':'old prompt','script':'old-lease-scanner',
             'role_pool':{'path':str(self.pool_path),'role':'A','kind':'business'}}
        with mock.patch.dict(os.environ,{'_HERMES_CRON_EXTERNAL_WORKER':execution_id}):
            with pools.prepare_role_pool_run(job,execution_id=execution_id,session_id='native-'+execution_id,home=self.home) as scoped:
                self.assertEqual(scoped['_role_pool_scope']['schema_version'],2)
                self.assertIsNone(scoped['script'])
                self.assertIn('role_development',scoped['enabled_toolsets'])
                self.assertNotEqual(scoped['workdir'],str(self.repo))
                self.assertEqual(os.environ['HERMES_ROLE_WORK_ID'],scoped['_role_pool_scope']['work_id'])
        frozen=pools.scope_for_session('native-'+execution_id,self.home)
        self.assertEqual(frozen['task_id'],'T1')
        self.assertEqual(self.ledger.current_work('TESTER'),None)
        report=workflow.prepare_report({'id':'TESTER-report','role_pool':{'role':'TESTER'},'prompt':'report'},
                  pool_path=self.pool_path,execution={'execution_id':uuid.uuid4().hex},home=self.home,session_id='report')
        self.assertEqual(report['_role_pool_scope']['kind'],'report')
        self.assertIsNone(self.ledger.current_work('TESTER'))
        self.assertEqual(workflow.tool_decision(report['_role_pool_scope'],'terminal')['action'],'block')

    def test_control_request_is_durable_deduplicated_and_paused_dispatch_stays_paused(self):
        source={'authenticated':True,'current_full':True,'message_id':'m1','chat_id':'chat-COORDINATOR',
                'user_id':'user','primary_text':'全部权限：暂停当前需求'}
        first=workflow.submit_control(self.pool_path,'COORDINATOR','pause',{},source)
        duplicate=workflow.submit_control(self.pool_path,'COORDINATOR','pause',{},source)
        self.assertEqual(first['status'],'COMMITTED')
        self.assertEqual(duplicate['status'],'DUPLICATE')
        self.assertEqual(self.ledger.current_work('COORDINATOR'),None)
        jobs=json.loads((self.home/'cron/jobs.json').read_text())
        jobs['jobs'][0]['paused']=True
        (self.home/'cron/jobs.json').write_text(json.dumps(jobs))
        result=workflow.dispatch_event(self.pool_path,'COORDINATOR',home=self.home,
                                      dispatcher=lambda _:self.fail('paused dispatch'))
        self.assertEqual(result['state'],'DEFERRED')
        self.assertEqual(self.ledger.snapshot()['controls'][0]['state'],'ACCEPTED')

    def test_control_change_blocks_old_shell_but_keeps_safe_checkpoint_and_release(self):
        _,identity=self.bind('A')
        scope={**identity,'kind':'business','workflow_version':1,'pool_path':str(self.pool_path)}
        self.assertCommitted(self.ledger.freeze_admission('isolated gate',command_id='freeze'))
        self.assertFalse(workflow.role_state(self.pool_path,'B')['available'])
        with self.assertRaisesRegex(ValueError,'MIGRATION_ADMISSION_FROZEN'):
            workflow.tool_decision(scope,'terminal',{'command':'true'})
        self.assertCommitted(workflow.command(scope,'checkpoint',{'checkpoint':{'next':'安全收口'}},command_id='paused-cp'))
        forged={**scope,'work_id':'wrong'}
        with self.assertRaises(ValueError):
            workflow.current_input(self.pool_path,'A',scope=forged)

    def test_native_shell_confines_host_writes_and_tracks_real_tool_scope(self):
        _,identity=self.bind('A')
        destination=self.path.parent/'workspaces'/identity['work_id']
        destination.parent.mkdir(parents=True)
        subprocess.run(['git','clone','-q','--shared',str(self.repo),str(destination)],check=True)
        scope={**identity,'kind':'business','workflow_version':1,'pool_path':str(self.pool_path),
               'worktree':str(destination),'write_scope':['app.py']}
        original=(self.repo/'app.py').read_text()
        program='''from pathlib import Path
Path('app.py').write_text('changed in private clone')
try:
 Path(%r).write_text('unsafe')
except OSError:
 pass
else:
 raise AssertionError('host write escaped sandbox')
''' % str(self.repo/'app.py')
        command=shlex.join([sys.executable,'-c',program])
        directive=workflow.tool_decision(scope,'terminal',{'command':command})
        self.assertEqual(directive['action'],'modify')
        result=subprocess.run(directive['args']['command'],shell=True,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual((destination/'app.py').read_text(),'changed in private clone')
        self.assertEqual((self.repo/'app.py').read_text(),original)
        tools=self.ledger.snapshot()['execution_tools']
        self.assertEqual(len(tools),1)
        self.assertEqual(tools[0]['execution_id'],identity['execution_id'])
        # File tools also fail closed unless their scoped native backend has
        # been registered; a policy-only allow must never reach host writes.
        with self.assertRaisesRegex(ValueError, 'SCOPED_FILE_HANDLER_NOT_READY'):
            workflow.tool_decision(scope,'write_file',{'path':str(self.repo/'app.py'),'content':'unsafe'})

    def scope(self, identity, workspace=None):
        result={**identity,'kind':'business','workflow_version':1,'pool_path':str(self.pool_path)}
        if workspace:
            result['worktree']=str(workspace)
            result['write_scope']=['app.py']
        return result

    def native_candidate(self, identity, value):
        workspace=self.path.parent/'workspaces'/identity['work_id']
        workspace.parent.mkdir(parents=True,exist_ok=True)
        subprocess.run(['git','clone','-q','--shared',str(self.repo),str(workspace)],check=True)
        scope=self.scope(identity,workspace)
        script='''from pathlib import Path
import json, subprocess
Path('app.py').write_text('def value():\\n    return %s\\n')
subprocess.run(['git','add','app.py'],check=True)
subprocess.run(['git','-c','user.name=Isolated Developer','-c','user.email=dev@example.invalid','commit','-qm','candidate'],check=True)
result=subprocess.run([%r,'-B','-c','from app import value; assert value()==%s'])
Path('.git/self-test.json').write_text(json.dumps({'command':'actual value assertion','exit_code':result.returncode}))
raise SystemExit(result.returncode)
''' % (value,sys.executable,value)
        directive=workflow.tool_decision(scope,'terminal',{'command':shlex.join([sys.executable,'-c',script])})
        execution=subprocess.run(directive['args']['command'],shell=True,capture_output=True,text=True)
        self.assertEqual(execution.returncode,0,execution.stderr)
        spec={'repo':str(workspace),'base':self.base,'head':self.git('rev-parse','HEAD',cwd=workspace),
              'ac_version':'ac-sha256-frozen-v1','evidence':[{'path':str(workspace/'.git/self-test.json')}]}
        delivered=workflow.command(scope,'deliver',{'candidate':spec,'summary':'实现并实际自测当前冻结候选'},
                                   command_id='deliver-'+identity['execution_id'])
        self.assertCommitted(delivered)
        return delivered['candidate_id']

    def test_work_bound_fail_rework_independent_retest_complete_and_single_report(self):
        from workflow_reports import generate_periodic, deliver_notification
        coord_process,coord=self.bind('COORDINATOR')
        dev_process,dev=self.bind('A')
        first=self.native_candidate(dev,2)
        self.assertFalse(self.ledger.acceptance(first)['ready'])
        self.assertEqual(workflow.role_state(self.pool_path,'A')['status'],'UNKNOWN')
        self.close(dev_process,dev)
        test_process,tester=self.bind('TESTER')
        # The standalone program produces JUnit from real unittest assertions in
        # the independent, frozen runner checkout. The expectation is the AC.
        program='''import unittest,xml.etree.ElementTree as E,sys
from app import value
class Behavior(unittest.TestCase):
 def test_value(self): self.assertEqual(value(),1)
r=unittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromTestCase(Behavior))
s=E.Element('testsuite',tests=str(r.testsRun),failures=str(len(r.failures)),errors=str(len(r.errors)),skipped='0')
c=E.SubElement(s,'testcase',name='test_value')
for _,msg in r.failures: E.SubElement(c,'failure').text=msg
for _,msg in r.errors: E.SubElement(c,'error').text=msg
E.ElementTree(s).write(sys.argv[1]); sys.exit(0 if r.wasSuccessful() else 1)
'''
        payload={'candidate_id':first,'argv':[sys.executable,'-B','-c',program,'{junit_xml}'],'report_format':'junit'}
        failed=workflow.command(self.scope(tester),'verify',payload,command_id='actual-failing-test')
        self.assertEqual(failed['test_stats']['mechanical_result'],'FAIL',failed)
        failure=failed['publication']['verification_id']
        self.close(test_process,tester)
        self.assertCommitted(workflow.command(self.scope(coord),'decide',{'candidate_id':first,'decision':'REWORK'},command_id='rework'))
        new_process,new_dev=self.bind('A')
        self.assertEqual(new_dev['attempt'],2)
        second=self.native_candidate(new_dev,1)
        self.close(new_process,new_dev)
        new_test_process,new_tester=self.bind('TESTER')
        passed=workflow.command(self.scope(new_tester),'verify',
            {**payload,'candidate_id':second,'supersedes':[failure]},command_id='actual-passing-retest')
        self.assertEqual(passed['test_stats']['mechanical_result'],'PASS',passed)
        self.assertCommitted(passed['publication'])
        self.close(new_test_process,new_tester)
        self.assertTrue(self.ledger.acceptance(second)['ready'])
        self.assertCommitted(workflow.command(self.scope(coord),'decide',{'candidate_id':second,'decision':'APPROVE'},command_id='approve'))
        delivery=self.root/'delivery.json'; delivery.write_text(json.dumps({'candidate_id':second,'scope':'local isolated delivery'}))
        self.assertCommitted(workflow.command(self.scope(coord),'decide',{'candidate_id':second,'decision':'COMPLETE',
            'evidence':{'delivery':{'path':str(delivery)}}},command_id='complete'))
        snapshot=self.ledger.snapshot()
        self.assertEqual(next(t for t in snapshot['tasks'] if t['task_id']=='T1')['phase'],'COMPLETED')
        self.assertEqual(len(snapshot['candidates']),2)
        self.assertEqual(len(snapshot['verifications']),2)
        self.assertEqual(self.git('rev-parse','HEAD'),self.base)
        before=self.ledger.snapshot()
        reports=[]
        def analyze(facts):
            reports.append(facts)
            return {'model_request_id':'fake-t1-analysis','model':'offline-provider','route':'unknown',
                    'analysis':{'sections':{name:['**状态：** 当前仅核对到本单证据，待验收。'] for name in
                                ('当前工作','本次完成','阻塞与依赖','下一步计划')},
                                'evidence_ids':facts['evidence_ids']}}
        report=generate_periodic(self.ledger,'A',self.now-60,self.now,usage_index=self.root/'usage.db',
            project=self.root,analyze=analyze,command_id='t1-report',pool_path=self.pool_path)
        self.assertEqual(report['generation']['state'],'GENERATED')
        sent=[]
        def send(text):
            sent.append(text); return {'message_id':'fake-delivery'}
        delivered=deliver_notification(self.ledger,report['notification_id'],send,command_id='t1-send')
        self.assertEqual(delivered['state'],'SENT')
        deliver_notification(self.ledger,report['notification_id'],send,command_id='t1-send-replay')
        self.assertEqual(len(reports),1); self.assertEqual(len(sent),1)
        after=self.ledger.snapshot()
        for field in ('works','roles','executions','event_high_water'):
            self.assertEqual(before[field],after[field])
        self.close(coord_process,coord)

    def test_same_work_survives_two_ended_executions_and_old_cursor_cannot_ack_new_event(self):
        first_process,first=self.bind('A')
        issued=self.ledger.work_inputs(first,limit=100,command_id='first-input')
        self.assertCommitted(workflow.command(self.scope(first),'checkpoint',{
            'checkpoint':{'next':'保留本任务下一语义步骤'},'processed_through':issued['through'],
            'yield_execution':True},command_id='yield'))
        self.close(first_process,first)
        second_process,second=self.bind('A')
        self.assertEqual(first['work_id'],second['work_id'])
        self.assertGreater(second['execution_generation'],first['execution_generation'])
        with self.assertRaises(ValueError):
            workflow.command(self.scope(first),'checkpoint',{'checkpoint':{}},command_id='old-write')
        page=self.ledger.work_inputs(second,command_id='second-input')
        source={'authenticated':True,'current_full':True,'message_id':'new-event','user_id':'u',
                'primary_text':'全部权限：暂停测试者'}
        event=self.assertCommitted(self.ledger.control_request('pause',{'run_id':'run-one','role':'TESTER'},source,command_id='new-control'))
        self.assertCommitted(workflow.command(self.scope(second),'checkpoint',{
            'checkpoint':{'next':'读取后来事件'},'processed_through':page['through']},command_id='old-prefix'))
        pending=self.ledger.work_inputs(second,command_id='pending-new')
        self.assertIn(event['event_id'],[row['event_id'] for row in pending['events']])
        self.close(second_process,second)

    def test_existing_tick_collects_detached_result_and_restarts_waiting_work_only_on_event(self):
        from workflow_services import submit_service
        from test_workflow_services import ExternalService
        developer,dev=self.bind('A')
        candidate,_=self.candidate(dev)
        self.close(developer,dev)
        coordinator,coord=self.bind('COORDINATOR')
        provenance={'authenticated':True,'current_full':True,'message_id':'service-permission','chat_id':'chat-COORDINATOR',
                    'user_id':'user','primary_text':'全部权限：授权当前候选OCR一次'}
        grant=self.assertCommitted(workflow.submit_control(self.pool_path,'COORDINATOR','authorize_service',{
            'candidate_id':candidate,'service_kind':'ocr','max_calls':1,'expires_at':time.time()+3600},provenance))
        class Service(ExternalService):
            ready=False
            def collect(self,kind,reference):
                return super().collect(kind,reference) if self.ready else {'state':'WAITING'}
        service=Service(self.root)
        self.assertCommitted(submit_service(self.ledger,coord,candidate,'ocr',{'background_file':'fixture'},
            action_id='existing-service',authorization={'authorization_id':grant['authorization_id']},
            command_id='service-submit',adapter=service))
        inputs=self.ledger.work_inputs(coord,command_id='waiting-input')
        self.assertCommitted(workflow.command(self.scope(coord),'checkpoint',{
            'checkpoint':{'waiting_external':['existing-service'],'next':'核服务终态与完整问题'},
            'processed_through':inputs['through'],'yield_execution':True},command_id='waiting'))
        self.close(coordinator,coord)
        requested=[]
        dispatched=threading.Event()
        def dispatch(job):
            requested.append(job)
            if job=='COORDINATOR-poll': dispatched.set()
        result=workflow.event_tick(self.pool_path,home=self.home,dispatcher=dispatch,service_adapter=service,
            notification_transport=lambda row:{'delivered':True,'message_id':'offline-'+row['notification_id']})
        self.assertNotIn('COORDINATOR-poll',requested,result)
        self.assertFalse(self.ledger.admission_hint('COORDINATOR')['can_start_model'])
        self.assertEqual(service.calls,1)
        service.ready=True; requested.clear()
        result=workflow.event_tick(self.pool_path,home=self.home,dispatcher=dispatch,service_adapter=service,
            notification_transport=lambda row:{'delivered':True,'message_id':'offline-'+row['notification_id']})
        self.assertTrue(dispatched.wait(3),result)
        self.assertIn('COORDINATOR-poll',requested,result)
        self.assertEqual(result['services'][0]['state'],'RESULT_READY')
        self.assertEqual(self.ledger.snapshot()['verifications'],[])
        resumed,new_coord=self.bind('COORDINATOR')
        self.assertEqual(new_coord['work_id'],coord['work_id'])
        self.assertEqual(service.calls,1)
        self.close(resumed,new_coord)

    def test_running_shell_and_its_child_stop_on_control_revocation(self):
        worker,identity=self.bind('A')
        destination=self.path.parent/'workspaces'/identity['work_id']
        destination.parent.mkdir(parents=True)
        subprocess.run(['git','clone','-q','--shared',str(self.repo),str(destination)],check=True)
        program="from pathlib import Path; import time,subprocess,sys; Path('.git/started').touch(); subprocess.Popen([sys.executable,'-c',\"import time; from pathlib import Path; time.sleep(4); Path('.git/escaped-child').touch()\"]); time.sleep(30)"
        directive=workflow.tool_decision(self.scope(identity,destination),'terminal',{
            'command':shlex.join([sys.executable,'-c',program])})
        shell=subprocess.Popen(directive['args']['command'],shell=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.addCleanup(lambda:shell.kill() if shell.poll() is None else None)
        deadline=time.monotonic()+5
        while not (destination/'.git/started').exists() and shell.poll() is None and time.monotonic()<deadline:
            time.sleep(0.05)
        self.assertTrue((destination/'.git/started').exists())
        self.assertCommitted(self.ledger.freeze_admission('control revocation',command_id='revoke-running'))
        stdout,stderr=shell.communicate(timeout=8)
        self.assertEqual(shell.returncode,125,(stdout,stderr))
        tools=self.ledger.snapshot()['execution_tools']
        self.assertEqual(len(tools),1)
        self.close(worker,identity)
        self.assertEqual(workflow.role_state(self.pool_path,'A')['status'],'PAUSED')
        self.assertFalse((destination/'.git/escaped-child').exists())


if __name__=='__main__':unittest.main()
