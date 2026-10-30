"""Real Git integration and independent checks through the existing runner."""
import json
from pathlib import Path
import subprocess
import sys
import unittest

try:
    from test_business_ledger import LedgerFixture
except ModuleNotFoundError:
    from tests.test_business_ledger import LedgerFixture
from delivery_evidence import execute_candidate_tests
from workflow_merge import merge_candidate

CHECK = '''import app,sys,unittest,xml.etree.ElementTree as E
class Check(unittest.TestCase):
 def test_actual_candidate(self): self.assertEqual(app.value(),int(sys.argv[2]))
r=unittest.TestResult(); unittest.defaultTestLoader.loadTestsFromTestCase(Check).run(r)
s=E.Element('testsuite',tests=str(r.testsRun),failures=str(len(r.failures)),errors=str(len(r.errors)),skipped='0')
c=E.SubElement(s,'testcase',name='test_actual_candidate')
for _,m in r.failures: E.SubElement(c,'failure').text=m
for _,m in r.errors: E.SubElement(c,'error').text=m
E.ElementTree(s).write(sys.argv[1]); sys.exit(0 if r.wasSuccessful() else 1)
'''


class WorkflowMergeTests(LedgerFixture):
    def setUp(self):
        super().setUp()
        self.check=self.root/'check.py'; self.check.write_text(CHECK)
        dev_process,dev=self.bind('A')
        clone=self.path.parent/'workspaces'/dev['work_id']
        clone.parent.mkdir(parents=True)
        subprocess.run(['git','clone','-q','--no-hardlinks',str(self.repo),str(clone)],check=True)
        self.git('config','user.name','Isolated Merge Test',cwd=clone)
        self.git('config','user.email','merge@example.invalid',cwd=clone)
        (clone/'app.py').write_text('def value():\n    return 1\n')
        self.git('add','app.py',cwd=clone); self.git('commit','-qm','candidate',cwd=clone)
        receipt=self.root/'dev.txt'; receipt.write_text('actual development checks')
        self.head=self.git('rev-parse','HEAD',cwd=clone)
        self.candidate_id=self.assertCommitted(self.ledger.deliver(dev,{'repo':str(clone),
            'base':self.base,'head':self.head,'ac_version':'ac-sha256-frozen-v1',
            'evidence':[{'path':str(receipt)}]},'冻结独立clone候选',command_id='deliver-clone'))['candidate_id']
        self.close(dev_process,dev)
        tester_process,tester=self.bind('TESTER')
        checked=execute_candidate_tests(self.ledger,tester,self.candidate_id,
            [sys.executable,'-c',CHECK,'{junit_xml}','1'],command_id='actual-test',report_format='junit')
        self.assertCommitted(checked)
        self.close(tester_process,tester)
        self.coord_process,self.coord=self.bind('COORDINATOR')
        self.branch=self.git('symbolic-ref','--short','HEAD')
        self.provenance={'authenticated':True,'current_full':True,'message_id':'merge-grant',
            'user_id':'u','primary_text':'授权当前固定候选快进合入并执行固定检查'}

    def grant(self, *, postmerge_expected=1, identifier='merge-one'):
        return self.assertCommitted(self.ledger.grant_merge_authorization({
            'authorization_id':identifier,'run_id':'run-one','task_id':'T1','attempt':1,
            'repo':str(self.repo),'branch':self.branch,'base':self.base,'ac_version':'ac-sha256-frozen-v1',
            'expires_at':self.now+3600,'postmerge_argv':[sys.executable,'-c',CHECK,'{junit_xml}',str(postmerge_expected)],
            'junit_format':'junit','timeout_seconds':20},self.provenance,command_id='grant-'+identifier))['authorization_id']

    def approve(self):
        self.assertCommitted(self.ledger.decide(self.coord,self.candidate_id,'APPROVE',command_id='approve'))

    def test_exact_authorized_fast_forward_postcheck_replay_and_explicit_completion(self):
        authorization=self.grant(); self.approve()
        result=self.assertCommitted(merge_candidate(self.ledger,self.coord,self.candidate_id,authorization,command_id='merge'))
        self.assertEqual(self.head,self.git('rev-parse','HEAD'))
        self.assertFalse(result['business_complete'])
        record=json.loads(Path(result['evidence']).read_text())
        self.assertEqual(1,record['postmerge']['executed'])
        self.assertEqual('PASS',record['postmerge']['mechanical_result'])
        replay=merge_candidate(self.ledger,self.coord,self.candidate_id,authorization,command_id='merge')
        self.assertEqual('DUPLICATE',replay['status'])
        self.assertEqual(1,len(self.ledger.snapshot()['merges']))
        self.assertEqual('REVIEW',self.ledger.snapshot()['tasks'][0]['phase'])
        receipt=self.root/'delivery.txt'; receipt.write_text('真实快进合入与独立及合入后检查证据已核验')
        (self.repo/'app.py').write_text('changed after merge\n')
        self.assertEqual('FINAL_MERGED_WORKSPACE_CHANGED',self.ledger.decide(self.coord,self.candidate_id,'COMPLETE',
            evidence={'delivery':{'path':str(receipt)},'merged_head':self.head},command_id='changed-complete')['code'])
        self.git('restore','app.py')
        self.assertCommitted(self.ledger.decide(self.coord,self.candidate_id,'COMPLETE',
            evidence={'delivery':{'path':str(receipt)},'merged_head':self.head},command_id='complete'))
        request=self.assertCommitted(self.ledger.control_request('cancel',{'run_id':'run-one','task_id':'T2'},
            self.provenance,command_id='cancel-terminal-task'))
        self.assertCommitted(self.ledger.apply_control(request['request_id'],command_id='apply-cancel-terminal-task'))
        page=self.assertCommitted(self.ledger.work_inputs(self.coord,command_id='terminal-inputs'))
        self.assertCommitted(self.ledger.checkpoint(self.coord,{'last_action':'全部任务已终态'},
            processed_through=page['through'],command_id='terminal-checkpoint'))
        self.assertCommitted(self.ledger.release_work(self.coord,reason='当前run交付完成',command_id='terminal-release'))
        self.close(self.coord_process,self.coord)
        self.assertFalse(self.ledger.admission_hint('COORDINATOR')['can_start_model'])
        self.assertCommitted(self.ledger.control_request('continue',{'run_id':'run-one'},
            self.provenance,command_id='new-terminal-request'))
        self.assertTrue(self.ledger.admission_hint('COORDINATOR')['can_start_model'])

    def test_missing_grant_or_approval_and_changed_destination_refuse_before_git(self):
        denied=merge_candidate(self.ledger,self.coord,self.candidate_id,'missing',command_id='none')
        self.assertFalse(denied['ready'])
        authorization=self.grant()
        self.assertEqual('COORDINATOR_APPROVAL_REQUIRED',merge_candidate(self.ledger,self.coord,
            self.candidate_id,authorization,command_id='unapproved')['code'])
        self.approve()
        (self.repo/'app.py').write_text('dirty\n')
        self.assertEqual('MERGE_BASE_CHANGED_OR_DIRTY',merge_candidate(self.ledger,self.coord,
            self.candidate_id,authorization,command_id='dirty')['code'])
        self.assertEqual(self.base,self.git('rev-parse','HEAD'))

    def test_actual_postmerge_failure_keeps_completion_closed_and_never_retries(self):
        authorization=self.grant(postmerge_expected=2); self.approve()
        failed=merge_candidate(self.ledger,self.coord,self.candidate_id,authorization,command_id='failed-merge')
        self.assertEqual('UNVERIFIED',failed['status'])
        self.assertEqual(self.head,self.git('rev-parse','HEAD'))
        self.assertEqual([],self.ledger.snapshot()['merges'])
        self.assertEqual('DUPLICATE',merge_candidate(self.ledger,self.coord,self.candidate_id,
            authorization,command_id='failed-merge')['status'])
        receipt=self.root/'delivery.txt'; receipt.write_text('不得忽略真实合入后断言失败')
        denied=self.ledger.decide(self.coord,self.candidate_id,'COMPLETE',
            evidence={'delivery':{'path':str(receipt)}},command_id='bad-complete')
        self.assertEqual('AUTHORIZED_MERGE_AND_POSTMERGE_EVIDENCE_REQUIRED',denied['code'])

    def test_authorized_missing_branch_is_prepared_without_shell_or_moving_original_branch(self):
        original=self.branch
        self.branch='feat/authorized-target'
        authorization=self.grant();self.approve()
        self.assertCommitted(merge_candidate(self.ledger,self.coord,self.candidate_id,authorization,command_id='prepare-merge'))
        self.assertEqual(self.branch,self.git('branch','--show-current'))
        self.assertEqual(self.base,self.git('rev-parse',original))
        self.assertEqual(self.head,self.git('rev-parse',self.branch))

    def test_dirty_shared_workspace_uses_managed_branch_and_preserves_external_files(self):
        original=self.branch;self.branch='feat/authorized-target'
        authorization=self.grant();self.approve()
        (self.repo/'external.md').write_text('other session unfinished content')
        result=self.assertCommitted(merge_candidate(self.ledger,self.coord,self.candidate_id,authorization,command_id='dirty-prepare'))
        self.assertEqual(original,self.git('branch','--show-current'))
        self.assertEqual('other session unfinished content',(self.repo/'external.md').read_text())
        self.assertEqual(self.head,self.git('rev-parse',self.branch))
        receipt=json.loads(Path(result['evidence']).read_text())
        self.assertIn('integration_worktree',receipt['frozen'])

    def test_dirty_feature_can_release_to_identical_main_then_complete_and_refresh_sibling(self):
        from workflow_integration_recovery import refresh_base
        original=self.branch
        if original!='main':
            self.git('branch','main',self.base)
        self.git('checkout','-qb','feat/authorized-target')
        self.branch='feat/authorized-target'
        (self.repo/'app.py').write_text('user edit must survive\n')
        (self.repo/'handoff.md').write_text('untracked valuable delivery\n')
        index=(self.repo/'.git/index').read_bytes()
        files={p:p.read_bytes() for p in (self.repo/'app.py',self.repo/'handoff.md')}
        authorization=self.grant();self.approve()
        result=self.assertCommitted(merge_candidate(self.ledger,self.coord,self.candidate_id,authorization,command_id='isolated-merge'))
        self.assertEqual('main',self.git('branch','--show-current'))
        self.assertEqual(self.base,self.git('rev-parse','main'))
        self.assertEqual(self.head,self.git('rev-parse',self.branch))
        self.assertEqual(index,(self.repo/'.git/index').read_bytes())
        for path,content in files.items():self.assertEqual(content,path.read_bytes())
        receipt=self.root/'delivery.txt';receipt.write_text('isolated authorized merge verified')
        self.assertCommitted(self.ledger.decide(self.coord,self.candidate_id,'COMPLETE',
            evidence={'delivery':{'path':str(receipt)},'merged_head':self.head},command_id='isolated-complete'))
        # A sibling candidate on the old base must resume from the integrated
        # feature ref, not main's unrelated HEAD or dirty shared workspace.
        with self.ledger._connection() as db:
            db.execute("UPDATE tasks SET phase='REVIEW',attempt=1 WHERE task_id='T2'")
            db.execute("INSERT INTO candidates(candidate_id,run_id,task_id,attempt,role,repo,base,head,ac_version,evidence,summary,state,created_at) "
                "SELECT 'sibling',run_id,'T2',1,role,repo,base,head,ac_version,evidence,summary,'CURRENT',created_at FROM candidates WHERE candidate_id=?",(self.candidate_id,))
            db.commit()
        refreshed=self.assertCommitted(refresh_base(self.ledger,self.coord,'sibling','same branch audited base',command_id='refresh-isolated'))
        self.assertEqual(self.head,refreshed['base'])
        self.assertFalse(refreshed['prior_checks_reused'])

    def test_dirty_feature_does_not_release_when_main_is_different(self):
        if self.branch!='main':self.git('branch','main',self.base)
        self.git('checkout','-qb','feat/authorized-target')
        self.branch='feat/authorized-target'
        (self.repo/'new.txt').write_text('committed only on feature')
        self.git('add','new.txt');self.git('commit','-qm','different current head')
        (self.repo/'external.md').write_text('preserve')
        authorization=self.grant();self.approve()
        result=merge_candidate(self.ledger,self.coord,self.candidate_id,authorization,command_id='different-main')
        self.assertEqual('SHARED_BRANCH_RELEASE_REQUIRES_IDENTICAL_MAIN',result['code'])
        self.assertEqual(self.branch,self.git('branch','--show-current'))

    def test_changed_managed_workspace_blocks_completion(self):
        self.branch='feat/authorized-target';authorization=self.grant();self.approve()
        (self.repo/'external.md').write_text('preserve')
        result=self.assertCommitted(merge_candidate(self.ledger,self.coord,self.candidate_id,authorization,command_id='managed'))
        evidence=json.loads(Path(result['evidence']).read_text())
        target=Path(evidence['frozen']['integration_worktree'])
        (target/'app.py').write_text('changed after integration')
        receipt=self.root/'delivery.txt';receipt.write_text('cannot complete')
        denied=self.ledger.decide(self.coord,self.candidate_id,'COMPLETE',
            evidence={'delivery':{'path':str(receipt)},'merged_head':self.head},command_id='dirty-managed-complete')
        self.assertEqual('FINAL_MERGED_WORKSPACE_CHANGED',denied['code'])

    def test_existing_authorized_branch_at_different_head_is_not_reset(self):
        self.branch='feat/authorized-target'
        self.git('-c','protocol.file.allow=always','fetch','--no-tags',
            self.ledger.snapshot()['candidates'][0]['repo'],self.head)
        self.git('branch',self.branch,self.head)
        authorization=self.grant();self.approve()
        result=merge_candidate(self.ledger,self.coord,self.candidate_id,authorization,command_id='wrong-target')
        self.assertEqual('MERGE_TARGET_BRANCH_CHANGED',result['code'])
        self.assertEqual(self.head,self.git('rev-parse',self.branch))


if __name__=='__main__': unittest.main()
