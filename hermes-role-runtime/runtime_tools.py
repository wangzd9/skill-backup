#!/usr/bin/env python3
"""角色确定性工具入口；可从 Skill scripts 的软链接运行。"""
import argparse
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent))
from coord_queue import pending_requests, acknowledge_request, enqueue
from load_metrics import collect, render
from operation_jobs import execute_plan, start_job, inspect_state, preflight, operation_states, _submission_error
from runtime_common import read_json, save_json, locked
from role_records import record_role
from delivery_evidence import check_evidence, execute_with_evidence, validate_evidence


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
    script_dir=Path.home()/'.codex/skills/complex-task-coordinator/scripts'
    sys.path.insert(0,str(script_dir))
    import role_runtime
    root,binding=role_runtime.binding(project)
    if role not in binding['roles']: raise ValueError('角色未绑定')
    token=read_json(Path(token_file)).get('token')
    with role_runtime.locked(role_runtime.lease_lock(root,role)):
        _,current=role_runtime.binding(project)
        if role not in current['roles']: raise ValueError('角色绑定已改变')
        role_runtime.guard(current,role)
        role_runtime.valid_lease(root,role,token)
        yield


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='action',required=True)
    p=sub.add_parser('load'); p.add_argument('--url',default='http://192.168.2.45:8002/metrics')
    p.add_argument('--cache',default=str(Path.home()/'.hermes/runtime/qwen-load.json')); p.add_argument('--json',action='store_true')
    p=sub.add_parser('request'); p.add_argument('--project',required=True); p.add_argument('--input',required=True)
    p=sub.add_parser('queue'); p.add_argument('--project',required=True)
    p=sub.add_parser('progress-facts'); p.add_argument('--project',required=True)
    p.add_argument('--json',action='store_true',help='输出完整可核验事实；默认只显示定位与指纹')
    p=sub.add_parser('report-input'); p.add_argument('--project',required=True)
    p.add_argument('--role',required=True,choices=('COORDINATOR','A','B','C','TESTER'))
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
        if args.action=='load':
            value=collect(args.url,Path(args.cache))
            if not args.json: print(render(value)); return 0
        elif args.action=='request':
            request=read_json(Path(args.input))
            sys.path.insert(0,str(Path.home()/'.codex/skills/complex-task-coordinator/scripts'))
            import role_runtime
            root,binding=role_runtime.binding(args.project)
            if request.get('chat_id')!=binding['roles']['COORDINATOR']['chat_id']:
                raise ValueError('请求必须来自当前绑定协调群的原生消息')
            value=enqueue(root,**{k:request.get(k) for k in ('message_id','chat_id','user_id','text','context')})
        elif args.action=='queue': value=pending_requests(args.project)
        elif args.action=='report-input':
            from report_contract import build_report, render_report
            packet=refresh_progress_facts(args.project)
            value=build_report(packet,args.role)
            value['markdown']=render_report(value)
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
