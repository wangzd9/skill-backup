#!/usr/bin/env python3
"""角色确定性工具入口；可从 Skill scripts 的软链接运行。"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sys
import subprocess

sys.path.insert(0,str(Path(__file__).resolve().parent))
from coord_queue import pending_requests, acknowledge_request, enqueue
from load_metrics import collect, render
from operation_jobs import execute_plan, start_job, inspect_state, preflight, operation_states, _submission_error
from runtime_common import read_json, save_json, locked
from role_records import record_role
from delivery_evidence import check_evidence, execute_with_evidence, validate_evidence


def send_claim_notice(row):
    """Use Hermes' existing native platform credentials; never echo raw errors."""
    chat = row.get('chat_id')
    if not isinstance(chat, str) or not chat or any(ord(c) < 32 for c in chat):
        raise ValueError('领取通知没有可信绑定群')
    # This is the separate, authorized claim event. Cron's final response must
    # not repeat it; inheriting its auto-final guard would silently skip this send.
    environment = {key: value for key, value in os.environ.items()
                   if key not in {'HERMES_CRON_AUTO_DELIVER_PLATFORM', 'HERMES_CRON_AUTO_DELIVER_CHAT_ID',
                                  'HERMES_CRON_AUTO_DELIVER_THREAD_ID'}}
    run = subprocess.run(['hermes', 'send', '--to', 'feishu:'+chat, '--json', '--file', '-'],
                         input=row['text'], text=True, capture_output=True, timeout=45, env=environment)
    try: result = json.loads(run.stdout)
    except (ValueError, TypeError): result = {}
    return {'delivered': run.returncode == 0 and result.get('success') is True
                         and not result.get('skipped') and bool(result.get('message_id')),
            'message_id': result.get('message_id'), 'exit_code': run.returncode}


def refresh_progress_facts(project, *, at=None, snapshot=None):
    """Publish a replaceable report view with an immutable, matching evidence file."""
    from progress_projection import build_projection, reporting_packet
    root=Path(project).resolve()
    folder=root/'inbox/control/runtime/progress'
    with locked(folder/'facts.lock'):
        full=build_projection(root,at=at,snapshot=snapshot)
        evidence=folder/'evidence'/(full['fingerprint']+'.json')
        packet=reporting_packet(full,str(evidence))
        save_json(evidence,full)
        save_json(folder/'facts.json',packet)
    return packet


@contextmanager
def owner_guard(project,role,token_file):
    # 复用现有角色租约和 ACTIVE 门禁，不另造授权或自动接管。
    import role_runtime
    root,binding=role_runtime.binding(project)
    if role not in binding['roles']: raise ValueError('角色未绑定')
    token=read_json(Path(token_file)).get('token')
    with role_runtime.legacy_admission(project), role_runtime.locked(role_runtime.lease_lock(root,role)):
        _,current=role_runtime.binding(project)
        if role not in current['roles']: raise ValueError('角色绑定已改变')
        role_runtime.guard(current,role)
        role_runtime.valid_lease(root,role,token)
        yield


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='action',required=True)
    for name in ('requirement-list','requirement-status','requirement-resolve'):
        p=sub.add_parser(name); p.add_argument('--pool',default=str(Path.home()/'.hermes/role-pool.json'))
        if name=='requirement-resolve': p.add_argument('--role',required=True,choices=('COORDINATOR','A','B','C','TESTER'))
    p=sub.add_parser('load'); p.add_argument('--url',default='http://192.168.2.45:8002/metrics')
    p.add_argument('--cache',default=str(Path.home()/'.hermes/runtime/qwen-load.json')); p.add_argument('--json',action='store_true')
    p=sub.add_parser('request'); p.add_argument('--project',required=True); p.add_argument('--input',required=True)
    p=sub.add_parser('queue'); p.add_argument('--project',required=True)
    p=sub.add_parser('progress-facts'); p.add_argument('--project',required=True)
    p.add_argument('--json',action='store_true',help='输出完整可核验事实；默认只显示定位与指纹')
    p=sub.add_parser('report-input'); p.add_argument('--project',required=True)
    p.add_argument('--role',required=True,choices=('COORDINATOR','A','B','C','TESTER'))
    p=sub.add_parser('acceptance-check'); p.add_argument('--project',required=True); p.add_argument('--input',required=True)
    p=sub.add_parser('live-progress'); p.add_argument('--project',required=True)
    p.add_argument('--role',required=True,choices=('COORDINATOR','A','B','C','TESTER'))
    p=sub.add_parser('role-status'); p.add_argument('--project',required=True)
    p.add_argument('--role',choices=('A','B','C','TESTER')); p.add_argument('--json',action='store_true')
    p=sub.add_parser('context-input'); p.add_argument('--project',required=True); p.add_argument('--role',required=True)
    p=sub.add_parser('summary-input'); p.add_argument('--project',required=True); p.add_argument('--role',required=True)
    p.add_argument('--kind',choices=('phase','morning'),required=True)
    for name in ('task-claim','slot-release','continuation','document-archive'):
        p=sub.add_parser(name); p.add_argument('--project',required=True); p.add_argument('--role',required=True)
        p.add_argument('--input',required=True); p.add_argument('--token-file',required=True)
    for name in ('continuation-ack','continuation-recover-input'):
        p=sub.add_parser(name); p.add_argument('--project',required=True); p.add_argument('--role',required=True)
        p.add_argument('--id',required=True); p.add_argument('--evidence',required=True); p.add_argument('--token-file',required=True)
        p.add_argument('--fingerprint',required=True)
    p=sub.add_parser('claim-notifications'); p.add_argument('--project',required=True); p.add_argument('--role',required=True)
    p.add_argument('--send',action='store_true')
    p=sub.add_parser('queue-ack'); p.add_argument('--project',required=True); p.add_argument('--request-id',required=True)
    p.add_argument('--evidence',required=True); p.add_argument('--token-file',required=True)
    p=sub.add_parser('doctor'); p.add_argument('--cwd',required=True); p.add_argument('--storage-path',action='append',default=[])
    p.add_argument('--need-bytes',type=int,default=0); p.add_argument('--program',action='append',default=[])
    p.add_argument('--docker',action='store_true')
    p=sub.add_parser('start'); p.add_argument('--project',required=True); p.add_argument('--role',required=True)
    p.add_argument('--spec',required=True); p.add_argument('--token-file',required=True)
    p=sub.add_parser('status'); p.add_argument('--state',required=True)
    p=sub.add_parser('role-record'); p.add_argument('--project',required=True); p.add_argument('--role',required=True)
    p.add_argument('--input',required=True); p.add_argument('--token-file',required=True)
    p=sub.add_parser('evidence-record'); p.add_argument('--project',required=True); p.add_argument('--role',required=True)
    p.add_argument('--spec',required=True); p.add_argument('--token-file',required=True)
    p=sub.add_parser('evidence-check'); p.add_argument('--record',required=True)
    p=sub.add_parser('worker'); p.add_argument('--spec',required=True); p.add_argument('--state',required=True); p.add_argument('--sha256',required=True)
    args=parser.parse_args(); exit_code=0
    try:
        if args.action in ('requirement-list','requirement-status','requirement-resolve'):
            from requirement_runtime import list_requirements,status,resolve_role
            if args.action=='requirement-list': value=list_requirements(args.pool)
            elif args.action=='requirement-status': value=status(args.pool)
            else: value=resolve_role(args.pool,args.role)
        elif args.action=='load':
            value=collect(args.url,Path(args.cache))
            if not args.json: print(render(value)); return 0
        elif args.action=='request':
            request=read_json(Path(args.input))
            import role_runtime
            root,binding=role_runtime.binding(args.project)
            if request.get('chat_id')!=binding['roles']['COORDINATOR']['chat_id']:
                raise ValueError('请求必须来自当前绑定协调群的原生消息')
            value=enqueue(root,**{k:request.get(k) for k in ('message_id','chat_id','user_id','text','context')})
        elif args.action=='queue': value=pending_requests(args.project)
        elif args.action=='role-status':
            from role_status import role_status, collect_role_statuses, render_status_line
            value=role_status(args.project,args.role) if args.role else collect_role_statuses(args.project)
            if not args.json:
                print(render_status_line({args.role:value} if args.role else value)); return 0
        elif args.action=='context-input':
            from context_input import build_context
            value=build_context(args.project,args.role)
        elif args.action=='summary-input':
            from report_summary import build_summary_input
            value=build_summary_input(args.project,args.role,args.kind)
        elif args.action=='task-claim':
            from task_claim import claim_task, drain_claim_notifications
            value=claim_task(args.project,args.role,read_json(Path(args.input)),args.token_file)
            value['delivery']=drain_claim_notifications(args.project,args.role,send_claim_notice)
        elif args.action=='slot-release':
            from task_claim import release_slot
            value=release_slot(args.project,args.role,read_json(Path(args.input)),args.token_file)
        elif args.action=='claim-notifications':
            from task_claim import drain_claim_notifications, pending_claim_notifications
            value=drain_claim_notifications(args.project,args.role,send_claim_notice) if args.send else pending_claim_notifications(args.project,args.role,include_suppressed=True)
        elif args.action=='continuation':
            from context_input import publish_continuation
            value=publish_continuation(args.project,args.role,read_json(Path(args.input)),args.token_file)
        elif args.action=='continuation-ack':
            from context_input import acknowledge_continuation
            value=acknowledge_continuation(args.project,args.role,args.id,args.evidence,args.token_file,expected_sha256=args.fingerprint)
        elif args.action=='continuation-recover-input':
            from context_input import recover_continuation_input
            value=recover_continuation_input(args.project,args.role,args.id,args.evidence,args.token_file,expected_sha256=args.fingerprint)
        elif args.action=='document-archive':
            from document_maintenance import archive_role_document
            value=archive_role_document(args.project,args.role,read_json(Path(args.input)),args.token_file)
        elif args.action=='live-progress':
            from progress_query import live_progress_report
            value=live_progress_report(args.project,args.role)
        elif args.action=='report-input':
            from report_contract import build_report, render_report
            packet=refresh_progress_facts(args.project)
            value=build_report(packet,args.role)
            value['markdown']=render_report(value)
        elif args.action=='acceptance-check':
            from acceptance_check import check_acceptance
            value=check_acceptance(args.project,read_json(Path(args.input)))
            exit_code=0 if value['state']=='TEST_GATE_CLEAR' else 1
        elif args.action=='progress-facts':
            root=Path(args.project).resolve()
            destination=root/'inbox/control/runtime/progress/facts.json'
            packet=refresh_progress_facts(root)
            value=packet if args.json else {'kind':packet['kind'],'project':str(root),
                  'observed_at':packet['observed_at'],'fingerprint':packet['fingerprint'],
                  'facts':str(destination),'current_tasks':packet['overall']['current_tasks']['value']}
        elif args.action=='queue-ack':
            with owner_guard(args.project,'COORDINATOR',args.token_file):
                value=acknowledge_request(args.project,args.request_id,args.evidence)
        elif args.action=='doctor':
            value=preflight(args.cwd,args.need_bytes,args.program,args.storage_path,args.docker); exit_code=0 if value['ok'] else 1
        elif args.action=='role-record':
            value=record_role(args.project,args.role,read_json(Path(args.input)),args.token_file)
        elif args.action=='evidence-check':
            value=check_evidence(args.record); exit_code=0 if value['mechanical_match'] else 1
        elif args.action in ('start','evidence-record'):
            plan=read_json(Path(args.spec))
            if args.action=='evidence-record' or 'evidence' in plan:
                validate_evidence(plan.get('evidence'))
            with owner_guard(args.project,args.role,args.token_file):
                value=start_job(args.project,args.role,plan)
            exit_code=1 if value['status'] in ('FAILED','UNKNOWN') else 0
        elif args.action=='status': value=inspect_state(args.state)
        else:
            plan=read_json(Path(args.spec))
            encoded=json.dumps(plan,ensure_ascii=False,sort_keys=True).encode()
            if hashlib.sha256(encoded).hexdigest()!=args.sha256:
                _submission_error(Path(args.state),'FAILED','冻结操作输入发生变化',1)
                raise ValueError('冻结操作输入发生变化')
            value=execute_with_evidence(plan,args.state) if 'evidence' in plan else execute_plan(plan,args.state)
            exit_code=value['exit_code'] or 0
    except Exception as exc:
        if args.action=='worker':
            # Includes malformed frozen JSON and evidence pre-snapshot failures. A failed
            # worker must not strand the submitted operation as PENDING forever.
            # The existing submission receipt never overwrites a live/terminal worker.
            try:
                _submission_error(Path(args.state),'FAILED',type(exc).__name__+': worker 输入或取证失败',1)
            except Exception:
                pass
        value={'status':'ERROR','error':f'{type(exc).__name__}: {exc}'}; exit_code=1
    print(json.dumps(value,ensure_ascii=False)); return exit_code


if __name__=='__main__': raise SystemExit(main())
