"""A queued native grant authorizes only its own subsequent role turn."""
from types import SimpleNamespace
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

from tests import test_manual_gateway as fixture


class ManualBusyNativeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixture.ManualGatewayTests.setUp(self)

    async def test_full_grant_queued_during_readonly_turn_has_its_own_native_scope(self):
        await self._run_real_followup_case()

    async def test_rewritten_priority_event_keeps_primary_receipt_through_real_drain(self):
        await self._run_real_followup_case(already_prepared=True)

    async def test_old_full_grant_releases_before_new_grant_enters_real_followup(self):
        await self._run_real_followup_case(old_full=True)

    async def test_next_question_does_not_inherit_old_full_turn_permission(self):
        await self._run_real_followup_case(old_full=True,new_full=False)

    async def _run_real_followup_case(self, *, already_prepared=False, old_full=False,new_full=True):
        from agent.turn_input import capture_turn_input
        from gateway.config import GatewayConfig, Platform
        from gateway.run import GatewayRunner
        from gateway.session import SessionSource
        from gateway.session_context import reset_session_vars
        from plugins.platforms.feishu.adapter import FeishuAdapter

        transport = object.__new__(FeishuAdapter)
        transport._bot_open_id, transport._bot_user_id, transport._bot_name = 'bot', '', ''
        transport._download_feishu_message_resources = AsyncMock(return_value=([], []))
        transport.get_chat_info = AsyncMock(return_value={'name': 'role fixture'})
        transport._resolve_sender_profile = AsyncMock(return_value={'user_id': 'u', 'user_name': 'U', 'user_id_alt': ''})
        transport.build_source = lambda **kw: SessionSource(platform=Platform.FEISHU, **kw)
        transport._resolve_channel_prompt = lambda *args: None
        transport._dispatch_inbound_event = AsyncMock()
        transport._pending_messages = {}
        transport.send_typing = AsyncMock()

        async def event(message_id, text):
            message = SimpleNamespace(content=json.dumps({'text': text}), message_type='text',
                                      mentions=[], chat_id='A', parent_id=None)
            await transport._process_inbound_message(data={}, message=message,
                sender_id=SimpleNamespace(open_id='u'), chat_type='group', message_id=message_id)
            return transport._dispatch_inbound_event.call_args.args[0]

        ingress=[]
        async def dispatch(name, **kw):
            if name == 'pre_gateway_busy_dispatch':
                return [await self.plugin.on_busy_message(**kw)]
            if name == 'pre_gateway_dispatch':
                ingress.append(kw['event'].message_id)
                return [await self.plugin.on_message(**kw)]
            return []

        reset_session_vars()
        self.addCleanup(reset_session_vars)
        with patch.dict(os.environ, {'HERMES_HOME': str(self.home)}), \
             patch('gateway.run._hermes_home', self.home), \
             patch('hermes_cli.lifecycle.ainvoke_hook', dispatch):
            runner = object.__new__(GatewayRunner)
            runner.config = GatewayConfig()
            runner.adapters = {}
            adapter = transport
            runner._delivery_adapter_for = lambda source: adapter
            runner._is_user_authorized = lambda source: True
            runner._is_user_authorized_for_source = lambda source: True
            runner._admit_bot_message_for_source = lambda source: True
            runner._send_busy_ack_reply = AsyncMock()
            runner._resolve_busy_steer_or_redirect = AsyncMock()
            runner._draining = False
            old = await event('query', '全部权限：执行当前派单' if old_full else '当前进展如何？')
            await runner._hm_pre_gateway_dispatch_hook(old, old.source)
            tokens = runner._set_session_env(SimpleNamespace(source=old.source, session_key='role-key'))
            try:
                old_info = dict(capture_turn_input(SimpleNamespace(platform='feishu'), old.message_id))
                self.plugin.on_llm(session_id='A', turn_id='old-turn', trusted_turn_input=old_info)
                if old_full:
                    self.assertTrue(json.loads(self.plugin.lease_tool({'action': 'claim'},
                        session_id='A',turn_id='old-turn'))['claimed'])
                grant = await event('grant', '全部权限，执行当前既有派单' if new_full else '当前任务进行得怎样？')
                if already_prepared:
                    # The priority/race entry has already run native ingress.
                    grant = await runner._hm_pre_gateway_dispatch_hook(grant,grant.source)
                self.assertTrue(await runner._handle_active_session_busy_message(grant, 'role-key'))
                if not old_full:
                    self.assertEqual(self.plugin.on_tool(tool_name='role_manual_lease', session_id='A',
                        turn_id='old-turn', trusted_turn_input=old_info)['action'], 'block')
                if not already_prepared:
                    self.assertFalse(capture_turn_input(SimpleNamespace(platform='feishu'), grant.message_id)['trusted_gateway'])
            finally:
                runner._clear_session_env(tokens)
            # TurnFacade finalizes the old native turn before gateway FIFO drain.
            self.plugin.on_turn_end(session_id='A', turn_id='old-turn')
            old_result={'messages':[],'final_response':'旧回合答复'}
            queued,pending=await runner._run_agent_drain_pending(old_result,adapter,old.source,'role-key')
            self.assertIs(queued,grant)
            runner._session_key_for_source=lambda source:'role-key'
            runner._intake_adapter_for=lambda source:None
            runner._refresh_agent_cache_message_count=AsyncMock()
            runner._run_agent_deliver_first_response=AsyncMock()

            async def prepare(**kw):
                return kw['event'].text

            seen=[]
            async def run_next(**kw):
                # Only model execution is replaced. Real FIFO drain, native
                # pre-dispatch and recursive-call arguments determine identity.
                seen.append(kw)
                self.assertEqual(kw['inbound_message_id'],'grant')
                self.assertIn('当前原生消息明确授予' if new_full else '本回合默认只读',kw['message'])
                self.assertIs(kw['history'],old_result['messages'])
                try:
                    info=dict(capture_turn_input(SimpleNamespace(platform='feishu'),kw['inbound_message_id']))
                    self.assertTrue(info['trusted_gateway'])
                    self.plugin.on_llm(session_id='A',turn_id='new-turn',trusted_turn_input=info)
                    if not new_full:
                        self.assertEqual(self.plugin.on_tool(tool_name='role_manual_lease',session_id='A',
                            turn_id='new-turn',trusted_turn_input=info)['action'],'block')
                        with self.assertRaises(ValueError):
                            self.plugin.lease_tool({'action':'claim'},session_id='A',turn_id='new-turn')
                        return {'messages':[],'final_response':'只读答复'}
                    self.assertIsNone(self.plugin.on_tool(tool_name='role_manual_lease',session_id='A',
                        turn_id='new-turn',trusted_turn_input=info))
                    lease=json.loads(self.plugin.lease_tool({'action':'claim'},session_id='A',turn_id='new-turn'))
                    self.assertTrue(lease['claimed'])
                    self.assertIsNone(self.plugin.on_tool(tool_name='terminal',session_id='A',
                        turn_id='new-turn',trusted_turn_input=info))
                    self.assertEqual(self.plugin.on_tool(tool_name='terminal',session_id='A',
                        turn_id='old-turn',trusted_turn_input=old_info)['action'],'block')
                    return {'messages':[],'final_response':'新回合已处理'}
                finally:
                    self.plugin.on_turn_end(session_id='A',turn_id='new-turn')

            runner._prepare_profile_scoped_inbound_message_text=prepare
            runner._run_agent=run_next
            turn_ctx=SimpleNamespace(source=old.source,session_id='A',session_key='role-key',run_generation=1,
                _interrupt_depth=0,history=[],_status_thread_metadata=None,context_prompt='',result_holder=[old_result])
            stale_tokens=runner._set_session_env(SimpleNamespace(source=old.source,session_key='role-key'))
            try:
                await runner._run_agent_queued_followup(turn_ctx,adapter,pending,queued,old_result,old_result,None)
            finally:
                runner._clear_session_env(stale_tokens)
            self.assertEqual(len(seen),1)
            self.assertEqual(ingress.count('grant'),1)
            runner._resolve_busy_steer_or_redirect.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
