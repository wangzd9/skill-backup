"""Boundary regressions for ingress scope and future-turn-only activation."""
import json
import unittest

from tests import test_manual_gateway as fixture


class ManualGatewayEdgeTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixture.ManualGatewayTests.setUp
    request = fixture.ManualGatewayTests.request
    tool = fixture.ManualGatewayTests.tool

    async def test_ingress_project_rebinding_cannot_reuse_full_grant(self):
        _, info = await self.request('本轮授予全部权限：执行任务')
        replacement = self.home / 'replacement'
        replacement.mkdir()
        binding = json.loads((self.project / 'ROLE-BINDINGS.json').read_text())
        binding['project_root'] = str(replacement)
        (replacement / 'ROLE-BINDINGS.json').write_text(json.dumps(binding))
        self.plugin.registry.write_text(json.dumps({'projects': [{'project_root': str(replacement)}]}))

        self.plugin.on_llm(session_id='A', turn_id='rebound', trusted_turn_input=info)
        denied = self.plugin.on_tool(session_id='A', turn_id='rebound',
                                     tool_name='role_manual_lease', trusted_turn_input=info)
        self.assertEqual(denied['action'], 'block')
        with self.assertRaises(ValueError):
            self.plugin.lease_tool({'action': 'claim'}, session_id='A', turn_id='rebound')
        self.assertFalse((replacement / 'inbox').exists())
        self.assertFalse((self.project / 'inbox').exists())

    async def test_native_activation_failure_keeps_full_primary_request_readonly(self):
        self.plugin.native_ready = False
        _, info = await self.request('本轮授予全部权限：执行任务')
        context = self.plugin.on_llm(session_id='A', turn_id='no-native', trusted_turn_input=info)
        self.assertIn('只读', context)
        for name in ('role_manual_lease', 'terminal', 'write_file'):
            self.assertEqual(self.tool(turn='no-native', tool=name, info=info)['action'], 'block')
        with self.assertRaises(ValueError):
            self.plugin.lease_tool({'action': 'claim'}, session_id='A', turn_id='no-native')
        self.assertFalse((self.project / 'inbox').exists())

    async def test_restored_cron_id_end_cleans_only_its_exact_manual_owner(self):
        _, info = await self.request('本轮授予全部权限：执行任务')
        session = 'cron_job-A_20261016_180000'
        self.plugin.on_llm(session_id=session, turn_id='manual', trusted_turn_input=info)
        claim = json.loads(self.plugin.lease_tool({'action': 'claim'}, session_id=session, turn_id='manual'))
        self.assertTrue(claim['claimed'])
        runtime = fixture.manual_business.runtime_module()
        owned = runtime.lease_path(self.project, 'A')
        foreign = runtime.lease_path(self.project, 'B')
        runtime.save(foreign, {'owner': 'foreign', 'token': 'keep', 'expires_at': 0})
        before = foreign.read_bytes()

        # A reload discards grants but must still release the exact manual owner.
        fresh = fixture.module.RuntimePlugin(registry=self.plugin.registry, metrics_url='unused', home=self.home)
        fresh.on_turn_end(session_id=session, turn_id='manual')
        self.assertFalse(owned.exists())
        self.assertEqual(before, foreign.read_bytes())


if __name__ == '__main__':
    unittest.main()
