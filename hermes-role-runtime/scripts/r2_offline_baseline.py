#!/usr/bin/env python3
"""Reproducible synthetic R2 measurement; never a production latency benchmark.

Runs the actual tick, stores, claims, running set, synchronous event entry and
run_one_job. Only its business body is replaced by a controlled Event wait;
external worker launch, hooks, notification drains and maintenance are disabled.
"""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import resource
import sys
import threading
import time
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
CORE=Path('/home/wang/.hermes/hermes-agent')
for directory in (ROOT,ROOT/'tests',CORE): sys.path.insert(0,str(directory))
from test_workflow_dispatch import WakeupTests
import workflow_runtime as workflow
import workflow_dispatch as dispatch
from cron import jobs,scheduler,executions
from cron.scheduler_provider import _profile_cron_scope
from hermes_constants import get_hermes_home
from tools import cronjob_tools as native


def dump(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+'\n')


def main(output):
    output.mkdir(parents=True,exist_ok=False)
    (output/'runner-executed.py').write_bytes(Path(__file__).read_bytes())
    fixture=WakeupTests(); fixture.setUp()
    release=threading.Event(); fixture.releases.append(release)
    timeline=[]; timeline_lock=threading.Lock(); peak={'threads':0,'adapter_calls':0}
    source_claim=jobs.claim_job_for_fire
    started={}; submissions=[]; ordinary=[]; profiles=[]; futures=[]
    cpu_start=resource.getrusage(resource.RUSAGE_SELF); wall_start=time.monotonic_ns()
    def event(kind,**fields):
        row={'stage':kind,'at':time.time(),'monotonic_ns':time.monotonic_ns(),**fields}
        with timeline_lock: timeline.append(row)
        return row
    def resources(label):
        usage=resource.getrusage(resource.RUSAGE_SELF)
        count=dispatch.state()['active']; threads=threading.active_count()
        peak['threads']=max(peak['threads'],threads); peak['adapter_calls']=max(peak['adapter_calls'],count)
        event('resources',label=label,adapter_calls=count,threads=threads,
              max_rss_kib=usage.ru_maxrss,user_cpu_seconds=usage.ru_utime,system_cpu_seconds=usage.ru_stime)
    def claim(job_id,**kw):
        result=source_claim(job_id,**kw)
        event('actual_claim_returned',home=str(get_hermes_home()),job_id=job_id,
              outcome='ACQUIRED' if result else 'NOT_ACQUIRED',path='manual_event' if kw.get('manual') else 'ticker')
        return result
    def body(job,**kw):
        home=str(get_hermes_home()); key=(home,job['id'])
        execution=executions.mark_execution_running(job['execution_id'])
        event('synthetic_body_entered',home=home,job_id=job['id'],execution_id=job['execution_id'],
              actual_execution_status=(execution or {}).get('status'),
              authority='synthetic_body_not_business_execution')
        started.setdefault(key,threading.Event()).set()
        if job.get('name')=='ordinary-second':
            pass
        elif not release.wait(15):
            raise TimeoutError('offline harness release missing')
        owner=job['fire_claim']['by']
        jobs.mark_job_run(job['id'],True,expected_fire_owner=owner)
        executions.finish_execution(job['execution_id'],success=True,delivery_outcome='not_applicable')
        event('synthetic_body_finished',home=home,job_id=job['id'])
        return True
    def wait_body(home,job_id):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            with timeline_lock:
                found=any(row['stage']=='synthetic_body_entered' and row['home']==str(home)
                          and row['job_id']==job_id for row in timeline)
            if found: return
            threading.Event().wait(.005)
        raise AssertionError('native body did not enter')
    try:
        profiles=[fixture.pool('baseline-profile-'+str(i)) for i in range(6)]
        # Replace minimal fixture job rows with fully normalized native jobs.
        role_jobs={}
        for pool,home in profiles:
            with _profile_cron_scope(home):
                (home/'cron/jobs.json').write_text('{"jobs":[]}')
                job=jobs.create_job(prompt='OFFLINE BODY ONLY',schedule='every 5m',
                                     name='role-coordinator',deliver='local')
                role_jobs[str(home)]=job
            value=json.loads(pool.read_text()); value['roles']['COORDINATOR']['poll_job_id']=job['id']
            pool.write_text(json.dumps(value))
        tick_home=profiles[0][1]
        with _profile_cron_scope(tick_home):
            for name in ('ordinary-first','ordinary-second'):
                job=jobs.create_job(prompt='OFFLINE BODY ONLY',schedule='every 5m',name=name,deliver='local')
                jobs.update_job(job['id'],{'next_run_at':scheduler._hermes_now().isoformat()})
                ordinary.append(job)
        with contextlib.ExitStack() as patches:
            for name,value in (('_launch_external_cron_worker',False),('_should_yield_tick_to_fresh_gateway',None),
                    ('_maybe_reap_dead_owners',None),('_maybe_run_worktree_maintenance',None),
                    ('_sweep_stale_inflight_for_tick',None),('_sweep_mcp_orphans',None),
                    ('_sweep_mcp_orphans_when_all_done',None),('_resolve_max_parallel_workers',1)):
                patches.enter_context(mock.patch.object(scheduler,name,return_value=value))
            patches.enter_context(mock.patch('hermes_cli.lifecycle.invoke_hook',return_value=[]))
            patches.enter_context(mock.patch('cron.bot_chat_delivery.drain_in_background',return_value=None))
            patches.enter_context(mock.patch.object(scheduler,'_run_one_job_body',side_effect=body))
            patches.enter_context(mock.patch.object(scheduler,'claim_job_for_fire',side_effect=claim))
            patches.enter_context(mock.patch.object(native,'claim_job_for_fire',side_effect=claim))
            resources('before')
            event('ticker_submit_begin',home=str(tick_home))
            with _profile_cron_scope(tick_home):
                tick_count=scheduler.tick(verbose=False,sync=False)
                with scheduler._running_lock:
                    futures=list(scheduler._running_futures.values())
            event('ticker_submit_returned',home=str(tick_home),count=tick_count)
            wait_body(tick_home,ordinary[0]['id'])
            for pool,home in profiles[:5]:
                committed=workflow.open_ledger(pool,readonly=False).control_request('continue',{'run_id':'run-one'},
                    {'authenticated':True,'current_full':True,'message_id':'offline-commit','user_id':'offline'},
                    command_id='offline-commit')
                if committed['status']!='COMMITTED': raise AssertionError(committed)
                marker={'at':time.time(),'monotonic_ns':time.monotonic_ns(),'event_id':committed['event_id']}
                event('adapter_submit_begin',home=str(home))
                result=workflow.request_role_wakeup(pool,'COORDINATOR',home=home,
                    source='offline_baseline',committed_at=marker)
                submissions.append({'home':str(home),'pool_path':str(pool),'result':result})
                event('adapter_submit_returned',home=str(home),result=result)
                if result['state']!='SUBMITTED': raise AssertionError(result)
                wait_body(home,role_jobs[str(home)]['id'])
            full=workflow.request_role_wakeup(profiles[5][0],'COORDINATOR',home=profiles[5][1],source='offline_capacity')
            event('capacity_full_result',result=full)
            joined_commit=workflow.open_ledger(profiles[0][0],readonly=False).control_request(
                'continue',{'run_id':'run-one'},
                {'authenticated':True,'current_full':True,'message_id':'offline-join','user_id':'offline'},
                command_id='offline-join')
            joined_marker={'at':time.time(),'monotonic_ns':time.monotonic_ns(),'event_id':joined_commit['event_id']}
            joined=workflow.request_role_wakeup(profiles[0][0],'COORDINATOR',home=tick_home,
                source='control',committed_at=joined_marker)
            event('coalesced_result',result=joined)
            # A ticker attempt for the same HOME/job uses the native exclusion,
            # independently of coalescing; no second model/body is started.
            with _profile_cron_scope(tick_home):
                executor=scheduler._get_parallel_pool(1)
                duplicate=scheduler._submit_with_guard(role_jobs[str(tick_home)],executor,
                    lambda job:scheduler._process_due_job(job,None,None,False))
            event('same_home_ticker_exclusion',outcome='REJECTED' if duplicate is None else 'SUBMITTED')
            resources('synthetic_saturation')
            event('synthetic_release',note='harness-controlled Event wait; not workload/service time')
            release.set()
            for future in futures: future.result(timeout=8)
            fixture.wait_idle()
            resources('after')
        role_receipts=[]
        for pool,home in profiles:
            role_receipts.extend(dispatch.receipts(home,pool))
        dump(output/'dispatch-receipts.json',role_receipts)
        dump(output/'timeline.json',timeline)
        dump(output/'submissions.json',submissions)
        failures=[r for r in role_receipts if r['stage']=='finish' and r['outcome']!='DISPATCH_REQUESTED']
        metrics=[]
        for submission in submissions:
            request=submission['result']['request_id']; rows=[r for r in role_receipts if r['request_id']==request]
            stages={r['stage']:r for r in rows}
            def delta(before,after):
                return ((stages[after]['monotonic_ns']-stages[before]['monotonic_ns'])/1e6
                        if before in stages and after in stages else None)
            metrics.append({'request_id':request,'home':submission['home'],
                'prepared_to_entered_ms':delta('prepared','request_entered'),
                'entered_to_claim_ms':delta('request_entered','claim_decided'),
                'claim_to_finish_ms':delta('claim_decided','finish'),
                'commit_to_join_ms':delta('commit','join'),
                'missing_stages':sorted(dispatch.REQUIRED_STAGES-set(stages))})
        tick_start=next(r for r in timeline if r['stage']=='ticker_submit_begin')
        ordinary_metrics=[]
        for job in ordinary:
            rows=[r for r in timeline if r.get('job_id')==job['id']]
            start=next(r for r in rows if r['stage']=='synthetic_body_entered')
            end=next(r for r in rows if r['stage']=='synthetic_body_finished')
            ordinary_metrics.append({'job_id':job['id'],'name':job['name'],
                'tick_submit_to_synthetic_body_ms':(start['monotonic_ns']-tick_start['monotonic_ns'])/1e6,
                'synthetic_body_ms':(end['monotonic_ns']-start['monotonic_ns'])/1e6})
        cpu_end=resource.getrusage(resource.RUSAGE_SELF)
        summary={'schema_version':1,'classification':'OFFLINE_SYNTHETIC_FUNCTIONAL_BASELINE',
            'performance_verdict':'UNVERIFIED_TARGET_NOT_FROZEN',
            'production_latency_inference':False,'provider_calls':0,'messages':0,
            'synthetic_wait':'Event held until five adapter bodies enter; durations include intentional wait',
            'native_paths':['tick(sync=False) -> original per-HOME pool(1) -> real claim -> run_one_job',
                            'bounded adapter(5) -> execute_job_for_event -> real claim/running set -> run_one_job'],
            'counts':{'ordinary_cron_submitted':tick_count,'ordinary_cron_finished':len(ordinary_metrics),
                      'role_submitted':len(submissions),'role_native_finishes':len([r for r in role_receipts if r['stage']=='native_return']),
                      'role_finish_failures':len(failures),'capacity_deferred':int(full.get('reason')=='CAPACITY_FULL'),
                      'coalesced':int(joined['state']=='COALESCED'),'same_home_ticker_excluded':int(duplicate is None),
                      'measurement_gaps':len(dispatch.state()['measurement_gaps'])},
            'resources':{'peak':peak,'ticker_workers':1,'ticker_queue_has_waiting_ordinary_job':True,
                'cpu_saturated':'UNKNOWN_NOT_MEASURED','cpu_user_delta':cpu_end.ru_utime-cpu_start.ru_utime,
                'cpu_system_delta':cpu_end.ru_stime-cpu_start.ru_stime,
                'wall_ms':(time.monotonic_ns()-wall_start)/1e6},
            'roles':metrics,'ordinary_cron':ordinary_metrics,
            'unmeasured_business_stages':{key:'UNKNOWN' for key in ('agent_start','input_ready',
                'first_provider_request','business_execution_bound','result_visible','business_end')},
            'manifest':dispatch.observer_binding()[1],
            'runner_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'source_sha256':{str(path):hashlib.sha256(path.read_bytes()).hexdigest() for path in
                             (ROOT/'workflow_dispatch.py',ROOT/'workflow_runtime.py',ROOT/'__init__.py',
                              CORE/'tools/cronjob_tools.py',CORE/'cron/scheduler.py',CORE/'cron/scheduler_tick.py')},
            'raw_receipts_sha256':hashlib.sha256((output/'dispatch-receipts.json').read_bytes()).hexdigest()}
        dump(output/'summary.json',summary)
        print(json.dumps({'output':str(output),'counts':summary['counts'],'performance_verdict':summary['performance_verdict']}))
    finally:
        release.set()
        for _,home in profiles:
            scheduler.discard_parallel_pools([str(home.resolve())])
        fixture.tearDown()


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('output',type=Path)
    main(parser.parse_args().output.resolve())
