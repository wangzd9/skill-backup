"""固定工程操作：检查实际文件系统、失败即停、持久退出结果；不裁决业务门禁。"""
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
from runtime_common import locked, now, process_identity, read_json, save_json


def preflight(cwd, need_bytes=0, programs=(), paths=(), docker=False):
    failures=[]; checked=[]
    paths=list(paths)
    if need_bytes<0: raise ValueError('空间需求不能为负数')
    if docker:
        try:
            result=subprocess.run(['docker','info','--format','{{json .DockerRootDir}}'],capture_output=True,text=True,timeout=10,check=True)
            paths.append(json.loads(result.stdout))
        except (OSError,ValueError,subprocess.SubprocessError) as exc:
            failures.append('无法核实 Docker 实际存储根：'+type(exc).__name__)
    for path in dict.fromkeys(Path(item).resolve() for item in [cwd,*paths]):
        if not path.is_dir(): failures.append(f'目录不存在：{path}'); continue
        usage=shutil.disk_usage(path)
        checked.append({'path':str(path),'device':path.stat().st_dev,'free_bytes':usage.free})
        if usage.free<need_bytes: failures.append(f'目标文件系统空间不足：{path}')
    missing=[name for name in programs if shutil.which(name) is None]
    failures.extend('缺少程序：'+name for name in missing)
    return {'checked_at':now(),'ok':not failures,'filesystems':checked,'failures':failures}


def validate(plan):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,100}',plan.get('operation_id','')):
        raise ValueError('需要稳定 operation_id；重新执行须有明确新 attempt/ID')
    if not Path(plan['cwd']).is_dir(): raise ValueError('工作目录不存在')
    if not isinstance(plan.get('steps'),list) or not plan['steps']:
        raise ValueError('缺少操作步骤')
    timeout = plan.get('timeout_seconds')
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 86400):
        raise ValueError('timeout_seconds 必须为0到86400之间的秒数')
    environment = plan.get('environment', {})
    if not isinstance(environment, dict) or any(not isinstance(key, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key)
            or not isinstance(value, str) or '\0' in value for key, value in environment.items()):
        raise ValueError('environment 必须为有效变量名和字符串值')
    for step in plan['steps']:
        if set(step)-{'argv','stdin_gzip'}: raise ValueError('不支持的步骤字段')
        argv=step.get('argv')
        if not isinstance(argv,list) or not argv or not all(isinstance(x,str) and x and '\0' not in x for x in argv):
            raise ValueError('命令必须为完整 argv 列表，不拼接 shell 命令')
        if step.get('stdin_gzip') and not Path(step['stdin_gzip']).is_file():
            raise ValueError('gzip 输入不存在')


def _step(step,cwd,log,*,timeout=None,environment=None,process_hook=None):
    process=subprocess.Popen(step['argv'],cwd=cwd,stdin=subprocess.PIPE if step.get('stdin_gzip') else subprocess.DEVNULL,
                             stdout=log,stderr=subprocess.STDOUT,start_new_session=True,
                             env={**os.environ, **(environment or {})})
    try:
        if process_hook is not None:
            process_hook(process)
        if step.get('stdin_gzip'):
            try:
                with gzip.open(step['stdin_gzip'],'rb') as stream:
                    shutil.copyfileobj(stream,process.stdin,1024*1024)
                process.stdin.close()
            except (OSError,EOFError) as exc:
                # 同时核对解压和下游返回值，禁止 pipe 最后一项成功掩盖输入损坏。
                try: process.stdin.close()
                except OSError: pass
                try: code=process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid,signal.SIGTERM)
                    try: code=process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid,signal.SIGKILL); code=process.wait()
                log.write((f'\n输入解压/传输失败：{type(exc).__name__}\n').encode()); log.flush()
                return code or 1
        return process.wait(timeout=timeout)
    finally:
        if process.poll() is None:
            # 仅本脚本刚创建的子进程组；不触碰其他任务或未知进程。
            os.killpg(process.pid,signal.SIGTERM)
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid,signal.SIGKILL); process.wait()


def execute_plan(plan,state_path,*,process_hook=None):
    validate(plan); state_path=Path(state_path)
    state_path.parent.mkdir(parents=True,exist_ok=True)
    result={'operation_id':plan['operation_id'],'task':plan.get('task'),'attempt':plan.get('attempt'),
            'role':plan.get('role'),'status':'RUNNING','started_at':now(),**process_identity(os.getpid()),
            'log':str(state_path.with_suffix('.log')),'steps':[]}
    # 同一状态路径的 worker 不能并行执行，第二个 worker 等待后也拒绝重做。
    with locked(str(state_path)+'.worker.lock'):
        existing=read_json(state_path,{}) or {}
        if existing.get('status') not in (None,'PENDING'):
            raise ValueError('该操作已有执行记录，禁止隐式重试')
        save_json(state_path,result)
        try:
            checks=preflight(plan['cwd'],plan.get('need_bytes',0),
                             [s['argv'][0] for s in plan['steps']],plan.get('storage_paths',[]),plan.get('check_docker',False))
            result['preflight']=checks
            if not checks['ok']: raise ValueError('环境检查失败：'+'；'.join(checks['failures']))
            with state_path.with_suffix('.log').open('ab',buffering=0) as log:
                code=0
                for number,step in enumerate(plan['steps'],1):
                    result.update(current_step=number,updated_at=now()); save_json(state_path,result)
                    code=_step(step,plan['cwd'],log,timeout=plan.get('timeout_seconds'),environment=plan.get('environment'),process_hook=process_hook)
                    result['steps'].append({'step':number,'exit_code':code,'ended_at':now()})
                    if code: break
            result.update(status='SUCCEEDED' if code==0 else 'FAILED',exit_code=code)
        except subprocess.TimeoutExpired:
            result.update(status='TIMED_OUT',exit_code=124,error='COMMAND_TIMEOUT',timeout_seconds=plan['timeout_seconds'])
        except KeyboardInterrupt:
            result.update(status='CANCELLED',exit_code=130,error='COMMAND_CANCELLED')
        except Exception as exc:
            result.update(status='FAILED',exit_code=1,error=f'{type(exc).__name__}: {exc}')
        result.update(ended_at=now()); save_json(state_path,result)
    return result


def inspect_state(path):
    result=read_json(path)
    if not isinstance(result,dict): raise ValueError('操作状态不存在或损坏')
    if result.get('status')=='PENDING':
        submission=read_json(Path(path).with_suffix('.submission.json'),{})
        if submission:
            return {**result,**submission}
        if result.get('unit'):
            try:
                check=subprocess.run(['systemctl','--user','show',result['unit'],
                                      '--property=ActiveState','--property=LoadState'],
                                     capture_output=True,text=True,timeout=3)
                fields=dict(line.split('=',1) for line in check.stdout.splitlines() if '=' in line)
                if check.returncode or fields.get('LoadState')=='not-found' or fields.get('ActiveState') in ('inactive','failed'):
                    return {**result,'status':'UNKNOWN','reason':'尚无 worker 结果且服务未运行；需对账，不自动重跑'}
            except (OSError,subprocess.TimeoutExpired):
                return {**result,'status':'UNKNOWN','reason':'无法核实 systemd 提交状态；需对账'}
    if result.get('status')=='RUNNING':
        actual=process_identity(result.get('pid'))
        if not actual or any(actual.get(k)!=result.get(k) for k in ('process_start','boot_id')):
            return {**result,'status':'UNKNOWN','reason':'原 worker 不在；不推断成功、不自动重跑'}
    return result


def operation_states(project,role):
    root=Path(project)/'inbox/control/runtime/operations'/role
    return [dict(inspect_state(p),source=str(p)) for p in sorted(root.glob('*/result.json'))]


def start_job(project,role,plan):
    if os.environ.get('HERMES_ROLE_POOL_DRAIN_ONLY')=='1':
        raise ValueError('需求切换收口回合不能提交新长操作')
    validate(plan)
    from requirement_scope import require_task_scope
    require_task_scope(project,plan.get('task'))
    if role not in ('COORDINATOR','A','B','C','TESTER'): raise ValueError('无效角色')
    base=Path(project).resolve()/'inbox/control/runtime/operations'/role/plan['operation_id']
    frozen={**plan,'role':role}; encoded=json.dumps(frozen,ensure_ascii=False,sort_keys=True).encode()
    sha=hashlib.sha256(encoded).hexdigest()
    with locked(base/'start.lock'):
        spec=base/'spec.json'; state=base/'result.json'
        if spec.exists():
            if read_json(spec)!=frozen: raise ValueError('同一操作 ID 的输入改变，禁止覆盖')
            return {**inspect_state(state),'started':False}
        from requirement_scope import task_scope,task_allowed
        scope=task_scope(project)
        busy=[r for r in operation_states(project,role) if r['status'] in ('PENDING','RUNNING','UNKNOWN')
              and task_allowed(scope,r.get('task'))]
        if busy:
            raise ValueError('本角色仍有在途或待对账工程操作，禁止启动第二项：'+busy[0]['operation_id'])
        save_json(spec,frozen)
        unit='role-operation-'+hashlib.sha256(str(base).encode()).hexdigest()[:24]
        save_json(state,{'operation_id':plan['operation_id'],'status':'PENDING','role':role,
                         'task':plan.get('task'),'attempt':plan.get('attempt'),'unit':unit,'requested_at':now()})
        command=['systemd-run','--user','--collect','--unit='+unit,'--property=KillMode=control-group',
                 sys.executable,str(Path(__file__).with_name('runtime_tools.py')),'worker',
                 '--spec',str(spec),'--state',str(state),'--sha256',sha]
        try:
            completed=subprocess.run(command,capture_output=True,text=True,timeout=15)
        except (OSError,subprocess.TimeoutExpired) as exc:
            # 超时可能已经被 systemd 接受，保留 UNKNOWN 供对账，不自动再启动。
            return _submission_error(state,'UNKNOWN',str(exc))
        if completed.returncode:
            return _submission_error(state,'FAILED',completed.stderr[-1500:],completed.returncode)
        return {**read_json(state),'started':True,'state_path':str(state)}


def _submission_error(state,status,error,exit_code=None):
    # worker 可能已先写终态；只在未启动时改提交状态，不覆盖真实运行结果。
    submission={'status':status,'error':error}
    if exit_code is not None: submission['exit_code']=exit_code
    # 独立提交回执，不等长 worker 锁，也不覆盖它刚写出的终态。
    save_json(Path(state).with_suffix('.submission.json'),submission)
    return inspect_state(state)
