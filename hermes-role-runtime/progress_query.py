"""Read current authoritative records for a human-facing query, without business writes."""
from datetime import datetime
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

from coord_queue import pending_requests
if __package__:
    from .progress_display import render_progress, enrich_records
else:
    from progress_display import render_progress, enrich_records

def progress_query(text):
    # Only explicit opt-in. Natural-language progress always stays in the model conversation.
    return text.strip() == '/progress'


def collect_live(root):
    scripts=Path.home()/'.codex/skills/complex-task-coordinator/scripts'
    if str(scripts) not in sys.path: sys.path.insert(0,str(scripts))
    from role_observer import collect
    snapshot,_=collect(root,'COORDINATOR')
    from runtime_tools import refresh_progress_facts
    packet=refresh_progress_facts(root,snapshot=snapshot)
    return {**snapshot,'projection':packet}


def progress_answer(project,job,at=None):
    root=Path(project).resolve()
    at=at or datetime.now(ZoneInfo('Asia/Shanghai'))
    try:
        snapshot=collect_live(root)
        if snapshot.get('role')!='COORDINATOR' or Path(snapshot.get('project','')).resolve()!=root:
            raise ValueError('progress identity mismatch')
        pending=pending_requests(root)
    except Exception:
        poll='已启用' if job.get('enabled') is True else '**暂停或未配置**'
        return ('**协调者｜当前进展无法核实**\n\n'
                '- **原因：** 当前文档读取失败或身份/内容不一致；没有把旧快照冒充最新状态。\n'
                '- **调度：** 后台巡检'+poll+'；本次不改变调度。\n'
                '- **下一步：** 请协调者核对来源后说明进展；可直接发送“请核对当前进展并解释”。')
    return render_progress(snapshot,at=at,job=job,pending_count=len(pending))
