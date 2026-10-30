"""群入口只保存指令；唯一协调业务入口消费，不另存任务状态。"""
import hashlib
from pathlib import Path
from runtime_common import locked, now, read_json, save_json


def directory(project):
    return Path(project).resolve()/'inbox/coordinator/requests'


def enqueue(project, *, message_id, chat_id, user_id, text, context=None):
    if not all(isinstance(v,str) and v.strip() for v in (message_id,chat_id,user_id,text)):
        raise ValueError('缺少完整消息身份或正文，未登记')
    request_id=hashlib.sha256(('feishu\0'+chat_id+'\0'+message_id).encode()).hexdigest()
    path=directory(project)/(request_id+'.json')
    authored={'message_id':message_id,'chat_id':chat_id,'user_id':user_id,'text':text,'context':context or {}}
    with locked(directory(project)/'queue.lock'):
        old=read_json(path)
        if old:
            if any(old.get(k)!=v for k,v in authored.items()):
                raise ValueError('同一消息身份出现冲突，禁止替换原指令')
            return {**old,'created':False}
        row={'schema':'coordinator-request/v1','request_id':request_id,
             'received_at':now(),'status':'PENDING',**authored}
        save_json(path,row)
        return {**row,'created':True}


def pending_requests(project):
    rows=[]
    for path in directory(project).glob('*.json'):
        row=read_json(path)
        if row.get('status')=='PENDING': rows.append({**row,'source':str(path)})
    return sorted(rows,key=lambda r:(r['received_at'],r['request_id']))


def acknowledge_request(project, request_id, evidence):
    if len(request_id)!=64 or any(c not in '0123456789abcdef' for c in request_id):
        raise ValueError('无效 request ID')
    evidence=Path(evidence).resolve(); root=Path(project).resolve()
    if not evidence.is_file() or not evidence.stat().st_size or not evidence.is_relative_to(root):
        raise ValueError('必须提供项目内非空处理记录；不能只确认观察游标')
    with locked(directory(root)/'queue.lock'):
        path=directory(root)/(request_id+'.json'); row=read_json(path)
        if not row: raise ValueError('请求不存在')
        if row['status']=='HANDLED': return row
        row.update(status='HANDLED',handled_at=now(),evidence=str(evidence),
                   evidence_sha256=hashlib.sha256(evidence.read_bytes()).hexdigest())
        save_json(path,row)
        return row
