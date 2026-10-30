"""Explicit cross-project read subscriptions; never ownership or action authority."""
import hashlib
import json
from pathlib import Path
import sqlite3

from runtime_common import read_json

REGISTRY = Path.home()/'.hermes/role-projects.json'
SUBSCRIBABLE_ROLES = {'A', 'B', 'C', 'TESTER'}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validate_subscriptions(project, binding, *, registry=None):
    primary = Path(project).resolve()
    values = binding.get('roles', {}).get('COORDINATOR', {}).get('coordinated_projects', [])
    if not isinstance(values, list) or len(values) > 8:
        raise ValueError('coordinated_projects must contain at most eight explicit project subscriptions')
    if not values:
        return []
    registry = read_json(registry or REGISTRY, {}) or {}
    projects = registry.get('projects')
    if not isinstance(projects, list) or any(not isinstance(row, dict) or not isinstance(row.get('project_root'), str)
                                             for row in projects):
        raise ValueError('coordinated project registry is unavailable or invalid')
    registered = {Path(row['project_root']).resolve() for row in projects}
    result, seen = [], set()
    for value in values:
        if not isinstance(value, dict) or set(value) != {'project_root', 'roles'}:
            raise ValueError('coordinated project requires only project_root and roles')
        raw, roles = value['project_root'], value['roles']
        if not isinstance(raw, str) or not Path(raw).is_absolute() or str(Path(raw).resolve()) != raw:
            raise ValueError('coordinated project root must be an absolute canonical path')
        root = Path(raw)
        if root == primary or root in seen or root not in registered:
            raise ValueError('coordinated project must be unique, different from primary, and registered')
        if (not isinstance(roles, list) or not roles or any(not isinstance(role, str) for role in roles)
                or len(roles) != len(set(roles))
                or any(role not in SUBSCRIBABLE_ROLES for role in roles)):
            raise ValueError('coordinated roles must explicitly name unique A/B/C/TESTER roles')
        target = read_json(root/'ROLE-BINDINGS.json', {}) or {}
        if (target.get('schema_version') != 1 or target.get('project_root') != raw or not isinstance(target.get('roles'), dict)
                or any(role not in target['roles'] for role in roles)):
            raise ValueError('coordinated target role binding changed or is unavailable')
        seen.add(root)
        result.append({'project_root': str(root), 'roles': list(roles)})
    return result


def _selected(value, keys):
    return {key: value[key] for key in keys if key in value}


def _brief(value, limit=600):
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit]+'…（按source读原件）'
    if isinstance(value, list): return [_brief(item, limit) for item in value[:8]]
    if isinstance(value, dict): return {key: _brief(item, limit) for key, item in value.items()}
    return value


def _file_fact(root, path):
    source = path.resolve()
    if not source.is_relative_to(root):
        raise ValueError('coordinated evidence is outside its declared project')
    before = source.stat()
    raw = source.read_bytes()
    after = source.stat()
    if ((before.st_ino, before.st_size, before.st_mtime_ns) !=
            (after.st_ino, after.st_size, after.st_mtime_ns)):
        raise ValueError('coordinated evidence changed during collection')
    return {'project_root': str(root), 'source': str(source.relative_to(root)),
            'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw),
            'mtime_ns': after.st_mtime_ns}


def _review_inputs(root):
    """Observe existing independent services without requiring a bound coordinator.

    Imports are lazy: the role observer supplies role input collection and calls
    this only after its own module is loaded. No service submission is possible.
    """
    from role_observer import ocr_observations
    from strong_review import Ledger
    ocr_runs, watch = ocr_observations(root)
    ledger = Ledger()
    strong = ledger.status(str(root))
    call_keys = ('id', 'review_id', 'status', 'created', 'finished', 'result_path', 'spec_path',
                 'unit', 'service_state', 'service_identity', 'wrapper_alive', 'child_alive')
    # Ledger.status exposes the globally serialized active queue. A subscription
    # must never start watching another project's independent review.
    active = [_selected(row, call_keys) for row in strong.get('active_calls', [])
              if row.get('project') == str(root)]
    last = _selected(strong.get('last_call') or {}, call_keys) or None
    semantic = {**_selected(strong, ('authorization', 'grant_id', 'expires_at')),
                'active_calls': active, 'last_call': last}
    # Ordinary rolling-window quota changes and service/log timestamps are not
    # business inputs. A known quota wait does need the available-slot edge.
    checkpoint = read_json(root/'inbox/control/runtime/checkpoints/COORDINATOR.json', {}) or {}
    if checkpoint.get('status') == 'WAITING_REVIEW_QUOTA':
        semantic['quota_available'] = strong.get('used_last_60_minutes', 0) < strong.get('limit', 5)
    watch['strong-review'] = _digest(semantic)
    summary = {**semantic, **_selected(strong, ('used_last_60_minutes', 'limit', 'next_slot_at')),
               'source': str(ledger.database), 'project_root': str(root)}
    return {'ocr_runs': ocr_runs, 'strong_review': summary, 'watch': watch}


def collect_coordinated_inputs(project, binding, collector, *, review_collector=None):
    """Hash declared input changes and return bounded evidence locations, no bodies.

    Target roles cannot be COORDINATOR, so subscriptions never recurse. Disabling
    target index publication is required: the subscriber does not own that role.
    """
    projects, watch, gates = [], {}, []
    for subscription in validate_subscriptions(project, binding):
        root = Path(subscription['project_root'])
        row = {**subscription, 'roles_state': {}, 'test_receipts': [], 'errors': [],
               'reviews': {'ocr_runs': [], 'strong_review': {}}}
        prefix = 'coordinated:'+str(root)+':'
        target_binding = read_json(root/'ROLE-BINDINGS.json', {}) or {}
        if target_binding.get('control') != 'ACTIVE':
            gates.append({'project_root': str(root), 'state': 'BLOCKED',
                          'code': 'COORDINATED_PROJECT_NOT_ACTIVE', 'control': target_binding.get('control')})
        watch[prefix+'binding'] = _digest({'control': target_binding.get('control'),
            'roles': {role: target_binding['roles'][role] for role in subscription['roles']}})
        for role in subscription['roles']:
            try:
                snapshot, target_watch = collector(root, role, publish_indexes=False)
                for source, fingerprint in target_watch.items():
                    watch[prefix+role+':'+source] = fingerprint
                checkpoint = read_json(root/f'inbox/control/runtime/checkpoints/{role}.json', {}) or {}
                cursor = read_json(root/f'inbox/control/runtime/observer/{role}.json', {}) or {}
                pending = cursor.get('pending') or {}
                row['roles_state'][role] = {
                    'project_root': str(root), 'role': role, 'control': snapshot.get('control'),
                    'checkpoint': _brief(_selected(checkpoint, ('task', 'attempt', 'status', 'event_id', 'event',
                        'recorded_at', 'head', 'evidence', 'next', 'pending_actions', 'open_gates'))),
                    'checkpoint_source': 'inbox/control/runtime/checkpoints/'+role+'.json',
                    'latest_event': snapshot.get('events', {}).get(role),
                    'latest_dispatch': snapshot.get('dispatches', {}).get(role),
                    'tasks': snapshot.get('tasks', [])[:12],
                    'task_count': len(snapshot.get('tasks', [])),
                    'pending': {**_selected(pending, ('fingerprint', 'first_seen_at', 'error')),
                                'changed_sources': pending.get('changed_sources', [])[:6],
                                'source': 'inbox/control/runtime/observer/'+role+'.json'},
                    'continuations': [_selected(item, ('continuation_id', 'source', 'input_sha256',
                        'task', 'attempt', 'runnable_now', 'resume_state'))
                        for item in snapshot.get('continuations', [])[:8]],
                    'owner_process': snapshot.get('processes', {}).get(role),
                }
            except (OSError, ValueError, KeyError, TypeError) as exc:
                error = {'role': role, 'code': 'COORDINATED_INPUT_UNVERIFIED',
                         'error': type(exc).__name__+': '+str(exc)[:240]}
                row['errors'].append(error)
                watch[prefix+'collection-error:'+role] = _digest(error)
                gates.append({'project_root': str(root), 'role': role, 'state': 'BLOCKED', **error})
        try:
            reviews = (review_collector or _review_inputs)(root)
            ocr_runs = reviews['ocr_runs']
            row['reviews'] = {'ocr_runs': ocr_runs[-12:], 'ocr_run_count': len(ocr_runs),
                              'strong_review': reviews['strong_review']}
            for source, fingerprint in reviews['watch'].items():
                watch[prefix+'review:'+source] = fingerprint
            terminal = {'REVIEW_COMPLETE', 'AWAITING_DECISION'}
            failed = {'LAUNCH_FAILED', 'CANCELLED', 'HARD_DEADLINE', 'REVIEW_INCOMPLETE',
                      'BLOCKED', 'UNKNOWN', 'INTERRUPTED', 'FAILED'}
            for review in ocr_runs[-12:]:
                state = review.get('state')
                if state not in terminal | failed:
                    continue
                gates.append({'project_root': str(root), 'scope': 'independent_review_run',
                    'state': 'BLOCKED' if state in failed else 'REVIEW_REQUIRED',
                    'code': 'LINKED_OCR_RESULT_MUST_BE_READ',
                    **_selected(review, ('run_id', 'task', 'attempt', 'head', 'source', 'error')),
                    'review_state': state,
                    'instruction': '按该run/task/attempt核对实际结果和门禁决定；历史记录不等于当前候选验收。'})
            strong = row['reviews']['strong_review']
            last = strong.get('last_call') or {}
            if last and last.get('status') not in {'RESERVED', 'RUNNING'}:
                gates.append({'project_root': str(root), 'scope': 'independent_review_call',
                    'state': 'REVIEW_REQUIRED' if last.get('status') == 'SUCCEEDED' else 'BLOCKED',
                    'code': 'LINKED_STRONG_REVIEW_RESULT_MUST_BE_READ',
                    **_selected(last, ('id', 'review_id', 'status', 'result_path', 'spec_path', 'unit')),
                    'source': strong.get('source'),
                    'instruction': '核对调用身份、结果hash及未关闭问题；服务退出或SUCCEEDED标签不能直接视为验收通过。'})
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            error = {'code': 'COORDINATED_REVIEWS_UNVERIFIED',
                     'error': type(exc).__name__+': '+str(exc)[:240]}
            row['errors'].append(error)
            watch[prefix+'review-error'] = _digest(error)
            gates.append({'project_root': str(root), 'state': 'BLOCKED', **error})
        if 'TESTER' in subscription['roles']:
            try:
                receipts = []
                for path in sorted((root/'test-receipts').rglob('*.md')):
                    fact = _file_fact(root, path)
                    watch[prefix+fact['source']] = fact['sha256']
                    receipts.append(fact)
                row['test_receipt_count'] = len(receipts)
                row['test_receipts'] = sorted(receipts, key=lambda item: (item['mtime_ns'], item['source']), reverse=True)[:12]
                gates.append({'project_root': str(root), 'role': 'TESTER', 'state': 'REVIEW_REQUIRED',
                    'code': 'LATEST_LINKED_TEST_EVIDENCE_MUST_BE_READ',
                    'latest_event': row['roles_state'].get('TESTER', {}).get('latest_event'),
                    'receipt_sources': [item['source'] for item in row['test_receipts']],
                    'required_check': 'runtime_tools.py acceptance-check --project '+str(root)+' --input <固定task/repo/head的spec.json>',
                    'instruction': '合入/宣称门禁闭合前，实读该项目最新独立测试事件和对应候选回执；核对head与未解除FAIL/BLOCKED。增量OCR零问题不能关闭未修复的测试失败。'})
            except (OSError, ValueError, KeyError, TypeError) as exc:
                error = {'role': 'TESTER', 'code': 'COORDINATED_RECEIPTS_UNVERIFIED',
                         'error': type(exc).__name__+': '+str(exc)[:240]}
                row['errors'].append(error)
                watch[prefix+'receipt-error'] = _digest(error)
                gates.append({'project_root': str(root), 'state': 'BLOCKED', **error})
        projects.append(row)
    return {'projects': projects, 'watch': watch, 'gates': gates,
            'instructions': '仅为primary协调者的显式只读订阅，不授予目标项目写权限、租约或接管权。来源必须连同project_root解析；未核验目标最新独立测试证据不得宣称门禁闭合。'}


def context_view(value):
    """Keep every declared project/role visible while bounding model input."""
    projects = []
    for project in value['projects']:
        roles = {}
        for role, item in project['roles_state'].items():
            event = item.get('latest_event') or {}
            roles[role] = {'checkpoint_source': item['checkpoint_source'],
                'control': item.get('control'),
                **_selected(item['checkpoint'], ('task', 'attempt', 'status', 'head', 'event', 'recorded_at')),
                'latest_event': {**_brief(_selected(event, ('event_id', 'task', 'attempt', 'status', 'summary', 'evidence')), 240),
                                 'source': event.get('source') or next(iter(event.get('sources') or []), None)},
                'pending_source': item['pending']['source'],
                'continuations': item['continuations'][:2],
                'tasks': [_selected(task, ('task', 'state', 'title', 'owner')) for task in item['tasks'][:4]],
                'task_count': item['task_count'],
                'latest_dispatch': _brief(item.get('latest_dispatch'), 180)}
        projects.append({'project_root': project['project_root'], 'roles': project['roles'],
            'roles_state': roles, 'test_receipts': project['test_receipts'][:4],
            'reviews': {'ocr_runs': [_brief(_selected(review, ('run_id', 'task', 'attempt', 'head', 'source',
                'state', 'gate_decision', 'error')), 180) for review in project['reviews']['ocr_runs'][-6:]],
                'ocr_run_count': project['reviews'].get('ocr_run_count', 0),
                'strong_review': project['reviews']['strong_review']},
            'test_receipt_count': project.get('test_receipt_count', 0), 'errors': project['errors']})
    result = {'projects': projects, 'gates': value['gates'], 'instructions': value['instructions']}
    if len(json.dumps(result, ensure_ascii=False).encode()) > 16*1024:
        result['gates'] = [{**_selected(gate, ('project_root', 'role', 'state', 'code'))}
                           for gate in value['gates']]
        for project in projects:
            project['test_receipts'] = project['test_receipts'][:1]
            project['reviews']['ocr_runs'] = project['reviews']['ocr_runs'][-1:]
            strong = project['reviews']['strong_review']
            project['reviews']['strong_review'] = {**_selected(strong, ('authorization', 'source')),
                'active_calls': [_selected(call, ('id', 'status', 'result_path')) for call in strong.get('active_calls', [])],
                'last_call': _selected(strong.get('last_call') or {}, ('id', 'status', 'result_path'))}
            for item in project['roles_state'].values():
                event = item.pop('latest_event', {})
                item['latest_event_source'] = event.get('source')
                item.pop('latest_dispatch', None)
                item['tasks'] = [_selected(task, ('task', 'state')) for task in item['tasks']]
    if len(json.dumps(result, ensure_ascii=False).encode()) > 16*1024:
        raise ValueError('coordinated context exceeds 16KiB; shorten configured project paths or split explicit subscriptions')
    return result
