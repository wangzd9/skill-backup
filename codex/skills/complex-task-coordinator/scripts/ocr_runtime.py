#!/usr/bin/env python3
"""本机协调者OCR唯一入口：冻结材料、独立systemd服务、只读状态、定向停止。"""

import argparse
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time
import uuid

from ocr_session_status import diagnose
from ocr_results import inspect_results, execution_spec


OCR = Path('/home/wang/.npm-global/bin/ocr')
CONFIG = Path('/home/wang/.opencodereview/config.json')
SESSIONS = Path('/home/wang/.opencodereview/sessions')
GLOBAL_LOCK = Path('/home/wang/.local/state/ocr-runtime/ocr.lock')
PROVIDER = 'vllm-local'
ENDPOINT = 'http://192.168.2.45:8002/v1'
VERSION = '1.6.0'
MODEL_SELECTOR = Path('/home/wang/work/hermes-role-runtime/ocr_model_route.py')
ROUTING_PYTHON = Path('/home/wang/.hermes/hermes-agent/venv/bin/python')
PARAMS = ['--audience', 'agent', '--format', 'json', '--provider', PROVIDER,
          '--timeout', '60', '--max-tokens', '0', '--effort', 'medium', '--concurrency', '1']
SERVICE_KEYS = ['ActiveState', 'SubState', 'MainPID', 'ControlGroup', 'Result', 'ExecMainStatus']


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def atomic_json(path, value):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with temporary.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def run_command(command, cwd=None, timeout=30, env=None):
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, timeout=timeout)
    if result.returncode:
        # 配置可能包含凭据，禁止把第三方原始错误或参数整体回显。
        raise RuntimeError('COMMAND_FAILED: ' + Path(str(command[0])).name)
    return result.stdout


def git(repo, *args):
    return run_command(['git', '-C', str(repo), *args]).decode('utf-8').strip()


def configuration():
    raw = CONFIG.read_bytes()
    value = json.loads(raw)
    provider = value.get('custom_providers', {}).get(PROVIDER, {})
    if provider.get('url', '').rstrip('/') != ENDPOINT:
        raise RuntimeError('ENDPOINT_MISMATCH: 当前配置必须为永久LAN直连；入口不会改写或恢复配置')
    if not provider.get('model'):
        raise RuntimeError('MODEL_MISSING')
    return digest(raw), provider['model']


def environment():
    env = dict(os.environ)
    env['PATH'] = '/home/wang/.local/bin:/home/wang/.npm-global/bin:' + env.get('PATH', '')
    env['OCR_LLM_TIMEOUT'] = '900'
    return env


def select_model_route(project):
    return json.loads(run_command([str(ROUTING_PYTHON), str(MODEL_SELECTOR),
                                  'select', str(project), '--ocr-config', str(CONFIG)]))


def batch_route(spec):
    policy = spec.get('model_routing')
    if not policy:
        return None
    if digest(MODEL_SELECTOR.read_bytes()) != policy['selector_sha256']:
        raise RuntimeError('MODEL_SELECTOR_CHANGED')
    route = select_model_route(Path(spec['project']))
    if not route:
        raise RuntimeError('MODEL_ROUTING_DISABLED: 不回退到固定本地供应商')
    execution_spec(spec, {'execution_route': route})
    return {key: value for key, value in route.items() if key != 'allowed_routes'}


def preview(repo, base, head, excludes=(), background=None):
    command = [str(OCR), 'review', '--from', base, '--to', head, '--preview', '--format', 'json',
               '--provider', PROVIDER]
    if excludes:
        command += ['--exclude', ','.join(excludes)]
    if background:
        command += ['--background-file', str(background)]
    value = json.loads(run_command(command, cwd=repo, env=environment()))
    if not isinstance(value.get('files'), list):
        raise RuntimeError('PREVIEW_SCHEMA_UNSUPPORTED')
    return value


def selected(value):
    return [item['path'] for item in value['files'] if item.get('will_review') is True]


def resolved_rules(repo, base, head, batch, background):
    command = [str(OCR), 'delegate', 'rule', '--from', base, '--to', head, '--format', 'json',
               '--background-file', str(background)]
    if batch['exclude']:
        command += ['--exclude', ','.join(batch['exclude'])]
    command += batch['files']
    value = json.loads(run_command(command, cwd=repo, env=environment()))
    if value.get('schema_version') != '1' or not isinstance(value.get('groups'), list):
        raise RuntimeError('RESOLVED_RULE_SCHEMA_UNSUPPORTED')
    return value


def rules_digest(value):
    return digest(json.dumps(value, sort_keys=True, ensure_ascii=False).encode())


def validate_snapshot(spec):
    repo = Path(spec['repo'])
    if git(repo, 'rev-parse', 'HEAD') != spec['head']:
        raise RuntimeError('HEAD_CHANGED: 必须使用固定交付worktree')
    if git(repo, 'status', '--porcelain', '--untracked-files=no'):
        raise RuntimeError('TRACKED_WORKTREE_DIRTY: 先冻结开发交付')
    if git(repo, 'merge-base', spec['base'], spec['head']) != spec['base']:
        raise RuntimeError('BASE_NOT_ANCESTOR')
    data = run_command(['git', '-C', str(repo), 'diff', '--binary', spec['base'], spec['head']])
    if digest(data) != spec['diff_sha256']:
        raise RuntimeError('DIFF_IDENTITY_CHANGED')


def validate_control(spec):
    value = read_json(Path(spec['project']) / 'ROLE-BINDINGS.json')
    if value.get('control') != 'ACTIVE':
        raise RuntimeError('PROJECT_NOT_ACTIVE')


@contextmanager
def lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('OCR_BUSY: 已有在途锁，不排重复任务') from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def existing_ocr():
    """只匹配实际可执行参数，不将包装器中的命令文本当成OCR进程。"""
    result = []
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            args = (proc / 'cmdline').read_bytes().split(b'\0')
            args = [os.fsdecode(arg) for arg in args if arg]
            direct = args and Path(args[0]).name in ('ocr', 'opencodereview', 'open-code-review')
            node = len(args) > 1 and Path(args[0]).name == 'node' and Path(args[1]).name == 'ocr'
            if (direct and len(args) > 1 and args[1] in ('review', 'r')) or (
                    node and len(args) > 2 and args[2] in ('review', 'r')):
                result.append(int(proc.name))
        except (OSError, ValueError):
            continue
    return result


def load_spec(directory):
    raw = (directory / 'spec.json').read_bytes()
    if digest(raw) != (directory / 'spec.sha256').read_text().strip():
        raise RuntimeError('SPEC_CHANGED: 创建新run，不改冻结材料')
    spec = json.loads(raw)
    version = spec.get('runtime_version')
    if (spec.get('schema') != 'coordinator.ocr-run/v1' or version not in ('1.5.0', VERSION)
            or (version == '1.5.0' and spec.get('model_routing'))):
        raise RuntimeError('SPEC_VERSION_UNSUPPORTED')
    for name, expected in spec['artifacts'].items():
        if digest((directory / name).read_bytes()) != expected:
            raise RuntimeError('ARTIFACT_CHANGED: ' + name)
    return spec


def service_info(unit):
    result = subprocess.run(['systemctl', '--user', 'show', unit, *[
        '--property=' + key for key in SERVICE_KEYS]], capture_output=True, text=True, timeout=15)
    if result.returncode:
        return {'ActiveState': 'unknown', 'Result': 'query_failed'}
    return dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)


def prepare(args):
    directory = args.output.resolve()
    if directory.exists():
        raise RuntimeError('RUN_DIRECTORY_EXISTS: 不覆盖历史，使用新run目录')
    project = args.project.resolve()
    if any(not getattr(args, name).strip() for name in ('task', 'attempt', 'authorization')):
        raise RuntimeError('TASK_ATTEMPT_AUTHORIZATION_REQUIRED')
    if not (project / 'ROLE-BINDINGS.json').is_file():
        raise RuntimeError('PROJECT_BINDING_REQUIRED')
    binding = read_json(project / 'ROLE-BINDINGS.json')
    if binding.get('control') != 'ACTIVE':
        raise RuntimeError('PROJECT_NOT_ACTIVE')
    repo = args.repo.resolve()
    base = git(repo, 'rev-parse', '--verify', args.base + '^{commit}')
    head = git(repo, 'rev-parse', '--verify', args.head + '^{commit}')
    cfg_hash, model = configuration()
    ocr_version = run_command([str(OCR), '--version'], env=environment()).decode().strip()
    if 'v1.13.0' not in ocr_version:
        raise RuntimeError('OCR_VERSION_UNVALIDATED: 先核对新版参数和manifest能力')
    spec = {'schema': 'coordinator.ocr-run/v1', 'runtime_version': VERSION,
            'project': str(project), 'repo': str(repo), 'base': base, 'head': head,
            'task': args.task, 'attempt': args.attempt, 'authorization_source': args.authorization,
            'config_sha256': cfg_hash, 'model': model, 'provider': PROVIDER, 'endpoint': ENDPOINT,
            'ocr_version': ocr_version, 'ocr_launcher_sha256': digest(OCR.read_bytes()),
            'diff_sha256': digest(run_command(['git', '-C', str(repo), 'diff', '--binary', base, head])),
            'created_at': now(), 'global_lock': str(GLOBAL_LOCK),
            'project_lock': str(project / 'inbox/ocr.lock'), 'artifacts': {}}
    route = select_model_route(project)
    if route:
        spec['model_routing'] = {'selector_sha256': digest(MODEL_SELECTOR.read_bytes()),
                                 'allowed_routes': route['allowed_routes']}
    validate_snapshot(spec)
    background = args.background_file.read_bytes()
    if not background.strip():
        raise RuntimeError('BACKGROUND_REQUIRED: 引用原需求/AC、不变量和允许行为')
    all_preview = preview(repo, base, head, background=args.background_file.resolve())
    paths = selected(all_preview)
    if not paths or len(set(paths)) != len(paths):
        raise RuntimeError('NO_REVIEWABLE_FILES_OR_DUPLICATES: 记录排除依据，不执行空审查')
    all_paths = [item['path'] for item in all_preview['files']]
    if any(',' in path or '\n' in path or '*' in path or '?' in path or '[' in path or ']' in path
           or path.startswith('!') for path in all_paths):
        raise RuntimeError('PATH_CANNOT_BE_EXCLUDED_EXACTLY: 工具模式语义需人工适配')
    batches = read_json(args.batches_file) if args.batches_file else [[path] for path in paths]
    if not isinstance(batches, list) or not batches or any(not isinstance(b, list) or not b
                                                        or len(b) > 12 for b in batches):
        raise RuntimeError('INVALID_BATCHES: 每批1至12文件，缺省逐文件')
    flattened = [p for batch in batches for p in batch]
    if len(flattened) != len(set(flattened)) or set(flattened) != set(paths):
        raise RuntimeError('BATCH_COVERAGE_MISMATCH: 必须恰好覆盖所有实际入选文件')
    prepared = []
    for number, files in enumerate(batches, 1):
        excludes = [path for path in all_paths if path not in files]
        value = preview(repo, base, head, excludes, args.background_file.resolve())
        if set(selected(value)) != set(files):
            raise RuntimeError('BATCH_PREVIEW_MISMATCH')
        batch = {'id': f'b{number:02}', 'files': files, 'exclude': excludes, 'preview': value}
        rules = resolved_rules(repo, base, head, batch, args.background_file.resolve())
        batch.update({'resolved_rules': rules, 'rules_sha256': rules_digest(rules)})
        prepared.append(batch)
    identity = digest(json.dumps({'repo': str(repo), 'base': base, 'head': head,
                      'files': sorted(paths), 'background': digest(background), 'config': cfg_hash,
                      'rules': [b['rules_sha256'] for b in prepared],
                      'model_routing': spec.get('model_routing')},
                     sort_keys=True).encode())
    spec['review_identity'] = identity
    spec['retry_depth'] = 0
    reuse_run = getattr(args, 'reuse_run', None)
    retry_reason = getattr(args, 'retry_reason', None)
    if bool(reuse_run) != bool(retry_reason):
        raise RuntimeError('RETRY_REASON_REQUIRED: 同时指定reuse-run和根因处理说明')
    if reuse_run:
        previous_directory = reuse_run.resolve()
        previous = load_spec(previous_directory)
        if previous['review_identity'] != identity:
            raise RuntimeError('REUSE_IDENTITY_MISMATCH: 需求/配置/输入变化后不能复用旧覆盖')
        if previous.get('retry_depth', 0) >= 1:
            raise RuntimeError('RETRY_LIMIT: 同一身份最多初审加一次补审，继续需升级裁决')
        previous_status = status(previous_directory)
        if previous_status['state'] in ('PREPARED', 'SUBMITTED', 'RUNNING', 'UNKNOWN') or (
                previous_status['service'].get('ActiveState') in ('active', 'activating', 'deactivating')):
            raise RuntimeError('REUSE_SOURCE_NOT_STOPPED')
        if previous_status['state'] == 'AWAITING_DECISION':
            raise RuntimeError('ALREADY_REVIEWED: 裁决发现，不重复调用')
        spec.update({'reuse_run': str(previous_directory), 'retry_reason': retry_reason,
                     'retry_depth': previous.get('retry_depth', 0) + 1})
        for batch in prepared:
            for entry in previous_status['recorded_state'].get('batches', []):
                if (entry.get('state') == 'REVIEW_COMPLETE' and entry.get('returncode') == 0
                        and set(entry['files']) == set(batch['files']) and entry.get('session_file')):
                    if assess_session(Path(entry['session_file']), spec, batch,
                                      entry.get('execution_route'))['coverage_complete']:
                        batch['reuse_session'] = entry['session_file']
                        if entry.get('execution_route'):
                            batch['reuse_execution_route'] = entry['execution_route']
    # 所有机械预检成功后才创建材料目录；不调用模型。
    directory.mkdir(parents=True)
    run_id = uuid.uuid4().hex
    spec.update({'run_id': run_id, 'unit': 'coordinator-ocr-' + run_id + '.service',
                 'batches': prepared, 'preview': all_preview,
                 'parameters': PARAMS, 'request_timeout_seconds': 900,
                 'batch_hard_seconds': 3900})
    artifacts = {'background.md': background,
                 'diff.patch': run_command(['git', '-C', str(repo), 'diff', '--binary', base, head]),
                 'ocr_runtime.py': Path(__file__).read_bytes(),
                 'ocr_results.py': Path(__file__).with_name('ocr_results.py').read_bytes(),
                 'ocr_session_status.py': Path(__file__).with_name('ocr_session_status.py').read_bytes()}
    for name, data in artifacts.items():
        (directory / name).write_bytes(data)
        spec['artifacts'][name] = digest(data)
    atomic_json(directory / 'spec.json', spec)
    (directory / 'spec.sha256').write_text(digest((directory / 'spec.json').read_bytes()) + '\n')
    for name in [*artifacts, 'spec.json', 'spec.sha256']:
        (directory / name).chmod(0o444)
    atomic_json(directory / 'state.json', {'state': 'PREPARED', 'updated_at': now(),
                                         'run_id': run_id, 'gate_decision': 'NOT_EVALUATED'})
    return {'state': 'PREPARED', 'run': str(directory), 'unit': spec['unit'],
            'selected': len(paths), 'batches': len(batches), 'model_calls': 0}


def launch(directory):
    spec = load_spec(directory)
    with lock(directory / 'dispatch.lock'):
        state = read_json(directory / 'state.json')
        if state['state'] != 'PREPARED':
            return status(directory)  # 同run永不重复起跑，失败需要新run且实质处理根因。
        info = service_info(spec['unit'])
        if info.get('ActiveState') in ('active', 'activating', 'deactivating'):
            return status(directory)
        if existing_ocr():
            raise RuntimeError('LEGACY_OCR_BUSY: 现有审查仍在途，先观察收口')
        with lock(GLOBAL_LOCK), lock(Path(spec['project_lock'])):
            validate_control(spec)
            validate_snapshot(spec)
            cfg_hash, _ = configuration()
            if cfg_hash != spec['config_sha256']:
                raise RuntimeError('CONFIG_CHANGED: 重新预检，禁止改冻结spec')
            ledger = Path(spec['project']) / 'inbox/control/runtime/ocr' / (spec['review_identity'] + '.json')
            if ledger.exists():
                previous_run = read_json(ledger)['run']
                if previous_run != str(directory) and previous_run != spec.get('reuse_run'):
                    raise RuntimeError('DUPLICATE_REVIEW: 已有同快照/背景/配置的run: ' + previous_run)
            ledger.parent.mkdir(parents=True, exist_ok=True)
            atomic_json(ledger, {'run': str(directory), 'unit': spec['unit'], 'updated_at': now()})
        deadline = len(spec['batches']) * spec['batch_hard_seconds'] + 120
        command = ['systemd-run', '--user', '--no-ask-password', '--unit=' + spec['unit'],
                   '--service-type=exec', '--property=Restart=no', '--property=KillMode=control-group',
                   '--property=TimeoutStopSec=20', '--property=RuntimeMaxSec=' + str(deadline),
                   '--property=StandardOutput=append:' + str(directory / 'service.log'),
                   '--property=StandardError=append:' + str(directory / 'service.log'),
                   '--working-directory=' + spec['repo'],
                   '--setenv=PATH=' + environment()['PATH'],
                   '/usr/bin/python3', str(directory / 'ocr_runtime.py'), '_worker', '--run', str(directory)]
        atomic_json(directory / 'state.json', {'state': 'SUBMITTED', 'run_id': spec['run_id'],
                    'updated_at': now(), 'unit': spec['unit'], 'gate_decision': 'NOT_EVALUATED'})
        try:
            run_command(command)
        except Exception:
            atomic_json(directory / 'state.json', {'state': 'LAUNCH_FAILED', 'updated_at': now(),
                        'gate_decision': 'NOT_EVALUATED'})
            raise
    return status(directory)


def session_files(repo):
    return set((SESSIONS / str(repo).strip('/').replace('/', '-')).glob('*.jsonl'))


def find_session(before, repo, base, head):
    matches = []
    for path in session_files(repo) - before:
        try:
            first = json.loads(path.open(encoding='utf-8').readline())
            if (first.get('type') == 'session_start' and Path(first.get('cwd', '')).resolve() == repo
                    and first.get('diffFrom') == base and first.get('diffTo') == head):
                matches.append(path)
        except (OSError, ValueError):
            continue
    if len(matches) > 1:
        raise RuntimeError('MULTIPLE_SESSIONS: 保留未判覆盖，排查重复入口')
    return matches[0] if matches else None


def assess_session(path, spec, batch, route=None):
    spec = execution_spec(spec, {'execution_route': route})
    value = diagnose(path)
    manifest = {}
    for line in path.read_text().splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get('type') == 'session_end':
            manifest = event.get('run_manifest', {})
    selected_paths = [item.get('path') for item in manifest.get('coverage', {}).get('selected', [])]
    reviewed_input = manifest.get('input', {})
    identities_match = (reviewed_input.get('resolved_base') == spec['base']
                        and reviewed_input.get('resolved_head') == spec['head']
                        and set(selected_paths) == set(batch['files'])
                        and len(selected_paths) == len(batch['files'])
                        and manifest.get('execution', {}).get('provider') == spec['provider']
                        and manifest.get('execution', {}).get('model') == spec['model'])
    value['batch_identity_matches'] = identities_match
    value['coverage_complete'] = value['coverage_complete'] and identities_match
    return value


def worker(directory):
    spec = load_spec(directory)
    # 只能由独立service运行；弱模型不能用_worker绕回terminal子进程。
    cgroup = Path('/proc/self/cgroup').read_text()
    if '/' + spec['unit'] not in cgroup or 'hermes-worker-cron-' in cgroup:
        raise RuntimeError('NOT_IN_DEDICATED_SERVICE')
    cancelled = [False]
    signal.signal(signal.SIGTERM, lambda *_: cancelled.__setitem__(0, True))
    signal.signal(signal.SIGINT, lambda *_: cancelled.__setitem__(0, True))
    result = {'state': 'RUNNING', 'updated_at': now(), 'pid': os.getpid(), 'cgroup': cgroup.strip(),
              'process_start_ticks': Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19],
              'run_id': spec['run_id'], 'unit': spec['unit'], 'batches': [], 'gate_decision': 'NOT_EVALUATED'}
    atomic_json(directory / 'state.json', result)
    child = None
    guards = ExitStack()
    try:
        guards.enter_context(lock(GLOBAL_LOCK))
        guards.enter_context(lock(Path(spec['project_lock'])))
        if existing_ocr():
            raise RuntimeError('LEGACY_OCR_BUSY')
        for batch in spec['batches']:
            if cancelled[0]:
                result['state'] = 'CANCELLED'
                break
            validate_control(spec)
            validate_snapshot(spec)
            if digest(OCR.read_bytes()) != spec['ocr_launcher_sha256']:
                raise RuntimeError('OCR_LAUNCHER_CHANGED')
            cfg_hash, _ = configuration()
            if cfg_hash != spec['config_sha256']:
                raise RuntimeError('CONFIG_CHANGED')
            value = preview(Path(spec['repo']), spec['base'], spec['head'], batch['exclude'],
                            directory / 'background.md')
            if set(selected(value)) != set(batch['files']):
                raise RuntimeError('PREVIEW_CHANGED')
            rules = resolved_rules(Path(spec['repo']), spec['base'], spec['head'], batch,
                                   directory / 'background.md')
            if rules_digest(rules) != batch['rules_sha256']:
                raise RuntimeError('REVIEW_RULES_CHANGED')
            if batch.get('reuse_session'):
                diagnosis = assess_session(Path(batch['reuse_session']), spec, batch,
                                           batch.get('reuse_execution_route'))
                if not diagnosis['coverage_complete']:
                    raise RuntimeError('REUSE_COVERAGE_CHANGED')
                atomic_json(directory / (batch['id'] + '-diagnosis.json'), diagnosis)
                result['batches'].append({'id': batch['id'], 'files': batch['files'],
                    'state': 'REVIEW_COMPLETE', 'returncode': 0, 'reused': True,
                    'session_file': batch['reuse_session']})
                if batch.get('reuse_execution_route'):
                    result['batches'][-1]['execution_route'] = batch['reuse_execution_route']
                atomic_json(directory / 'state.json', result)
                continue
            entry = {'id': batch['id'], 'files': batch['files'], 'state': 'RUNNING', 'started_at': now()}
            route = batch_route(spec)
            if route:
                entry['execution_route'] = route
            result['batches'].append(entry)
            output = directory / (batch['id'] + '.log')
            if output.exists():
                raise RuntimeError('LOG_ALREADY_EXISTS')
            before = session_files(Path(spec['repo']))
            parameters = list(PARAMS)
            if route:
                parameters[parameters.index('--provider') + 1] = route['provider']
                parameters[parameters.index('--effort') + 1] = route['effort']
            command = [str(OCR), 'review', '--from', spec['base'], '--to', spec['head'], *parameters,
                       '--model', route['model'] if route else spec['model'],
                       '--background-file', str(directory / 'background.md')]
            if batch['exclude']:
                command += ['--exclude', ','.join(batch['exclude'])]
            started = time.monotonic()
            with output.open('x') as log:
                child = subprocess.Popen(command, cwd=spec['repo'], env=environment(),
                                         stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                entry['pid'] = child.pid
                entry['process_start_ticks'] = Path(f'/proc/{child.pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
                timed_out = False
                while child.poll() is None:
                    path = find_session(before, Path(spec['repo']), spec['base'], spec['head'])
                    if path:
                        entry['session_file'] = str(path)
                        entry['session_mtime'] = path.stat().st_mtime
                    result['updated_at'] = now()
                    atomic_json(directory / 'state.json', result)
                    if cancelled[0] or time.monotonic() - started > spec['batch_hard_seconds']:
                        timed_out = not cancelled[0]
                        os.killpg(child.pid, signal.SIGTERM)
                        try:
                            child.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            os.killpg(child.pid, signal.SIGKILL)
                        break
                    time.sleep(2)
                child.wait()
            entry.update({'returncode': child.returncode, 'ended_at': now(),
                          'duration_seconds': round(time.monotonic() - started, 2)})
            path = find_session(before, Path(spec['repo']), spec['base'], spec['head'])
            complete = False
            if path:
                entry['session_file'] = str(path)
                diagnosis = assess_session(path, spec, batch, route)
                atomic_json(directory / (batch['id'] + '-diagnosis.json'), diagnosis)
                complete = diagnosis['coverage_complete']
            cfg_hash, _ = configuration()
            if cfg_hash != spec['config_sha256']:
                raise RuntimeError('CONFIG_CHANGED_DURING_REVIEW')
            validate_snapshot(spec)
            if cancelled[0] or timed_out:
                entry['state'] = 'CANCELLED' if cancelled[0] else 'HARD_DEADLINE'
            else:
                entry['state'] = 'REVIEW_COMPLETE' if child.returncode == 0 and complete else 'REVIEW_INCOMPLETE'
            result['updated_at'] = now()
            atomic_json(directory / 'state.json', result)
            if entry['state'] != 'REVIEW_COMPLETE':
                result['state'] = entry['state']
                break  # 失败立即停链，不自动换参/重试/继续后批。
        else:
            result['state'] = 'AWAITING_DECISION'
    except Exception as exc:
        result.update({'state': 'BLOCKED', 'error': str(exc) if isinstance(exc, RuntimeError)
                       else type(exc).__name__})
    finally:
        # 任意诊断/文件读取异常也必须收尾子进程，再结束服务；不能释放锁后留孤儿。
        if child is not None and child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
            except ProcessLookupError:
                child.wait()
        guards.close()
        result.update({'updated_at': now(), 'ended_at': now()})
        atomic_json(directory / 'state.json', result)
    return result


def status(directory):
    spec = load_spec(directory)
    state = read_json(directory / 'state.json')
    info = service_info(spec['unit'])
    status_value = state['state']
    if status_value in ('SUBMITTED', 'RUNNING') and info.get('ActiveState') not in ('active', 'activating'):
        status_value = 'UNKNOWN' if info.get('ActiveState') == 'unknown' else 'INTERRUPTED'
    return {'state': status_value, 'run': str(directory), 'unit': spec['unit'],
            'service': info, 'recorded_state': state, 'gate_decision': 'NOT_EVALUATED',
            'findings_evaluated': False,
            'results_command': ['python3', str(Path(__file__).resolve()), 'results', '--run', str(directory)],
            'note': 'status只证明运行/覆盖状态；接受前必须读取results并逐项裁决真实comments。'}


def stop(directory):
    spec = load_spec(directory)
    run_command(['systemctl', '--user', 'stop', spec['unit']], timeout=30)
    return status(directory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    prepare_parser = sub.add_parser('prepare', help='零模型预检与冻结，默认逐文件')
    for name in ('project', 'repo', 'background-file', 'output'):
        prepare_parser.add_argument('--' + name, type=Path, required=True)
    for name in ('base', 'head', 'task', 'attempt', 'authorization'):
        prepare_parser.add_argument('--' + name, required=True)
    prepare_parser.add_argument('--batches-file', type=Path, help='二维JSON文件清单，必须覆盖全部入选文件')
    prepare_parser.add_argument('--reuse-run', type=Path, help='仅身份完全一致的已退出失败链，可复用成功批')
    prepare_parser.add_argument('--retry-reason', help='说明失败根因、实质处理和仍有效的调用授权')
    for action in ('start', 'status', 'results', 'stop', '_worker'):
        sub.add_parser(action).add_argument('--run', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.action == 'prepare':
            result = prepare(args)
        else:
            directory = args.run.resolve()
            result = {'start': launch, 'status': status, 'results': inspect_results,
                      'stop': stop, '_worker': worker}[args.action](directory)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as exc:
        print(json.dumps({'state': 'BLOCKED', 'error': str(exc) if isinstance(exc, RuntimeError)
                          else type(exc).__name__, 'gate_decision': 'NOT_EVALUATED'}, ensure_ascii=False))
        return 2
    return 2 if result.get('evidence_state') == 'INSUFFICIENT_EVIDENCE' else 0


if __name__ == '__main__':
    raise SystemExit(main())
