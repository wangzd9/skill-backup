"""Native role-group control requests, separate from business ownership."""
import re

from manual_access import full_permission_action


def direct_selection(primary_text, pool_path):
    """Recognize a complete selection command; ordinary questions stay read-only.

    Ambiguous prose is left to the role model and its authorized selection tool.
    Only exact catalog identities are accepted by this mechanical shortcut.
    """
    action = full_permission_action(primary_text)
    if action is None:
        return None
    from requirement_runtime import list_requirements
    catalog = list_requirements(pool_path)
    # Validation, including aliases and path confinement, belongs to the runtime.
    # Returning the original target preserves its identity and user scope.
    if isinstance(catalog, dict):
        catalog = catalog.get('requirements', [])
    values = []
    for row in catalog:
        values.extend(row.get(key) for key in ('id', 'requirement_id', 'title', 'path', 'package_root'))
        values.extend(row.get('aliases', []))
        if row.get('path'): values.append(row['path']+'/README.md')
    from control_intents import selection_plan
    plan = selection_plan(action, set(value for value in values if isinstance(value, str)))
    return plan['target'] if plan else None


def selection_receipt(result):
    state = result.get('state', result.get('status', 'PENDING'))
    target = result.get('requirement_id', result.get('target', '指定需求'))
    if isinstance(target, dict):
        target = target.get('requirement_id', target.get('id', '指定需求'))
    return ('已记录需求切换请求：' + str(target) + '（' + str(state) + '）。'
            '后台将在现有工作到达安全检查点后切换；当前任务、租约和在途操作继续保留。'
            '本消息不会另起一套开发。')
