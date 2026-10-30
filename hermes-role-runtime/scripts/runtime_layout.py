#!/usr/bin/env python3
"""Install/check compatibility Skill entries; runtime files have one owner."""
import argparse
import hashlib
import json
import stat
from pathlib import Path
import os

ROOT = Path(__file__).resolve().parents[1]


def reconcile(entry, *, apply=False):
    entry = Path(entry).absolute()
    results = []
    for source in sorted(ROOT.glob('*.py')):
        target = entry/source.name
        valid = target.is_symlink() and target.resolve() == source.resolve()
        if not valid and apply:
            entry.mkdir(parents=True, exist_ok=True)
            if target.exists() and not target.is_symlink() and target.read_bytes() != source.read_bytes():
                raise ValueError('DIVERGENT_ENTRY_REQUIRES_BACKUP:'+str(target))
            temporary = target.with_name('.'+target.name+'.link')
            temporary.unlink(missing_ok=True)
            temporary.symlink_to(source)
            os.replace(temporary, target)
            valid = True
        results.append({'entry': str(target), 'source': str(source), 'valid': valid,
                        'sha256': hashlib.sha256(source.read_bytes()).hexdigest()})
    # A regular Python file outside the canonical root is an incomplete release.
    extra = [str(p) for p in entry.glob('*.py') if not (ROOT/p.name).is_file()]
    return {'valid': all(r['valid'] for r in results) and not extra, 'entries': results,
            'unmanaged_entries': extra, 'source_root': str(ROOT)}


def wrapper_layout(home, *, apply=False):
    """Materialize cron wrapper entries as regular files.

    Hermes cron script validation (scheduler_script._resolve_script_path) rejects any
    path that resolves outside HERMES_HOME/scripts/ — which breaks wrapper symlinks
    pointing at the canonical runtime tree. Shell wrappers exec an explicit runtime
    entry; lease_observe locates its profile's installed plugin. Byte copies remain
    inside the validated directory and are checked against Git-owned sources.
    """
    home=Path(home).resolve()
    pairs=[(ROOT/'lease_observe.py',home/'scripts/lease_observe.py')]
    pairs += [(source,home/'scripts/five-roles'/source.name) for source in sorted((ROOT/'scripts/five-roles').glob('*.sh'))]
    rows=[]
    for source,target in pairs:
        valid=(not target.is_symlink()) and target.is_file() and target.read_bytes()==source.read_bytes()
        if apply and not valid:
            if target.is_symlink() or not target.exists():
                target.parent.mkdir(parents=True,exist_ok=True)
                temporary=target.with_name('.'+target.name+'.copy'); temporary.unlink(missing_ok=True)
                temporary.write_bytes(source.read_bytes()); os.replace(temporary,target)
                try:
                    mode=stat.S_IMODE(os.stat(source).st_mode)
                    os.chmod(target,mode if mode else 0o755)
                except OSError:
                    os.chmod(target,0o755)
                valid=True
            elif target.read_bytes()!=source.read_bytes():
                raise ValueError('DIVERGENT_ENTRY_REQUIRES_BACKUP:'+str(target))
        rows.append({'entry':str(target),'source':str(source),'valid':valid})
    return {'valid':all(row['valid'] for row in rows),'entries':rows}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--entry', type=Path, default=Path.home()/'.codex/skills/complex-task-coordinator/scripts')
    parser.add_argument('--home',type=Path,default=Path.home()/'.hermes')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    value = reconcile(args.entry, apply=args.apply)
    value['wrappers']=wrapper_layout(args.home,apply=args.apply)
    value['valid']=value['valid'] and value['wrappers']['valid']
    print(json.dumps(value, ensure_ascii=False, indent=2))
    raise SystemExit(0 if value['valid'] else 1)
