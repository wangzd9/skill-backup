"""Freeze a factual delivery receipt for the coordinator's gated completion."""
import hashlib
import json


def complete(ledger, identity, candidate_id, summary, *, command_id):
    checked=ledger.validate_identity(identity)
    if not checked.get('valid'):
        return checked
    if checked['work']['role']!='COORDINATOR':
        return {'status':'REJECTED','code':'COORDINATOR_REQUIRED'}
    if not isinstance(summary,str) or not summary.strip() or len(summary)>65536:
        raise ValueError('BOUNDED_FINAL_DELIVERY_SUMMARY_REQUIRED')
    snapshot=ledger.snapshot(checked['work']['run_id'])
    candidate=next(row for row in snapshot['candidates'] if row['candidate_id']==candidate_id)
    if candidate['run_id']!=checked['work']['run_id']:
        raise ValueError('FINAL_DELIVERY_RUN_MISMATCH')
    if not ledger.acceptance(candidate_id).get('ready'):
        return {'status':'REJECTED','code':'ACCEPTANCE_GATES_NOT_CLEAR'}
    merges=[row for row in snapshot['merges'] if row['candidate_id']==candidate_id]
    document={'candidate_id':candidate_id,'run_id':candidate['run_id'],'task_id':candidate['task_id'],
              'attempt':candidate['attempt'],'base':candidate['base'],'head':candidate['head'],
              'summary':summary,'merges':merges,
              'verifications':[row for row in snapshot['verifications'] if row['candidate_id']==candidate_id],
              'production_deployed':False}
    raw=json.dumps(document,ensure_ascii=False,sort_keys=True,indent=2).encode()
    sha=hashlib.sha256(raw).hexdigest()
    folder=ledger.path.parent/'deliveries';folder.mkdir(exist_ok=True)
    path=folder/(sha+'.json')
    if not path.exists():
        with path.open('xb') as output:output.write(raw)
        path.chmod(0o400)
    elif path.read_bytes()!=raw:
        raise ValueError('FINAL_DELIVERY_RECEIPT_CHANGED')
    result=ledger.decide(identity,candidate_id,'COMPLETE',command_id=command_id,
        evidence={'delivery':{'path':str(path),'sha256':sha},'merged_head':candidate['head'] if merges else None})
    return {**result,'delivery_receipt':str(path)}


from pathlib import Path


def evidence_directory(ledger, work_id, *, create=False):
    import hashlib
    from pathlib import Path
    root=ledger.path.parent.resolve()
    parent=root/'delivery-evidence'
    path=parent/hashlib.sha256(work_id.encode()).hexdigest()
    if parent.is_symlink() or path.is_symlink():raise ValueError('DELIVERY_EVIDENCE_DIRECTORY_CHANGED')
    if create:
        path.mkdir(parents=True,exist_ok=True,mode=0o700)
        parent.chmod(0o700);path.chmod(0o700)
    if path.resolve()!=path:raise ValueError('DELIVERY_EVIDENCE_DIRECTORY_CHANGED')
    return path


def contract(ledger, work):
    return {'directory':str(evidence_directory(ledger,work['work_id'])),
        'write_tool':'当前受限terminal可写本单证据目录；使用绝对路径重定向自测输出。',
        'freeze':'先commit业务改动，保持候选HEAD和整个Git状态干净（含untracked）；不要把自测输出留在工作区。',
        'references':'candidate.evidence逐项使用现存持久文件的绝对路径；可附sha256；运行时重新校验。',
        'retention':'工作结束后保留证据及哈希；不是可随意清理的临时缓存。'}


def preflight(ledger, candidate):
    """Report all evidence field errors before candidate insertion, no guessing."""
    from business_ledger import _deny, _Denied
    from pathlib import Path
    errors=[]
    for index,ref in enumerate(candidate.get('evidence') or []):
        field='candidate.evidence.'+str(index)+'.path'
        if not isinstance(ref,dict) or not isinstance(ref.get('path'),str):
            errors.append({'field':field,'code':'EVIDENCE_FILE_REQUIRED'});continue
        path=Path(ref['path'])
        if not path.is_absolute():
            errors.append({'field':field,'path':str(path),'code':'EVIDENCE_ABSOLUTE_PATH_REQUIRED'});continue
        try:
            ledger._evidence(ref)
        except (OSError,ValueError,_Denied) as exc:
            # Native refusal details are safe; never echo file contents.
            errors.append({'field':field,'path':str(path),'code':getattr(exc,'code',str(exc))})
    if errors:_deny('DELIVERY_EVIDENCE_PREFLIGHT_FAILED',errors=errors,
        recovery='把自测输出写入delivery_evidence.directory，按实际绝对路径提交；不忽略dirty、不猜相对路径。')
