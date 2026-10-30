from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from activity_signals import activity_step, record_feedback, feedback_summary


def observed(now, **updates):
    value={'identity':{'pool':'p','generation':1,'run_id':'r','role':'A','work_id':'w','execution_id':'e','execution_generation':1},
        'current_work_valid':True,'checkpoint_at':0,'required_sources':['tools','model_requests'],
        'sources':[{'source_id':source,'source_version':'v1','check_status':'VERIFIED','complete':True,
                    'last_verified_at':now,'max_gap_seconds':600,'evidence_ref':'fixture:'+source}
                    for source in ('tools','model_requests')], 'lifecycles':[]}
    value.update(updates)
    return value


class ActivitySignalTests(unittest.TestCase):
    def test_heartbeat_does_not_hide_no_activity_but_current_tool_does(self):
        fact,events=activity_step({},observed(2000,heartbeat_at=2000),2000,threshold_seconds=1800)
        self.assertEqual(fact['last_activity_at'],0)
        self.assertEqual([row['type'] for row in events],['NO_ACTIVITY'])
        active=observed(2010,lifecycles=[{'operation_id':'read','source_id':'tools','verified':True,'kind':'file_read',
            'started_at':2005,'ended_at':2009}])
        fact,events=activity_step(fact,active,2010,threshold_seconds=1800)
        self.assertEqual(fact['checkpoint_age'],2010)
        self.assertEqual(fact['last_activity_at'],2009)
        self.assertEqual([row['transition'] for row in events],['recovered'])

    def test_registered_long_wait_is_normal_wait_and_expired_contract_is_separate(self):
        life={'operation_id':'long','source_id':'model_requests','verified':True,'kind':'model',
              'started_at':100,'ended_at':None,'wait_until':5000,'evidence_ref':'request:1'}
        fact,events=activity_step({},observed(2000,lifecycles=[life]),2000,threshold_seconds=600)
        self.assertEqual(fact['state'],'NORMAL_WAIT');self.assertEqual(events,[])
        fact,events=activity_step(fact,observed(5001,lifecycles=[life]),5001,threshold_seconds=600)
        self.assertEqual([row['type'] for row in events],['OPERATION_WAIT_EXPIRED'])
        self.assertEqual(fact['checkpoint_age'],5001)

    def test_missing_partial_stale_source_is_blind_not_inactive_and_no_false_recovery(self):
        fact,_=activity_step({},observed(2000),2000,threshold_seconds=600)
        for update in ('missing','partial','stale'):
            value=observed(2100)
            if update=='missing':value['sources'].pop()
            if update=='partial':value['sources'][1]['complete']=False
            if update=='stale':value['sources'][1]['last_verified_at']=1
            new,events=activity_step(fact,value,2100,threshold_seconds=600)
            self.assertIn('ACTIVITY_SOURCE_UNVERIFIABLE',[row['type'] for row in events])
            self.assertFalse(any(row['type']=='NO_ACTIVITY' and row['transition']=='recovered' for row in events))
            self.assertEqual(new['state'],'UNVERIFIABLE')

    def test_no_wait_contract_is_unknown_and_policy_is_explicit(self):
        value=observed(2000,lifecycles=[{'operation_id':'open','source_id':'tools','verified':True,'started_at':10}])
        fact,events=activity_step({},value,2000,threshold_seconds=600)
        self.assertEqual(fact['state'],'UNVERIFIABLE')
        self.assertFalse(any(row['type']=='NO_ACTIVITY' for row in events))
        fact,events=activity_step({},observed(2000),2000)
        self.assertIn('POLICY_UNCONFIGURED',fact['reason_codes'])
        self.assertFalse(any(row['type']=='NO_ACTIVITY' for row in events))

    def test_duplicate_persistent_anomaly_dedup_and_termination_is_not_recovery(self):
        fact,events=activity_step({},observed(2000),2000,threshold_seconds=600)
        fact,events=activity_step(fact,observed(2100),2100,threshold_seconds=600)
        self.assertEqual(events,[])
        fact,events=activity_step(fact,observed(2200,current_work_valid=False,work_terminated=True),2200,threshold_seconds=600)
        self.assertEqual([row['transition'] for row in events],['closed_with_work'])

    def test_termination_closes_no_activity_even_with_blind_source_and_invalid_policy_is_rejected(self):
        fact,_=activity_step({},observed(2000),2000,threshold_seconds=600)
        ended=observed(2200,current_work_valid=False,work_terminated=True)
        ended['sources'][0]['complete']=False
        fact,events=activity_step(fact,ended,2200,threshold_seconds=600)
        self.assertIn(('NO_ACTIVITY','closed_with_work'),[(row['type'],row['transition']) for row in events])
        for threshold in (float('inf'),float('nan'),True,0):
            with self.assertRaises(ValueError):activity_step({},observed(2000),2000,threshold_seconds=threshold)

    def test_source_recovery_never_invents_old_activity_or_operation(self):
        source=observed(2000);source['sources'][0]['complete']=False
        fact,events=activity_step({},source,2000,threshold_seconds=600)
        fact,events=activity_step(fact,observed(2100),2100,threshold_seconds=600)
        self.assertEqual(fact['last_activity_at'],0)
        self.assertEqual(sorted((row['type'],row['transition']) for row in events),
                         [('ACTIVITY_SOURCE_UNVERIFIABLE','recovered'),('NO_ACTIVITY','first')])

    def test_expired_wait_requires_verified_same_operation_end_to_recover(self):
        life={'operation_id':'long','source_id':'tools','verified':True,'kind':'tool',
              'started_at':100,'ended_at':None,'wait_until':200}
        fact,_=activity_step({},observed(201,lifecycles=[life]),201,threshold_seconds=600)
        blind=observed(300);blind['sources'][0]['complete']=False
        fact,events=activity_step(fact,blind,300,threshold_seconds=600)
        self.assertFalse(any(row['type']=='OPERATION_WAIT_EXPIRED' and row['transition']=='recovered' for row in events))
        ended={**life,'ended_at':350}
        fact,events=activity_step(fact,observed(400,lifecycles=[ended]),400,threshold_seconds=600)
        self.assertIn(('OPERATION_WAIT_EXPIRED','recovered'),[(row['type'],row['transition']) for row in events])

    def test_feedback_unreviewed_denominator_and_unknown_rate(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'feedback.sqlite3'
            report=feedback_summary(path,0,100,anomalies=['one','two'])
            self.assertIsNone(report['false_positive_rate'])
            self.assertEqual(report['counts']['UNREVIEWED'],2)
            record_feedback(path,anomaly_id='one',rule_version='v1',label='FALSE_POSITIVE',
                reviewer='human',at=10,basis='fixture:long-tool',scope={'role':'A'})
            report=feedback_summary(path,0,100,anomalies=['one','two'],rule_version='v1')
            self.assertEqual(report['false_positive_rate'],1)
            self.assertEqual(report['reviewed_fraction'],.5)
            self.assertEqual(report['counts']['UNREVIEWED'],1)
            self.assertFalse(report['automatic_downgrade'])
            with self.assertRaises(ValueError):record_feedback(path,anomaly_id='two',rule_version='v1',
                label='ACKNOWLEDGED',reviewer='human',at=11,basis='read notification',scope={})

    def test_latest_label_scope_and_rolling_window_are_explicit(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'feedback.sqlite3'
            for at,label in ((10,'FALSE_POSITIVE'),(20,'TRUE_POSITIVE')):
                record_feedback(path,anomaly_id='one',rule_version='v1',label=label,
                    reviewer='human',at=at,basis='fixture:review',scope={'role':'A'})
            report=feedback_summary(path,15,100,rule_version='v1',scope={'role':'A'})
            self.assertEqual(report['false_positive_rate'],0)
            self.assertEqual(report['counts']['TRUE_POSITIVE'],1)
            self.assertIsNone(feedback_summary(path,30,100,rule_version='v1')['false_positive_rate'])


if __name__=='__main__':unittest.main()
