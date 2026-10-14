#!/usr/bin/env python3
"""Read authoritative documents to route reports; never claim tasks or call a model."""
import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROLES = {'A': ('1号', '执行者 A'), 'B': ('2号', '执行者 B'),
         'C': ('3号', '执行者 C'), 'TESTER': ('TESTER', '测试者')}
TERMINAL = {'ACCEPTED', 'ACCEPT_MERGED', 'ACCEPTED_MERGED', 'DONE', 'CLOSED',
            'CANCELLED', 'CANCELED', 'SUPERSEDED', 'REJECTED'}


def task_rows(text):
    """Shared parser; retain a legacy cell view for existing callers."""
    from progress_projection import parse_task_rows
    return {key:[key,row['state'],row['title'],'',row['dependencies'],row['progress'],row['owner']]
            for key,row in parse_task_rows(text).items()}


def accepted_dispatch(root, dispatch):
    for folder in [root / 'inbox/coordinator', root / 'decision']:
        if not folder.exists():
            continue
        for p in folder.rglob('*.md'):
            if p.stat().st_mtime < dispatch.stat().st_mtime:
                continue
            text = p.read_text()
            legacy_acceptance = ('acceptance' in p.name and
                                 str(dispatch.relative_to(root)) in text and
                                 bool(re.search(r'^\s*-\s*裁决[：:].*申报接受', text, re.M)))
            if legacy_acceptance:
                return True
            if not re.search(r'^\s*-?\s*status\s*[:：]\s*ACCEPT(?:ED)?(?:\b|_)', text, re.M | re.I):
                continue
            # Require the exact dispatch path or a task/attempt identity field, not a summary mention.
            path_match = bool(re.search(r'^\s*-?\s*(?:派单|dispatch)\s*[:：].*' +
                                       re.escape(str(dispatch.relative_to(root))), text, re.M | re.I))
            identity = re.search(r'^\s*-?\s*(?:\*\*)?(?:task/attempt|task|任务/attempt|派单)(?:\*\*)?\s*[:：](.*)$', text, re.M | re.I)
            identity_match = bool(identity and dispatch.parent.name in identity.group(1) and
                                  dispatch.stem in identity.group(1))
            if path_match or identity_match:
                return True
    return False


def routing(root, role):
    bindings = json.loads((root / 'ROLE-BINDINGS.json').read_text())
    if Path(bindings['project_root']).resolve() != root.resolve():
        return False, ['binding root mismatch']
    if bindings.get('control') != 'ACTIVE':
        return False, ['project is not ACTIVE']
    text = (root / 'TODO.md').read_text()
    from progress_projection import parse_task_rows, _owner_role, TERMINAL as TASK_TERMINAL, PAUSED
    tasks = parse_task_rows(text)
    if not tasks:
        return False, ['task table is unrecognized']
    markers = {'A': ['1号', '执行者A', '执行者 A'],
               'B': ['2号', '执行者B', '执行者 B'],
               'C': ['3号', '执行者C', '执行者 C'],
               'TESTER': ['TESTER', '测试者']}[role]
    reasons = []
    for task, row in tasks.items():
        if (_owner_role(row['owner']) == role or any(m in row['owner'] for m in markers)) and row['state'] not in TASK_TERMINAL | PAUSED:
            reasons.append('assigned nonterminal task: ' + task)
    lease_path = root / f'inbox/control/runtime/leases/{role}.json'
    if lease_path.exists() and json.loads(lease_path.read_text() or '{}').get('owner'):
        reasons.append('role lease exists; liveness requires normal checking')
    folder = root / 'inbox' / ROLES[role][0]
    if not folder.is_dir():
        return False, ['role inbox is absent']
    for p in folder.rglob('*.md'):
        if re.search(r'(?:^|[-_])ack(?:[-_.]|$)', p.name, re.I):
            continue
        body = p.read_text()
        head = body[:1800]
        if not (re.search(r'dispatch|rework', p.name, re.I) or
                re.search(r'^#.*(?:派单|返修)', head, re.M)):
            continue
        match = re.search(r'^\s*(?:-\s*)?(?:\*\*)?status(?:\*\*)?\s*[:：]\s*([A-Za-z_]+)', body, re.M | re.I)
        if match and match.group(1).upper() in TERMINAL:
            continue
        from progress_projection import TASK_PATTERN
        taskmatch = re.search(TASK_PATTERN, p.parent.name + '\n' + head)
        key = taskmatch.group(0) if taskmatch else None
        parent = key.rsplit('-',1)[0] if key and key.count('-')>1 else re.sub(r'[A-Z]$', '', key or '')
        row = tasks.get(key) or tasks.get(parent)
        if row and row['state'] in TASK_TERMINAL and p.stat().st_mtime <= (root / 'TODO.md').stat().st_mtime:
            # TODO acceptance supersedes old dispatch status, without changing that status.
            continue
        if accepted_dispatch(root, p):
            continue
        reasons.append('unresolved or unclassified dispatch: ' + str(p.relative_to(root)))
    return not reasons, reasons


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--role', choices=ROLES, required=True)
    parser.add_argument('--mode', choices=['active-gate', 'idle-message', 'inspect'], required=True)
    args = parser.parse_args()
    try:
        idle, reasons = routing(args.project, args.role)
    except (OSError, ValueError, KeyError) as exc:
        idle, reasons = False, ['classification unavailable: ' + type(exc).__name__]
    if args.mode == 'inspect':
        print(json.dumps({'role': args.role, 'confirmed_idle': idle, 'reasons': reasons}, ensure_ascii=False))
    elif args.mode == 'active-gate':
        print(json.dumps({'wakeAgent': not idle, 'role': args.role, 'confirmed_idle': idle,
                          'reasons': reasons[:6]}, ensure_ascii=False))
    elif idle:
        now = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M +08:00')
        print(f'**{ROLES[args.role][1]}：无任务、无变化 {now}**')
    # Unknown/busy sends no idle message; normal 20-minute agent reports remain enabled.


if __name__ == '__main__':
    main()
