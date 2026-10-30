import json
import hashlib
import os
import time
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
import role_observer as observer


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        roles={r:{'skill':'test','session_file':f'sessions/{r}.md'} for r in observer.ROLES}
        self.write('ROLE-BINDINGS.json',json.dumps({'schema_version':1,'project_root':str(self.root),'control':'ACTIVE','roles':roles}))
        self.write('TODO.md','| TASK-01 | ✅ | work | AC | none | DONE | 1号 |\n')
        for folder in ['inbox/1号','inbox/2号','inbox/3号','inbox/TESTER']:
            (self.root/folder).mkdir(parents=True)
        self.ledger=patch('strong_review.Ledger.status',return_value={'authorization':'MANUAL_ONLY'})
        self.ledger.start()
    def tearDown(self):
        self.ledger.stop();self.tmp.cleanup()
    def write(self,name,body):
        p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(body);return p
    def ack(self,role='A'):
        s=observer.observe(self.root,role)
        observer.acknowledge(self.root,role,s['fingerprint'])
        return s
    def test_idle_checked_without_model_and_tick_updates(self):
        first=self.ack()
        second=observer.observe(self.root,'A')
        self.assertFalse(second['wakeAgent'])
        tick=json.loads((self.root/'inbox/control/runtime/ticks/A.json').read_text())
        self.assertEqual(tick['mode'],'script_poll')
        self.assertEqual(tick['fingerprint'],first['fingerprint'])
    def test_normal_owner_release_without_business_change_does_not_echo(self):
        self.ack('A')
        lease=self.write('inbox/control/runtime/leases/A.json',json.dumps({
            'owner':'current-worker','pid':os.getpid(),'process_start':observer.runtime.pid_identity(os.getpid())}))
        current=observer.observe(self.root,'A')
        observer.acknowledge(self.root,'A',current['fingerprint'])
        lease.unlink()
        self.assertFalse(observer.observe(self.root,'A')['wakeAgent'])
    def test_pure_session_rotation_preserves_observed_identity_without_ack_mutation(self):
        from document_store import rotate_document
        source=self.write('sessions/A.md','# 旧活动记录\n'+('已记录业务历史\n'*600))
        old=self.ack('A')
        cursor=self.root/'inbox/control/runtime/observer/A.json'
        before=cursor.read_bytes()
        checkpoint={'schema_version':1,'role':'A','task':'TASK-01','attempt':'attempt-1','status':'REVIEW_PENDING'}
        rotate_document(self.root,source,checkpoint,force=True)
        self.assertEqual(cursor.read_bytes(),before)
        current=observer.observe(self.root,'A')
        self.assertFalse(current['wakeAgent'])
        self.assertEqual(current['fingerprint'],old['fingerprint'])
        rotate_document(self.root,source,checkpoint,force=True)
        self.assertFalse(observer.observe(self.root,'A')['wakeAgent'])
    def test_session_rotation_never_hides_new_dispatch_or_raw_session_bytes(self):
        from document_store import rotate_document
        source=self.write('sessions/A.md','# 旧活动记录\n'+('业务历史\n'*600))
        old=self.ack('A')
        checkpoint={'schema_version':1,'role':'A','task':'TASK-01','attempt':'attempt-1','status':'REVIEW_PENDING'}
        rotate_document(self.root,source,checkpoint,force=True)
        self.write('inbox/1号/TASK-02/attempt-1-dispatch.md','# 派单 TASK-02\n- status: PENDING\n')
        with self.assertRaisesRegex(ValueError,'STALE_OBSERVATION'):
            observer.acknowledge(self.root,'A',old['fingerprint'])
        self.assertTrue(observer.observe(self.root,'A')['wakeAgent'])
        acknowledged=self.ack('A')
        source.write_text(source.read_text()+'\n新业务检查点：发现未解除门禁。\n')
        with self.assertRaisesRegex(ValueError,'STALE_OBSERVATION'):
            observer.acknowledge(self.root,'A',acknowledged['fingerprint'])
        self.assertTrue(observer.observe(self.root,'A')['wakeAgent'])
    def test_rotation_restores_exact_legacy_observation_spans_and_keeps_provenance(self):
        from document_store import rotate_document
        source=self.write('sessions/A.md','# 活动记录\n尚需验证\n')
        before=source.read_bytes();block='- 只读巡检，无业务变化\n'.encode()
        source.write_bytes(before+block)
        self.register([{'kind':'session_block','source':'sessions/A.md','offset':len(before),'length':len(block),
                        'sha256':hashlib.sha256(block).hexdigest()}])
        self.ack('A')
        checkpoint={'schema_version':1,'role':'A','task':'TASK-01','attempt':'attempt-1','status':'REVIEW_PENDING'}
        rotate_document(self.root,source,checkpoint,force=True)
        self.assertFalse(observer.observe(self.root,'A')['wakeAgent'])
        # A later exact observation registration must retain the old archive
        # offsets; otherwise a subsequent rotation loses its provenance.
        raw=source.read_bytes();start=raw.index(b'- ');length=raw.index(b'\n',start)+1-start
        self.register([{'kind':'session_block','source':'sessions/A.md','offset':start,'length':length,
                        'sha256':hashlib.sha256(raw[start:start+length]).hexdigest()}])
        index=json.loads((self.root/'inbox/control/runtime/observation-index/A.json').read_text())
        self.assertTrue(index.get('archived_entries'))
        self.ack('A')
        rotate_document(self.root,source,checkpoint,force=True)
        self.assertFalse(observer.observe(self.root,'A')['wakeAgent'])
    def test_tampered_rotation_evidence_is_visible_to_observer(self):
        from document_store import rotate_document
        source=self.write('sessions/A.md','# 活动记录\n原业务证据\n')
        self.ack('A')
        rotate_document(self.root,source,{'schema_version':1,'role':'A','task':'TASK-01','attempt':'attempt-1'},force=True)
        archive=next(path for path in (self.root/'archive/role-runtime').glob('*/*.md') if not path.name.endswith('.active.md'))
        archive.write_text('已被改变的归档\n')
        result=observer.observe(self.root,'A')
        self.assertTrue(result['wakeAgent'])
        self.assertIn('DOCUMENT_CONFLICT',result.get('error',''))
    def test_missing_rotation_index_or_replacement_and_invalid_provenance_are_visible(self):
        from document_store import rotate_document
        source=self.write('sessions/A.md','# 活动记录\n当前未完成事项\n')
        self.ack('A')
        rotate_document(self.root,source,{'schema_version':1,'role':'A','task':'TASK-01','attempt':'attempt-1'},force=True)
        replacement=next((self.root/'archive/role-runtime').glob('*/*.active.md'))
        raw=replacement.read_bytes();replacement.unlink()
        self.assertIn('DOCUMENT_CONFLICT',observer.observe(self.root,'A').get('error',''))
        replacement.write_bytes(raw)
        index=self.root/'inbox/control/runtime/document-history.db'
        db=index.read_bytes();index.unlink()
        self.assertIn('DOCUMENT_CONFLICT',observer.observe(self.root,'A').get('error',''))
        index.write_bytes(db)
        self.write('inbox/control/runtime/observation-index/A.json',json.dumps({
            'schema_version':1,'entries':[],'archived_entries':[{'generation':'a'*32,'entries':['damaged']}]}))
        self.assertIn('DOCUMENT_CONFLICT',observer.observe(self.root,'A').get('error',''))
    def test_quota_counter_alone_does_not_wake_coordinator(self):
        with patch('strong_review.Ledger.status',return_value={'authorization':'AUTHORIZED','used_last_60_minutes':2}):
            self.ack('COORDINATOR')
        with patch('strong_review.Ledger.status',return_value={'authorization':'AUTHORIZED','used_last_60_minutes':1}):
            self.assertFalse(observer.observe(self.root,'COORDINATOR')['wakeAgent'])
    def test_continuation_remains_pending_after_observer_ack(self):
        from context_input import publish_continuation
        self.ack('A')
        self.write('receipts/next-input.md','TASK-02 attempt-1 待完成下一验证\n')
        self.write('inbox/control/runtime/checkpoints/A.json',json.dumps({
            'schema_version':1,'role':'A','task':'TASK-02','attempt':'attempt-1','status':'RUNNING'}))
        token=self.write('token.json',json.dumps({'token':'valid'}))
        lease=self.write('inbox/control/runtime/leases/A.json',json.dumps({
            'owner':'test','token':'valid','expires_at':time.time()+60}))
        try:
            publish_continuation(self.root,'A',{'continuation_id':'next','task':'TASK-02','attempt':'attempt-1',
                'checkpoint':'receipts/next-input.md','next':'完成下一验证','runnable':True},token)
        finally:
            lease.unlink()
        current=observer.observe(self.root,'A')
        self.assertTrue(current['wakeAgent'])
        observer.acknowledge(self.root,'A',current['fingerprint'])
        self.assertTrue(observer.observe(self.root,'A')['wakeAgent'])
    def test_legacy_continuation_conflict_reports_once_without_losing_intent_or_new_dispatch(self):
        from context_input import publish_continuation, recover_continuation_input
        checkpoint = {'schema_version':1,'role':'A','task':'TASK-02','attempt':'attempt-1','status':'RUNNING'}
        self.write('inbox/control/runtime/checkpoints/A.json',json.dumps(checkpoint))
        source=self.write('receipts/next-input.md','TASK-02 attempt-1 原始输入\n')
        original=source.read_bytes()
        token=self.write('token.json',json.dumps({'token':'valid'}))
        lease=self.write('inbox/control/runtime/leases/A.json',json.dumps({'owner':'test','token':'valid','expires_at':time.time()+60}))
        continuation=publish_continuation(self.root,'A',{'continuation_id':'next','task':'TASK-02','attempt':'attempt-1',
            'checkpoint':'receipts/next-input.md','next':'完成下一验证','runnable':True},token)
        path=next((self.root/'inbox/control/runtime/continuations/A').glob('*.json'))
        del continuation['checkpoint_snapshot']; path.write_text(json.dumps(continuation))
        source.write_text('TASK-02 attempt-1 消费完成事件已追加\n')
        self.ack('A')
        before=path.read_bytes()
        self.assertFalse(observer.observe(self.root,'A')['wakeAgent'])
        self.assertEqual(path.read_bytes(),before)
        self.write('inbox/1号/TASK-03/new-dispatch.md','# 派单\n- status: PENDING\n')
        # An unbound legacy lease is unknown ownership, not proof of an empty
        # slot. Preserve the new input while requiring owner verification.
        self.assertFalse(observer.observe(self.root,'A')['wakeAgent'])
        self.ack('A')
        self.write('receipts/recovered-original.md',original.decode())
        recover_continuation_input(self.root,'A','next','receipts/recovered-original.md',token,
                                   expected_sha256=continuation['input_sha256'])
        self.assertFalse(observer.observe(self.root,'A')['wakeAgent'])
        lease.unlink()
        self.assertTrue(observer.observe(self.root,'A')['wakeAgent'])
    def test_continuation_ack_cannot_make_stale_observer_ack_consume_a_new_dispatch(self):
        from context_input import publish_continuation, acknowledge_continuation
        self.write('inbox/control/runtime/checkpoints/A.json',json.dumps({
            'schema_version':1,'role':'A','task':'TASK-02','attempt':'attempt-1','status':'RUNNING'}))
        self.write('receipts/next-input.md','TASK-02 attempt-1 原始输入\n')
        self.write('receipts/completed.md','TASK-02 attempt-1 已完成下一验证\n')
        token=self.write('token.json',json.dumps({'token':'valid'}))
        self.write('inbox/control/runtime/leases/A.json',json.dumps({'owner':'test','token':'valid','expires_at':time.time()+60}))
        continuation=publish_continuation(self.root,'A',{'continuation_id':'next','task':'TASK-02','attempt':'attempt-1',
            'checkpoint':'receipts/next-input.md','next':'完成下一验证','runnable':True},token)
        observed=observer.observe(self.root,'A')
        acknowledge_continuation(self.root,'A','next','receipts/completed.md',token,
                                  expected_sha256=continuation['input_sha256'])
        self.write('inbox/1号/TASK-03/new-dispatch.md','# 派单\n- status: PENDING\n')
        with self.assertRaisesRegex(ValueError,'STALE_OBSERVATION'):
            observer.acknowledge(self.root,'A',observed['fingerprint'])
        next_poll=observer.observe(self.root,'A')
        self.assertIn('inbox/1号/TASK-03/new-dispatch.md',next_poll['changed_sources'])
    def test_coordinator_after_rebind_only_reads_operations_for_bound_roles(self):
        binding=json.loads((self.root/'ROLE-BINDINGS.json').read_text())
        binding['roles']={'COORDINATOR':binding['roles']['COORDINATOR']}
        self.write('ROLE-BINDINGS.json',json.dumps(binding))
        def operations(root, role):
            if role not in binding['roles']: raise KeyError(role)
            return []
        with patch('runtime_tools.operation_states',side_effect=operations) as collect:
            tick=observer.observe(self.root,'COORDINATOR')
        self.assertNotEqual(tick['reason'],'observer_unavailable')
        self.assertEqual([call.args[1] for call in collect.call_args_list],['COORDINATOR'])
    def test_group_instruction_is_not_consumed_by_observer_ack(self):
        from runtime_tools import pending_requests
        from coord_queue import enqueue, acknowledge_request
        self.ack('COORDINATOR')
        request=enqueue(self.root,message_id='msg1',chat_id='coord',user_id='user',text='核对交付门禁')
        result=observer.observe(self.root,'COORDINATOR')
        self.assertTrue(result['wakeAgent'])
        observer.acknowledge(self.root,'COORDINATOR',result['fingerprint'])
        self.assertTrue(observer.observe(self.root,'COORDINATOR')['wakeAgent'])
        self.assertEqual(len(pending_requests(self.root)),1)
        evidence=self.write('decision/request.md','已处理；原门禁保持')
        acknowledge_request(self.root,request['request_id'],evidence)
        result=observer.observe(self.root,'COORDINATOR')
        observer.acknowledge(self.root,'COORDINATOR',result['fingerprint'])
        self.assertFalse(observer.observe(self.root,'COORDINATOR')['wakeAgent'])
    def test_operation_exit_wakes_owner_without_touching_task_state(self):
        self.ack('A')
        self.write('inbox/control/runtime/operations/A/test/result.json',json.dumps({'operation_id':'test','role':'A','status':'RUNNING','pid':999999999,'process_start':'1','boot_id':'old'}))
        result=observer.observe(self.root,'A')
        self.assertTrue(result['wakeAgent'])
        snapshot=json.loads(Path(result['snapshot']).read_text())
        self.assertEqual(snapshot['operations'][0]['status'],'UNKNOWN')
    def test_dispatch_wakes_until_success_ack(self):
        self.ack()
        self.write('inbox/1号/TASK-02/free-name.md','# 派单\n- status: PENDING\n- task: TASK-02\n')
        one=observer.observe(self.root,'A');two=observer.observe(self.root,'A')
        self.assertTrue(one['wakeAgent']);self.assertTrue(two['wakeAgent'])
        observer.acknowledge(self.root,'A',two['fingerprint'])
        # A handled input is not reopened merely because the task is nonterminal.
        self.assertFalse(observer.observe(self.root,'A')['wakeAgent'])
    def test_ack_cannot_swallow_new_observation(self):
        old=self.ack()
        self.write('inbox/1号/rework.md','# 返修\n- status: PENDING\n')
        observer.observe(self.root,'A')
        with self.assertRaises(ValueError):
            observer.acknowledge(self.root,'A',old['fingerprint'])
    def test_ack_cannot_swallow_input_without_an_intervening_scan(self):
        old=self.ack()
        self.write('inbox/1号/rework.md','# 返修\n- status: PENDING\n')
        with self.assertRaisesRegex(ValueError, 'STALE_OBSERVATION'):
            observer.acknowledge(self.root,'A',old['fingerprint'])
        self.assertTrue(observer.observe(self.root,'A')['wakeAgent'])
    def test_quiet_event_does_not_wake(self):
        self.ack()
        self.write('events/A/noise.json',json.dumps({'event_id':'a-poll-nochange','status':'IDLE_POLL','summary':'无新派单'}))
        self.assertFalse(observer.observe(self.root,'A')['wakeAgent'])
    def test_substantive_event_wakes(self):
        self.ack()
        self.write('events/A/real.json',json.dumps({'event_id':'delivered','status':'REVIEW_PENDING','summary':'已交付','task':'TASK-02'}))
        self.assertTrue(observer.observe(self.root,'A')['wakeAgent'])
    def test_night_quiet_does_not_prevent_new_work_wakeup(self):
        self.ack()
        self.write('events/A/real.json', json.dumps({'event_id': 'new-task', 'status': 'RUNNING', 'task': 'TASK-02'}))
        with patch.object(observer.activity_report, 'quiet', return_value=True):
            result = observer.observe(self.root, 'A')
        self.assertTrue(result['wakeAgent'])
        self.assertTrue(result['quiet_hours'])
        self.assertTrue((self.root/'inbox/control/runtime/activity').is_dir())
    def test_delivery_mentioning_no_new_task_is_not_filtered(self):
        self.ack()
        self.write('events/A/real.json',json.dumps({'event_id':'delivered','status':'REVIEW_PENDING','summary':'已交付，无新任务'}))
        self.assertTrue(observer.observe(self.root,'A')['wakeAgent'])
    def test_business_event_id_and_summary_do_not_authorize_quiet_filter(self):
        self.ack()
        self.write('events/A/real.json',json.dumps({'event_id':'delivered-nochange','status':'REVIEW_PENDING','summary':'无新任务'}))
        self.assertTrue(observer.observe(self.root,'A')['wakeAgent'])
    def test_identical_event_mirror_is_not_a_second_input(self):
        row={'event_id':'delivery','role':'A','task':'TASK-01','status':'REVIEW_PENDING','summary':'已交付',
             'occurred_at':'2026-10-14T09:00:00+08:00','recorded_at':'2026-10-14T09:01:00+08:00'}
        self.write('events/A/canonical.json',json.dumps(row))
        self.ack()
        row['recorded_at']='2026-10-14T09:02:00+08:00'
        self.write('inbox/events/A/mirror.json',json.dumps(row))
        result=observer.observe(self.root,'A')
        self.assertFalse(result['wakeAgent'])
        snapshot=observer.refresh(self.root,'A')
        self.assertEqual(len(snapshot['events']['A']['sources']),2)
    def test_same_event_id_payload_conflict_must_wake(self):
        row={'event_id':'delivery','role':'A','task':'TASK-01','status':'REVIEW_PENDING','summary':'已交付'}
        self.write('events/A/canonical.json',json.dumps(row));self.ack()
        row['status']='FAILED'
        self.write('inbox/events/A/mirror.json',json.dumps(row))
        self.assertTrue(observer.observe(self.root,'A')['wakeAgent'])
        conflicts=observer.refresh(self.root,'A')['event_conflicts']
        self.assertEqual(conflicts[0]['event_id'],'delivery')
    def register(self, entries, role='A'):
        self.write(f'inbox/control/runtime/leases/{role}.json',json.dumps({'owner':'test','token':'valid', 'expires_at':time.time()+60}))
        try:
            return observer.register_observations(self.root,role,{'schema_version':1,'classification':'READ_ONLY_OBSERVATION',
                'business_change':False,'entries':entries},'valid')
        finally:
            (self.root/f'inbox/control/runtime/leases/{role}.json').unlink()
    def test_exact_registered_legacy_session_observation_does_not_echo(self):
        self.write('sessions/A.md','## 当前状态\n开发中\n');self.ack()
        body=(self.root/'sessions/A.md').read_bytes();block='- 最后更新：2026-10-14 09:35 只读观察，零业务变化\n'.encode()
        (self.root/'sessions/A.md').write_bytes(body+block)
        self.register([{'kind':'session_block','source':'sessions/A.md','offset':len(body),'length':len(block),
                        'sha256':hashlib.sha256(block).hexdigest()}])
        self.assertFalse(observer.observe(self.root,'A')['wakeAgent'])
        (self.root/'sessions/A.md').write_bytes(body+block.replace('零业务变化'.encode(),'发现派单搁浅'.encode()))
        self.assertTrue(observer.observe(self.root,'A')['wakeAgent'])
    def test_unknown_session_observation_and_business_update_both_wake(self):
        self.write('sessions/A.md','开发中\n');self.ack()
        self.write('sessions/A.md','开发中\n第74轮只读观察：等待口径错误，三提交搁浅\n')
        self.assertTrue(observer.observe(self.root,'A')['wakeAgent'])
    def test_observation_registration_never_consumes_real_pending_input(self):
        self.write('sessions/A.md','开发中\n');old=self.ack()
        block='纯观察记录\n'.encode();p=self.root/'sessions/A.md';offset=p.stat().st_size
        p.write_bytes(p.read_bytes()+block)
        self.write('inbox/1号/new.md','# 返修\nstatus: PENDING\n')
        self.register([{'kind':'session_block','source':'sessions/A.md','offset':offset,'length':len(block),
                        'sha256':hashlib.sha256(block).hexdigest()}])
        with self.assertRaisesRegex(ValueError,'STALE_OBSERVATION'):
            observer.acknowledge(self.root,'A',old['fingerprint'])
        self.assertTrue(observer.observe(self.root,'A')['wakeAgent'])
    def test_registered_event_mirror_suppression_is_exact_and_conflict_safe(self):
        self.ack()
        row={'event_id':'obs72','role':'A','status':'OBSERVING','summary':'纯观察'}
        path=self.write('events/A/obs.json',json.dumps(row))
        self.register([{'kind':'event','source':'events/A/obs.json','sha256':hashlib.sha256(path.read_bytes()).hexdigest()}])
        self.write('inbox/events/A/mirror.json',json.dumps(row))
        self.assertFalse(observer.observe(self.root,'A')['wakeAgent'])
        row['summary']='发现新阻塞'
        self.write('inbox/events/A/mirror.json',json.dumps(row))
        self.assertTrue(observer.observe(self.root,'A')['wakeAgent'])
    def test_registered_raw_observation_cannot_suppress_concurrent_blocked_replacement(self):
        self.ack()
        row={'event_id':'obs-race','role':'A','status':'OBSERVING','summary':'纯观察'}
        path=self.write('events/A/obs-race.json',json.dumps(row))
        self.register([{'kind':'event','source':'events/A/obs-race.json','sha256':hashlib.sha256(path.read_bytes()).hexdigest()}])
        self.assertFalse(observer.observe(self.root,'A')['wakeAgent'])
        original=Path.read_bytes;replaced=[]
        def read_then_replace(target):
            raw=original(target)
            if target==path and not replaced:
                replaced.append(True)
                path.write_text(json.dumps({**row,'status':'BLOCKED','summary':'发现发布失败，必须处理'}))
            return raw
        with patch.object(Path,'read_bytes',read_then_replace):
            result=observer.observe(self.root,'A')
        self.assertTrue(result['wakeAgent'])
        self.assertEqual(observer.refresh(self.root,'A')['events']['A']['status'],'BLOCKED')
    def test_explicit_business_event_cannot_be_registered_as_observation(self):
        for field,value in (('kind','business'),('business_change',True)):
            with self.subTest(field=field):
                path=self.write('events/A/business.json',json.dumps({'event_id':'business','role':'A',
                    'status':'BLOCKED','summary':'需要修复',field:value}))
                with self.assertRaisesRegex(ValueError,'business event'):
                    self.register([{'kind':'event','source':'events/A/business.json',
                        'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}])
    def test_event_latest_orders_aware_instant_not_iso_text(self):
        self.write('events/A/newer.json',json.dumps({'event_id':'newer','status':'DONE','summary':'已完成',
            'occurred_at':'2026-10-14T10:00:00+00:00'}))
        self.write('events/A/older.json',json.dumps({'event_id':'older','status':'RUNNING','summary':'仍在执行',
            'occurred_at':'2026-10-14T11:00:00+08:00'}))
        latest=observer.refresh(self.root,'A')['events']['A']
        self.assertEqual(latest['event_id'],'newer')
        self.assertEqual(latest['occurred_at'],'2026-10-14T10:00:00+00:00')
    def test_unknown_event_time_falls_back_without_claiming_occurrence(self):
        path=self.write('events/A/unknown-time.json',json.dumps({'event_id':'unknown-time','status':'BLOCKED',
            'summary':'有问题','occurred_at':'2026-10-14 10:2x'}))
        os.utime(path,(2000000000,2000000000))
        latest=observer.refresh(self.root,'A')['events']['A']
        self.assertEqual(latest['occurred_at'],'2026-10-14 10:2x')
        self.assertEqual(latest['event_time_basis'],'source_mtime')
        self.assertEqual(latest['event_time_status'],'unknown')
    def test_unknown_markdown_body_change_after_known_fields_must_wake(self):
        body='- event_id: md-long\n- status: REVIEW_PENDING\n- summary: 已交付\n\n'+('旧材料\n'*100)
        self.write('events/A/long.md',body);self.ack()
        self.write('events/A/long.md',body+'\n新增核验：发布路径越界，必须阻断\n')
        self.assertTrue(observer.observe(self.root,'A')['wakeAgent'])
    def test_identical_markdown_mirror_is_not_a_second_event(self):
        body='- event_id: md-mirror\n- status: REVIEW_PENDING\n- summary: 已交付\n\n独立正文保留\n'
        self.write('events/A/long.md',body);self.ack()
        self.write('inbox/events/A/mirror.md',body)
        self.assertFalse(observer.observe(self.root,'A')['wakeAgent'])
    def test_markdown_unknown_context_is_not_hidden_by_quiet_status(self):
        body='- event_id: md-quiet\n- status: NO_CHANGE\n- summary: 只读观察\n'
        self.write('events/A/quiet.md',body);self.ack()
        self.write('events/A/quiet.md',body+'\n新核验：后台错误吞掉了失败，必须修复\n')
        self.assertTrue(observer.observe(self.root,'A')['wakeAgent'])
    def test_explicit_business_kind_overrides_legacy_quiet_status(self):
        self.ack()
        self.write('events/A/declared-business.json',json.dumps({'event_id':'new-input','role':'A',
            'kind':'business','status':'NO_CHANGE','summary':'用户要求核对失败判定'}))
        self.assertTrue(observer.observe(self.root,'A')['wakeAgent'])
    def test_observation_registration_rejects_business_input_and_wrong_hash(self):
        p=self.write('inbox/1号/dispatch.md','# 派单\nstatus: PENDING\n');raw=p.read_bytes()
        with self.assertRaisesRegex(ValueError,'bound role session'):
            self.register([{'kind':'session_block','source':'inbox/1号/dispatch.md','offset':0,'length':len(raw),
                            'sha256':hashlib.sha256(raw).hexdigest()}])
        self.write('sessions/A.md','## 当前状态\n观察\n')
        with self.assertRaisesRegex(ValueError,'OBSERVATION_CONTENT_CHANGED'):
            self.register([{'kind':'session_block','source':'sessions/A.md','offset':len('## 当前状态\n'.encode()),'length':len('观察\n'.encode()),'sha256':'0'*64}])
    def test_observation_registration_rejects_blanket_session_exclusion(self):
        raw='只读观察\n'.encode();self.write('sessions/A.md',raw.decode())
        with self.assertRaisesRegex(ValueError,'blanket session exclusion'):
            self.register([{'kind':'session_block','source':'sessions/A.md','offset':0,'length':len(raw),
                            'sha256':hashlib.sha256(raw).hexdigest()}])
    def test_registration_requires_existing_active_role_lease(self):
        p=self.write('sessions/A.md','观察\n');raw=p.read_bytes()
        manifest={'schema_version':1,'classification':'READ_ONLY_OBSERVATION','business_change':False,'entries':[
            {'kind':'session_block','source':'sessions/A.md','offset':0,'length':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}]}
        with self.assertRaisesRegex(ValueError,'writer token'):
            observer.register_observations(self.root,'A',manifest,'invalid')
    def test_independent_observation_samples_do_not_wake_business_role(self):
        self.ack()
        self.write('inbox/control/runtime/observations/A/sample.json',json.dumps({'status':'RUNNING','observed_at':'2026-10-14T09:00:00+08:00'}))
        self.assertFalse(observer.observe(self.root,'A')['wakeAgent'])
    def test_task_notice_only_wakes_selected_roles_and_latest_heartbeat_is_not_input(self):
        for role in ('A','C','COORDINATOR'):self.ack(role)
        notice={'schema_version':1,'observation_id':'live','notify_roles':['C','COORDINATOR'],
                'revision':1,'semantic':{'status':'SCENE_RUNNING'},'observed_at':'2026-10-14T10:00:00+08:00'}
        self.write('inbox/control/runtime/task-notices/live.json',json.dumps(notice))
        path=self.write('inbox/control/runtime/task-observations/live/latest.json',json.dumps({**notice,'heartbeat':{'age_seconds':30}}))
        self.assertFalse(observer.observe(self.root,'A')['wakeAgent'])
        self.assertTrue(observer.observe(self.root,'C')['wakeAgent'])
        self.assertTrue(observer.observe(self.root,'COORDINATOR')['wakeAgent'])
        self.ack('C');path.write_text(json.dumps({**notice,'heartbeat':{'age_seconds':10},'observed_at':'2026-10-14T10:05:00+08:00'}))
        self.assertFalse(observer.observe(self.root,'C')['wakeAgent'])
        self.assertEqual(observer.refresh(self.root,'C')['task_observations'][0]['heartbeat']['age_seconds'],10)
        notice['revision']=2;notice['semantic']['status']='SCENE_DONE'
        self.write('inbox/control/runtime/task-notices/live.json',json.dumps(notice))
        self.assertTrue(observer.observe(self.root,'C')['wakeAgent'])
    def test_registered_sampler_runs_once_per_unique_project_not_per_role(self):
        registry=self.write('registry.json',json.dumps({'projects':[{'project_root':str(self.root)},{'project_root':str(self.root)}]}))
        with patch('task_observation.sample_registered') as sample:
            records=observer.scan_registry(registry)
        self.assertEqual(len(records),5);sample.assert_called_once_with(self.root)
    def test_invalid_task_notice_fails_safe(self):
        self.ack('C');self.write('inbox/control/runtime/task-notices/broken.json','{}')
        self.assertEqual(observer.observe(self.root,'C')['reason'],'observer_unavailable')
    def test_process_exit_or_pid_reuse(self):
        lease={'owner':'worker','pid':99,'process_start':'100'}
        self.assertEqual(observer.process_health(lease,lambda p:'100')['state'],'alive')
        self.assertEqual(observer.process_health(lease,lambda p:'101')['state'],'exited_or_reused')
        self.assertEqual(observer.process_health(lease,lambda p:None)['state'],'exited_or_reused')
        self.assertEqual(observer.process_health({'owner':'worker'})['state'],'unverified')

    def test_expired_lease_keeps_live_owner_distinct_from_business_validity(self):
        """TTL failure is visible but never proves that its writer exited."""
        lease={'owner':'worker','pid':99,'process_start':'100',
               'expires_at':time.time()-3600}
        health=observer.process_health(lease,lambda p:'100')
        self.assertEqual(health['state'],'unverified')
        self.assertEqual(health['reason'],'lease_expired_owner_alive')
        self.assertEqual(health['owner_process_state'],'alive')
        self.assertTrue(observer.owner_blocks_new_execution(health))

    def test_active_lease_with_live_process_still_counts_as_working(self):
        """上限保护：租约有效且进程活着时仍判 alive，不能误放开并发唤醒。"""
        lease={'owner':'worker','pid':99,'process_start':'100',
               'expires_at':time.time()+300}
        self.assertEqual(observer.process_health(lease,lambda p:'100')['state'],'alive')

    def test_lease_without_expiry_keeps_legacy_liveness_semantics(self):
        """旧租约没有 expires_at：维持原有判活语义，不因缺字段而误判停手。"""
        lease={'owner':'worker','pid':99,'process_start':'100'}
        self.assertEqual(observer.process_health(lease,lambda p:'100')['state'],'alive')

    def test_stale_heartbeat_preserves_live_process_identity(self):
        lease={'owner':'worker','pid':99,'process_start':'100',
               'expires_at':time.time()+9999,
               'last_seen':time.time()-(observer.STALE_HEARTBEAT_SECONDS+600)}
        health=observer.process_health(lease,lambda p:'100')
        self.assertEqual(health['state'],'unverified')
        self.assertEqual(health['reason'],'heartbeat_stale_owner_activity_unverified')
        self.assertEqual(health['owner_process_state'],'alive')

    def test_fresh_heartbeat_on_active_lease_is_alive(self):
        lease={'owner':'worker','pid':99,'process_start':'100',
               'expires_at':time.time()+300,'last_seen':time.time()-60}
        self.assertEqual(observer.process_health(lease,lambda p:'100')['state'],'alive')

    def test_process_change_wakes_and_never_deletes_lease(self):
        self.write('events/A/wait.json',json.dumps({'event_id':'wait','status':'REVIEW_PENDING','summary':'交付待验收'}))
        p=self.write('inbox/control/runtime/leases/A.json',json.dumps({'owner':'worker','pid':99,'process_start':'100'}))
        with patch('role_runtime.pid_identity',return_value='100'):
            self.ack()
            self.assertFalse(observer.observe(self.root,'A')['wakeAgent'])
        with patch('role_runtime.pid_identity',return_value='101'):
            self.assertTrue(observer.observe(self.root,'A')['wakeAgent'])
        self.assertTrue(p.exists())
    def completed_checkpoint(self,status='TEST_REPORT_PENDING'):
        event={'schema_version':1,'kind':'business','role':'A','event_id':'A:delivery',
               'task':'TASK-02','attempt':'attempt-1','status':status,'evidence':'receipts/delivery.md'}
        source='events/A/'+hashlib.sha256(event['event_id'].encode()).hexdigest()+'.json'
        self.write('receipts/delivery.md','TASK-02 attempt-1 完成验证\n')
        self.write(source,json.dumps(event))
        return self.write('inbox/control/runtime/checkpoints/A.json',json.dumps({**event,'event':source}))
    def test_delivered_checkpoint_with_exact_business_event_does_not_wake_on_normal_exit(self):
        self.completed_checkpoint()
        lease=self.write('inbox/control/runtime/leases/A.json',json.dumps({'owner':'worker','pid':99,'process_start':'100'}))
        with patch('role_runtime.pid_identity',return_value='100'): self.ack('A')
        with patch('role_runtime.pid_identity',return_value='101'):
            self.assertFalse(observer.observe(self.root,'A')['wakeAgent'])
            self.assertEqual(json.loads(Path(observer.observe(self.root,'A')['snapshot']).read_text())['processes']['A']['state'],'exited_or_reused')
            self.write('inbox/1号/TASK-03/new-dispatch.md','# 派单\n- status: PENDING\n')
            self.assertTrue(observer.observe(self.root,'A')['wakeAgent'])
        self.assertTrue(lease.exists())
    def test_completion_label_without_matching_delivery_event_keeps_worker_death_visible(self):
        for mode in ('missing','mismatched','running'):
            with self.subTest(mode=mode):
                checkpoint=self.completed_checkpoint('RUNNING' if mode=='running' else 'TEST_REPORT_PENDING')
                if mode=='missing':
                    row=json.loads(checkpoint.read_text()); del row['event']; checkpoint.write_text(json.dumps(row))
                elif mode=='mismatched':
                    row=json.loads(checkpoint.read_text()); row['attempt']='attempt-2'; checkpoint.write_text(json.dumps(row))
                self.write('inbox/control/runtime/leases/A.json',json.dumps({'owner':'worker','pid':99,'process_start':'100'}))
                with patch('role_runtime.pid_identity',return_value='100'): self.ack('A')
                with patch('role_runtime.pid_identity',return_value='101'):
                    self.assertTrue(observer.observe(self.root,'A')['wakeAgent'])
    def test_delivered_event_does_not_hide_active_operation_owner_exit(self):
        self.completed_checkpoint()
        self.write('inbox/control/runtime/leases/A.json',json.dumps({'owner':'worker','pid':99,'process_start':'100'}))
        active={'source':str(self.root/'operations/ongoing.json'),'role':'A','status':'RUNNING','operation_id':'active'}
        with patch('runtime_tools.operation_states',return_value=[active]):
            with patch('role_runtime.pid_identity',return_value='100'): self.ack('A')
            with patch('role_runtime.pid_identity',return_value='101'):
                self.assertTrue(observer.observe(self.root,'A')['wakeAgent'])
    def test_delivered_event_does_not_hide_runnable_continuation_owner_exit(self):
        self.completed_checkpoint()
        self.write('inbox/control/runtime/leases/A.json',json.dumps({'owner':'worker','pid':99,'process_start':'100'}))
        pending={'source':'continuation/ongoing.json','role':'A','runnable':True,'runnable_now':True}
        with patch('context_input.pending_continuations',return_value=[pending]):
            with patch('role_runtime.pid_identity',return_value='100'): self.ack('A')
            with patch('role_runtime.pid_identity',return_value='101'):
                snapshot,_=observer.collect(self.root,'A')
                # This remains new semantic input even independently of the
                # continuation's normal forced wake.
                cursor=json.loads((self.root/'inbox/control/runtime/observer/A.json').read_text())
                self.assertNotEqual(snapshot['fingerprint'],cursor['acknowledged']['fingerprint'])
    def test_other_role_task_progress_does_not_wake_idle_role(self):
        self.write('TODO.md','| TASK-01 | ✅ | work | AC | none | DONE | 1号 |\n| TASK-02 | 🏃 | work | AC | none | RUNNING | 2号 |\n')
        self.ack()
        self.write('TODO.md','| TASK-01 | ✅ | work | AC | none | DONE | 1号 |\n| TASK-02 | 🏃 | work | AC | none | 开发新进展 | 2号 |\n')
        self.assertFalse(observer.observe(self.root,'A')['wakeAgent'])
    def test_canonical_role_document_wakes_only_owner_and_coordinator(self):
        self.write('sessions/A.md','A当前状态：等待派单\n')
        self.write('sessions/B.md','B当前状态：等待派单\n')
        self.ack('A');self.ack('B');self.ack('COORDINATOR')
        self.write('sessions/A.md','A当前状态：收到返修提示\n')
        self.assertTrue(observer.observe(self.root,'A')['wakeAgent'])
        self.assertFalse(observer.observe(self.root,'B')['wakeAgent'])
        self.assertTrue(observer.observe(self.root,'COORDINATOR')['wakeAgent'])
    def test_busy_queue_survives_independent_scanner_process_restart(self):
        import subprocess
        import sys
        self.ack()
        self.write('inbox/1号/new-dispatch.md','# 派单\nstatus: PENDING\n')
        registry=self.write('registry.json',json.dumps({'projects':[{'project_root':str(self.root)}]}))
        cmd=[sys.executable,str(Path(observer.__file__).resolve()),'scan','--registry',str(registry)]
        first=subprocess.run(cmd,check=True,capture_output=True,text=True)
        second=subprocess.run(cmd,check=True,capture_output=True,text=True)
        for result in (first,second):
            row=next(x for x in json.loads(result.stdout) if x.get('role')=='A')
            self.assertTrue(row['wakeAgent'])
            self.assertIn('inbox/1号/new-dispatch.md',row['pending']['changed_sources'])
        state=json.loads((self.root/'inbox/control/runtime/observer/A.json').read_text())
        observer.acknowledge(self.root,'A',state['observed']['fingerprint'])
        self.assertFalse(observer.observe(self.root,'A')['wakeAgent'])
    def test_phase_cards_in_snapshot_and_not_inferred_as_done(self):
        self.write('TODO.md','| TASK-16A 接收 | REQ | TASK-02 | behavior | REVIEW_PENDING / 1号 |\n')
        s=observer.refresh(self.root,'A')
        self.assertFalse(s['confirmed_idle'])
        self.assertEqual(s['tasks'][0]['task'],'TASK-16A')
    def test_invalid_sources_fail_safe(self):
        self.ack()
        self.write('TODO.md','damaged')
        self.assertTrue(observer.observe(self.root,'A')['wakeAgent'])
        self.write('ROLE-BINDINGS.json','{}')
        self.assertTrue(observer.observe(self.root,'A')['wakeAgent'])
    def test_control_change_wakes_once_then_no_business_tick(self):
        self.ack()
        p=self.root/'ROLE-BINDINGS.json';data=json.loads(p.read_text());data['control']='PAUSED';p.write_text(json.dumps(data))
        self.assertTrue(observer.observe(self.root,'A')['wakeAgent'])
        self.ack()
        self.assertFalse(observer.observe(self.root,'A')['wakeAgent'])
    def test_unchanged_coordinator_does_not_invoke_model_on_timer(self):
        self.ack('COORDINATOR')
        self.assertFalse(observer.observe(self.root,'COORDINATOR')['wakeAgent'])
        self.assertFalse(observer.observe(self.root,'COORDINATOR',clock=lambda:observer.time.time()+1801)['wakeAgent'])

    def ocr_run(self, name='run-1', state='RUNNING', registered=True):
        directory='reviews/TASK-02/ocr-'+name
        spec={'schema':'coordinator.ocr-run/v1','project':str(self.root),'run_id':name,
              'review_identity':'identity-'+name,'task':'TASK-02','attempt':'attempt-1',
              'base':'a'*40,'head':'b'*40,'unit':'coordinator-ocr-'+name+'.service',
              'batches':[{'id':'b01','files':['app/service.py']}],
              'provider':'vllm-local','model':'test'}
        self.write(directory+'/spec.json',json.dumps(spec))
        record={'state':state,'run_id':name,'unit':spec['unit'],'updated_at':'2026-10-15T01:00:00+08:00',
                'batches':[{'id':'b01','files':['app/service.py'],'state':'RUNNING','pid':11}],
                'gate_decision':'NOT_EVALUATED'}
        path=self.write(directory+'/state.json',json.dumps(record))
        if registered:
            self.write('inbox/control/runtime/ocr/'+spec['review_identity']+'.json',
                       json.dumps({'run':str(path.parent),'unit':spec['unit'],'updated_at':record['updated_at']}))
        return path,record,spec

    def test_ocr_terminal_result_wakes_coordinator_and_remains_pending_until_ack(self):
        path,record,_=self.ocr_run();self.ack('COORDINATOR')
        record.update(state='AWAITING_DECISION',ended_at='2026-10-15T01:59:00+08:00')
        record['batches'][0].update(state='REVIEW_COMPLETE',returncode=0,session_file='/external/session.jsonl')
        path.write_text(json.dumps(record))
        result=observer.observe(self.root,'COORDINATOR')
        self.assertTrue(result['wakeAgent'])
        self.assertIn(str(path.relative_to(self.root)),result['pending']['changed_sources'])
        self.assertTrue(observer.observe(self.root,'COORDINATOR')['wakeAgent'])
        snapshot=observer.refresh(self.root,'COORDINATOR')
        self.assertEqual(snapshot['ocr_runs'][0]['state'],'AWAITING_DECISION')
        self.assertEqual(snapshot['ocr_runs'][0]['gate_decision'],'NOT_EVALUATED')
        observer.acknowledge(self.root,'COORDINATOR',snapshot['fingerprint'])
        self.assertFalse(observer.observe(self.root,'COORDINATOR')['wakeAgent'])

    def test_ocr_heartbeat_batch_progress_and_log_appends_do_not_wake(self):
        path,record,_=self.ocr_run();self.ack('COORDINATOR')
        record.update(updated_at='2026-10-15T01:05:00+08:00',heartbeat=99)
        record['batches'][0].update(session_mtime=123,session_file='/external/new-session.jsonl',pid=12)
        record['batches'].append({'id':'b02','files':['app/next.py'],'state':'RUNNING'})
        path.write_text(json.dumps(record))
        self.write(str(path.parent.relative_to(self.root))+'/b01.log','new progress\n'*2000)
        self.assertFalse(observer.observe(self.root,'COORDINATOR')['wakeAgent'])

    def test_ocr_failure_and_identity_change_wake(self):
        path,record,_=self.ocr_run();self.ack('COORDINATOR')
        record['batches'][0].update(state='HARD_DEADLINE',returncode=1)
        path.write_text(json.dumps(record))
        self.assertTrue(observer.observe(self.root,'COORDINATOR')['wakeAgent'])
        self.ack('COORDINATOR')
        spec_path=path.with_name('spec.json');spec=json.loads(spec_path.read_text());spec['head']='c'*40
        spec_path.write_text(json.dumps(spec))
        self.assertTrue(observer.observe(self.root,'COORDINATOR')['wakeAgent'])

    def test_ocr_legacy_unregistered_run_is_observed_and_other_roles_are_isolated(self):
        path,record,_=self.ocr_run(registered=False)
        for role in observer.ROLES:self.ack(role)
        record.update(state='BLOCKED',error='snapshot changed');path.write_text(json.dumps(record))
        self.assertTrue(observer.observe(self.root,'COORDINATOR')['wakeAgent'])
        for role in ('A','B','C','TESTER'):
            self.assertFalse(observer.observe(self.root,role)['wakeAgent'])

    def test_ocr_terminal_queues_while_owner_alive_and_survives_release(self):
        path,record,_=self.ocr_run();self.ack('COORDINATOR')
        lease=self.write('inbox/control/runtime/leases/COORDINATOR.json',json.dumps({
            'owner':'human-role','pid':os.getpid(),'process_start':observer.runtime.pid_identity(os.getpid())}))
        record['state']='AWAITING_DECISION';path.write_text(json.dumps(record))
        result=observer.observe(self.root,'COORDINATOR')
        self.assertFalse(result['wakeAgent'])
        self.assertEqual(result['reason'],'queued_while_role_owner_alive')
        self.assertIn(str(path.relative_to(self.root)),result['pending']['changed_sources'])
        lease.unlink()
        self.assertTrue(observer.observe(self.root,'COORDINATOR')['wakeAgent'])

    def test_ocr_terminal_ack_race_cannot_consume_unseen_result(self):
        path,record,_=self.ocr_run();old=self.ack('COORDINATOR')
        record['state']='AWAITING_DECISION';path.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError,'STALE_OBSERVATION'):
            observer.acknowledge(self.root,'COORDINATOR',old['fingerprint'])
        self.assertTrue(observer.observe(self.root,'COORDINATOR')['wakeAgent'])

    def test_ocr_bound_worker_exit_wakes_without_final_state_write(self):
        path,record,_=self.ocr_run()
        record.update(pid=99,process_start_ticks='100');path.write_text(json.dumps(record))
        with patch('role_runtime.pid_identity',return_value='100'):
            self.ack('COORDINATOR')
            self.assertFalse(observer.observe(self.root,'COORDINATOR')['wakeAgent'])
        for identity in (None,'101','UNVERIFIABLE'):
            with self.subTest(identity=identity),patch('role_runtime.pid_identity',return_value=identity):
                result=observer.observe(self.root,'COORDINATOR')
                self.assertTrue(result['wakeAgent'])
                run=observer.refresh(self.root,'COORDINATOR')['ocr_runs'][0]
                self.assertEqual(run['recorded_state'],'RUNNING')
                self.assertEqual(run['state'],'UNKNOWN' if identity=='UNVERIFIABLE' else 'INTERRUPTED')
                self.assertEqual(json.loads(path.read_text())['state'],'RUNNING')

    def test_ocr_legacy_missing_worker_is_interrupted_without_claiming_completion(self):
        path,record,spec=self.ocr_run(registered=False)
        record.update(pid=999999999,cgroup='0::/app.slice/'+spec['unit']);path.write_text(json.dumps(record))
        result=observer.observe(self.root,'COORDINATOR')
        self.assertTrue(result['wakeAgent'])
        run=observer.refresh(self.root,'COORDINATOR')['ocr_runs'][0]
        self.assertEqual(run['state'],'INTERRUPTED')
        self.assertEqual(run['gate_decision'],'NOT_EVALUATED')

    def test_ocr_legacy_live_pid_needs_matching_dedicated_cgroup(self):
        state={'state':'RUNNING','pid':99,'cgroup':'0::/app.slice/coordinator-ocr-test.service'}
        spec={'unit':'coordinator-ocr-test.service'}
        with patch('role_runtime.pid_identity',return_value='100'):
            with patch.object(Path,'read_text',return_value=state['cgroup']+'\n'):
                self.assertEqual(observer.ocr_process_state(state,spec),'alive')
            with patch.object(Path,'read_text',return_value='0::/another.service\n'):
                self.assertEqual(observer.ocr_process_state(state,spec),'exited_or_reused')
            with patch.object(Path,'read_text',side_effect=PermissionError()):
                self.assertEqual(observer.ocr_process_state(state,spec),'unverified')

    def test_ocr_malformed_state_and_outside_registered_path_fail_closed(self):
        path,_,spec=self.ocr_run();self.ack('COORDINATOR');path.write_text('{broken')
        result=observer.observe(self.root,'COORDINATOR')
        self.assertEqual(result['reason'],'observer_unavailable')
        self.assertIn('OCR',result['error'])
        self.assertIn('state.json',result['error'])
        self.write('inbox/control/runtime/ocr/'+spec['review_identity']+'.json',json.dumps({'run':'/tmp/outside-ocr'}))
        result=observer.observe(self.root,'COORDINATOR')
        self.assertEqual(result['reason'],'observer_unavailable')
        self.assertIn('outside project',result['error'])

    def test_ocr_missing_state_invalid_index_and_escaped_symlink_do_not_silently_skip(self):
        path,record,spec=self.ocr_run();self.ack('COORDINATOR')
        path.unlink()
        result=observer.observe(self.root,'COORDINATOR')
        self.assertEqual(result['reason'],'observer_unavailable')
        self.assertIn('state.json',result['error'])
        path.write_text(json.dumps(record))
        index=self.root/('inbox/control/runtime/ocr/'+spec['review_identity']+'.json')
        original=index.read_text();index.write_text('[]')
        self.assertEqual(observer.observe(self.root,'COORDINATOR')['reason'],'observer_unavailable')
        index.write_text(original)
        with tempfile.TemporaryDirectory() as outside:
            target=Path(outside)/'state.json';target.write_text(json.dumps(record))
            path.unlink();path.symlink_to(target)
            result=observer.observe(self.root,'COORDINATOR')
            self.assertEqual(result['reason'],'observer_unavailable')
            self.assertIn('outside project',result['error'])

    def test_ocr_ready_legacy_run_is_in_initial_notification_sources(self):
        path,_,_=self.ocr_run(state='AWAITING_DECISION',registered=False)
        result=observer.observe(self.root,'COORDINATOR')
        self.assertTrue(result['wakeAgent'])
        self.assertIn(str(path.relative_to(self.root)),result['pending']['changed_sources'])

    def test_ocr_result_observation_does_not_override_paused_control(self):
        path,record,_=self.ocr_run()
        binding=self.root/'ROLE-BINDINGS.json';data=json.loads(binding.read_text());data['control']='PAUSED';binding.write_text(json.dumps(data))
        self.ack('COORDINATOR')
        record['state']='AWAITING_DECISION';path.write_text(json.dumps(record))
        result=observer.observe(self.root,'COORDINATOR')
        self.assertEqual(result['control'],'PAUSED')
        with self.assertRaisesRegex(ValueError,'not ACTIVE'):
            observer.runtime.guard(data,'COORDINATOR')
        self.assertEqual(json.loads(path.read_text())['gate_decision'],'NOT_EVALUATED')
    def test_running_work_without_changes_does_not_start_second_round(self):
        self.write('events/A/real.json',json.dumps({'event_id':'work','status':'RUNNING','task':'TASK-01'}))
        self.ack()
        self.assertFalse(observer.observe(self.root,'A',clock=lambda:observer.time.time()+1801)['wakeAgent'])
    def test_queue_survives_busy_worker_and_coalesces_changes(self):
        old=self.ack()
        self.write('inbox/1号/new-dispatch.md','# 派单\nstatus: PENDING\n')
        first=observer.observe(self.root,'A')
        self.write('inbox/1号/new-context.md','# 本单契约补充\n时间单位为ns\n')
        second=observer.observe(self.root,'A')
        self.assertTrue(second['wakeAgent'])
        self.assertEqual(first['pending']['first_seen_at'],second['pending']['first_seen_at'])
        self.assertIn('inbox/1号/new-dispatch.md',second['pending']['changed_sources'])
        self.assertIn('inbox/1号/new-context.md',second['pending']['changed_sources'])
        with self.assertRaises(ValueError):
            observer.acknowledge(self.root,'A',old['fingerprint'])
        observer.acknowledge(self.root,'A',second['fingerprint'])
        self.assertFalse(observer.observe(self.root,'A')['wakeAgent'])
        state=json.loads((self.root/'inbox/control/runtime/observer/A.json').read_text())
        self.assertNotIn('pending',state)

    def test_live_role_owner_queues_changes_without_second_model_and_resumes_after_release(self):
        self.ack()
        lease=self.write('inbox/control/runtime/leases/A.json',json.dumps({
            'owner':'human-role','pid':os.getpid(),'process_start':observer.runtime.pid_identity(os.getpid())}))
        self.write('inbox/1号/new-dispatch.md','# 派单\nstatus: PENDING\n')
        result=observer.observe(self.root,'A')
        self.assertFalse(result['wakeAgent'])
        self.assertEqual(result['reason'],'queued_while_role_owner_alive')
        self.assertIn('inbox/1号/new-dispatch.md',result['pending']['changed_sources'])
        self.assertTrue(lease.exists())
        lease.unlink()  # Simulate the owner performing its normal release.
        resumed=observer.observe(self.root,'A')
        self.assertTrue(resumed['wakeAgent'])
        self.assertEqual(result['pending']['first_seen_at'],resumed['pending']['first_seen_at'])

    def test_broken_source_is_queued_while_owner_alive_and_verified_after_release(self):
        self.ack()
        lease=self.write('inbox/control/runtime/leases/A.json',json.dumps({
            'owner':'human-role','pid':os.getpid(),'process_start':observer.runtime.pid_identity(os.getpid())}))
        todo=self.root/'TODO.md'; original=todo.read_text(); todo.write_text('broken')
        result=observer.observe(self.root,'A')
        self.assertFalse(result['wakeAgent'])
        state=json.loads((self.root/'inbox/control/runtime/observer/A.json').read_text())
        self.assertIn('error',state['pending'])
        lease.unlink(); todo.write_text(original)
        self.assertTrue(observer.observe(self.root,'A')['wakeAgent'])

    def test_corrupt_notification_cursor_is_preserved_and_other_roles_still_scanned(self):
        cursor=self.write('inbox/control/runtime/observer/A.json','{"pending": CORRUPTED INPUT')
        raw=cursor.read_bytes()
        registry=self.write('registry.json',json.dumps({'projects':[{'project_root':str(self.root)}]}))
        rows=observer.scan_registry(registry)
        self.assertEqual(len(rows),5)
        state=json.loads(cursor.read_text())
        self.assertEqual(Path(state['recovery_required']['preserved']).read_bytes(),raw)
        self.assertTrue(state['pending'])

    def test_large_change_queue_keeps_full_index_without_flooding_model_input(self):
        self.ack()
        for number in range(30):
            self.write('inbox/1号/context-'+str(number)+'.md','New task detail '+str(number))
        result=observer.observe(self.root,'A')
        self.assertEqual(len(result['pending']['changed_sources']),12)
        saved=json.loads(Path(result['pending']['full_source_index']).read_text())
        self.assertGreaterEqual(len(saved['pending']['changed_sources']),30)
        self.assertEqual(result['pending']['changed_source_count'],len(saved['pending']['changed_sources']))
    def test_reverted_change_still_needs_queue_consumption(self):
        self.ack()
        path=self.root/'TODO.md';original=path.read_text()
        path.write_text(original.replace('DONE','新通知'))
        observer.observe(self.root,'A')
        path.write_text(original)
        current=observer.observe(self.root,'A')
        self.assertTrue(current['wakeAgent'])
        observer.acknowledge(self.root,'A',current['fingerprint'])
        self.assertFalse(observer.observe(self.root,'A')['wakeAgent'])
    def test_independent_scan_checks_all_roles_and_preserves_worker_lease(self):
        lease=self.write('inbox/control/runtime/leases/A.json',json.dumps({'owner':'busy-worker','pid':os.getpid(),'process_start':observer.runtime.pid_identity(os.getpid())}))
        original=lease.read_text()
        registry=self.write('registry.json',json.dumps({'projects':[{'project_root':str(self.root)},{'project_root':str(self.root)}]}))
        records=observer.scan_registry(registry)
        self.assertEqual(len(records),5)
        self.assertEqual(lease.read_text(),original)
        self.assertTrue(all(row.get('pending') for row in records))
        for role in observer.ROLES:
            state=json.loads((self.root/f'inbox/control/runtime/observer/{role}.json').read_text())
            self.assertNotIn('acknowledged',state)
    def test_report_projection_bounded_and_preserves_evidence_time(self):
        self.write('events/A/progress.json',json.dumps({'event_id':'work','summary':'进展'*10000,'occurred_at':'2020-01-01T00:00:00+08:00','evidence':'receipts/x.md'}))
        s=observer.refresh(self.root,'A');out=observer.render(s)
        self.assertLess(len(out),3000)
        self.assertIn('2020-01-01',out)
        self.assertIn('未执行当次运行核验',out)
    def test_new_dispatch_overrides_old_idle_classification(self):
        self.write('events/TESTER/old.json',json.dumps({'event_id':'idle','status':'IDLE','summary':'曾无任务'}))
        self.write('inbox/TESTER/TASK-02/anything.md','# 派单\nstatus: PENDING\n')
        s=observer.refresh(self.root,'TESTER')
        self.assertFalse(s['confirmed_idle'])
        self.assertIn('新派单',observer.render(s))


class PoolObserverTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.pool=Path(self.tmp.name)/'pool.json'
        self.root=Path(self.tmp.name)/'target'
        self.module=Mock()
        self.module.status.return_value={'pending':[]}
        self.module.resolve_role.return_value={'state':'ACTIVE','pool_path':str(self.pool),
            'project_root':str(self.root),'generation':2,'requirement_id':'new-requirement','role':'A'}
        for target, value in [('requirement_module',self.module)]:
            cm=patch.object(observer.runtime,target,return_value=value);cm.start();self.addCleanup(cm.stop)
        cm=patch.dict(os.environ,{},clear=True);cm.start();self.addCleanup(cm.stop)

    def test_independent_poll_reconciles_and_observes_current_root(self):
        with patch.object(observer,'_pool_adapter',return_value='adapter'), patch.object(observer,'observe',return_value={'wakeAgent':True}) as observe:
            result=observer.pool_poll(self.pool,'A')
        self.module.reconcile.assert_called_once_with(self.pool,scheduler_adapter='adapter',apply=True)
        observe.assert_called_once_with(self.root,'A')
        self.assertEqual(result['role_pool']['requirement_id'],'new-requirement')

    def test_frozen_worker_does_not_follow_changed_current_generation(self):
        with patch.dict(os.environ,{'HERMES_ROLE_POOL_PATH':str(self.pool),
            'HERMES_ROLE_POOL_GENERATION':'1','HERMES_ROLE_NAME':'A',
            'HERMES_ROLE_PROJECT_ROOT':str(self.root),'HERMES_ROLE_REQUIREMENT_ID':'old'}), patch.object(observer,'observe') as observe:
            result=observer.pool_poll(self.pool,'A')
        self.assertFalse(result['wakeAgent'])
        self.assertEqual(result['reason'],'role_pool_frozen_scope_changed')
        observe.assert_not_called();self.module.reconcile.assert_not_called()

    def test_pending_selection_does_not_expose_old_scope_as_runnable(self):
        self.module.status.return_value={'pending':[{'request_id':'new-request'}]}
        with patch.object(observer,'observe') as observe:
            result=observer.pool_poll(self.pool,'A',reconcile=False)
        self.assertFalse(result['wakeAgent'])
        self.assertEqual(result['reason'],'role_pool_selection_pending')
        observe.assert_not_called()

    def test_idle_pool_or_broken_resolution_never_wakes_model(self):
        self.module.resolve_role.return_value={'state':'IDLE'}
        with patch.object(observer,'observe') as observe:
            self.assertFalse(observer.pool_poll(self.pool,'A',reconcile=False)['wakeAgent'])
        observe.assert_not_called()
        self.module.resolve_role.side_effect=ValueError('invalid pool')
        self.assertFalse(observer.pool_poll(self.pool,'A',reconcile=False)['wakeAgent'])


if __name__=='__main__':
    unittest.main()
