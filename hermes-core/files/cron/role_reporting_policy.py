"""Local role cron notification quiet hours; execution and evidence stay active."""
from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo
from hermes_cli.config import get_hermes_home

ROLE_SKILLS = {'complex-task-coordinator', 'complex-task-executor', 'complex-task-tester'}


def suppress_role_delivery(job, at=None):
    if not ROLE_SKILLS.intersection(job.get('skills') or []):
        return False
    policy_path = Path(get_hermes_home()) / 'role-reporting-policy.json'
    if not policy_path.exists():
        return False
    policy = json.loads(policy_path.read_text())
    at = (at or datetime.now(ZoneInfo(policy['timezone']))).astimezone(ZoneInfo(policy['timezone']))
    return at.hour >= policy['quiet_start_hour'] or at.hour < policy['quiet_end_hour']
