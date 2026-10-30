"""Review ordinary documentation and commit only the exact reviewed bytes.

No shell, hooks, checkout, push, code staging or task acceptance is exposed.
"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tempfile

from runtime_common import locked, read_json, save_json


MAX_BYTES = 2 * 1024 * 1024


def git(repo, *args, env=None, data=None):
    clean = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    clean.update(env or {})
    return subprocess.check_output(['git', '--no-optional-locks', '-c', 'core.fsmonitor=false',
        '-c', 'core.hooksPath=/dev/null', '-C', str(repo), *args], env=clean,
        input=data, stderr=subprocess.PIPE, timeout=20)


def document_bytes(repo, name):
    path = PurePosixPath(name)
    if (path.is_absolute() or str(path) != name or any(p in {'..', '.git'} for p in path.parts) or not path.parts
            or path.suffix.lower() not in {'.md', '.rst', '.txt'}):
        raise ValueError('ORDINARY_DOCUMENTATION_PATH_REQUIRED')
    if not (path.parts[0] in {'docs', 'doc'} or name in {
            'README.md', 'HANDOFF.md', 'CHANGELOG.md', 'DEVELOPMENT-LOG.md'}):
        raise ValueError('ORDINARY_DOCUMENTATION_PATH_REQUIRED')
    # Instructions, requirements and acceptance contracts need their own change
    # process. A Markdown extension is not authority to change those contracts.
    if any(re.search(r'(^|[-_.])(agents?|skills?|rules?|instructions?|prompts?|prd|ac|adr|'
                     r'requirements?|contracts?|architecture|todo|plans?|planning|design)([-_.]|$)',
                     part, re.I) for part in path.parts):
        raise ValueError('DOCUMENTATION_CONTRACT_OR_INSTRUCTIONS_REQUIRE_SEPARATE_CHANGE')
    current = Path(repo)
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('DOCUMENTATION_SYMLINK_REJECTED')
    fd = os.open(current, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_BYTES
                or info.st_mode & 0o111):
            raise ValueError('DOCUMENTATION_NOT_REGULAR_OR_TOO_LARGE')
        raw = stream.read(MAX_BYTES + 1)
    if not raw or len(raw) > MAX_BYTES or b'\0' in raw:
        raise ValueError('DOCUMENTATION_TEXT_REQUIRED')
    raw.decode('utf-8')
    return raw


def inspect(repo):
    repo = Path(repo).resolve()
    before = git(repo, 'rev-parse', 'HEAD').decode().strip()
    names = set(git(repo, 'diff', '--name-only', '--no-renames', '-z', 'HEAD', '--').decode().split('\0'))
    names.update(git(repo, 'ls-files', '--others', '--exclude-standard', '-z').decode().split('\0'))
    documents, other = [], []
    for name in sorted(names - {''}):
        try:
            raw = document_bytes(repo, name)
            diff = git(repo, 'diff', '--no-ext-diff', '--no-textconv', '--no-renames',
                       'HEAD', '--', name).decode('utf-8', errors='replace')
            documents.append({'path': name, 'sha256': hashlib.sha256(raw).hexdigest(),
                              'content': raw.decode(), 'diff_from_head': diff})
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            other.append({'path': name, 'reason': str(exc)[:160]})
    return {'head': before, 'branch': git(repo, 'symbolic-ref', '--short', 'HEAD').decode().strip(),
            'stable_head': before == git(repo, 'rev-parse', 'HEAD').decode().strip(),
            'documents': documents, 'other_changes': other,
            'policy': '逐份读content/diff核对事实、任务关联、敏感信息及是否改变AC；可确认的普通文档单独提交。'
                      '归属未知不等于不可审核；文件仍在变化时重新读取，不凭后缀自动提交。'}


def commit_reviewed(repo, files, expected_head, expected_branch, summary, directory):
    """Exact tree + CAS ref; preserve every unrelated index/worktree entry.

    The durable intent reconciles a crash between ref and index updates without
    creating a second commit or claiming an unknown side effect succeeded.
    """
    repo = Path(repo).resolve(); directory = Path(directory)
    if (not files or len(files) > 32 or len({v['path'] for v in files}) != len(files)
            or not summary.strip() or len(summary) > 500):
        raise ValueError('DOCUMENTATION_REVIEW_REQUIRED')
    request = {'repo': str(repo), 'files': files, 'expected_head': expected_head,
               'expected_branch': expected_branch, 'summary': summary}
    request_hash = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    intent_path = directory / 'intent.json'
    index = Path(git(repo, 'rev-parse', '--path-format=absolute', '--git-path', 'index').decode().strip())
    lock_path = Path(str(index) + '.lock')
    if intent_path.exists():
        intent = read_json(intent_path)
        if intent['request_hash'] != request_hash:
            raise ValueError('DOCUMENTATION_COMMAND_ID_CONFLICT')
        if intent.get('result'):
            return {**intent['result'], 'replayed': True}
        # Never guess whether a user changed an index/ref after a crash.
        raise ValueError('DOCUMENTATION_SIDE_EFFECT_REQUIRES_RECONCILIATION:' + str(intent_path))
    if git(repo, 'symbolic-ref', '--short', 'HEAD').decode().strip() != expected_branch:
        raise ValueError('DOCUMENTATION_BRANCH_CHANGED')
    if git(repo, 'rev-parse', 'HEAD').decode().strip() != expected_head:
        raise ValueError('DOCUMENTATION_HEAD_CHANGED')
    git(repo, 'check-ref-format', 'refs/heads/' + expected_branch)
    for marker in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply'):
        if Path(git(repo, 'rev-parse', '--git-path', marker).decode().strip()).is_absolute():
            pending = Path(git(repo, 'rev-parse', '--git-path', marker).decode().strip())
        else:
            pending = repo / git(repo, 'rev-parse', '--git-path', marker).decode().strip()
        if pending.exists():
            raise ValueError('DOCUMENTATION_GIT_OPERATION_IN_PROGRESS')
    snapshots = {}
    for entry in files:
        if not entry.get('review', '').strip():
            raise ValueError('DOCUMENTATION_FILE_REVIEW_REQUIRED')
        raw = document_bytes(repo, entry['path'])
        if hashlib.sha256(raw).hexdigest() != entry['sha256']:
            raise ValueError('DOCUMENTATION_CHANGED_AFTER_REVIEW:' + entry['path'])
        mode = git(repo, 'ls-tree', expected_head, '--', entry['path']).split(b' ', 1)[0]
        if mode not in {b'', b'100644'}:
            raise ValueError('DOCUMENTATION_MODE_NOT_SUPPORTED')
        snapshots[entry['path']] = raw
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    published = False
    try:
        original_index = index.read_bytes()
        with tempfile.TemporaryDirectory(dir=directory) as temp:
            commit_index = str(Path(temp) / 'commit-index')
            next_index = str(Path(temp) / 'next-index')
            Path(next_index).write_bytes(original_index)
            git(repo, 'read-tree', expected_head, env={'GIT_INDEX_FILE': commit_index})
            for name, raw in snapshots.items():
                oid = git(repo, 'hash-object', '-w', '--stdin', data=raw).decode().strip()
                for private in (commit_index, next_index):
                    git(repo, 'update-index', '--add', '--cacheinfo', '100644', oid, name,
                        env={'GIT_INDEX_FILE': private})
            tree = git(repo, 'write-tree', env={'GIT_INDEX_FILE': commit_index}).decode().strip()
            if tree == git(repo, 'rev-parse', expected_head + '^{tree}').decode().strip():
                raise ValueError('DOCUMENTATION_NO_CONTENT_CHANGE')
            message = summary.strip() + '\n\nCoordinator-Documentation-Review: ' + request_hash + '\n'
            commit = git(repo, 'commit-tree', tree, '-p', expected_head, data=message.encode()).decode().strip()
            for name, raw in snapshots.items():
                if document_bytes(repo, name) != raw:
                    raise ValueError('DOCUMENTATION_CHANGED_AFTER_REVIEW:' + name)
            if (git(repo, 'symbolic-ref', '--short', 'HEAD').decode().strip() != expected_branch
                    or index.read_bytes() != original_index):
                raise ValueError('DOCUMENTATION_REPOSITORY_CHANGED')
            next_bytes = Path(next_index).read_bytes()
            with os.fdopen(fd, 'wb') as stream:
                fd = None
                stream.write(next_bytes); stream.flush(); os.fsync(stream.fileno())
            save_json(intent_path, {'request_hash': request_hash, 'request': request,
                'commit': commit, 'index_before_sha256': hashlib.sha256(original_index).hexdigest(),
                'index_after_sha256': hashlib.sha256(next_bytes).hexdigest()})
            git(repo, 'update-ref', '-m', 'coordinator: reviewed documentation',
                'refs/heads/' + expected_branch, commit, expected_head)
            published = True
            os.replace(lock_path, index)
        result = {'commit': commit, 'parent': expected_head, 'branch': expected_branch,
                  'repo': str(repo), 'paths': list(snapshots), 'receipt': str(intent_path)}
        save_json(intent_path, {**read_json(intent_path), 'result': result})
        return result
    finally:
        if fd is not None:
            os.close(fd)
        # A post-ref failure leaves the index lock and intent for reconciliation.
        if not published:
            lock_path.unlink(missing_ok=True)


def commit(ledger, identity, payload, *, command_id):
    from business_ledger import _deny
    def operation(db):
        work, _ = ledger._identity(db, identity)
        if work['role'] != 'COORDINATOR':
            _deny('COORDINATOR_REQUIRED')
        run = ledger._one(db, 'runs', 'run_id', work['run_id'])
        repo = Path(run['repo']).resolve()
        try:
            result = commit_reviewed(repo, directory=ledger.path.parent / 'documentation-commits' / key,
                                     **payload)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            intent = ledger.path.parent / 'documentation-commits' / key / 'intent.json'
            _deny(str(exc)[:500], unknown=intent.exists())
        event = ledger._event(db, 'DOCUMENTATION_COMMITTED', result,
            run_id=work['run_id'], work_id=work['work_id'], role=work['role'])
        return {**result, 'event_id': event}
    # Match merge's file-lock -> SQLite ordering. Never wait on the merge lock
    # while holding a database write transaction.
    snapshot = ledger.snapshot()
    work = next((w for w in snapshot['works'] if w['work_id'] == identity['work_id']), None)
    if not work:
        raise ValueError('WORK_NOT_FOUND')
    run = next(r for r in snapshot['runs'] if r['run_id'] == work['run_id'])
    repo = Path(run['repo']).resolve()
    key = hashlib.sha256(command_id.encode()).hexdigest()
    resource = ledger.path.parent / 'merge-locks' / hashlib.sha256(str(repo).encode()).hexdigest()
    with locked(resource.with_suffix('.lock')):
        return ledger._command('commit_documentation', command_id, [identity, payload], operation)
