"""Isolated history/current review reporting; never alter production records."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tests.test_business_ledger import LedgerFixture
from workflow_query import review_action_projection,live_progress,_artifact
from workflow_reports import render_live_progress_fallback,_coordinator_progress_format_error


def synthetic():
    # Explicit synthetic lifecycle fixture, not real review evidence.
    return {'tasks':[{'run_id':'r','task_id':'T','attempt':2,'phase':'REVIEW'}],
        'candidates':[{'candidate_id':'old','run_id':'r','task_id':'T','attempt':1,'state':'SUPERSEDED'},
            {'candidate_id':'new','run_id':'r','task_id':'T','attempt':2,'state':'CURRENT'}],
        'external_actions':[{'action_id':name,'candidate_id':candidate,'kind':'ocr','state':state}
            for name,candidate,state in [('old-incomplete','old','INCOMPLETE'),
                ('old-unknown','old','UNKNOWN'),('old-running','old','SUBMITTED'),
                ('new-incomplete','new','INCOMPLETE'),('new-ready','new','RESULT_READY')]],
        'verifications':[{'candidate_id':'new','check_name':'ocr','result':'PASS'}]}


class ProjectionTests(unittest.TestCase):
    def test_old_incomplete_is_history_but_live_unknown_and_ready_are_not_hidden(self):
        snapshot=synthetic();before=copy.deepcopy(snapshot)
        value=review_action_projection(snapshot,'r')
        self.assertEqual(['old-unknown','old-running','new-ready','new-incomplete'],[a['action_id'] for a in value['attention']])
        self.assertEqual(1,value['historical_incomplete_count'])
        self.assertEqual({'T':1},value['historical_incomplete_by_task'])
        self.assertEqual((1,1),(value['current_ocr_pass_count'],value['current_ocr_candidate_count']))
        self.assertEqual(snapshot,before)

    def test_candidate_flag_alone_cannot_override_attempt_or_cancelled_task(self):
        snapshot=synthetic();snapshot['candidates'][0]['state']='CURRENT'
        self.assertNotIn('old-incomplete',[a['action_id'] for a in review_action_projection(snapshot,'r')['attention']])
        snapshot['tasks'][0]['phase']='CANCELLED'
        value=review_action_projection(snapshot,'r')
        self.assertNotIn('new-ready',[a['action_id'] for a in value['attention']])
        self.assertEqual(0,value['current_ocr_candidate_count'])
        self.assertIn('old-unknown',[a['action_id'] for a in value['attention']])


class ReportProjectionTests(LedgerFixture):
    def test_real_rework_query_retains_old_records_without_reactivating_them(self):
        self.pool=self.root/'isolated.json';self.pool.write_text('{}')
        proc,dev=self.bind('A');cid,_=self.candidate(dev);self.close(proc,dev)
        proc,coord=self.bind('COORDINATOR')
        # Explicit isolated service rows exercise reporting, not service execution.
        with self.ledger._connection() as db:
            for name,state in [('old-incomplete','INCOMPLETE'),('old-unknown','UNKNOWN')]:
                db.execute('INSERT INTO external_actions(action_id,candidate_id,kind,authorization,state,created_at) VALUES(?,?,?,?,?,?)',
                    (name,cid,'ocr','{}',state,self.now))
        self.assertCommitted(self.ledger.decide(coord,cid,'REWORK',command_id='isolate-real-rework'))
        before=self.ledger.snapshot()
        with patch('workflow_query.ocr_gate_state',return_value={'state':'OPEN'}):
            packet=live_progress(self.pool,'COORDINATOR')
        self.assertLess(len(json.dumps(packet,ensure_ascii=False).encode()),8192)
        self.assertTrue(packet['ocr_serial_gate']['blocked'])
        self.assertEqual(['old-unknown'],[a['action_id'] for a in packet['external_actions']])
        self.assertEqual(1,packet['external_action_summary']['historical_incomplete_count'])
        manifest=json.loads(Path(packet['artifacts']['manifest']['path']).read_text())
        facts=json.loads(Path(manifest['facts']['path']).read_text())
        self.assertEqual({'old-incomplete','old-unknown'},{a['action_id'] for a in facts['external_actions']})
        text=render_live_progress_fallback(packet,'COORDINATOR')
        self.assertNotIn('待处置审查',text);self.assertIn('旧候选遗留记录',text)
        self.assertIn('原生结果未知',text);self.assertEqual(before,self.ledger.snapshot())

    def render(self,context):
        ref=_artifact(self.ledger,'isolated-context',context)
        manifest=_artifact(self.ledger,'isolated-manifest',{'report_context':ref})
        packet={'run':{'run_id':'r','requirement_id':'isolated'},'event_high_water':10,
            'checked_at':'2026-10-30T17:41:00+08:00','tasks':[],
            'roles':[{'role':'TESTER','control':'ACTIVE','current_work_id':'new-work','writer_execution_id':'new-execution'}],
            'completion':{'state':'VERIFYING','validation_task_id':'FINAL'},
            'open_gates':[{'code':'VERIFICATION_EXCEPTION_HANDOFF_REQUIRED'}],
            'artifacts':{'manifest':manifest}}
        return render_live_progress_fallback(packet,'COORDINATOR')

    def test_fallback_distinguishes_incomplete_from_ready_and_names_real_next_step(self):
        context={'run_id':'r','event_high_water':10,'review_activity':review_action_projection(synthetic(),'r')}
        context['verification_exceptions']=[{'blocking':True,'task_id':'T','state':'AWAITING_COORDINATOR_DISPOSITION',
            'observation_event_id':9}]
        text=self.render(context)
        self.assertIn('审查待裁决',text);self.assertIn('结果已落账',text)
        self.assertIn('审查未完成',text);self.assertIn('缺少可裁决的完整结果',text)
        self.assertNotIn('old-incomplete',text)
        self.assertIn('协调者异常处置',text);self.assertIn('不等最终集成先失败',text)
        self.assertIn('补验异常待协调者核证处置',text)
        self.assertIsNone(_coordinator_progress_format_error(text))

    def test_previous_work_checkpoint_keeps_reference_without_replaying_stale_body(self):
        context={'run_id':'r','event_high_water':10,'roles':{'TESTER':{'work_id':'retired-work',
            'recorded_at':'2026-10-30T17:26:00+08:00','event_id':8,
            'summary':'isolated stale event pagination narrative'}}}
        text=self.render(context)
        self.assertIn('前一工作单记录',text);self.assertIn('event:8',text)
        self.assertNotIn('isolated stale event pagination narrative',text)
        self.assertIn('2026-10-30T17:26',text)

    def test_long_current_review_list_exposes_unexpanded_count_and_state(self):
        activity=review_action_projection(synthetic(),'r')
        activity['attention'].extend([{**activity['attention'][-1],'action_id':'isolated-extra-'+str(i)} for i in range(3)])
        text=self.render({'run_id':'r','event_high_water':10,'review_activity':activity})
        self.assertIn('其他审查待办',text);self.assertIn('另有 3 项',text)
        self.assertIn('INCOMPLETE 3 项',text);self.assertIn('不表示已处理',text)

    def test_strong_review_history_is_not_reported_as_current_or_never_existing(self):
        context={'run_id':'r','event_high_water':10,'strong_review_history_count':3,'strong_review_actions':[]}
        text=self.render(context)
        self.assertIn('当前没有待处置或在途强审动作',text);self.assertIn('历史共 3 项',text)
        self.assertNotIn('未登记强审动作',text)
