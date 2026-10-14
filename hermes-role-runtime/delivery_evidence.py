"""Mechanical test evidence for the existing operation runner; never grants a business PASS."""
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

from runtime_common import locked, now, read_json, save_json


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _git(cwd, *args):
    result = subprocess.run(['git', '-C', str(cwd), *args], capture_output=True, timeout=30)
    if result.returncode:
        # Never echo stderr or commands: they can contain remote credentials.
        raise ValueError('无法读取 Git 快照：' + args[0])
    return result.stdout


def validate_evidence(spec):
    if not isinstance(spec, dict) or set(spec) - {'inputs', 'config', 'environment'}:
        raise ValueError('evidence 仅接受 inputs/config 文件列表和 environment 变量名列表')
    for key in ('inputs', 'config', 'environment'):
        if not isinstance(spec.get(key, []), list) or any(not isinstance(x, str) or not x or '\0' in x for x in spec.get(key, [])):
            raise ValueError('evidence 列表无效：' + key)


def snapshot(cwd, spec):
    """Hash actual bytes, both index/worktree diffs and declared external inputs."""
    validate_evidence(spec)
    cwd = Path(cwd).resolve()
    root = Path(os.fsdecode(_git(cwd, 'rev-parse', '--show-toplevel')).strip()).resolve()
    head = _git(root, 'rev-parse', 'HEAD').decode().strip()
    status = _git(root, 'status', '--porcelain=v1', '-z', '--untracked-files=all')
    cached = _git(root, 'diff', '--binary', '--cached', 'HEAD')
    working = _git(root, 'diff', '--binary')
    names = _git(root, 'ls-files', '--others', '--exclude-standard', '-z')
    untracked = []
    for raw in filter(None, names.split(b'\0')):
        name = os.fsdecode(raw)
        path = root/name
        before = path.lstat()
        digest = hashlib.sha256(os.fsencode(os.readlink(path))).hexdigest() if path.is_symlink() else _file_hash(path)
        after = path.lstat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            raise ValueError('取证过程中未跟踪文件变化，快照不可信')
        untracked.append({'path': name, 'sha256': digest, 'symlink': path.is_symlink()})
    if (head.encode() != _git(root, 'rev-parse', 'HEAD').strip() or
            status != _git(root, 'status', '--porcelain=v1', '-z', '--untracked-files=all') or
            cached != _git(root, 'diff', '--binary', '--cached', 'HEAD') or working != _git(root, 'diff', '--binary') or
            names != _git(root, 'ls-files', '--others', '--exclude-standard', '-z')):
        raise ValueError('取证过程中代码变化，快照不可信')
    files = []
    for kind in ('inputs', 'config'):
        for name in spec.get(kind, []):
            path = (cwd/name).resolve()
            before = path.stat()
            sha = _file_hash(path)
            after = path.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                raise ValueError('取证过程中声明输入变化，快照不可信')
            files.append({'kind': kind, 'path': str(path), 'sha256': sha, 'size': after.st_size})
    variables = {name: os.environ.get(name) for name in sorted(spec.get('environment', []))}
    identity = {'cwd': str(cwd), 'repository': str(root), 'commit': head,
                'dirty': bool(status), 'status_sha256': hashlib.sha256(status).hexdigest(),
                'index_diff_sha256': hashlib.sha256(cached).hexdigest(),
                'worktree_diff_sha256': hashlib.sha256(working).hexdigest(), 'untracked': untracked,
                'files': files, 'environment_names': sorted(variables), 'environment_sha256': _hash(variables),
                'python': sys.version, 'executable': str(Path(sys.executable).resolve()), 'platform': platform.platform()}
    return {'captured_at': now(), 'identity': identity, 'sha256': _hash(identity)}


def execute_with_evidence(plan, state_path):
    """Wrap, rather than replace, operation_jobs' sole worker and failure handling."""
    from operation_jobs import execute_plan
    state_path = Path(state_path)
    evidence_path = state_path.with_suffix('.evidence.json')
    with locked(state_path.with_suffix('.evidence.lock')):
        before_path = state_path.with_suffix('.before.json')
        if before_path.exists() or evidence_path.exists():
            raise ValueError('该操作已有取证记录，禁止重新执行；新执行须使用新 operation_id')
        before = snapshot(plan['cwd'], plan['evidence'])
        if state_path.resolve().is_relative_to(Path(before['identity']['repository'])):
            raise ValueError('操作状态/日志须保存在代码仓库外，避免取证输出改变待验快照')
        save_json(before_path, before)
        result = execute_plan(plan, state_path)
        try:
            after = snapshot(plan['cwd'], plan['evidence'])
            error = None
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            after = None
            error = type(exc).__name__ + ': 快照无法核实'
        log = state_path.with_suffix('.log')
        row = {'schema_version': 1, 'recorded_at': now(), 'operation_id': plan['operation_id'],
               'task': plan.get('task'), 'attempt': plan.get('attempt'), 'role': plan.get('role'),
               'cwd': str(Path(plan['cwd']).resolve()), 'evidence_spec': plan['evidence'],
               'commands': [{'program': Path(s['argv'][0]).name, 'argc': len(s['argv']), 'argv_sha256': _hash(s['argv'])}
                            for s in plan['steps']], 'plan_sha256': _hash(plan),
               'before': before, 'after': after, 'execution_status': result['status'],
               'exit_code': result['exit_code'], 'started_at': result['started_at'], 'ended_at': result['ended_at'],
               'steps': result['steps'], 'state_path': str(state_path.resolve()), 'state_sha256': _file_hash(state_path),
               'log_path': str(log.resolve()), 'log_sha256': _file_hash(log) if log.exists() else None,
               'snapshot_match': bool(after and before['sha256'] == after['sha256']),
               'capture_error': error, 'business_pass': False,
               'limits': '只核对 Git 内容、声明输入/配置及所列环境变量；不证明测试覆盖、远端环境或业务验收。'}
        save_json(evidence_path, row)
        return {**result, 'evidence': str(evidence_path), 'snapshot_match': row['snapshot_match'], 'business_pass': False}


def check_evidence(path):
    row = read_json(path)
    if not isinstance(row, dict) or row.get('schema_version') != 1:
        raise ValueError('取证文件不存在或格式无效')
    reasons = []
    if row.get('execution_status') != 'SUCCEEDED' or row.get('exit_code') != 0:
        reasons.append('测试命令未成功完成')
    if not row.get('snapshot_match'):
        reasons.append('运行前后快照不匹配或不可核实')
    for key in ('state', 'log'):
        try:
            actual = _file_hash(row[key+'_path'])
        except OSError:
            actual = None
        if not row.get(key+'_sha256') or actual != row.get(key+'_sha256'):
            reasons.append(key + ' 文件缺失或已变化')
    try:
        current = snapshot(row['cwd'], row['evidence_spec'])
        if not row.get('after') or current['sha256'] != row['after']['sha256']:
            reasons.append('当前代码/输入/配置/所列环境与测试快照不同')
    except (OSError, ValueError, subprocess.SubprocessError):
        reasons.append('无法核对当前快照')
    return {'checked_at': now(), 'operation_id': row.get('operation_id'), 'mechanical_match': not reasons,
            'execution_status': row.get('execution_status'), 'exit_code': row.get('exit_code'),
            'commit': (row.get('after') or row.get('before') or {}).get('identity', {}).get('commit'),
            'reasons': reasons, 'business_pass': False, 'record': str(Path(path).resolve())}
