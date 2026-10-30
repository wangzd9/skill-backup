"""Content-addressed OCR admission, isolated from business grants and gates.

No candidate, ledger, service, systemd unit or business OCR lock is used here.
The bundled reference describes a previously audited launcher/protocol, not a
version allowlist. Native code must earn admission with two real review batches.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time
import uuid


CHECKS = ('manifest_schema', 'launcher', 'real_execution', 'parser', 'parameters', 'exit_codes')
FLAGS = ('audience', 'format', 'provider', 'timeout', 'max-tokens', 'effort',
         'concurrency', 'from', 'to', 'background-file')
POLICY = 'ocr.auto-admission/v1'
BASELINE = Path(__file__).with_name('ocr_compat_baseline.json')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def timestamp():
    return datetime.now(timezone.utc).isoformat()


class CompatibilityRejected(RuntimeError):
    def __init__(self, code, detail, *, evidence_path=None):
        super().__init__(code + ': ' + detail)
        self.code = code
        self.details = {'code': code, 'detail': detail, 'evidence_path': evidence_path,
                        'business_side_effects': 0}


def baseline():
    value = json.loads(BASELINE.read_text())
    if value.get('schema') != 'ocr.compat-baseline/v1' or not value.get('files'):
        raise CompatibilityRejected('COMPAT_BASELINE_UNVERIFIABLE', '恢复随运行时代码发布的协议基线')
    return value


def launcher_surface(launcher):
    launcher = Path(launcher).resolve()
    if launcher.name != 'ocr.js' or launcher.parent.name != 'bin':
        raise CompatibilityRejected('LAUNCHER_INCOMPATIBLE', '需要受校验的 npm bin/ocr.js launcher')
    package = launcher.parent.parent
    files = [launcher, *sorted((package/'scripts').glob('*.js'))]
    actual = {str(p.relative_to(package)): sha(p.read_bytes()) for p in files if not p.is_symlink()}
    expected = baseline()['files']
    changed = sorted(p for p in actual.keys() | expected.keys() if actual.get(p) != expected.get(p))
    if changed:
        raise CompatibilityRejected('LAUNCHER_INCOMPATIBLE',
                                    'launcher/scripts 字节变化，需适配并验证协议基线：' + ', '.join(changed))
    return actual


def key_for(identity):
    return sha(canonical([identity['version'], identity['native_binary_sha256'],
                          identity['launcher_sha256']]).encode())


def policy_digest():
    # A changed validator, parser or baseline must not reuse old attestations.
    return sha(b''.join(Path(__file__).with_name(name).read_bytes() for name in
                        ('ocr_compat.py', 'ocr_runtime.py', 'ocr_results.py',
                         'ocr_session_status.py', 'ocr_compat_baseline.json')))


def read_registry(path):
    try:
        value = json.loads(path.read_text())
        if value.get('schema') == POLICY and isinstance(value.get('admissions'), dict):
            return value
    except (OSError, ValueError, AttributeError):
        pass
    # Corrupt/unreadable is a cache miss; publication still has to succeed.
    return {'schema': POLICY, 'admissions': {}}


def cache_valid(record, identity, surface, policy):
    if not isinstance(record, dict):
        return False
    checks = record.get('checks')
    if not isinstance(checks, dict) or any(not isinstance(v, dict) for v in checks.values()):
        return False
    if (record.get('state') != 'ADMITTED' or record.get('policy_sha256') != policy
            or record.get('identity') != identity or record.get('launcher_files') != surface
            or set(record.get('checks', {})) != set(CHECKS)
            or not all(record['checks'][c].get('passed') is True for c in CHECKS)):
        return False
    try:
        root = Path(record['evidence_path'])
        files = record['evidence_sha256']
        if not isinstance(files, dict) or any(not isinstance(n, str) or Path(n).is_absolute()
                                            or '..' in Path(n).parts for n in files):
            return False
        return bool(files) and all(sha((root/name).read_bytes()) == digest
                                   for name, digest in files.items())
    except (OSError, KeyError, TypeError, ValueError):
        return False


@contextmanager
def validation_lock(path, timeout=720):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('a') as stream:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise CompatibilityRejected('COMPAT_VALIDATION_BUSY', '独立校验锁超时，稍后重试预检')
                time.sleep(0.2)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def command(argv, cwd, env, timeout=40):
    """Bounded process tree: a timed-out review cannot escape as an orphan."""
    with subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, start_new_session=True) as process:
        try:
            out, err = process.communicate(timeout=timeout)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            raise
    return subprocess.CompletedProcess(argv, process.returncode, out, err)


def isolated_review(runtime, identity, surface, directory):
    """Exercise the installed identity's immutable image with the real parser."""
    directory.mkdir(parents=True, mode=0o700)
    repo = directory/'repo'; repo.mkdir()
    env = {**runtime.environment(), 'OCR_ENABLE_TELEMETRY': '0', 'NODE_PATH': ''}
    def git(*args):
        value = command(['git', '-C', str(repo), *args], repo, env)
        if value.returncode:
            raise CompatibilityRejected('COMPAT_RUN_FAILED', '隔离样本 Git 准备失败')
        return value.stdout.decode().strip()
    git('init', '-q')
    for label, amount in [('base', 1), ('review', 2)]:
        for name in ('sample_a.py', 'sample_b.py'):
            (repo/name).write_text('def increment(value):\n    return value + ' + str(amount) + '\n')
        git('add', '.')
        git('-c', 'user.name=OCR compatibility', '-c', 'user.email=compat@invalid', 'commit', '-qm', label)
        if label == 'base':
            base = git('rev-parse', 'HEAD')
    head = git('rev-parse', 'HEAD')
    background = directory/'background.md'
    background.write_text('Isolated OCR protocol self-test. Both increment functions intentionally '
                          'change from adding 1 to adding 2. This is the required behavior. '
                          'Review correctness; this is not a business candidate or approval.\n')
    private = directory/'ocr-private'; private.mkdir(mode=0o700)
    config = json.loads(runtime.CONFIG.read_text())
    provider = config.get('custom_providers', {}).get(runtime.PROVIDER)
    if not isinstance(provider, dict) or provider.get('url', '').rstrip('/') != runtime.ENDPOINT:
        raise CompatibilityRejected('COMPAT_RUN_FAILED', '本地校验 provider 配置不可验证')
    (private/'config.json').write_text(json.dumps({'custom_providers': {runtime.PROVIDER: provider}}))
    (private/'config.json').chmod(0o600)
    image = directory/'image'; (image/'bin').mkdir(parents=True); (image/'scripts').mkdir()
    source = runtime.OCR.resolve().parent.parent
    for name, expected in surface.items():
        raw = (source/name).read_bytes()
        if sha(raw) != expected:
            raise CompatibilityRejected('EXECUTION_IDENTITY_MISMATCH', '校验镜像创建时 launcher 变化')
        (image/name).write_bytes(raw)
    shutil.copyfile(source/'package.json', image/'package.json')
    raw = Path(identity['native_binary_path']).read_bytes()
    if sha(raw) != identity['native_binary_sha256']:
        raise CompatibilityRejected('EXECUTION_IDENTITY_MISMATCH', '校验镜像创建时 native binary 变化')
    (image/'bin/opencodereview').write_bytes(raw); (image/'bin/opencodereview').chmod(0o555)
    launcher = image/'bin/ocr.js'; launcher.chmod(0o555)
    # Namespace shadows OCR config/session state. No systemd and no business locks.
    prefix = ['bwrap', '--die-with-parent', '--unshare-pid', '--ro-bind', '/', '/',
              '--bind', str(directory), str(directory), '--ro-bind', str(image), str(image),
              '--bind', str(private), str(Path.home()/'.opencodereview'),
              '--tmpfs', '/tmp', '--dev', '/dev', '--proc', '/proc', '--chdir', str(repo)]
    commands = []
    def invoke(label, args, timeout=40):
        argv = prefix + ['node', str(launcher), *args]
        result = command(argv, repo, env, timeout)
        (directory/(label+'.stdout')).write_bytes(result.stdout)
        (directory/(label+'.stderr')).write_bytes(result.stderr)
        commands.append({'label': label, 'argv': argv, 'returncode': result.returncode})
        runtime.atomic_json(directory/'commands.json', commands)
        return result
    version = invoke('version', ['--version'])
    if version.returncode or version.stdout.decode().strip() != identity['version']:
        raise CompatibilityRejected('EXIT_CODE_INCOMPATIBLE', '--version 失败或镜像身份不一致')
    help_run = invoke('review-help', ['review', '--help'])
    reference = baseline()['review_help']
    actual = help_run.stdout.decode()
    missing = [flag for flag in FLAGS if '--'+flag+' ' not in actual]
    # Compare each used flag's complete type/description/default, not just existence.
    def flag_line(text, flag):
        return next((line.strip() for line in text.splitlines()
                     if re.search(r'(?<![\w-])--'+re.escape(flag)+r'\s', line)), None)
    changed = [flag for flag in FLAGS if flag_line(actual, flag) != flag_line(reference, flag)]
    if help_run.returncode or missing or changed:
        raise CompatibilityRejected('PARAMETER_SURFACE_INCOMPATIBLE',
                                    '缺失参数 '+str(missing)+'；类型/说明/默认值变化 '+str(changed))
    refusal = invoke('invalid-flag', ['review', '--ocr-compat-invalid-flag'])
    if refusal.returncode == 0:
        raise CompatibilityRejected('EXIT_CODE_INCOMPATIBLE', '非法参数拒绝却退出 0')
    checks = {'manifest_schema': {'passed': True, 'schemas': [identity['manifest_schema']]},
              'launcher': {'passed': True, 'files': surface},
              'parameters': {'passed': True, 'flags': list(FLAGS), 'help_sha256': sha(help_run.stdout)},
              'exit_codes': {'passed': True, 'version': version.returncode,
                             'refusal': refusal.returncode, 'success': []}}
    run_id = 'compat-'+uuid.uuid4().hex
    spec = {'schema': 'coordinator.ocr-run/v1', 'run_id': run_id,
            'review_identity': sha((base+head).encode()), 'repo': str(repo), 'base': base, 'head': head,
            'provider': runtime.PROVIDER, 'model': provider['model'], 'ocr_version': identity['version'],
            'artifacts': {}, 'batches': []}
    state = {'run_id': run_id, 'state': 'AWAITING_DECISION', 'batches': []}
    session_ids = []
    model_requests = 0
    for index, path in enumerate(('sample_a.py', 'sample_b.py'), 1):
        bid = 'b%02d' % index
        excluded = 'sample_b.py' if index == 1 else 'sample_a.py'
        args = ['review', '--from', base, '--to', head, *runtime.PARAMS,
                '--model', provider['model'], '--background-file', str(background), '--exclude', excluded]
        result = invoke(bid, args, timeout=360)
        if result.returncode:
            raise CompatibilityRejected('COMPAT_RUN_FAILED', bid+' 真实 review 失败，查看隔离输出')
        from ocr_results import Evidence, log_evidence
        log = directory/(bid+'.log'); log.write_bytes(result.stdout)
        parsed = log_evidence(Evidence(), log)
        sid = parsed.get('session_id')
        if not isinstance(sid, str) or not re.fullmatch(r'[A-Za-z0-9-]{1,100}', sid):
            raise CompatibilityRejected('COMPAT_RUN_FAILED', bid+' 缺失有效 session_id')
        sessions = list((private/'sessions').rglob(sid+'.jsonl'))
        if len(sessions) != 1:
            raise CompatibilityRejected('COMPAT_RUN_FAILED', bid+' session 缺失或重复')
        # Keep only immutable parsing evidence in the receipt. Credentials are never included.
        session = directory/'sessions'/(sid+'.jsonl')
        session.parent.mkdir(exist_ok=True); shutil.copyfile(sessions[0], session)
        diagnosis = runtime.assess_session(session, spec, {'files': [path]})
        runtime.atomic_json(directory/(bid+'-diagnosis.json'), diagnosis)
        manifest = parsed.get('manifest', {})
        if manifest.get('execution', {}).get('configured_concurrency') != 1:
            raise CompatibilityRejected('PARAMETER_SURFACE_INCOMPATIBLE', '--concurrency 1 未在实际执行中生效')
        summary = parsed.get('summary', {})
        events = [json.loads(line) for line in session.read_text().splitlines()]
        requests = [event for event in events if event.get('type') == 'llm_request']
        model_requests += len(requests)
        if not any('Isolated OCR protocol self-test.' in canonical(event) for event in requests):
            raise CompatibilityRejected('PARAMETER_SURFACE_INCOMPATIBLE',
                                        '--background-file 未进入实际模型请求')
        usages = [event.get('usage', {}) for event in events if event.get('type') == 'llm_response']
        valid_usage = (bool(usages) and all(type(summary.get(k)) is int and summary[k] >= 0
                       for k in ('input_tokens', 'output_tokens', 'total_tokens'))
                       and all(all(type(u.get(k)) is int and u[k] >= 0
                                   for k in ('prompt_tokens', 'completion_tokens')) for u in usages))
        if (not valid_usage or summary['input_tokens'] != sum(u['prompt_tokens'] for u in usages)
                or summary['output_tokens'] != sum(u['completion_tokens'] for u in usages)
                or summary['total_tokens'] != summary['input_tokens'] + summary['output_tokens']):
            raise CompatibilityRejected('PARSER_INCOMPATIBLE', bid+' session/summary 用量缺失或不一致')
        spec['batches'].append({'id': bid, 'files': [path]})
        state['batches'].append({'id': bid, 'files': [path], 'state': 'REVIEW_COMPLETE',
                                 'returncode': 0, 'session_file': str(session)})
        checks['exit_codes']['success'].append(result.returncode)
        session_ids.append(sid)
    runtime.atomic_json(directory/'spec.json', spec)
    (directory/'spec.sha256').write_text(sha((directory/'spec.json').read_bytes())+'\n')
    runtime.atomic_json(directory/'state.json', state)
    inspected = runtime.inspect_results(directory)
    runtime.atomic_json(directory/'parser-result.json', inspected)
    if (inspected['evidence_state'] != 'READY_FOR_ADJUDICATION'
            or len(inspected['batches']) != 2 or len(set(session_ids)) != 2):
        raise CompatibilityRejected('PARSER_INCOMPATIBLE', '真实两批产物未通过结果解析器')
    checks['real_execution'] = {'passed': True, 'batch_count': 2, 'session_ids': session_ids,
                                'model_requests': model_requests}
    checks['parser'] = {'passed': True, 'evidence_state': inspected['evidence_state'],
                        'coverage_complete': inspected['coverage_complete']}
    return checks, session_ids


def ensure_admission(runtime, *, state_dir=None):
    state_dir = Path(state_dir) if state_dir is not None else Path.home()/'.hermes'
    registry = state_dir/'ocr-version-admissions.json'
    with validation_lock(state_dir/'ocr-version-validation.lock'):
        identity = None
        directory = None
        try:
            identity = runtime.execution_identity()
            surface = launcher_surface(runtime.OCR)
            key = key_for(identity)
            policy = policy_digest()
            data = read_registry(registry)
            record = data['admissions'].get(key)
            if cache_valid(record, identity, surface, policy):
                return {'record': record, 'cache_hit': True, 'validation_model_calls': 0}
            directory = state_dir/'ocr-compat-evidence'/(key+'-'+uuid.uuid4().hex[:12])
            checks, sessions = isolated_review(runtime, identity, surface, directory)
            if runtime.execution_identity() != identity or launcher_surface(runtime.OCR) != surface:
                raise CompatibilityRejected('EXECUTION_IDENTITY_MISMATCH', '自检期间安装执行体变化，重新预检')
            evidence_files = ('commands.json', 'spec.json', 'spec.sha256', 'state.json', 'parser-result.json',
                              'b01.log', 'b02.log',
                              'b01-diagnosis.json', 'b02-diagnosis.json',
                              'version.stdout', 'review-help.stdout', 'invalid-flag.stderr',
                              *('sessions/'+sid+'.jsonl' for sid in sessions))
            record = {'schema': POLICY, 'state': 'ADMITTED', 'version': identity['version'],
                      'native_binary_sha256': identity['native_binary_sha256'],
                      'launcher_sha256': identity['launcher_sha256'], 'identity': identity,
                      'launcher_files': surface, 'validated_at': timestamp(), 'checks': checks,
                      'validation_run_id': sessions, 'evidence_path': str(directory),
                      'policy_sha256': policy,
                      'evidence_sha256': {name: sha((directory/name).read_bytes()) for name in evidence_files}}
            runtime.atomic_json(directory/'admission.json', record)
            data['admissions'][key] = record
            runtime.atomic_json(registry, data)
            return {'record': record, 'cache_hit': False, 'validation_review_calls': 2,
                    'validation_model_calls': checks['real_execution']['model_requests']}
        except Exception as error:
            failure = error if isinstance(error, CompatibilityRejected) else CompatibilityRejected(
                str(error).split(':', 1)[0] if isinstance(error, runtime.PreflightRejected)
                else 'COMPAT_RUN_FAILED', '版本自检失败：'+type(error).__name__+'；查看隔离证据并修复后重试')
            detected = '未取得版本'
            try:
                detected = json.loads((runtime.OCR.resolve().parent.parent/'package.json').read_text()).get('version', detected)
            except (OSError, ValueError, AttributeError):
                pass
            failure.details.update(version=(identity or {}).get('version', detected),
                                   evidence_path=str(directory) if directory else None,
                                   validation_may_have_model_calls=bool(directory))
            # One durable, actionable alert per content identity and failure, not per tick.
            try:
                alerts_path = state_dir/'ocr-version-alerts.json'
                alerts = read_registry(alerts_path)
                alert_key = sha(canonical([identity, failure.code, failure.details['detail']]).encode())
                first = alert_key not in alerts['admissions']
                alerts['admissions'][alert_key] = failure.details
                runtime.atomic_json(alerts_path, alerts)
                failure.details['alert'] = first
            except OSError:
                failure.details['alert'] = True
            if failure is error:
                raise
            raise failure from error
