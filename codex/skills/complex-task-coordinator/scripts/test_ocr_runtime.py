#!/usr/bin/env python3
"""零模型回归：隔离Git/config/锁，假OCR；实际systemd验证跨调用进程结束。"""

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import ocr_runtime as runtime


FAKE_OCR = r'''#!/usr/bin/python3
import json,sys,time,uuid
from pathlib import Path
root=Path(__file__).parent
args=sys.argv[1:]
if '--version' in args:
 print('OpenCodeReview v1.13.0 (fixture)');sys.exit(0)
if args[:2]==['delegate','rule']:
 print(json.dumps({'schema_version':'1','groups':[{'rule':'fixture correctness rules'}]}));sys.exit(0)
def arg(key):return args[args.index(key)+1]
excludes=arg('--exclude').split(',') if '--exclude' in args else []
files=['a.py','b.py','tests/test_a.py']
chosen=[f for f in files if f not in excludes and not f.startswith('tests/')]
if '--preview' in args:
 print(json.dumps({'files':[{'path':f,'will_review':f in chosen,'exclude_reason':'default_path' if f.startswith('tests/') else 'user_exclude'} for f in files]}));sys.exit(0)
mode=(root/'mode').read_text().strip()
with (root/'calls').open('a') as f:f.write('review\n')
repo=Path.cwd();sid=str(uuid.uuid4())
path=root/'sessions'/str(repo).strip('/').replace('/','-')/(sid+'.jsonl');path.parent.mkdir(parents=True,exist_ok=True)
def event(e):
 e.update(sessionId=sid,timestamp='2026-10-13T06:00:00Z')
 with path.open('a') as f:f.write(json.dumps(e)+'\n')
event({'type':'session_start','cwd':str(repo),'diffFrom':arg('--from'),'diffTo':arg('--to')})
if mode=='stall':time.sleep(120)
else:time.sleep(0.2)
items=[{'item_id':f,'path':f,'fingerprint':'fixed-'+f} for f in chosen]
failed=[] if mode=='complete' or (mode=='second-partial' and 'b.py' not in chosen) else [dict(i,reason='stopped because context compression exceeded its threshold') for i in items]
manifest={'schema_version':'ocr.run-manifest/v1','terminal_state':'complete' if not failed else 'failed',
'input':{'resolved_base':arg('--from'),'resolved_head':arg('--to')},
'execution':{'provider':arg('--provider'),'model':arg('--model'),'ocr_version':'v1.13.0'},
'coverage':{'selected':items,'completed':items if not failed else [],'reused':[],'failed':failed,'waived':[]}}
event({'type':'session_end','run_manifest':manifest})
print(json.dumps({'findings':[]}))
# partial故意退出0，以验证不能把退出码当覆盖成功。
'''


class RuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='ocr-runtime-test-')
        cls.root = Path(cls.temp.name)
        cls.repo = cls.root / 'repo'
        cls.repo.mkdir()
        cls.git('init', '-q')
        (cls.repo / 'a.py').write_text('a = 1\n')
        cls.git('add', '.')
        cls.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@invalid', 'commit', '-qm', 'base')
        cls.base = cls.git('rev-parse', 'HEAD')
        (cls.repo / 'a.py').write_text('a = 2\n')
        (cls.repo / 'b.py').write_text('b = 3\n')
        (cls.repo / 'tests').mkdir()
        (cls.repo / 'tests/test_a.py').write_text('assert True\n')
        cls.git('add', '.')
        cls.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@invalid', 'commit', '-qm', 'target')
        cls.head = cls.git('rev-parse', 'HEAD')
        cls.project = cls.root / 'project'
        cls.project.mkdir()
        (cls.project / 'ROLE-BINDINGS.json').write_text('{"control":"ACTIVE"}')
        cls.config = cls.root / 'config.json'
        cls.config.write_text(json.dumps({'custom_providers': {'vllm-local': {
            'url': runtime.ENDPOINT, 'model': 'fixture-model'}}}))
        cls.original_config = cls.config.read_bytes()
        cls.background = cls.root / 'background.md'
        cls.background.write_text('Fixture AC: review both changed functions.\n')
        cls.fake = cls.root / 'fake-ocr'
        cls.fake.write_text(FAKE_OCR)
        cls.fake.chmod(0o755)
        cls.selector = cls.root / 'selector.py'
        cls.selector.write_text('''import json
from pathlib import Path
root=Path(__file__).parent
if not (root/'routing-on').exists():
 print('null')
else:
 count=(root/'calls').read_text().count('review') if (root/'calls').exists() else 0
 count-=int((root/'routing-on').read_text() or '0')
 routes=[{'provider':'fixture-cloud-a','model':'model-a','endpoint':'https://a.invalid/v1'},
         {'provider':'fixture-cloud-b','model':'model-b','endpoint':'https://b.invalid/v1'}]
 route=routes[min(count,1)]
 print(json.dumps({**route,'allowed_routes':routes,'effort':'high'}))
''')
        (cls.root / 'mode').write_text('complete')
        cls.code = cls.root / 'code'
        cls.code.mkdir()
        source = Path(runtime.__file__).read_text()
        source = source.replace("Path('/home/wang/.npm-global/bin/ocr')", repr(cls.fake))
        source = source.replace("Path('/home/wang/.opencodereview/config.json')", repr(cls.config))
        source = source.replace("Path('/home/wang/.opencodereview/sessions')", repr(cls.root / 'sessions'))
        source = source.replace("Path('/home/wang/.local/state/ocr-runtime/ocr.lock')", repr(cls.root / 'global.lock'))
        source = source.replace("Path('/home/wang/work/hermes-role-runtime/ocr_model_route.py')", repr(cls.selector))
        # 测试只调假OCR；不阻塞或接管生产已有OCR。另有实际进程识别检查。
        source = source.replace('def existing_ocr():\n', 'def existing_ocr():\n    return []\n')
        source = source.replace('PosixPath(', 'Path(')
        (cls.code / 'ocr_runtime.py').write_text(source)
        shutil.copy2(Path(runtime.__file__).with_name('ocr_session_status.py'), cls.code / 'ocr_session_status.py')
        shutil.copy2(Path(runtime.__file__).with_name('ocr_results.py'), cls.code / 'ocr_results.py')
        spec = importlib.util.spec_from_file_location('isolated_runtime', cls.code / 'ocr_runtime.py')
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)
        cls.units = []

    @classmethod
    def tearDownClass(cls):
        for unit in cls.units:
            subprocess.run(['systemctl', '--user', 'stop', unit], capture_output=True)
            subprocess.run(['systemctl', '--user', 'reset-failed', unit], capture_output=True)
        cls.temp.cleanup()

    @classmethod
    def git(cls, *args):
        return subprocess.check_output(['git', '-C', str(cls.repo), *args], stderr=subprocess.DEVNULL).decode().strip()

    def setUp(self):
        self.module.CONFIG.write_bytes(self.original_config)
        (self.root / 'mode').write_text('complete')
        (self.project / 'ROLE-BINDINGS.json').write_text('{"control":"ACTIVE"}')

    def make_run(self, name, batches=None, identity_name=None, reuse_run=None):
        background = self.root / ((identity_name or name) + '-background.md')
        background.write_text('Fixture AC: review both functions. Context: ' + (identity_name or name))
        args = type('Args', (), {'output': self.root / name, 'project': self.project,
            'repo': self.repo, 'base': self.base, 'head': self.head, 'task': 'FIXTURE', 'attempt': name,
            'authorization': 'zero-model-isolated-fixture', 'background_file': background,
            'batches_file': batches, 'reuse_run': reuse_run,
            'retry_reason': 'fixture eliminated prior failure, zero real model' if reuse_run else None})()
        result = self.module.prepare(args)
        self.units.append(result['unit'])
        return args.output

    def wait_final(self, directory):
        end = time.monotonic() + 25
        while time.monotonic() < end:
            value = self.module.status(directory)
            if (value['recorded_state']['state'] not in ('SUBMITTED', 'RUNNING', 'PREPARED')
                    and value['service'].get('ActiveState') not in ('active', 'activating', 'deactivating')):
                return value
            time.sleep(0.2)
        self.fail('fixture did not finish in 25s')

    def test_01_prepare_no_model_full_coverage(self):
        directory = self.make_run('prepare')
        spec = self.module.load_spec(directory)
        self.assertEqual([b['files'] for b in spec['batches']], [['a.py'], ['b.py']])
        self.assertFalse((self.root / 'calls').exists())
        self.assertEqual(self.module.CONFIG.read_bytes(), self.original_config)

    def test_legacy_15_frozen_run_remains_readable(self):
        directory = self.make_run('legacy-readable')
        path = directory / 'spec.json'
        spec = json.loads(path.read_text())
        spec['runtime_version'] = '1.5.0'
        path.chmod(0o644)
        path.write_text(json.dumps(spec))
        hash_path = directory / 'spec.sha256'
        hash_path.chmod(0o644)
        hash_path.write_text(self.module.digest(path.read_bytes()))
        self.assertEqual(self.module.load_spec(directory)['runtime_version'], '1.5.0')

    def test_dynamic_cloud_switch_between_batches_preserves_coverage_identity(self):
        marker = self.root / 'routing-on'
        marker.write_text(str((self.root / 'calls').read_text().count('review')
                              if (self.root / 'calls').exists() else 0))
        try:
            directory = self.make_run('dynamic-routing')
            self.module.launch(directory)
            result = self.wait_final(directory)
            self.assertEqual(result['state'], 'AWAITING_DECISION')
            entries = result['recorded_state']['batches']
            self.assertEqual([e['execution_route']['model'] for e in entries], ['model-a', 'model-b'])
            for entry in entries:
                diagnosis = json.loads((directory / (entry['id'] + '-diagnosis.json')).read_text())
                self.assertTrue(diagnosis['batch_identity_matches'])
            self.assertEqual(self.module.CONFIG.read_bytes(), self.original_config)
        finally:
            marker.unlink(missing_ok=True)

    def test_02_missing_or_overlapping_batch_fails(self):
        for index, batches in enumerate(([['a.py']], [['a.py'], ['a.py', 'b.py']])):
            path = self.root / f'bad-batches-{index}.json'
            path.write_text(json.dumps(batches))
            with self.assertRaisesRegex(RuntimeError, 'BATCH_COVERAGE_MISMATCH'):
                self.make_run(f'bad-batches-{index}', path)

    def test_03_stale_endpoint_fails_without_mutation(self):
        raw = self.config.read_text().replace(runtime.ENDPOINT, 'http://127.0.0.1:18002/v1')
        self.config.write_text(raw)
        with self.assertRaisesRegex(RuntimeError, 'ENDPOINT_MISMATCH'):
            self.make_run('bad-endpoint')
        self.assertEqual(self.config.read_text(), raw)

    def test_04_dirty_snapshot_fails(self):
        path = self.repo / 'a.py'
        before = path.read_bytes()
        try:
            path.write_text('a = 999\n')
            with self.assertRaisesRegex(RuntimeError, 'TRACKED_WORKTREE_DIRTY'):
                self.make_run('dirty')
        finally:
            path.write_bytes(before)

    def test_05_spec_and_background_tamper_fail(self):
        for name in ('spec.json', 'background.md'):
            directory = self.make_run('tamper-' + name)
            path = directory / name
            path.chmod(0o644)
            path.write_bytes(path.read_bytes() + b'\n')
            with self.assertRaisesRegex(RuntimeError, 'SPEC_CHANGED|ARTIFACT_CHANGED'):
                self.module.load_spec(directory)

    def test_06_lock_busy_no_model(self):
        directory = self.make_run('busy')
        with self.module.lock(self.module.GLOBAL_LOCK):
            with self.assertRaisesRegex(RuntimeError, 'OCR_BUSY'):
                self.module.launch(directory)
        self.assertEqual(self.module.read_json(directory / 'state.json')['state'], 'PREPARED')

    def test_07_control_paused_blocks_start(self):
        directory = self.make_run('paused')
        (self.project / 'ROLE-BINDINGS.json').write_text('{"control":"PAUSED"}')
        with self.assertRaisesRegex(RuntimeError, 'PROJECT_NOT_ACTIVE'):
            self.module.launch(directory)

    def test_08_worker_cannot_be_called_in_terminal(self):
        directory = self.make_run('worker-direct')
        with self.assertRaisesRegex(RuntimeError, 'NOT_IN_DEDICATED_SERVICE'):
            self.module.worker(directory)

    def test_09_service_survives_launcher_scope_exit_and_no_duplicate(self):
        directory = self.make_run('persistent')
        spec = self.module.load_spec(directory)
        launcher_unit = 'ocr-fixture-launcher-' + spec['run_id'] + '.scope'
        command = ['systemd-run', '--user', '--scope', '--quiet', '--unit=' + launcher_unit,
                   '/usr/bin/python3', str(self.code / 'ocr_runtime.py'), 'start', '--run', str(directory)]
        subprocess.run(command, check=True, capture_output=True, timeout=15)
        result = self.wait_final(directory)
        self.assertEqual(result['state'], 'AWAITING_DECISION')
        self.assertEqual(result['gate_decision'], 'NOT_EVALUATED')
        self.assertIn('/' + spec['unit'], result['recorded_state']['cgroup'])
        self.assertNotIn(launcher_unit, result['recorded_state']['cgroup'])
        calls = (self.root / 'calls').read_text()
        self.module.launch(directory)
        self.assertEqual((self.root / 'calls').read_text(), calls)
        self.assertEqual(self.config.read_bytes(), self.original_config)

    def test_10_partial_rc_zero_stops_chain(self):
        directory = self.make_run('partial')
        before = (self.root / 'calls').read_text().count('review')
        (self.root / 'mode').write_text('partial')
        self.module.launch(directory)
        result = self.wait_final(directory)
        self.assertEqual(result['state'], 'REVIEW_INCOMPLETE')
        self.assertEqual(result['recorded_state']['batches'][0]['returncode'], 0)
        self.assertEqual(len(result['recorded_state']['batches']), 1)
        self.assertEqual((self.root / 'calls').read_text().count('review'), before + 1)

    def test_11_stop_dedicated_unit_kills_child_releases_lock(self):
        directory = self.make_run('cancel')
        (self.root / 'mode').write_text('stall')
        self.module.launch(directory)
        end = time.monotonic() + 10
        pid = None
        while time.monotonic() < end:
            state = self.module.read_json(directory / 'state.json')
            if state.get('batches') and state['batches'][0].get('pid'):
                pid = state['batches'][0]['pid']
                break
            time.sleep(0.2)
        self.assertIsNotNone(pid)
        result = self.module.stop(directory)
        self.assertEqual(result['state'], 'CANCELLED')
        self.assertFalse(Path(f'/proc/{pid}').exists())
        with self.module.lock(self.module.GLOBAL_LOCK):
            pass
        self.assertEqual(self.config.read_bytes(), self.original_config)

    def test_12_hard_kill_cannot_be_called_complete(self):
        directory = self.make_run('interrupted')
        state = {'state': 'RUNNING', 'gate_decision': 'NOT_EVALUATED'}
        self.module.atomic_json(directory / 'state.json', state)
        with patch.object(self.module, 'service_info', return_value={'ActiveState': 'failed'}):
            self.assertEqual(self.module.status(directory)['state'], 'INTERRUPTED')

    def test_13_new_directory_cannot_duplicate_same_review(self):
        directory = self.make_run('duplicate', identity_name='persistent')
        with self.assertRaisesRegex(RuntimeError, 'DUPLICATE_REVIEW'):
            self.module.launch(directory)

    def test_14_reuse_only_complete_batch_then_retry_limit(self):
        first = self.make_run('reuse-first', identity_name='reuse')
        (self.root / 'mode').write_text('second-partial')
        self.module.launch(first)
        result = self.wait_final(first)
        self.assertEqual(result['state'], 'REVIEW_INCOMPLETE')
        self.assertEqual([b['state'] for b in result['recorded_state']['batches']],
                         ['REVIEW_COMPLETE', 'REVIEW_INCOMPLETE'])
        second = self.make_run('reuse-second', identity_name='reuse', reuse_run=first)
        spec = self.module.load_spec(second)
        self.assertIn('reuse_session', spec['batches'][0])
        self.assertNotIn('reuse_session', spec['batches'][1])
        before = (self.root / 'calls').read_text().count('review')
        self.module.launch(second)
        result = self.wait_final(second)
        self.assertEqual(result['state'], 'REVIEW_INCOMPLETE')
        self.assertTrue(result['recorded_state']['batches'][0]['reused'])
        self.assertEqual((self.root / 'calls').read_text().count('review'), before + 1)
        with self.assertRaisesRegex(RuntimeError, 'RETRY_LIMIT'):
            self.make_run('reuse-third', identity_name='reuse', reuse_run=second)

    def test_15_same_files_wrong_base_head_or_model_not_complete(self):
        directory = self.make_run('identity')
        spec = self.module.load_spec(directory)
        # 使用已完成fixture日志，逐项改变期待身份，不能复用它放行不同版本。
        prior = self.module.read_json(self.root / 'persistent/state.json')['batches'][0]['session_file']
        batch = {'files': ['a.py']}
        self.assertTrue(self.module.assess_session(Path(prior), spec, batch)['coverage_complete'])
        for key in ('base', 'head', 'model'):
            bad = dict(spec, **{key: 'wrong'})
            self.assertFalse(self.module.assess_session(Path(prior), bad, batch)['coverage_complete'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
