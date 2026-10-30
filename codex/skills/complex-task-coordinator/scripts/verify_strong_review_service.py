#!/usr/bin/env python3
"""Opt-in real user-systemd lifecycle check using isolated state and fake Codex only.

Copies the reviewed runner into a temporary fixture, replaces its STATE constant
there only, and never accesses the production ledger or executes installed Codex.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch


def check():
    source = Path(__file__).with_name('strong_review.py')
    with tempfile.TemporaryDirectory(prefix='strong-review-service-fixture-') as scratch:
        root = Path(scratch)
        script = root / 'skills/complex-task-coordinator/scripts/strong_review.py'
        script.parent.mkdir(parents=True)
        state = root / 'state'
        original = "STATE = Path.home() / '.local/state/complex-task-reviews'"
        code = source.read_text()
        assert code.count(original) == 1
        script.write_text(code.replace(original, 'STATE = Path(' + repr(str(state)) + ')'))
        reviewer = root / 'skills/complex-task-reviewer/SKILL.md'
        reviewer.parent.mkdir()
        reviewer.write_text('Read-only offline CORRECTION fixture. No external calls.')
        module_spec = importlib.util.spec_from_file_location('isolated_strong_review', script)
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        project = root / 'project'
        project.mkdir()
        (project / 'ROLE-BINDINGS.json').write_text(json.dumps({'project_root': str(project),
                'control': 'ACTIVE', 'roles': {'COORDINATOR': {}}, 'migration': {'status': 'ACTIVE'}}))
        subprocess.run(['git', 'init', '-q', str(project)], check=True)
        subprocess.run(['git', '-C', str(project), 'add', 'ROLE-BINDINGS.json'], check=True)
        subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(project),
                        '-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid',
                        'commit', '-qm', 'fixture'], check=True)
        target = subprocess.check_output(['git', '-C', str(project), 'rev-parse', 'HEAD'], text=True).strip()
        package = project / 'review.md'
        package.write_text('Offline fixed package: no network, no production state.')
        evidence = root / 'user-message.txt'
        evidence.write_text('Fixture-only authorization, never applicable to production.')
        ledger = module.Ledger()
        started = time.time()
        ledger.grant(str(project), 'offline-service-fixture', evidence, evidence.read_text(), started, started + 300,
                     tasks=['TASK-FIXTURE'], repo=str(project), model='offline-fixture-model')
        bin_dir = root / 'bin'
        bin_dir.mkdir()
        fake = bin_dir / 'codex'
        fake.write_text('''#!/usr/bin/python3
import json, os, pathlib, sys, time
args=sys.argv[1:]
assert args[0]=='exec' and args[args.index('--sandbox')+1]=='read-only'
assert 'approval_policy="never"' in args
assert args[args.index('--model')+1]=='offline-fixture-model'
prompt=sys.stdin.read()
assert 'no network' in prompt and 'TASK-FIXTURE' in prompt
result=pathlib.Path(args[args.index('--output-last-message')+1])
(result.parent/'fake-started.json').write_text(json.dumps({'pid':os.getpid(),'cgroup':pathlib.Path('/proc/self/cgroup').read_text()}))
time.sleep(3)
result.write_text('PASS\\nOffline fixture completed after caller termination.\\n')
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':11,'output_tokens':7}}),flush=True)
''')
        fake.chmod(0o700)
        arguments = dict(project=str(project), repo=str(project), package=str(package), review_id='R-FIXTURE',
                         task_id='TASK-FIXTURE', base=target, target=target, risk='fixture risk',
                         question='Does service survive caller cleanup?', ac='fixture AC', gate='fixture gate',
                         retry_of=None, timeout_seconds=20)
        args_file = root / 'args.json'
        args_file.write_text(json.dumps(arguments))
        launcher = root / 'caller.py'
        submitted = root / 'submitted.json'
        launcher.write_text('''import argparse, importlib.util, json, pathlib, sys, time
script,args_file,submitted=sys.argv[1:]
spec=importlib.util.spec_from_file_location('fixture',script)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
args=argparse.Namespace(**json.loads(pathlib.Path(args_file).read_text()))
call=module.run_review(args,module.Ledger())
pathlib.Path(submitted).write_text(json.dumps(call))
time.sleep(60)
''')
        environment = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ['PATH'])
        caller = subprocess.Popen([sys.executable, str(launcher), str(script), str(args_file), str(submitted)],
                                  env=environment, start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        call = None
        try:
            deadline = time.monotonic() + 15
            while not submitted.exists() and caller.poll() is None and time.monotonic() < deadline:
                time.sleep(.05)
            if not submitted.exists():
                raise AssertionError('fixture submission failed: ' + caller.communicate(timeout=2)[1].decode())
            call = json.loads(submitted.read_text())
            assert call['status'] in ('RESERVED', 'RUNNING')
            os.killpg(caller.pid, signal.SIGTERM)
            caller.wait(timeout=5)
            directory = Path(call['spec_path']).parent
            marker = directory / 'fake-started.json'
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                current = ledger.call(call['id'])
                if current['status'] in ('SUCCEEDED', 'FAILED') and module.service_info(call['unit']).get('ActiveState') in ('inactive', 'failed'):
                    break
                time.sleep(.05)
            assert current['status'] == 'SUCCEEDED', current
            identity = json.loads(marker.read_text())
            assert call['unit'] in identity['cgroup'] and 'hermes-worker-cron-' not in identity['cgroup']
            assert identity['pid'] != caller.pid
            result = Path(current['result_path']).read_text()
            assert 'after caller termination' in result
            assert json.loads(current['usage']) == [{'input_tokens': 11, 'output_tokens': 7}]
            assert not (directory / 'snapshot').exists()
            with patch.dict(os.environ, environment):
                reused = module.run_review(argparse.Namespace(**arguments), ledger)
            assert reused['reused'] and reused['id'] == call['id']
            assert ledger.status(str(project))['used_last_60_minutes'] == 1
            return {'success': True, 'fake_codex_only': True, 'production_ledger_accessed': False,
                    'caller_terminated': caller.returncode, 'status': current['status'],
                    'unit': call['unit'], 'worker_cgroup': current['wrapper_cgroup'],
                    'fake_codex_cgroup': identity['cgroup'].strip(), 'usage': json.loads(current['usage']),
                    'frozen_model': json.loads(Path(call['spec_path']).read_text())['model'],
                    'reused_without_second_charge': True}
        finally:
            if caller.poll() is None:
                os.killpg(caller.pid, signal.SIGKILL)
                caller.wait(timeout=5)
            if call:
                subprocess.run(['systemctl', '--user', 'stop', call['unit']], capture_output=True, timeout=10)
                subprocess.run(['systemctl', '--user', 'reset-failed', call['unit']], capture_output=True, timeout=10)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    report = check()
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + '\n')
    print(text)
