"""Plugin controls and independent-turn queueing after native authorization."""


async def handle_plugin_busy_control(gateway, event, *, session_key=None):
    from hermes_cli.lifecycle import ainvoke_hook
    results = await ainvoke_hook('pre_gateway_busy_dispatch', event=event, gateway=gateway)
    if any(isinstance(result, dict) and result.get('action') == 'skip' for result in results):
        return True
    directive = next((result for result in results
                      if isinstance(result, dict) and result.get('action') == 'queue'), None)
    if directive is None or not session_key:
        return False
    adapter = gateway._delivery_adapter_for(event.source)
    if adapter is None or not isinstance(getattr(adapter, '_pending_messages', None), dict):
        import logging
        logging.getLogger(__name__).warning('Plugin requested an independent turn but native FIFO is unavailable')
        message = directive.get('queue_unavailable_message')
        if adapter is not None and isinstance(message, str) and message:
            await gateway._send_busy_ack_reply(event, adapter, message)
        # An unavailable transport cannot turn a queue directive into steering.
        return True
    # Keep the original event and native primary-text receipt together. Steering
    # would attach new authorization to the running turn's old identity; media
    # merging could instead move the grant onto another message's identity.
    full = gateway._queue_depth(session_key, adapter=adapter) >= gateway._BUSY_QUEUE_MAX_PENDING
    if not full:
        gateway._enqueue_fifo(session_key, event, adapter)
    message = directive.get('queue_full_message' if full else 'message')
    if isinstance(message, str) and message:
        await gateway._send_busy_ack_reply(event, adapter, message)
    # A full queue must never fall back to steering or interrupting this event.
    return True
