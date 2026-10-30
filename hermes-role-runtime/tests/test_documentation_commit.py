"""Real Git tests for coordinator documentation judgment/commit boundaries."""
import hashlib
import json
from unittest import mock

from tests.test_business_ledger import LedgerFixture
from workflow_documentation import commit, inspect
import workflow_runtime as workflow


class DocumentationCommitTests(LedgerFixture):
    def review(self, path='docs/guide.md', content='已核对的使用说明\n'):
        file = self.repo/path; file.parent.mkdir(exist_ok=True, parents=True)
        file.write_text(content)
        return {'path': path, 'sha256': hashlib.sha256(file.read_bytes()).hexdigest(),
                'review': '已核对当前代码行为与任务范围，不改变需求、权限或验收标准。'}

    def payload(self, *entries):
        return {'files': list(entries), 'expected_head': self.git('rev-parse','HEAD'),
                'expected_branch': self.git('symbolic-ref','--short','HEAD'),
                'summary': 'docs: 提交已审核的普通使用说明'}

    def test_native_commit_preserves_staged_and_unstaged_code_and_replays_once(self):
        _, coord = self.bind('COORDINATOR')
        entry = self.review()
        (self.repo/'app.py').write_text('staged code\n'); self.git('add','app.py')
        (self.repo/'app.py').write_text('unstaged code\n')
        staged = self.git('show', ':app.py')
        payload = self.payload(entry)
        scope = {'pool_path': str(self.root/'isolated.json'), 'kind': 'business',
                 'role': 'COORDINATOR', 'run_id':'run-one', **coord}
        with mock.patch('workflow_runtime.open_ledger', return_value=self.ledger):
            result = self.assertCommitted(workflow.command(scope, 'commit_documentation', payload, command_id='doc'))
            replay = workflow.command(scope, 'commit_documentation', payload, command_id='doc')
        self.assertEqual('DUPLICATE', replay['status'])
        self.assertEqual('docs/guide.md', self.git('diff-tree','--no-commit-id','--name-only','-r',result['commit']))
        self.assertEqual(self.base, self.git('rev-parse','HEAD^'))
        self.assertEqual(staged, self.git('show', ':app.py'))
        self.assertEqual('unstaged code\n', (self.repo/'app.py').read_text())
        self.assertEqual('app.py', self.git('diff','--cached','--name-only'))
        self.assertEqual(1, len([e for e in self.ledger.events() if e['kind']=='DOCUMENTATION_COMMITTED']))

    def test_readonly_review_has_full_text_diff_and_digest_without_index_write(self):
        self.review(); before = (self.repo/'.git/index').read_bytes()
        result = inspect(self.repo)
        self.assertTrue(result['stable_head'])
        self.assertEqual('docs/guide.md', result['documents'][0]['path'])
        self.assertIn('已核对', result['documents'][0]['content'])
        self.assertEqual(before, (self.repo/'.git/index').read_bytes())
        (self.repo/'docs/guide.md').write_text('新变化\n')
        self.assertNotEqual(result['documents'][0]['sha256'], inspect(self.repo)['documents'][0]['sha256'])

    def test_changed_after_review_stale_head_and_branch_are_rejected_without_commit(self):
        _, coord = self.bind('COORDINATOR'); entry = self.review(); payload = self.payload(entry)
        (self.repo/'docs/guide.md').write_text('另一个会话的新内容\n')
        result = commit(self.ledger, coord, payload, command_id='changed')
        self.assertIn('DOCUMENTATION_CHANGED_AFTER_REVIEW', result['code'])
        self.assertEqual(self.base, self.git('rev-parse','HEAD'))
        self.assertEqual('DOCUMENTATION_HEAD_CHANGED', commit(self.ledger, coord,
            {**payload,'expected_head':'0'*40}, command_id='head')['code'])
        self.assertEqual('DOCUMENTATION_BRANCH_CHANGED', commit(self.ledger, coord,
            {**payload,'expected_branch':'wrong'}, command_id='branch')['code'])

    def test_code_contracts_instructions_symlinks_and_traversal_are_not_ordinary_docs(self):
        _, coord = self.bind('COORDINATOR')
        for i, name in enumerate(['app.py','AGENTS.md','docs/PRD.md','docs/acceptance-contract.md',
                                  'docs/../HANDOFF.md','docs/.git/secret.md','docs/instructions.md']):
            entry = {'path':name,'sha256':'0'*64,'review':'不能由普通文档入口变更'}
            result = commit(self.ledger, coord, self.payload(entry), command_id='deny'+str(i))
            self.assertEqual('REJECTED', result['status'], result)
        external=self.root/'outside.md'; external.write_text('outside')
        (self.repo/'docs').mkdir(exist_ok=True)
        (self.repo/'docs/link.md').symlink_to(external)
        result=commit(self.ledger,coord,self.payload({'path':'docs/link.md',
            'sha256':hashlib.sha256(b'outside').hexdigest(),'review':'symlink'}),command_id='link')
        self.assertEqual('DOCUMENTATION_SYMLINK_REJECTED',result['code'])
        self.assertEqual('outside',external.read_text())

    def test_developer_cannot_commit_shared_documents_and_busy_index_is_preserved(self):
        _, dev = self.bind('A'); entry=self.review(); payload=self.payload(entry)
        self.assertEqual('COORDINATOR_REQUIRED',commit(self.ledger,dev,payload,command_id='dev')['code'])
        _, coord=self.bind('COORDINATOR')
        lock=self.repo/'.git/index.lock';lock.write_text('other git operation')
        result=commit(self.ledger,coord,payload,command_id='busy')
        self.assertEqual('REJECTED',result['status'])
        self.assertEqual('other git operation',lock.read_text())
        self.assertEqual(self.base,self.git('rev-parse','HEAD'))

    def test_no_hooks_no_unreviewed_extra_paths_and_crash_replay_uses_durable_receipt(self):
        _, coord=self.bind('COORDINATOR'); entry=self.review(); other=self.review('docs/other.md','keep\n')
        hook=self.repo/'.git/hooks/pre-commit';hook.write_text('#!/bin/sh\nexit 99\n');hook.chmod(0o755)
        payload=self.payload(entry)
        result=self.assertCommitted(commit(self.ledger,coord,payload,command_id='crash'))
        with self.ledger._connection() as db:
            db.execute("DELETE FROM commands WHERE command_id='crash'");db.commit()
        replay=self.assertCommitted(commit(self.ledger,coord,payload,command_id='crash'))
        self.assertTrue(replay['replayed'])
        self.assertEqual(result['commit'],self.git('rev-parse','HEAD'))
        self.assertEqual('?? docs/other.md',self.git('status','--porcelain'))

    def test_partial_side_effect_intent_is_not_blindly_replayed(self):
        _, coord=self.bind('COORDINATOR'); entry=self.review(); payload=self.payload(entry)
        result=self.assertCommitted(commit(self.ledger,coord,payload,command_id='partial'))
        path=__import__('pathlib').Path(result['receipt'])
        intent=json.loads(path.read_text());intent.pop('result');path.write_text(json.dumps(intent))
        with self.ledger._connection() as db:
            db.execute("DELETE FROM commands WHERE command_id='partial'");db.commit()
        replay=commit(self.ledger,coord,payload,command_id='partial')
        self.assertEqual('UNVERIFIED',replay['status'])
        self.assertIn('RECONCILIATION',replay['code'])
        self.assertEqual(result['commit'],self.git('rev-parse','HEAD'))
