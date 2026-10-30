"""Real files/imports/ledger identities at the role-pool scheduler boundary."""
import json
import os
from pathlib import Path
import sqlite3

import pytest

from cron import role_pool_scheduler as pools


RUNTIME = '''
import json
from pathlib import Path
def reconcile(pool_path, scheduler_adapter=None, apply=False):
    assert apply is True
    assert scheduler_adapter is not None
    with scheduler_adapter.switch_guard([], 'test'):
        x=json.loads(Path(pool_path).read_text())
        return x.get('reconcile', {'state':'NO_PENDING_REQUEST'})
def resolve_role(pool_path, role):
    x=json.loads(Path(pool_path).read_text())
    return dict(x['scope'], role=role, job_id=x['roles'][role]['poll_job_id'])
def status(pool_path):
    return {'pending':json.loads(Path(pool_path).read_text()).get('pending',[])}
'''


def setup_home(tmp_path, name='home'):
    home = tmp_path/name
    runtime = home/'plugins/role-workflow-runtime'
    runtime.mkdir(parents=True)
    (runtime/'requirement_runtime.py').write_text(RUNTIME)
    root = home/'business'
    root.mkdir()
    pool_path = home/'role-pool.json'
    pool_path.write_text(json.dumps({'roles': {'A': {'poll_job_id': 'job-a'},
                                                     'COORDINATOR': {'poll_job_id': 'job-c'}},
        'scope': {'state': 'ACTIVE', 'generation': 0, 'requirement_id': name,
                  'project_root': str(root)}}))
    cron = home/'cron'
    cron.mkdir()
    (cron/'jobs.json').write_text(json.dumps({'jobs': [{'id':'job-a'}, {'id':'job-c'}, {'id':'report-a'}]}))
    with sqlite3.connect(cron/'executions.db') as db:
        db.execute('CREATE TABLE executions (id TEXT, job_id TEXT, pid INTEGER, process_started_at INTEGER, status TEXT)')
    return home, pool_path, root


def execution(home, identifier, job='job-a', state='running', pid=None, start=None):
    pid = os.getpid() if pid is None else pid
    if start is None and pid == os.getpid():
        start = Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19]
    with sqlite3.connect(home/'cron/executions.db') as db:
        db.execute('INSERT INTO executions VALUES (?,?,?,?,?)', (identifier, job, pid, start, state))


def test_freeze_real_import_and_restore_env_across_two_homes(tmp_path, monkeypatch):
    homes = [setup_home(tmp_path, name) for name in ('alpha','beta')]
    monkeypatch.setenv('HERMES_ROLE_PROJECT_ROOT', 'prior')
    for index in (0,1,0):
        home, pool, root = homes[index]
        identifier = ('a' if index == 0 else 'b')*32
        if not (home/'runtime/role-pool-executions'/(identifier+'.json')).exists():
            execution(home, identifier)
        monkeypatch.setenv('_HERMES_CRON_EXTERNAL_WORKER', identifier)
        job = {'id':'job-a','workdir':'obsolete','prompt':'pool job',
               'role_pool':{'path':str(pool),'role':'A'}}
        session = 'cron_job-a_'+str(index)
        with pools.prepare_role_pool_run(job, execution_id=identifier, session_id=session, home=home) as current:
            assert current['workdir'] == str(root)
            assert os.environ['HERMES_ROLE_POOL_GENERATION'] == '0'
            assert os.environ['HERMES_ROLE_REQUIREMENT_ID'] == home.name
            assert pools.scope_for_session(session, home)['project_root'] == str(root)
            assert job['workdir'] == 'obsolete'
        assert os.environ['HERMES_ROLE_PROJECT_ROOT'] == 'prior'


def test_unadopted_or_foreign_worker_never_falls_back_to_old_root(tmp_path, monkeypatch):
    home, pool, _ = setup_home(tmp_path)
    execution(home, 'a'*32, pid=99999999, start=1)
    monkeypatch.setenv('_HERMES_CRON_EXTERNAL_WORKER', 'a'*32)
    job = {'id':'job-a', 'role_pool':{'path':str(pool), 'role':'A'}}
    with pytest.raises(pools.RolePoolUnavailable, match='IDENTITY_UNVERIFIED'):
        with pools.prepare_role_pool_run(job, execution_id='a'*32, session_id='cron-a',home=home):
            pytest.fail('must not run')
    monkeypatch.delenv('_HERMES_CRON_EXTERNAL_WORKER')
    with pytest.raises(pools.RolePoolUnavailable, match='REQUIRES_EXTERNAL'):
        with pools.prepare_role_pool_run(job,execution_id='a'*32,session_id='cron-a',home=home):
            pytest.fail('must not run')


def test_idle_and_pending_pool_skip_new_business_but_report_is_readonly(tmp_path, monkeypatch):
    home, pool, _ = setup_home(tmp_path)
    execution(home, 'a'*32)
    monkeypatch.setenv('_HERMES_CRON_EXTERNAL_WORKER', 'a'*32)
    job = {'id':'job-a', 'role_pool':{'path':str(pool),'role':'A'}}
    value = json.loads(pool.read_text())
    value['reconcile'] = {'state':'WAITING_SAFE_POINT','blockers':[{'code':'CRON_EXECUTION_ACTIVE_OR_UNKNOWN'}]}
    pool.write_text(json.dumps(value))
    with pools.prepare_role_pool_run(job,execution_id='a'*32,session_id='cron-a',home=home) as current:
        assert current is None
    report = {'id':'report-a','role_pool':{'path':str(pool),'role':'A','kind':'report'}}
    execution(home, 'b'*32, job='report-a')
    monkeypatch.setenv('_HERMES_CRON_EXTERNAL_WORKER', 'b'*32)
    monkeypatch.setattr(pools._runtime(home), 'reconcile', lambda *a, **k: pytest.fail('report mutated selection'))
    with pools.prepare_role_pool_run(report,execution_id='b'*32,session_id='cron-r',home=home) as current:
        assert current['_role_pool_scope']['kind'] == 'report'
    rows = pools.CronRolePoolAdapter(pool,home=home).inspect(['job-a','report-a'])['executions']
    assert {row['job_id'] for row in rows} == {'job-a'}
    value['scope']['state'] = 'IDLE'
    pool.write_text(json.dumps(value))
    with pools.prepare_role_pool_run(report,execution_id='b'*32,session_id='cron-r2',home=home) as current:
        assert current is None


def test_recovered_selection_with_newer_pending_never_starts_intermediate_owner(tmp_path, monkeypatch):
    home, pool, _ = setup_home(tmp_path)
    execution(home, 'a'*32)
    monkeypatch.setenv('_HERMES_CRON_EXTERNAL_WORKER', 'a'*32)
    value = json.loads(pool.read_text())
    value.update(reconcile={'state': 'APPLIED'}, pending=[{'request_id': 'newer', 'status': 'ACCEPTED'}])
    pool.write_text(json.dumps(value))
    job = {'id': 'job-a', 'role_pool': {'path': str(pool), 'role': 'A'}}
    with pools.prepare_role_pool_run(job, execution_id='a'*32, session_id='cron-a', home=home) as current:
        assert current is None
    assert not (home/'runtime/role-pool-executions'/('a'*32+'.json')).exists()


def test_scheduler_admission_failure_is_chinese_and_contains_no_untrusted_payload(tmp_path, monkeypatch):
    from cron import scheduler
    home, pool, _ = setup_home(tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.delenv('_HERMES_CRON_EXTERNAL_WORKER', raising=False)
    result = scheduler.run_job({'id': 'job-a', 'prompt': 'private original user message',
        'role_pool': {'path': str(pool), 'role': 'A'}}, execution_id='a'*32)
    assert result[0] is False
    assert '角色池运行范围校验失败' in result[3]
    assert 'private original user message' not in result[1]


def test_legacy_mixed_assignment_restores_existing_scope_without_restricting_reports(tmp_path, monkeypatch):
    home, pool, root = setup_home(tmp_path)
    value = json.loads(pool.read_text())
    value['scope'].update(legacy_assignment=True, requirement_root=str(root))
    pool.write_text(json.dumps(value))
    identifier = 'a'*32
    execution(home, identifier)
    monkeypatch.setenv('_HERMES_CRON_EXTERNAL_WORKER', identifier)
    job = {'id': 'job-a', 'prompt': '后台执行', 'role_pool': {'path': str(pool), 'role': 'A'}}
    with pools.prepare_role_pool_run(job, execution_id=identifier, session_id='legacy-a', home=home) as current:
        assert current['_role_pool_scope']['legacy_assignment'] is True
        assert '原绑定和已授权范围恢复' in current['prompt']
        assert '本包任务编号定位' not in current['prompt']
    identifier = 'b'*32
    execution(home, identifier, job='report-a')
    monkeypatch.setenv('_HERMES_CRON_EXTERNAL_WORKER', identifier)
    report = {'id': 'report-a', 'prompt': '只读总结',
              'role_pool': {'path': str(pool), 'role': 'A', 'kind': 'report'}}
    with pools.prepare_role_pool_run(report, execution_id=identifier, session_id='legacy-r', home=home) as current:
        assert current['prompt'] == report['prompt']


def test_only_known_independent_blockers_allow_coordinator_drain(tmp_path, monkeypatch):
    home, pool, _ = setup_home(tmp_path)
    execution(home, 'c'*32, job='job-c')
    monkeypatch.setenv('_HERMES_CRON_EXTERNAL_WORKER','c'*32)
    job={'id':'job-c','prompt':'base','role_pool':{'path':str(pool),'role':'COORDINATOR'}}
    value=json.loads(pool.read_text())
    value['reconcile']={'state':'WAITING_SAFE_POINT','blockers':[{'code':'OCR_ACTIVE_OR_UNRESOLVED'}]}
    pool.write_text(json.dumps(value))
    with pools.prepare_role_pool_run(job,execution_id='c'*32,session_id='cron-c',home=home) as current:
        assert current['_role_pool_scope']['drain_only'] is True
        assert os.environ['HERMES_ROLE_POOL_DRAIN_ONLY']=='1'
        assert '不得新派单' in current['prompt']
    value['reconcile']['blockers'].append({'code':'UNKNOWN_NEW_BLOCKER'})
    pool.write_text(json.dumps(value))
    with pools.prepare_role_pool_run(job,execution_id='c'*32,session_id='cron-c2',home=home) as current:
        assert current is None


def test_immutable_execution_scope_and_historical_attribution(tmp_path, monkeypatch):
    home,pool,old=setup_home(tmp_path)
    execution(home,'a'*32)
    monkeypatch.setenv('_HERMES_CRON_EXTERNAL_WORKER','a'*32)
    job={'id':'job-a','role_pool':{'path':str(pool),'role':'A'}}
    with pools.prepare_role_pool_run(job,execution_id='a'*32,session_id='cron-old',home=home):
        pass
    value=json.loads(pool.read_text());value['scope'].update(generation=1,requirement_id='new')
    pool.write_text(json.dumps(value))
    assert pools.scope_for_session('cron-old',home)['requirement_id']==home.name
    with pytest.raises(pools.RolePoolUnavailable,match='SCOPE_CONFLICT'):
        with pools.prepare_role_pool_run(job,execution_id='a'*32,session_id='cron-old',home=home):
            pytest.fail('immutable scope cannot be retargeted')


def test_completed_live_and_missing_identity_still_block_switch(tmp_path):
    home,pool,_=setup_home(tmp_path)
    execution(home,'a'*32,state='completed')
    execution(home,'b'*32,state='unknown',pid=99999999,start=1)
    execution(home,'c'*32,state='running',start='UNVERIFIED')
    view=pools.CronRolePoolAdapter(pool,home=home).inspect(['job-a'])
    rows={row['execution_id']:row for row in view['executions']}
    assert rows['a'*32]['state']=='terminal_owner_still_alive'
    assert 'b'*32 not in rows
    assert not rows['c'*32]['safe_checkpoint']


def test_job_normalizer_and_update_preserve_pause_schedule_origin(tmp_path,monkeypatch):
    from cron import jobs
    monkeypatch.setattr(jobs,'JOBS_FILE',tmp_path/'cron/jobs.json')
    monkeypatch.setattr(jobs,'CRON_DIR',tmp_path/'cron')
    pool=tmp_path/'pool.json'
    job=jobs.create_job('固定角色池','5m',paused=True,paused_reason='user pause',origin={'platform':'feishu','chat_id':'test'})
    before={k:job.get(k) for k in ['id','enabled','state','schedule','origin','deliver','paused_at','paused_reason']}
    current=jobs.update_job(job['id'],{'role_pool':{'path':str(pool),'role':'A'}})
    assert {k:current.get(k) for k in before}==before
    assert current['role_pool']['kind']=='business'
    for invalid in [{'path':'relative','role':'A'},{'path':str(pool),'role':'OTHER'}, {'path':str(pool),'role':'A','kind':'unsafe'}]:
        with pytest.raises(ValueError):
            jobs.update_job(job['id'],{'role_pool':invalid})


def test_real_scheduler_script_gets_frozen_cwd_env_before_model(tmp_path,monkeypatch):
    from cron import scheduler
    home,pool,root=setup_home(tmp_path)
    identifier='a'*32
    execution(home,identifier)
    monkeypatch.setenv('_HERMES_CRON_EXTERNAL_WORKER',identifier)
    monkeypatch.setenv('HERMES_HOME',str(home))
    monkeypatch.setattr(pools,'get_hermes_home',lambda:home)
    monkeypatch.setattr(scheduler,'_get_hermes_home',lambda:home)
    scripts=home/'scripts';scripts.mkdir()
    capture=home/'captured.json'
    script=scripts/'probe.py'
    script.write_text('import os,json\nfrom pathlib import Path\n'+
        'Path('+repr(str(capture))+').write_text(json.dumps({"cwd":os.getcwd(),"root":os.environ.get("HERMES_ROLE_PROJECT_ROOT"),"generation":os.environ.get("HERMES_ROLE_POOL_GENERATION")}))\n'+
        'print(json.dumps({"wakeAgent":False}))\n')
    # A scheduler handoff payload captured before deployment must not retain its
    # old static script/prompt. The explicit current store opts this job in.
    stored={'id':'job-a','prompt':'pool prompt','script':str(script),'workdir':str(home),
            'role_pool':{'path':str(pool),'role':'A'}}
    (home/'cron/jobs.json').write_text(json.dumps({'jobs':[stored]}))
    payload={'id':'job-a','prompt':'obsolete root','workdir':str(home)}
    result=scheduler.run_job(payload,execution_id=identifier)
    assert result[0] is True
    assert result[2]==scheduler.SILENT_MARKER
    assert json.loads(capture.read_text())=={'cwd':str(root),'root':str(root),'generation':'0'}
    assert 'role_pool' not in payload
