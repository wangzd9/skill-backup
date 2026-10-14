#!/usr/bin/env python3
"""Grant, meter and launch read-only Codex reviews; no task or approval decisions."""
import argparse
import contextlib
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from zoneinfo import ZoneInfo

LIMIT = 5
WINDOW = 3600
STATE = Path.home() / '.local/state/complex-task-reviews'
TZ = ZoneInfo('Asia/Shanghai')


def stamp(value):
    return dt.datetime.fromtimestamp(value, TZ).isoformat(timespec='seconds')


def parse_time(value):
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed.timestamp()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def pid_start(pid):
    try:
        return Path(f'/proc/{int(pid)}/stat').read_text().rsplit(') ', 1)[1].split()[19]
    except (FileNotFoundError, ProcessLookupError):
        return None
    except (OSError, ValueError, IndexError):
        return 'UNKNOWN'


def alive(pid, identity):
    current = pid_start(pid) if pid else None
    return current is not None and (current == 'UNKNOWN' or not identity or current == identity)


def project_binding(project, for_run=False):
    root = Path(project).resolve()
    data = json.loads((root / 'ROLE-BINDINGS.json').read_text())
    if Path(data['project_root']).resolve() != root or 'COORDINATOR' not in data['roles']:
        raise ValueError('INVALID_PROJECT_BINDING')
    if for_run and (data.get('control') != 'ACTIVE' or data.get('migration', {}).get('status') == 'UNBOUND'):
        raise ValueError('PROJECT_NOT_RUNNING: review permission does not start development or bind roles')
    return root, data


class Ledger:
    # Tests inject an isolated directory/clock. Production CLI has no state/quota override.
    def __init__(self, directory=STATE, clock=time.time):
        self.directory = Path(directory)
        self.database = self.directory / 'ledger.sqlite3'
        self.clock = clock

    @contextlib.contextmanager
    def transaction(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        con = sqlite3.connect(self.database, timeout=15)
        con.row_factory = sqlite3.Row
        try:
            con.executescript('''
                CREATE TABLE IF NOT EXISTS grants (
                    id TEXT PRIMARY KEY, project TEXT NOT NULL, source_id TEXT NOT NULL,
                    source_file TEXT NOT NULL, source_hash TEXT NOT NULL, user_quote TEXT NOT NULL,
                    starts REAL NOT NULL, ends REAL, tasks TEXT NOT NULL, model TEXT, repo TEXT NOT NULL,
                    revoked REAL, revoke_reason TEXT, UNIQUE(project, source_id));
                CREATE TABLE IF NOT EXISTS calls (
                    id TEXT PRIMARY KEY, project TEXT NOT NULL, grant_id TEXT NOT NULL,
                    review_id TEXT NOT NULL, task_id TEXT NOT NULL, target TEXT NOT NULL,
                    package_hash TEXT NOT NULL, created REAL NOT NULL, status TEXT NOT NULL,
                    wrapper_pid INTEGER, wrapper_start TEXT, child_pid INTEGER, child_start TEXT,
                    result_path TEXT NOT NULL, log_path TEXT NOT NULL,
                    finished REAL, detail TEXT, usage TEXT, result_hash TEXT);
                CREATE INDEX IF NOT EXISTS calls_created ON calls(created);
                CREATE INDEX IF NOT EXISTS calls_identity ON calls(project, review_id, target, package_hash);
            ''')
            os.chmod(self.database, 0o600)
            con.execute('BEGIN IMMEDIATE')
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def grant(self, project, source_id, source_file, quote, starts, ends, tasks=(), model=None, repo=None):
        source_file = Path(source_file).resolve()
        if not source_id.strip() or not quote.strip() or not source_file.is_file():
            raise ValueError('EXPLICIT_USER_EVIDENCE_REQUIRED')
        if not math.isfinite(starts) or (ends is not None and (not math.isfinite(ends) or ends <= max(starts, self.clock()))):
            raise ValueError('INVALID_AUTHORIZATION_INTERVAL')
        with self.transaction() as con:
            old = con.execute('SELECT * FROM grants WHERE project=? AND source_id=?', (project, source_id)).fetchone()
            if old:
                return dict(old)  # Re-reading one user message never renews authorization.
            con.execute('UPDATE grants SET revoked=?, revoke_reason=? WHERE project=? AND revoked IS NULL',
                        (self.clock(), 'replaced by new explicit user authorization', project))
            key = uuid.uuid4().hex
            con.execute('INSERT INTO grants VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,NULL)',
                        (key, project, source_id, str(source_file), digest(source_file), quote,
                         starts, ends, json.dumps(list(tasks)), model, str(Path(repo or project).resolve())))
            return dict(con.execute('SELECT * FROM grants WHERE id=?', (key,)).fetchone())

    def revoke(self, project, reason):
        with self.transaction() as con:
            n = con.execute('UPDATE grants SET revoked=?, revoke_reason=? WHERE project=? AND revoked IS NULL',
                            (self.clock(), reason, project)).rowcount
        return {'revoked': n, 'reason': reason}

    def _authorization(self, con, project, task_id=None):
        row = con.execute('SELECT * FROM grants WHERE project=? ORDER BY rowid DESC LIMIT 1', (project,)).fetchone()
        now = self.clock()
        if not row or row['revoked'] is not None:
            raise ValueError('NO_ACTIVE_AUTHORIZATION')
        if now < row['starts']:
            raise ValueError('AUTHORIZATION_NOT_STARTED')
        if row['ends'] is not None and now >= row['ends']:
            raise ValueError('AUTHORIZATION_EXPIRED')
        tasks = json.loads(row['tasks'])
        if task_id and tasks and task_id not in tasks:
            raise ValueError('TASK_OUTSIDE_AUTHORIZATION')
        return row

    def reserve(self, project, review_id, task_id, target, package_hash, retry_of=None, repo=None):
        with self.transaction() as con:
            grant = self._authorization(con, project, task_id)
            if repo is not None and grant['repo'] != str(Path(repo).resolve()):
                raise ValueError('REPOSITORY_OUTSIDE_AUTHORIZATION')
            prior = con.execute('SELECT * FROM calls WHERE project=? AND review_id=? AND target=? AND package_hash=? ORDER BY rowid DESC LIMIT 1',
                                (project, review_id, target, package_hash)).fetchone()
            if prior:
                if prior['status'] == 'SUCCEEDED':
                    if not Path(prior['result_path']).is_file() or digest(prior['result_path']) != prior['result_hash']:
                        raise ValueError('RESULT_EVIDENCE_CHANGED: inspect instead of automatically recalling')
                    return {**dict(prior), 'reused': True}
                if prior['status'] in ('RESERVED', 'RUNNING'):
                    raise ValueError('REVIEW_ALREADY_IN_FLIGHT')
                if retry_of != prior['id']:
                    raise ValueError('FAILED_REVIEW_REQUIRES_EXPLICIT_RETRY_OF: no automatic retry loop')
            elif retry_of:
                raise ValueError('RETRY_ID_DOES_NOT_MATCH_REVIEW')
            if con.execute("SELECT 1 FROM calls WHERE status IN ('RESERVED','RUNNING') LIMIT 1").fetchone():
                raise ValueError('GLOBAL_REVIEW_BUSY: check the existing process, do not launch another')
            count = con.execute('SELECT count(*) FROM calls WHERE created>?', (self.clock() - WINDOW,)).fetchone()[0]
            if count >= LIMIT:
                raise ValueError('ROLLING_HOUR_LIMIT: maximum five reserved review starts across projects')
            key = uuid.uuid4().hex
            output_dir = Path(project) / 'reviews/strongmodel/runs' / key
            con.execute('''INSERT INTO calls
                (id,project,grant_id,review_id,task_id,target,package_hash,created,status,
                 wrapper_pid,wrapper_start,result_path,log_path)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (key, project, grant['id'], review_id, task_id, target, package_hash, self.clock(), 'RESERVED',
                 os.getpid(), pid_start(os.getpid()), str(output_dir / 'result.md'), str(output_dir / 'codex.jsonl')))
            return {**dict(con.execute('SELECT * FROM calls WHERE id=?', (key,)).fetchone()), 'model': grant['model'], 'reused': False}

    def launch(self, call_id, launch_process):
        # Revoke and actual subprocess start serialize under the same global transaction.
        with self.transaction() as con:
            call = con.execute('SELECT * FROM calls WHERE id=?', (call_id,)).fetchone()
            if not call or call['status'] != 'RESERVED':
                raise ValueError('CALL_NOT_RESERVED')
            grant = self._authorization(con, call['project'], call['task_id'])
            if grant['id'] != call['grant_id']:
                raise ValueError('AUTHORIZATION_REPLACED_BEFORE_LAUNCH')
            project_binding(call['project'], for_run=True)
            child = launch_process()
            con.execute('UPDATE calls SET status=?,child_pid=?,child_start=? WHERE id=?',
                        ('RUNNING', child.pid, pid_start(child.pid), call_id))
            return child

    def finish(self, call_id, status, detail, usage=None):
        with self.transaction() as con:
            call = con.execute('SELECT * FROM calls WHERE id=?', (call_id,)).fetchone()
            if not call:
                raise ValueError('UNKNOWN_CALL')
            result_hash = digest(call['result_path']) if status == 'SUCCEEDED' else None
            con.execute('UPDATE calls SET status=?,finished=?,detail=?,usage=?,result_hash=? WHERE id=?',
                        (status, self.clock(), detail, json.dumps(usage), result_hash, call_id))

    def recover(self, call_id, evidence):
        evidence = Path(evidence).resolve()
        if not evidence.is_file():
            raise ValueError('STOPPED_PROCESS_EVIDENCE_REQUIRED')
        with self.transaction() as con:
            call = con.execute('SELECT * FROM calls WHERE id=?', (call_id,)).fetchone()
            if not call or call['status'] not in ('RESERVED', 'RUNNING'):
                raise ValueError('NO_UNRESOLVED_CALL')
            if alive(call['wrapper_pid'], call['wrapper_start']) or alive(call['child_pid'], call['child_start']):
                raise ValueError('REVIEW_PROCESS_STILL_ALIVE_OR_UNVERIFIABLE')
            con.execute('UPDATE calls SET status=?,finished=?,detail=? WHERE id=?',
                        ('LOST', self.clock(), 'verified stopped; inspect partial result: ' + str(evidence), call_id))
        return {'recovered': call_id, 'quota_refunded': False}

    def status(self, project):
        empty = {'project': project, 'authorization': 'MANUAL_ONLY', 'used_last_60_minutes': 0,
                 'limit': LIMIT, 'active_calls': [], 'last_call': None, 'tokens': None, 'cost': None}
        if not self.database.exists():
            return empty
        con = sqlite3.connect(self.database.as_uri() + '?mode=ro', uri=True, timeout=15)
        con.row_factory = sqlite3.Row
        try:
            grant = con.execute('SELECT * FROM grants WHERE project=? ORDER BY rowid DESC LIMIT 1', (project,)).fetchone()
            calls = con.execute('SELECT created FROM calls WHERE created>? ORDER BY created DESC', (self.clock() - WINDOW,)).fetchall()
            active = [dict(row) for row in con.execute("SELECT id,project,review_id,status,created FROM calls WHERE status IN ('RESERVED','RUNNING')")]
            last = con.execute('SELECT id,review_id,status,created,finished,result_path,usage FROM calls WHERE project=? ORDER BY rowid DESC LIMIT 1', (project,)).fetchone()
            result = {**empty, 'used_last_60_minutes': len(calls), 'active_calls': active,
                      'next_slot_at': stamp(calls[LIMIT - 1]['created'] + WINDOW) if len(calls) >= LIMIT else None,
                      'last_call': dict(last) if last else None}
            if grant:
                state = 'REVOKED' if grant['revoked'] is not None else 'SCHEDULED' if self.clock() < grant['starts'] else 'EXPIRED' if grant['ends'] is not None and self.clock() >= grant['ends'] else 'AUTHORIZED'
                _, binding = project_binding(project)
                if binding.get('control') == 'COMPLETE':
                    state = 'SCOPE_COMPLETE'
                elif binding.get('control') != 'ACTIVE':
                    state = 'PROJECT_' + str(binding.get('control'))
                result.update({'authorization': state, 'grant_id': grant['id'], 'starts_at': stamp(grant['starts']),
                               'expires_at': stamp(grant['ends']) if grant['ends'] is not None else 'until authorized task scope completes',
                               'task_scope': json.loads(grant['tasks']), 'source_id': grant['source_id'],
                               'repository': grant['repo'],
                               'calls_under_authorization': con.execute('SELECT count(*) FROM calls WHERE grant_id=?', (grant['id'],)).fetchone()[0]})
            if last and last['usage']:
                result['tokens'] = json.loads(last['usage'])
            return result
        finally:
            con.close()


def git(repo, *args):
    return subprocess.check_output(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(repo), *args], text=True, stderr=subprocess.PIPE).strip()


def run_review(args, ledger):
    project, _ = project_binding(args.project, for_run=True)
    repo = Path(args.repo).resolve()
    package = Path(args.package).resolve()
    if not package.is_relative_to(project) or not package.is_file():
        raise ValueError('REVIEW_PACKAGE_MUST_BE_IN_BOUND_PROJECT')
    for text in (args.risk, args.question, args.ac, args.gate):
        if not text.strip():
            raise ValueError('SPECIFIC_RISK_QUESTION_AC_AND_GATE_REQUIRED')
    base, target = [git(repo, 'rev-parse', '--verify', '--end-of-options', value + '^{commit}') for value in (args.base, args.target)]
    package_bytes = package.read_bytes()
    package_text = package_bytes.decode()
    call = ledger.reserve(str(project), args.review_id, args.task_id, target, hashlib.sha256(package_bytes).hexdigest(), args.retry_of, str(repo))
    if call['reused']:
        return call
    folder = Path(call['result_path']).parent
    snapshot = folder / 'snapshot'
    temporary_result = folder / 'result.tmp'
    try:
        folder.mkdir(parents=True, exist_ok=False)
        git(repo, 'worktree', 'add', '--detach', str(snapshot), target)
        reviewer = Path(__file__).resolve().parents[2] / 'complex-task-reviewer/SKILL.md'
        prompt = '\n\n'.join([
            reviewer.read_text(),
            'You are a read-only reviewer authorized by the user. No code edits, model delegation, merges, deployments or extra review calls.',
            json.dumps({'review_id': args.review_id, 'grant_id': call['grant_id'], 'call_id': call['id'],
                        'base': base, 'target': target, 'task': args.task_id, 'gate': args.gate,
                        'risk': args.risk, 'question': args.question, 'AC': args.ac}, ensure_ascii=False),
            'Return the complete independent result as your final message; the wrapper writes it. Do not try to write a result file in the read-only sandbox. Freeze existing requirements; OUT_OF_SCOPE suggestions do not block.',
            'The following is the frozen review package. Read actual code in this detached snapshot and the cited verification evidence; do not merely trust the summary.',
            package_text])
        (folder / 'input.md').write_text(prompt)
        argv = ['codex', 'exec', '--sandbox', 'read-only', '-c', 'approval_policy="never"',
                '-C', str(snapshot), '--json', '--output-last-message', str(temporary_result)]
        if call.get('model'):
            argv += ['--model', call['model']]
        argv += ['-']
        with Path(call['log_path']).open('wb') as log:
            child = ledger.launch(call['id'], lambda: subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=log,
                                                                       stderr=subprocess.STDOUT, start_new_session=True))
            try:
                child.communicate(prompt.encode(), timeout=args.timeout_seconds)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
                raise ValueError('REVIEW_TIMEOUT: failed start remains in hourly count')
        if child.returncode != 0 or not temporary_result.is_file() or not temporary_result.read_text().strip():
            raise ValueError('REVIEW_FAILED_OR_EMPTY_RESULT: inspect log, never assume PASS')
        if git(snapshot, 'rev-parse', 'HEAD') != target or git(snapshot, 'diff', '--name-only', 'HEAD'):
            raise ValueError('REVIEW_SNAPSHOT_CHANGED')
        os.replace(temporary_result, call['result_path'])
        usage = []
        for line in Path(call['log_path']).read_text(errors='replace').splitlines():
            try:
                event = json.loads(line)
                if event.get('type') == 'turn.completed' and isinstance(event.get('usage'), dict):
                    usage.append(event['usage'])
            except (ValueError, AttributeError):
                continue
        ledger.finish(call['id'], 'SUCCEEDED', 'CLI result received; coordinator must still verify snapshot and findings', usage or None)
        return {**call, 'status': 'SUCCEEDED', 'usage': usage or None, 'cost': None, 'approval': 'NOT_DECIDED'}
    except Exception as exc:
        ledger.finish(call['id'], 'FAILED', str(exc))
        raise
    finally:
        if snapshot.exists():
            # Only remove the snapshot created by this call; never reset the source worktree.
            try:
                git(repo, 'worktree', 'remove', str(snapshot))
            except subprocess.CalledProcessError:
                pass  # Retain unexpected files for inspection; do not force-remove them.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    grant = commands.add_parser('grant')
    grant.add_argument('--project', required=True)
    grant.add_argument('--repo', required=True)
    grant.add_argument('--source-id', required=True)
    grant.add_argument('--source-file', required=True)
    grant.add_argument('--user-quote', help='Optional exact excerpt; by default read the original message from source-file')
    grant.add_argument('--starts-at')
    interval = grant.add_mutually_exclusive_group(required=True)
    interval.add_argument('--duration-minutes', type=float)
    interval.add_argument('--until')
    interval.add_argument('--until-project-complete', action='store_true')
    grant.add_argument('--task-ids', nargs='*', default=[])
    grant.add_argument('--model')
    for name in ['status', 'revoke']:
        part = commands.add_parser(name)
        part.add_argument('--project', required=True)
        if name == 'revoke':
            part.add_argument('--reason', required=True)
    run = commands.add_parser('run')
    for key in ['project', 'repo', 'review-id', 'task-id', 'base', 'target', 'package', 'risk', 'question', 'ac', 'gate']:
        run.add_argument('--' + key, required=True)
    run.add_argument('--decision', choices=['REVIEW_NOW'], required=True)
    run.add_argument('--retry-of')
    run.add_argument('--timeout-seconds', type=int, default=1800)
    recover = commands.add_parser('recover')
    recover.add_argument('--call-id', required=True)
    recover.add_argument('--evidence', required=True)
    args = parser.parse_args()
    try:
        ledger = Ledger()
        if args.action == 'grant':
            project, _ = project_binding(args.project)
            starts = parse_time(args.starts_at) if args.starts_at else ledger.clock()
            if args.duration_minutes is not None and (not math.isfinite(args.duration_minutes) or args.duration_minutes <= 0):
                raise ValueError('DURATION_MUST_BE_FINITE_AND_POSITIVE')
            ends = starts + args.duration_minutes * 60 if args.duration_minutes is not None else parse_time(args.until) if args.until else None
            repo = Path(args.repo).resolve()
            git(repo, 'rev-parse', '--show-toplevel')
            quote = args.user_quote if args.user_quote is not None else Path(args.source_file).read_text()
            result = ledger.grant(str(project), args.source_id, args.source_file, quote, starts, ends, args.task_ids, args.model, str(repo))
        elif args.action == 'run':
            if args.timeout_seconds <= 0:
                raise ValueError('TIMEOUT_MUST_BE_POSITIVE')
            result = run_review(args, ledger)
        elif args.action == 'recover':
            result = ledger.recover(args.call_id, args.evidence)
        else:
            project, _ = project_binding(args.project)
            result = ledger.status(str(project)) if args.action == 'status' else ledger.revoke(str(project), args.reason)
        print(json.dumps({'success': True, **result}, ensure_ascii=False))
    except (ValueError, OSError, KeyError, sqlite3.Error, subprocess.CalledProcessError) as exc:
        print(json.dumps({'success': False, 'error': str(exc)}, ensure_ascii=False))
        sys.exit(1)


if __name__ == '__main__':
    main()
