"""Real leases, Git and claims across compatible packages; no scheduler/send."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import role_runtime
from runtime_common import read_json,save_json
from requirement_scope import checkpoint_for_scope,park_checkpoint,restore_scope_checkpoint
from role_records import record_role,role_guard
from task_claim import claim_task,release_slot
from tests.test_task_claim import DAY


class ScopeCheckpointTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.container=Path(temporary.name)
        self.root=self.container/'legacy';self.root.mkdir()
        self.packages={}
        for task in ('TASK-01','TASK-02'):
            package=self.container/task;package.mkdir();self.packages[task]=package
            (package/'TODO.md').write_text('本包任务编号定位：`'+task+'`。\n')
        save_json(self.root/'ROLE-BINDINGS.json',{'schema_version':1,'project_root':str(self.root),
            'control':'ACTIVE','roles':{'A':{'session_file':'sessions/A.md','chat_id':'test-group'},
                                    'COORDINATOR':{'session_file':'sessions/COORDINATOR.md'}}})
        self.tokens={}
        for role in ('A','COORDINATOR'):
            lease=role_runtime.execute(argparse.Namespace(action='claim',project=str(self.root),role=role,
                owner='scope-test-'+role,pid=None,ttl=300))
            token=self.root/(role+'-token.json');save_json(token,{'token':lease['token']});self.tokens[role]=token
        self.wt=self.root/'worktree';self.wt.mkdir()
        self.git('init','-b','task-branch')
        self.git('-c','user.name=Test','-c','user.email=test@example.invalid','commit','--allow-empty','-m','base')
        base=self.git('rev-parse','HEAD');self.specs={}
        for task in ('TASK-01','TASK-02'):
            dispatch=self.root/'inbox/1号'/task/'attempt-1-dispatch.md';dispatch.parent.mkdir(parents=True)
            dispatch.write_text('# 派单\n- status: DISPATCHED\n- task: '+task+'\n- attempt: attempt-1\n- role: A\n- base: '+base+'\n')
            self.specs[task]={'task':task,'attempt':'attempt-1','dispatch':str(dispatch.relative_to(self.root)),
                              'base':base,'branch':'task-branch','worktree':str(self.wt)}
        self.path=self.root/'inbox/control/runtime/checkpoints/A.json'
        self.select('TASK-01')

    def git(self,*args):
        return subprocess.check_output(['git','-C',str(self.wt),*args],text=True,stderr=subprocess.DEVNULL).strip()

    def select(self,task):
        binding=read_json(self.root/'ROLE-BINDINGS.json')
        binding['requirement_authorization']={'scope':'existing_requirement_business',
            'requirement_id':task,'requirement_root':str(self.packages[task]),'runtime_root':str(self.root),
            'request_id':'accepted-'+task}
        save_json(self.root/'ROLE-BINDINGS.json',binding)

    def claim(self,task):
        return claim_task(self.root,'A',self.specs[task],self.tokens['A'],at=DAY)

    def progress(self,task,status='CODING'):
        evidence=self.root/'receipts'/task/'attempt-1.md';evidence.parent.mkdir(parents=True,exist_ok=True)
        evidence.write_text(task+' attempt-1 已保存实际进展\n')
        record_role(self.root,'A',{'event_id':'A:'+task+':'+status,'kind':'business','task':task,
            'attempt':'attempt-1','status':status,'summary':'真实进展','evidence':str(evidence.relative_to(self.root)),
            'next':'保留下一步','pending_actions':['保留未完动作'],'open_gates':['保留未解除门禁'],
            'references':['receipts/'+task+'/attempt-1.md'],'occurrence':{'kind':'now'}},self.tokens['A'])
        return evidence

    def park(self,value):
        with role_guard(self.root,'A',self.tokens['A']):
            return park_checkpoint(self.root,'A',value)

    def test_real_claim_switch_back_restores_progress_and_release_without_reclaim_or_notice(self):
        one=self.claim('TASK-01')
        evidence=self.progress('TASK-01','REVIEW_PENDING')
        release_slot(self.root,'A',{'task':'TASK-01','attempt':'attempt-1',
            'evidence':str(evidence.relative_to(self.root))},self.tokens['COORDINATOR'],at=DAY)
        final_one=read_json(self.path)
        ack_before=Path(one['ack']).read_bytes()
        self.select('TASK-02')
        two=self.claim('TASK-02');self.assertTrue(two['claimed'])
        self.progress('TASK-02');final_two=read_json(self.path)
        self.select('TASK-01')
        restored,source=checkpoint_for_scope(self.root,'A')
        self.assertEqual(restored,final_one)
        self.assertIn('/scope-checkpoints/A/snapshots/',source)
        notices={str(p):p.read_bytes() for p in (self.root/'inbox/control/runtime/claim-outbox').rglob('*.json')}
        events={str(p):p.read_bytes() for p in (self.root/'events').rglob('*.json')}
        self.assertFalse(self.claim('TASK-01')['claimed'])
        self.assertEqual(read_json(self.path),final_one)
        self.assertEqual(Path(one['ack']).read_bytes(),ack_before)
        self.assertTrue(read_json(self.path)['slot_released'])
        self.assertEqual(read_json(self.path)['open_gates'],['保留未解除门禁'])
        self.select('TASK-02')
        self.assertFalse(self.claim('TASK-02')['claimed'])
        self.assertEqual(read_json(self.path),final_two)
        self.assertEqual(read_json(self.path)['status'],'CODING')
        self.assertEqual(notices,{str(p):p.read_bytes() for p in (self.root/'inbox/control/runtime/claim-outbox').rglob('*.json')})
        self.assertEqual(events,{str(p):p.read_bytes() for p in (self.root/'events').rglob('*.json')})

    def test_old_unreleased_scope_does_not_block_new_claim_but_selected_unreleased_work_does(self):
        self.claim('TASK-01');old=read_json(self.path)
        self.select('TASK-02');self.assertTrue(self.claim('TASK-02')['claimed'])
        self.assertEqual(restore_scope_checkpoint(self.root,'A','TASK-02','attempt-1')[0]['status'],'ACKED_RUNNING')
        self.select('TASK-01')
        restored,_=checkpoint_for_scope(self.root,'A');self.assertEqual(restored,old)
        dispatch=self.root/self.specs['TASK-01']['dispatch']
        second=dispatch.with_name('attempt-2-dispatch.md');second.write_text(dispatch.read_text().replace('attempt-1','attempt-2'))
        spec={**self.specs['TASK-01'],'attempt':'attempt-2','dispatch':str(second.relative_to(self.root))}
        with self.assertRaisesRegex(ValueError,'未释放'):
            claim_task(self.root,'A',spec,self.tokens['A'],at=DAY)
        self.assertFalse(second.with_name('attempt-2-ack.md').exists())

    def test_parking_preserves_complete_original_bytes_and_is_idempotent(self):
        value={'role':'A','task':'TASK-01','attempt':'1','slot_released':True,
               'slot_release':{'arbitrary':'完整原字段'},'next':'下一步','open_gates':['门禁'],'extra':{'nested':[1,2]}}
        raw=json.dumps(value,ensure_ascii=False,separators=(',',':')).encode()
        self.path.parent.mkdir(parents=True,exist_ok=True);self.path.write_bytes(raw)
        first=self.park(value);self.park(value)
        self.assertEqual(Path(first['source']).read_bytes(),raw)
        self.assertEqual(first['sha256'],hashlib.sha256(raw).hexdigest())
        self.assertEqual(len(read_json(Path(first['source']).parent.parent/'index.json')['snapshots']),1)
        self.assertEqual(self.path.read_bytes(),raw)

    def test_interruption_before_index_does_not_replace_active_and_can_retry(self):
        self.claim('TASK-01');original=self.path.read_bytes();value=read_json(self.path)
        with patch('requirement_scope.save_json',side_effect=OSError('exit after immutable snapshot')),self.assertRaises(OSError):
            self.park(value)
        self.assertEqual(self.path.read_bytes(),original)
        result=self.park(value)
        self.assertEqual(Path(result['source']).read_bytes(),original)
        self.assertEqual(len(list(Path(result['source']).parent.glob('*.json'))),1)

    def test_corrupt_snapshot_fails_closed_without_rewriting_active_or_index(self):
        self.claim('TASK-01');parked=self.park(read_json(self.path))
        Path(parked['source']).write_text('{}')
        self.select('TASK-02')
        save_json(self.path,{'role':'A','task':'TASK-02','attempt':'1'})
        self.select('TASK-01')
        index=Path(parked['source']).parent.parent/'index.json';before=(self.path.read_bytes(),index.read_bytes())
        with self.assertRaisesRegex(ValueError,'哈希'):
            checkpoint_for_scope(self.root,'A')
        self.assertEqual((self.path.read_bytes(),index.read_bytes()),before)

    def test_exact_task_attempt_restore_uses_latest_progress_not_initial_claim(self):
        self.claim('TASK-01');initial=read_json(self.path)
        final={**copy.deepcopy(initial),'status':'REVIEW_PENDING','next':'最新交付下一步',
               'recorded_at':'2099-01-01T00:00:00+08:00','slot_release':{'retain':'完整'}}
        self.park(final)
        restored,_=restore_scope_checkpoint(self.root,'A','TASK-01','attempt-1')
        self.assertEqual(restored,final)
        self.assertFalse(self.claim('TASK-01')['claimed'])
        self.assertEqual(read_json(self.path),final)
        self.assertEqual(restore_scope_checkpoint(self.root,'A','TASK-01','other-attempt')[0],{})


if __name__=='__main__':unittest.main()
