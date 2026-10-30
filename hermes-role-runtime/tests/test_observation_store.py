"""R1 tests use the actual v1 interval algorithm, never a replacement scorer."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import observation_store as store
import activity_report as report

START = datetime.fromisoformat('2026-10-20T08:00:00+08:00')


def row(seconds=0, **updates):
    value = {'at':(START+timedelta(seconds=seconds)).isoformat(), 'role':'A', 'category':'waiting',
        'task':'TASK-01','attempt':1,'summary':'等待独立结果','source':'events/A/check.json',
        'basis':'script_observation','raw_status':'WAITING','classification_reason':'event_status:WAITING',
        'identity':{'pool':'pool','generation':2,'run_id':'run','role':'A','work_id':'work',
                    'execution_id':'exec','execution_generation':1}, 'extra_null':None,'extra_number':7}
    value.update(updates)
    return value


class ObservationStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self,value,**kwargs):
        return store.write_sample(self.root,value,legacy_mode='LEGACY_ONLY',**kwargs)

    def switch(self):
        manifest = store.prepare_cutover(self.root,writer_epoch='epoch-2',writer_stopped_ref='fixture:all-writers-stopped')
        store.publish_manifest(self.root,manifest,expected_manifest_id=manifest['previous_manifest_id'])
        return manifest

    def read(self,start=START,end=START+timedelta(hours=1)):
        return store.read_samples(self.root,'A',start,end,legacy_mode='LEGACY_ONLY')

    def intervals(self,values,start=START,end=START+timedelta(hours=1)):
        rows = [{**item,'_at':report.parse_at(item['at'])} for item in values]
        rows.sort(key=lambda item:item['_at'])
        return report.intervals(rows,start,end)

    def test_absent_manifest_requires_explicit_legacy_installation_and_never_guesses_v2(self):
        self.write(row())
        with self.assertRaisesRegex(store.ObservationGap,'MANIFEST_REQUIRED'):
            store.read_samples(self.root,'A',START,START+timedelta(hours=1))
        self.assertEqual(self.read()[0],[row()])
        self.switch()
        (self.root/store.MANIFEST).unlink()
        with self.assertRaisesRegex(store.ObservationGap,'MANIFEST_REQUIRED'):
            self.read()
        with self.assertRaisesRegex(store.ObservationGap,'MANIFEST_REQUIRED'):
            self.write(row(10))

    def test_subscribed_legacy_read_never_creates_target_directory_or_lock(self):
        missing = self.root/'uncreated-project'
        rows, _ = store.read_samples(missing,'A',START,START+timedelta(hours=1),legacy_mode='LEGACY_ONLY')
        self.assertEqual(rows,[])
        self.assertFalse(missing.exists())
        path=self.root/(store.BASE+'/activity/2026-10-20/A.jsonl')
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(row())+'\n')
        before={str(item.relative_to(self.root)):item.read_bytes() if item.is_file() else None
                for item in self.root.rglob('*')}
        self.assertEqual(self.read()[0],[row()])
        after={str(item.relative_to(self.root)):item.read_bytes() if item.is_file() else None
               for item in self.root.rglob('*')}
        self.assertEqual(before,after)

    def test_existing_read_lock_blocks_real_manifest_publication(self):
        self.write(row())
        manifest=store.prepare_cutover(self.root,writer_epoch='new',writer_stopped_ref='stopped')
        started,finished=threading.Event(),threading.Event()
        def publish():
            started.set()
            try:
                return store.publish_manifest(self.root,manifest,expected_manifest_id=manifest['previous_manifest_id'])
            finally:
                finished.set()
        with ThreadPoolExecutor(max_workers=1) as executor:
            with store.routing_lock(self.root,readonly=True):
                future=executor.submit(publish)
                self.assertTrue(started.wait(2))
                self.assertFalse(finished.wait(.05))
                self.assertFalse((self.root/store.MANIFEST).exists())
            self.assertEqual(future.result(timeout=3)['manifest_id'],manifest['manifest_id'])
        self.assertEqual(self.read()[0],[row()])

    def test_missing_read_lock_concurrent_publish_fails_without_legacy_fallback(self):
        self.write(row())
        manifest=store.prepare_cutover(self.root,writer_epoch='new',writer_stopped_ref='stopped')
        (self.root/(store.BASE+'/activity-storage.lock')).unlink()
        original=store._read_v1
        published=False
        def publish_during_read(*args,**kwargs):
            nonlocal published
            rows=original(*args,**kwargs)
            if not published:
                published=True
                store.publish_manifest(self.root,manifest,expected_manifest_id=manifest['previous_manifest_id'])
            return rows
        with patch.object(store,'_read_v1',side_effect=publish_during_read):
            with self.assertRaisesRegex(store.ObservationGap,'ROUTING_CHANGED_DURING_READ'):
                self.read()
        rows,metadata=self.read()
        self.assertEqual(rows,[row()])
        self.assertEqual(metadata['mode'],'DUAL_READ_V2')

    def test_readonly_v2_and_coverage_queries_do_not_recreate_missing_lock(self):
        self.switch()
        self.write(row())
        lock=self.root/(store.BASE+'/activity-storage.lock')
        lock.unlink()
        self.assertEqual(self.read()[0],[row()])
        self.assertEqual(store.coverage_runs(self.root,'A'),[])
        self.assertFalse(lock.exists())

    def test_hour_identical_payload_dedup_keeps_all_real_boundaries_and_types(self):
        self.switch()
        expected = [row(seconds) for seconds in range(0,3600,300)]
        for value in expected:
            self.write(value)
        actual,metrics = self.read()
        self.assertEqual(actual,expected)
        self.assertEqual(self.intervals(actual)[1]['waiting'],3600)
        costs = store.storage_metrics(self.root)
        self.assertEqual(costs['payload_count'],1)
        self.assertEqual(costs['observation_count'],12)
        self.assertEqual(metrics['observations_read'],12)
        self.assertIsNone(costs['model_token_savings'])

    def test_one_sample_expires_at_600_and_recovery_does_not_backfill_gap(self):
        self.switch();self.write(row());self.write(row(1200))
        segments,totals = self.intervals(self.read()[0])
        self.assertEqual(totals['waiting'],1200)
        self.assertEqual(totals['unknown'],2400)
        self.assertEqual(segments[1]['start'],(START+timedelta(seconds=600)).isoformat())

    def test_actual_refresh_at_600_overrides_event_at_450(self):
        self.switch()
        for offset in (0,300,600):self.write(row(offset))
        event = row(450,category='working',basis='recorded_event',raw_status='RUNNING',classification_reason='recorded_event_status:RUNNING')
        actual = self.intervals([*self.read(end=START+timedelta(seconds=1200))[0],event],end=START+timedelta(seconds=1200))
        expected = self.intervals([row(0),row(300),row(600),event],end=START+timedelta(seconds=1200))
        self.assertEqual(actual,expected)
        self.assertEqual(actual[1]['working'],150)
        self.assertEqual(actual[1]['waiting'],1050)

    def test_dual_read_schema_less_v1_late_v2_and_same_timestamp_stable_order(self):
        original = row(600);self.write(original)
        self.switch()
        late = row(300,category='blocked',classification_reason='late-evidence')
        same = row(600,category='working',classification_reason='same-time-new-sample')
        self.write(late);self.write(same)
        actual,_ = self.read(end=START+timedelta(seconds=1000))
        self.assertEqual(actual,[original,late,same])
        expected = self.intervals([original,late,same],end=START+timedelta(seconds=1000))
        self.assertEqual(self.intervals(actual,end=START+timedelta(seconds=1000)),expected)

    def test_equivalence_across_600_601_windows_midnight_and_restart(self):
        self.switch()
        values = [row(offset) for offset in (0,600,1201,10800,25200,57600,57900,58200)]
        for value in values:self.write(value)
        for left,right in ((0,601),(10800,25200),(57600,58600),(58000,58600)):
            a,b = START+timedelta(seconds=left),START+timedelta(seconds=right)
            actual,_ = self.read(start=a,end=b)
            filtered = [item for item in values if a-timedelta(seconds=600)<=report.parse_at(item['at'])<=b]
            self.assertEqual(self.intervals(actual,a,b),self.intervals(filtered,a,b))
        # A fresh module-independent read uses committed database state only.
        self.assertEqual(store.read_samples(self.root,'A',START,START+timedelta(days=1))[0],values)

    def test_payload_identity_reason_event_and_null_missing_are_not_content_deduped(self):
        self.switch()
        values = [row(),row(1,event_id='event-1'),row(2,event_id='event-2'),row(3,reason_codes=['new']),
            row(4,identity={'run_id':'different','execution_generation':2}),row(5,attempt='1'),row(6,task=None)]
        for value in values:self.write(value)
        self.assertEqual(store.storage_metrics(self.root)['payload_count'],len(values))
        self.assertEqual(self.read()[0],values)

    def test_original_times_missing_null_type_and_extra_fields_round_trip(self):
        self.switch()
        value=row(observed_at=None,recorded_at=3,custom={'s':None,'n':1,'b':True},classification_reason=None)
        value.pop('basis')
        self.write(value)
        self.assertEqual(self.read()[0],[value])

    def test_concurrent_same_time_samples_and_explicit_id_conflicts(self):
        self.switch()
        with ThreadPoolExecutor(max_workers=5) as executor:
            receipts=list(executor.map(lambda number:self.write(row(extra_number=number)),range(10)))
        self.assertEqual(len({item['seq'] for item in receipts}),10)
        self.assertEqual(len(self.read()[0]),10)
        receipt=self.write(row(),observation_id='stable-id')
        self.assertTrue(self.write(row(),observation_id='stable-id')['duplicate'])
        with self.assertRaisesRegex(store.ObservationGap,'ID_CONFLICT'):
            self.write(row(1),observation_id='stable-id')

    def test_manifest_unknown_conflicting_formats_overlap_and_missing_source(self):
        manifest=self.switch()
        cases=[]
        one=deepcopy(manifest);one['streams'][0]['parser_id']='mystery';cases.append(one)
        one=deepcopy(manifest);one['streams'][0]['schema_version']=9;cases.append(one)
        one=deepcopy(manifest);one['streams'][0]['source_id']='absent';cases.append(one)
        one=deepcopy(manifest);duplicate=deepcopy(one['streams'][0]);duplicate['stream_id']='overlap';duplicate.pop('writer');one['streams'].append(duplicate);cases.append(one)
        for value in cases:
            with self.assertRaises(store.ObservationGap):store.validate_manifest(self.root,value)
        with self.assertRaisesRegex(store.ObservationGap,'COMPARE_AND_SWAP'):
            store.publish_manifest(self.root,manifest,expected_manifest_id='wrong')

    def test_legacy_file_drift_and_bad_tail_stop_without_parser_fallback(self):
        self.write(row());manifest=self.switch()
        legacy=next(item for item in manifest['streams'] if item['parser_id']=='activity_v1')
        with (self.root/legacy['locator']).open('a') as out:out.write(json.dumps(row(10))+'\n')
        with self.assertRaisesRegex(store.ObservationGap,'HIGH_WATER_DRIFT'):self.read()
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/(store.BASE+'/activity/2026-10-20/A.jsonl');path.parent.mkdir(parents=True)
            path.write_text(json.dumps(row()))
            with self.assertRaisesRegex(store.ObservationGap,'INCOMPLETE_LEGACY_TAIL'):
                store.prepare_cutover(temp,writer_epoch='x',writer_stopped_ref='stopped')

    def test_cutover_rejects_escaped_legacy_symlink_before_reading_it(self):
        with tempfile.TemporaryDirectory() as outside:
            source=Path(outside)/'A.jsonl';source.write_text(json.dumps(row())+'\n')
            path=self.root/(store.BASE+'/activity/2026-10-20/A.jsonl')
            path.parent.mkdir(parents=True);path.symlink_to(source)
            with patch.object(Path,'read_bytes',side_effect=AssertionError('no escaped data read')):
                with self.assertRaisesRegex(store.ObservationGap,'LOCATOR_ESCAPES_RUNTIME'):
                    store.prepare_cutover(self.root,writer_epoch='x',writer_stopped_ref='stopped')

    def test_missing_payload_orphan_source_reference_and_high_water_are_detected(self):
        for corruption in ('missing','orphan','check','high_water'):
            with self.subTest(corruption=corruption),tempfile.TemporaryDirectory() as temp:
                root=Path(temp);manifest=store.prepare_cutover(root,writer_epoch='x',writer_stopped_ref='stopped')
                store.publish_manifest(root,manifest,expected_manifest_id=manifest['previous_manifest_id'])
                store.write_sample(root,row(),source_checks=[{'source_id':'source','source_version':'1','observed_at':START.timestamp(),
                    'last_verified_at':START.timestamp(),'max_gap_seconds':600,'check_status':'VERIFIED'}])
                with sqlite3.connect(root/store.STORE) as db:
                    if corruption=='missing':db.execute('DELETE FROM payloads')
                    if corruption=='orphan':db.execute("INSERT INTO payloads VALUES ('orphan',2,'v','{}')")
                    if corruption=='check':db.execute('DELETE FROM source_checks')
                    if corruption=='high_water':db.execute("UPDATE store_meta SET value='999' WHERE key LIKE 'source_high_water:%'")
                with self.assertRaises(store.ObservationGap):store.read_samples(root,'A',START,START+timedelta(hours=1))

    def test_transaction_failure_leaves_no_orphan_or_skeleton(self):
        self.switch()
        with patch.object(store,'_normalized_checks',side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):self.write(row())
        self.assertEqual(self.read()[0],[])
        self.assertEqual(store.storage_metrics(self.root)['payload_count'],0)

    def test_publish_crash_is_failclosed_and_exact_prepared_manifest_can_finish(self):
        manifest=store.prepare_cutover(self.root,writer_epoch='new',writer_stopped_ref='stopped')
        actual=store._atomic
        def interrupt(path,value):
            if path.name=='activity-routing.json':raise RuntimeError('power loss')
            return actual(path,value)
        with patch.object(store,'_atomic',side_effect=interrupt):
            with self.assertRaises(RuntimeError):store.publish_manifest(self.root,manifest,expected_manifest_id=manifest['previous_manifest_id'])
        with self.assertRaisesRegex(store.ObservationGap,'MANIFEST_REQUIRED'):self.read()
        store.publish_manifest(self.root,manifest,expected_manifest_id=manifest['previous_manifest_id'])
        self.write(row());self.assertEqual(self.read()[0],[row()])

    def test_two_sources_independent_coverage_failure_and_recovery_no_backfill(self):
        self.switch()
        def checks(offset,broken=False):
            return [{'source_id':source,'source_version':'v1','observed_at':START.timestamp()+offset,
                'check_status':'FAILED' if broken and source=='tools' else 'VERIFIED',
                'last_verified_at':START.timestamp()+offset,'max_gap_seconds':600,
                'reason_codes':['read_failed'] if broken and source=='tools' else [],'evidence_ref':'fixture:'+source}
                for source in ('tools','model')]
        for offset,broken in ((0,False),(300,True),(600,True),(1200,False)):
            self.write(row(offset),source_checks=checks(offset,broken))
        runs=store.coverage_runs(self.root,'A')
        tools=[item for item in runs if item['source_id']=='tools']
        model=[item for item in runs if item['source_id']=='model']
        self.assertEqual([(r['state_since']-START.timestamp(),r['coverage_until']-START.timestamp()) for r in tools],[(0,300),(1200,1800)])
        self.assertEqual([(r['state_since']-START.timestamp(),r['coverage_until']-START.timestamp()) for r in model],[(0,1800)])

    def test_explicit_rollback_keeps_sequence_then_uses_only_new_legacy_writer(self):
        self.write(row(0));self.switch();self.write(row(300));self.write(row(600))
        expected=self.intervals(self.read()[0])
        manifest=store.prepare_rollback(self.root,writer_epoch='rollback-3',writer_stopped_ref='v2-stopped')
        # Merely preparing rollback does not change routing.
        self.assertEqual(self.read()[1]['mode'],'DUAL_READ_V2')
        store.publish_manifest(self.root,manifest,expected_manifest_id=manifest['previous_manifest_id'])
        self.assertEqual(self.intervals(self.read()[0]),expected)
        self.write(row(900))
        self.assertEqual(len(self.read()[0]),4)
        self.assertEqual(store.storage_metrics(self.root)['observation_count'],2)
        second=store.prepare_cutover(self.root,writer_epoch='epoch-4',writer_stopped_ref='new-v1-stopped')
        store.publish_manifest(self.root,second,expected_manifest_id=second['previous_manifest_id'])
        self.write(row(1200))
        self.assertEqual(len(self.read()[0]),5)

    def test_actual_activity_report_sample_and_points_use_manifest_adapter(self):
        self.switch()
        snapshot={'project':str(self.root),'role':'A','observed_at':START.isoformat(),
            'events':{'A':{'status':'WAITING','task':'TASK-1','event_id':'e'}},'processes':{}}
        expected=report.sample(snapshot)
        rows,events,errors=report.points(self.root,'A',START,START+timedelta(seconds=600))
        self.assertEqual(errors,[])
        self.assertEqual([{k:v for k,v in item.items() if k!='_at'} for item in rows],[expected])
        self.assertEqual(store.storage_metrics(self.root)['observation_count'],1)
        self.assertFalse((self.root/(store.BASE+'/activity/2026-10-20/A.jsonl')).exists())

    def test_actual_sample_preserves_all_process_reasons_and_fact_sources(self):
        self.switch()
        health={'state':'unverified','owner_process_state':'alive','business_authority_state':'EXPIRED',
            'heartbeat_state':'STALE','activity_source_state':'NOT_CHECKED','checked_at':START.timestamp(),
            'source':'legacy_lease_and_pid_start','reason':'lease_expired_owner_alive',
            'reason_codes':['lease_expired_owner_alive','heartbeat_stale_owner_activity_unverified']}
        snapshot={'project':str(self.root),'role':'A','observed_at':START.isoformat(),
            'events':{'A':{'status':'WAITING','task':'TASK-1'}},'processes':{'A':health},
            'reason_codes':['additional-source-gap']}
        value=report.sample(snapshot)
        self.assertEqual(value['owner_process_state'],'alive')
        self.assertEqual(set(value['reason_codes']),{'additional-source-gap','lease_expired_owner_alive',
                                                   'heartbeat_stale_owner_activity_unverified'})
        self.assertEqual(value['reason'],health['reason'])
        for fact in value['process_facts'].values():
            self.assertEqual(fact['source'],health['source'])
            self.assertEqual(fact['checked_at'],health['checked_at'])
        rows,_,errors=report.points(self.root,'A',START,START+timedelta(seconds=600))
        self.assertEqual(errors,[])
        self.assertEqual(rows[0]['process_facts'],value['process_facts'])

    def test_summary_and_model_packet_keep_bounded_fact_diagnostics_and_full_reference(self):
        import report_summary
        self.switch()
        for offset in range(0,600,60):
            health={'state':'unverified','owner_process_state':'alive','business_authority_state':'EXPIRED',
                'heartbeat_state':'STALE','activity_source_state':'NOT_CHECKED',
                'checked_at':START.timestamp()+offset,'source':'lease-and-pid',
                'reason':'lease_expired_owner_alive','reason_codes':['lease_expired_owner_alive',
                    'heartbeat_stale_owner_activity_unverified']}
            report.sample({'project':str(self.root),'role':'A','observed_at':(START+timedelta(seconds=offset)).isoformat(),
                'events':{'A':{'status':'WAITING','task':'TASK-1'}},'processes':{'A':health}})
        summary=report.summarize(self.root,'A',START,START+timedelta(seconds=600),
            timing_index=self.root/'timing.db',executions_db=self.root/'executions.db',include_usage=False)
        self.assertEqual(len(summary['observation_diagnostics']),10)
        self.assertEqual(summary['totals_seconds']['waiting'],600)
        self.assertIn('进程仍存活',summary['observation_reason_text']['lease_expired_owner_alive'])
        data={'schema_version':2,'role':'A','kind':'phase','generated_at':START.isoformat(),
            'start':START.isoformat(),'end':(START+timedelta(seconds=600)).isoformat(),
            'artifact':'report.json','roles':[summary],'current':{}}
        packet=report_summary.summary_packet(data)['roles'][0]
        self.assertEqual(len(packet['observation_diagnostics']),6)
        self.assertEqual(packet['omitted_observation_diagnostics'],4)
        self.assertEqual(packet['observation_diagnostics_reference'],'report.json#/roles/0/observation_diagnostics')
        self.assertEqual(packet['observation_diagnostics'][0]['process_facts']['owner_process_state']['checked_at'],
                         START.timestamp()+240)
        self.assertEqual(set(packet['observation_reason_codes']),set(health['reason_codes']))

    def test_real_points_merge_different_business_events_between_refreshes(self):
        self.switch()
        for offset in (0,300,600):self.write(row(offset))
        folder=self.root/'events/A';folder.mkdir(parents=True)
        for identity,offset in (('event-one',450),('event-two',500)):
            (folder/(identity+'.json')).write_text(json.dumps({'role':'A','event_id':identity,
                'occurred_at':(START+timedelta(seconds=offset)).isoformat(),'status':'RUNNING',
                'summary':'same summary','task':'TASK-01','attempt':1}))
        actual,events,errors=report.points(self.root,'A',START,START+timedelta(seconds=1200))
        self.assertEqual(errors,[]);self.assertEqual(len(events),2)
        self.assertEqual(report.intervals(actual,START,START+timedelta(seconds=1200))[1]['working'],150)

    def test_state_change_without_source_check_and_check_reason_change_split_coverage(self):
        self.switch()
        def check(offset,reasons=()):return [{'source_id':'tools','source_version':'v1',
            'observed_at':START.timestamp()+offset,'last_verified_at':START.timestamp()+offset,
            'check_status':'VERIFIED','max_gap_seconds':600,'reason_codes':list(reasons)}]
        self.write(row(0),source_checks=check(0))
        self.write(row(300,category='working',classification_reason='changed'))
        self.write(row(400,category='working',classification_reason='changed'),source_checks=check(400))
        self.write(row(500,category='working',classification_reason='changed'),source_checks=check(500,['versioned-reason']))
        spans=store.coverage_runs(self.root,'A')
        self.assertEqual([(value['state_since']-START.timestamp(),value['coverage_until']-START.timestamp())
                         for value in spans],[(0,300),(400,500),(500,1100)])


if __name__=='__main__':unittest.main()
