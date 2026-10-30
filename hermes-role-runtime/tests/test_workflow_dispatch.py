"""Bounded wakeups through isolated ledgers and the real synchronous native entry."""
import contextlib
import contextvars
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
CORE=Path('/home/wang/.hermes/hermes-agent')
for directory in (ROOT,ROOT/'tests',CORE):
    sys.path.insert(0,str(directory))
from test_business_ledger import LedgerFixture
import workflow_runtime as workflow
import workflow_dispatch as dispatch
from tools import cronjob_tools as native
from cron import scheduler
from cron.scheduler_provider import _profile_cron_scope
from hermes_constants import get_hermes_home


class WakeupTests(LedgerFixture):
    def setUp(self):
        super().setUp()
        self.pool_path,self.home=self.pool('base',self.path)
        self.releases=[]
        with dispatch._STATE['lock']:
            dispatch._STATE['closed']=False
            dispatch._STATE['gaps'].clear()

    def pool(self,name,database=None):
        home=self.root/name/'home'; (home/'cron').mkdir(parents=True)
        pool=self.root/'isolated.json' if database else self.root/name/'pool.json'
        pool.write_text(json.dumps({'roles':{role:{'poll_job_id':role+'-poll','chat_id':'chat-'+role}
                                            for role in workflow.ROLES}}))
        (home/'cron/jobs.json').write_text(json.dumps({'jobs':[{'id':role+'-poll',
            'name':role+' poll','enabled':True,'paused':False} for role in workflow.ROLES]}))
        if database is None:
            database=workflow.ledger_path(pool)
            database.parent.mkdir(parents=True)
            with sqlite3.connect(self.path) as source,sqlite3.connect(database) as target:
                source.backup(target)
        return pool,home

    def wait_idle(self):
        deadline=time.monotonic()+5
        while dispatch.state()['active'] and time.monotonic()<deadline:
            time.sleep(.01)
        self.assertEqual(dispatch.state()['active'],0,dispatch.state())

    def tearDown(self):
        for event in self.releases: event.set()
        self.wait_idle()
        with dispatch._STATE['lock']: dispatch._STATE['closed']=False
        super().tearDown()

    def native_body(self,body):
        stack=contextlib.ExitStack()
        stack.enter_context(mock.patch.object(native,'claim_job_for_fire',
            side_effect=lambda job,**kw:{'id':job,'fire_claim':{'by':'offline-owner'}}))
        stack.enter_context(mock.patch.object(native,'get_job',return_value={'last_status':'ok'}))
        stack.enter_context(mock.patch.object(native,'_run_heartbeat',side_effect=lambda *a:contextlib.nullcontext()))
        stack.enter_context(mock.patch.object(scheduler,'run_one_job',side_effect=body))
        return stack

    def test_slow_native_job_does_not_block_other_roles_notifications_or_collection_lock(self):
        release=threading.Event(); self.releases.append(release)
        entered={role:threading.Event() for role in ('COORDINATOR','A')}
        def body(job,**kwargs):
            entered[job['id'].removesuffix('-poll')].set()
            self.assertTrue(release.wait(5))
            return True
        self.assertCommitted(self.ledger.queue_notification('WORK_CLAIMED',{'text':'offline notice'},
            command_id='notice',source_event_id=1,run_id='run-one',role='A'))
        before=self.ledger.snapshot()
        sent=[]
        # This case exercises dispatch concurrency in daytime. Delivery's
        # quiet-hours case is independent of the wall time running this suite.
        with self.native_body(body), mock.patch.object(workflow,'open_ledger',return_value=self.ledger):
            started=time.monotonic()
            result=workflow.event_tick(self.pool_path,home=self.home,
                notification_transport=lambda row:sent.append(row) or {'delivered':True,'message_id':'offline'})
            self.assertLess(time.monotonic()-started,3)
            self.assertEqual(len(sent),1)
            for event in entered.values(): self.assertTrue(event.wait(3),result)
            self.assertEqual(dispatch.state()['active'],2)
            joined=workflow.request_role_wakeup(self.pool_path,'COORDINATOR',home=self.home,source='control')
            self.assertEqual(joined['state'],'COALESCED')
            self.assertEqual(joined['request_id'],result['dispatch']['COORDINATOR']['request_id'])
            rows=dispatch.receipts(self.home,self.pool_path,joined['request_id'])
            self.assertEqual(dispatch.receipt_path(self.home,self.pool_path).stat().st_mode & 0o777,0o600)
            self.assertIn('request_entered',[r['stage'] for r in rows])
            self.assertIn('join',[r['stage'] for r in rows])
            self.assertNotIn('finish',[r['stage'] for r in rows])
            self.assertEqual(self.ledger.snapshot()['event_high_water'],before['event_high_water'])
            # The original admission lock is free while the native body waits.
            from cron.role_pool_scheduler import admission_guard
            with admission_guard(self.pool_path): pass
            release.set(); self.wait_idle()
        rows=dispatch.receipts(self.home,self.pool_path,joined['request_id'])
        self.assertEqual([r['outcome'] for r in rows if r['stage']=='claim_decided'],['ACQUIRED'])
        self.assertIn('finish',[r['stage'] for r in rows])

    def test_five_process_slots_are_shared_across_homes_and_independent_of_ticker_pool_queue(self):
        release=threading.Event(); ticker_release=threading.Event()
        self.releases.extend((release,ticker_release))
        ticker_started=threading.Event(); queued_started=threading.Event()
        homes=[self.pool('profile-'+str(i)) for i in range(6)]
        entered={str(home):threading.Event() for _,home in homes}
        profile=contextvars.ContextVar('offline_profile')
        seen=[]
        def body(job,**kwargs):
            home=str(get_hermes_home()); seen.append((home,profile.get()))
            entered[home].set()
            self.assertTrue(release.wait(5))
            return True
        with _profile_cron_scope(homes[0][1]):
            ticker_pool=scheduler._get_parallel_pool(1)
            first=ticker_pool.submit(lambda:ticker_started.set() or ticker_release.wait(5))
            second=ticker_pool.submit(queued_started.set)
        self.assertTrue(ticker_started.wait(3))
        try:
            with self.native_body(body),mock.patch.object(scheduler,'_get_parallel_pool',
                    side_effect=AssertionError('direct events must not enter ticker executor')):
                for pool,home in homes[:5]:
                    token=profile.set(str(home))
                    try: result=workflow.request_role_wakeup(pool,'COORDINATOR',home=home)
                    finally: profile.reset(token)
                    self.assertEqual(result['state'],'SUBMITTED',result)
                    self.assertTrue(entered[str(home)].wait(3))
                self.assertEqual(dispatch.state()['active'],5)
                self.assertFalse(queued_started.is_set())
                sixth=workflow.request_role_wakeup(homes[5][0],'COORDINATOR',home=homes[5][1])
                self.assertEqual(sixth['reason'],'CAPACITY_FULL')
                self.assertFalse(entered[str(homes[5][1])].is_set())
                ticker_release.set(); first.result(timeout=3); second.result(timeout=3)
                self.assertTrue(queued_started.is_set())
                self.assertEqual(dispatch.state()['active'],5)
                self.assertTrue(all(home==scope for home,scope in seen))
                release.set(); self.wait_idle()
        finally:
            ticker_release.set(); release.set()
            ticker_pool.shutdown(wait=True)
            scheduler.discard_parallel_pools([str(homes[0][1].resolve())])

    def test_same_stable_role_does_not_gain_a_slot_after_generation_change_or_wrong_token_release(self):
        release=threading.Event(); entered=threading.Event(); self.releases.append(release)
        def body(job):
            entered.set(); self.assertTrue(release.wait(5)); return {'success':False}
        before=self.ledger.snapshot()['event_high_water']
        first=workflow.request_role_wakeup(self.pool_path,'COORDINATOR',home=self.home,dispatcher=body)
        self.assertTrue(entered.wait(3))
        key=dispatch._intent(self.pool_path,'COORDINATOR',self.home)['key']
        self.assertIsNone(dispatch._release(key,'another-token'))
        self.assertEqual(dispatch.state()['active'],1)
        with sqlite3.connect(self.path) as db: db.execute('UPDATE authority SET generation=generation+1')
        changed=workflow.request_role_wakeup(self.pool_path,'COORDINATOR',home=self.home,dispatcher=body)
        self.assertEqual(changed['state'],'DEFERRED_SCOPE_CHANGE')
        self.assertEqual(changed['existing_request_id'],first['request_id'])
        self.assertEqual(dispatch.state()['active'],1)
        self.assertEqual(self.ledger.snapshot()['event_high_water'],before)

    def test_failed_start_and_receipt_writes_release_slots_and_closed_adapter_preserves_events(self):
        before=self.ledger.snapshot()['event_high_water']
        failed=mock.Mock(); failed.start.side_effect=RuntimeError('cannot start')
        with mock.patch.object(dispatch,'spawn_context_thread',return_value=failed):
            result=workflow.request_role_wakeup(self.pool_path,'COORDINATOR',home=self.home)
        self.assertEqual(result['reason'],'THREAD_START_FAILED')
        self.assertEqual(dispatch.state()['active'],0)
        release=threading.Event(); entered=threading.Event(); self.releases.append(release)
        original=dispatch.spawn_context_thread
        def start_then_raise(*a,**kw):
            thread=original(*a,**kw); start=thread.start
            def broken_start(): start(); raise RuntimeError('after actual thread start')
            thread.start=broken_start
            return thread
        def slow(job):
            entered.set(); self.assertTrue(release.wait(5)); return {'success':False}
        with mock.patch.object(dispatch,'spawn_context_thread',side_effect=start_then_raise):
            result=workflow.request_role_wakeup(self.pool_path,'COORDINATOR',home=self.home,dispatcher=slow)
        self.assertEqual(result['state'],'SUBMITTED')
        self.assertTrue(entered.wait(3))
        self.assertEqual(dispatch.state()['active'],1)
        release.set(); self.wait_idle()
        called=threading.Event()
        with mock.patch.object(dispatch,'_persist',side_effect=OSError('offline disk unavailable')), \
                mock.patch.object(dispatch.logger,'warning',side_effect=RuntimeError('diagnostic logger unavailable')):
            result=workflow.request_role_wakeup(self.pool_path,'COORDINATOR',home=self.home,
                dispatcher=lambda job:called.set() or {'success':False})
            self.assertEqual(result['state'],'SUBMITTED')
            self.assertTrue(called.wait(3)); self.wait_idle()
        self.assertTrue(any(r['stage']=='finish' for r in dispatch.state()['measurement_gaps']))
        dispatch.close()
        self.assertEqual(workflow.request_role_wakeup(self.pool_path,'COORDINATOR',home=self.home)['reason'],'ADAPTER_CLOSED')
        self.assertEqual(self.ledger.snapshot()['event_high_water'],before)

    def test_capability_negotiation_never_guesses_or_retries_after_an_actual_call(self):
        for declaration,supported in ((None,False),({'api_major':2},False),
            ({**dict(native.EVENT_OBSERVER_CAPABILITIES),'api_major':True},False),
            ({**dict(native.EVENT_OBSERVER_CAPABILITIES),'stage_schema_major':True},False),
            ({**dict(native.EVENT_OBSERVER_CAPABILITIES),'api_minor':-1},False),
            ({**dict(native.EVENT_OBSERVER_CAPABILITIES),'api_minor':True},False),
            ({**dict(native.EVENT_OBSERVER_CAPABILITIES),'api_minor':'0'},False),
            ({**dict(native.EVENT_OBSERVER_CAPABILITIES),'api_minor':9},True),
            ({**dict(native.EVENT_OBSERVER_CAPABILITIES),'stages':['request_entered']},False)):
            calls=[]
            def adapter(job,**kwargs): calls.append(kwargs); return {'success':False}
            if declaration is not None: adapter.EVENT_OBSERVER_CAPABILITIES=declaration
            result=workflow.dispatch_event(self.pool_path,'COORDINATOR',home=self.home,
                                           dispatcher=adapter,observer=lambda **kw:None)
            self.assertEqual(result['state'],'DISPATCH_REQUESTED')
            self.assertEqual(len(calls),1)
            self.assertEqual('observer' in calls[0],supported)
        calls=[]
        def broken(job,*,observer): calls.append(job); raise TypeError('after possible claim')
        broken.EVENT_OBSERVER_CAPABILITIES=native.EVENT_OBSERVER_CAPABILITIES
        result=workflow.dispatch_event(self.pool_path,'COORDINATOR',home=self.home,
                                       dispatcher=broken,observer=lambda **kw:None)
        self.assertEqual(len(calls),1)
        self.assertTrue(result['interface_defect'])

    def test_post_commit_uses_return_time_and_skips_duplicate_and_mechanical_events(self):
        created=self.ledger.create_run('second',self.root,self.repo,['T1'],
                                      run_id='second-run',command_id='second-run')
        scope={'pool_path':str(self.pool_path),'home':str(self.home)}
        captured=[]
        def submit(*a,**kw): captured.append(kw); return {'state':'DEFERRED'}
        with mock.patch.object(workflow,'request_role_wakeup',side_effect=submit):
            before=time.time(); workflow.request_business_wakeups(scope,created); after=time.time()
            self.assertEqual(len(captured),len(workflow.ROLES))
            self.assertTrue(all(before<=r['committed_at']['at']<=after for r in captured))
            captured.clear()
            workflow.request_business_wakeups(scope,{**created,'status':'DUPLICATE'})
            self.assertEqual(captured,[])
            _,identity=self.bind('A')
            checkpoint=self.ledger.checkpoint(identity,{'next':'offline safe checkpoint'},command_id='mechanical')
            workflow.request_business_wakeups(scope,checkpoint)
            self.assertEqual(captured,[])
        # Original fixture clock differs from actual post-return capture.
        self.assertNotEqual(self.ledger.events(after=created['event_id']-1)[0]['created_at'],before)

    def test_fifty_durable_events_join_control_and_tick_without_consuming_the_business_cursor(self):
        release=threading.Event(); entered=threading.Event(); self.releases.append(release)
        calls=[]
        def body(job):
            if job=='COORDINATOR-poll':
                calls.append(job); entered.set(); self.assertTrue(release.wait(10))
            return {'success':False}
        first=workflow.request_role_wakeup(self.pool_path,'COORDINATOR',home=self.home,dispatcher=body)
        self.assertTrue(entered.wait(3))
        ids=[]
        for index in range(50):
            result=self.ledger.control_request('continue',{'run_id':'run-one'},
                {'authenticated':True,'current_full':True,'message_id':'burst-'+str(index),'user_id':'offline'},
                command_id='burst-'+str(index))
            committed={'at':time.time(),'monotonic_ns':time.monotonic_ns(),'event_id':result['event_id']}
            ids.append(result['event_id'])
            joined=workflow.request_role_wakeup(self.pool_path,'COORDINATOR',home=self.home,
                dispatcher=body,source='control',committed_at=committed)
            self.assertEqual(joined['state'],'COALESCED')
            self.assertEqual(joined['request_id'],first['request_id'])
        tick=workflow.event_tick(self.pool_path,home=self.home,dispatcher=body,
                                 notification_transport=lambda row:self.fail('no notification expected'))
        self.assertEqual(tick['dispatch']['COORDINATOR']['state'],'COALESCED')
        rows=dispatch.receipts(self.home,self.pool_path,first['request_id'])
        self.assertEqual(len([r for r in rows if r['stage']=='join']),51)
        observed={event for row in rows if row['stage']=='commit' for event in row['source_event_ids'] or []}
        self.assertTrue(set(ids).issubset(observed))
        self.assertEqual(calls,['COORDINATOR-poll'])
        self.assertTrue(set(ids).issubset({e['event_id'] for e in self.ledger.events(limit=1000)}))
        self.assertFalse(any(w['processed_through'] for w in self.ledger.snapshot()['works']))
        release.set(); self.wait_idle()
        self.assertEqual(calls,['COORDINATOR-poll'])

    def test_plugin_releases_collection_lock_before_blocked_notification_and_later_scan(self):
        spec=importlib.util.spec_from_file_location('dispatch_plugin_test',ROOT/'__init__.py')
        module=importlib.util.module_from_spec(spec); sys.modules[spec.name]=module; spec.loader.exec_module(module)
        plugin=module.RuntimePlugin(registry=self.root/'registry.json',metrics_url='',home=self.home,pool_path=self.pool_path)
        from role_status import runtime_module
        runtime_module(); import role_observer
        entered=threading.Event(); release=threading.Event(); later=threading.Event(); self.releases.append(release)
        def drain(*a,**kw):
            entered.set(); self.assertTrue(release.wait(5)); return []
        def collect(*a,**kw):
            if entered.is_set(): later.set()
            return {'outbox':str(self.root/'outbox')}
        from agent import memory_provider
        original_spawn=memory_provider.spawn_context_thread
        threads=[]
        def spawn(*a,**kw):
            thread=original_spawn(*a,**kw); threads.append(thread); return thread
        with mock.patch.object(role_observer,'observation_tick',side_effect=collect), \
             mock.patch.object(role_observer,'drain_monitor_notifications',side_effect=drain), \
             mock.patch.object(memory_provider,'spawn_context_thread',side_effect=spawn), \
             mock.patch.object(module.workflow,'collect_event_tick',return_value=None):
            self.assertTrue(plugin.observation_tick()['scheduled'])
            self.assertTrue(entered.wait(3))
            self.assertTrue(plugin._workflow_tick_lock.acquire(timeout=3)); plugin._workflow_tick_lock.release()
            self.assertTrue(plugin.observation_tick()['scheduled'])
            self.assertTrue(later.wait(3))
            # later is signalled inside collection, before its lock is released.
            self.assertTrue(plugin._workflow_tick_lock.acquire(timeout=3)); plugin._workflow_tick_lock.release()
            release.set()
            self.assertTrue(plugin._workflow_delivery_lock.acquire(timeout=3)); plugin._workflow_delivery_lock.release()
            for thread in threads:
                thread.join(3); self.assertFalse(thread.is_alive())

    def test_registered_native_tool_hooks_capture_terminal_metadata_and_leave_missing_id_incomplete(self):
        spec=importlib.util.spec_from_file_location('activity_plugin_test',ROOT/'__init__.py')
        module=importlib.util.module_from_spec(spec); sys.modules[spec.name]=module; spec.loader.exec_module(module)
        class Context:
            hooks={}
            def get_config(inner,key,default=None):
                return str(self.pool_path) if key=='pool_path' else default
            def register_hook(inner,name,callback): inner.hooks[name]=callback
            def register_tool(inner,*a,**kw): pass
        ctx=Context()
        with _profile_cron_scope(self.home),mock.patch.object(module,'activate_current_native_policy',return_value={'state':'ACTIVATED'}):
            module.register(ctx)
        plugin=ctx.hooks['pre_tool_call'].__self__
        _,identity=self.bind('A')
        scope={**identity,'kind':'business','workflow_version':1,'pool_path':str(self.pool_path)}
        trusted={'role':'A','project':str(self.root),'session_kind':'business_cron'}
        from workflow_activity import read_activity
        with mock.patch.object(plugin,'bind_manual_turn',return_value=trusted), \
                mock.patch.object(plugin,'_frozen_scope',return_value=scope):
            ctx.hooks['pre_llm_call'](session_id='trusted-native')
            self.assertIsNone(ctx.hooks['pre_tool_call'](session_id='trusted-native',
                tool_name='read_file',tool_call_id='actual-native-id',args={'path':'PRIVATE-ARGUMENT'}))
            inflight=read_activity(self.home,scope,now=time.time())
            self.assertTrue(inflight['sources'][0]['complete'])
            self.assertIsNone(inflight['lifecycles'][0]['ended_at'])
            from model_tools import _emit_post_tool_call_hook
            with mock.patch('hermes_cli.lifecycle.has_hook',side_effect=lambda name:name in ctx.hooks), \
                    mock.patch('hermes_cli.lifecycle.invoke_hook',side_effect=lambda name,**kw:ctx.hooks[name](**kw)):
                _emit_post_tool_call_hook(function_name='read_file',function_args={'path':'PRIVATE-ARGUMENT'},
                    result='PRIVATE-RESULT',session_id='trusted-native',tool_call_id='actual-native-id',status='success')
            observed=read_activity(self.home,scope,now=time.time())
            self.assertTrue(observed['sources'][0]['complete'],observed)
            self.assertEqual(observed['lifecycles'][0]['outcome'],'SUCCESS')
            self.assertIsNotNone(observed['lifecycles'][0]['ended_at'])
            raw=(self.home/'runtime/workflow-activity/tools.sqlite3').read_bytes()
            self.assertNotIn(b'PRIVATE-ARGUMENT',raw); self.assertNotIn(b'PRIVATE-RESULT',raw)
            # The real emitter also reports abandoned workers. Its terminal
            # wrapper is not proof of an underlying tool end.
            with mock.patch('hermes_cli.lifecycle.has_hook',side_effect=lambda name:name in ctx.hooks), \
                    mock.patch('hermes_cli.lifecycle.invoke_hook',side_effect=lambda name,**kw:ctx.hooks[name](**kw)):
                for index,(status,error_type) in enumerate((('timeout','tool_timeout'),
                        ('error','thread_missing_result'),('cancelled','tool_interrupted'),
                        ('cancelled','keyboard_interrupt'),('error','tool_abandoned'))):
                    call_id='unverified-native-'+str(index)
                    ctx.hooks['pre_tool_call'](session_id='trusted-native',tool_name='read_file',
                                              tool_call_id=call_id,args={})
                    _emit_post_tool_call_hook(function_name='read_file',function_args={},result='PRIVATE-RESULT',
                        session_id='trusted-native',tool_call_id=call_id,status=status,error_type=error_type)
            observed=read_activity(self.home,scope,now=time.time())
            self.assertFalse(observed['sources'][0]['complete'])
            uncertain=[row for row in observed['lifecycles'] if row['operation_id'].startswith('unverified-native-')]
            self.assertEqual(len(uncertain),5)
            self.assertTrue(all(row['ended_at'] is None for row in uncertain))
            self.assertEqual(sum(not row['end_verified'] for row in observed['terminal_reports']),5)
            ctx.hooks['pre_tool_call'](session_id='trusted-native',tool_name='read_file',
                                      tool_call_id='actual-error',args={})
            ctx.hooks['post_tool_call'](session_id='trusted-native',tool_name='read_file',
                tool_call_id='actual-error',status='error',error_type='FileNotFoundError')
            actual_error=next(row for row in read_activity(self.home,scope,now=time.time())['lifecycles']
                             if row['operation_id']=='actual-error')
            self.assertIsNotNone(actual_error['ended_at'])
            self.assertEqual(actual_error['outcome'],'ERROR')
            self.assertIsNone(ctx.hooks['pre_tool_call'](session_id='trusted-native',tool_name='read_file',args={}))
            self.assertFalse(read_activity(self.home,scope,now=time.time())['sources'][0]['complete'])
            with mock.patch('workflow_activity.record_tool_start',side_effect=OSError('observation unavailable')):
                self.assertIsNone(ctx.hooks['pre_tool_call'](session_id='trusted-native',tool_name='read_file',
                    tool_call_id='missing-write',args={}))

    def test_registered_main_and_aux_hooks_collect_physical_attempts_and_stream_unknown(self):
        spec=importlib.util.spec_from_file_location('model_activity_plugin_test',ROOT/'__init__.py')
        module=importlib.util.module_from_spec(spec); sys.modules[spec.name]=module; spec.loader.exec_module(module)
        class Context:
            def __init__(inner): inner.hooks={}
            def get_config(inner,key,default=None):
                return str(self.pool_path) if key=='pool_path' else default
            def register_hook(inner,name,callback): inner.hooks[name]=callback
            def register_tool(inner,*a,**kw): pass
        ctx=Context()
        with _profile_cron_scope(self.home),mock.patch.object(module,'activate_current_native_policy',return_value={'state':'ACTIVATED'}):
            module.register(ctx)
        plugin=ctx.hooks['pre_api_request'].__self__
        _,identity=self.bind('A')
        scope={**identity,'kind':'business','workflow_version':1,'pool_path':str(self.pool_path)}
        trusted={'role':'A','project':str(self.root),'session_kind':'business_cron'}
        from workflow_activity import read_model_activity
        with mock.patch.object(plugin,'bind_manual_turn',return_value=trusted), \
                mock.patch.object(plugin,'_frozen_scope',return_value=scope), \
                mock.patch.object(module.time,'time',return_value=0), \
                mock.patch.object(plugin,'record_api_time') as old_time, \
                mock.patch.object(plugin,'record_usage') as old_usage:
            ctx.hooks['pre_llm_call'](session_id='trusted-native')
            common={'session_id':'trusted-native','model':'fixture-model','api_request_id':'main',
                    'physical_attempt_id':'main-one','started_at':100,'attempt_started_at':100}
            ctx.hooks['pre_api_request'](**common,request={'body':{'PRIVATE-PROMPT':'PRIVATE-PAYLOAD'}})
            ctx.hooks['api_request_error'](**common,ended_at=150,retry_pending=True,error='PRIVATE-ERROR')
            retry={**common,'physical_attempt_id':'main-two','attempt_started_at':160}
            ctx.hooks['pre_api_request'](**retry)
            inflight=read_model_activity(self.home,scope,1000)
            self.assertTrue(inflight['sources'][0]['complete'])
            self.assertIsNone(inflight['lifecycles'][0]['ended_at'])
            ctx.hooks['post_api_request'](**retry,ended_at=200)
            auxiliary={**common,'api_request_id':'aux','physical_attempt_id':'aux-one',
                       'attempt_started_at':210,'aux_task':'compression'}
            ctx.hooks['pre_auxiliary_call'](**auxiliary)
            opened=read_model_activity(self.home,scope,1000)
            self.assertTrue(opened['sources'][0]['complete'])
            self.assertIsNone(next(row for row in opened['lifecycles'] if row['operation_id']=='aux-one')['ended_at'])
            self.assertEqual(next(row for row in opened['lifecycles'] if row['operation_id']=='aux-one')['kind'],'compaction')
            ctx.hooks['post_auxiliary_call'](**auxiliary,ended_at=220,streaming=False)
            complete=read_model_activity(self.home,scope,1000)
            self.assertTrue(complete['sources'][0]['complete'],complete)
            self.assertEqual({row['operation_id'] for row in complete['lifecycles']},
                             {'main-one','main-two','aux-one'})
            streamed={**auxiliary,'physical_attempt_id':'stream-handle','attempt_started_at':230}
            ctx.hooks['pre_auxiliary_call'](**streamed)
            ctx.hooks['post_auxiliary_call'](**streamed,ended_at=240,streaming=True)
            incomplete=read_model_activity(self.home,scope,1000)
            self.assertFalse(incomplete['sources'][0]['complete'])
            self.assertIsNone(next(row for row in incomplete['lifecycles'] if row['operation_id']=='stream-handle')['ended_at'])
            self.assertEqual(old_time.call_count,4)
            self.assertEqual(old_usage.call_count,8)
            raw=(self.home/'runtime/workflow-activity/model_requests/attempts.sqlite3').read_bytes()
            self.assertNotIn(b'PRIVATE-PROMPT',raw); self.assertNotIn(b'PRIVATE-PAYLOAD',raw)
            self.assertNotIn(b'PRIVATE-ERROR',raw)
            # A collector database failure must retain its own gap and still
            # reach the original independent time and usage paths.
            with mock.patch('workflow_activity._model_database',side_effect=sqlite3.OperationalError('locked fixture')):
                self.assertIsNone(ctx.hooks['pre_api_request'](**{**common,'physical_attempt_id':'lost'}))
            self.assertEqual(old_time.call_count,5)
            self.assertEqual(old_usage.call_count,9)
            self.assertFalse(read_model_activity(self.home,scope,1000)['sources'][0]['complete'])
