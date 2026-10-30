import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path.home() / '.hermes/hermes-agent'))
from model_routing import ModelRouter, parse_command, choose_route


class RoutingTests(unittest.TestCase):
    def test_local_admission_uses_queue_only(self):
        # 只按等待队列判断：有空槽即可用本地；running 未知或存在排队仍走云端。
        for metrics, expected in (({'running': 0, 'waiting': 0}, 'local'),
                                  ({'running': 4, 'waiting': 0}, 'local'),
                                  ({'running': 1, 'waiting': 0}, 'local'),
                                  ({'running': 4, 'waiting': 12}, 'cloud'),
                                  ({'running': 0, 'waiting': 1}, 'cloud'),
                                  ({'running': None, 'waiting': 0}, 'cloud'),
                                  ({'running': 4, 'waiting': None}, 'cloud'),
                                  ({}, 'cloud')):
            self.assertEqual(choose_route('auto', metrics)[0], expected, metrics)
            self.assertEqual(choose_route('local', metrics)[0], expected, metrics)

    def test_mode_short_circuits_cloud_without_probe(self):
        for mode in ('cloud', 'broken'):
            self.assertEqual(choose_route(mode, {'running': 0, 'waiting': 0})[0], 'cloud')

    def test_ocr_profile_pins_local_and_bypasses_queue(self):
        # OCR 固定本地：OCR 选路只看 ocr_profile，且不因队列忙/探测失败改判云端。
        from hermes_cli.config import atomic_config_write
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            atomic_config_write(home / 'config.yaml', {'plugins': {'entries': {
                'role-workflow-runtime': {'settings': {'model_routing': {
                    'ocr_enabled': True, 'ocr_profile': 'xcoding',
                    'cloud_profiles': {'xcoding': {'cloud_model': 'gpt-6.1-sol'}}}}}}}})
            router = ModelRouter(home, 'http://unused/metrics')
            # 群会话配置不带 OCR 标记；OCR 选择器显式传 ocr_profile 才带。
            self.assertNotIn('_ocr_route', router.config())
            self.assertTrue(router.config('xcoding').get('_ocr_route'))
            ocr = router.config('xcoding')
            for mode in ('auto', 'local', 'cloud'):
                router.state.parent.mkdir(parents=True, exist_ok=True)
                router.state.write_text(json.dumps({'mode': mode}))
                decision = router.decision(ocr)
                self.assertEqual(decision['route'], 'local', (mode, decision))
                self.assertEqual(decision['reason'], 'OCR 固定本地')
            with patch('model_routing.get_text', side_effect=TimeoutError('endpoint down')):
                self.assertEqual(ModelRouter(home, 'http://unused/metrics').decision(ocr)['route'], 'local')
            # 显式关闭时才允许回云端。
            self.assertEqual(router.decision({**ocr, 'ocr_local_only': False})['route'], 'cloud')

    def test_group_sessions_ignore_ocr_pin(self):
        # OCR 固定本地不得泄漏到群会话选路：无 profile 的 config 仍按队列判定。
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            router = ModelRouter(home, 'http://unused/metrics')
            router.state.parent.mkdir(parents=True, exist_ok=True)
            router.state.write_text(json.dumps({'mode': 'auto'}))
            with patch('model_routing.get_text', return_value='vllm:num_requests_running{engine="0"} 2\n'
                                                             'vllm:num_requests_waiting{engine="0"} 0\n'):
                self.assertEqual(router.decision({'enabled': True})['route'], 'local')
            with patch('model_routing.get_text', return_value='vllm:num_requests_running{engine="0"} 2\n'
                                                             'vllm:num_requests_waiting{engine="0"} 3\n'):
                self.assertEqual(router.decision({'enabled': True})['route'], 'cloud')

    def test_commands_are_exact_not_prompt_fragments(self):
        for text, action in (('切换到云端模型', 'cloud'), ('/模型 自动', 'auto'),
                             ('/llm local', 'local'), ('查看模型状态', 'status'),
                             ('切换到GPT', 'cloud:xcoding'),
                             ('/llm cloud sub2api', 'cloud:sub2api')):
            self.assertEqual(parse_command(text), action)
        self.assertIsNone(parse_command('请解释“切换到云端模型”的意思'))

    def test_mode_persists_and_cloud_skips_probes(self):
        with tempfile.TemporaryDirectory() as tmp:
            router = ModelRouter(Path(tmp), 'http://unused/metrics')
            with patch.object(router, 'config', return_value={'enabled': True}), \
                 patch.object(router, 'cloud_ready', return_value=True):
                router.set_mode('cloud')
            other = ModelRouter(Path(tmp), 'http://unused/metrics')
            self.assertEqual(other.mode(), 'cloud')
            with patch.object(other, 'pipeline_busy', side_effect=AssertionError('no probe')):
                self.assertEqual(other.decision({'enabled': True})['route'], 'cloud')

    def test_missing_credential_cannot_commit_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            router = ModelRouter(Path(tmp), 'http://unused/metrics')
            with patch.object(router, 'config', return_value={'enabled': True}), \
                 patch.object(router, 'cloud_ready', return_value=False):
                with self.assertRaises(ValueError): router.set_mode('cloud')
            self.assertFalse(router.state.exists())

    def test_active_pipeline_and_unknown_status_are_busy(self):
        with tempfile.TemporaryDirectory() as tmp:
            router = ModelRouter(Path(tmp), 'http://unused/metrics')
            for rows, busy in (([], False), ([{'status': 'SCENE_DONE'}], False),
                               ([{'status': 'SCENE_RUNNING'}], True),
                               ([{'status': 'SCENE_SLICE_QUEUE'}], True),
                               ([{'status': 'NEW_UNKNOWN_STATE'}], True)):
                with patch('model_routing.get_json', return_value=rows):
                    self.assertEqual(router.pipeline_busy({'pipeline_url': 'http://localhost/runs'}), busy)
            with patch('model_routing.get_json', side_effect=TimeoutError):
                self.assertIsNone(router.pipeline_busy({'pipeline_url': 'http://localhost/runs'}))

    def test_corrupt_policy_does_not_restore_local_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            router = ModelRouter(Path(tmp), 'http://unused/metrics')
            router.state.parent.mkdir(parents=True)
            router.state.write_text('{broken')
            self.assertEqual(router.mode(), 'cloud')

    def test_auto_cloud_policy_ignores_manual_memory_and_cloud_keeps_manual_choice(self):
        from hermes_cli.config import atomic_config_write
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            atomic_config_write(home / 'config.yaml', {'plugins': {'entries': {
                'role-workflow-runtime': {'settings': {'model_routing': {
                    'default_cloud_profile': 'xcoding', 'auto_cloud_profile': 'sub2api',
                    'cloud_profiles': {
                        'sub2api': {'cloud_model': 'deepseek-v4.1-flash'},
                        'xcoding': {'cloud_model': 'gpt-5.5'}}}}}}}})
            router = ModelRouter(home, 'http://unused/metrics')
            self.assertEqual(router.config()['cloud_model'], 'deepseek-v4.1-flash')
            with patch.object(router, 'cloud_ready', return_value=True):
                router.set_mode('cloud:xcoding')
                router.set_mode('auto')
            other = ModelRouter(home, 'http://unused/metrics')
            self.assertEqual(other.mode(), 'auto')
            self.assertEqual(other.config()['cloud_model'], 'deepseek-v4.1-flash')
            with patch.object(other, 'cloud_ready', return_value=True):
                other.set_mode('cloud')
            self.assertEqual(other.config()['cloud_model'], 'gpt-5.5')
            self.assertEqual(other.config('sub2api')['cloud_model'], 'deepseek-v4.1-flash')
            before = other.state.read_text()
            with self.assertRaises(ValueError):
                other.set_mode('cloud:unknown')
            self.assertEqual(other.state.read_text(), before)

    def test_real_provider_and_credentials_stay_scoped_a_b_a(self):
        from agent.secret_scope import set_secret_scope, reset_secret_scope
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        from hermes_cli.config import atomic_config_write
        with tempfile.TemporaryDirectory() as tmp:
            homes = [Path(tmp) / label for label in ('a', 'b')]
            routers = []
            for home, label in zip(homes, ('a', 'b')):
                home.mkdir()
                (home / '.env').write_text('NUMENUME_API_KEY=fixture-' + label + '\n')
                atomic_config_write(home / 'config.yaml', {
                    'providers': {'numenume-cloud': {
                        'base_url': 'http://127.0.0.1/' + label + '/v1',
                        'api_mode': 'chat_completions', 'key_env': 'NUMENUME_API_KEY'}},
                    'plugins': {'entries': {'role-workflow-runtime': {'settings': {
                        'model_routing': {'cloud_model': 'model-' + label}}}}}})
                routers.append(ModelRouter(home, 'http://unused/metrics'))
            for index in (0, 1, 0):
                token = set_hermes_home_override(homes[index])
                secret_token = set_secret_scope({'NUMENUME_API_KEY': ''})
                try:
                    router = routers[index]
                    runtime = router.runtime(router.config(), 'cloud')
                    label = ('a', 'b')[index]
                    self.assertEqual(runtime['api_key'], 'fixture-' + label)
                    self.assertEqual(runtime['model'], 'model-' + label)
                    self.assertEqual(runtime['base_url'], 'http://127.0.0.1/' + label + '/v1')
                    self.assertEqual(routers[1 - index].secret('NUMENUME_API_KEY'), '')
                finally:
                    reset_secret_scope(secret_token)
                    reset_hermes_home_override(token)


if __name__ == '__main__': unittest.main()
