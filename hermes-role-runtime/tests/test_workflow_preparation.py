"""Exercise the real contract bootstrap against isolated Git/SQLite/processes."""
import hashlib
import json
from pathlib import Path
from unittest import mock

from test_business_ledger import LedgerFixture
from workflow_registration import register_packages, packages
from workflow_preparation import prepare, activate, requests, next_actions, task_in_source
from workflow_runtime import submit_control


class PreparationTests(LedgerFixture):
    def setUp(self):
        super().setUp()
        self.package=self.root/'new-package';self.package.mkdir()
        (self.package/'TODO.md').write_text('OLD-01 historical task and criteria must remain.\n')
        (self.package/'proposal.md').write_text('TASK-BR-00 TASK-BR-01 TASK-BR-02 TASK-BR-03\nSource integrity and asynchronous reconciliation provider contract.\n')
        self.entry={'requirement_id':'new-package','requirement_root':str(self.package),
            'repo':str(self.repo),'kind':'development','task_ids':['OLD-01'],
            'contract_files':['TODO.md'],'requires_explicit_task_ids':True}
        self.assertCommitted(register_packages(self.ledger,[self.entry],
            {'purpose':'registration_only','primary_text':'register only'},command_id='register'))
        self.target={k:self.entry[k] for k in ('requirement_id','requirement_root','repo')}
        self.target.update(task_ids=['TASK-BR-'+str(n).zfill(2) for n in range(4)],
            contract_files=['TODO.md','proposal.md'])
        self.source={'authenticated':True,'current_full':False,'chat_id':'chat','message_id':'native',
            'user_id':'user','primary_text':'请统一推进 new-package 缺陷补齐，先纳入正式合同、重新登记 TASK-BR-00～03，按依赖自动推进；不部署生产。'}
        self.pool=self.root/'isolated.json'

    def queue(self):
        return submit_control(self.pool,'COORDINATOR','accept_requirement',self.target,self.source)

    def review(self,request_id):
        old=(self.package/'TODO.md').read_text()
        return {'request_id':request_id,'task_ids':self.target['task_ids'],
            'summary':'Preserve old criteria; incorporate bounded local proposal.',
            'changes':[{'path':'TODO.md','sha256':hashlib.sha256(old.encode()).hexdigest(),
                        'content':old+'\nLocal approved addendum:\n'+'\n'.join(self.target['task_ids'])+'\n'}]}

    def test_native_request_prepares_registers_accepts_selects_publishes_and_claims_without_reset(self):
        old=self.ledger.snapshot()
        queued=self.assertCommitted(self.queue())
        self.assertFalse(queued['requirement_accepted'])
        self.assertEqual('DUPLICATE',self.queue()['status'])
        self.assertEqual('PAUSED',packages(self.ledger)['new-package']['run_control'])
        self.assertEqual(old['tasks'],self.ledger.snapshot()['tasks'])
        worker,owner=self.bind('COORDINATOR')
        review=self.review(queued['request_id'])
        import workflow_decision_context as context
        from test_semantic_decision import synthetic
        packet,snapshot=synthetic()
        snapshot=self.ledger.snapshot('run-one')
        packet.update(run=next(r for r in snapshot['runs'] if r['run_id']=='run-one'),next_actions=next_actions(snapshot))
        prepared=context.prepare(packet,snapshot,context.directory(packet,snapshot)['items'][0])
        self.assertEqual(queued['request_id'],prepared['facts']['preparation_request']['request_id'])
        self.assertEqual('FOUND',prepared['source_status'])
        from workflow_runtime import _execute_command
        scope=dict(owner,kind='business',pool_path=str(self.pool))
        result=self.assertCommitted(_execute_command(scope,'prepare_requirement',review,command_id='prepare'))
        self.assertTrue(result['requirement_accepted'],result)
        self.assertEqual('run-one',self.ledger.mode()['current_run_id'])
        actions=next_actions(self.ledger.snapshot('run-one'))
        self.assertEqual('activate_requirement',actions[0]['action'])
        self.assertTrue((self.package/'TODO.md').read_text().startswith('OLD-01 historical'))
        self.assertEqual('DUPLICATE',prepare(self.ledger,owner,review,command_id='retry')['status'])
        activated=self.assertCommitted(activate(self.ledger,owner,{'request_id':queued['request_id'],
            'reason':'Provider first; old business work retained.'},command_id='activate-new'))
        self.assertTrue(activated['scope_handoff_required'])
        self.assertEqual('safe_release',activated['next_action']['action'])
        stale=self.ledger.publish_plan('prepared-new-package',1,[self.task('TASK-BR-00','A')],
            identity=owner,command_id='old-scope-cannot-plan')
        self.assertEqual('REJECTED',stale['status'])
        self.assertEqual([], [t for t in self.ledger.snapshot()['tasks'] if t['run_id']=='prepared-new-package'])
        self.assertCommitted(self.ledger.release_work(owner,reason='RUN_SWITCHED',command_id='release-old'))
        self.close(worker,owner)
        new_worker,new_owner=self.bind('COORDINATOR')
        plan=[self.task(t,'A',dependencies=[] if i==0 else [self.target['task_ids'][i-1]])
              for i,t in enumerate(self.target['task_ids'])]
        self.assertCommitted(self.ledger.publish_plan('prepared-new-package',1,plan,identity=new_owner,command_id='new-plan'))
        dev_worker,dev=self.bind('A')
        self.assertEqual('TASK-BR-00',dev['task_id'])
        snap=self.ledger.snapshot()
        self.assertEqual(2,len(snap['runs']))
        self.assertEqual(old['tasks'],[t for t in snap['tasks'] if t['run_id']=='run-one'])
        self.assertEqual(1,len([e for e in snap['events'] if e['kind']=='REQUIREMENT_ACCEPTED']))
        self.assertFalse(next_actions(snap))

    def test_foreign_and_conflicting_edits_stop_before_file_effects(self):
        queued=self.assertCommitted(self.queue());worker,owner=self.bind('COORDINATOR')
        review=self.review(queued['request_id']);before=(self.package/'TODO.md').read_bytes()
        bad=json.loads(json.dumps(review));bad['changes'][0]['path']='../app.py'
        with self.assertRaisesRegex(ValueError,'OUTSIDE_FROZEN_SCOPE'):
            prepare(self.ledger,owner,bad,command_id='foreign')
        (self.package/'TODO.md').write_bytes(before+b'\nintervening human edit')
        result=prepare(self.ledger,owner,review,command_id='changed')
        self.assertEqual('PREPARATION_FILE_CHANGED',result['code'])
        self.assertEqual(before+b'\nintervening human edit',(self.package/'TODO.md').read_bytes())
        self.assertEqual('PAUSED',packages(self.ledger)['new-package']['run_control'])
        # Replaying a rejected command is still a rejection, never a committed
        # preparation. After explicit reconciliation a new attempt can proceed.
        replay=prepare(self.ledger,owner,review,command_id='changed')
        self.assertEqual('REJECTED',replay['original_status'])
        (self.package/'TODO.md').write_bytes(before)
        self.assertTrue(self.assertCommitted(prepare(self.ledger,owner,review,command_id='reconciled'))['requirement_accepted'])

    def test_interrupted_file_effect_uses_durable_journal_and_original_review(self):
        queued=self.assertCommitted(self.queue());worker,owner=self.bind('COORDINATOR')
        review=self.review(queued['request_id'])
        import workflow_preparation
        calls=[]
        def interrupted(*args,**kwargs):
            calls.append(True)
            raise OSError('isolated power-loss simulation after atomic file replacement')
        with mock.patch('workflow_registration.freeze_sources',side_effect=interrupted):
            with self.assertRaises(OSError):prepare(self.ledger,owner,review,command_id='first')
        self.assertEqual('STAGED',requests(self.ledger.snapshot())[queued['request_id']]['state'])
        self.assertIn('TASK-BR-03',(self.package/'TODO.md').read_text())
        self.assertEqual('PAUSED',packages(self.ledger)['new-package']['run_control'])
        self.assertTrue(self.assertCommitted(prepare(self.ledger,owner,review,command_id='resume'))['requirement_accepted'])
        self.assertEqual(1,len([e for e in self.ledger.snapshot()['events'] if e['kind']=='REQUIREMENT_PREPARATION_STAGED']))

    def test_provider_decomposition_is_grounded_and_old_task_is_not_restarted(self):
        queued=self.assertCommitted(self.queue());worker,owner=self.bind('COORDINATOR')
        review=self.review(queued['request_id']);review['task_ids']+=['TASK-PROVIDER-01']
        review['changes'][0]['content']+='TASK-PROVIDER-01\n'
        review['additional_task_sources']={'TASK-PROVIDER-01':{'path':'proposal.md',
            'quote':'Source integrity and asynchronous reconciliation provider contract.'}}
        result=self.assertCommitted(prepare(self.ledger,owner,review,command_id='provider'))
        accepted=next(e for e in self.ledger.snapshot()['events'] if e['kind']=='REQUIREMENT_ACCEPTED')
        self.assertIn('TASK-PROVIDER-01',accepted['payload']['task_ids'])
        self.assertNotIn('OLD-01',accepted['payload']['task_ids'])

    def test_redundant_requested_task_citations_accept_exact_frozen_absolute_and_relative_paths(self):
        queued=self.assertCommitted(self.queue());worker,owner=self.bind('COORDINATOR')
        review=self.review(queued['request_id'])
        review['additional_task_sources']={task:{'path':str(self.package/'proposal.md') if n%2 else 'proposal.md',
            'quote':'Source integrity and asynchronous reconciliation provider contract.'}
            for n,task in enumerate(review['task_ids'])}
        self.assertTrue(self.assertCommitted(prepare(self.ledger,owner,review,command_id='redundant'))['requirement_accepted'])

    def test_redundant_citation_never_ignores_bad_evidence_or_outside_task(self):
        queued=self.assertCommitted(self.queue());worker,owner=self.bind('COORDINATOR')
        review=self.review(queued['request_id'])
        before=self.ledger.snapshot();original=(self.package/'TODO.md').read_bytes()
        quote='Source integrity and asynchronous reconciliation provider contract.'
        cases=[('TASK-BR-00',{'path':str(self.root/'other'/'proposal.md'),'quote':quote}),
            ('TASK-BR-00',{'path':'../new-package/proposal.md','quote':quote}),
            ('TASK-BR-00',{'path':'TODO.md','quote':quote}),
            ('TASK-BR-00',{'path':'proposal.md','quote':'Invented evidence never appeared in frozen proposal.'}),
            ('TASK-UNREQUESTED-01',{'path':'proposal.md','quote':quote})]
        for n,(task,citation) in enumerate(cases):
            with self.subTest(task=task,citation=citation):
                review['additional_task_sources']={task:citation}
                with self.assertRaisesRegex(ValueError,'PREPARATION_(TASK_NOT_GROUNDED|NEW_TASK_SOURCE_MAPPING_REQUIRED)'):
                    prepare(self.ledger,owner,review,command_id='invalid-citation-'+str(n))
                self.assertEqual(original,(self.package/'TODO.md').read_bytes())
                self.assertEqual(before['events'],self.ledger.snapshot()['events'])

    def test_missing_provider_citation_and_historical_restart_stay_rejected(self):
        queued=self.assertCommitted(self.queue());worker,owner=self.bind('COORDINATOR')
        review=self.review(queued['request_id'])
        for task in ('TASK-PROVIDER-01','OLD-01'):
            with self.subTest(task=task):
                review['task_ids']=self.target['task_ids']+[task]
                review['additional_task_sources']={}
                with self.assertRaisesRegex(ValueError,'PREPARATION_NEW_TASK_SOURCE_MAPPING_REQUIRED'):
                    prepare(self.ledger,owner,review,command_id='missing-'+task)
        review['additional_task_sources']={'OLD-01':{'path':'proposal.md',
            'quote':'Source integrity and asynchronous reconciliation provider contract.'}}
        with self.assertRaisesRegex(ValueError,'PREPARATION_NEW_TASK_SOURCE_MAPPING_REQUIRED'):
            prepare(self.ledger,owner,review,command_id='historical')

    def test_source_queries_and_unlisted_historical_scope_are_not_enqueued(self):
        for primary in ('现在呢','new-package 的开发状态是什么？','请开发 new-package，纳入合同 TASK-BR-00。'):
            with self.assertRaises(ValueError):
                submit_control(self.pool,'COORDINATOR','accept_requirement',self.target,dict(self.source,primary_text=primary))
        self.assertFalse(requests(self.ledger.snapshot()))

    def test_range_and_bare_answer_recording_do_not_create_permissions(self):
        from workflow_user_decisions import answer_source,record
        self.assertTrue(all(task_in_source(t,self.source['primary_text']) for t in self.target['task_ids']))
        self.assertFalse(task_in_source('TASK-BR-04',self.source['primary_text']))
        self.assertFalse(task_in_source('TASK-XX-01',self.source['primary_text']))
        self.assertEqual('b',answer_source('B')['choice'])
        before=self.ledger.snapshot()
        self.assertCommitted(record(self.ledger,{},dict(self.source,primary_text='B'),command_id='bare-answer'))
        after=self.ledger.snapshot()
        self.assertEqual(before['authorizations'],after['authorizations'])
        self.assertEqual(before['runs'],after['runs'])
        self.assertFalse(requests(after))

    def test_two_packages_queue_durably_without_switching_or_duplicate_runs(self):
        second=self.root/'second-package';second.mkdir()
        (second/'TODO.md').write_text('OLD-SR historical')
        (second/'proposal.md').write_text('TASK-SR-00 TASK-SR-01 TASK-SR-02 TASK-SR-03')
        entry=dict(self.entry,requirement_id='second-package',requirement_root=str(second),task_ids=['OLD-SR'])
        self.assertCommitted(register_packages(self.ledger,[entry],
            {'purpose':'registration_only','primary_text':'second'},command_id='register-second'))
        primary=self.source['primary_text']+' second-package 接纳 TASK-SR-00～03，保留两包既有进展。'
        self.source=dict(self.source,primary_text=primary)
        first=self.assertCommitted(self.queue())
        target=dict(self.target,requirement_id='second-package',requirement_root=str(second),
            task_ids=['TASK-SR-'+str(n).zfill(2) for n in range(4)])
        other=self.assertCommitted(submit_control(self.pool,'COORDINATOR','accept_requirement',target,self.source))
        self.assertNotEqual(first['request_id'],other['request_id'])
        self.assertEqual('run-one',self.ledger.mode()['current_run_id'])
        self.assertEqual(3,len(self.ledger.snapshot()['runs']))
        actions=next_actions(self.ledger.snapshot('run-one'))
        self.assertEqual(2,len(actions))
        self.assertTrue(all(a['action']=='prepare_requirement' for a in actions))

    def test_native_history_recovery_preserves_original_message_and_refuses_status_query(self):
        import sqlite3
        from workflow_preparation import recover_native_request
        self.pool.write_text(json.dumps({'roles':{'COORDINATOR':{'chat_id':'chat'}}}))
        state=self.root/'state.db'
        prefix='[角色群只读/控制请求] 角色：COORDINATOR\n只读说明\n用户原文及附件/引用如下：\n'
        with sqlite3.connect(state) as db:
            db.execute('CREATE TABLE sessions(id,source,user_id,chat_id)')
            db.execute('CREATE TABLE messages(id,session_id,role,content,platform_message_id,timestamp)')
            db.execute('INSERT INTO sessions VALUES(?,?,?,?)',('s','feishu','user','chat'))
            db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?)',(7,'s','user',prefix+self.source['primary_text'],'original-native-id',123))
            db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?)',(8,'s','user',prefix+'现在呢','status-native-id',124))
        result=self.assertCommitted(recover_native_request(self.pool,state,7,self.target,
            expected_primary_sha256=hashlib.sha256(self.source['primary_text'].encode()).hexdigest()))
        item=requests(self.ledger.snapshot())[result['request_id']]
        self.assertEqual('original-native-id',item['provenance']['message_id'])
        self.assertEqual('native_history_recovery',item['provenance']['source_kind'])
        with self.assertRaisesRegex(ValueError,'EXPLICIT_REQUIREMENT_DEVELOPMENT_SOURCE_REQUIRED'):
            recover_native_request(self.pool,state,8,self.target,expected_primary_sha256=hashlib.sha256('现在呢'.encode()).hexdigest())

    def test_registration_json_is_journaled_and_updated_without_rewriting_old_task_scope(self):
        old=dict(self.entry,run_id='prepared-new-package',production_permission=False,development_permission=False)
        (self.package/'REGISTRATION.json').write_text(json.dumps(old))
        self.entry['contract_files'].append('REGISTRATION.json')
        self.assertCommitted(register_packages(self.ledger,[self.entry],
            {'purpose':'registration_only','primary_text':'descriptor'},command_id='register-descriptor'))
        self.target['contract_files'].append('REGISTRATION.json')
        queued=self.assertCommitted(self.queue());worker,owner=self.bind('COORDINATOR')
        self.assertCommitted(prepare(self.ledger,owner,self.review(queued['request_id']),command_id='prepare-json'))
        current=json.loads((self.package/'REGISTRATION.json').read_text())
        self.assertEqual('prepared-new-package',current['run_id'])
        self.assertIn('OLD-01',current['task_ids'])
        self.assertIn('TASK-BR-03',current['task_ids'])
        self.assertFalse(current['production_permission'])

    def test_append_preserves_formal_history_and_keeps_payload_bounded(self):
        queued=self.assertCommitted(self.queue());worker,owner=self.bind('COORDINATOR')
        review=self.review(queued['request_id']);old=(self.package/'TODO.md').read_text()
        change=review['changes'][0];change['append']=change.pop('content')[len(old):]
        from workflow_contracts import validate_payload
        validate_payload('prepare_requirement',review)
        self.assertCommitted(prepare(self.ledger,owner,review,command_id='append'))
        self.assertTrue((self.package/'TODO.md').read_text().startswith(old))
