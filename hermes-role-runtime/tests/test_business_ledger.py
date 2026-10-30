"""Real local work/execution/Git lifecycle tests; no production resources."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from zoneinfo import ZoneInfo

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from business_ledger import BusinessLedger, ROLES, _process


class LedgerFixture(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='business-ledger-test-')
        self.root=Path(self.temp.name)
        self.repo=self.root/'repo'; self.repo.mkdir()
        self.workers=[]
        self.git('init','-q')
        self.git('config','user.name','Isolated Test')
        self.git('config','user.email','test@example.invalid')
        (self.repo/'app.py').write_text('def value():\n    return 0\n')
        self.git('add','app.py'); self.git('commit','-qm','base')
        self.base=self.git('rev-parse','HEAD')
        self.now=datetime(2026,10,19,12,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        self.path=self.root/'isolated.runtime/business.sqlite3'
        self.ledger=BusinessLedger(self.path,clock=lambda:self.now)
        result=self.ledger.create_run('requirement',self.root,self.repo,['T1','T2','T3'],run_id='run-one',command_id='run')
        self.assertCommitted(result)
        self.plan=[self.task('T1','A'),self.task('T2','A',dependencies=['T1'])]
        self.assertCommitted(self.ledger.publish_plan('run-one',1,self.plan,command_id='plan'))
        self.assertCommitted(self.ledger.select_run('run-one',command_id='select'))
        self.assertCommitted(self.ledger.set_mode('ACTIVE',command_id='activate'))

    def tearDown(self):
        for worker in self.workers:
            try:
                os.killpg(worker.pid,signal.SIGKILL)
            except ProcessLookupError:
                pass
            worker.wait(timeout=5)
            if worker.stdout is not None:
                worker.stdout.close()
        self.temp.cleanup()

    def assertCommitted(self,result):
        self.assertEqual(result['status'],'COMMITTED',result)
        return result

    def git(self,*args,cwd=None):
        return subprocess.check_output(['git','-C',str(cwd or self.repo),*args],text=True,stderr=subprocess.DEVNULL).strip()

    def task(self,task,role,*,dependencies=None,scope=None,checks=None):
        return {'task_id':task,'role':role,'dependencies':dependencies or [],'scope':scope or ['app.py'],
                'base':self.base,'ac_version':'ac-sha256-frozen-v1','required_checks':checks or ['independent_test']}

    def worker(self,program=None):
        process=subprocess.Popen([sys.executable,'-c',program or 'import time; time.sleep(120)'],
                                 start_new_session=True,stdout=subprocess.PIPE,text=True)
        self.workers.append(process)
        observed=_process(process.pid)
        return process,{'execution_id':uuid.uuid4().hex,'pid':process.pid,
                        'process_start':observed['process_start'],'scope':observed['cgroup'],
                        'generation':self.ledger.mode()['generation']}

    def bind(self,role):
        process,spec=self.worker()
        result=self.assertCommitted(self.ledger.bind_execution(role,spec,command_id='bind-'+spec['execution_id']))
        self.assertIsNotNone(result.get('identity'),result)
        return process,result['identity']

    def close(self,process,identity):
        process.terminate(); process.wait(timeout=5)
        return self.assertCommitted(self.ledger.end_execution(identity,command_id='end-'+identity['execution_id']))

    def candidate(self,identity,value=1):
        (self.repo/'app.py').write_text('def value():\n    return '+str(value)+'\n')
        self.git('add','app.py'); self.git('commit','-qm','candidate '+str(value))
        evidence=self.root/('self-test-'+uuid.uuid4().hex+'.txt')
        evidence.write_text('actual frozen development evidence\n')
        spec={'repo':str(self.repo),'base':self.base,'head':self.git('rev-parse','HEAD'),
              'ac_version':'ac-sha256-frozen-v1','evidence':[{'path':str(evidence)}]}
        result=self.assertCommitted(self.ledger.deliver(identity,spec,'已冻结候选并完成本地开发检查',command_id='deliver-'+identity['execution_id']))
        return result['candidate_id'],spec

    def actual_test(self,candidate_id,*,expected=1):
        candidate=next(v for v in self.ledger.snapshot()['candidates'] if v['candidate_id']==candidate_id)
        directory=self.root/('tester-'+uuid.uuid4().hex)
        self.git('worktree','add','--detach','--quiet',str(directory),candidate['head'])
        output=self.root/('junit-'+uuid.uuid4().hex+'.xml')
        # This is a JUnit adapter over a real unittest execution in an independent
        # frozen Git worktree. Result fields come from TestResult, not ledger writes.
        program='''import importlib.util,sys,unittest,xml.etree.ElementTree as E
spec=importlib.util.spec_from_file_location('app',sys.argv[1]); app=importlib.util.module_from_spec(spec); spec.loader.exec_module(app)
class Behavior(unittest.TestCase):
 def test_value(self): self.assertEqual(app.value(),int(sys.argv[3]))
result=unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromTestCase(Behavior))
suite=E.Element('testsuite',tests=str(result.testsRun),failures=str(len(result.failures)),errors=str(len(result.errors)),skipped=str(len(result.skipped)))
case=E.SubElement(suite,'testcase',name='test_value',classname='Behavior')
for _,message in result.failures: E.SubElement(case,'failure').text=message
for _,message in result.errors: E.SubElement(case,'error').text=message
E.ElementTree(suite).write(sys.argv[2],encoding='utf-8')
sys.exit(0 if result.wasSuccessful() else 1)
'''
        run=subprocess.run([sys.executable,'-B','-c',program,str(directory/'app.py'),str(output),str(expected)],capture_output=True,text=True)
        self.assertEqual(self.git('rev-parse','HEAD',cwd=directory),candidate['head'])
        self.assertEqual(self.git('status','--porcelain',cwd=directory),'')
        return {'path':str(output),'head':candidate['head'],'exit_code':run.returncode},run


class BusinessLedgerTests(LedgerFixture):
    def test_readonly_missing_never_creates_paths_and_roles_remain_five(self):
        missing=self.root/'absent/path/ledger.sqlite3'
        with self.assertRaises(FileNotFoundError):
            BusinessLedger(missing,readonly=True)
        self.assertFalse(missing.parent.exists())
        reader=BusinessLedger(self.path,readonly=True)
        self.assertEqual(tuple(r['role'] for r in reader.snapshot()['roles']),ROLES)
        with self.assertRaises(PermissionError):
            reader.select_run('run-one',command_id='read-write-denied')

    def test_plan_dag_scope_and_inflight_version_are_atomic(self):
        _,coord=self.bind('COORDINATOR')
        cyclic=[self.task('T1','A',dependencies=['T2']),self.task('T2','A',dependencies=['T1'])]
        result=self.ledger.publish_plan('run-one',2,cyclic,identity=coord,command_id='cycle')
        self.assertEqual(result['code'],'PLAN_DEPENDENCY_CYCLE')
        self.assertEqual(self.ledger.snapshot()['runs'][0]['plan_version'],1)
        bad=[self.task('NOT-ACCEPTED','A')]
        self.assertEqual(self.ledger.publish_plan('run-one',2,bad,identity=coord,command_id='outside')['code'],
                         'TASK_OUTSIDE_ACCEPTED_SCOPE_OR_DUPLICATE')
        _,dev=self.bind('A')
        changed=[{**self.plan[0],'scope':['other.py']},self.plan[1]]
        self.assertEqual(self.ledger.publish_plan('run-one',2,changed,identity=coord,command_id='inflight')['code'],
                         'IN_FLIGHT_TASK_SPEC_IMMUTABLE')
        self.assertTrue(self.ledger.validate_identity(dev)['valid'])

    def test_staged_dependencies_allow_contract_work_early_but_keep_integration_and_acceptance_gates(self):
        _,coord=self.bind('COORDINATOR')
        original=self.plan[1]
        interface={'symbol':'value','contract':'returns configured value'}
        provider={**self.plan[0],'provided_contracts':[{'contract_id':'T1-value-v1',
            'version':'t1-interface-v1','interface':interface}]}
        self.assertCommitted(self.ledger.publish_plan('run-one',2,[provider,original],
            identity=coord,command_id='provider-contract-plan'))
        developer,dev=self.bind('A')
        source_candidate,source_spec=self.candidate(dev,value=1)
        self.close(developer,dev)
        consumer={**original,'role':'B','base':self.base,
            'dependencies':['T1'],
            'stage_dependencies':{'development_start':[],'integration':['T1'],'acceptance':['T1']},
            'dependency_contracts':[{'task_id':'T1','contract_id':'T1-value-v1','version':'t1-interface-v1',
                'interface':interface,
                'simulation':{'module':'fixture','replacement':'real T1 import after merge'}}],
            'ac':{'behavior':'consumer uses value()'},
            'acceptance':{'success':'value() contract is preserved after integration'},
            'inputs':{'dependency':'T1 interface t1-interface-v1'},
            'stop_conditions':['interface version changes'],
            'minimum_reading_set':['app.py:value','T1 interface contract v1'],
            'risk_scenarios':[{'id':'R1','scenario':'provider API changes','invariant':'typed contract preserved',
                               'verification':'contract test'}],
            'test_environment':{'kind':'local unittest','fixtures':['fixture T1 provider'],'cleanup':'temporary checkout'},
            'escalation_conditions':['interface version mismatch','contract test cannot model provider']}
        revised=[provider,consumer]
        self.assertCommitted(self.ledger.publish_plan('run-one',3,revised,identity=coord,command_id='staged-plan'))
        self.assertEqual(True,self.ledger.dependency_readiness('run-one','T2','development_start')['ready'])
        self.assertEqual(False,self.ledger.dependency_readiness('run-one','T2','integration')['ready'])
        _,task=self.bind('B')
        self.assertEqual('T2',task['task_id'])

        # Model a verified recorded merge for the source task. The integration
        # gate opens only when that commit is included in this task's frozen base.
        with self.ledger._connection() as db:
            db.execute("INSERT INTO authorizations VALUES('fixture-merge-auth','merge','{}','{}',?,NULL)",(self.now,))
            db.execute("INSERT INTO merges VALUES(?,?,?,?,?,?)",
                       ('fixture-merge',source_candidate,'fixture-merge-auth',source_spec['head'],'{}',self.now))
            db.execute("UPDATE tasks SET base=? WHERE run_id='run-one' AND task_id='T2'",(source_spec['head'],))
        self.assertEqual(True,self.ledger.dependency_readiness('run-one','T2','integration')['ready'])
        self.assertEqual(False,self.ledger.dependency_readiness('run-one','T2','acceptance')['ready'])

        # An edge may be relaxed only at development start. Every prerequisite
        # still has to gate integration, and integration prerequisites still
        # have to gate final acceptance.
        for label,stages in (
            ('start-only',{'development_start':['T1'],'integration':[],'acceptance':[]}),
            ('integration-only',{'development_start':[],'integration':['T1'],'acceptance':[]}),
        ):
            malformed_stages={**consumer,'stage_dependencies':stages,
                              'dependency_contracts':[]}
            rejected=self.ledger.publish_plan('run-one',4,[provider,malformed_stages],
                identity=coord,command_id='staged-plan-'+label)
            self.assertEqual('STAGED_DEPENDENCY_GATES_NOT_MONOTONIC',rejected['code'])

        # A staged edge without a versioned interface and executable simulation
        # is rejected before it can make a dependent task claimable.
        malformed={**consumer,'dependency_contracts':[]}
        self.assertEqual('PRESTART_DEPENDENCY_CONTRACT_REQUIRED',
            self.ledger.publish_plan('run-one',4,[provider,malformed],identity=coord,
                                     command_id='staged-plan-missing-contract')['code'])

        stale_provider={**provider,'provided_contracts':[{'contract_id':'T1-value-v1',
            'version':'t0-interface','interface':interface}]}
        self.assertEqual('DEPENDENCY_INTERFACE_CONTRACT_MISMATCH',
            self.ledger.publish_plan('run-one',4,[stale_provider,consumer],identity=coord,
                command_id='staged-plan-provider-mismatch')['code'])

    def test_concurrent_role_claim_is_one_writer_one_claim_event(self):
        _,first=self.worker(); _,second=self.worker()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results=list(executor.map(lambda pair:self.ledger.bind_execution('A',pair[1],command_id=pair[0]),
                                      [('claim-one',first),('claim-two',second)]))
        self.assertEqual(sum(r['status']=='COMMITTED' for r in results),1,results)
        self.assertEqual(sum(r.get('code')=='OLD_WRITER_NOT_CLEARED' for r in results),1,results)
        events=[e for e in self.ledger.events() if e['kind']=='WORK_CLAIMED']
        self.assertEqual(len(events),1)
        self.assertEqual(len(self.ledger.outbox_items()),1)
        winner=next(v for v in results if v['status']=='COMMITTED')
        retry=self.ledger.bind_execution('A',first,command_id='claim-one')
        self.assertEqual(retry['status'],'DUPLICATE')
        conflict=self.ledger.bind_execution('B',first,command_id='claim-one')
        self.assertEqual(conflict['code'],'COMMAND_ID_CONFLICT')
        self.assertEqual(self.ledger.current_work('A')['work_id'],winner['work']['work_id'])

    def test_cross_execution_checkpoint_and_event_prefix_survive(self):
        process,identity=self.bind('A')
        inputs=self.assertCommitted(self.ledger.work_inputs(identity,command_id='inputs'))
        later=self.ledger.control_request('continue',{'run_id':'run-one'},
            {'authenticated':True,'current_full':True,'message_id':'later','user_id':'u'},command_id='later')
        self.assertGreater(later['event_id'],inputs['through'])
        self.assertEqual(self.ledger.ack_events(identity,later['event_id'],command_id='swallow')['code'],'EVENT_PREFIX_NOT_ISSUED')
        checkpoint={'next':'继续实现剩余断言','last_action':'真实文件已检查','unresolved_failures':['must-keep']}
        self.assertCommitted(self.ledger.checkpoint(identity,checkpoint,processed_through=inputs['through'],yield_execution=True,command_id='yield'))
        _,other=self.worker()
        self.assertEqual(self.ledger.bind_execution('A',other,command_id='too-early')['code'],'OLD_WRITER_NOT_CLEARED')
        self.close(process,identity)
        new_process,new_identity=self.bind('A')
        work=self.ledger.current_work('A')
        self.assertEqual(work['work_id'],identity['work_id'])
        self.assertEqual(work['checkpoint'],checkpoint)
        self.assertEqual(work['processed_through'],inputs['through'])
        self.assertEqual(new_identity['execution_generation'],identity['execution_generation']+1)
        self.assertFalse(self.ledger.validate_identity(identity)['valid'])
        remaining=self.ledger.work_inputs(new_identity,command_id='remaining')['events']
        self.assertIn(later['event_id'],[e['event_id'] for e in remaining])

    def test_live_and_leftover_child_writer_prevent_reassignment(self):
        program="import subprocess,sys,time; child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)']); print(child.pid,flush=True); time.sleep(120)"
        process,spec=self.worker(program)
        child_pid=int(process.stdout.readline())
        identity=self.assertCommitted(self.ledger.bind_execution('A',spec,command_id='child-bind'))['identity']
        result=self.ledger.end_execution(identity,{'scope_empty':True},command_id='false-proof')
        self.assertEqual(result['code'],'WRITER_STILL_ALIVE')
        process.terminate(); process.wait(timeout=5)
        self.assertEqual(self.ledger.end_execution(identity,command_id='child-left')['code'],'CHILD_WRITER_STILL_ALIVE')
        os.kill(child_pid,signal.SIGKILL)
        # Reaped/orphan zombie holds no writer. Linux observes it as Z.
        for _ in range(100):
            observed=_process(child_pid)
            if not observed or observed.get('state')=='Z': break
        result=self.ledger.end_execution(identity,command_id='child-ended')
        self.assertCommitted(result)

    def test_pause_continue_does_not_resume_and_task_cancel_drains(self):
        _,identity=self.bind('A')
        provenance={'authenticated':True,'current_full':True,'message_id':'pause','user_id':'u'}
        request=self.assertCommitted(self.ledger.control_request('pause',{'run_id':'run-one'},provenance,command_id='pause'))
        self.assertCommitted(self.ledger.apply_control(request['request_id'],command_id='apply-pause'))
        self.assertFalse(self.ledger.validate_identity(identity)['valid'])
        self.assertTrue(self.ledger.validate_identity(identity,allow_drain=True)['valid'])
        request=self.ledger.control_request('continue',{'run_id':'run-one'},provenance,command_id='continue')
        self.ledger.apply_control(request['request_id'],command_id='apply-continue')
        self.assertEqual(self.ledger.snapshot()['runs'][0]['control'],'PAUSED')
        request=self.ledger.control_request('resume',{'run_id':'run-one'},provenance,command_id='resume')
        self.ledger.apply_control(request['request_id'],command_id='apply-resume')
        self.assertTrue(self.ledger.validate_identity(identity)['valid'])
        request=self.ledger.control_request('cancel',{'run_id':'run-one','task_id':'T1'},provenance,command_id='cancel')
        self.ledger.apply_control(request['request_id'],command_id='apply-cancel')
        self.assertFalse(self.ledger.validate_identity(identity)['valid'])
        self.assertCommitted(self.ledger.checkpoint(identity,{'next':'取消安全收口'},command_id='cancel-cp'))
        self.assertCommitted(self.ledger.release_work(identity,reason='任务已获授权取消',command_id='cancel-release'))

    def test_real_git_delivery_independent_fail_rework_pass_and_completion(self):
        dev_process,dev=self.bind('A')
        coord_process,coord=self.bind('COORDINATOR')
        candidate,spec=self.candidate(dev,value=1)
        self.assertIsNone(self.ledger.current_work('A'))
        _,blocked=self.worker()
        self.assertEqual(self.ledger.bind_execution('A',blocked,command_id='old-writer')['code'],'OLD_WRITER_NOT_CLEARED')
        self.close(dev_process,dev)
        test_process,tester=self.bind('TESTER')
        fail_evidence,run=self.actual_test(candidate,expected=2)
        self.assertEqual(run.returncode,1)
        fail=self.assertCommitted(self.ledger.publish_verification(tester,candidate,'independent_test','FAIL',fail_evidence,command_id='fail'))
        self.close(test_process,tester)
        self.assertFalse(self.ledger.acceptance(candidate)['ready'])
        self.assertEqual(self.ledger.decide(coord,candidate,'APPROVE',command_id='premature')['code'],'ACCEPTANCE_GATES_NOT_CLEAR')
        rework=self.assertCommitted(self.ledger.decide(coord,candidate,'REWORK',command_id='rework',evidence={'reason':'修复独立断言失败'}))
        self.assertEqual(rework['attempt'],2)
        dev2_process,dev2=self.bind('A')
        candidate2,spec2=self.candidate(dev2,value=2)
        self.close(dev2_process,dev2)
        test2_process,tester2=self.bind('TESTER')
        pass_evidence,run=self.actual_test(candidate2,expected=2)
        self.assertEqual(run.returncode,0,run.stderr)
        rejected=self.ledger.publish_verification(tester2,candidate2,'independent_test','PASS',pass_evidence,
                              command_id='missing-failure-closure')
        self.assertEqual(rejected['code'],'FAILURE_CLOSURE_REQUIRED')
        self.assertTrue(self.ledger.validate_identity(tester2)['valid'])
        passed=self.assertCommitted(self.ledger.publish_verification(tester2,candidate2,'independent_test','PASS',pass_evidence,
                              supersedes=[fail['verification_id']],command_id='pass'))
        original_sha=next(row for row in self.ledger.snapshot()['verifications'] if row['verification_id']==passed['verification_id'])['evidence']['sha256']
        # Reproduce a pre-fix historical PASS which omitted closure and ended
        # its TESTER work. The repair must use the exact existing test evidence.
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE verifications SET supersedes='[]' WHERE verification_id=?",(passed['verification_id'],))
        self.close(test2_process,tester2)
        self.assertFalse(self.ledger.acceptance(candidate2)['ready'])
        preview=self.ledger.acceptance(candidate2)['failure_closure_preview']
        self.assertEqual([fail['verification_id']],[row['verification_id'] for row in preview])
        self.assertEqual({'source_candidate_id':candidate,'source_attempt':1,
                          'current_candidate_id':candidate2,'current_attempt':2,
                          'closure_eligible':True},
                         {key:preview[0][key] for key in ('source_candidate_id','source_attempt',
                          'current_candidate_id','current_attempt','closure_eligible')})
        repair_process,repair=self.bind('TESTER')
        from workflow_progression import next_actions
        action=next_actions({'role':'TESTER','work':self.ledger.current_work('TESTER')},self.ledger)[0]
        self.assertEqual('close_verification',action['action'])
        self.assertEqual(passed['verification_id'],action['payload']['verification_id'])
        self.assertEqual([fail['verification_id']],action['payload']['supersedes'])
        closure=self.assertCommitted(self.ledger.close_verification(repair,passed['verification_id'],
                              [fail['verification_id']],command_id='repair-historical-closure'))
        repaired=next(row for row in self.ledger.snapshot()['verifications'] if row['verification_id']==closure['verification_id'])
        self.assertEqual(repaired['evidence']['sha256'],original_sha)
        self.assertEqual(repaired['evidence']['path'],pass_evidence['path'])
        self.close(repair_process,repair)
        self.assertTrue(self.ledger.acceptance(candidate2)['ready'])
        self.assertCommitted(self.ledger.decide(coord,candidate2,'APPROVE',command_id='approve'))
        receipt=self.root/'delivery.txt'; receipt.write_text('当前候选已批准，交付位置与限制已记录')
        self.assertCommitted(self.ledger.decide(coord,candidate2,'COMPLETE',evidence={'delivery':{'path':str(receipt)}},command_id='complete'))
        phase={t['task_id']:t['phase'] for t in self.ledger.snapshot()['tasks']}
        self.assertEqual(phase,{'T1':'COMPLETED','T2':'PENDING'})
        _,next_identity=self.bind('A')
        self.assertEqual(next_identity['task_id'],'T2')
        self.assertEqual(len(self.ledger.snapshot()['candidates']),2)

    def test_wrong_head_zero_tests_tampered_evidence_and_old_fail_do_not_pass(self):
        process,identity=self.bind('A')
        candidate,spec=self.candidate(identity)
        self.close(process,identity)
        _,tester=self.bind('TESTER')
        evidence,run=self.actual_test(candidate)
        wrong={**evidence,'head':self.base}
        self.assertEqual(self.ledger.publish_verification(tester,candidate,'independent_test','PASS',wrong,command_id='wrong-head')['code'],'TEST_HEAD_MISMATCH')
        zero=self.root/'zero.xml'; zero.write_text('<testsuite tests="0" failures="0" errors="0" skipped="0"/>')
        self.assertEqual(self.ledger.publish_verification(tester,candidate,'independent_test','PASS',
            {'path':str(zero),'head':spec['head'],'exit_code':0},command_id='zero')['code'],'ZERO_EXECUTED_TESTS')
        self.assertCommitted(self.ledger.publish_verification(tester,candidate,'independent_test','PASS',evidence,command_id='good'))
        self.assertTrue(self.ledger.acceptance(candidate)['ready'])
        Path(evidence['path']).write_text('<testsuite tests="1"/>')
        self.assertEqual(self.ledger.acceptance(candidate)['reasons'],['EVIDENCE_HASH_MISMATCH'])

    def test_outbox_unknown_never_resends_and_reports_do_not_create_events(self):
        _,_=self.bind('A')
        notice=self.ledger.outbox_items()[0]
        self.assertCommitted(self.ledger.claim_delivery(notice['notification_id'],command_id='sending'))
        self.assertCommitted(self.ledger.ack_delivery(notice['notification_id'],'UNKNOWN',command_id='unknown'))
        self.assertEqual(self.ledger.claim_delivery(notice['notification_id'],command_id='no-retry')['code'],'DELIVERY_NOT_PENDING')
        high=self.ledger.snapshot()['event_high_water']
        report=self.ledger.queue_notification('period-summary',{'analysis':'实质总结','generation_state':'GENERATED'},
            source_event_id=notice['event_id'],run_id='run-one',role='A',command_id='report')
        self.assertCommitted(report)
        self.assertEqual(self.ledger.snapshot()['event_high_water'],high)
        self.now=datetime(2026,10,19,23,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        night=self.ledger.queue_notification('period-summary',{'analysis':'夜间总结'},source_event_id=notice['event_id'],command_id='night')
        self.assertEqual(night['state'],'SUPPRESSED')

    def test_mode_change_requires_writer_end_and_monotonic_generation(self):
        process,identity=self.bind('A')
        self.assertEqual(self.ledger.set_mode('LEGACY',migration_manifest={'final_high_water':1},command_id='live-mode')['code'],'WRITER_NOT_CLEARED')
        self.close(process,identity)
        self.assertEqual(self.ledger.set_mode('LEGACY',command_id='missing-export')['code'],'FINAL_EXPORT_MANIFEST_REQUIRED')
        before=self.ledger.mode()['generation']
        result=self.assertCommitted(self.ledger.set_mode('LEGACY',migration_manifest={'final_high_water':self.ledger.snapshot()['event_high_water']},command_id='rollback'))
        self.assertEqual(result['generation'],before+1)
        self.assertFalse(self.ledger.validate_identity(identity)['valid'])
        self.assertEqual(self.ledger.export_state()['state']['mode'],'LEGACY')

    def test_switch_restores_old_work_only_for_drain_then_new_run(self):
        process,identity=self.bind('A')
        self.assertCommitted(self.ledger.checkpoint(identity,{'next':'旧run安全收口'},yield_execution=True,command_id='old-cp'))
        self.close(process,identity)
        self.assertCommitted(self.ledger.create_run('other',self.root,self.repo,['NEW'],run_id='run-two',command_id='run-two'))
        self.assertCommitted(self.ledger.select_run('run-two',command_id='select-two'))
        new_process,spec=self.worker()
        resumed=self.assertCommitted(self.ledger.bind_execution('A',spec,command_id='old-resume'))
        self.assertTrue(resumed['drain_only'])
        self.assertEqual(resumed['identity']['run_id'],'run-one')
        self.assertEqual(resumed['work']['work_id'],identity['work_id'])
        self.assertFalse(self.ledger.validate_identity(resumed['identity'])['valid'])
        self.assertTrue(self.ledger.validate_identity(resumed['identity'],allow_drain=True)['valid'])
        self.assertCommitted(self.ledger.release_work(resumed['identity'],reason='旧run检查点已保存',command_id='drain-release'))
        self.close(new_process,resumed['identity'])
        _,coord=self.bind('COORDINATOR')
        self.assertEqual(coord['run_id'],'run-two')

    def test_detached_service_terminal_keeps_old_run_without_role_or_owner(self):
        process,identity=self.bind('A')
        candidate,spec=self.candidate(identity)
        self.close(process,identity)
        coord_process,coord=self.bind('COORDINATOR')
        token='isolated-unpredictable-service-token-'+uuid.uuid4().hex
        authorization={'authorization_id':'explicit-review-auth','candidate_id':candidate,'authorized':True,
                       'expires_at':self.now+3600,'service_id':'ocr-service-one',
                       'service_token_sha256':hashlib.sha256(token.encode()).hexdigest()}
        provenance={'authenticated':True,'current_full':True,'message_id':'service-grant','user_id':'u','primary_text':'本轮授予全部权限：对当前候选OCR一次'}
        self.assertCommitted(self.ledger.grant_external_authorization({'authorization_id':'explicit-review-auth','candidate_id':candidate,
            'service_kind':'ocr','max_calls':1,'expires_at':self.now+3600},provenance,command_id='grant-service'))
        authorization={key:authorization[key] for key in ('authorization_id','service_id','service_token_sha256')}
        self.assertCommitted(self.ledger.register_external_action(coord,'action-one','ocr',candidate,authorization,command_id='submit-service'))
        self.assertCommitted(self.ledger.checkpoint(coord,{'next':'等待独立服务终态'},yield_execution=True,command_id='coord-wait'))
        self.close(coord_process,coord)
        self.assertCommitted(self.ledger.mark_external_unknown('action-one',reason='caller已退出，先核实独立服务',command_id='unknown-action'))
        self.assertCommitted(self.ledger.create_run('other',self.root,self.repo,['NEW'],run_id='run-two',command_id='service-new-run'))
        self.assertCommitted(self.ledger.select_run('run-two',command_id='service-select-new'))
        terminal=self.root/'service-terminal.json'
        terminal.write_text(json.dumps({'action_id':'action-one','candidate_id':candidate,'run_id':'run-one',
            'task_id':'T1','attempt':1,'base':self.base,'head':spec['head'],'ac_version':spec['ac_version'],
            'service_id':'ocr-service-one','result':'PASS','complete':True}))
        denied=self.ledger.record_service_result('action-one','PASS',{'path':str(terminal)},
            service_identity={'service_id':'ocr-service-one','token':'wrong'},command_id='wrong-service-credential')
        self.assertEqual(denied['code'],'DETACHED_SERVICE_IDENTITY_UNVERIFIED')
        result=self.assertCommitted(self.ledger.record_service_result('action-one','PASS',{'path':str(terminal)},
            service_identity={'service_id':'ocr-service-one','token':token},command_id='service-result'))
        self.assertEqual(result['run_id'],'run-one')
        self.assertEqual(self.ledger.mode()['current_run_id'],'run-two')
        self.assertEqual(len(self.ledger.snapshot()['roles']),5)
        self.assertFalse(any(w['role']=='REVIEWER' for w in self.ledger.snapshot()['works']))

    def test_staged_import_freeze_contract_and_atomic_export(self):
        other=BusinessLedger(self.root/'staging/business.sqlite3',clock=lambda:self.now)
        source=self.root/'legacy-source.json'; source.write_text('{"attempt":0,"status":null}\n')
        proposal={'source':str(source),'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
                  'identity_fields':{'attempt':{'present':True,'type':'int','value':0},
                                     'status':{'present':True,'type':'null','value':None}},
                  'raw_source':json.loads(source.read_bytes())}
        self.assertCommitted(other.stage_import({'high_water':11},[proposal],command_id='stage-history'))
        frozen=other.export_state()
        self.assertEqual(frozen['state']['tasks'],[])
        self.assertEqual(frozen['state']['imports'][0]['manifest']['proposals'][0]['raw_source']['attempt'],0)
        self.assertFalse(frozen['state']['imports'][0]['manifest']['schedulable'])
        self.assertIn('events',frozen['state'])
        freeze=self.assertCommitted(self.ledger.freeze_admission('迁移核对',command_id='freeze'))
        _,execution=self.worker()
        self.assertEqual(self.ledger.bind_execution('A',execution,command_id='frozen-bind')['code'],'MIGRATION_ADMISSION_FROZEN')
        opened=self.assertCommitted(self.ledger.open_admission(expected_generation=self.ledger.mode()['generation'],
            migration_manifest={'validated':True,'gates':['P0','T1','T2','M','R']},command_id='open'))
        self.assertEqual(opened['admission_open'],1)
        _,coord=self.bind('COORDINATOR')
        contract={'ac':{'text':'value 必须返回指定整数','source_sha256':'a'*64},
                  'inputs':[{'path':'app.py','purpose':'固定业务输入'}],
                  'stop_conditions':['身份冲突立即停止新动作'],'acceptance':['独立断言通过']}
        plan=[{**self.plan[0],**contract},self.plan[1]]
        self.assertCommitted(self.ledger.publish_plan('run-one',2,plan,identity=coord,command_id='contract-plan'))
        actual=next(t for t in self.ledger.snapshot()['tasks'] if t['task_id']=='T1')
        self.assertEqual(actual['contract'],contract)

    def test_deterministic_dedicated_clone_delivers_but_arbitrary_clone_rejected(self):
        process,identity=self.bind('A')
        destination=self.ledger.path.parent/'workspaces'/identity['work_id']
        destination.parent.mkdir(parents=True)
        subprocess.run(['git','clone','--shared','--no-checkout','--quiet',str(self.repo),str(destination)],check=True)
        self.git('checkout','--detach',self.base,cwd=destination)
        self.git('config','user.name','Isolated Developer',cwd=destination)
        self.git('config','user.email','dev@example.invalid',cwd=destination)
        (destination/'app.py').write_text('def value():\n    return 9\n')
        self.git('add','app.py',cwd=destination); self.git('commit','-qm','isolated candidate',cwd=destination)
        arbitrary=self.root/'arbitrary-clone'
        subprocess.run(['git','clone','--quiet',str(destination),str(arbitrary)],check=True)
        evidence=self.root/'clone-evidence.txt'; evidence.write_text('独立clone真实代码证据')
        candidate={'repo':str(arbitrary),'base':self.base,'head':self.git('rev-parse','HEAD',cwd=destination),
                   'ac_version':'ac-sha256-frozen-v1','evidence':[{'path':str(evidence)}]}
        self.assertEqual(self.ledger.deliver(identity,candidate,'任意clone不得授予交付',command_id='wrong-clone')['code'],'CANDIDATE_SCOPE_MISMATCH')
        candidate['repo']=str(destination)
        result=self.assertCommitted(self.ledger.deliver(identity,candidate,'专属clone冻结交付',command_id='right-clone'))
        actual=next(c for c in self.ledger.snapshot()['candidates'] if c['candidate_id']==result['candidate_id'])
        self.assertEqual(actual['repo'],str(destination))
        self.assertEqual(self.git('rev-parse','HEAD'),self.base)


if __name__=='__main__':
    unittest.main()
