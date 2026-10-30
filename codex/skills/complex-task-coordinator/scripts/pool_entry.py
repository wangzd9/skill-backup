#!/usr/bin/env python3
"""Stable read-only reporting entry for a role pool's frozen scope."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent))


def active_report_binding(pool_path, role):
    from workflow_runtime import mode, open_ledger
    if mode(pool_path)['mode'] != 'ACTIVE':
        return None
    pool = json.loads(Path(pool_path).read_text())
    if role not in pool.get('roles', {}):
        raise ValueError('报告角色不在固定角色池内')
    snapshot = open_ledger(pool_path).snapshot()
    # A previously admitted report retains its frozen run even after selection
    # changes. Report scope is read-only and never borrows that run's writer.
    run_id = os.environ.get('HERMES_ROLE_RUN_ID') or snapshot.get('current_run_id')
    run = next((item for item in snapshot['runs'] if item['run_id'] == run_id), None)
    if not run:
        return {'state': 'UNSELECTED', 'project_root': None, 'generation': snapshot['generation']}
    return {'state': 'ACTIVE', 'project_root': run['requirement_root'], 'run_id': run['run_id'],
            'requirement_id': run['requirement_id'], 'generation': snapshot['generation']}


def report_project(pool_path,role):
    names=('HERMES_ROLE_POOL_PATH','HERMES_ROLE_POOL_GENERATION','HERMES_ROLE_REQUIREMENT_ID',
           'HERMES_ROLE_PROJECT_ROOT','HERMES_ROLE_NAME')
    if any(os.environ.get(key) for key in names):
        if (not all(os.environ.get(key) for key in names)
                or Path(os.environ['HERMES_ROLE_POOL_PATH']).resolve()!=Path(pool_path).resolve()
                or os.environ['HERMES_ROLE_NAME']!=role):
            raise ValueError('报告冻结范围不完整或不匹配')
        # A report admitted before a switch remains a read-only report of its
        # original scope. Never reattribute it to the newly selected demand.
        return os.environ['HERMES_ROLE_PROJECT_ROOT']
    binding = active_report_binding(pool_path, role)
    if binding is not None:
        return binding['project_root']
    from requirement_runtime import resolve_role
    resolved=resolve_role(pool_path,role)
    return resolved['project_root'] if resolved.get('state')=='ACTIVE' else None


@contextmanager
def report_scope(pool_path,role):
    """Fence only fact collection; never hold admission during model generation."""
    home=os.environ.get('HERMES_HOME',str(Path.home()/'.hermes'))
    sys.path.insert(0,str(Path(home)/'hermes-agent'))
    from cron.role_pool_scheduler import admission_guard
    with admission_guard(pool_path):
        project=report_project(pool_path,role)
        current=active_report_binding(pool_path,role)
        workflow_report=current is not None
        if current is None:
            from requirement_runtime import resolve_role
            current=resolve_role(pool_path,role)
        if project is not None and (current.get('project_root')!=project
                or current.get('state') not in ('ACTIVE','PAUSED')
                or not workflow_report and os.environ.get('HERMES_ROLE_POOL_GENERATION') is not None
                and str(current.get('generation'))!=os.environ['HERMES_ROLE_POOL_GENERATION']):
            raise ValueError('需求在报告采集前已切换，本次总结未生成')
        yield project


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('phase','morning','report','idle-report'))
    parser.add_argument('--pool',default=str(Path.home()/'.hermes/role-pool.json'))
    parser.add_argument('--role',required=True,choices=('COORDINATOR','A','B','C','TESTER'))
    parser.add_argument('--hermes-home',default=str(Path.home()/'.hermes'))
    args=parser.parse_args()
    try:
        with report_scope(args.pool,args.role) as project:
            if not project:return 0
            if args.action in ('phase','morning'):
                from report_summary import activity_module,build_summary_input
                if activity_module().quiet():return 0
                value=build_summary_input(project,args.role,args.action,home=Path(args.hermes_home),pool_path=args.pool)
                print(value['prompt']);return 0
            if active_report_binding(args.pool,args.role) is not None:
                from workflow_runtime import role_state, ROLES
                from workflow_reports import render_status_brief
                states=[role_state(args.pool, role) for role in ROLES]
                print(render_status_brief({'role_states':states}))
                return 0
            script=Path(__file__).resolve().with_name('role_observer.py')
            return subprocess.run([sys.executable,str(script),args.action,'--project',project,'--role',args.role]).returncode
    except Exception as exc:
        print(json.dumps({'state':'ERROR','error':type(exc).__name__,'message':str(exc)[:500]},ensure_ascii=False),file=sys.stderr)
        return 1


if __name__=='__main__':raise SystemExit(main())
