#!/usr/bin/env python3
"""Make/verify a private exact-source release; no live state or credentials."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import workflow_migration as migration
from scripts.runtime_layout import reconcile, wrapper_layout


def encoded(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()


def git(root,*args):
    return subprocess.check_output(['git','-C',str(root),*args],stderr=subprocess.DEVNULL)


def source_bytes(item):
    path=Path(item['path'])
    if item['kind']=='routing_semantics':
        return encoded(migration.routing_semantics(path))
    if item['kind']=='monitor_delivery_semantics':
        from monitor_queue import policy
        return encoded(policy(path.parent/'outbox'))
    return path.read_bytes()


def build(output,validation, *, profile_home=None):
    profile=Path(profile_home).resolve() if profile_home is not None else Path.home()
    roots=[ROOT,(profile/'.hermes/hermes-agent').resolve()]
    repos=[]
    for root in roots:
        repos.append({'root':str(root),'commit':git(root,'rev-parse','HEAD').decode().strip(),
            'tracked':set(git(root,'ls-files','-z').decode().split('\0'))})
    version=migration.code_version(profile_home=profile_home)
    tests=json.loads(Path(validation).read_text())
    if tests.get('version_digest')!=version['digest'] or tests.get('passed') is not True:
        raise ValueError('CURRENT_VERSION_VALIDATION_REQUIRED')
    if profile_home is not None and tests.get('profile_home')!=str(profile):
        raise ValueError('VALIDATION_PROFILE_MISMATCH')
    entry=reconcile(profile/'.codex/skills/complex-task-coordinator/scripts')
    wrappers=wrapper_layout(profile/'.hermes')
    if not entry['valid'] or not wrappers['valid']:
        raise ValueError('ENTRY_LAYOUT_UNVERIFIED')
    installed_sources = {str(Path(row['entry']).resolve()): Path(row['source']).resolve()
                         for row in wrappers['entries']}
    contents={}; sources=[]
    for item in version['files']:
        path=Path(item['path'])
        data=source_bytes(item)
        digest=hashlib.sha256(data).hexdigest()
        if digest!=item['sha256']: raise ValueError('SOURCE_CHANGED:'+str(path))
        managed=False
        managed_source=installed_sources.get(str(path),path)
        if managed_source != path and managed_source.read_bytes() != data:
            raise ValueError('INSTALLED_SOURCE_CHANGED:'+str(path))
        for repo in repos:
            root=Path(repo['root'])
            if managed_source.is_relative_to(root):
                relative=str(managed_source.relative_to(root))
                if relative not in repo['tracked']:
                    raise ValueError('UNTRACKED_RELEASE_SOURCE:'+str(path))
                if git(root,'diff','HEAD','--',relative):
                    raise ValueError('DIRTY_RELEASE_SOURCE:'+str(path))
                managed=True
                break
        if not managed and path.suffix in ('.py','.sh'):
            raise ValueError('UNOWNED_EXECUTABLE_SOURCE:'+str(path))
        name='sources/'+digest
        contents.setdefault(name,data)
        sources.append({**item,'archive_path':name,'git_managed':managed,
            'managed_source':str(managed_source),'mode':path.stat().st_mode&0o777 if path.exists() else 0o600})
    manifest={'schema_version':1,'version_digest':version['digest'],
        'profile_home':str(profile), 'deployment_acceptance':False,
        'repos':[{k:v for k,v in row.items() if k!='tracked'} for row in repos],
        'sources':sources,'skill_entries':entry,'wrapper_entries':wrappers,'validation':tests,
        'scope':'source/layout only; no business databases, leases, logs, credentials or new deployment acceptance',
        'restore':'Restore canonical source files and exact links after draining affected writers. Preserve business state and later increments. Routing semantics are a check, never a credential/config overwrite.'}
    contents['release.json']=encoded(manifest)
    output=Path(output).resolve(); output.parent.mkdir(parents=True,exist_ok=True)
    if output.exists(): raise FileExistsError(output)
    with tarfile.open(output,'w:gz') as stream:
        for name,data in contents.items():
            info=tarfile.TarInfo(name);info.size=len(data);info.mode=0o600
            stream.addfile(info,io.BytesIO(data))
    output.chmod(0o600)
    verify(output)
    return {'archive':str(output),'sha256':hashlib.sha256(output.read_bytes()).hexdigest(),
        'sources':len(sources),'version_digest':version['digest'],'repos':manifest['repos']}


def verify(archive):
    with tarfile.open(archive,'r:gz') as stream:
        manifest=json.load(stream.extractfile('release.json'))
        names={m.name for m in stream.getmembers()}
        if any(not n.startswith('sources/') and n!='release.json' for n in names):
            raise ValueError('UNEXPECTED_RELEASE_MEMBER')
        for source in manifest['sources']:
            raw=stream.extractfile(source['archive_path']).read()
            if hashlib.sha256(raw).hexdigest()!=source['sha256']:
                raise ValueError('CORRUPT_RELEASE_SOURCE')
    return {'verified':True,'sources':len(manifest['sources']),'version_digest':manifest['version_digest']}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--validation',type=Path)
    parser.add_argument('--verify',type=Path)
    parser.add_argument('--profile-home',type=Path,help='Explicit isolated release layout; does not alter live entries or grant deployment acceptance.')
    args=parser.parse_args()
    if args.verify: result=verify(args.verify)
    elif args.output and args.validation: result=build(args.output,args.validation,profile_home=args.profile_home)
    else: parser.error('use --verify or --output with --validation')
    print(json.dumps(result,ensure_ascii=False,indent=2))
