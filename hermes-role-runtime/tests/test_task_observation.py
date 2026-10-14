import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import task_observation as sampler


class TaskObservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.spec = {'observation_id':'spray-live','task':'TASK-11S','run_id':'scene-4acf689f','attempt':1,
                     'notify_roles':['COORDINATOR','C','TESTER'],'backend_url':'http://192.168.2.134:8180',
                     'remote_alias':'processing-machine','container':'rosbag-scene-proxy','storage_path':'/mnt/nas_backup',
                     'business_timezone':'Asia/Shanghai','heartbeat_stale_seconds':180,'min_free_bytes':100}
        self.moment = datetime(2026,10,14,2,0,0,tzinfo=timezone.utc)
        self.pipeline = {'pipeline_run_id':self.spec['run_id'],'attempt':1,'source_id':'source1','status':'SCENE_RUNNING',
                         'heartbeat_at':'2026-10-14T09:59:30','steps':[{'step_name':'INFER','attempt':1,'status':'RUNNING'}]}
        self.scene = {**self.pipeline,'effective_config_hash':'hash1'}
        self.remote = {'observed_at':self.moment.isoformat(),'errors':{},'proxy_health':{'status':'ok','active_runs':[{'pipeline_run_id':self.spec['run_id']}]},
                       'container':{'id':'container1','state':{'Status':'running','Running':True}},
                       'process':{'pid':1,'start_ticks':'42','boot_id':'boot1','cpu_ticks':100,
                                  'files':{'checkpoint.json':{'mtime_ns':10,'content':{'steps':{'INFER':{'status':'RUNNING'}}}}}},
                       'disk':{'free':1000,'used':500,'total':1500}}
    def tearDown(self):
        self.tmp.cleanup()
    def get(self,url,timeout):
        self.assertLessEqual(timeout,6)
        return copy.deepcopy(self.pipeline if '/pipeline-runs/' in url else self.scene)
    def read(self,now=None):
        with patch.object(sampler,'_get_json',side_effect=self.get),patch.object(sampler,'_remote',return_value=copy.deepcopy(self.remote)):
            return sampler.sample(self.spec,now or self.moment)
    def install(self):
        path=self.root/'inbox/control/runtime/task-observation-spec.json';path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(self.spec));return path
    def registered(self):
        with patch.object(sampler,'_get_json',side_effect=self.get),patch.object(sampler,'_remote',return_value=copy.deepcopy(self.remote)):
            with patch.object(sampler,'datetime') as clock:
                clock.now.return_value=self.moment;clock.fromisoformat=datetime.fromisoformat
                return sampler.sample_registered(self.root)
    def test_no_spec_has_zero_io_or_network(self):
        with patch.object(sampler,'_get_json') as request,patch.object(sampler,'_remote') as remote:
            self.assertIsNone(sampler.sample_registered(self.root))
            request.assert_not_called();remote.assert_not_called()
            self.assertEqual(list(self.root.iterdir()),[])
    def test_naive_heartbeat_uses_explicit_business_timezone(self):
        out=self.read();self.assertEqual(out['heartbeat']['age_seconds'],30)
        self.assertEqual(out['heartbeat']['timezone_basis'],'configured_business_timezone')
        self.assertEqual(out['heartbeat']['interpreted_at'],'2026-10-14T09:59:30+08:00')
    def test_explicit_offset_is_not_reinterpreted(self):
        self.pipeline['heartbeat_at']='2026-10-14T01:59:30Z'
        self.assertEqual(self.read()['heartbeat']['age_seconds'],30)
        self.assertEqual(self.read()['heartbeat']['timezone_basis'],'explicit_offset')
    def test_normal_heartbeat_cpu_and_mtime_do_not_change_business_fingerprint(self):
        one=self.read();self.pipeline['heartbeat_at']='2026-10-14T09:59:45'
        self.remote['process']['cpu_ticks']=500;self.remote['process']['files']['checkpoint.json']['mtime_ns']=20
        self.remote['disk']['free']=900
        self.assertEqual(one['semantic_fingerprint'],self.read()['semantic_fingerprint'])
    def test_no_change_refreshes_latest_but_not_notice_revision_or_file(self):
        self.install();first=self.registered()
        notice=self.root/'inbox/control/runtime/task-notices/spray-live.json';original=notice.read_bytes()
        self.moment+=timedelta(seconds=60);self.pipeline['heartbeat_at']='2026-10-14T10:00:30'
        second=self.registered()
        self.assertEqual(first['revision'],1);self.assertEqual(second['revision'],1)
        self.assertEqual(original,notice.read_bytes())
        self.assertNotEqual(first['observed_at'],second['observed_at'])
    def test_heartbeat_threshold_and_recovery_emit_two_changes(self):
        self.install();healthy=self.registered();self.moment+=timedelta(seconds=200)
        stale=self.registered();self.assertEqual(stale['health']['heartbeat'],'stale')
        self.pipeline['heartbeat_at']='2026-10-14T10:03:10'
        restored=self.registered()
        self.assertEqual((healthy['revision'],stale['revision'],restored['revision']),(1,2,3))
    def test_timeout_unknown_is_once_and_recovery_notifies(self):
        self.install()
        with patch.object(sampler,'_get_json',side_effect=TimeoutError('request deadline')), \
             patch.object(sampler,'_remote',side_effect=subprocess.TimeoutExpired('ssh',25)):
            one=sampler.sample_registered(self.root);two=sampler.sample_registered(self.root)
        self.assertEqual(one['revision'],two['revision'])
        self.assertIn('remote',one['semantic']['unknown'])
        self.assertEqual(self.registered()['revision'],one['revision']+1)
    def test_attempt_and_checkpoint_step_transition_notify(self):
        self.install();one=self.registered()
        self.pipeline['attempt']=2;self.pipeline['steps'][0]['attempt']=2
        self.scene['attempt']=2;self.scene['steps'][0]['attempt']=2
        two=self.registered();self.assertEqual(two['revision'],one['revision']+1)
        self.assertEqual(two['attempt'],2);self.assertEqual(two['configured_attempt'],1)
        self.remote['process']['files']['checkpoint.json']['content']['steps']['INFER']['status']='DONE'
        self.assertEqual(self.registered()['revision'],two['revision']+1)
    def test_low_disk_and_backend_disagreement_are_not_hidden(self):
        self.remote['disk']['free']=50;self.scene['status']='SCENE_DONE'
        out=self.read();self.assertEqual(out['health']['disk'],'low')
        self.assertIn('backend_consistency',out['semantic']['unknown'])
    def test_proxy_losing_active_run_is_a_health_change(self):
        first=self.read();self.remote['proxy_health']['active_runs']=[]
        second=self.read();self.assertEqual(second['health']['proxy_run'],'absent')
        self.assertNotEqual(first['semantic_fingerprint'],second['semantic_fingerprint'])
    def test_notice_written_before_latest_crash_is_not_renotified(self):
        self.install();first=self.registered()
        path=self.root/'inbox/control/runtime/task-observations/spray-live/latest.json';path.unlink()
        self.assertEqual(self.registered()['revision'],first['revision'])
    def test_invalid_spec_and_recovery_have_explicit_configuration_notice(self):
        path=self.install();path.write_text('{}')
        bad=sampler.sample_registered(self.root)
        self.assertEqual(bad['observation_id'],'task-observation-config')
        self.assertEqual(sampler.sample_registered(self.root)['revision'],bad['revision'])
        path.write_text(json.dumps(self.spec));self.registered()
        config=json.loads((self.root/'inbox/control/runtime/task-notices/task-observation-config.json').read_text())
        self.assertEqual(config['semantic'],{'configuration':'valid'})
        self.assertEqual(config['revision'],bad['revision']+1)
    def test_unsafe_input_rejected_before_network(self):
        for key,value in [('remote_alias','-oProxyCommand=evil'),('container','x;evil'),('run_id','../run'),
                          ('storage_path','/mnt/../etc'),('backend_url','http://u:p@192.168.2.1:80')]:
            with self.subTest(key=key),patch.object(sampler,'_get_json') as request:
                with self.assertRaises(ValueError):sampler.sample({**self.spec,key:value})
                request.assert_not_called()
    def test_remote_command_is_fixed_argv_and_timeout_is_bounded(self):
        with patch.object(sampler.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'{}','')) as run:
            sampler._remote(self.spec,25)
        args=run.call_args.args[0];self.assertEqual(args[-3:],['processing-machine','python3','-'])
        self.assertTrue(run.call_args.kwargs['check']);self.assertEqual(run.call_args.kwargs['timeout'],25)
        compile(run.call_args.kwargs['input'],'<remote>','exec')
    def test_entire_http_get_has_hard_wall_deadline(self):
        with patch.object(sampler.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'{}','')) as run:
            self.assertEqual(sampler._get_json('http://192.168.2.134:8180/api/v1/pipeline-runs/scene-a',6),{})
        self.assertEqual(run.call_args.kwargs['timeout'],6)
        self.assertTrue(run.call_args.kwargs['check'])
        self.assertEqual(run.call_args.args[0][1],'-c')
        compile(run.call_args.args[0][2],'<get>','exec')


if __name__=='__main__':unittest.main()
