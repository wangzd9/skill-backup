"""Validate role bindings and isolate damaged entries across registered projects."""
import argparse
import json
from pathlib import Path

ROLES = frozenset({'COORDINATOR', 'A', 'B', 'C', 'TESTER'})


def inspect_binding(root, value):
    root = Path(root).resolve()
    if (not isinstance(value, dict) or value.get('schema_version') != 1
            or not isinstance(value.get('project_root'), str)
            or Path(value['project_root']).resolve() != root
            or not isinstance(value.get('roles'), dict)
            or not isinstance(value.get('control'), str)):
        raise ValueError('INVALID_PROJECT_BINDING: ' + str(root))
    rows, issues = [], []
    for role, item in value['roles'].items():
        if role not in ROLES:
            issues.append({'project': str(root), 'role': role,
                           'code': 'UNKNOWN_ROLE_OR_AUDIT_RECORD'})
            continue
        problem = None
        if not isinstance(item, dict):
            item, problem = {}, 'ROLE_ENTRY_NOT_OBJECT'
        elif not isinstance(item.get('chat_id'), str) or not item['chat_id'].strip():
            problem = 'ROLE_CHAT_ID_MISSING'
        elif any(key in item for key in ('root', 'role', 'control', 'binding_error')):
            problem = 'ROLE_IDENTITY_OVERRIDE'
        row = {**item, 'root': root, 'role': role, 'control': value['control']}
        if problem:
            row['binding_error'] = problem
            issues.append({'project': str(root), 'role': role, 'code': problem})
        rows.append(row)
    return rows, issues


def registry_bindings(registry, previous=()):
    value = json.loads(Path(registry).read_text())
    if not isinstance(value, dict) or not isinstance(value.get('projects'), list):
        raise ValueError('INVALID_ROLE_REGISTRY')
    rows, issues, roots = [], [], set()
    for entry in value['projects']:
        if not isinstance(entry, dict) or not isinstance(entry.get('project_root'), str):
            raise ValueError('INVALID_ROLE_REGISTRY_ENTRY')
        root = Path(entry['project_root']).resolve()
        if root in roots:
            raise ValueError('DUPLICATE_REGISTERED_PROJECT: ' + str(root))
        roots.add(root)
        try:
            selected, errors = inspect_binding(root, json.loads((root/'ROLE-BINDINGS.json').read_text()))
        except (OSError, ValueError, TypeError) as exc:
            # Retain known identities only as quarantined rows; never retain
            # their former authority when their current binding is unreadable.
            selected = [{**row, 'binding_error': 'PROJECT_BINDING_UNREADABLE'}
                        for row in previous if row.get('root') == root]
            issues.append({'project': str(root), 'code': 'PROJECT_BINDING_UNREADABLE',
                           'error_type': type(exc).__name__,
                           'identity_unknown': not selected or any(not row.get('chat_id') for row in selected)})
            errors = []
        for row in selected:
            if row.get('binding_error'):
                old = next((item for item in previous if item.get('root') == root
                            and item.get('role') == row['role']), {})
                # Last-known IDs are quarantine locators, never old authority.
                for field in ('chat_id', 'poll_job_id', 'report_job_id', 'morning_report_job_id'):
                    if not row.get(field) and old.get(field):
                        row[field] = old[field]
        for issue in errors:
            if issue['code'] != 'UNKNOWN_ROLE_OR_AUDIT_RECORD':
                affected = [row for row in selected if row.get('role') == issue.get('role')]
                issue['identity_unknown'] = not affected or any(not row.get('chat_id') for row in affected)
        rows.extend(selected)
        issues.extend(errors)
    return rows, issues


def validate_candidate(project, candidate, registry=None):
    value = json.loads(Path(candidate).read_text())
    rows, issues = inspect_binding(project, value)
    coordinator = value['roles'].get('COORDINATOR')
    if isinstance(coordinator, dict) and coordinator.get('coordinated_projects'):
        from coordination_inputs import validate_subscriptions
        try:
            validate_subscriptions(project, value, registry=registry)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            issues.append({'project': str(Path(project).resolve()), 'role': 'COORDINATOR',
                           'code': 'INVALID_COORDINATED_PROJECT_SUBSCRIPTION',
                           'error_type': type(exc).__name__})
    if registry:
        existing, other_issues = registry_bindings(registry)
        rows += [row for row in existing if row['root'] != Path(project).resolve()]
        issues += [issue for issue in other_issues if issue['project'] != str(Path(project).resolve())]
    for fields in (('chat_id',), ('poll_job_id', 'report_job_id', 'morning_report_job_id', 'idle_report_job_id')):
        seen = {}
        for row in rows:
            for field in fields:
                identifier = row.get(field)
                if not identifier:
                    continue
                if not isinstance(identifier, str) or identifier in seen:
                    issues.append({'project': str(row['root']), 'role': row['role'],
                                   'code': 'DUPLICATE_OR_INVALID_BINDING_ID', 'field': field})
                else:
                    seen[identifier] = row
    return {'state': 'INVALID' if issues else 'VALID', 'issues': issues,
            'roles': [row['role'] for row in rows if row['root'] == Path(project).resolve()]}


def main():
    parser = argparse.ArgumentParser(description='只读校验待写入的角色绑定及跨项目身份冲突。')
    parser.add_argument('--project', required=True)
    parser.add_argument('--input', required=True)
    parser.add_argument('--registry')
    args = parser.parse_args()
    result = validate_candidate(args.project, args.input, args.registry)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['state'] == 'VALID' else 1


if __name__ == '__main__':
    raise SystemExit(main())
