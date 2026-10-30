"""Read-only independent-test gate for one exact candidate; never approves a merge."""
import hashlib
import json
from pathlib import Path
import re
import subprocess

FAILURE = re.compile(r'\bFAIL(?:ED|URE)?\b|未通过|失败', re.I)


def _source(root, name):
    path = (root/name).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError('测试回执必须位于目标项目内')
    return path


def _receipts(root, task):
    result = {}
    folder = _source(root, 'test-receipts/'+task)
    for path in sorted(folder.rglob('*')):
        if path.is_file():
            path = _source(root, str(path.relative_to(root)))
            result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def freeze_verification(root, role, value, task):
    """Only the leased independent tester may publish a candidate test verdict.

    This authenticates the role and freezes the actual receipt, not its semantic
    correctness. Test coverage, OCR and review gates remain separate.
    """
    if role != 'TESTER' or not isinstance(value, dict):
        raise ValueError('verification 仅允许独立测试者填写')
    if set(value)-{'head', 'result', 'receipt', 'receipt_sha256', 'receipt_inventory', 'supersedes',
                  'supersedes_event_sha256'}:
        raise ValueError('verification 包含未知字段')
    if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value.get('head', '')):
        raise ValueError('verification.head 必须是完整冻结commit')
    if value.get('result') not in ('PASS', 'FAIL'):
        raise ValueError('verification.result 必须为PASS或FAIL')
    supersedes = value.get('supersedes', [])
    if (not isinstance(supersedes, list) or len(supersedes) > 200
            or any(not isinstance(item, str) or not item.startswith('TESTER:') for item in supersedes)
            or len(set(supersedes)) != len(supersedes)):
        raise ValueError('verification.supersedes 必须为明确的TESTER事件ID')
    frozen_failures = {}
    for event_path in sorted((Path(root)/'events/TESTER').glob('*.json')):
        raw = event_path.read_bytes()
        event = json.loads(raw)
        if event.get('event_id') not in supersedes:
            continue
        if event.get('role') != 'TESTER' or event.get('task') != task or event['event_id'] in frozen_failures:
            raise ValueError('supersedes 旧事件身份冲突或属于其他任务')
        frozen_failures[event['event_id']] = hashlib.sha256(raw).hexdigest()
    if set(frozen_failures) != set(supersedes):
        raise ValueError('supersedes 仅可关闭已存在的同任务TESTER事件，不能预先确认未来输入')
    if value.get('supersedes_event_sha256', frozen_failures) != frozen_failures:
        raise ValueError('supersedes 冻结事件已变化，必须重新核对失败关闭证据')
    if not isinstance(value.get('receipt'), str) or not value['receipt']:
        raise ValueError('verification 缺少实际测试回执')
    path = _source(Path(root).resolve(), value['receipt'])
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if value.get('receipt_sha256', digest) != digest:
        raise ValueError('测试回执hash已变化')
    inventory = _receipts(Path(root).resolve(), task)
    inventory[str(path.relative_to(Path(root).resolve()))] = digest
    if value.get('receipt_inventory', inventory) != inventory:
        raise ValueError('测试回执集合已变化，需要重新核验并发布结果')
    return {**value, 'supersedes': supersedes, 'receipt_sha256': digest,
            'receipt_inventory': inventory, 'supersedes_event_sha256': frozen_failures}


def _inputs(root, task):
    rows, sources = [], {}
    for path in sorted((root/'events/TESTER').glob('*.json')):
        raw = path.read_bytes()
        row = json.loads(raw)
        if row.get('task') != task:
            continue
        sources[str(path.relative_to(root))] = hashlib.sha256(raw).hexdigest()
        rows.append(row)
        verification = row.get('verification') or {}
        if verification.get('receipt'):
            receipt = _source(root, verification['receipt'])
            try:
                sources[str(receipt.relative_to(root))] = hashlib.sha256(receipt.read_bytes()).hexdigest()
            except OSError as exc:
                # Keep absent historical inputs in the fingerprint. Candidate
                # validation still rejects a missing current receipt, while a
                # later valid PASS can close history with fresh evidence.
                sources[str(receipt.relative_to(root))] = 'UNAVAILABLE:'+type(exc).__name__
    sources.update(_receipts(root, task))
    return rows, sources


def check_acceptance(project, spec):
    root = Path(project).resolve()
    if not isinstance(spec, dict) or set(spec)-{'task', 'repo', 'head'}:
        raise ValueError('acceptance spec仅接受task、repo、head')
    task, head = spec.get('task', ''), spec.get('head', '')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,159}', task):
        raise ValueError('无效task身份')
    if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', head):
        raise ValueError('head必须是完整冻结commit，不接受可变分支名')
    repo = Path(spec['repo']).resolve()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args], text=True, timeout=15).strip()
    resolved = git('rev-parse', '--verify', head+'^{commit}')
    if resolved != head:
        raise ValueError('候选commit身份不一致')
    reasons, invalid_candidates = [], []
    initial_status = git('status', '--porcelain')
    initial_head = git('rev-parse', 'HEAD')
    if initial_status:
        reasons.append('CANDIDATE_WORKTREE_DIRTY')
    if initial_head != head:
        reasons.append('CANDIDATE_HEAD_CHANGED')
    rows, sources = _inputs(root, task)
    failures = {row['event_id'] for row in rows if row.get('event_id') and (
        (row.get('verification') or {}).get('result') == 'FAIL'
        or (not row.get('verification') and FAILURE.search(str(row.get('status', ''))+' '+str(row.get('summary', ''))))
        or row.get('open_gates'))}
    candidates = []
    for row in rows:
        item = row.get('verification') or {}
        if item.get('result') != 'PASS' or item.get('head') != head:
            continue
        try:
            if 'receipt_inventory' not in item:
                raise ValueError('缺少测试输入集合')
            if item.get('supersedes') and 'supersedes_event_sha256' not in item:
                raise ValueError('缺少已关闭事件的冻结输入身份')
            verified = freeze_verification(root, 'TESTER', item, task)
        except (OSError, ValueError, TypeError):
            # An obsolete PASS is historical evidence. A later exact PASS may
            # legitimately replace it after the receipt set has changed.
            invalid_candidates.append('TEST_RECEIPT_CHANGED_OR_MISSING:'+str(row.get('event_id')))
            continue
        if row.get('role') != 'TESTER' or row.get('open_gates'):
            continue
        # A new PASS never implicitly erases an older failure, even on an
        # unchanged file or a new attempt. The tester must name each closure.
        if failures - set(verified['supersedes']):
            continue
        candidates.append(row)
    if not candidates:
        reasons.extend(invalid_candidates)
        reasons.append('CURRENT_HEAD_INDEPENDENT_PASS_REQUIRED')
        if failures:
            reasons.append('UNRESOLVED_TEST_FAILURE_OR_GATE')
    # Detect evidence changes during collection; callers must recheck immediately
    # before their existing merge path. This tool does not intercept arbitrary Git.
    _, rechecked = _inputs(root, task)
    if sources != rechecked:
        reasons.append('TEST_INPUT_CHANGED_DURING_CHECK')
    final_head = git('rev-parse', 'HEAD')
    final_status = git('status', '--porcelain')
    if final_head != initial_head or final_head != head:
        reasons.append('CANDIDATE_HEAD_CHANGED_DURING_CHECK')
    if final_status != initial_status:
        reasons.append('CANDIDATE_WORKTREE_CHANGED_DURING_CHECK')
    if final_status:
        reasons.append('CANDIDATE_WORKTREE_DIRTY')
    fingerprint = hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest()
    return {'state': 'BLOCKED' if reasons else 'TEST_GATE_CLEAR', 'project': str(root),
            'task': task, 'head': head, 'input_fingerprint': fingerprint,
            'sources': [{'source': key, 'sha256': value} for key, value in sorted(sources.items())],
            'failure_event_ids': sorted(failures), 'reasons': sorted(set(reasons)),
            'pass_event_ids': [r['event_id'] for r in candidates],
            'historical_invalid_candidates': invalid_candidates,
            'boundary': '仅核对独立测试门禁；仍须原测试覆盖、OCR、强审、授权及合入门禁。'}
