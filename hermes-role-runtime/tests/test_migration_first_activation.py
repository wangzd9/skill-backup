"""First activation through the real verifier on declared isolated environments.

P0 is a real live child; T1 executes a frozen checkout; T2 runs a real temporary
user service. M uses explicitly synthetic provider/channel authority fixtures.
This proves the cross-environment algorithm, NEVER production real-model M.
"""
import json
from pathlib import Path
import os
import signal
import subprocess
import unittest
import uuid

from tests import test_workflow_migration as mt
from tests import test_workflow_evidence as et
from tests import test_workflow_m_gate as mm
import workflow_migration as migration


class FirstActivationTests(unittest.TestCase):
    def fixture(self, cls):
        fixture=cls(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        return fixture

    def test_first_activation_all_real_verifier_branches_and_undeclared_pool_rejection(self):
        version=migration.code_version()['digest']
        target=self.fixture(mt.MigrationTests)
        target.coverage_patch.stop()  # Real version coverage, no verifier/coverage patches.
        acceptance=self.fixture(mt.MigrationTests); acceptance.coverage_patch.stop()
        _,live=acceptance.staged(False); acceptance.plan(live)
        self.assertEqual(live.set_mode('ACTIVE',command_id='acceptance-active')['status'],'COMMITTED')
        self.assertEqual(live.open_admission(command_id='acceptance-open',expected_generation=1,
            migration_manifest={'validated':True})['status'],'COMMITTED')
        _,identity=acceptance.worker(live)
        acceptance.write(acceptance.home/'runtime/role-pool-executions'/(identity['execution_id']+'.json'),{
            **identity,'schema_version':2,'workflow_version':1,'runtime_version_digest':version})
        p0={'pool_path':str(acceptance.pool),'home':str(acceptance.home),'generation':1,
            'execution_id':identity['execution_id']}

        business=self.fixture(et.WorkflowEvidenceTests)
        tested=business.execute(); self.assertEqual(tested['status'],'COMMITTED',tested)
        execution=next(r for r in business.ledger.snapshot()['executions'] if r['execution_id']==business.tester['execution_id'])
        os.kill(execution['pid'],signal.SIGTERM); os.waitpid(execution['pid'],0)
        self.assertEqual(business.ledger.end_execution(business.tester,command_id='tester-ended')['status'],'COMMITTED')
        coord=business.bind('COORDINATOR')
        self.assertEqual(business.ledger.decide(coord,business.candidate,'APPROVE',command_id='approve')['status'],'COMMITTED')
        delivery=business.root/'delivery.txt'; delivery.write_text('isolated delivery')
        self.assertEqual(business.ledger.decide(coord,business.candidate,'COMPLETE',
            evidence={'delivery':{'path':str(delivery)}},command_id='complete')['status'],'COMMITTED')
        t1={'database':str(business.ledger.path),'run_id':business.run,'candidate_id':business.candidate}

        identifier=uuid.uuid4().hex; unit='first-activation-fixture-'+identifier+'.service'
        spec=target.write(target.home/'service-spec.json',{'run_id':identifier,'unit':unit,'runtime_version_digest':version})
        self.addCleanup(lambda:subprocess.run(['systemctl','--user','stop',unit],capture_output=True,timeout=10))
        launched=subprocess.run(['systemd-run','--user','--quiet','--no-ask-password','--unit='+unit,
            '--property=Type=oneshot','--property=RemainAfterExit=yes','/bin/true'],capture_output=True,text=True,timeout=10)
        self.assertEqual(launched.returncode,0,launched.stderr)
        t2={'spec_path':str(spec),'spec_sha256':migration.file_sha(spec),'unit':unit,'service_id':identifier}

        model=mm.MGateContractTests(); model.acceptance_version=version
        model.setUp(); self.addCleanup(model.doCleanups); self.addCleanup(model.tearDown)
        model_proof=model.item['proof']
        environments={
            'P0':{'home':str(acceptance.home),'pool_path':str(acceptance.pool),'database':str(live.path),'run_id':'run-one'},
            'T1':{'database':str(business.ledger.path),'run_id':business.run},
            'T2':{'spec_path':str(spec),'unit':unit,'service_id':identifier},
            'M':{'home':str(model.home),'controlled_project':str(model.root),'run_id':'run-one'}}
        for env in environments.values(): env['purpose']='isolated_acceptance'
        manifest=migration.inspect(target.home,target.pool,at=target.now,acceptance_environments=environments)
        self.assertEqual(migration._version_coverage(manifest['version']),[])
        prepared=migration.prepare(manifest,target.proposal)
        self.assertEqual(prepared['result']['status'],'COMMITTED',prepared)
        from business_ledger import BusinessLedger
        ledger=BusinessLedger(migration.ledger_path(target.pool)); target.plan(ledger)
        self.assertEqual(ledger.mode()['mode'],'STAGED')
        paths=target.evidence(manifest)
        proofs={'P0':p0,'T1':t1,'T2':t2,'M':model_proof,
                'R':{'reviewed_by':'isolated-fixture','resolutions':[]}}
        for path in paths:
            row=json.loads(path.read_text()); row['proof']=proofs[row['gate']]
            row['offline_contract_fixture_only']=True
            path.write_text(json.dumps(row))
        p0_path=next(p for p in paths if p.name=='P0.json')
        row=json.loads(p0_path.read_text()); saved=json.loads(json.dumps(row))
        row['proof']['pool_path']=str(target.pool); p0_path.write_text(json.dumps(row))
        rejected=migration.activate(manifest,paths,at=target.now)
        self.assertEqual(rejected['status'],'REJECTED')
        self.assertTrue(any('ACCEPTANCE_ENVIRONMENT_MISMATCH' in v for v in rejected['readiness']['problems']))
        self.assertEqual(ledger.mode()['mode'],'STAGED')
        p0_path.write_text(json.dumps(saved))
        result=migration.activate(manifest,paths,at=target.now)
        self.assertEqual(result['status'],'COMMITTED',result)
        self.assertEqual(set(result['readiness']['gates']),set(migration.GATES))
        self.assertEqual(ledger.mode()['mode'],'ACTIVE')
        self.assertEqual(migration.activate(manifest,paths,at=target.now)['status'],'DUPLICATE')
