import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path.home() / '.hermes/hermes-agent'))
from ocr_model_route import select
from model_routing import ModelRouter


class OCRRouteTests(unittest.TestCase):
    def test_same_selection_drives_chat_and_ocr_and_rejects_configuration_drift(self):
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        from agent.secret_scope import set_secret_scope, reset_secret_scope
        from hermes_cli.config import atomic_config_write
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            project = home / 'project'
            project.mkdir()
            (home / 'role-projects.json').write_text(json.dumps({'projects': [{'project_root': str(project)}]}))
            profiles = {
                'sub2api': {'cloud_provider': 'custom:sub2api', 'cloud_key_env': 'SUB2API_API_KEY', 'cloud_model': 'deepseek-v4-pro'},
                'xcoding': {'cloud_provider': 'custom:xcoding', 'cloud_key_env': 'XCODING_API_KEY', 'cloud_model': 'gpt-6.1-sol'}}
            providers = {name: {'base_url': 'https://' + name + '.invalid/v1', 'api_mode': 'responses',
                                'key_env': value['cloud_key_env']} for name, value in profiles.items()}
            atomic_config_write(home / 'config.yaml', {'providers': providers, 'plugins': {'entries': {
                'role-workflow-runtime': {'settings': {'model_routing': {
                    'enabled': True, 'ocr_enabled': True, 'default_cloud_profile': 'sub2api',
                    'cloud_profiles': profiles}}}}}})
            native = {'custom_providers': {'hermes-' + name: {
                'url': providers[name]['base_url'], 'model': value['cloud_model'], 'protocol': 'openai-responses'}
                for name, value in profiles.items()}}
            ocr_config = home / 'ocr.json'
            ocr_config.write_text(json.dumps(native))
            token = set_hermes_home_override(home)
            st = set_secret_scope({'SUB2API_API_KEY': 'fixture-a', 'XCODING_API_KEY': 'fixture-b'})
            try:
                router = ModelRouter(home, '')
                for profile in ('sub2api', 'xcoding', 'sub2api'):
                    router.set_mode('cloud:' + profile)
                    route = select(home, project, ocr_config)
                    self.assertEqual(route['model'], router.config()['cloud_model'])
                    self.assertEqual(route['provider'], 'hermes-' + profile)
                native['custom_providers']['hermes-sub2api']['url'] = 'https://stale.invalid/v1'
                ocr_config.write_text(json.dumps(native))
                with self.assertRaises(ValueError): select(home, project, ocr_config)
                self.assertIsNone(select(home, home / 'unregistered', ocr_config))
            finally:
                reset_secret_scope(st)
                reset_hermes_home_override(token)


if __name__ == '__main__':
    unittest.main()
