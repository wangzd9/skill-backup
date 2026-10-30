"""Native file UX with the existing writer-bound sandbox, not host write access.

Native handlers retain sensitive/instruction/stale-overwrite guards and tracking.
Only their file backend is scoped to this call; unrelated sessions keep the host
backend. Structured content travels over pipes, never a model shell argument.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import uuid

BACKEND = ContextVar('role_file_backend', default=None)
REGISTERED = set()


def target(scope, raw):
    if not isinstance(raw, str) or not raw.strip() or '\x00' in raw:
        raise ValueError('FILE_PATH_REQUIRED')
    root = Path(scope['worktree']).absolute()
    path = Path(raw).expanduser()
    path = path if path.is_absolute() else root / path
    # Never resolve a symlink away before enforcing its literal path.
    if '..' in path.parts or not path.is_relative_to(root):
        raise ValueError('WRITE_OUTSIDE_CURRENT_WORK')
    relative = path.relative_to(root).as_posix()
    if '.git' in path.parts or relative == '.':
        raise ValueError('PROTECTED_WORKSPACE_METADATA')
    if not any(fnmatch.fnmatchcase(relative, rule) or relative == rule.rstrip('/')
               or relative.startswith(rule.rstrip('/') + '/') for rule in scope.get('write_scope', [])):
        raise ValueError('FILE_OUTSIDE_TASK_SCOPE')
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('SYMLINK_EDIT_NOT_ALLOWED')
        if current.exists() and not (current.is_dir() or current.is_file()):
            raise ValueError('REGULAR_FILE_REQUIRED')
    return str(path)


def validate_edit(scope, name, args):
    if scope.get('role') not in {'A', 'B', 'C'} or scope.get('kind') != 'business' or scope.get('drain_only'):
        raise ValueError('ROLE_CAPABILITY_NOT_ALLOWED')
    import workflow_runtime as workflow
    root = Path(scope.get('worktree', '')).resolve()
    expected = workflow.ledger_path(scope['pool_path']).parent / 'workspaces' / scope['work_id']
    if root != expected or not (root / '.git').is_dir():
        raise ValueError('PRIVATE_WORKSPACE_REQUIRED')
    if name == 'write_file':
        if not isinstance(args.get('content'), str):
            raise ValueError('FILE_CONTENT_REQUIRED')
        return [target(scope, args.get('path'))]
    if name != 'patch':
        raise ValueError('FILE_TOOL_REQUIRED')
    if args.get('mode', 'replace') == 'replace':
        if any(not isinstance(args.get(k), str) for k in ('old_string', 'new_string')):
            raise ValueError('PATCH_STRINGS_REQUIRED')
        return [target(scope, args.get('path'))]
    if args.get('mode') != 'patch' or not isinstance(args.get('patch'), str):
        raise ValueError('PATCH_CONTENT_REQUIRED')
    from tools.patch_parser import parse_v4a_patch
    operations, error = parse_v4a_patch(args['patch'])
    if error or not operations:
        raise ValueError('INVALID_OR_EMPTY_PATCH')
    paths = []
    for operation in operations:
        paths.append(target(scope, operation.file_path))
        if operation.new_path:
            paths.append(target(scope, operation.new_path))
    return paths


def backend_ready(scope):
    return str(Path(scope['pool_path']).resolve()) in REGISTERED


def install(ctx, plugin):
    """Use the host's explicit builtin-override opt-in and scoped registrations."""
    REGISTERED.discard(str(Path(plugin.pool_path).resolve()))
    import tools.file_tools as native
    patch_schema = {**native.PATCH_SCHEMA, 'description': native._PATCH_V4A_DESCRIPTION,
                    'parameters': {'type': 'object', 'properties': {
                        'mode': native._PATCH_V4A_PARAMS['mode'],
                        **native.PATCH_SCHEMA['parameters']['properties'],
                        'patch': native._PATCH_V4A_PARAMS['patch']}}}
    original = native._get_file_ops
    if not getattr(original, '_role_scoped', False):
        def scoped_ops(task_id='default'):
            return BACKEND.get() or original(task_id)
        scoped_ops._role_scoped = True
        native._get_file_ops = scoped_ops
    for name, handler, schema in (
            ('write_file', native._handle_write_file, native.WRITE_FILE_SCHEMA),
            ('patch', native._handle_patch, patch_schema)):
        def scoped_handler(args, _name=name, _handler=handler, **kwargs):
            scope = plugin._frozen_scope(kwargs.get('session_id', ''))
            identity = plugin.api_attribution_details(kwargs.get('session_id', ''))
            if not scope or not scope.get('workflow_version'):
                if identity and __import__('workflow_runtime').active(plugin.pool_path):
                    raise ValueError('ROLE_BUSINESS_SCOPE_REQUIRED')
                return _handler(args, **kwargs)
            import workflow_runtime as workflow
            # Execution-time recheck, even if the caller skipped pre_tool_call.
            workflow.guard(scope, operation=_name, capability='workspace_write')
            paths = validate_edit(scope, _name, args)
            normalized = dict(args)
            if args.get('path'):
                normalized['path'] = target(scope, args['path'])
            if _name == 'patch' and args.get('mode') == 'patch':
                collected = native._collect_v4a_header_paths(args['patch'])
                if isinstance(collected, str):
                    return collected
                mapping = {p: target(scope, p) for p in collected[0]}
                normalized['patch'] = native._rewrite_v4a_patch_paths_for_host(args['patch'], mapping, None)
            token = BACKEND.set(RemoteOperations(scope, paths))
            try:
                return _handler(normalized, **kwargs)
            finally:
                BACKEND.reset(token)
        registration = ctx.register_tool(name, 'file', schema, scoped_handler, override=True,
                                        disclosure='direct')
        if registration is None:
            raise ValueError('SCOPED_FILE_HANDLER_REGISTRATION_FAILED')
    REGISTERED.add(str(Path(plugin.pool_path).resolve()))
    return {'state': 'ACTIVATED', 'protocol': 'sandbox-files/1', 'pid': os.getpid(),
            'pool_path': str(Path(plugin.pool_path).resolve()),
            'source_sha256': {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                for name in ('workflow_file_tools.py', 'workflow_sandbox.py', 'workflow_runtime.py',
                             'workflow_tool_bridge.py', 'workflow_progression.py')}}


class RemoteOperations:
    env = None  # Native guards resolve against real host absolute paths.

    def __init__(self, scope, paths):
        self.scope = scope
        self.paths = paths
        self.cwd = scope['worktree']

    def call(self, method, **arguments):
        import workflow_runtime as workflow
        import workflow_diagnostics as diagnostics
        workflow.guard(self.scope, operation=method, capability='workspace_write')
        script = Path(__file__).resolve()
        import tools
        core = Path(tools.__file__).resolve().parent.parent
        request = {'scope': self.scope, 'method': method, 'arguments': arguments}
        packet = {'scope': self.scope, 'cwd': self.cwd, 'file_edit': True,
                  'command': shlex.join([sys.executable, '-I', '-c',
                      'import sys,runpy;sys.path[:0]=[sys.argv[1],str(__import__("pathlib").Path(sys.argv[2]).parent)];runpy.run_path(sys.argv[2],run_name="__main__")',
                      str(core), str(script)]),
                  'stdin_data': json.dumps(request),
                  'diagnostic_operation': diagnostics.current_operation()}
        result = subprocess.run([sys.executable, str(script.with_name('workflow_sandbox.py')), '--stdin-packet'],
                                input=json.dumps(packet), capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise ValueError('SANDBOX_FILE_OPERATION_FAILED: ' + result.stderr[-1500:])
        return json.loads(result.stdout)

    def write_file(self, path, content, pre_content=None):
        from tools.file_operations_common import WriteResult
        return WriteResult(**self.call('write_file', path=path, content=content, pre_content=pre_content))

    def patch_replace(self, path, old_string, new_string, replace_all=False):
        from tools.file_operations_common import PatchResult
        return PatchResult(**self.call('patch_replace', path=path, old_string=old_string,
                                      new_string=new_string, replace_all=replace_all))

    def patch_v4a(self, patch):
        from tools.file_operations_common import PatchResult
        return PatchResult(**self.call('patch_v4a', patch_content=patch))

    def read_file_raw(self, path):
        from tools.file_operations_common import ReadResult
        return ReadResult(**self.call('read_file_raw', path=path))


def secure_operations(scope):
    """Keep native fuzzy/V4A/encoding/lint behavior; replace filesystem syscalls.

    Directory-fd traversal rejects symlinks at every component. Atomic rename
    replaces a hard link rather than overwriting its external inode. .git is
    additionally mounted read-only by the enclosing sandbox.
    """
    from tools.file_operations import ShellFileOperations
    from tools.file_operations_common import ExecuteResult, ReadResult, WriteResult, _detect_line_ending

    class Operations(ShellFileOperations):
        def __init__(self):
            super().__init__(None, cwd=scope['worktree'])

        @contextmanager
        def parent(self, path, create=False):
            path = Path(target(scope, path))
            root = Path(scope['worktree'])
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                for part in path.relative_to(root).parts[:-1]:
                    if create:
                        try:
                            os.mkdir(part, mode=0o755, dir_fd=fd)
                        except FileExistsError:
                            pass
                    next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    os.close(fd)
                    fd = next_fd
                yield fd, path.name
            finally:
                os.close(fd)

        def read_file_raw(self, path):
            try:
                with self.parent(path) as (fd, name):
                    with os.fdopen(os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd), 'rb') as stream:
                        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                            raise ValueError('REGULAR_FILE_REQUIRED')
                        raw = stream.read()
                return ReadResult(content=raw.decode('utf-8', 'surrogateescape'), file_size=len(raw))
            except FileNotFoundError:
                return ReadResult(error='File not found: ' + str(path))

        def _cat(self, path):
            result = self.read_file_raw(path)
            return ExecuteResult(stdout=result.error or result.content, exit_code=1 if result.error else 0)

        def _probe_write_target(self, path, pre_content, want_pre):
            result = self.read_file_raw(path)
            raw = result.content if not result.error else ''
            return raw.startswith('\ufeff'), pre_content if pre_content is not None else raw, _detect_line_ending(raw)

        def _atomic_write(self, path, content):
            temporary = '.workflow-edit-' + uuid.uuid4().hex
            with self.parent(path, create=True) as (fd, name):
                mode = 0o644
                try:
                    previous = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if not stat.S_ISREG(previous.st_mode):
                        raise ValueError('REGULAR_FILE_REQUIRED')
                    mode = stat.S_IMODE(previous.st_mode) & 0o777
                except FileNotFoundError:
                    pass
                try:
                    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                         0o600, dir_fd=fd)
                    with os.fdopen(descriptor, 'wb') as stream:
                        stream.write(content.encode('utf-8', 'surrogateescape'))
                        stream.flush()
                        os.fchmod(stream.fileno(), mode)
                        os.fsync(stream.fileno())
                    # Same writer and exact scope are rechecked by the parent
                    # runner throughout this process's lifetime.
                    target(scope, path)
                    os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
                finally:
                    try:
                        os.unlink(temporary, dir_fd=fd)
                    except FileNotFoundError:
                        pass
            return ExecuteResult(stdout='', exit_code=0)

        def _verify_written_hash(self, path, content_bytes):
            raw = self.read_file_raw(path)
            okay = not raw.error and raw.content.encode('utf-8', 'surrogateescape') == content_bytes
            return (True, None) if okay else (False, WriteResult(error='Post-write verification failed'))

        def _snapshot_lsp_baseline(self, path):
            pass  # No external language server inside the no-network namespace.

        def _maybe_lsp_diagnostics(self, path, **kwargs):
            return None

        def _exec(self, command, **kwargs):
            return ExecuteResult(stdout='External linter unavailable in scoped editor', exit_code=127)

        def delete_file(self, path):
            with self.parent(path) as (fd, name):
                if not stat.S_ISREG(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
                    raise ValueError('REGULAR_FILE_REQUIRED')
                os.unlink(name, dir_fd=fd)
            return WriteResult()

        def move_file(self, src, dst):
            result = self.read_file_raw(src)
            if result.error:
                return WriteResult(error=result.error)
            result = self.write_file(dst, result.content)
            return result if result.error else self.delete_file(src)

    return Operations()


def edit(request):
    scope = request['scope']
    method = request['method']
    arguments = request['arguments']
    if method not in {'write_file', 'patch_replace', 'patch_v4a', 'read_file_raw'}:
        raise ValueError('FILE_OPERATION_REQUIRED')
    if method == 'patch_v4a':
        validate_edit(scope, 'patch', {'mode': 'patch', 'patch': arguments['patch_content']})
    else:
        target(scope, arguments['path'])
    result = getattr(secure_operations(scope), method)(**arguments)
    value = result.to_dict()
    if getattr(result, '_content_sha256', None):
        value['_content_sha256'] = result._content_sha256
    return value


if __name__ == '__main__':
    # The host venv dependencies are used read-only; no source is imported from
    # the untrusted checkout. The script's directory remains sys.path[0].
    try:
        print(json.dumps(edit(json.load(sys.stdin)), ensure_ascii=True))
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
