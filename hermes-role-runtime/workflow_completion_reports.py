"""Fixed local receipt paths for final validation and requirement closure."""
import hashlib
from pathlib import Path


def _save(ledger, work, relative, text):
    run=next(r for r in ledger.snapshot()['runs'] if r['run_id']==work['run_id'])
    root=Path(run['requirement_root']).resolve()
    path=root/relative
    if not path.resolve().is_relative_to(root) or any(p.is_symlink() for p in [path,*path.parents] if p.is_relative_to(root)):
        raise ValueError('COMPLETION_RECEIPT_PATH_CHANGED')
    path.parent.mkdir(parents=True,exist_ok=True)
    raw=(text.rstrip()+'\n').encode()
    if path.exists():
        if path.read_bytes()!=raw:
            raise ValueError('COMPLETION_RECEIPT_CONFLICT')
    else:
        with path.open('xb') as stream:
            stream.write(raw)
    return {'path':str(path),'sha256':hashlib.sha256(raw).hexdigest()}


def validation_receipt(ledger, work, ctx, payload, result):
    lines=[f"# {ctx['plan']['validation_task_id']} 独立综合验证 · attempt {work['attempt']}",
        '',f"**结果：{result}；范围：已接受的本地开发验收。**",'',
        f"固定版本：`{ctx['target']['head']}`；分支：`{ctx['target']['branch']}`。",'',payload['summary'],
        '', '## 逐项验收证据','']
    for check in payload['checks']:
        lines.append(f"- **{check['id']}：{check['result']}**；类型 {check['mode']}；测试事件 {check['test_event_ids']}；{check['notes']}")
    lines+=['','## 问题与回派','']
    lines += [f"- **{i['id']} → {i.get('task_id') or '环境核查'}：** {i['summary']}；测试事件 {i['test_event_ids']}" for i in payload['issues']] or ['- 本次无新增已确认代码问题。']
    lines+=['','本地替身/隔离测试结果不代表真实节点、生产或 NAS 已启用。']
    return _save(ledger,work,Path('test-receipts')/ctx['plan']['validation_task_id']/f"attempt-{work['attempt']}.md",'\n'.join(lines))


def closure_receipt(ledger, work, state, payload):
    lines=[f"# {state['plan']['closure_task_id']} 本地开发验收收口",'',
        f"**固定版本：`{state['target']['head']}`；最终验证事件：{state['report']['event_id']}。**",'',
        payload['summary'],'','## 协调者证据复核','',payload['review'],'',
        '## 历史失败处置','']
    lines += [f"- **{row['verification_id']}：** {row['reason']}；本次复验证据事件 {row['test_event_ids']}" for row in payload['historical_failures']] or ['- 无待处置历史失败。']
    lines+=['','本次完成仅指已接受的本地开发范围；真实节点验收、生产启用、远端 push 等按原后置条件单独记录。']
    return _save(ledger,work,Path('reports')/state['plan']['closure_task_id']/f"validation-{state['report']['event_id']}.md",'\n'.join(lines))
