#!/usr/bin/env python3
"""Keep isolated unittest resources for audit; never a business/service runner.

No real model, production DB, messaging or deployment is used. Actual child
processes and test units are still stopped by the original fixture cleanups.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
for value in (ROOT,ROOT/'tests',Path('/home/wang/.hermes/hermes-agent')):
    sys.path.insert(0,str(value))


def write(path,value):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n'); path.chmod(0o600)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    output=args.output.resolve()
    output.mkdir(parents=True,exist_ok=False,mode=0o700)
    resources=output/'resources';resources.mkdir(mode=0o700)
    from workflow_migration import code_version
    before=code_version()
    write(output/'runtime-version-before.json',before)
    names=[
      'test_workflow_integration.WorkflowIntegrationTests.test_native_worker_admission_injects_current_work_and_report_does_not_take_slot',
      'test_workflow_integration.WorkflowIntegrationTests.test_work_bound_fail_rework_independent_retest_complete_and_single_report',
      'test_workflow_integration.WorkflowIntegrationTests.test_same_work_survives_two_ended_executions_and_old_cursor_cannot_ack_new_event',
      'test_workflow_integration.WorkflowIntegrationTests.test_existing_tick_collects_detached_result_and_restarts_waiting_work_only_on_event',
      'test_workflow_integration.WorkflowIntegrationTests.test_running_shell_and_its_child_stop_on_control_revocation',
      'test_business_ledger.BusinessLedgerTests.test_real_git_delivery_independent_fail_rework_pass_and_completion',
      'test_workflow_merge.WorkflowMergeTests.test_exact_authorized_fast_forward_postcheck_replay_and_explicit_completion',
      'test_workflow_merge.WorkflowMergeTests.test_actual_postmerge_failure_keeps_completion_closed_and_never_retries',
      'test_native_workflow_services.NativeWorkflowServiceTests.test_native_ocr_real_service_survives_caller_exit_with_frozen_coverage',
      'test_native_workflow_services.NativeWorkflowServiceTests.test_native_strong_review_real_service_survives_caller_exit_without_approval',
      'test_native_workflow_services.NativeWorkflowServiceTests.test_revocation_in_dispatch_gap_native_cli_refuses_before_provider_or_unit',
    ]
    current={'directory':resources};original=tempfile.TemporaryDirectory
    def preserved(*positional,**keywords):
        keywords['dir']=str(current['directory'])
        temporary=original(*positional,**keywords)
        temporary._finalizer.detach()
        temporary.cleanup=lambda:None
        return temporary
    class Result(unittest.TextTestResult):
        def startTest(self,test):
            directory=resources/test.id();directory.mkdir(mode=0o700)
            current['directory']=directory
            super().startTest(test)
    suite=unittest.defaultTestLoader.loadTestsFromNames(names)
    tempfile.TemporaryDirectory=preserved
    started=datetime.now(timezone.utc).isoformat()
    try:
        with (output/'unittest.log').open('w') as stream:
            result=unittest.TextTestRunner(stream=stream,verbosity=2,resultclass=Result).run(suite)
    finally:
        tempfile.TemporaryDirectory=original
    ended=datetime.now(timezone.utc).isoformat()
    after=code_version();write(output/'runtime-version-after.json',after)
    exports=[];repos=[];services=[]
    from business_ledger import BusinessLedger
    for database in resources.rglob('business.sqlite3'):
        target=database.with_name('business-export.json')
        write(target,BusinessLedger(database,readonly=True).snapshot())
        with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True) as connection:
            quick_check=connection.execute('PRAGMA quick_check').fetchone()[0]
        exports.append({'database':str(database),'export':str(target),'sqlite_quick_check':quick_check})
    for gitdir in resources.rglob('.git'):
        if not gitdir.is_dir(): continue
        repo=gitdir.parent
        def git(*arguments):
            run=subprocess.run(['git','-C',str(repo),*arguments],capture_output=True,text=True)
            return {'exit_code':run.returncode,'stdout':run.stdout}
        repos.append({'repo':str(repo),'head':git('rev-parse','HEAD'),'status':git('status','--porcelain'),
                      'log':git('log','--oneline','-5')})
    for path in resources.rglob('spec.json'):
        try: spec=json.loads(path.read_text())
        except (OSError,ValueError): continue
        if not spec.get('unit'): continue
        info=subprocess.run(['systemctl','--user','show',spec['unit'],
            '--property=LoadState,ActiveState,MainPID,ControlGroup,Result,ExecMainStatus'],capture_output=True,text=True)
        services.append({'spec':str(path),'unit':spec['unit'],'runtime_version_digest':spec.get('runtime_version_digest'),
                         'scope':{key:spec.get(key) for key in ('repo','base','head','target','task','task_id','attempt')},
                         'post_cleanup_observation':{'exit_code':info.returncode,'stdout':info.stdout},
                         'live_activation_proof':False})
    failed={test.id():detail for test,detail in result.failures+result.errors}
    suite_xml=ET.Element('testsuite',name='isolated-role-redesign-validation',tests=str(result.testsRun),
                         failures=str(len(result.failures)),errors=str(len(result.errors)),skipped=str(len(result.skipped)))
    for name in names:
        case=ET.SubElement(suite_xml,'testcase',name=name)
        if name in failed: ET.SubElement(case,'failure').text=failed[name]
    ET.ElementTree(suite_xml).write(output/'unittest.xml',encoding='utf-8',xml_declaration=True)
    files=[]
    for path in output.rglob('*'):
        if path.is_file() and not path.is_symlink():
            files.append({'path':str(path),'bytes':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    exit_code=0 if result.wasSuccessful() and before['digest']==after['digest'] else 1
    manifest={'schema_version':1,'kind':'isolated-unittest-capture','command':[sys.executable,str(Path(__file__).resolve()),*sys.argv[1:]],
              'started_at':started,'ended_at':ended,'exit_code':exit_code,'tests':names,
              'tests_run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),
              'runtime_version_digest':before['digest'],'runtime_version_unchanged':before['digest']==after['digest'],
              'resources':str(resources),'ledger_exports':exports,'git_repositories':repos,'native_services':services,'files':files,
              'real_model_calls':0,'external_messages':0,'production_writes':False,'providers':'fake executables only',
              'actual_resources':'real SQLite, Git, local subprocesses and user-systemd units',
              'activation_gate_receipt':False,'limits':'测试证据不是生产激活回执。原fixture已停止真实子进程并清理独立unit；不能拿清理后的unit伪造实时T2 proof。'}
    write(output/'manifest.json',manifest)
    print(json.dumps({'output':str(output),'manifest':str(output/'manifest.json'),'exit_code':exit_code,'tests_run':result.testsRun}))
    return exit_code


if __name__=='__main__':raise SystemExit(main())
