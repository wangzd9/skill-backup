"""Deterministic, profile-scoped model selection; never calls a model to choose a model."""
import json
from pathlib import Path
import urllib.request

from load_metrics import parse_metrics
from runtime_common import locked, read_json, save_json, now

MODES = {'auto': '自动', 'cloud': '云端', 'local': '本地优先（忙时云端）'}
COMMANDS = {
    '切换到云端模型': 'cloud', '切换到云端': 'cloud',
    '切换到本地模型': 'local', '切换到本地': 'local',
    '恢复自动切换': 'auto', '切换到自动模式': 'auto',
    '查看模型状态': 'status', '当前模型状态': 'status',
    '切换到GPT': 'cloud:xcoding', '切换到GPT模型': 'cloud:xcoding',
    '切换到DeepSeek': 'cloud:sub2api', '切换到DeepSeek模型': 'cloud:sub2api',
}
ARGS = {'云端': 'cloud', 'cloud': 'cloud', '自动': 'auto', 'auto': 'auto',
        '本地': 'local', 'local': 'local', '状态': 'status', 'status': 'status'}
FINISHED = {'PUBLISHED', 'NVIDIA_DONE', 'SUCCEEDED', 'FAILED', 'CANCELLED',
            'QUARANTINED', 'INPUT_INVALID', 'INPUT_UNSTABLE', 'FINGERPRINT_CONFLICT',
            'SCENE_DONE', 'SCENE_PARTIAL_FAILED', 'SCENE_FAILED'}


def parse_command(text):
    text = text.strip()
    if text in COMMANDS: return COMMANDS[text]
    parts = text.split()
    if parts and parts[0] in ('/llm', '/模型'):
        if len(parts) == 3 and ARGS.get(parts[1]) == 'cloud':
            return 'cloud:' + parts[2]
        return ARGS.get(parts[1], 'help') if len(parts) == 2 else ('status' if len(parts) == 1 else 'help')
    return None


def choose_route(mode, metrics):
    if mode == 'cloud': return 'cloud', '人工指定云端'
    if mode not in MODES: return 'cloud', '模式无效，保护本地资源'
    if metrics.get('running') is None or metrics.get('waiting') != 0:
        return 'cloud', '本地有等待请求或负载未知'
    return 'local', '本地无等待请求'


def get_text(url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=2) as response:
        body = response.read(2_097_153)
    if len(body) > 2_097_152: raise ValueError('response too large')
    return body.decode('utf-8')


def get_json(url): return json.loads(get_text(url))


class ModelRouter:
    def __init__(self, home, metrics_url):
        self.home = Path(home)
        self.metrics_url = metrics_url
        self.state = self.home / 'runtime/model-routing-state.json'

    def config(self, profile=None):
        from hermes_cli.config_effective import load_user_config_effective
        config = load_user_config_effective(self.home / 'config.yaml')
        settings = dict(config.get('plugins', {}).get('entries', {}).get('role-workflow-runtime', {}).get('settings', {}).get('model_routing', {}))
        selected = profile or self._state().get('cloud_profile') or settings.get('default_cloud_profile')
        if selected:
            profiles = settings.get('cloud_profiles', {})
            if selected not in profiles:
                raise ValueError('未知云端供应商；请用 /llm status 查看已配置供应商。')
            settings.update({k: v for k, v in profiles[selected].items()
                             if k in ('cloud_provider', 'cloud_model', 'cloud_key_env')})
            settings['selected_cloud_profile'] = selected
        return settings

    def _state(self):
        try:
            state = read_json(self.state, {})
            return state if isinstance(state, dict) else {'mode': 'cloud'}
        except (OSError, ValueError):
            return {'mode': 'cloud'}

    def mode(self):
        mode = self._state().get('mode', 'auto')
        return mode if mode in MODES else 'cloud'

    def secret(self, name):
        # The .env fallback lets an operator add the key without restarting the gateway.
        from dotenv import dotenv_values
        from hermes_constants import get_hermes_home
        from agent.secret_scope import get_secret_str
        if Path(get_hermes_home()).resolve() != self.home.resolve(): return ''
        return get_secret_str(name, '').strip() or str(dotenv_values(self.home / '.env').get(name) or '').strip()

    def cloud_ready(self, config):
        return bool(self.secret(config.get('cloud_key_env', 'NUMENUME_API_KEY')))

    def set_mode(self, mode):
        profile = None
        if mode.startswith('cloud:'):
            mode, profile = mode.split(':', 1)
            if not profile: raise ValueError('请指定云端供应商名称。')
        if mode not in MODES: raise ValueError('模式无效')
        config = self.config(profile) if profile else self.config()
        if not self.cloud_ready(config):
            name = config.get('cloud_key_env', 'NUMENUME_API_KEY')
            raise ValueError(f'云端密钥尚未配置；请在本机 ~/.hermes/.env 配置 {name}，勿发送到群里。模式未改变。')
        with locked(str(self.state) + '.lock'):
            state = self._state()
            state.update(mode=mode, updated_at=now())
            if profile: state['cloud_profile'] = profile
            save_json(self.state, state)

    def pipeline_busy(self, config):
        """历史保留：判流水线忙闲已不再参与选路，仅沿用原只读语义供诊断。"""
        try:
            rows = get_json(config['pipeline_url'])
            if not isinstance(rows, list): return None
            return any(not isinstance(row, dict) or row.get('status') not in FINISHED for row in rows)
        except (KeyError, OSError, ValueError, TypeError): return None

    def decision(self, config):
        mode = self.mode()
        if mode == 'cloud':
            return {'mode': mode, 'route': 'cloud', 'reason': '人工指定云端'}
        metrics = {}
        try: metrics = parse_metrics(get_text(self.metrics_url))
        except (OSError, ValueError): pass
        route, reason = choose_route(mode, metrics)
        return {'mode': mode, 'route': route, 'reason': reason}

    def runtime(self, config, route):
        from hermes_cli.runtime_provider import resolve_runtime_provider
        if route == 'cloud':
            if not self.cloud_ready(config): raise ValueError('云端密钥未配置；不会因此抢占本地模型。')
            provider = config.get('cloud_provider', 'custom:numenume-cloud')
            key = self.secret(config.get('cloud_key_env', 'NUMENUME_API_KEY'))
            target_model = config.get('cloud_model', 'deepseek-v4-pro')
        else:
            provider = config.get('local_provider', 'qwen-a100')
            key = None
            target_model = config['local_model']
        if route == 'cloud':
            from agent.secret_scope import current_secret_scope, set_secret_scope, reset_secret_scope
            scope = dict(current_secret_scope() or {})
            scope[config.get('cloud_key_env', 'NUMENUME_API_KEY')] = key
            token = set_secret_scope(scope)
            try:
                result = resolve_runtime_provider(requested=provider, explicit_api_key=key, target_model=target_model)
            finally:
                reset_secret_scope(token)
        else:
            result = resolve_runtime_provider(requested=provider, explicit_api_key=key, target_model=target_model)
        result['model'] = target_model
        result['provider'] = result.get('requested_provider') or provider
        if not result.get('model'): raise ValueError('供应商缺少模型配置')
        result['request_overrides'] = dict(result.get('request_overrides') or {})
        return {k: result[k] for k in ('model', 'provider', 'base_url', 'api_key', 'api_mode', 'capabilities', 'request_overrides') if k in result}

    def command(self, action):
        if action == 'help': return '用法：/llm cloud [供应商]（云端）、/llm auto（自动）、/llm local（本地优先，忙时云端）、/llm status（状态）。'
        if action != 'status':
            try: self.set_mode(action)
            except ValueError as exc: return str(exc)
        config = self.config()
        result = self.decision(config)
        enabled = config.get('enabled', False)
        ready = self.cloud_ready(config)
        return ('**模型切换**\n'
                f'- 模式：{MODES[result["mode"]]}\n'
                f'- 当前选择：{"本地" if result["route"] == "local" else "云端"}\n'
                f'- 原因：{result["reason"]}\n'
                f'- 云端：{config.get("cloud_model", "deepseek-v4-pro")}；密钥{"已配置" if ready else "未配置"}\n'
                f'- 供应商：{config.get("selected_cloud_profile", config.get("cloud_provider", "numenume-cloud"))}；可选：{", ".join(config.get("cloud_profiles", {})) or "当前供应商"}\n'
                f'- 选路：{"已启用" if enabled else "尚未启用"}\n'
                '- 范围：本机所有已绑定角色的群会话和后台任务。\n'
                '- 已发出的单次推理允许结束；后续推理请求按此设置选路。\n'
                '- OCR代码审查在后续批次同步供应商和模型；在途批次完成后切换。\n'
                '- 本次控制不调用大模型。')
