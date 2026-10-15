#!/usr/bin/env python3
"""本机模型路由判定的只读核验：当前模式 + 实时队列 + 判定与原因。

用途：回答"现在会走本地还是云端、为什么"，以及策略改动后留一条真实判定作证。
只读：读取 state 文件 + 一次 metrics GET；不写配置、不改模式、不调用任何模型。

用法：
    /home/wang/.hermes/hermes-agent/venv/bin/python verify_routing_decision.py
可选：--metrics-url 覆盖，--summarize-modes 额外列出 auto/local 两种假设下的判定。
"""
import argparse
import json
import sys
from pathlib import Path

HOME = Path.home() / '.hermes'
sys.path.insert(0, str(HOME / 'hermes-agent'))
sys.path.insert(0, str(HOME / 'plugins/role-workflow-runtime'))

from model_routing import ModelRouter, choose_route  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--metrics-url', default='http://192.168.2.45:8002/metrics')
    parser.add_argument('--summarize-modes', action='store_true',
                        help='额外列出 auto/local 两种假设模式下的判定')
    args = parser.parse_args()

    router = ModelRouter(HOME, args.metrics_url)
    config = router.config()
    state = router._state()
    mode = router.mode()

    metrics = {}
    metrics_error = None
    try:
        from model_routing import get_text
        from load_metrics import parse_metrics
        metrics = parse_metrics(get_text(router.metrics_url))
    except (OSError, ValueError) as exc:
        metrics_error = f'{type(exc).__name__}: {exc}'

    out = {
        'stored_mode': mode,
        'state': state,
        'cloud_profile': config.get('selected_cloud_profile'),
        'cloud_model': config.get('cloud_model'),
        'local_model': config.get('local_model'),
        'vllm_running': metrics.get('running'),
        'vllm_waiting': metrics.get('waiting'),
        'metrics_error': metrics_error,
        'decision': router.decision(config),
    }
    if args.summarize_modes:
        out['by_assumed_mode'] = {
            m: {'route': choose_route(m, metrics)[0], 'reason': choose_route(m, metrics)[1]}
            for m in ('auto', 'local', 'cloud')
        }
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
