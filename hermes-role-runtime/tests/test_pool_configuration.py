import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
spec=importlib.util.spec_from_file_location('pool_configuration',ROOT/'scripts/configure_role_pool.py')
configuration=importlib.util.module_from_spec(spec);spec.loader.exec_module(configuration)
from pool_entry import report_project
from requirement_runtime import list_requirements,resolve_role


class PoolConfigurationTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.home=Path(temporary.name);self.catalog=self.home/'prd/README.md';self.catalog.parent.mkdir()
        self.catalog.write_text('| 需求包 | 说明 | 入口 |\n|---|---|---|\n| [原始采集](raw/README.md) | 目标 | TODO |\n\n[旧运行根](legacy/README.md)')
        for name in ('raw','legacy'):
            folder=self.catalog.parent/name;folder.mkdir();(folder/'README.md').write_text(name);(folder/'TODO.md').write_text('未完成任务')
        roles={role:{'skill':'complex-task-coordinator' if role=='COORDINATOR' else 'complex-task-tester' if role=='TESTER' else 'complex-task-executor',
            'chat_id':role,'poll_job_id':'poll-'+role,'report_job_id':'report-'+role,
            'session_file':'sessions/'+role+'.md','worktree':'/old-worktree',
            'rebind':{'dispatched_task':'old-task'},'coordinated_projects':[{'project_root':'/old'}]}
            for role in configuration.ROLES}
        for name,selected in (('legacy',['COORDINATOR']),('raw',['A','B','C','TESTER'])):
            root=self.catalog.parent/name
            (root/'ROLE-BINDINGS.json').write_text(json.dumps({'schema_version':1,'control':'ACTIVE',
                'project_root':str(root),'roles':{role:roles[role] for role in selected}}))
        self.registry=self.home/'registry.json';self.registry.write_text(json.dumps({'projects':[
            {'project_root':str(self.catalog.parent/name)} for name in ('legacy','raw')]}))

    def test_catalog_anchor_keeps_legacy_live_roots_but_not_old_business_metadata(self):
        pool=configuration.build_pool(self.catalog,self.registry)
        self.assertEqual(list(pool['requirements']),['raw'])
        self.assertEqual(pool['current']['role_roots']['COORDINATOR'],str(self.catalog.parent/'legacy'))
        for role in configuration.ROLES:
            self.assertNotIn('rebind',pool['roles'][role]);self.assertNotIn('coordinated_projects',pool['roles'][role])
            self.assertNotIn('worktree',pool['roles'][role])
        # Bootstrap chooses a readable current ID without performing a business switch.
        pool['current']['requirement_id']='raw'
        pool_path=self.home/'pool.json';pool_path.write_text(json.dumps(pool))
        self.assertEqual(len(list_requirements(pool_path)['requirements']),1)
        self.assertEqual(resolve_role(pool_path,'A')['project_root'],str(self.catalog.parent/'raw'))

    def test_generic_jobs_do_not_embed_the_old_requirement(self):
        pool=configuration.build_pool(self.catalog,self.registry)
        updates,wrappers=configuration.updates_for(pool,self.home/'pool.json',ROOT)
        self.assertEqual(len(updates),10)
        for role in configuration.ROLES:
            update=updates['poll-'+role]
            self.assertEqual(update['workdir'],str(self.catalog.parent))
            self.assertNotIn('old-task',update['prompt']);self.assertNotIn('/old-worktree',update['prompt'])
            self.assertEqual(update['role_pool']['kind'],'business')
            self.assertEqual(updates['report-'+role]['role_pool']['kind'],'report')
            self.assertEqual(wrappers['poll-'+role],('poll',role))

    def test_report_uses_immutable_old_scope_and_rejects_partial_or_foreign_environment(self):
        pool=self.home/'pool.json'
        values={'HERMES_ROLE_POOL_PATH':str(pool),'HERMES_ROLE_POOL_GENERATION':'2',
                'HERMES_ROLE_REQUIREMENT_ID':'old','HERMES_ROLE_PROJECT_ROOT':'/old/root','HERMES_ROLE_NAME':'A'}
        with patch.dict(os.environ,values):
            self.assertEqual(report_project(pool,'A'),'/old/root')
            with self.assertRaises(ValueError):report_project(pool,'B')
            with self.assertRaises(ValueError):report_project(self.home/'foreign.json','A')
            with patch.dict(os.environ,HERMES_ROLE_POOL_GENERATION=''):
                with self.assertRaises(ValueError):report_project(pool,'A')

    def test_drain_cannot_claim_or_submit_new_independent_work(self):
        import operation_jobs,task_claim
        with patch.dict(os.environ,HERMES_ROLE_POOL_DRAIN_ONLY='1'):
            with self.assertRaisesRegex(ValueError,'不领取新任务'):task_claim.claim_task(self.home,'A',{},'missing')
            with self.assertRaisesRegex(ValueError,'不能提交新长操作'):operation_jobs.start_job(self.home,'COORDINATOR',{})
        self.assertFalse((self.home/'inbox').exists())


if __name__=='__main__':unittest.main()
