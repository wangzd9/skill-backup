"""Selected package boundaries for legacy packages sharing one runtime root."""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from runtime_common import locked, now, read_json, save_json


def task_scope(project):
    root=Path(project).resolve()
    binding=read_json(root/'ROLE-BINDINGS.json',{})
    authorization=binding.get('requirement_authorization') or {}
    selected=authorization.get('requirement_root')
    if not selected or Path(selected).resolve()==root:
        return None
    package=Path(selected)
    if not package.is_absolute() or package.resolve()!=package or package.parent!=root.parent:
        raise ValueError('兼容需求范围不是同目录下已选规范需求包')
    if authorization.get('runtime_root')!=str(root):
        raise ValueError('兼容需求授权与业务根不一致')
    source=package/'TODO.md'
    text=source.read_text() if source.is_file() else ''
    match=re.search(r'^本包任务编号定位[：:]\s*(.+)$',text,re.MULTILINE)
    tasks=sorted(set(re.findall(r'`(TASK-[A-Za-z0-9_-]+)`',match[1]))) if match else []
    return {'requirement_id':authorization.get('requirement_id'),'requirement_root':str(package),
            'runtime_root':str(root),'mode':'explicit_compatibility_task_list',
            'task_ids':tasks,'task_list_source':str(source),
            'instruction':'仅本包任务编号定位列出的精确task可执行；同根其他任务仅可追溯证据。新任务须先在本包任务定位登记，不复制业务状态。'}


def task_allowed(scope,task):
    return scope is None or isinstance(task,str) and task in scope['task_ids']


def require_task_scope(project,task):
    scope=task_scope(project)
    if not task_allowed(scope,task):
        raise ValueError('任务不在当前所选需求的明确范围内：'+str(task))
    return scope


def _checkpoint_paths(project, role):
    root=Path(project).resolve()
    if role not in {'COORDINATOR','A','B','C','TESTER'}:
        raise ValueError('无效角色检查点')
    base=root/'inbox/control/runtime/scope-checkpoints'/role
    if base.resolve()!=base:
        raise ValueError('检查点停放路径不是规范路径')
    return root,root/f'inbox/control/runtime/checkpoints/{role}.json',base


def _checkpoint_identity(value, role):
    if (not isinstance(value,dict) or value.get('role',role)!=role
            or any(not isinstance(value.get(key),str) or not value[key].strip()
                   for key in ('task','attempt'))):
        raise ValueError('停放检查点缺少明确角色、task/attempt')


def _checkpoint_index(base,role):
    index=read_json(base/'index.json',{'schema_version':1,'role':role,'snapshots':[]})
    if (not isinstance(index,dict) or index.get('schema_version')!=1 or index.get('role')!=role
            or not isinstance(index.get('snapshots'),list)):
        raise ValueError('停放检查点索引不可核实')
    seen=set()
    for item in index['snapshots']:
        if (not isinstance(item,dict) or not re.fullmatch(r'[a-f0-9]{64}',str(item.get('sha256','')))
                or item['sha256'] in seen or type(item.get('sequence')) is not int or item['sequence']<1
                or any(not isinstance(item.get(key),str) or not item[key] for key in ('task','attempt'))):
            raise ValueError('停放检查点索引身份不可核实')
        seen.add(item['sha256'])
    return index


def _snapshot_checkpoint(base,role,item):
    source=base/'snapshots'/(item['sha256']+'.json')
    if source.resolve()!=source:
        raise ValueError('停放检查点快照路径改变')
    raw=source.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=item['sha256']:
        raise ValueError('停放检查点快照哈希变化')
    checkpoint=json.loads(raw)
    _checkpoint_identity(checkpoint,role)
    if any(checkpoint[key]!=item[key] for key in ('task','attempt')):
        raise ValueError('停放检查点快照与索引身份不一致')
    return checkpoint,str(source)


def park_checkpoint(project,role,checkpoint):
    """Durably freeze the whole prior pointer before its caller overwrites it.

    Callers already hold the role guard. The immutable bytes become durable
    before their index, so an interruption never publishes an incomplete copy.
    Original ACKs, events, acceptance gates and historical files are untouched.
    """
    root,active,base=_checkpoint_paths(project,role)
    _checkpoint_identity(checkpoint,role)
    raw=(json.dumps(checkpoint,ensure_ascii=False,indent=2)+'\n').encode()
    if active.is_file():
        original=active.read_bytes()
        if json.loads(original)==checkpoint:
            raw=original
    digest=hashlib.sha256(raw).hexdigest()
    with locked(base/'index.lock'):
        index=_checkpoint_index(base,role)
        snapshot=base/'snapshots'/(digest+'.json')
        if snapshot.resolve()!=snapshot:
            raise ValueError('停放检查点快照路径改变')
        if snapshot.exists():
            if snapshot.read_bytes()!=raw:
                raise ValueError('停放检查点快照冲突')
        else:
            snapshot.parent.mkdir(parents=True,exist_ok=True)
            descriptor,temporary=tempfile.mkstemp(dir=snapshot.parent,prefix='.checkpoint-')
            try:
                with os.fdopen(descriptor,'wb') as stream:
                    stream.write(raw);stream.flush();os.fsync(stream.fileno())
                os.replace(temporary,snapshot)
                directory=os.open(snapshot.parent,os.O_RDONLY|os.O_DIRECTORY)
                try:os.fsync(directory)
                finally:os.close(directory)
            finally:
                if os.path.exists(temporary):os.unlink(temporary)
        if not any(item['sha256']==digest for item in index['snapshots']):
            index['snapshots'].append({'sha256':digest,'task':checkpoint['task'],'attempt':checkpoint['attempt'],
                'sequence':max((item['sequence'] for item in index['snapshots']),default=0)+1,'parked_at':now()})
            save_json(base/'index.json',index)
    return {'sha256':digest,'source':str(snapshot),'task':checkpoint['task'],'attempt':checkpoint['attempt']}


def checkpoint_for_scope(project,role,current=None,scope=None):
    """Read the current selected checkpoint, falling back to verified parking."""
    root,active,base=_checkpoint_paths(project,role)
    if current is None:current=read_json(active,{}) or {}
    if scope is None:scope=task_scope(root)
    if current and task_allowed(scope,current.get('task')):
        return current,str(active)
    if scope is None:return {},str(active)
    candidates=[]
    for item in _checkpoint_index(base,role)['snapshots']:
        if task_allowed(scope,item['task']):
            checkpoint,source=_snapshot_checkpoint(base,role,item)
            candidates.append((str(checkpoint.get('recorded_at','')),item['sequence'],checkpoint,source))
    if not candidates:return {},str(active)
    chosen=max(candidates,key=lambda item:item[:2])
    return chosen[2],chosen[3]


def restore_scope_checkpoint(project,role,task,attempt):
    """Return the newest exact checkpoint; never reconstruct the old claim state.

    This is a read-only lookup. A guarded caller may park the active pointer and
    publish the returned full checkpoint. No archived record is rewritten.
    """
    root,active,base=_checkpoint_paths(project,role)
    require_task_scope(root,task)
    candidates=[]
    current=read_json(active,{}) or {}
    if current.get('task')==task and current.get('attempt')==attempt:
        candidates.append((str(current.get('recorded_at','')),float('inf'),current,str(active)))
    for item in _checkpoint_index(base,role)['snapshots']:
        if item['task']==task and item['attempt']==attempt:
            checkpoint,source=_snapshot_checkpoint(base,role,item)
            candidates.append((str(checkpoint.get('recorded_at','')),item['sequence'],checkpoint,source))
    if not candidates:return {},str(active)
    chosen=max(candidates,key=lambda item:item[:2])
    return chosen[2],chosen[3]
