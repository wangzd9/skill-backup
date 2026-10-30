"""Sole native CLIs and real user-systemd; fake only provider executables."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

try:
    from test_business_ledger import LedgerFixture
except ModuleNotFoundError:
    from tests.test_business_ledger import LedgerFixture
from workflow_services import NativeServices, collect_service_result, validate_native_action, submit_service

SCRIPTS=Path(__file__).resolve().parents[1]


class NativeWorkflowServiceTests(LedgerFixture):
    def setUp(self):
        super().setUp()
        self.now=time.time()
        worker,identity=self.bind('A')
        self.candidate_id,self.candidate_spec=self.candidate(identity)
        self.close(worker,identity)
        self.coord_worker,self.coord=self.bind('COORDINATOR')
        self.units=[]
        self.addCleanup(self.stop_units)
        self.scripts=self.root/'scripts'; self.scripts.mkdir()
        self.bin=self.root/'bin'; self.bin.mkdir()
        self.config=self.root/'ocr-config.json'
        self.config.write_text(json.dumps({'custom_providers':{'vllm-local':{
            'url':'http://192.168.2.45:8002/v1','model':'fixture-model'}}}))
        self.selector=self.root/'selector.py'; self.selector.write_text("print('null')\n")
        self.background=self.root/'background.md'; self.background.write_text('Original frozen AC: value() returns 1')
        self.package=self.root/'review-package.md'; self.package.write_text('Review exact frozen value() behavior and AC evidence')
        # Reuse the established fake provider's manifest shape. The tested
        # launcher, worker, ledger and systemd lifecycle remain the real code.
        sys.path.insert(0,str(SCRIPTS))
        import test_ocr_runtime
        fake=test_ocr_runtime.FAKE_OCR.replace("files=['a.py','b.py','tests/test_a.py']","files=['app.py']")
        fake=fake.replace('else:time.sleep(0.2)','else:time.sleep(2)')
        fake=fake.replace("manifest={'schema_version':'ocr.run-manifest/v1',", "manifest={'schema_version':'ocr.run-manifest/v1','run_id':sid,'operation':'review',")
        fake=fake.replace("event({'type':'session_end','run_manifest':manifest})", "for item in items: event({'type':'review_item_done','filePath':item['path'],'fingerprint':item['fingerprint'],'comments':[]})\nevent({'type':'session_end','run_manifest':manifest})")
        fake=fake.replace("print(json.dumps({'findings':[]}))", "print(json.dumps({'status':'complete','session_id':sid,'manifest':manifest,'llm':{'provider':arg('--provider'),'model':arg('--model')},'summary':{'comments':0,'files_reviewed':len(items)},'comments':[]}))")
        self.ocr=self.root/'fake-ocr'; self.ocr.write_text(fake); self.ocr.chmod(0o755)
        (self.root/'mode').write_text('complete')
        replacements={"Path('/home/wang/.npm-global/bin/ocr')":'Path('+repr(str(self.ocr))+')',
            "Path('/home/wang/.opencodereview/config.json')":'Path('+repr(str(self.config))+')',
            "Path('/home/wang/.opencodereview/sessions')":'Path('+repr(str(self.root/'sessions'))+')',
            "Path('/home/wang/.local/state/ocr-runtime/ocr.lock')":'Path('+repr(str(self.root/'ocr.lock'))+')',
            "Path(__file__).resolve().parent/'ocr_model_route.py'":'Path('+repr(str(self.selector))+')'}
        source=(SCRIPTS/'ocr_runtime.py').read_text()
        for before,after in replacements.items(): source=source.replace(before,after)
        # The fixture has its own lock/provider and must never take ownership of
        # a production OCR already running on the same host.
        source=source.replace('def existing_ocr():\n','def existing_ocr():\n    return []\n')
        # This fixture's executable is intentionally not the installed npm CLI.
        source=source.replace('def admit_ocr(state_dir=None):\n',
            "def admit_ocr(state_dir=None):\n    return {'record': {'identity': execution_identity(), 'fixture_only': True}, 'cache_hit': True}\n")
        (self.scripts/'ocr_runtime.py').write_text(source)
        for name in ('ocr_results.py','ocr_session_status.py','ocr_compat.py','ocr_compat_baseline.json'): shutil.copy2(SCRIPTS/name,self.scripts/name)
        source=(SCRIPTS/'strong_review.py').read_text()
        source=source.replace("STATE = Path.home() / '.local/state/complex-task-reviews'",'STATE = Path('+repr(str(self.root/'review-service-state'))+')')
        source=source.replace("reviewer = Path(__file__).resolve().parents[2] / 'complex-task-reviewer/SKILL.md'",
            'reviewer = Path('+repr(str(Path.home()/'.codex/skills/complex-task-reviewer/SKILL.md'))+')')
        source=source.replace("model = checked_model(grant['model']) if grant['model'] is not None else resolve_default_model(repo or grant['repo'], target, project)","model = 'fixture-model'")
        (self.scripts/'strong_review.py').write_text(source)
        codex=self.bin/'codex'
        codex.write_text('''#!/usr/bin/python3
import json,sys,time
from pathlib import Path
sys.stdin.read(); time.sleep(2)
Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text('Frozen candidate independently inspected; finding-one requires explicit adjudication.\\n')
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}))
'''); codex.chmod(0o755)
        self.native=NativeServices(); self.native.scripts=self.scripts

    def stop_units(self):
        for unit in self.units:
            subprocess.run(['systemctl','--user','stop',unit],capture_output=True)
            subprocess.run(['systemctl','--user','reset-failed',unit],capture_output=True)

    def submit_detached(self,kind):
        authorization='trusted-'+kind
        self.assertCommitted(self.ledger.grant_external_authorization({'authorization_id':authorization,
            'candidate_id':self.candidate_id,'service_kind':kind,'max_calls':1,'expires_at':self.now+3600},
            {'authenticated':True,'current_full':True,'message_id':'user-'+kind,'user_id':'u',
             'primary_text':'允许固定候选的一次独立服务调用'},command_id='grant-'+kind))
        background={'background_text':self.background.read_text()} if getattr(self,'inline_background',False) else {'background_file':str(self.background)}
        request=background if kind=='ocr' else {'package':str(self.package),
            'risk':'value regression','question':'Does frozen value meet AC?','ac':'value()==1','gate':'exact candidate'}
        inputs=self.root/'submit.json'; inputs.write_text(json.dumps({'database':str(self.path),'identity':self.coord,
            'candidate_id':self.candidate_id,'kind':kind,'request':request,'authorization_id':authorization,
            'scripts':str(self.scripts)}))
        program='''import json,sys
from pathlib import Path
from business_ledger import BusinessLedger
from workflow_services import NativeServices,submit_service
v=json.loads(Path(sys.argv[1]).read_text()); adapter=NativeServices(); adapter.scripts=Path(v['scripts'])
r=submit_service(BusinessLedger(v['database']),v['identity'],v['candidate_id'],v['kind'],v['request'],action_id='native-'+v['kind'],authorization={'authorization_id':v['authorization_id']},command_id='native-submit',adapter=adapter)
print(json.dumps(r)); sys.exit(0 if r['status']=='COMMITTED' else 1)
'''
        env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1]),'PATH':str(self.bin)+':'+os.environ['PATH']}
        caller=subprocess.Popen([sys.executable,'-c',program,str(inputs)],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        stdout,stderr=caller.communicate(timeout=30)
        self.assertEqual(0,caller.returncode,(stdout,stderr))
        result=json.loads(stdout); reference=result['reference']; self.units.append(reference['unit'])
        self.assertFalse((self.root/'ROLE-BINDINGS.json').exists())
        private=next(self.path.parent.glob('services/*/invocation.json'))
        with self.assertRaisesRegex(ValueError,'WORKFLOW_NATIVE_SERVICE_SCOPE_MISMATCH'):
            validate_native_action(private,kind,{'head':'0'*40})
        info=subprocess.run(['systemctl','--user','show',reference['unit'],'--property=MainPID,ActiveState,ControlGroup'],capture_output=True,text=True,check=True)
        service=dict(line.split('=',1) for line in info.stdout.splitlines() if '=' in line)
        self.assertEqual('active',service['ActiveState'],result)
        self.assertNotEqual('0',service['MainPID'])
        self.assertTrue(service['ControlGroup'].endswith('/'+reference['unit']))
        self.assertNotEqual(str(caller.pid),service['MainPID'])
        # End the submitting business execution while the independent service lives.
        self.close(self.coord_worker,self.coord)
        self.assertIsNotNone(self.coord_worker.poll())
        return result

    def assert_collected(self,kind):
        result=self.submit_detached(kind)
        end=time.monotonic()+25
        while time.monotonic()<end:
            collected=collect_service_result(self.ledger,'native-'+kind,command_id='collect-'+kind,adapter=self.native)
            if collected['status']=='COMMITTED': break
            time.sleep(.2)
        self.assertEqual('COMMITTED',collected['status'],collected)
        self.assertEqual('RESULT_READY',collected['state'])
        self.assertFalse(collected['business_pass'])
        self.assertEqual([],self.ledger.snapshot()['verifications'])
        native=Path(result['reference']['native_directory']); spec=json.loads((native/'spec.json').read_text())
        self.assertEqual(result['reference']['unit'],spec['unit'])
        self.assertIn('runtime_version_digest',spec)
        terminal=next(self.path.parent.glob('services/*/terminal.json'))
        self.assertEqual(self.candidate_spec['head'],json.loads(terminal.read_text())['head'])

    def test_legacy_cli_cannot_use_schema2_without_frozen_private_action(self):
        result=subprocess.run([sys.executable,str(self.scripts/'ocr_runtime.py'),'prepare',
            '--project',str(self.root),'--repo',str(self.repo),'--base',self.base,'--head',self.candidate_spec['head'],
            '--task','T1','--attempt','1','--authorization','self-asserted','--background-file',str(self.background),
            '--output',str(self.root/'unauthorized-ocr')],capture_output=True,text=True)
        self.assertNotEqual(0,result.returncode)
        self.assertFalse((self.root/'unauthorized-ocr').exists())
        self.assertFalse((self.root/'calls').exists())

    def test_native_ocr_rejects_outside_symlink_escape_and_nonfile_inputs_before_cli(self):
        outside=tempfile.TemporaryDirectory(prefix='ocr-outside-requirement-')
        self.addCleanup(outside.cleanup)
        secret=Path(outside.name)/'unrelated.txt'; secret.write_text('unrelated host file')
        link=self.root/'escape-background.md'; link.symlink_to(secret)
        snapshot=self.ledger.snapshot()
        frozen={'candidate':snapshot['candidates'][0],'run':snapshot['runs'][0],
                'authorization_id':'none','action_id':'none'}
        for request in ({'background_file':str(secret)},{'background_file':str(link)},
                        {'background_file':str(self.root)},
                        {'background_file':str(self.background),'batches_file':str(secret)}):
            with self.subTest(request=request), self.assertRaisesRegex(ValueError,'OCR_INPUT_MUST_BE_REGULAR_FILE'):
                self.native.submit('ocr',frozen,request,self.root/'rejected-service')
        self.assertFalse((self.root/'rejected-service').exists())
        self.assertFalse((self.root/'calls').exists())

    def test_revocation_in_dispatch_gap_native_cli_refuses_before_provider_or_unit(self):
        provenance={'authenticated':True,'current_full':True,'message_id':'gap-user','user_id':'u',
                    'primary_text':'授权一次固定候选OCR，并允许随后撤销'}
        self.assertCommitted(self.ledger.grant_external_authorization({'authorization_id':'gap-grant',
            'candidate_id':self.candidate_id,'service_kind':'ocr','max_calls':1,'expires_at':self.now+3600},
            provenance,command_id='gap-grant'))
        fixture=self
        class RevokeBeforeNative(NativeServices):
            def submit(adapter,kind,frozen,request,directory):
                fixture.assertCommitted(fixture.ledger.revoke_authorization('gap-grant',provenance,command_id='gap-revoke'))
                return super().submit(kind,frozen,request,directory)
        adapter=RevokeBeforeNative(); adapter.scripts=self.scripts
        result=submit_service(self.ledger,self.coord,self.candidate_id,'ocr',{'background_file':str(self.background)},
            action_id='revoke-gap',authorization={'authorization_id':'gap-grant'},command_id='gap-submit',adapter=adapter)
        self.assertEqual('REJECTED',result['status'])
        self.assertEqual('WORKFLOW_FROZEN_SERVICE_AUTHORIZATION_REQUIRED',result['code'])
        self.assertFalse(result['dispatch_performed'])
        private=next(self.path.parent.glob('services/*/invocation.json'))
        self.assertFalse((private.parent/'native').exists())
        self.assertFalse((self.root/'calls').exists())
        self.assertEqual('AUTHORIZATION_REVOKED_OR_WRONG_KIND',
            self.ledger.external_dispatch_readiness(self.coord,'revoke-gap')['code'])

    def test_native_ocr_real_service_survives_caller_exit_with_frozen_coverage(self):
        self.assert_collected('ocr')

    def test_inline_background_runs_through_real_native_cli_without_requirement_write(self):
        self.inline_background=True
        self.assert_collected('ocr')
        invocation=next(self.path.parent.glob('services/*/invocation.json'))
        value=json.loads(invocation.read_text())
        path=invocation.parent/'background.md'
        self.assertEqual(self.background.read_bytes(),path.read_bytes())
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),value['reference']['inputs'][0]['sha256'])
        repeated=submit_service(self.ledger,self.coord,self.candidate_id,'ocr',
            {'background_text':self.background.read_text()},action_id='native-ocr',
            authorization={'authorization_id':'trusted-ocr'},command_id='repeat',adapter=self.native)
        # The old submitting owner is already gone: no second side effect is possible.
        self.assertNotEqual('COMMITTED',repeated['status'])
        self.assertEqual(1,len(self.ledger.snapshot()['external_actions']))

    def test_inline_expired_grant_or_ambiguous_input_creates_no_background_or_action(self):
        self.assertCommitted(self.ledger.grant_external_authorization({'authorization_id':'expired-inline',
            'candidate_id':self.candidate_id,'service_kind':'ocr','max_calls':1,'expires_at':self.now+1},
            {'authenticated':True,'current_full':True,'message_id':'expiry','user_id':'u','primary_text':'限一次'},command_id='expiry'))
        self.now+=2
        for number,request in enumerate(({'background_text':'frozen AC'},
                {'background_text':'frozen AC','background_file':str(self.background)})):
            result=submit_service(self.ledger,self.coord,self.candidate_id,'ocr',request,
                action_id='expired-'+str(number),authorization={'authorization_id':'expired-inline'},
                command_id='expired-'+str(number),adapter=self.native)
            self.assertEqual('REJECTED',result['status'],result)
        self.assertEqual([],self.ledger.snapshot()['external_actions'])
        self.assertEqual([],list(self.path.parent.glob('services/*/background.md')))

    def test_native_busy_preflight_does_not_consume_grant_or_register_unknown_action(self):
        self.assertCommitted(self.ledger.grant_external_authorization({'authorization_id':'busy-grant',
            'candidate_id':self.candidate_id,'service_kind':'ocr','max_calls':1,'expires_at':self.now+3600},
            {'authenticated':True,'current_full':True,'message_id':'busy-grant','user_id':'u','primary_text':'一次OCR'},
            command_id='busy-grant'))
        script=self.scripts/'ocr_runtime.py'
        script.write_text(script.read_text().replace('def existing_ocr():\n    return []\n',
                                                     'def existing_ocr():\n    return [12345]\n'))
        value=submit_service(self.ledger,self.coord,self.candidate_id,'ocr',{'background_text':'fixed AC'},
            action_id='busy',authorization={'authorization_id':'busy-grant'},command_id='busy',adapter=self.native)
        self.assertEqual('NATIVE_OCR_BUSY',value['code'])
        self.assertFalse(value['authorization_consumed'])
        self.assertEqual([],self.ledger.snapshot()['external_actions'])
        self.assertEqual([],list(self.path.parent.glob('services/*/invocation.json')))

    def test_lost_start_receipt_recovers_prepared_facts_without_replay_or_success(self):
        self.assertCommitted(self.ledger.grant_external_authorization({'authorization_id':'lost-grant',
            'candidate_id':self.candidate_id,'service_kind':'ocr','max_calls':1,'expires_at':self.now+3600},
            {'authenticated':True,'current_full':True,'message_id':'lost-grant','user_id':'u','primary_text':'一次OCR'},
            command_id='lost-grant'))
        class LoseStartReceipt(NativeServices):
            def _cli(adapter,script,arguments,**kwargs):
                if arguments[0]=='start':
                    raise RuntimeError('isolated lost start receipt; no provider started')
                return super()._cli(script,arguments,**kwargs)
        adapter=LoseStartReceipt();adapter.scripts=self.scripts
        value=submit_service(self.ledger,self.coord,self.candidate_id,'ocr',{'background_text':'fixed AC'},
            action_id='lost',authorization={'authorization_id':'lost-grant'},command_id='lost',adapter=adapter)
        self.assertEqual('EXTERNAL_SUBMISSION_UNKNOWN',value['code'])
        native=next(self.path.parent.glob('services/*/native'))
        background=native/'background.md';original=background.read_bytes()
        background.chmod(0o600);background.write_bytes(b'tampered')
        self.assertEqual('UNVERIFIED',collect_service_result(self.ledger,'lost',command_id='tampered-read',adapter=adapter)['status'])
        background.write_bytes(original)
        self.now+=4000
        self.close(self.coord_worker,self.coord)
        result=self.assertCommitted(collect_service_result(self.ledger,'lost',command_id='recover',adapter=adapter))
        self.assertEqual('INCOMPLETE',result['state'])
        terminal=json.loads(next(self.path.parent.glob('services/*/terminal.json')).read_text())
        self.assertEqual('INCOMPLETE',terminal['state'])
        self.assertFalse(terminal['business_pass'])
        self.assertFalse((self.root/'calls').exists())
        self.assertEqual([],self.ledger.snapshot()['verifications'])

    def test_native_strong_review_real_service_survives_caller_exit_without_approval(self):
        self.assert_collected('strong_review')


if __name__=='__main__': unittest.main()
