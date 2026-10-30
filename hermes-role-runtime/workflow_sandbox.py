"""Confine development shell writes to its private, work-bound Git clone.

The existing terminal still owns execution/output. This wrapper adds a mount/PID
namespace and registers its real process scope in the existing execution ledger.
It never admits work, renews a lease or interprets business success.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import uuid
import workflow_diagnostics as diagnostics


def tool_directive(scope, tool_name, args):
    if tool_name in {'write_file', 'patch'}:
        from workflow_file_tools import validate_edit, backend_ready
        if not backend_ready(scope):
            raise ValueError('SCOPED_FILE_HANDLER_NOT_READY')
        validate_edit(scope, tool_name, args)
        return None  # The scoped native handler below owns sandbox execution.
    if tool_name != 'terminal':
        return {'action': 'block', 'message': '本单写入须通过受限 terminal；文件工具可用于只读查看。'}
    if not isinstance(args.get('command'), str) or not args['command'].strip():
        raise ValueError('SHELL_COMMAND_REQUIRED')
    if args.get('pty') or args.get('background'):
        import workflow_runtime as workflow
        error = workflow.rejected(scope, 'SANDBOX_FOREGROUND_REQUIRED', operation='terminal',
                                  capability='workspace_write')
        return {'action': 'block', 'message': '本单仅开放受限前台命令；长操作由现有固定runner执行。',
                'decision': error.decision}
    root = Path(scope.get('worktree', '')).resolve()
    expected = Path(scope['pool_path']).resolve().with_suffix('.runtime') / 'workspaces' / scope['work_id']
    if root != expected or not (root / '.git').is_dir():
        raise ValueError('PRIVATE_WORKSPACE_REQUIRED')
    cwd = Path(args.get('workdir') or root).resolve()
    if not cwd.is_relative_to(root) or not cwd.is_dir():
        raise ValueError('WORKDIR_OUTSIDE_CURRENT_WORK')
    if shutil.which('bwrap') is None:
        raise ValueError('WORKSPACE_SANDBOX_UNAVAILABLE')
    packet = {'scope': scope, 'command': args['command'], 'cwd': str(cwd),
              'diagnostic_operation': diagnostics.current_operation()}
    encoded = base64.urlsafe_b64encode(json.dumps(packet).encode()).decode()
    command = shlex.join([sys.executable, str(Path(__file__).resolve()), '--packet', encoded])
    return {'action': 'modify', 'args': {'command': command, 'workdir': str(root), 'pty': False}}


def run(packet):
    with diagnostics.operation_context(packet['scope'],
            **packet.get('diagnostic_operation', {})):
        try:
            return _run(packet)
        except Exception as exc:
            if not getattr(exc, 'decision', None):
                import workflow_runtime as workflow
                exc.decision = diagnostics.record_result(workflow.ledger_path(packet['scope']['pool_path']),
                    {'status':'REJECTED' if isinstance(exc,ValueError) else 'UNVERIFIED',
                     'code':str(exc) if isinstance(exc,ValueError) else 'SANDBOX_SOURCE_UNAVAILABLE'},
                    operation='terminal', phase='SANDBOX_PREFLIGHT', side_effect_state='NONE')
            raise


def _run(packet):
    import workflow_runtime as workflow
    scope = packet['scope']
    workflow.guard(scope, operation='terminal', capability='workspace_write',
                   phase='SANDBOX_PREFLIGHT', side_effect_state='NONE')
    root = Path(scope['worktree']).resolve()
    cwd = Path(packet['cwd']).resolve()
    expected = workflow.ledger_path(scope['pool_path']).parent / 'workspaces' / scope['work_id']
    if root != expected or not cwd.is_relative_to(root) or not (root / '.git').is_dir():
        raise ValueError('PRIVATE_WORKSPACE_REQUIRED')
    # A dedicated host process group remains observable even after a caller dies.
    # The namespace init kills all namespace children when it exits.
    if os.getsid(0) != os.getpid():
        os.setsid()
    from business_ledger import _process
    actual = _process(os.getpid())
    ledger = workflow.open_ledger(scope['pool_path'], readonly=False)
    tool_id = uuid.uuid4().hex
    registration = ledger.register_tool_execution(workflow.identity_from_scope(scope),
        {'tool_id': tool_id, 'pid': os.getpid(), 'process_start': actual['process_start'],
         'cgroup': actual['cgroup']}, command_id='native-tool:' + tool_id)
    if registration.get('status') != 'COMMITTED':
        raise workflow.GuardRejected(registration)
    run_record = next(row for row in ledger.snapshot()['runs'] if row['run_id'] == scope['run_id'])
    source_objects = Path(subprocess.check_output(
        ['git', '-C', run_record['repo'], 'rev-parse', '--path-format=absolute', '--git-common-dir'],
        text=True).strip()) / 'objects'
    scratch = root / '.git' / 'workflow-tmp'
    scratch.mkdir(exist_ok=True)
    from workflow_delivery import evidence_directory
    evidence_root=evidence_directory(ledger,scope['work_id'],create=True)
    command = [shutil.which('bwrap') or '/usr/bin/bwrap', '--die-with-parent', '--unshare-all',
               '--ro-bind', '/', '/', '--proc', '/proc', '--dev', '/dev',
               '--tmpfs', '/tmp', '--tmpfs', '/run',
               '--ro-bind', str(source_objects), str(source_objects),
               '--bind', str(root), str(root), '--bind',str(evidence_root),str(evidence_root),
               '--remount-ro', '/tmp', '--setenv', 'TMPDIR', str(scratch),
               '--chdir', str(cwd), '--cap-drop', 'ALL']
    if packet.get('file_edit'):
        command += ['--ro-bind', str(root / '.git'), str(root / '.git')]
    command += ['--', '/bin/sh', '-c', packet['command']]
    child = None

    def interrupt(signum, _frame):
        raise InterruptedError('NATIVE_TOOL_INTERRUPTED')

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, interrupt)
    try:
        workflow.guard(scope, operation='terminal', capability='workspace_write',
                       phase='SANDBOX_PREFLIGHT', side_effect_state='NONE')
        child = subprocess.Popen(command, **({'stdin': subprocess.PIPE} if 'stdin_data' in packet else {}))
        if 'stdin_data' in packet:
            # communicate() writes incrementally while draining stdout in the
            # outer runner. Its timeout preserves the same writer recheck loop.
            child_input = packet['stdin_data'].encode()
        while child.poll() is None:
            workflow.guard(scope, operation='terminal', capability='workspace_write',
                           phase='SANDBOX_REAUTHORIZATION', side_effect_state='POSSIBLE')
            try:
                if 'stdin_data' in packet:
                    child.communicate(input=child_input, timeout=0.5)
                    return child.returncode
                return child.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                child_input = None
                continue
        return child.returncode
    except BaseException as exc:
        if not getattr(exc, 'decision', None):
            receipt = diagnostics.record_result(workflow.ledger_path(scope['pool_path']),
                {'status':'UNVERIFIED', 'code':'SANDBOX_EXECUTION_INTERRUPTED'},
                operation='terminal', phase='SANDBOX_EXECUTION',
                side_effect_state='POSSIBLE' if child is not None else 'NONE')
            try:
                exc.decision = receipt
            except (AttributeError, TypeError):
                pass
        raise
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--packet')
    parser.add_argument('--stdin-packet', action='store_true')
    arguments = parser.parse_args()
    try:
        packet = json.load(sys.stdin) if arguments.stdin_packet else json.loads(base64.urlsafe_b64decode(arguments.packet))
        sys.exit(run(packet))
    except Exception as exc:
        print('受限命令停止：' + str(exc), file=sys.stderr)
        if getattr(exc, 'decision', None):
            print('诊断收据：' + json.dumps(exc.decision, ensure_ascii=False), file=sys.stderr)
        sys.exit(125)
