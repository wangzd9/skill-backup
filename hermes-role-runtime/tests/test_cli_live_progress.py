"""隔离真实账本：CLI应读取指定ACTIVE池，不回退旧派单或改变业务。"""
from contextlib import redirect_stdout
import io
import json
import os
from unittest import mock

from test_final_git_delivery import FinalGitDeliveryTests as _Base
import runtime_tools


class CliLiveProgressTests(_Base):
    def query(self, *, project=None, pool=True, environment=None):
        args=['runtime_tools.py','live-progress','--project',str(project or self.root),
              '--role','COORDINATOR']
        if pool:
            args += ['--pool',str(self.root/'isolated.json')]
        output=io.StringIO()
        with mock.patch.dict(os.environ,environment or {},clear=True), \
                mock.patch('sys.argv',args), redirect_stdout(output):
            code=runtime_tools.main()
        return code,json.loads(output.getvalue())

    def test_cli_uses_current_delivery_and_preserves_business_ledger(self):
        request=self.request()
        before=self.path.read_bytes()
        with mock.patch('progress_query.live_progress_report',side_effect=AssertionError('legacy forbidden')):
            code,result=self.query()
        self.assertEqual(0,code,result)
        self.assertEqual('PENDING',result['final_deliveries'][0]['state'])
        self.assertEqual(request['request_id'],result['final_deliveries'][0]['request_id'])
        self.assertIn('最终交付接续',result['markdown'])
        self.assertEqual(before,self.path.read_bytes())

    def test_environment_pool_uses_same_active_entry(self):
        self.request()
        code,result=self.query(pool=False,environment={
            'HERMES_ROLE_POOL_PATH':str(self.root/'isolated.json')})
        self.assertEqual(0,code,result)
        self.assertEqual('PENDING',result['final_deliveries'][0]['state'])

    def test_wrong_project_and_inactive_pool_never_fall_back_to_legacy(self):
        with mock.patch('progress_query.live_progress_report',side_effect=AssertionError('legacy forbidden')):
            code,result=self.query(project=self.repo)
            self.assertEqual(1,code)
            self.assertIn('PROJECT_SCOPE_MISMATCH',result['error'])
            from business_ledger import BusinessLedger
            inactive=BusinessLedger(self.root/'inactive.runtime/business.sqlite3')
            self.assertCommitted(inactive.set_mode('STAGED',command_id='cli-stage'))
            code,result=self.query(pool=False,environment={
                'HERMES_ROLE_POOL_PATH':str(self.root/'inactive.json')})
            self.assertEqual(1,code)
            self.assertIn('WORKFLOW_NOT_ACTIVE',result['error'])

    def test_no_pool_retains_explicit_legacy_compatibility(self):
        with mock.patch('progress_query.live_progress_report',return_value={'legacy_fixture':True}) as legacy:
            code,result=self.query(pool=False)
        self.assertEqual(0,code,result)
        self.assertTrue(result['legacy_fixture'])
        legacy.assert_called_once_with(str(self.root),'COORDINATOR')
