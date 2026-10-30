import copy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid

from business_ledger import BusinessLedger, _hash
from migration_reconciliation import accept, adopt_ocr


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.repo=self.root/'repo';self.repo.mkdir()
        self.git('init','-q');self.git('config','user.name','test');self.git('config','user.email','test@localhost')
        (self.repo/'x.py').write_text('x=1\n');self.git('add','.');self.git('commit','-qm','original')
        self.base=self.git('rev-parse','HEAD');self.evidence=self.root/'receipt.md';self.evidence.write_text('Original delivered candidate; independent review not done.\n')
        self.proof={'path':str(self.evidence),'sha256':hashlib.sha256(self.evidence.read_bytes()).hexdigest()}
        self.ledger=BusinessLedger(self.root/'business.sqlite3')
        self.run=self.ledger.create_run('req',self.root,self.repo,['D03-A','D02-A'],command_id='run')['run_id']
        self.tasks=[{'task_id':t,'role':'A','scope':['*.py'],'base':self.base,'ac_version':'ac1'} for t in ('D03-A','D02-A')]
        self.ledger.publish_plan(self.run,1,self.tasks,command_id='plan')
        self.ledger.freeze_admission('migration',command_id='freeze')
        self.records=[{'source':str(self.root/(t+'.json')),'source_sha256':'a'*64,'identity':{'task':t},'raw':{'task':t,'role':'A','attempt':a}} for t,a in [('D03','attempt-1-r1'),('D02','attempt-1-r2')]]
        self.manifest={'sha256':'b'*64,'records':self.records}
        staged=self.ledger.stage_import(self.manifest,[],command_id='import');self.import_id=staged['import_id']
        self.states=[{'task_id':task,'legacy_task':r['raw']['task'],'legacy_attempt':r['raw']['attempt'],'attempt':attempt,'phase':phase,'record_sha256':_hash([r['source'],r['source_sha256'],r['identity']]),'evidence':[self.proof],'checkpoint':{'summary':'original checkpoint','open_gates':['Independent verification pending']}} for task,r,attempt,phase in [('D03-A',self.records[0],2,'REVIEW'),('D02-A',self.records[1],3,'DEVELOPING')]]
        self.states[0]['candidate']={'repo':str(self.repo),'base':self.base,'head':self.base,'ac_version':'ac1'}
        self.review={'reviewed_by':'migration operator','inventory_manifest_sha256':self.manifest['sha256'],'task_states':self.states,'coordinator_checkpoint':{'pending_actions':['Review original D03 candidate'],'open_gates':['OCR pending']},'resolutions':[{'record_sha256':s['record_sha256'],'run_id':self.run,'task_id':s['task_id'],'attempt':s['attempt']} for s in self.states]}

    def git(self,*args):return subprocess.check_output(['git','-C',str(self.repo),*args],stderr=subprocess.DEVNULL,text=True).strip()

    def test_original_delivery_and_development_are_preserved_without_pass_or_writer(self):
        result=accept(self.ledger,self.import_id,self.run,self.review,command_id='accept')
        self.assertEqual('COMMITTED',result['status'],result)
        value=self.ledger.snapshot()
        self.assertEqual({t['task_id']:(t['phase'],t['attempt']) for t in value['tasks']},{'D03-A':('REVIEW',2),'D02-A':('DEVELOPING',3)})
        self.assertEqual(1,len(value['candidates']));self.assertEqual(self.base,value['candidates'][0]['head'])
        self.assertFalse(value['executions']);self.assertFalse(value['verifications'])
        self.assertEqual('D02-A',self.ledger.current_work('A')['task_id'])
        self.assertEqual('QUEUED',self.ledger.current_work('A')['state'])
        self.assertEqual(['OCR pending'],self.ledger.current_work('COORDINATOR')['checkpoint']['open_gates'])
        self.assertFalse(self.ledger.admission_hint('A')['can_start_model'])
        self.assertEqual('DUPLICATE',accept(self.ledger,self.import_id,self.run,self.review,command_id='accept')['status'])

    def test_wrong_identity_unreviewed_record_and_changed_receipt_are_rejected_atomically(self):
        for name,alter in [('identity',lambda r:r['task_states'][0].update(legacy_attempt='attempt-99')),('coverage',lambda r:r['resolutions'].pop()),('terminal',lambda r:r['task_states'][0].update(phase='COMPLETED')),('artifact',lambda r:r['task_states'][0]['evidence'][0].update(sha256='0'*64)),('mapping',lambda r:r['resolutions'][0].update(attempt=1)),('excluded-source',lambda r:r['resolutions'][0].update(excluded=True,reason='Cannot exclude a selected responsibility source'))]:
            with self.subTest(name=name):
                review=copy.deepcopy(self.review);alter(review)
                result=accept(self.ledger,self.import_id,self.run,review,command_id='reject-'+name)
                self.assertEqual('REJECTED',result['status'],result)
                state=self.ledger.snapshot();self.assertFalse(state['works']);self.assertFalse(state['candidates'])
                self.assertEqual({'PENDING'},{t['phase'] for t in state['tasks']})

    def test_open_admission_cannot_accept_migration(self):
        with self.ledger._connection() as db:db.execute('UPDATE authority SET admission_open=1');db.commit()
        self.assertEqual('FROZEN_STAGED_IMPORT_REQUIRED',accept(self.ledger,self.import_id,self.run,self.review,command_id='bad-open')['code'])

    def test_existing_real_systemd_service_is_adopted_without_dispatch_or_pass(self):
        service_id=uuid.uuid4().hex;unit='coordinator-ocr-'+service_id+'.service'
        native=self.root/'ocr';native.mkdir();spec_path=native/'spec.json'
        spec={'run_id':service_id,'unit':unit,'project':str(self.root),'repo':str(self.repo),
              'task':'D03','attempt':'attempt-1-r1','base':self.base,'head':self.base}
        spec_path.write_text(json.dumps(spec))
        manifest={**self.manifest,'sha256':'c'*64,'sources':[{'path':str(spec_path),'sha256':hashlib.sha256(spec_path.read_bytes()).hexdigest()}]}
        import_id=self.ledger.stage_import(manifest,[],command_id='service-import')['import_id']
        review={**self.review,'inventory_manifest_sha256':manifest['sha256']}
        self.assertEqual('COMMITTED',accept(self.ledger,import_id,self.run,review,command_id='service-accept')['status'])
        subprocess.run(['systemd-run','--user','--quiet','--unit='+unit,'--property=RemainAfterExit=yes','--property=Type=oneshot','/usr/bin/true'],check=True,capture_output=True,timeout=20)
        self.addCleanup(lambda:subprocess.run(['systemctl','--user','stop',unit],capture_output=True))
        before=subprocess.check_output(['systemctl','--user','show',unit,'-p','InvocationID'],text=True)
        result=adopt_ocr(self.ledger,import_id,'D03-A',native,legacy_attempt='attempt-1-r1',command_id='adopt')
        self.assertEqual('COMMITTED',result['status'],result);self.assertTrue(result['no_dispatch'])
        after=subprocess.check_output(['systemctl','--user','show',unit,'-p','InvocationID'],text=True)
        self.assertEqual(before,after)
        snapshot=self.ledger.snapshot();self.assertEqual('SUBMITTED',snapshot['external_actions'][0]['state'])
        from workflow_reports import build_facts
        now=self.ledger.clock()
        facts=build_facts(self.ledger,'COORDINATOR',now-3600,now+1,
                          usage_index=self.root/'usage.db',project=self.root,run_id=self.run)
        self.assertEqual([result['action_id']],[a['action_id'] for a in facts['external_actions']])
        personal=build_facts(self.ledger,'A',now-3600,now+1,
                             usage_index=self.root/'usage.db',project=self.root,run_id=self.run)
        self.assertEqual([],personal['external_actions'])
        other=self.ledger.create_run('other',self.root,self.repo,['OTHER'],command_id='other')['run_id']
        unrelated=build_facts(self.ledger,'COORDINATOR',now-3600,now+1,
                              usage_index=self.root/'usage.db',project=self.root,run_id=other)
        self.assertEqual([],unrelated['external_actions'])
        self.assertFalse(snapshot['verifications']);self.assertFalse(snapshot['executions'])
        self.assertEqual('DUPLICATE',adopt_ocr(self.ledger,import_id,'D03-A',native,legacy_attempt='attempt-1-r1',command_id='adopt')['status'])
        with self.assertRaisesRegex(ValueError,'EXACT_EXISTING_OCR_SCOPE_REQUIRED'):
            adopt_ocr(self.ledger,import_id,'D03-A',native,legacy_attempt='other-attempt',command_id='wrong')
        self.ledger.set_mode('ACTIVE',command_id='observe-active')
        import role_observer
        observed=role_observer._workflow_observation(self.ledger.path,self.root/'pool.json',self.root,self.ledger.clock())
        self.assertFalse(any(row['type']=='observer_service_identity_unverified' for row in observed['faults']),observed['faults'])
        self.assertEqual('EXISTING_SERVICE_ADOPTED_NO_REPLAY',observed['services'][0]['identity_basis'])
        sidecar=next(self.ledger.path.parent.glob('services/*/invocation.json'))
        value=json.loads(sidecar.read_text());value['legacy_adoption']['original_attempt']='forged'
        sidecar.write_text(json.dumps(value))
        observed=role_observer._workflow_observation(self.ledger.path,self.root/'pool.json',self.root,self.ledger.clock())
        self.assertTrue(any(row['type']=='observer_service_identity_unverified' for row in observed['faults']))


if __name__=='__main__':unittest.main()
