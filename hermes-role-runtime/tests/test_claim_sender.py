import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime_tools import send_claim_notice


class ClaimSenderTests(unittest.TestCase):
    def test_claim_send_does_not_inherit_cron_final_duplicate_guard(self):
        environment = {'HERMES_CRON_AUTO_DELIVER_PLATFORM': 'feishu',
                       'HERMES_CRON_AUTO_DELIVER_CHAT_ID': 'bound-chat',
                       'HERMES_CRON_AUTO_DELIVER_THREAD_ID': '', 'HERMES_HOME': '/profile'}
        run = SimpleNamespace(returncode=0, stdout=json.dumps({'success': True, 'message_id': 'om-confirmed'}))
        with patch.dict(os.environ, environment, clear=True), patch('runtime_tools.subprocess.run', return_value=run) as sender:
            result = send_claim_notice({'chat_id': 'bound-chat', 'text': '领取 TASK-01，开始工作。'})
            kwargs = sender.call_args.kwargs
            self.assertEqual(kwargs['env'], {'HERMES_HOME': '/profile'})
            self.assertFalse(kwargs.get('shell', False))
            self.assertEqual(os.environ['HERMES_CRON_AUTO_DELIVER_CHAT_ID'], 'bound-chat')
        self.assertTrue(result['delivered'])

    def test_skipped_or_unconfirmed_native_result_is_not_delivery(self):
        for payload in ({'success': True, 'skipped': True, 'reason': 'cron_auto_delivery_duplicate_target'},
                        {'success': True}, {'success': False, 'message_id': 'om-no'}):
            run = SimpleNamespace(returncode=0, stdout=json.dumps(payload))
            with self.subTest(payload=payload), patch('runtime_tools.subprocess.run', return_value=run):
                self.assertFalse(send_claim_notice({'chat_id': 'bound-chat', 'text': '领取通知'})['delivered'])


if __name__ == '__main__':
    unittest.main()
