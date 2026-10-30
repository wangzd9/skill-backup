"""Activate native input-policy seams for future turns without restarting owners.

This is a bounded source migration, not a module reload: only the named functions
are compiled, all against their existing module namespaces. Already executing
turn-facade frames retain their old code and receive no new authority receipt.
"""
from __future__ import annotations

import ast
import hashlib
from pathlib import Path
import sys
import threading


_LOCK = threading.Lock()
_TARGETS = {
    "tools/tool_search.py": ("is_deferrable_tool_name",),
    # New registrations need the current slotted metadata layout. Existing
    # entries keep their identity and are read with compatible defaults.
    "tools/registry.py": ("ToolEntry", "ToolRegistry.register"),
    "gateway/native_turn_input.py": ("native_primary_input", "bind_native_primary_input",
                                      "current_native_primary_input", "clear_native_primary_input"),
    "agent/turn_input.py": ("capture_turn_input", "turn_input_for_hooks"),
    "gateway/session_context.py": ("get_bound_session_env", "reset_session_vars", "clear_session_vars"),
    "hermes_cli/plugins.py": ("_get_pre_tool_call_directive_details", "PluginContext.register_tool"),
    "agent/tool_executor.py": ("_pre_tool_block",),
    "agent/agent_runtime_helpers.py": ("_pre_tool_block_message",),
    "model_tools.py": ("_pre_dispatch_guards", "_execute_tool"),
    "gateway/platforms/base.py": ("merge_pending_message_event", "BasePlatformAdapter._enqueue_text_event",
                                  "BasePlatformAdapter._queue_text_debounce"),
    "plugins/platforms/feishu/adapter.py": ("FeishuAdapter._process_inbound_message",
                                          "FeishuAdapter._enqueue_text_event", "FeishuAdapter._enqueue_media_event"),
    "gateway/run_inbound.py": ("GatewayInboundMixin._hm_pre_gateway_dispatch_hook",
                               "GatewayInboundMixin._hm_handle_running_session_message"),
    "gateway/plugin_busy_control.py": ("handle_plugin_busy_control",),
    "gateway/run_busy.py": ("GatewayBusySessionMixin._handle_active_session_busy_message",),
    "gateway/run_turn.py": ("GatewayTurnMixin._run_agent_queued_followup",),
    "agent/turn_context.py": ("_bind_turn_identity", "_collect_pre_llm_call_context"),
    "agent/turn_facade.py": ("TurnFacadeMixin.run_conversation",),
    "agent/turn_api_request.py": ("build_api_request",),
    "agent/relay_runtime.py": ("RelaySessionCoordinator.begin_turn",
                               "RelaySessionCoordinator._register_native_turn",
                               "RelaySessionCoordinator.turn_state",
                               "RelaySessionCoordinator._unregister_active_turn"),
    "cron/executions.py": ("_owner_is_live", "_warn_live_owner_once", "recover_interrupted_executions"),
    "cron/scheduler.py": ("_latest_executions_for_releasable_claims", "sweep_stale_inflight", "_record_stale_release", "_construct_cron_agent"),
    "cron/occurrences.py": ("unclaimed_pending_slot",),
    "cron/role_reporting_policy.py": ("is_role_job", "suppress_role_delivery"),
    "cron/scheduler_delivery.py": ("_deliver_result",),
}


def _function(tree, dotted):
    body = tree.body
    for part in dotted.split("."):
        node = next((value for value in body if isinstance(value, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                     and value.name == part), None)
        if node is None:
            raise RuntimeError(f"Native policy activation source is missing {dotted}")
        body = node.body
    return node


def activate_native_turn_policy():
    """Rebind only known native seams; return file hashes and activation evidence.

    No service restart, in-flight owner mutation, model call or module reload. A
    policy plugin should call this before registering handlers that issue grants.
    """
    root = Path(__file__).resolve().parent.parent
    with _LOCK:
        modules = list({id(module): module for module in sys.modules.values() if module is not None}.values())
        replacements, hashes = [], {}
        loaded_by_path = {}
        target_names = {Path(relative).name for relative in _TARGETS}
        for module in modules:
            path = getattr(module, "__file__", None)
            if path and Path(path).name in target_names:
                loaded_by_path.setdefault(Path(path).resolve(), []).append(module)
        for relative, targets in _TARGETS.items():
            source = root / relative
            raw = source.read_bytes()
            hashes[relative] = hashlib.sha256(raw).hexdigest()
            tree = ast.parse(raw, filename=str(source))
            loaded = loaded_by_path.get(source, [])
            # Modules not imported yet will read the new disk code naturally.
            for module in loaded:
                for dotted in targets:
                    node = _function(tree, dotted)
                    owner, name = module, dotted
                    if "." in dotted:
                        class_name, name = dotted.split(".", 1)
                        owner = getattr(module, class_name)
                    old = getattr(owner, name, None)
                    if old is not None and not callable(old):
                        raise RuntimeError(f"Native policy activation target is not callable: {relative}:{dotted}")
                    staging = {}
                    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), module.__dict__, staging)
                    new = staging[name]
                    if old is not None:
                        new.__qualname__ = old.__qualname__
                    replacements.append((owner, name, old, new))
        # Compile/validate every replacement before changing a single live binding.
        aliases = {}
        for owner, name, old, new in replacements:
            setattr(owner, name, new)
            if old is not None and not isinstance(owner, type):
                # Existing from-import aliases must point to the same updated seam.
                aliases[id(old)] = (old, new)
        for module in modules:
            namespace = getattr(module, "__dict__", None)
            if isinstance(namespace, dict):
                for alias, value in list(namespace.items()):
                    replacement = aliases.get(id(value))
                    if replacement and value is replacement[0]:
                        namespace[alias] = replacement[1]
        from hermes_cli import plugins, plugins_dispatch
        plugins.VALID_HOOKS.add("on_turn_end")
        plugins_dispatch._HOOK_TIMEOUT_BOUNDED_HOOKS.add("on_turn_end")
        return {"schema_version": 1, "state": "ACTIVATED", "mode": "future_calls_only",
                "rebound": len(replacements), "source_sha256": hashes}
