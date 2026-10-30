"""Task consent survives real rework; fixed permissions and gates do not."""
from unittest import mock
import json

from tests.test_business_ledger import LedgerFixture
from workflow_merge_policy import authorization
from workflow_task_merge_policy import reconcile
from workflow_progression import merge_permission


class TaskMergePolicyTests(LedgerFixture):
    def grant(self, text=None, identifier='task-grant'):
        branch=self.git('symbolic-ref','--short','HEAD')
        source={'authenticated':True,'current_full':True,'message_id':'native-grant','user_id':'u',
                'primary_text':text or f'全部权限：授权本需求 run-one T1 合入 {branch}，有效期 72 小时。'}
        spec={'authorization_id':identifier,'run_id':'run-one','task_id':'T1','attempt':1,
              'repo':str(self.repo),'branch':branch,'base':self.base,'ac_version':'ac-sha256-frozen-v1',
              'expires_at':self.now+3600}
        return self.assertCommitted(self.ledger.grant_merge_authorization(spec,source,command_id='grant-'+identifier))

    def refreshed_candidate(self):
        dp,dev=self.bind('A');cid,_=self.candidate(dev);self.close(dp,dev)
        cp,coord=self.bind('COORDINATOR')
        self.assertCommitted(self.ledger.decide(coord,cid,'REWORK',evidence={'reason':'same accepted task retry'},command_id='rework'))
        dp,dev=self.bind('A');next_cid,_=self.candidate(dev,2);self.close(dp,dev)
        return coord,next_cid

    def test_task_permission_survives_new_attempt_but_requires_fresh_checks_and_approval(self):
        grant=self.grant();coord,cid=self.refreshed_candidate()
        candidate=next(c for c in self.ledger.snapshot()['candidates'] if c['candidate_id']==cid)
        hint=merge_permission(self.ledger.snapshot(),candidate,self.now)
        self.assertTrue(hint['available']);self.assertEqual(grant['task_policy_id'],hint['parent_policy_id'])
        permit=self.assertCommitted(authorization(self.ledger,coord,cid))
        readiness=self.ledger.merge_readiness(coord,cid,permit['authorization_id'])
        self.assertEqual('ACCEPTANCE_GATES_NOT_CLEAR',readiness['code'])
        self.assertEqual(permit['authorization_id'],authorization(self.ledger,coord,cid)['authorization_id'])
        derived=next(g for g in self.ledger.snapshot()['authorizations'] if g['authorization_id']==permit['authorization_id'])
        self.assertEqual(2,derived['spec']['attempt']);self.assertEqual(self.now+3600,derived['spec']['expires_at'])
        self.assertEqual([],self.ledger.snapshot()['decisions'][-1]['evidence'].get('inherited_checks',[]))

    def test_legacy_grant_reconstruction_uses_original_source_and_plan_not_current_scope(self):
        grant=self.grant();policy=grant['task_policy_id']
        with self.ledger._connection() as db:
            db.execute('DELETE FROM authorizations WHERE authorization_id=?',(policy,));db.commit()
        results=reconcile(self.ledger);self.assertEqual(1,len(results));self.assertCommitted(results[0])
        self.assertEqual([],reconcile(self.ledger))
        coord,cid=self.refreshed_candidate()
        self.assertCommitted(authorization(self.ledger,coord,cid))

    def test_explicit_attempt_candidate_or_base_authorization_is_never_promoted(self):
        branch=self.git('symbolic-ref','--short','HEAD')
        for i,restriction in enumerate(('仅本次','attempt 1','当前候选','base='+self.base)):
            grant=self.grant(f'全部权限：授权本需求 run-one T1 合入 {branch}，{restriction}。',identifier='exact'+str(i))
            self.assertNotIn('task_policy_id',grant)
        self.assertEqual([],reconcile(self.ledger))
        coord,cid=self.refreshed_candidate()
        self.assertEqual('ACCEPTED_REQUIREMENT_MERGE_POLICY_REQUIRED',authorization(self.ledger,coord,cid)['code'])

    def test_revocation_expiry_and_contract_change_block_new_and_already_derived_permits(self):
        grant=self.grant();coord,cid=self.refreshed_candidate()
        permit=self.assertCommitted(authorization(self.ledger,coord,cid))
        candidate=next(c for c in self.ledger.snapshot()['candidates'] if c['candidate_id']==cid)
        with self.ledger._connection() as db:
            original=db.execute("SELECT scope FROM tasks WHERE run_id='run-one' AND task_id='T1'").fetchone()[0]
            db.execute("UPDATE tasks SET scope=? WHERE run_id='run-one' AND task_id='T1'",(json.dumps(['app.py','other.py']),));db.commit()
        self.assertFalse(merge_permission(self.ledger.snapshot(),candidate,self.now)['available'])
        self.assertEqual('TASK_MERGE_POLICY_CONTRACT_CHANGED',self.ledger.merge_readiness(coord,cid,permit['authorization_id'])['code'])
        with self.ledger._connection() as db:
            db.execute("UPDATE tasks SET scope=? WHERE run_id='run-one' AND task_id='T1'",(original,));db.commit()
        self.now+=3601
        self.assertFalse(merge_permission(self.ledger.snapshot(),candidate,self.now)['available'])
        self.assertEqual('AUTHORIZATION_EXPIRED',self.ledger.merge_readiness(coord,cid,permit['authorization_id'])['code'])
        self.now-=3601
        source={'authenticated':True,'current_full':True,'message_id':'revoke','user_id':'u','primary_text':'全部权限：撤销授权 task-grant'}
        self.assertCommitted(self.ledger.revoke_authorization('task-grant',source,command_id='revoke'))
        self.assertFalse(merge_permission(self.ledger.snapshot(),candidate,self.now)['available'])
        self.assertEqual('PARENT_AUTHORIZATION_REVOKED_OR_EXPIRED',self.ledger.merge_readiness(coord,cid,permit['authorization_id'])['code'])

    def test_changed_scope_before_repair_does_not_become_the_historical_boundary(self):
        grant=self.grant()
        with self.ledger._connection() as db:
            db.execute('DELETE FROM authorizations WHERE authorization_id=?',(grant['task_policy_id'],))
            db.execute("UPDATE tasks SET scope=? WHERE run_id='run-one' AND task_id='T1'",(json.dumps(['app.py','new.py']),));db.commit()
        self.assertCommitted(reconcile(self.ledger)[0])
        coord,cid=self.refreshed_candidate()
        candidate=next(c for c in self.ledger.snapshot()['candidates'] if c['candidate_id']==cid)
        self.assertFalse(merge_permission(self.ledger.snapshot(),candidate,self.now)['available'])

    def test_completion_cannot_skip_merge_because_attempt_changed(self):
        self.grant();coord,cid=self.refreshed_candidate()
        proof=self.root/'delivery.txt';proof.write_text('not a merge receipt')
        with mock.patch.object(self.ledger,'_acceptance',return_value={'ready':True}):
            self.assertCommitted(self.ledger.decide(coord,cid,'APPROVE',command_id='approve'))
            result=self.ledger.decide(coord,cid,'COMPLETE',evidence={'delivery':{'path':str(proof)}},command_id='complete')
        self.assertEqual('AUTHORIZED_MERGE_AND_POSTMERGE_EVIDENCE_REQUIRED',result['code'])
