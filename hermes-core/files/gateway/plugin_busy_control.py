"""Control-only plugin interception after authorization, before the busy queue."""


async def handle_plugin_busy_control(gateway, event):
    from hermes_cli.lifecycle import ainvoke_hook
    results = await ainvoke_hook('pre_gateway_busy_dispatch', event=event, gateway=gateway)
    return any(isinstance(result, dict) and result.get('action') == 'skip' for result in results)
