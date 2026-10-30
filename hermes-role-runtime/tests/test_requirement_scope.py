import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from requirement_scope import task_scope,task_allowed,require_task_scope
from context_input import build_context
from task_claim import claim_task


class RequirementScopeTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.container=Path(temporary.name);self.root=self.container/'legacy';self.root.mkdir()
        self.package=self.container/'ui';self.package.mkdir()
        (self.package/'TODO.md').write_text('本包任务编号定位：`TASK-16`、`TASK-16C`、`TASK-16C-A`。\n')
        self.binding={'schema_version':1,'project_root':str(self.root),'control':'ACTIVE',
            'roles':{role:{'session_file':'sessions/'+role+'.md'} for role in ('COORDINATOR','A')},
            'requirement_authorization':{'requirement_id':'ui','requirement_root':str(self.package),'runtime_root':str(self.root)}}
        self.save('ROLE-BINDINGS.json',self.binding)
        (self.root/'TODO.md').write_text('| 任务 | 状态 | 内容 |\n|---|---|---|\n| TASK-16C | READY | UI |\n| TASK-16B | READY | 基础 |\n| TASK-12S | READY | 水雾 |\n')

    def save(self,relative,value):
        path=self.root/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value))

    def test_shared_root_membership_is_exact_and_not_task_prefix(self):
        scope=task_scope(self.root)
        self.assertTrue(task_allowed(scope,'TASK-16'))
        self.assertTrue(task_allowed(scope,'TASK-16C-A'))
        for task in ('TASK-16B','TASK-16C-A-OTHER','TASK-12S',None):
            self.assertFalse(task_allowed(scope,task))
            with self.assertRaises(ValueError):require_task_scope(self.root,task)

    def test_missing_compatibility_task_list_cannot_admit_arbitrary_old_work(self):
        (self.package/'TODO.md').write_text('任务尚未定位')
        self.assertEqual(task_scope(self.root)['task_ids'],[])
        with self.assertRaises(ValueError):require_task_scope(self.root,'TASK-16')

    def test_context_parks_other_packages_checkpoint_and_actions_without_changing_disk(self):
        self.save('inbox/control/runtime/checkpoints/COORDINATOR.json',{'task':'TASK-12S','attempt':'old','next':'继续水雾'})
        self.save('inbox/control/runtime/dispatch-index/COORDINATOR.json',{'entries':[
            {'task':'TASK-16C','attempt':'ui','source':'inbox/ui.md'},
            {'task':'TASK-12S','attempt':'spray','source':'inbox/spray.md'}]})
        before={str(path):path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        with patch('context_input.pending_continuations',return_value=[{'task':'TASK-12S','next':'不应启动'},
                                                                    {'task':'TASK-16C','next':'当前包'}]):
            result=build_context(self.root,'COORDINATOR')
        self.assertEqual(result['checkpoint'],{})
        self.assertEqual([x['task'] for x in result['dispatch_index']['entries']],['TASK-16C'])
        self.assertEqual([x['task'] for x in result['continuations']],['TASK-16C'])
        self.assertEqual(result['requirement_scope']['excluded_dispatches'],1)
        self.assertEqual(result['requirement_scope']['excluded_continuations'],1)
        self.assertTrue(all(x['task']!='TASK-12S' and x['task']!='TASK-16B' for x in result['related_tasks']))
        self.assertEqual(before,{str(path):path.read_bytes() for path in self.root.rglob('*') if path.is_file()})

    def test_task_claim_cannot_recover_old_package_dispatch_as_current_work(self):
        with self.assertRaisesRegex(ValueError,'不在当前所选需求'):
            claim_task(self.root,'A',{'task':'TASK-12S','attempt':'1'},'unused')
        self.assertFalse((self.root/'inbox').exists())

    def test_independent_root_and_legacy_bootstrap_keep_existing_scope(self):
        self.binding['requirement_authorization']['requirement_root']=str(self.root)
        self.save('ROLE-BINDINGS.json',self.binding)
        self.assertIsNone(task_scope(self.root));self.assertTrue(task_allowed(None,'TASK-any'))
        self.binding.pop('requirement_authorization');self.save('ROLE-BINDINGS.json',self.binding)
        self.assertIsNone(task_scope(self.root))

    def test_stopped_other_packages_operation_does_not_occupy_current_operation_slot(self):
        from operation_jobs import start_job
        plan={'operation_id':'ui-validation','task':'TASK-16C','attempt':'1','cwd':str(self.root),
              'steps':[{'argv':['fake-readonly-test']}]}
        with patch('operation_jobs.operation_states',return_value=[{'operation_id':'old-spray','task':'TASK-12S','status':'UNKNOWN'}]), \
             patch('operation_jobs.subprocess.run',return_value=SimpleNamespace(returncode=0)) as submit:
            self.assertTrue(start_job(self.root,'A',plan)['started'])
            submit.assert_called_once()
        with patch('operation_jobs.operation_states',return_value=[{'operation_id':'current-ui','task':'TASK-16C','status':'UNKNOWN'}]), \
             patch('operation_jobs.subprocess.run') as submit:
            with self.assertRaisesRegex(ValueError,'禁止启动第二项'):
                start_job(self.root,'A',{**plan,'operation_id':'ui-validation-second'})
            submit.assert_not_called()


if __name__=='__main__':unittest.main()
