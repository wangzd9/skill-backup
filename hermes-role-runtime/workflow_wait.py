"""Read-only wait revalidation. Rechecking never grants business permission."""
import os
import subprocess


def repo_observation(repo):
    try:
        def git(*args):
            return subprocess.check_output(['git', '--no-optional-locks', '-c', 'core.fsmonitor=false',
                '-c', 'core.untrackedCache=false', '-C', str(repo), *args],
                env={k:v for k,v in os.environ.items() if not k.startswith('GIT_')},
                stderr=subprocess.DEVNULL, text=True, timeout=3).strip()
        head = git('rev-parse', 'HEAD')
        branch = git('symbolic-ref', '--short', 'HEAD')
        dirty = bool(git('status', '--porcelain', '--untracked-files=normal'))
        if git('rev-parse', 'HEAD') != head:
            return {'state': 'UNKNOWN'}
        return {'state': 'OBSERVED', 'head': head, 'branch': branch, 'dirty': dirty}
    except (OSError, subprocess.SubprocessError):
        return {'state': 'UNKNOWN'}


def review(ledger, db, work, run):
    from business_ledger import _row
    event = _row(db.execute("SELECT * FROM events WHERE work_id=? AND kind='CHECKPOINT_SAVED' "
        'ORDER BY event_id DESC LIMIT 1', (work['work_id'],)).fetchone())
    # A checkpoint timestamp bounds even old string-only wait contracts. No
    # synthetic events or ACKs are needed to escape an incorrectly saved wait.
    saved_at = event['created_at'] if event else work['created_at']
    deadline = saved_at + 900
    result = {'due': ledger.clock() >= deadline, 'recheck_at': deadline,
              'reason': 'WAIT_RECHECK_DUE', 'gates_unchanged': True}
    previous = (event or {}).get('payload', {}).get('wait_observation')
    if work['role'] == 'COORDINATOR' and previous and not result['due']:
        current = repo_observation(run['repo'])
        # Content changes inside an already dirty tree are not progress. Observe
        # branch/base changes and dirty -> clean without polling file mtimes.
        if (previous.get('state') == current.get('state') == 'OBSERVED'
                and (previous['head'] != current['head'] or previous['branch'] != current['branch']
                     or (previous['dirty'] and not current['dirty']))):
            result.update(due=True, reason='WAIT_REPOSITORY_CHANGED')
        result['repository'] = current
    return result


def capabilities(role):
    developer = role in {'A', 'B', 'C'}
    return {'ordinary_workspace_write': developer, 'ordinary_terminal': developer,
            'coordinator_shell_available': False,
            'supplemental_mysql': '原生归属指令持久排队给TESTER；verify_supplemental提供隔离MySQL8.4与固定测试，无需生产URL、shell或重授；当前状态看supplemental_verifications。',
            'merge_execution': '有效授权spec.repo同仓；共享dirty时受管integration-worktrees隔离合入，回执记录实际位置，operations仅存日志',
            'coordination_actions': ['publish_plan','decide','checkpoint','safe_release','service_submit',
                'service_collect','service_decide','service_resume','service_reject','extend_task_scope',
                'refresh_task_base','refresh_development_base','merge','complete','complete_requirement','append_development_log','commit_documentation','resolve_user_decision','resolve_verification_exception'] if role == 'COORDINATOR' else [],
            'final_acceptance': '原生completion计划自动冻结集成版本并排队TESTER；verify_integration逐AC取证，deliver_integration交付，代码FAIL回原owner新attempt；最终PASS后协调者complete_requirement复核本地收口；无需用户另发派单裁决，规划者仍手动。',
            'documentation_policy': '协调者先用role_repo_state的documentation_review读真实diff与SHA，核对事实和范围；'
                '可确认的普通文档用commit_documentation单独提交，不能因归属未知一律等待用户；代码、指令与AC不走此入口。',
            'local_automation': '已接受需求内派单/开发/测试/返修/基线刷新/安全收口/本地合入自动接续，无需a/b或逐任务授权；OCR按原预算期限，生产/push边界单独核验。',
            'startup_authority': 'business_ledger.admission_hint (ACTIVE); pool.current is legacy metadata',
            'writer_fence_meaning': '该角色已有写执行，禁止并发第二个；不等于死锁',
            'wait_policy': '仅余真实外部条件才保存 waiting_external；15分钟或仓库状态变化后重新核验，门禁不解除'}
