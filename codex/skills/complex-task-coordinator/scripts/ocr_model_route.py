"""Bridge the fixed OCR batch runner to the same profile-scoped model policy.

Only metadata is returned by select. The key subcommand is consumed privately by
OpenCodeReview's native api_key_cmd; credentials never enter run artifacts.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path.home() / '.hermes/hermes-agent'))
from model_routing import ModelRouter


def select(home, project, ocr_config):
    from runtime_common import read_json
    registry = read_json(home / 'role-projects.json', {'projects': []})
    if project.resolve() not in {Path(p['project_root']).resolve() for p in registry['projects']}:
        return None
    router = ModelRouter(home, 'http://192.168.2.45:8002/metrics')
    config = router.config()
    if not config.get('enabled') or not config.get('ocr_enabled'):
        return None
    # OCR 可用独立供应商：只在审查链生效，不影响群会话与后台任务的选路。
    ocr_profile = config.get('ocr_profile')
    if ocr_profile:
        config = router.config(ocr_profile)
    # The installed metrics endpoint remains configurable just like the chat path.
    from hermes_cli.config_effective import load_user_config_effective
    settings = load_user_config_effective(home / 'config.yaml')['plugins']['entries']['role-workflow-runtime']['settings']
    router.metrics_url = settings.get('metrics_url', router.metrics_url)
    decision = router.decision(config)
    runtime = router.runtime(config, decision['route'])
    provider = ('vllm-local' if decision['route'] == 'local'
                else 'hermes-' + config['selected_cloud_profile'])
    native = json.loads(ocr_config.read_text())['custom_providers']
    item = native[provider]
    protocol = 'openai-responses' if runtime['api_mode'] == 'codex_responses' else 'openai'
    if (item['url'].rstrip('/') != runtime['base_url'].rstrip('/')
            or item['protocol'] != protocol):
        raise ValueError('OCR provider configuration differs from the selected Hermes route.')
    allowed = []
    for name, p in native.items():
        if name == 'vllm-local':
            model = config.get('local_model', p['model'])
        elif name.startswith('hermes-') and name[7:] in config.get('cloud_profiles', {}):
            model = config['cloud_profiles'][name[7:]]['cloud_model']
        else:
            continue
        allowed.append({'provider': name, 'model': model, 'endpoint': p['url']})
    return {'provider': provider, 'model': runtime['model'], 'endpoint': runtime['base_url'],
            'hermes_provider': runtime['provider'], 'reason': decision['reason'],
            'effort': 'high' if runtime['model'] == 'gpt-6.1-sol' else 'medium',
            'allowed_routes': allowed}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--home', type=Path, default=Path.home() / '.hermes')
    parser.add_argument('action', choices=('select', 'key'))
    parser.add_argument('value')
    parser.add_argument('--ocr-config', type=Path, default=Path.home() / '.opencodereview/config.json')
    args = parser.parse_args()
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    token = set_hermes_home_override(args.home)
    try:
        if args.action == 'key':
            if args.value not in {'SUB2API_API_KEY', 'XCODING_API_KEY', 'NUMENUME_API_KEY'}:
                raise ValueError('Unsupported credential name.')
            key = ModelRouter(args.home, '').secret(args.value)
            if not key: raise ValueError('Credential not configured.')
            print(key)
        else:
            print(json.dumps(select(args.home, Path(args.value), args.ocr_config), ensure_ascii=False))
    except Exception:
        print('OCR model selection/credential resolution failed; local fallback refused.', file=sys.stderr)
        raise SystemExit(1)
    finally:
        reset_hermes_home_override(token)


if __name__ == '__main__':
    main()
