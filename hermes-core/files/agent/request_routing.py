"""Apply an explicit plugin route before building each physical model request.

The native switch keeps transport, compressor and billing metadata coherent. Routing
does not rebuild the conversation's frozen system prompt or replay tool execution.
"""
from __future__ import annotations


def apply_model_route(agent):
    from hermes_cli.plugins import has_hook, invoke_hook
    if not has_hook('resolve_model_route'):
        return
    results = invoke_hook(
        'resolve_model_route', session_id=agent.session_id or '',
        platform=agent.platform or '', model=agent.model, provider=agent.provider,
        base_url=agent.base_url, api_mode=agent.api_mode,
    )
    for result in results:
        if not isinstance(result, dict):
            continue
        if result.get('error'):
            raise RuntimeError(str(result['error']))
        if result.get('action') == 'block':
            raise RuntimeError(str(result.get('message') or 'Model routing policy blocked this request.'))
        if 'runtime' in result and not isinstance(result['runtime'], dict):
            raise RuntimeError('Model route is malformed; original provider was not called.')
    # A policy veto wins even when a preceding callback proposed a valid route.
    for result in results:
        if not isinstance(result, dict) or 'runtime' not in result:
            continue
        runtime = result['runtime']
        required = ('model', 'provider', 'base_url', 'api_mode', 'api_key')
        if any(not runtime.get(k) for k in required):
            raise RuntimeError('Model route is incomplete; original provider was not called.')
        signature = tuple(runtime[k] for k in required)
        current = tuple(getattr(agent, k, None) for k in required)
        if signature != current:
            frozen_prompt = getattr(agent, '_cached_system_prompt', None)
            agent.switch_model(
                new_model=runtime['model'], new_provider=runtime['provider'],
                api_key=runtime['api_key'], base_url=runtime['base_url'], api_mode=runtime['api_mode'],
                capabilities=runtime.get('capabilities'),
            )
            agent._cached_system_prompt = frozen_prompt
        if 'request_overrides' in runtime:
            agent.request_overrides = dict(runtime['request_overrides'] or {})
            primary = getattr(agent, '_primary_runtime', None)
            if isinstance(primary, dict):
                primary['request_overrides'] = dict(agent.request_overrides)
        # An explicit routing policy owns fallback too; otherwise an old fallback can
        # re-enable a provider that the policy just excluded.
        agent._fallback_chain = []
        agent._fallback_model = None
        return
