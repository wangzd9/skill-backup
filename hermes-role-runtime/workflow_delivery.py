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
