"""Regress the real preflight failure with isolated Git, ledger and plugin calls."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from unittest.mock import patch

from tests.test_business_ledger import LedgerFixture
from tests.test_report_contract import fixture
import workflow_runtime as workflow
from workflow_query import (live_progress, repo_state, HISTORY_NOTICE,
    report_presentation_contract, _paged_artifact)


class WorkflowQueryTests(LedgerFixture):
    def setUp(self):
        super().setUp()
        self.pool = self.root/'isolated.json'
        self.pool.write_text('{}')

    @staticmethod
    def artifact_map(packet):
        reference=packet['artifacts']['manifest']
        return json.loads(Path(reference['path']).read_text())

    @staticmethod
    def read_paged(reference):
        manifest=json.loads(Path(reference['path']).read_text())
        serialized=[]
        for index_ref in manifest['indexes']:
            index=json.loads(Path(index_ref['path']).read_text())
            for page_ref in index['pages']:
                page=json.loads(Path(page_ref['path']).read_text())
                assert page_ref['bytes'] <= manifest['page_limit_bytes']
                serialized.append(page['data'])
        return json.loads(''.join(serialized)),manifest

    def test_current_global_gate_wins_over_stale_checkpoint_and_invocation(self):
        worker, identity = self.bind('A')
        cid = self.candidate(identity)[0]
        self.close(worker, identity)
        worker, coord = self.bind('COORDINATOR')
        cp = self.ledger.checkpoint(coord, {'evidence': '先修沙箱；OCR UNKNOWN', 'next': '旧建议'}, command_id='cp')
        self.close(worker, coord)
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT INTO external_actions(action_id,candidate_id,kind,authorization,state,created_at) VALUES(?,?,?,?,?,?)',
                       ('old-action', cid, 'ocr', '{}', 'INCOMPLETE', self.now))
        folder = self.path.parent/'services'/'old'; folder.mkdir(parents=True)
        sidecar = folder/'invocation.json'
        sidecar.write_text('{"action_id":"old-action","state":"SUBMITTED"}')
        before = self.ledger.snapshot()
        with patch('workflow_observation.saved_health', return_value={'raw': 'UNKNOWN'*500000}):
            packet = live_progress(self.pool, 'COORDINATOR')
        self.assertLess(len(json.dumps(packet, ensure_ascii=False).encode()), 8192)
        self.assertFalse(packet['ocr_serial_gate']['blocked'])
        self.assertEqual('INCOMPLETE', packet['external_actions'][0]['state'])
        self.assertNotIn('先修沙箱', json.dumps(packet, ensure_ascii=False))
        artifacts=self.artifact_map(packet)
        reference = artifacts['history']
        raw = Path(reference['path']).read_bytes()
        self.assertEqual(reference['sha256'], hashlib.sha256(raw).hexdigest())
        index=json.loads(raw)
        self.assertIn(HISTORY_NOTICE, index['history_policy'])
        detail,_=self.read_paged(index['detail_manifest'])
        history = next(w for w in detail['records'] if w['role'] == 'COORDINATOR')['checkpoint_history']
        self.assertEqual(cp['event_id'], history['event_id'])
        self.assertIn(HISTORY_NOTICE, history['notice'])
        self.assertIn('2026-10-19T12:00:00+08:00', history['recorded_at'])
        self.assertFalse(history['current_verified'])
        self.assertEqual(before, self.ledger.snapshot())
        self.assertEqual('SUBMITTED', json.loads(sidecar.read_text())['state'])

    def test_ocr_gate_is_global_even_when_selected_run_has_no_candidates(self):
        worker, identity = self.bind('A')
        cid = self.candidate(identity)[0]
        self.close(worker, identity)
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT INTO external_actions(action_id,candidate_id,kind,authorization,state,created_at) VALUES(?,?,?,?,?,?)',
                       ('unknown-other-run', cid, 'ocr', '{}', 'UNKNOWN', self.now))
        self.ledger.create_run('new', self.root, self.repo, ['N1'], run_id='new', command_id='new')
        self.ledger.select_run('new', command_id='select-new')
        packet = live_progress(self.pool, 'COORDINATOR')
        self.assertEqual('new', packet['run']['run_id'])
        self.assertTrue(packet['ocr_serial_gate']['blocked'])
        self.assertEqual(1, packet['ocr_serial_gate']['blocking_count'])
        self.assertIn('OCR_SERIAL_ACTION_STILL_ACTIVE_OR_UNKNOWN', [g['code'] for g in packet['open_gates']])

    def test_live_plan_exposes_risk_and_environment_contract_without_weakening_legacy_plan(self):
        worker, coord = self.bind('COORDINATOR')
        scenario={'id':'R1','scenario':'provider times out','invariant':'retry remains bounded',
                  'verification':'timeout test'}
        planned={**self.plan[0],'minimum_reading_set':['app.py:value'],
            'risk_scenarios':[scenario], 'test_environment':{'python':'3.11','command':'unittest'},
            'escalation_conditions':['provider contract changes']}
        self.assertCommitted(self.ledger.publish_plan('run-one',2,[planned,self.plan[1]],
            identity=coord,command_id='risk-plan'))
        packet=live_progress(self.pool,'COORDINATOR')
        task=next(item for item in packet['tasks'] if item['task_id']=='T1')
        self.assertEqual([scenario],task['risk_scenarios'])
        self.assertEqual(['app.py:value'],task['minimum_reading_set'])
        self.assertEqual({'python':'3.11','command':'unittest'},task['test_environment'])
        self.assertEqual(['provider contract changes'],task['escalation_conditions'])
        self.close(worker,coord)

    def test_live_query_carries_complete_role_specific_presentation_contract(self):
        self.assertEqual(['整体目标与进度','执行者进度','测试者进度','OCR情况和后续计划',
                          '强审查情况','风险','下一步计划'],
                         report_presentation_contract('COORDINATOR')['sections'])
        for role in ('A','B','C','TESTER'):
            self.assertEqual(['当前工作','本次完成','阻塞与依赖','下一步计划'],
                             report_presentation_contract(role)['sections'])
        self.assertIn('状态/结论',report_presentation_contract('COORDINATOR')['body_style'])

    def test_large_action_history_is_explicitly_paged_without_losing_gate_count(self):
        worker, identity = self.bind('A')
        cid = self.candidate(identity)[0]
        self.close(worker, identity)
        with sqlite3.connect(self.path) as db:
            db.executemany('INSERT INTO external_actions(action_id,candidate_id,kind,authorization,state,created_at) VALUES(?,?,?,?,?,?)',
                [('action-'+str(i), cid, 'ocr', '{}', 'UNKNOWN', self.now)
                 for i in range(100)])
        packet = live_progress(self.pool, 'COORDINATOR')
        self.assertLess(len(json.dumps(packet, ensure_ascii=False).encode()), 8192)
        self.assertTrue(packet['ocr_serial_gate']['blocked'])
        self.assertEqual(100,packet['ocr_serial_gate']['blocking_count'])
        self.assertEqual('UNKNOWN', packet['external_actions'][0]['state'])
        self.assertGreater(packet['omitted_counts']['external_actions'], 0)
        self.assertEqual(2,len(packet['tasks']))
        self.assertEqual(5,len(packet['roles']))
        facts = json.loads(Path(self.artifact_map(packet)['facts']['path']).read_text())
        self.assertEqual(100, len(facts['external_actions']))

    def test_large_history_artifact_is_losslessly_paged_below_query_budget(self):
        value={'history_policy':HISTORY_NOTICE,
            'records':[{'work_id':str(i),'role':'COORDINATOR','checkpoint':
                {'evidence':'状态核验完成。' * 40,'next':'继续检查MN任务。' * 30}}
                for i in range(90)]}
        reference=_paged_artifact(self.ledger,'large-history',value)
        recovered,manifest=self.read_paged(reference)
        self.assertEqual(value,recovered)
        self.assertEqual(90,len(recovered['records']))
        self.assertGreater(manifest['page_count'],1)
        self.assertLessEqual(max(ref['bytes'] for group in manifest['indexes']
            for ref in json.loads(Path(group['path']).read_text())['pages']),6144)

    def test_repo_query_observes_changes_without_index_writes_or_shell_hooks(self):
        self.git('branch', 'upstream')
        self.git('branch', '--set-upstream-to=upstream')
        (self.repo/'tracked file.py').write_text('content')
        self.git('add', '.'); self.git('commit', '-qm', 'ahead')
        self.git('mv', 'tracked file.py', 'renamed\nfile.py')
        (self.repo/'untracked file').write_text('dirty')
        marker = self.root/'hook-ran'
        hook = self.root/'fsmonitor'
        hook.write_text('#!/bin/sh\ntouch '+str(marker)+'\n');hook.chmod(0o700)
        self.git('config', 'core.fsmonitor', str(hook))
        index = self.repo/'.git/index'
        before = index.read_bytes()
        packet = repo_state(self.pool, 'COORDINATOR')
        self.assertEqual('OBSERVED', packet['state'])
        self.assertEqual(self.git('rev-parse', 'HEAD'), packet['head'])
        self.assertTrue(packet['dirty'])
        self.assertEqual(2, packet['dirty_count'])
        self.assertEqual(1, packet['ahead_behind']['ahead'])
        self.assertTrue(any('renamed\nfile.py' in row['record'] for row in packet['dirty_files']))
        self.assertEqual(before, index.read_bytes())
        self.assertFalse(marker.exists())

    def test_repo_failure_or_concurrent_change_never_reports_clean(self):
        with patch('workflow_query.subprocess.check_output', side_effect=subprocess.TimeoutExpired('git', 15)):
            packet = repo_state(self.pool, 'COORDINATOR')
        self.assertEqual('UNVERIFIED', packet['state'])
        self.assertIsNone(packet['dirty'])
        with patch('workflow_query.subprocess.check_output', side_effect=[
                b'old', b'# branch.head main\0', b'worktree /repo\0', b'new', b'# branch.head main\0']):
            packet = repo_state(self.pool, 'COORDINATOR')
        self.assertEqual('REPOSITORY_CHANGED_DURING_QUERY', packet['reason'])
        self.assertIsNone(packet['dirty'])

    def test_system_git_234_fallback_keeps_branch_dirty_and_quoted_worktrees(self):
        import os
        (self.repo/'untracked doc.md').write_text('preserve')
        before=(self.repo/'.git/index').read_bytes()
        # Exercise the actual binary used by cron, not the shell's newer Git.
        with patch.dict(os.environ, {'PATH':'/usr/bin:/bin'}):
            packet=repo_state(self.pool,'COORDINATOR')
        self.assertEqual('OBSERVED',packet['state'])
        self.assertTrue(packet['dirty'])
        self.assertEqual(self.git('rev-parse','HEAD'),packet['head'])
        self.assertIn('worktree '+str(self.repo),packet['worktrees'])
        self.assertEqual(before,(self.repo/'.git/index').read_bytes())

    def test_matching_head_index_blobs_do_not_hide_real_unstaged_content(self):
        index=self.repo/'.git/index'
        original=index.read_bytes()
        (self.repo/'app.py').write_text('def value():\n    return 123\n')
        packet=repo_state(self.pool,'COORDINATOR')
        raw=next(row['record'] for row in packet['dirty_files'] if row['record'].startswith('1 '))
        fields=raw.split(' ',8)
        self.assertEqual(fields[7],fields[6])  # HEAD/index equality is normal here.
        self.assertTrue(packet['dirty'])
        self.assertEqual('OBSERVED',packet['content_evidence_state'])
        self.assertEqual(1,packet['tracked_content_change_count'])
        self.assertEqual([{'path':'app.py','added':1,'deleted':1}],packet['unstaged_content_changes'])
        self.assertEqual(original,index.read_bytes())

    def test_untracked_file_is_not_attributed_or_called_safe_to_delete(self):
        (self.repo/'user handoff.md').write_text('valuable uncommitted delivery')
        packet=repo_state(self.pool,'COORDINATOR')
        self.assertTrue(packet['dirty'])
        self.assertEqual(0,packet['tracked_content_change_count'])
        self.assertTrue(packet['interpretation']['ownership'].startswith('未核实'))
        self.assertIn('grant.repo',packet['interpretation']['merge_location'])
        self.assertEqual('valuable uncommitted delivery',(self.repo/'user handoff.md').read_text())

    def test_group_tools_need_no_full_grant_and_cannot_choose_repository_or_shell(self):
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location('query_plugin_test', root/'__init__.py')
        module = importlib.util.module_from_spec(spec);sys.modules[spec.name] = module;spec.loader.exec_module(module)
        registry = self.root/'registry.json'; registry.write_text('{"projects":[]}')
        plugin = module.RuntimePlugin(registry=registry, metrics_url='unused',
                                      home=self.root, pool_path=self.pool)
        identity = {'role': 'COORDINATOR', 'project': str(self.root), 'session_kind': 'group_chat'}
        plugin.manual_turns[('group', 'turn')] = identity
        with patch.object(plugin, 'bind_manual_turn', return_value=identity):
            for name in ('role_live_progress', 'role_repo_state'):
                self.assertIsNone(plugin.on_tool(tool_name=name, session_id='group', turn_id='turn'))
            self.assertEqual('block', plugin.on_tool(tool_name='terminal', session_id='group', turn_id='turn')['action'])
        packet = json.loads(plugin.live_tool({}, session_id='group', turn_id='turn'))
        self.assertLess(len(json.dumps(packet, ensure_ascii=False).encode()), 8192)
        self.assertEqual(str(self.repo), packet['repository']['repo'])
        self.assertFalse(packet['repository']['dirty'])
        self.assertEqual(str(self.repo), json.loads(plugin.repo_tool({}, session_id='group', turn_id='turn'))['repo'])
        with self.assertRaisesRegex(ValueError, '不接受自选'):
            plugin.repo_tool({'repo': '/etc'}, session_id='group', turn_id='turn')
        with self.assertRaisesRegex(ValueError, '未绑定'):
            plugin.repo_tool({}, session_id='unbound', turn_id='turn')

    def test_current_input_labels_checkpoint_and_cancelled_candidates_have_no_next_action(self):
        worker, identity = self.bind('A')
        cid = self.candidate(identity)[0]
        self.close(worker, identity)
        worker, coord = self.bind('COORDINATOR')
        saved = self.ledger.checkpoint(coord, {'next': '历史 OCR 建议'}, command_id='cp')
        packet = workflow.current_input(self.pool, 'COORDINATOR')
        self.assertEqual(saved['event_id'], packet['work']['checkpoint_history']['event_id'])
        # A realistic retained candidate plus cancelled task must not suggest review/rework.
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE tasks SET phase='CANCELLED' WHERE task_id='T1'")
        packet = workflow.current_input(self.pool, 'COORDINATOR')
        self.assertFalse(any(row.get('candidate_id') == cid for row in packet['next_actions']))
        progress = live_progress(self.pool, 'COORDINATOR')
        row = next(t for t in progress['tasks'] if t['task_id'] == 'T1')
        self.assertEqual('CANCELLED', row['phase'])
        self.assertEqual(cid, row['candidate_id'])
        self.close(worker, coord)

    def test_rendered_checkpoint_suggestion_is_explicitly_historical(self):
        from report_contract import build_report, render_report
        packet = fixture()
        packet['runtime_observation'] = {'checked_at': '2026-10-22T12:45:00+08:00', 'roles': {
            'A': {'state': 'VERIFIED', 'owner': {'state': 'none'}, 'checkpoint': {'value': {
                'task': 'TASK-16C-A', 'recorded_at': '2026-10-22T10:55:00+08:00',
                'next': '先修测试沙箱'}}}}, 'errors': []}
        report = build_report(packet)
        text = render_report(report)
        self.assertIn('历史叙述 · 写于 2026-10-22 10:55 +08:00', text)
        self.assertIn(HISTORY_NOTICE, text)
        suggestion = next(i['text'] for s in report['sections'] for i in s['items'] if '先修测试沙箱' in i['text'])
        self.assertIn(HISTORY_NOTICE, suggestion)
