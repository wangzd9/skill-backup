"""Managed linked worktrees for the exact repository/branch in a merge grant."""
import hashlib
import json
import os
from pathlib import Path
import subprocess


def git(repo, *args):
    return subprocess.check_output(['git', '--no-optional-locks', '-c', 'core.fsmonitor=false',
        '-C', str(repo), *args], stderr=subprocess.DEVNULL, text=True, timeout=20,
        env={k:v for k,v in os.environ.items() if not k.startswith('GIT_')}).strip()


def path_for(ledger, spec):
    key = hashlib.sha256(json.dumps([str(Path(spec['repo']).resolve()), spec['branch']],
        ensure_ascii=False).encode()).hexdigest()
    return ledger.path.parent / 'integration-worktrees' / key / 'checkout'


def common(repo):
    return Path(git(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()


def verify(ledger, spec, receipt, head):
    """A receipt cannot substitute a clone, arbitrary path or unrelated ref."""
    frozen = receipt.get('frozen') or {}
    if frozen.get('destination_repo') != str(Path(spec['repo']).resolve()) or frozen.get('branch') != spec['branch']:
        return False
    location = frozen.get('integration_worktree')
    repo = Path(spec['repo']).resolve()
    if location:
        expected = path_for(ledger, spec)
        target = Path(location)
        if str(target) != str(expected) or target.resolve() != expected or common(target) != common(repo):
            return False
        if git(repo, 'rev-parse', '--verify', 'refs/heads/' + spec['branch']) != head:
            return False
    else:
        target = repo
    return (git(target, 'rev-parse', 'HEAD') == head
        and git(target, 'symbolic-ref', '--short', 'HEAD') == spec['branch']
        and not git(target, 'status', '--porcelain'))


def select(ledger, spec, base):
    """Return a fixed runner plan. Selection itself never changes Git state."""
    repo = Path(spec['repo']).resolve()
    target = path_for(ledger, spec)
    if target.exists():
        if (target.resolve() != target or common(target) != common(repo)
                or git(target, 'symbolic-ref', '--short', 'HEAD') != spec['branch']):
            raise ValueError('INTEGRATION_WORKTREE_IDENTITY_CHANGED')
        if git(target, 'status', '--porcelain') or git(target, 'rev-parse', 'HEAD') != base:
            raise ValueError('MERGE_BASE_CHANGED_OR_DIRTY')
        return target, [], True
    if not spec.get('managed_integration') and not git(repo, 'status', '--porcelain'):
        return repo, [], False
    # A dirty shared tree is never discarded, stashed or committed. The exact
    # authorized ref must be free, or releasable to an identical existing main.
    if spec['branch'] in ('main', 'master'):
        raise ValueError('MERGE_BASE_CHANGED_OR_DIRTY')
    if subprocess.run(['git','check-ref-format','--branch',spec['branch']],capture_output=True).returncode:
        raise ValueError('MERGE_TARGET_BRANCH_INVALID')
    branch = git(repo, 'symbolic-ref', '--short', 'HEAD')
    current = git(repo, 'rev-parse', 'HEAD')
    steps = []
    if branch == spec['branch']:
        try:
            main = git(repo, 'rev-parse', '--verify', 'refs/heads/main')
        except subprocess.SubprocessError as exc:
            raise ValueError('SHARED_BRANCH_RELEASE_REQUIRES_IDENTICAL_MAIN') from exc
        if current != base or main != base:
            raise ValueError('SHARED_BRANCH_RELEASE_REQUIRES_IDENTICAL_MAIN')
        # Identical tips make the existing index valid for main. Only HEAD is
        # rebound; no checkout, file bytes, modes or index entries are changed.
        steps.append({'argv':['git','-C',str(repo),'symbolic-ref','-m',
            'Release identical-base integration branch; preserve shared workspace','HEAD','refs/heads/main']})
    # Existing checkouts outside our managed target are not taken over.
    records = git(repo, 'worktree', 'list', '--porcelain').split('\n\n')
    for record in records:
        if 'branch refs/heads/' + spec['branch'] in record.splitlines():
            if not record.startswith('worktree ' + str(repo) + '\n') or branch != spec['branch']:
                raise ValueError('MERGE_TARGET_IN_OTHER_WORKTREE')
    existing = subprocess.run(['git','-C',str(repo),'rev-parse','--verify','refs/heads/'+spec['branch']],
        capture_output=True,text=True,timeout=20)
    if existing.returncode == 0 and existing.stdout.strip() != base:
        raise ValueError('MERGE_TARGET_BRANCH_CHANGED')
    if existing.returncode not in (0,128):
        raise ValueError('MERGE_TARGET_BRANCH_UNVERIFIED')
    argv=['git','-C',str(repo),'-c','core.hooksPath=/dev/null','worktree','add']
    argv += [str(target),spec['branch']] if existing.returncode == 0 else ['-b',spec['branch'],str(target),base]
    steps.append({'argv':argv})
    return target, steps, True


def completed_head(ledger, snapshot, run_id):
    """The integrated branch, rather than the shared checkout's unrelated HEAD."""
    candidates = {c['candidate_id']: c for c in snapshot['candidates'] if c['run_id'] == run_id}
    grants = {g['authorization_id']: g for g in snapshot['authorizations']}
    completed = {t['task_id'] for t in snapshot['tasks'] if t['run_id']==run_id and t['phase']=='COMPLETED'}
    for row in sorted(snapshot['merges'], key=lambda r:r['created_at'], reverse=True):
        candidate = candidates.get(row['candidate_id'])
        if not candidate or candidate['task_id'] not in completed:
            continue
        try:
            _, raw = ledger._evidence(row['evidence'])
            receipt = json.loads(raw)
            if verify(ledger, grants[row['authorization_id']]['spec'], receipt, row['head']):
                return row['head']
        except (OSError,ValueError,KeyError,subprocess.SubprocessError):
            continue
    return None
