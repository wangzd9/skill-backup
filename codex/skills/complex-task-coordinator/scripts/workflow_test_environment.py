"""Freeze the interpreter, import root and pytest plugins before candidate tests.

The probe runs in the very same namespace as the test. Infrastructure failures
never publish candidate FAIL evidence. No ambient PYTHONPATH/venv/pytest options
are inherited from whichever gateway or cron worker happened to call us.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


def prepare(command, candidate, checkout, artifacts, *, pytest):
    command = list(command)
    requested = command[0]
    is_python = Path(requested).name.startswith(('python', 'pypy'))
    preferred = Path(candidate.get('test_repository',candidate['repo'])) / '.venv/bin/python'
    if pytest and not is_python:
        executable = str(preferred) if preferred.is_file() else shutil.which('python3')
        command = [executable, '-m', 'pytest', *command[1:]]
    elif is_python:
        executable = requested if Path(requested).is_absolute() else str(preferred) if pytest and preferred.is_file() else shutil.which(requested)
        command[0] = executable
    else:
        executable = requested if Path(requested).is_absolute() else shutil.which(requested)
        command[0] = executable
    if not executable or not Path(executable).is_file():
        raise ValueError('TEST_ENVIRONMENT_UNAVAILABLE: interpreter/program not found')
    # Do not resolve a venv's symlink: that changes sys.prefix and site-packages.
    executable = os.path.abspath(executable)
    command[0] = executable
    environment = {'PATH': str(Path(executable).parent) + ':/usr/bin:/bin',
        'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'TZ': 'UTC',
        'HOME': str(artifacts / 'home'), 'PYTHONPATH': str(checkout),
        'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONHASHSEED': '0',
        'TMPDIR': str(artifacts / 'tmp'), 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'}
    (artifacts / 'home').mkdir(exist_ok=True)
    if (Path(executable).parent.parent / 'pyvenv.cfg').is_file():
        environment['VIRTUAL_ENV'] = str(Path(executable).parent.parent)
    return command, environment


def probe(command, wrap, *, pytest, path):
    """Probe dependencies/plugins and argv parsing, without collecting test code."""
    if not pytest:
        return {'state': 'READY', 'plugins': [], 'kind': 'explicit-junit-command'}
    code = ('import json,sys,pytest,importlib.metadata as m,importlib.util as u; '
            'print(json.dumps({"python":sys.version,"prefix":sys.prefix,'
            '"pytest":pytest.__version__,"distributions":sorted((d.metadata["Name"],d.version) for d in m.distributions()),'
            '"dependencies":[sys.executable,pytest.__file__]+[u.find_spec(e.value.split(":")[0]).origin '
            'for e in m.entry_points(group="pytest11")],"plugins":sorted('
            '[(e.name,e.value) for e in m.entry_points(group="pytest11")])}))')
    result = subprocess.run(wrap([command[0], '-c', code]), capture_output=True,
                            text=True, timeout=30)
    if result.returncode:
        raise ValueError('TEST_ENVIRONMENT_UNAVAILABLE: isolated interpreter cannot import pytest')
    try:
        manifest = json.loads(result.stdout)
    except ValueError as exc:
        raise ValueError('TEST_ENVIRONMENT_UNAVAILABLE: invalid interpreter probe') from exc
    plugins = [value for _, value in manifest['plugins']]
    # Enumerate installed plugins once, load exactly those on all later commands.
    plugin_args = [part for plugin in plugins for part in ('-p', plugin)]
    index=command.index('pytest')+1
    command[index:index] = plugin_args
    parse_code='import json,sys,pytest; c=pytest.Config.fromdictargs({},json.loads(sys.argv[1])); c._ensure_unconfigure()'
    checked = subprocess.run(wrap([command[0],'-c',parse_code,json.dumps(command[index:])]), capture_output=True,
                             text=True, timeout=30)
    if checked.returncode:
        raise ValueError('TEST_RUNNER_PLUGIN_MISSING: isolated pytest cannot parse requested options')
    manifest.update(state='READY', argv_sha256=hashlib.sha256(
        json.dumps(command).encode()).hexdigest())
    from runtime_common import save_json
    save_json(path, manifest)
    return manifest


def classify(stats, *, pytest):
    # pytest represents collection/setup failures as synthetic JUnit testcases.
    # Those do not establish that the candidate's business tests executed.
    if pytest and stats['errors']:
        stats['mechanical_result'] = 'UNVERIFIED'
        stats['reasons'].append('TEST_COLLECTION_OR_SETUP_UNAVAILABLE')
    return stats


def stability_command(command, artifacts):
    """The existing operation runner owns both repetitions and their process tree."""
    primary=artifacts/'junit.xml'; second=artifacts/'junit-repeat.xml'
    repeated=[arg.replace(str(primary),str(second)) for arg in command]
    launcher=artifacts/'stability-runner.py'
    source=('import json,subprocess,sys\nfrom pathlib import Path\n'
            'codes=[]\n'
            'for argv in '+repr([command,repeated])+':\n'
            ' codes.append(subprocess.run(argv).returncode)\n'
            'Path('+repr(str(artifacts/'stability.json'))+').write_text(json.dumps(codes))\n'
            'sys.exit(next((code for code in codes if code),0))\n')
    launcher.write_text(source)
    return [command[0],str(launcher)]


def check_stability(stats, artifacts, *, pytest):
    if not pytest:
        return stats
    from delivery_evidence import parse_test_results
    from runtime_common import read_json
    codes=read_json(artifacts/'stability.json',[])
    if not isinstance(codes,list) or len(codes)!=2 or any(type(code) is not int for code in codes):
        stats['mechanical_result']='UNVERIFIED';stats['reasons'].append('TEST_STABILITY_RESULT_UNAVAILABLE')
        return stats
    observations=[classify(parse_test_results(artifacts/name,
        execution_status='SUCCEEDED' if code==0 else 'FAILED',exit_code=code),pytest=True)
        for name,code in zip(('junit.xml','junit-repeat.xml'),codes)]
    keys=('tests','passed','failures','errors','skipped','mechanical_result','case_fingerprint')
    stats=observations[0]
    stats['stability_runs']=2
    stats['stability_match']=all([row.get(k) for k in keys]==[stats.get(k) for k in keys] for row in observations)
    stats['stability_evidence']={'path':str(artifacts/'junit-repeat.xml'),
                               'sha256':observations[1]['sha256'],'exit_codes':codes}
    if not stats['stability_match']:
        stats['mechanical_result']='UNVERIFIED';stats['reasons'].append('TEST_RESULT_NONDETERMINISTIC')
    return stats
