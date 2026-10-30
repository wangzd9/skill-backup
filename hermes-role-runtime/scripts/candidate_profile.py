#!/usr/bin/env python3
"""Create a fresh offline source/layout profile, without changing live entries.

Only Skill documents/templates are copied. Runtime links target this checkout;
the installed Hermes core is a read-only dependency of validation. No business
state, credentials, cron jobs, user config or service units are installed.
"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.runtime_layout import reconcile, wrapper_layout

SKILLS = ('complex-task-planner', 'complex-task-coordinator',
          'complex-task-executor', 'complex-task-tester',
          'complex-task-reviewer', 'feishu-progress-style')


def create(destination, source_home):
    destination = Path(destination).absolute()
    source_home = Path(source_home).resolve()
    # Refuse existing paths, including dangling symlinks. Never reuse a live home.
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    core = source_home/'.hermes/hermes-agent'
    if not (core/'.git').exists():
        raise ValueError('GIT_MANAGED_HERMES_CORE_REQUIRED')
    destination.mkdir(parents=True, mode=0o700)
    for name in SKILLS:
        source = source_home/'.codex/skills'/name
        if not (source/'SKILL.md').is_file():
            raise ValueError('SKILL_DOCUMENT_REQUIRED:'+name)
        for path in source.rglob('*'):
            if (not path.is_file() or path.suffix not in ('.md', '.yaml', '.sh', '.service', '.timer')
                    or any(part.startswith('.') for part in path.relative_to(source).parts)):
                continue
            target = destination/'.codex/skills'/name/path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
    hermes = destination/'.hermes'
    hermes.mkdir()
    (hermes/'hermes-agent').symlink_to(core.resolve(), target_is_directory=True)
    (hermes/'plugins').mkdir()
    (hermes/'plugins/role-workflow-runtime').symlink_to(ROOT, target_is_directory=True)
    entry = reconcile(destination/'.codex/skills/complex-task-coordinator/scripts', apply=True)
    wrappers = wrapper_layout(hermes, apply=True)
    result = {'profile_home': str(destination.resolve()), 'runtime_root': str(ROOT),
              'source_home': str(source_home), 'valid': entry['valid'] and wrappers['valid'],
              'skill_entries': entry, 'wrapper_entries': wrappers,
              'scope': 'offline source/layout; production routing and business state are not copied',
              'deployment_acceptance': False}
    (destination/'candidate-profile.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source-home', type=Path, default=Path.home())
    args = parser.parse_args()
    print(json.dumps(create(args.output, args.source_home), ensure_ascii=False, indent=2))
