"""Stable business tool discovery, matched to the unchanged runtime guards."""


def configure(role, *, readonly=False):
    from toolsets import create_custom_toolset
    from workflow_runtime import ROLES
    if role not in ROLES:
        raise ValueError('ROLE_NOT_IN_RUNTIME')
    # Native discovery may add tool_search/describe for exactly this surface.
    # Do not expose manual controls, skill mutation or denied MCP capabilities.
    create_custom_toolset('role_business_read_v1', '角色工作单只读材料及原生查询', tools=[
        'read_file', 'search_files', 'skills_list', 'skill_view', 'role_live_progress',
        'role_repo_state', 'role_requirement_catalog', 'role_work_input'])
    result = ['role_business_read_v1']
    if not readonly:
        create_custom_toolset('role_business_decision_v1', '本工作单只读裁决材料及动作合同', tools=['role_decision_context'])
        result.append('role_business_decision_v1')
        if role in {'A', 'B', 'C'}:
            create_custom_toolset('role_business_edit_v1', '本单开发及自测，仍逐次经过原生范围门禁',
                                  tools=['terminal', 'write_file', 'patch'])
            result.append('role_business_edit_v1')
        result.append({'COORDINATOR': 'role_coordination', 'TESTER': 'role_testing'}.get(role, 'role_development'))
    # This sentinel is the native scheduler's explicit MCP opt-out, not a new
    # permission mechanism. Tool invocation still checks identity and scope.
    return result + ['no_mcp']
