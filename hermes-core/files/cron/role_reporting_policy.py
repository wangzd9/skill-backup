"""Local role cron notification quiet hours; execution and evidence stay active."""
from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo
from hermes_cli.config import get_hermes_home

ROLE_SKILLS = {'complex-task-coordinator', 'complex-task-executor', 'complex-task-tester'}


def is_role_job(job, policy=None):
    """Identify role jobs from role skills or this profile's explicit bindings."""
    skills = job.get('skills') or []
    if isinstance(skills, str):
        skills = [skills]
    if ROLE_SKILLS.intersection(skills):
        return True
    if policy is None:
        policy_path = Path(get_hermes_home()) / 'role-reporting-policy.json'
        policy = json.loads(policy_path.read_text()) if policy_path.exists() else {}
    return job.get('id') in (policy.get('role_job_ids') or [])


def suppress_role_delivery(job, at=None):
    policy_path = Path(get_hermes_home()) / 'role-reporting-policy.json'
    if not policy_path.exists():
        return False
    policy = json.loads(policy_path.read_text())
    # Report sessions deliberately load no role skills. Their explicitly bound
    # IDs still need the same delivery-time gate, including delayed results.
    if not is_role_job(job, policy):
        return False
    at = (at or datetime.now(ZoneInfo(policy['timezone']))).astimezone(ZoneInfo(policy['timezone']))
    return at.hour >= policy['quiet_start_hour'] or at.hour < policy['quiet_end_hour']
