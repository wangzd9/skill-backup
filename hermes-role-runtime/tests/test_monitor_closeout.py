"""Real isolated queue/PID regressions for the rollout blockers; no network."""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import monitor_notifications as notifications
import monitor_queue as queue
import execution_retention
import role_observer as observer
import role_runtime as runtime
from test_lease_monitor import PoolFixture

DAY=datetime(2026,10,21,12,tzinfo=ZoneInfo('Asia/Shanghai'))
NOW=DAY.timestamp()


class ClassificationTests(PoolFixture):
    def test_saved_legacy_active_claim_is_reclassified_without_recovery_or_scope_deletion(self):
        child=self.worker();self.native(child);child.terminate();child.wait()
        with sqlite3.connect(self.home/'cron/executions.db') as db:db.execute('DELETE FROM executions')
        old={'identity':'legacy-active','type':'observer_active_execution_without_lease','role':'A',
            'source':str(runtime.lease_path(self.root,'A')),'episode':'legacy-episode','severity':1,'executions':['a'*32]}
        runtime.save(self.state/'state.json',{'active':{'legacy-active':old}})
        result=self.observe()
        self.assertFalse(any(v['type']=='observer_active_execution_without_lease' for v in result['anomalies']))
        event=next(e for e in result['transitions'] if e['identity']=='legacy-active')
        self.assertEqual(event['transition'],'reclassified_unverified')
        self.assertTrue((self.home/'runtime/role-pool-executions'/('a'*32+'.json')).exists())
    def test_missing_native_row_is_unknown_and_cannot_assert_active(self):
        child=self.worker(); self.native(child)
        child.terminate(); child.wait()
        with sqlite3.connect(self.home/'cron/executions.db') as db:
            db.execute('DELETE FROM executions')
        result=self.observe()
        self.assertTrue(any(f['type']=='observer_frozen_execution_unverified' for f in result['faults']))
        fault=next(f for f in result['faults'] if f['type']=='observer_frozen_execution_unverified')
        self.assertEqual(fault['observation_state'],'UNKNOWN')
        self.assertEqual(fault['native_row_state'],'MISSING')
        self.assertIn('NATIVE_EXECUTION_ROW_MISSING',fault['reason_codes'])
        self.assertFalse(any(f['type']=='observer_active_execution_without_lease' for f in result['faults']))
        self.assertEqual(result['roles']['A'][0]['execution_observation']['unknown'],['a'*32])
        self.assertTrue((self.home/'runtime/role-pool-executions'/('a'*32+'.json')).exists())

    def test_unknown_reasons_distinguish_exit_from_unverifiable_owner_without_new_incident(self):
        child=self.worker(); self.native(child);child.terminate();child.wait()
        first=self.observe()
        before=next(f for f in first['anomalies'] if f['type']=='observer_frozen_execution_unverified')
        self.assertEqual(before['observation_state'],'UNKNOWN')
        self.assertEqual(before['owner_process_state'],'exited_or_reused')
        self.assertIn('NATIVE_STATUS_NOT_TERMINAL',before['reason_codes'])
        # The pre-upgrade identity did not include the new reason fields.
        self.assertEqual(before['identity'],observer.digest({k:before[k] for k in ('type','source','role','execution_id')}))
        with sqlite3.connect(self.home/'cron/executions.db') as db:
            db.execute("UPDATE executions SET status='completed',process_started_at=NULL")
        after=self.observe()
        fault=next(f for f in after['anomalies'] if f['type']=='observer_frozen_execution_unverified')
        self.assertEqual(fault['owner_process_state'],'unverified')
        self.assertIn('OWNER_PROCESS_UNVERIFIED',fault['reason_codes'])
        self.assertNotIn('NATIVE_STATUS_NOT_TERMINAL',fault['reason_codes'])
        self.assertEqual(fault['episode'],before['episode'])
        self.assertFalse(any(e['type']==fault['type'] for e in after['transitions']))
        self.assertTrue((self.home/'runtime/role-pool-executions'/('a'*32+'.json')).exists())

    def test_terminal_native_with_alive_owner_still_protects_writer(self):
        child=self.worker(); self.native(child)
        with sqlite3.connect(self.home/'cron/executions.db') as db:
            db.execute("UPDATE executions SET status='completed'")
        result=self.observe()
        self.assertTrue(any(f['type']=='observer_active_execution_without_lease' for f in result['faults']))
        child.terminate(); child.wait()
        result=self.observe()
        self.assertFalse(any(f['type']=='observer_active_execution_without_lease' for f in result['faults']))

    def test_membership_changes_keep_episode_and_generation_changes_do_not_merge(self):
        child=self.worker(); self.native(child)
        first=self.observe()
        incident=next(e for e in first['transitions'] if e['type']=='observer_active_execution_without_lease')
        other=self.worker(); self.native(other,execution='b'*32)
        again=self.observe()
        self.assertFalse(any(e['type']=='observer_active_execution_without_lease' for e in again['transitions']))
        current=next(e for e in again['anomalies'] if e['type']=='observer_active_execution_without_lease')
        self.assertEqual(current['episode'],incident['episode'])
        self.assertEqual(current['executions'],['a'*32,'b'*32])
        # A new collector process loads the exact persisted episode.
        code='import sys;from role_observer import observe_pool_leases;import json;print(json.dumps(observe_pool_leases(sys.argv[1],home=sys.argv[2],state_dir=sys.argv[3])["transitions"]))'
        result=subprocess.run([sys.executable,'-B','-c',code,str(self.pool_path),str(self.home),str(self.state)],capture_output=True,text=True,check=True)
        self.assertFalse(any(e['type']=='observer_active_execution_without_lease' for e in json.loads(result.stdout)))
        self.pool['generation']=3; runtime.save(self.pool_path,self.pool); self.bind(self.root,3)
        self.native(self.worker(),generation=3,execution='c'*32)
        final=self.observe()
        values=[a for a in final['anomalies'] if a['type']=='observer_active_execution_without_lease']
        self.assertEqual({v['generation'] for v in values},{2,3})


class QueueTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory(); self.addCleanup(t.cleanup)
        self.base=Path(t.name); self.outbox=self.base/'outbox'; self.outbox.mkdir()

    def add(self,n,role='A',**event):
        e={'event_id':str(n),'role':role,'severity':1,'transition':'first','generated_at':NOW,
           'identity':'incident-'+str(n),'episode':'episode-'+str(n),**event}
        row={'event':e,'generation_receipt':{'state':'GENERATED'},'delivery_receipt':{'state':'PENDING'}}
        runtime.save(self.outbox/(str(n).zfill(5)+'.json'),row)
        return row

    def test_prepare_separates_touched_groups_from_pending_summaries_and_ordinary(self):
        self.add(0,'A',generated_at=NOW-8*3600)
        self.add(1,'B')
        first=notifications.prepare(self.outbox,at=DAY)
        self.assertEqual((first['pending'],first['pending_summary'],first['pending_ordinary']),(2,1,1))
        self.assertEqual(first['pending_by_role'],{'A':{'summary':1,'ordinary':0},'B':{'summary':0,'ordinary':1}})
        self.assertEqual(first['outbox_states'],{'PENDING':2,'SUPERSEDED':1})
        again=notifications.prepare(self.outbox,at=DAY)
        self.assertEqual(again['compacted_summary_groups'],0)
        self.assertEqual(again['pending_summary'],1)
        self.assertEqual(again['prepared_at'],NOW)

    def test_age_handoff_preserves_open_fact_and_recovery_is_only_historical(self):
        source=self.add(0,generated_at=NOW-8*3600)
        self.add(1,generated_at=NOW-8*3600,transition='recovered')
        self.add(2,generated_at=NOW-8*3600)
        state={'observed_at':NOW,'active':{source['event']['identity']:{'episode':source['event']['episode']}}}
        runtime.save(self.base/'state.json',state)
        runtime.save(self.base/'snapshot.json',{'at':NOW})
        # Below the backlog threshold: age alone triggers summary handoff.
        prepared=notifications.prepare(self.outbox,at=DAY,delivery_policy={'min_summary_members':1})
        self.assertEqual(prepared['pending_summary'],1)
        self.assertEqual(prepared['pending_ordinary'],0)
        sent=[]
        notifications.drain(self.outbox,lambda e:sent.append(e) or {'confirmed':True},at=DAY,delivery_policy={'min_summary_members':1})
        self.assertEqual(sent[0]['current_counts'],{'CURRENT_OPEN':1,'HISTORICAL_RECOVERY':1,'NOT_IN_ACTIVE_SCOPE':1})
        self.assertEqual(sent[0]['current_incident_count'],1)
        self.assertEqual(runtime.load(self.base/'state.json'),state)
        self.assertTrue(all(runtime.load(self.outbox/(str(n).zfill(5)+'.json'))['delivery_receipt']['state']=='SUPERSEDED' for n in range(3)))

    def test_realistic_1705_history_handoff_preserves_open_unknown_and_recovery(self):
        active={}
        originals={}
        for n in range(1705):
            role=runtime.ROLES[n%5]
            row=self.add(n,role,generated_at=NOW-8*3600,generation=2,
                transition='recovered' if n<498 else 'first')
            if 498<=n<1187:
                active[row['event']['identity']]={'episode':row['event']['episode']}
            path=self.outbox/(str(n).zfill(5)+'.json'); originals[path.name]=path.read_bytes()
        runtime.save(self.base/'state.json',{'observed_at':NOW,'active':active})
        runtime.save(self.base/'snapshot.json',{'at':NOW})
        result=notifications.prepare(self.outbox,at=DAY)
        self.assertEqual(result['state'],'PREPARED'); self.assertEqual(result['pending'],5)
        members=[]
        for p in self.outbox.glob('*.json'):
            row=runtime.load(p)
            if row['event'].get('summary_key'):
                members.extend(row['summary_members'])
            else:
                self.assertEqual(row['delivery_receipt']['state'],'SUPERSEDED')
                self.assertNotEqual(row['delivery_receipt']['state'],'EXPIRED')
        self.assertEqual(len(members),1705)
        self.assertEqual(sum(m['disposition']=='CURRENT_OPEN' for m in members),689)
        self.assertEqual(sum(m['disposition']=='HISTORICAL_RECOVERY' for m in members),498)
        for member in members:
            sha=member['source_sha256']; raw=(self.outbox/'evidence'/sha[:2]/(sha+'.json')).read_bytes()
            self.assertEqual(raw,originals[member['name']]); self.assertEqual(hashlib.sha256(raw).hexdigest(),sha)
        sent=[]
        notifications.drain(self.outbox,lambda e:sent.append(e) or {'confirmed':True},at=DAY,max_seconds=5)
        self.assertEqual(len(sent),5)
        self.assertTrue(all(e['type']=='observer_history_summary' for e in sent))
        self.assertEqual(sum(e['member_count'] for e in sent),1705)
        self.assertEqual(notifications.prepare(self.outbox,at=DAY)['pending'],0)

    def test_persistent_budget_fairness_unknown_and_restart(self):
        for n in range(10): self.add(n,'A')
        self.add(10,'B')
        policy={'pool_limit':2,'group_limit':1,'group_gap_seconds':0,'backlog_threshold':100}
        sent=[]
        notifications.drain(self.outbox,lambda e:sent.append(e) or {'confirmed':False},at=DAY,delivery_policy=policy)
        self.assertEqual({e['role'] for e in sent},{'A','B'})
        notifications.drain(self.outbox,lambda e:self.fail('budget/UNKNOWN repeated'),at=DAY,delivery_policy=policy)
        # A second OS process shares the same persisted budget.
        code='import sys,json;from monitor_notifications import drain;print(json.dumps(drain(sys.argv[1],lambda e:(_ for _ in ()).throw(AssertionError("must not send")),at=float(sys.argv[2]),delivery_policy=json.loads(sys.argv[3]))))'
        result=subprocess.run([sys.executable,'-B','-c',code,str(self.outbox),str(NOW),json.dumps(policy)],capture_output=True,text=True,check=True)
        self.assertNotIn('"attempted_at"',result.stdout)
        # An uncertain summary consumes its entire immutable membership. Only
        # newly observed input can become a subsequent delivery after refill.
        self.add(11,'A',generated_at=NOW+3601)
        notifications.drain(self.outbox,lambda e:sent.append(e) or {'confirmed':True},at=NOW+3601,delivery_policy=policy)
        self.assertEqual(len(sent),3)

    def test_two_processes_cannot_exceed_pool_window(self):
        for n in range(10):self.add(n,role='A' if n%2 else 'B')
        notifications.prepare(self.outbox,at=DAY)
        log=self.base/'sends'
        code='''import sys,json
from monitor_notifications import drain
def send(e):
 with open(sys.argv[2],'a') as f:f.write(e['event_id']+'\\n')
 return {'confirmed':True}
drain(sys.argv[1],send,at=float(sys.argv[3]),max_seconds=5,delivery_policy={'pool_limit':2,'group_limit':2,'group_gap_seconds':0})
'''
        children=[subprocess.Popen([sys.executable,'-B','-c',code,str(self.outbox),str(log),str(NOW)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(2)]
        for child in children:
            _,err=child.communicate(timeout=10); self.assertEqual(child.returncode,0,err)
        self.assertEqual(len(log.read_text().splitlines()),2)

    def test_index_rebuild_retains_unknown_budget_and_no_history_enumeration_steady_state(self):
        self.add(0)
        notifications.drain(self.outbox,lambda e:{'confirmed':False},at=DAY)
        (self.outbox/'.delivery-index.sqlite3').unlink()
        self.add(1)
        called=[]
        notifications.drain(self.outbox,lambda e:called.append(e) or {'confirmed':True},at=DAY)
        self.assertEqual(called,[])
        with patch.object(queue.os,'scandir',side_effect=AssertionError('steady-state re-enumeration')):
            notifications.drain(self.outbox,lambda e:self.fail('UNKNOWN repeated'),at=DAY)

    def test_changed_projection_replaces_stale_first_and_rechecks_summary(self):
        self.add(0)
        runtime.save(self.base/'state.json',{'observed_at':NOW,'active':{}})
        runtime.save(self.base/'snapshot.json',{'at':NOW})
        sent=[]
        notifications.drain(self.outbox,lambda e:sent.append(e) or {'confirmed':True},at=DAY,delivery_policy={'min_summary_members':1})
        self.assertEqual(sent[0]['type'],'observer_history_summary')
        self.assertEqual(sent[0]['current_counts'],{'NOT_IN_ACTIVE_SCOPE':1})
        self.assertEqual(runtime.load(self.outbox/'00000.json')['delivery_receipt']['state'],'SUPERSEDED')

    def test_summary_members_rebuilt_after_crash_between_summary_and_source(self):
        self.add(0,generated_at=NOW-8*3600)
        original=queue.write
        def interrupted(db,path,row):
            if row['delivery_receipt']['state']=='SUPERSEDED':
                raise OSError('fixture crash before source receipt')
            return original(db,path,row)
        with patch.object(queue,'write',side_effect=interrupted):
            with self.assertRaises(OSError):notifications.prepare(self.outbox,at=DAY)
        result=notifications.prepare(self.outbox,at=DAY)
        self.assertEqual(result['pending'],1)
        summaries=[runtime.load(p) for p in self.outbox.glob('*.json') if runtime.load(p)['event'].get('summary_key')]
        self.assertEqual(summaries[0]['event']['member_count'],1)
        with queue.database(self.outbox) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM summary_members').fetchone()[0],1)

    def test_registered_producer_avoids_rescanning_terminal_history(self):
        self.add(0)
        notifications.prepare(self.outbox,at=DAY)
        queue.enqueue(self.outbox/'new.json',{'event':{'event_id':'new','role':'B','transition':'first','generated_at':NOW},
            'delivery_receipt':{'state':'PENDING'}})
        with patch.object(queue.os,'scandir',side_effect=AssertionError('registered append must be indexed')):
            sent=[]
            notifications.drain(self.outbox,lambda e:sent.append(e) or {'confirmed':True},at=DAY)
        self.assertEqual(len(sent),2)

    def test_shared_chat_budget_is_shared_across_roles(self):
        pool=self.base/'pool.json'
        runtime.save(pool,{'roles':{'A':{'chat_id':'shared'},'B':{'chat_id':'shared'}}})
        self.add(0,'A',pool_path=str(pool));self.add(1,'B',pool_path=str(pool))
        sent=[]
        notifications.drain(self.outbox,lambda e:sent.append(e) or {'confirmed':True},at=DAY,
            delivery_policy={'group_limit':1,'group_gap_seconds':0})
        self.assertEqual(len(sent),1)

    def test_archive_never_removes_unknown_or_recent_budget_evidence(self):
        self.add(0,'A');self.add(1,'B')
        notifications.drain(self.outbox,lambda e:{'confirmed':e['role']=='A'},at=DAY)
        recent=queue.archive_terminal(self.outbox,before=NOW+1,at=NOW)
        self.assertEqual(recent['items'],[])
        old=queue.archive_terminal(self.outbox,before=NOW+1,at=NOW+3601)
        self.assertEqual(len(old['items']),1)
        self.assertEqual(runtime.load(self.outbox/'00001.json')['delivery_receipt']['state'],'UNKNOWN')
        archive=Path(old['items'][0]['path'])
        self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(),old['items'][0]['sha256'])
        self.assertFalse((self.outbox/'00000.json').exists())


class HeartbeatRetentionTests(unittest.TestCase):
    def test_only_native_and_descendant_proven_exit_archives_exact_heartbeat(self):
        with tempfile.TemporaryDirectory() as folder:
            home=Path(folder);pool=home/'pool.json'; execution='a'*32;missing='b'*32
            root=home/'project';root.mkdir();(home/'cron').mkdir()
            scope={'schema_version':1,'execution_id':execution,'job_id':'A-job','role':'A',
                'pool_path':str(pool),'project_root':str(root),'generation':1,'kind':'business'}
            unit='hermes-worker-cron-A-job-exec-'+execution+'.scope'
            writer={'pid':1000000000,'process_start':'123','cgroup':'0::/fixture/'+unit}
            with sqlite3.connect(home/'cron/executions.db') as db:
                db.execute('CREATE TABLE executions(id TEXT,job_id TEXT,pid INTEGER,process_started_at TEXT,status TEXT,writer_scope TEXT)')
                db.execute('INSERT INTO executions VALUES (?,?,?,?,?,?)',(execution,'A-job',1000000000,'123','completed',json.dumps(writer)))
            for identity in (execution,missing):
                runtime.save(home/'runtime/role-pool-executions'/(identity+'.json'),{**scope,'execution_id':identity})
                runtime.save(home/'runtime/legacy-heartbeats'/(identity+'.json'),{
                    'execution_id':identity,'pool_path':str(pool),'project_root':str(root),'role':'A',
                    'pid':1000000000,'process_start':'123','state':'healthy'})
            original=(home/'runtime/legacy-heartbeats'/(execution+'.json')).read_bytes()
            result=execution_retention.archive(home,pool,apply=True,min_age_seconds=0)
            self.assertEqual([r['execution_id'] for r in result['items']],[execution])
            archived=home/'runtime/role-pool-execution-archive'/execution/'heartbeat.json'
            self.assertEqual(archived.read_bytes(),original)
            self.assertFalse((home/'runtime/legacy-heartbeats'/(execution+'.json')).exists())
            self.assertTrue((home/'runtime/legacy-heartbeats'/(missing+'.json')).exists())
            self.assertTrue((home/'runtime/role-pool-executions'/(missing+'.json')).exists())


class LogicalSummaryTests(unittest.TestCase):
    setUp=QueueTests.setUp
    add=QueueTests.add
    def summarize(self,start,count,at=NOW):
        for n in range(start,start+count):self.add(n,generated_at=NOW-8*3600)
        return notifications.prepare(self.outbox,at=at)

    def test_sent_summary_reused_across_rounds_retains_members_and_payload(self):
        self.summarize(0,5)
        sent=[]
        notifications.drain(self.outbox,lambda e:sent.append(e) or {'confirmed':True},at=DAY)
        self.assertEqual(len(sent),1)
        self.summarize(5,5,at=NOW+601)
        notifications.drain(self.outbox,lambda e:self.fail('same lifetime sent twice'),at=NOW+601)
        with queue.database(self.outbox) as db:
            rows=db.execute('SELECT body FROM queue WHERE summary_key IS NOT NULL').fetchall()
            self.assertEqual(len(rows),1)
            row=json.loads(rows[0]['body'])
            self.assertEqual(row['event']['member_count'],10)
            self.assertEqual(row['delivery_snapshot']['member_count'],5)
            self.assertEqual(len(row['summary_members']),10)
            self.assertEqual(row['delivery_receipt']['state'],'SENT')

    def test_small_summary_deferred_until_five_and_nothing_lost(self):
        self.summarize(0,4)
        called=[]
        result=notifications.drain(self.outbox,lambda e:called.append(e) or {'confirmed':True},at=DAY)
        self.assertEqual(called,[])
        self.assertIn('MIN_SUMMARY_MEMBERS',[r.get('reason') for r in result])
        self.summarize(4,1)
        notifications.drain(self.outbox,lambda e:called.append(e) or {'confirmed':True},at=DAY)
        self.assertEqual([e['member_count'] for e in called],[5])
        with queue.database(self.outbox) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM summary_members').fetchone()[0],5)

    def test_summary_handoff_is_not_unconditionally_exempt(self):
        self.assertTrue(queue.needs_handoff(self.outbox,{'summary_key':'x'},NOW,queue.DEFAULT_POLICY))
        event={'type':'observer_history_summary','summary_key':'x','member_count':4,'lifecycle':'GENERATION_SCOPE'}
        self.assertTrue(queue.needs_handoff(self.outbox,event,NOW,queue.DEFAULT_POLICY))
        event['member_count']=5
        self.assertFalse(queue.needs_handoff(self.outbox,event,NOW,queue.DEFAULT_POLICY))

    def test_snapshot_jitter_is_fresh_and_absence_is_not_unresolved(self):
        runtime.save(self.base/'state.json',{'observed_at':NOW-240,'active':{}})
        runtime.save(self.base/'snapshot.json',{'at':NOW-240})
        self.assertTrue(queue._state(self.outbox,NOW)[1])
        self.assertEqual(queue.disposition({'identity':'absent'}, {},True),'NOT_IN_ACTIVE_SCOPE')
        self.assertEqual(queue.disposition({'identity':'absent'}, {},False),'SNAPSHOT_STALE')
        self.assertFalse(queue._state(self.outbox,NOW+361)[1])

    def test_unknown_lifetime_never_rearmed_and_generation_separates_lifetimes(self):
        self.summarize(0,5)
        notifications.drain(self.outbox,lambda e:{'confirmed':False},at=DAY)
        self.summarize(5,5,at=NOW+601)
        notifications.drain(self.outbox,lambda e:self.fail('uncertain delivery repeated'),at=NOW+601)
        for n in range(10,15):self.add(n,generated_at=NOW-8*3600,generation=2)
        notifications.prepare(self.outbox,at=NOW+3601)
        sent=[]
        notifications.drain(self.outbox,lambda e:sent.append(e) or {'confirmed':True},at=NOW+3601)
        self.assertEqual(len(sent),1)
        self.assertEqual(sent[0]['generation'],2)
