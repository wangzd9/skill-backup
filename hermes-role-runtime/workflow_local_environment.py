"""固定的本地测试环境入口；不接收外部数据库地址或 Docker 参数。"""
import hashlib
import subprocess


def contract():
    return {'mysql': {'profile': 'mysql', 'version': '8.4',
        'entry': 'role_test_command(action="verify", payload={candidate_id, argv, profile:"mysql"})',
        'integration_entry': 'verify_integration 的 profile="mysql"',
        'environment': ['DATABASE_URL', 'ROLE_TEST_MYSQL_URL', 'MN01_MYSQL_URL'],
        'lifecycle': '宿主机为每次验证创建独占库，通过 Unix socket 提供给隔离测试，结束清理自身容器。',
        'permission': '已接纳的本地测试直接使用；不要求用户重复选择环境，不向执行者开放 Docker。',
        'coverage': '环境就绪不等于业务验收通过；测试缺失、跳过及失败分别保留。'},
        'diagnosis': '执行者沙箱无 Docker 接口是预期隔离；不能据此判断本机缺少 MySQL。需要真实数据库时交原生 TESTER 固定候选验证。'}


def mysql_environment(profile, url):
    if profile == 'mysql_migration':
        return {'MN01_MYSQL_URL': url}
    if profile in ('candidate_mysql', 'integration_mysql'):
        return {key: url for key in ('DATABASE_URL', 'ROLE_TEST_MYSQL_URL', 'MN01_MYSQL_URL')}
    raise ValueError('FIXED_MYSQL_PROFILE_REQUIRED')


def valid_mysql_environment(profile, environment):
    if not isinstance(environment, dict) or not environment:
        return False
    url = environment.get('MN01_MYSQL_URL')
    if not isinstance(url, str) or not url or '\0' in url:
        return False
    # 在途旧集成回合仍可能携带旧单变量，保留其原合同。
    if profile=='integration_mysql' and environment=={'MN01_MYSQL_URL':url}:
        return True
    try:
        return environment == mysql_environment(profile, url)
    except ValueError:
        return False


def resource_clearance(ledger,folder):
    """Only observe the named native resource; never remove or replay it."""
    import subprocess
    from runtime_common import read_json,save_json
    from business_ledger import _process
    manifest=read_json(folder/'request.json',{}) or {}
    phase=read_json(folder/'execution-phase.json',{}) or {}
    command_id=phase.get('command_id');candidate=manifest.get('candidate') or {}
    if not command_id or hashlib.sha256(command_id.encode()).hexdigest()[:32]!=folder.name:
        raise ValueError('EXCEPTION_NATIVE_RESOURCE_IDENTITY_REQUIRED')
    with ledger._connection() as db:
        tools=list(db.execute('SELECT * FROM execution_tools WHERE tool_id LIKE ?',('operation-test-'+folder.name+'-%',)))
    for tool in tools:
        actual=_process(tool['pid'])
        if actual and (actual.get('state')=='unknown' or actual.get('process_start')==tool['process_start'] and actual.get('state')!='Z'):
            raise ValueError('EXCEPTION_RESOURCE_EXECUTION_NOT_CLEARED')
    directory=ledger.path.parent/'mysql-sessions'/hashlib.sha256((candidate['work_id']+command_id).encode()).hexdigest()[:16]
    name='role-mysql-'+hashlib.sha256(str(directory.resolve()).encode()).hexdigest()[:24]
    try:
        result=subprocess.run(['docker','ps','--all','--filter','name='+name,'--format','{{.ID}}'],
            capture_output=True,text=True,timeout=10)
    except (OSError,subprocess.SubprocessError):raise ValueError('EXCEPTION_RESOURCE_OBSERVATION_UNAVAILABLE')
    if result.returncode!=0:raise ValueError('EXCEPTION_RESOURCE_OBSERVATION_UNAVAILABLE')
    if result.stdout.strip():raise ValueError('EXCEPTION_RESOURCE_STILL_PRESENT')
    value={'state':'CLEARED','name':name,'container_absent':True,'registered_tool_processes_cleared':True,
        'business_result_unchanged':True,'observed_at':ledger.clock()}
    path=folder/'resource-reconciliation.json';save_json(path,value)
    return {'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),**value}


def verify_candidate(ledger, identity, payload, *, command_id):
    from delivery_evidence import execute_candidate_tests
    kwargs = dict(command_id=command_id, timeout_seconds=payload.get('timeout_seconds', 300),
        supersedes=payload.get('supersedes'), evidence=payload.get('evidence'),
        report_format=payload.get('report_format', 'pytest'))
    profile = payload.get('profile', 'local')
    if profile == 'local':
        return execute_candidate_tests(ledger, identity, payload['candidate_id'], payload['argv'], **kwargs)
    if profile != 'mysql':
        raise ValueError('FIXED_LOCAL_TEST_PROFILE_REQUIRED')
    return execute_mysql(ledger,identity,payload['candidate_id'],payload['argv'],
        verification_profile='candidate_mysql',**kwargs)


def execute_mysql(ledger, identity, candidate_id, argv, *, command_id,
                  verification_profile, timeout_seconds=300, supersedes=None,
                  evidence=None, report_format='pytest'):
    """三个既有验证流程共用数据库生命周期，不另造测试执行器。"""
    import subprocess
    from delivery_evidence import execute_candidate_tests, preflight_test_request,freeze_test_request,_hash
    kinds={'candidate_mysql':'verification','integration_mysql':'integration_verification',
           'mysql_migration':'supplemental_verification'}
    if verification_profile not in kinds:
        raise ValueError('FIXED_MYSQL_PROFILE_REQUIRED')
    kwargs=dict(command_id=command_id,timeout_seconds=timeout_seconds,supersedes=supersedes,
                evidence=evidence,report_format=report_format)
    invalid = preflight_test_request(argv, timeout_seconds, evidence, report_format)
    if invalid:
        return invalid
    if not isinstance(command_id, str) or not command_id or len(command_id)>240:
        raise ValueError('command_id 必须为稳定非空身份')
    directory = ledger.path.parent/'mysql-sessions'/hashlib.sha256((identity['work_id']+command_id).encode()).hexdigest()[:16]
    evidence = kwargs['evidence'] or {}
    kwargs['evidence'] = {**evidence, 'inputs': [*(evidence.get('inputs') or []), str(directory/'ready.json')]}
    folder = ledger.path.parent/'operations'/hashlib.sha256(command_id.encode()).hexdigest()[:32]
    # 已完成请求可以在原 work 结束后读回；原 runner 核完整冻结身份和参数。
    if not (folder/'request.json').exists():
        verified = ledger.validate_identity(identity)
        if not verified.get('valid'):
            return verified
        work = verified['work']
        if (work['role'] != 'TESTER' or work['kind'] != kinds[verification_profile]
                or work['candidate_id'] != candidate_id):
            raise ValueError('INDEPENDENT_TESTER_WORK_REQUIRED')
    from runtime_common import locked,read_json,save_json
    # 同命令并发也只能创建一次数据库；不能覆写已冻结 ready 证据。
    folder.mkdir(parents=True,exist_ok=True)
    requested={'identity':identity,'candidate_id':candidate_id,'argv':argv,
        'timeout_seconds':timeout_seconds,'supersedes':supersedes or [],
        'evidence':kwargs['evidence'],'report_format':report_format,'verification_profile':verification_profile}
    with locked(folder/'local-environment.lock'):
        if (folder/'request.json').exists():
            previous=read_json(folder/'request.json',{}) or {}
            if previous.get('input_sha256')!=_hash(requested):
                raise ValueError('TEST_COMMAND_ID_CONFLICT')
            failure=read_json(folder/'environment-failure.json',{}) or {}
            if failure:
                from workflow_verification_exceptions import record
                return {**failure,'exception_handoff':record(ledger,folder),'replayed':True}
            if not (folder/'result.evidence.json').exists():
                result={'status':'UNVERIFIED', 'code':'TEST_EXECUTION_RECOVERY_REQUIRED',
                    'state_path':str(folder/'result.json'), 'business_pass':False,
                    'responsible_role':'COORDINATOR','next_action':'原请求已冻结，先核原进程和数据库会话；不得重建数据库或重跑。'}
                manifest=read_json(folder/'request.json',{}) or {}
                if manifest.get('candidate') and (read_json(folder/'execution-phase.json',{}) or {}).get('command_id'):
                    result.update(candidate=manifest['candidate'],phase='TEST_EXECUTION',profile=verification_profile)
                    save_json(folder/'environment-failure.json',result)
                    from workflow_verification_exceptions import record
                    result['exception_handoff']=record(ledger,folder)
                return result
            return execute_candidate_tests(ledger, identity, candidate_id, argv,
                verification_profile=verification_profile, **kwargs)
        if verification_profile=='candidate_mysql':
            candidates = ledger.snapshot(verified['work']['run_id'])['candidates']
            if not any(c['candidate_id']==candidate_id and c['state']=='CURRENT' for c in candidates):
                raise ValueError('CURRENT_CANDIDATE_REQUIRED')
        from workflow_supplemental import mysql_session
        frozen=freeze_test_request(ledger,identity,candidate_id,requested,folder,verification_profile)
        if 'candidate' not in frozen:return frozen
        phase='ENVIRONMENT_PREPARING';result=None
        save_json(folder/'execution-phase.json',{'state':phase,'command_id':command_id})
        try:
            with mysql_session(directory) as (url, marker):
                phase='TEST_EXECUTION'
                result=execute_candidate_tests(ledger, identity, candidate_id, argv,
                    verification_profile=verification_profile, environment_overrides=mysql_environment(verification_profile, url), **kwargs)
                phase='ENVIRONMENT_CLEANUP'
            return result
        except (OSError,ValueError,subprocess.SubprocessError) as exc:
            code=str(exc).split(':')[0] if isinstance(exc,ValueError) else type(exc).__name__
            failure={'status':'UNVERIFIED','code':code,'phase':phase,'candidate':frozen['candidate'],
                'profile':verification_profile,'test_started':phase!='ENVIRONMENT_PREPARING',
                'business_pass':False,'business_result_unchanged':True,'publication':(result or {}).get('publication'),
                'session_directory':str(directory),
                'next_action':'协调者核原始环境/执行/清理证据；未知资源先对账，不因更换命令号重建数据库或重跑。'}
            save_json(folder/'environment-failure.json',failure)
            save_json(folder/'execution-phase.json',{'state':phase+'_UNVERIFIED','command_id':command_id})
            from workflow_verification_exceptions import record
            failure['exception_handoff']=record(ledger,folder)
            return failure
