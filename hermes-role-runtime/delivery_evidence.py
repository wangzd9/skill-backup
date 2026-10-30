"""Mechanical test evidence for the existing operation runner; never grants a business PASS."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

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


def execute_with_evidence(plan, state_path, *, process_hook=None):
    """Wrap, rather than replace, operation_jobs' sole worker and failure handling."""
    from operation_jobs import execute_plan
    from workflow_migration import code_version
    state_path = Path(state_path)
    evidence_path = state_path.with_suffix('.evidence.json')
    with locked(state_path.with_suffix('.evidence.lock')):
        before_path = state_path.with_suffix('.before.json')
        if before_path.exists() or evidence_path.exists():
            raise ValueError('该操作已有取证记录，禁止重新执行；新执行须使用新 operation_id')
        before = snapshot(plan['cwd'], plan['evidence'])
        before['runtime_version_digest'] = code_version()['digest']
        if state_path.resolve().is_relative_to(Path(before['identity']['repository'])):
            raise ValueError('操作状态/日志须保存在代码仓库外，避免取证输出改变待验快照')
        save_json(before_path, before)
        result = execute_plan(plan, state_path, process_hook=process_hook)
        try:
            after = snapshot(plan['cwd'], plan['evidence'])
            after['runtime_version_digest'] = code_version()['digest']
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
               'runtime_version_unchanged': bool(after and before['runtime_version_digest'] == after['runtime_version_digest']),
               'capture_error': error, 'business_pass': False,
               'limits': '只核对 Git 内容、声明输入/配置及所列环境变量；不证明测试覆盖、远端环境或业务验收。'}
        if plan.get('candidate'):
            row['candidate'] = plan['candidate']
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


def parse_test_results(path, *, execution_status='SUCCEEDED', exit_code=0):
    """Read JUnit testcases, checking declared counters without counting suites twice.

    This is mechanical test execution evidence. Test selection and AC coverage
    remain the tester's judgment; zero executed cases never yield a PASS.
    """
    result = {'format': 'junit', 'path': str(Path(path).resolve()), 'sha256': None,
              'tests': 0, 'passed': 0, 'failures': 0, 'errors': 0, 'skipped': 0,
              'executed': 0, 'mechanical_result': 'UNVERIFIED', 'reasons': [], 'first_failure': None}
    try:
        source = Path(path)
        before = source.stat()
        if before.st_size > 8 * 1024 * 1024:
            raise ValueError('TEST_RESULT_TOO_LARGE')
        raw = source.read_bytes()
        after = source.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            raise ValueError('TEST_RESULT_CHANGED_DURING_READ')
        result['sha256'] = hashlib.sha256(raw).hexdigest()
        root = ET.fromstring(raw)
        tag = lambda node: node.tag.rsplit('}', 1)[-1]
        if tag(root) not in ('testsuite', 'testsuites'):
            raise ValueError('INVALID_JUNIT_ROOT')
        cases = [node for node in root.iter() if tag(node) == 'testcase']
        if not any(tag(node) == 'testsuite' for node in root.iter()):
            raise ValueError('JUNIT_SUITE_REQUIRED')

        def counts(nodes):
            value = dict(tests=len(nodes), failures=0, errors=0, skipped=0)
            for case in nodes:
                outcomes = {tag(node) for node in case}
                if len(outcomes & {'failure', 'error', 'skipped'}) > 1:
                    raise ValueError('CONFLICTING_TESTCASE_OUTCOME')
                for child, field in [('failure', 'failures'), ('error', 'errors'), ('skipped', 'skipped')]:
                    value[field] += child in outcomes
            return value

        actual = counts(cases)
        for suite in root.iter():
            if tag(suite) not in ('testsuite', 'testsuites'):
                continue
            observed = counts([node for node in suite.iter() if tag(node) == 'testcase'])
            for key, number in observed.items():
                if key in suite.attrib:
                    declared = suite.attrib[key]
                    if not re.fullmatch(r'\d+', declared) or int(declared) != number:
                        raise ValueError('JUNIT_COUNTER_MISMATCH:' + key)
        result.update(actual)
        result['case_fingerprint']=_hash(sorted((case.get('classname',''),case.get('name',''),
            sorted(tag(child) for child in case if tag(child) in ('failure','error','skipped'))) for case in cases))
        result['executed'] = actual['tests'] - actual['skipped']
        result['passed'] = result['executed'] - actual['failures'] - actual['errors']
        for case in cases:
            failure = next((child for child in case if tag(child) in ('failure', 'error')), None)
            if failure is not None:
                result['first_failure'] = {'name': str(case.get('name', ''))[:240],
                    'classname': str(case.get('classname', ''))[:240],
                    'message': str(failure.get('message') or failure.text or '')[:512]}
                break
        if result['executed'] <= 0:
            result['reasons'].append('ZERO_EXECUTED_TESTS')
        elif execution_status in ('TIMED_OUT', 'CANCELLED', 'RUNNING', 'PENDING', 'UNKNOWN'):
            result['reasons'].append('TEST_EXECUTION_' + execution_status)
        elif actual['failures'] or actual['errors'] or exit_code != 0 or execution_status != 'SUCCEEDED':
            result['mechanical_result'] = 'FAIL'
            result['reasons'].append('TEST_FAILURE_OR_COMMAND_ERROR')
        else:
            result['mechanical_result'] = 'PASS'
    except (OSError, ValueError, ET.ParseError) as exc:
        result['reasons'].append(str(exc) if isinstance(exc, ValueError) else 'MACHINE_TEST_RESULT_UNAVAILABLE:' + type(exc).__name__)
    return result


def check_candidate_test_evidence(evidence, candidate):
    """Recheck the frozen checkout, command/log hashes and exact candidate identity."""
    reasons = []
    for key in ('run_id', 'task_id', 'attempt', 'repo', 'base', 'head', 'ac_version'):
        if evidence.get(key) != candidate.get(key):
            reasons.append('TEST_CANDIDATE_IDENTITY_MISMATCH:' + key)
    runner = evidence.get('runner_evidence') or {}
    try:
        if not runner.get('sha256') or _file_hash(runner['path']) != runner['sha256']:
            raise ValueError('RUNNER_EVIDENCE_CHANGED')
        checked = check_evidence(runner['path'])
        # A real failed test has valid unchanged evidence even though the
        # command cannot meet the old mechanical success predicate.
        execution_reasons = [reason for reason in checked['reasons'] if reason != '测试命令未成功完成']
        reasons.extend(execution_reasons)
        row = read_json(runner['path'])
        identity = (row.get('before') or {}).get('identity', {})
        if identity.get('commit') != candidate.get('head') or identity.get('dirty'):
            reasons.append('TEST_CHECKOUT_HEAD_OR_CONTENT_MISMATCH')
        for key in ('run_id', 'work_id', 'task_id', 'attempt', 'repo', 'base', 'head', 'ac_version', 'candidate_id'):
            if (row.get('candidate') or {}).get(key) != evidence.get(key):
                reasons.append('RUNNER_CANDIDATE_IDENTITY_MISMATCH:' + key)
        stats = parse_test_results(evidence['path'], execution_status=row['execution_status'], exit_code=row['exit_code'])
        stability=evidence.get('stability')
        if stability:
            from workflow_test_environment import check_stability
            stats=check_stability(stats,Path(evidence['path']).parent,pytest=True)
            if (not stats.get('stability_match') or stats.get('stability_evidence')!=stability):
                reasons.append('TEST_STABILITY_EVIDENCE_CHANGED_OR_UNVERIFIED')
        if evidence.get('sha256') != stats.get('sha256'):
            reasons.append('TEST_RESULT_HASH_MISMATCH')
        if evidence.get('exit_code') != row['exit_code']:
            reasons.append('TEST_EXIT_CODE_MISMATCH')
        if stats['mechanical_result'] == 'UNVERIFIED':
            reasons.extend(stats['reasons'])
    except (OSError, ValueError, KeyError, TypeError):
        reasons.append('RUNNER_EVIDENCE_UNAVAILABLE_OR_CHANGED')
    return {'valid': not reasons, 'reasons': sorted(set(reasons))}


def _candidate_test_command(command, *, checkout, artifacts, candidate, evidence, environment=None):
    """Constrain the existing runner's child, never introduce a second runner."""
    sandbox = shutil.which('bwrap')
    if sandbox is None:
        raise ValueError('TEST_SANDBOX_UNAVAILABLE')
    common = Path(_git(candidate['repo'], 'rev-parse', '--path-format=absolute', '--git-common-dir').decode().strip())
    readonly = [Path(candidate['repo']).resolve(), common]
    for kind in ('inputs', 'config'):
        readonly += [(checkout / name).resolve() for name in evidence.get(kind, [])]
    args = [sandbox, '--die-with-parent', '--unshare-all', '--ro-bind', '/', '/',
            '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp', '--tmpfs', '/run', '--clearenv']
    for path in dict.fromkeys(readonly):
        args += ['--ro-bind', str(path), str(path)]
    args += ['--bind', str(checkout), str(checkout), '--bind', str(artifacts), str(artifacts),
             '--remount-ro', '/tmp', '--chdir', str(checkout), '--cap-drop', 'ALL']
    from workflow_test_environment import prepare
    if environment is None:
        command, environment = prepare(command, candidate, checkout, artifacts, pytest=False)
    for key, value in sorted(environment.items()):
        args += ['--setenv', key, value]
    for key in evidence.get('environment', []):
        if key in os.environ and key not in environment:
            args += ['--setenv', key, os.environ[key]]
    args += ['--setenv', 'PYTHONDONTWRITEBYTECODE', '1',
             '--setenv', 'TMPDIR', str(artifacts/'tmp'), '--', *command]
    return args


def execute_candidate_tests(ledger, identity, candidate_id, argv, *, command_id,
                            timeout_seconds=300, supersedes=None, evidence=None, report_format='pytest',
                            verification_profile=None, environment_overrides=None):
    """Test one injected TESTER candidate through the existing operation runner.

    Results and logs stay beside the ledger; the detached checkout is retained
    for verification and recovery. Replaying a command cannot rerun the tests.
    A missing result stays unverified and never publishes a made-up FAIL/PASS.
    """
    from operation_jobs import validate
    if not isinstance(command_id, str) or not command_id or len(command_id) > 240:
        raise ValueError('command_id 必须为稳定非空身份')
    # Inspect an existing identity read-only before reporting NOT_STARTED.
    # Correctable new requests never reserve that identity; a prepared request
    # remains immutable even when its replacement parameters are invalid.
    requested = {'identity':identity,'candidate_id':candidate_id,'argv':argv,
        'timeout_seconds':timeout_seconds,'supersedes':supersedes or [],
        'evidence':evidence if evidence is not None else {},'report_format':report_format}
    if verification_profile:requested['verification_profile']=verification_profile
    existing_folder=ledger.path.parent/'operations'/hashlib.sha256(command_id.encode()).hexdigest()[:32]
    previous=read_json(existing_folder/'request.json',{})
    if previous and previous.get('input_sha256')!=_hash(requested):
        raise ValueError('TEST_COMMAND_ID_CONFLICT')
    invalid=preflight_test_request(argv,timeout_seconds,evidence,report_format,
        verification_profile=verification_profile,environment_overrides=environment_overrides)
    if invalid:
        if previous:
            return {**invalid,'phase':'PREPARED','side_effect_state':'POSSIBLE',
                'new_command_id_required':True,'next_action':'已有冻结请求；核对原执行状态，不得改参重用 command_id。'}
        return invalid
    if not isinstance(argv, list) or not argv or any(not isinstance(v, str) or not v or '\0' in v for v in argv):
        raise ValueError('测试命令必须为完整 argv 列表')
    if report_format not in ('pytest', 'junit'):
        raise ValueError('report_format 仅支持 pytest 或 junit')
    supersedes = supersedes or []
    requested = {'identity': identity, 'candidate_id': candidate_id, 'argv': argv,
                 'timeout_seconds': timeout_seconds, 'supersedes': supersedes or [],
                 'evidence': evidence or {}, 'report_format': report_format}
    if verification_profile:
        requested['verification_profile']=verification_profile
    folder = ledger.path.parent / 'operations' / hashlib.sha256(command_id.encode()).hexdigest()[:32]
    checkout, artifacts = folder/'checkout', folder/'artifacts'
    report = artifacts/'junit.xml'
    state = folder/'result.json'
    folder.mkdir(parents=True, exist_ok=True)
    (artifacts/'tmp').mkdir(parents=True, exist_ok=True)
    with locked(folder/'test.lock'):
        manifest_path = folder/'request.json'
        manifest = read_json(manifest_path, {})
        if manifest:
            if manifest.get('input_sha256') != _hash(requested):
                raise ValueError('TEST_COMMAND_ID_CONFLICT')
            candidate = manifest['candidate']
        else:
            verified = ledger.validate_identity(identity)
            if not verified.get('valid'):
                return verified
            work = verified['work']
            if work['role'] != 'TESTER' or work['kind'] not in ('verification','supplemental_verification','integration_verification') or work['candidate_id'] != candidate_id:
                raise ValueError('INDEPENDENT_TESTER_WORK_REQUIRED')
            from workflow_verification_lifecycle import pending
            with ledger._connection() as db:
                if pending(db,work['work_id']):
                    raise ValueError('VERIFICATION_RETIREMENT_PENDING')
            if work['kind']=='supplemental_verification':
                from workflow_supplemental import get_context
                supplemental_context=get_context(ledger,work)
                if verification_profile!='mysql_migration' or supplemental_context.get('profile')!=verification_profile:
                    raise ValueError('SUPPLEMENTAL_PROFILE_ENTRY_REQUIRED')
                if (supplemental_context.get('candidate_id')!=candidate_id
                        or supplemental_context.get('run_id')!=work['run_id']
                        or supplemental_context.get('task_id')!=work['task_id']
                        or supplemental_context.get('attempt')!=work['attempt']):
                    raise ValueError('SUPPLEMENTAL_PROFILE_BINDING_MISMATCH')
            elif verification_profile and work['kind']!='integration_verification':
                raise ValueError('SUPPLEMENTAL_PROFILE_WORK_REQUIRED')
            if work['kind']=='integration_verification':
                from workflow_completion import context, target_current
                snapshot=ledger.snapshot()
                if candidate_id is not None or verification_profile not in ('integration_local','integration_mysql') or not target_current(ledger,snapshot,work):
                    raise ValueError('INTEGRATION_FIXED_TARGET_REQUIRED')
                candidate={**context(snapshot,work)['target'],'task_id':work['task_id'],
                    'attempt':work['attempt'],'candidate_id':None,'kind':'integration_verification'}
            else:
                candidates = [item for item in ledger.snapshot(work['run_id'])['candidates'] if item['candidate_id'] == candidate_id]
                if len(candidates) != 1 or candidates[0]['state'] != 'CURRENT':
                    raise ValueError('CURRENT_CANDIDATE_REQUIRED')
                candidate = {key: candidates[0][key] for key in ('candidate_id', 'run_id', 'task_id', 'attempt', 'repo', 'base', 'head', 'ac_version')}
            candidate['test_repository']=next(row['repo'] for row in ledger.snapshot()['runs'] if row['run_id']==work['run_id'])
            candidate['work_id'] = work['work_id']
            if work['run_id'] != candidate['run_id'] or work['task_id'] != candidate['task_id'] or work['attempt'] != candidate['attempt']:
                raise ValueError('TEST_WORK_CANDIDATE_IDENTITY_MISMATCH')
            save_json(manifest_path, {'input_sha256': _hash(requested), 'candidate': candidate,'state':'PREPARED'})
        if not checkout.exists():
            _git(candidate['repo'], 'worktree', 'add', '--detach', str(checkout), candidate['head'])
        if _git(checkout, 'rev-parse', 'HEAD').decode().strip() != candidate['head']:
            raise ValueError('TEST_CHECKOUT_HEAD_MISMATCH')
        runner_path = state.with_suffix('.evidence.json')
        if not runner_path.exists():
            live_identity=ledger.validate_identity(identity)
            if not live_identity.get('valid'):return live_identity
            if state.exists() or state.with_suffix('.before.json').exists():
                return {'status': 'UNVERIFIED', 'code': 'TEST_EXECUTION_RECOVERY_REQUIRED', 'state_path': str(state)}
            if _git(checkout, 'status', '--porcelain'):
                raise ValueError('TEST_CHECKOUT_DIRTY')
            if report.exists():
                raise ValueError('TEST_RESULT_ALREADY_EXISTS')
            command = list(argv)
            if report_format == 'pytest':
                # Preserve the caller's pytest plugins/config and test selection;
                # only direct its generated artifacts outside candidate content.
                if not (Path(command[0]).name.startswith('pytest') or '-m' in command and
                        command[command.index('-m') + 1:command.index('-m') + 2] == ['pytest']):
                    raise ValueError('PYTEST_COMMAND_REQUIRED')
                if any(arg.startswith(('--junitxml', '--junit-xml')) for arg in command):
                    raise ValueError('JUnit 输出路径由运行时注入')
                command += ['--junitxml=' + str(report), '-o', 'cache_dir=' + str(artifacts/'pytest-cache')]
            else:
                if not any('{junit_xml}' in arg for arg in command):
                    raise ValueError('JUnit 命令必须含运行时输出占位 {junit_xml}')
                command = [arg.replace('{junit_xml}', str(report)) for arg in command]
            spec = evidence or {}
            validate_evidence(spec)
            from workflow_test_environment import prepare, probe
            try:
                command, environment = prepare(command, candidate, checkout, artifacts, pytest=report_format=='pytest')
                if environment_overrides:
                    if verification_profile not in ('mysql_migration','integration_mysql') or set(environment_overrides)!={'MN01_MYSQL_URL'}:
                        raise ValueError('FIXED_MYSQL_PROFILE_ENVIRONMENT_REQUIRED')
                    environment.update(environment_overrides)
                def wrap(argv):
                    return _candidate_test_command(argv, checkout=checkout, artifacts=artifacts,
                        candidate=candidate, evidence=spec, environment=environment)
                environment_manifest=probe(command, wrap, pytest=report_format=='pytest', path=folder/'environment.json')
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                result = {'status':'UNVERIFIED', 'code':str(exc).split(':')[0] if isinstance(exc,ValueError)
                          else 'TEST_ENVIRONMENT_UNAVAILABLE', 'candidate':candidate,
                          'publication':None, 'business_pass':False, 'repair_candidate':False,
                          'next_action':'保留候选，使用项目测试环境与已安装插件；执行超时用 timeout_seconds。修正 argv 后使用新的 command_id 重试验证。'}
                save_json(folder/'environment-failure.json', result)
                return result
            if report_format=='pytest':
                from workflow_test_environment import stability_command
                command=stability_command(command,artifacts)
                spec={**spec,'config':[*(spec.get('config') or []),
                    str(artifacts/'stability-runner.py'),str(folder/'environment.json'),
                    *dict.fromkeys(path for path in environment_manifest.get('dependencies',[]) if path and Path(path).is_file())]}
            command = wrap(command)
            plan = {'operation_id': 'test-' + folder.name, 'cwd': str(checkout), 'role': 'TESTER',
                    'task': candidate['task_id'], 'attempt': candidate['attempt'], 'steps': [{'argv': command}],
                    'timeout_seconds': timeout_seconds, 'environment': {'PYTHONDONTWRITEBYTECODE': '1'},
                    'evidence': spec, 'candidate': candidate}
            validate(plan)
            def register_process(process):
                from business_ledger import _process
                actual = _process(process.pid)
                if not actual or actual.get('state') == 'unknown':
                    raise ValueError('TEST_PROCESS_IDENTITY_UNVERIFIED')
                tool_id = 'operation-' + plan['operation_id'] + '-' + str(process.pid)
                result = ledger.register_tool_execution(identity, {
                    'tool_id': tool_id, 'pid': process.pid,
                    'process_start': actual['process_start'], 'cgroup': actual['cgroup'],
                }, command_id=tool_id)
                if result.get('status') not in ('COMMITTED', 'DUPLICATE'):
                    raise ValueError(result.get('code', 'TEST_PROCESS_REGISTRATION_FAILED'))
            save_json(folder/'execution-phase.json',{'state':'STARTED','command_id':command_id})
            execute_with_evidence(plan, state, process_hook=register_process)
        row = read_json(runner_path)
        stats = parse_test_results(report, execution_status=row['execution_status'], exit_code=row['exit_code'])
        from workflow_test_environment import classify
        stats = classify(stats, pytest=report_format=='pytest')
        from workflow_test_environment import check_stability
        stats = check_stability(stats,artifacts,pytest=report_format=='pytest')
        if verification_profile in ('mysql_migration','integration_mysql') and stats['skipped']:
            stats['mechanical_result']='UNVERIFIED'
            stats['reasons'].append('REAL_MYSQL_EXECUTION_REQUIRED')
        frozen = {**candidate, 'path': str(report), 'sha256': stats['sha256'], 'exit_code': row['exit_code'],
                  'runner_evidence': {'path': str(runner_path), 'sha256': _file_hash(runner_path)}}
        if verification_profile:
            frozen['verification_profile']=verification_profile
        if stats.get('stability_evidence'):
            frozen['stability']=stats['stability_evidence']
        checked = check_candidate_test_evidence(frozen, candidate)
        result = {'status': 'UNVERIFIED', 'candidate': candidate, 'test_stats': stats,
                  'runner_evidence': frozen['runner_evidence'], 'validation': checked, 'business_pass': False}
        if candidate.get('kind')=='integration_verification':
            from workflow_completion import record_test
            # An unverified setup is retained as a test fact, never candidate
            # PASS/FAIL. The final per-AC delivery decides coverage separately.
            result['publication']=record_test(ledger,identity,candidate,frozen,stats,command_id=command_id)
            result['status']=result['publication']['status']
        elif stats['mechanical_result'] in ('PASS', 'FAIL') and checked['valid']:
            result['publication'] = ledger.publish_verification(identity, candidate_id,
                'supplemental_mysql' if verification_profile else 'independent_test',
                stats['mechanical_result'], frozen, command_id=command_id,
                supersedes=[] if verification_profile and stats['mechanical_result']!='PASS' else supersedes)
            result['status'] = result['publication']['status']
        else:
            result['code'] = 'TEST_RESULT_UNVERIFIED'
        save_json(folder/'verification.json', result)
        save_json(folder/'execution-phase.json',{'state':'RESULT_READY','command_id':command_id})
        return result


def preflight_test_request(argv, timeout_seconds, evidence, report_format, *, verification_profile=None, environment_overrides=None):
    import math
    code=None;field=None
    if not isinstance(argv,list) or not argv or any(not isinstance(v,str) or not v or '\0' in v for v in argv):
        code='INVALID_TEST_ARGV';field='argv'
    elif report_format not in ('pytest','junit'):
        code='INVALID_TEST_REPORT_FORMAT';field='report_format'
    elif type(timeout_seconds) not in (int,float) or not math.isfinite(timeout_seconds) or not 0<timeout_seconds<=86400:
        code='INVALID_TEST_TIMEOUT';field='timeout_seconds'
    elif report_format=='pytest' and not (Path(argv[0]).name.startswith('pytest') or
            '-m' in argv and argv[argv.index('-m')+1:argv.index('-m')+2]==['pytest']):
        code='PYTEST_COMMAND_REQUIRED';field='argv'
    elif report_format=='pytest' and any(arg.startswith(('--junitxml','--junit-xml')) for arg in argv):
        code='JUNIT_PATH_RUNTIME_MANAGED';field='argv'
    elif report_format=='junit' and not any('{junit_xml}' in arg for arg in argv):
        code='JUNIT_PLACEHOLDER_REQUIRED';field='argv'
    if code is None:
        try:validate_evidence({} if evidence is None else evidence)
        except (ValueError,TypeError,AttributeError):code='INVALID_TEST_EVIDENCE';field='evidence'
    if code is None and environment_overrides is not None and (
            not isinstance(environment_overrides,dict) or set(environment_overrides)!={'MN01_MYSQL_URL'}
            or verification_profile not in ('mysql_migration','integration_mysql')
            or not isinstance(environment_overrides.get('MN01_MYSQL_URL'),str)
            or not environment_overrides['MN01_MYSQL_URL'] or '\0' in environment_overrides['MN01_MYSQL_URL']):
        code='FIXED_MYSQL_PROFILE_ENVIRONMENT_REQUIRED';field='environment_overrides'
    if code:
        return {'status':'REJECTED','code':code,'phase':'INVALID_NOT_STARTED','side_effect_state':'NONE',
            'retryable':True,'new_command_id_required':False,'invalid_fields':[field],
            'publication':None,'business_pass':False,
            'next_action':'修正所列参数后可沿用 command_id；尚未冻结请求、建立 checkout 或启动测试。'}
    return None
