"""Bounded read-only scene sampling; no model, messaging, retries or business writes."""
import hashlib
import ipaddress
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
from runtime_common import locked, read_json, save_json

ROLES = {'COORDINATOR', 'A', 'B', 'C', 'TESTER'}
TERMINAL = {'SCENE_DONE', 'SCENE_FAILED', 'FAILED', 'CANCELLED', 'CANCELED', 'DONE'}
HTTP_CODE = r'''
import json,sys,urllib.request
with urllib.request.urlopen(sys.argv[1],timeout=5) as response:body=response.read(2097153)
if len(body)>2097152:raise ValueError('backend response exceeds bounded read')
value=json.loads(body)
if not isinstance(value,dict):raise ValueError('backend response must be an object')
print(json.dumps(value))
'''

# This program and its child argv are fixed. Spec values are JSON data, never
# shell fragments. TCP evidence is namespace-wide, not attributed to PID 1.
PROCESS_CODE = r'''
import json,sys
from pathlib import Path
run,attempt=sys.argv[1:]
proc=Path('/proc/1');parts=(proc/'stat').read_text().rsplit(') ',1)[1].split()
out={'pid':1,'start_ticks':parts[19],'cpu_ticks':int(parts[11])+int(parts[12]),
     'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
out['namespace_tcp_to_llm']=[]
for name in ('net/tcp','net/tcp6'):
 for row in (proc/name).read_text().splitlines()[1:]:
  cells=row.split()
  if cells[2]=='2D02A8C0:1F42':
   out['namespace_tcp_to_llm'].append({'remote':cells[2],'state':cells[3]})
work=Path('/var/lib/scene-proxy/work')/run/('attempt-'+attempt)
base=Path('/var/lib/scene-proxy/work').resolve()
if not work.resolve().is_relative_to(base):raise ValueError('work path escape')
out['files']={}
for name in ('checkpoint.json','spray_model_results.json','render_log.json'):
 p=work/name
 if not p.exists():out['files'][name]={'exists':False};continue
 if not p.resolve().is_relative_to(base):raise ValueError('evidence path escape')
 s=p.stat();row={'exists':True,'bytes':s.st_size,'mtime_ns':s.st_mtime_ns}
 if name=='checkpoint.json':
  if s.st_size>262144:raise ValueError('checkpoint exceeds bounded read')
  row['content']=json.loads(p.read_text())
 out['files'][name]=row
print(json.dumps(out))
'''
REMOTE_CODE = r'''
import json,shutil,subprocess,urllib.request
from datetime import datetime,timezone
cfg=json.loads(SPEC_JSON)
out={'observed_at':datetime.now(timezone.utc).isoformat(timespec='seconds'),'errors':{}}
def probe(name,fn):
 try:out[name]=fn()
 except Exception as e:out['errors'][name]={'type':type(e).__name__,'detail':str(e)[:400]}
def health():
 with urllib.request.urlopen('http://127.0.0.1:8767/health',timeout=4) as response:
  return json.loads(response.read(1048576))
def docker(args):
 value=subprocess.run(['docker']+args,capture_output=True,text=True,timeout=6,check=True)
 return json.loads(value.stdout)
probe('proxy_health',health)
probe('container',lambda:docker(['inspect','--format','{{json .}}',cfg['container']]))
if 'container' in out:
 c=out['container'];out['container']={'id':c.get('Id'),'image':c.get('Image'),'state':
  {k:c.get('State',{}).get(k) for k in ('Status','Running','Pid','StartedAt','ExitCode','Error')}}
probe('process',lambda:docker(['exec',cfg['container'],'python3','-c',PROCESS_SCRIPT,cfg['run_id'],str(cfg['attempt'])]))
probe('disk',lambda:dict(zip(('total','used','free'),shutil.disk_usage(cfg['storage_path']))))
print(json.dumps(out))
'''


def validate(spec):
    if not isinstance(spec, dict):
        raise ValueError('spec must be an object')
    for name in ('observation_id', 'run_id', 'task', 'container', 'remote_alias'):
        if not isinstance(spec.get(name), str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', spec[name]):
            raise ValueError('invalid ' + name)
    if spec['observation_id'] == 'task-observation-config':
        raise ValueError('observation_id is reserved for configuration health')
    if type(spec.get('attempt')) is not int or spec['attempt'] < 1:
        raise ValueError('attempt must be a positive integer')
    roles = spec.get('notify_roles')
    if not isinstance(roles, list) or not roles or any(r not in ROLES for r in roles):
        raise ValueError('invalid notify_roles')
    url = urlsplit(spec.get('backend_url', ''))
    if url.scheme not in ('http', 'https') or url.username or url.password or url.query or url.fragment or url.path not in ('', '/'):
        raise ValueError('invalid backend_url')
    if url.hostname != 'localhost' and not ipaddress.ip_address(url.hostname or '').is_private:
        raise ValueError('backend must be a private address')
    if not url.port:
        raise ValueError('explicit backend port is required')
    storage = spec.get('storage_path', '')
    if not isinstance(storage, str) or not storage.startswith('/mnt/') or str(PurePosixPath(storage)) != storage or '..' in PurePosixPath(storage).parts:
        raise ValueError('storage_path must be a canonical /mnt path')
    ZoneInfo(spec['business_timezone'])
    for name, default in (('heartbeat_stale_seconds', 180), ('min_free_bytes', 0)):
        if type(spec.get(name, default)) is not int or spec.get(name, default) < (1 if name.startswith('heartbeat') else 0):
            raise ValueError('invalid ' + name)
    return spec


def _get_json(url, timeout):
    # A socket timeout alone is not a wall-clock bound for a trickling body.
    # The fixed child gives each entire GET a hard deadline, including decode.
    response = subprocess.run([sys.executable, '-c', HTTP_CODE, url], text=True,
                              capture_output=True, timeout=timeout, check=True)
    return json.loads(response.stdout)


def _remote(spec, timeout):
    code = REMOTE_CODE.replace('SPEC_JSON', repr(json.dumps(spec))).replace('PROCESS_SCRIPT', repr(PROCESS_CODE))
    response = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
                               spec['remote_alias'], 'python3', '-'], input=code,
                              text=True, capture_output=True, timeout=timeout, check=True)
    value = json.loads(response.stdout)
    if not isinstance(value, dict):
        raise ValueError('remote response must be an object')
    return value


def _steps(rows, attempt):
    if not isinstance(rows, list):
        raise ValueError('steps must be a list')
    selected = []
    for row in rows:
        if not isinstance(row, dict) or not row.get('step_name') or not row.get('status'):
            raise ValueError('unrecognized step')
        if row.get('attempt') == attempt:
            selected.append({k: row.get(k) for k in ('step_name', 'attempt', 'status', 'error_message')})
    return sorted(selected, key=lambda r: r['step_name'])


def sample(spec, now=None):
    validate(spec)
    moment = now or datetime.now(timezone.utc)
    deadline = time.monotonic() + 45
    out = {'schema_version': 1, **{k: spec[k] for k in ('observation_id', 'task', 'run_id', 'attempt', 'notify_roles')},
           'observed_at': moment.isoformat(timespec='seconds'), 'errors': {},
           'scope': 'read-only evidence; not progress percentage, content validation or approval'}
    for name, route in (('pipeline', 'pipeline-runs'), ('scene', 'scene-analysis-runs')):
        try:
            value = _get_json(spec['backend_url'].rstrip('/')+'/api/v1/'+route+'/'+spec['run_id'], min(6, max(.1, deadline-time.monotonic())))
            if value.get('pipeline_run_id') != spec['run_id'] or type(value.get('attempt')) is not int or not value.get('status'):
                raise ValueError('run response identity/status missing or mismatched')
            out[name] = {k: value.get(k) for k in ('pipeline_run_id', 'source_id', 'status', 'attempt', 'heartbeat_at',
                'lease_expires_at', 'lease_owner', 'error_message', 'effective_config_hash', 'frozen_at', 'submission_id', 'profile_id', 'scenes')}
            out[name]['steps'] = _steps(value.get('steps'), value['attempt'])
        except Exception as exc:
            out['errors'][name] = {'type': type(exc).__name__, 'detail': str(exc)[:400]}
    actual = out.get('pipeline', {}).get('attempt', spec['attempt'])
    out['configured_attempt'], out['attempt'] = spec['attempt'], actual
    try:
        out['remote'] = _remote({**spec, 'attempt': actual}, min(25, max(.1, deadline-time.monotonic())))
        for name, error in out['remote'].get('errors', {}).items():
            out['errors']['remote_'+name] = error
    except Exception as exc:
        out['errors']['remote'] = {'type': type(exc).__name__, 'detail': str(exc)[:400]}
    pipe, scene, remote = out.get('pipeline', {}), out.get('scene', {}), out.get('remote', {})
    health = {}
    if pipe:
        health['heartbeat'] = 'not_required' if pipe['status'] in TERMINAL else 'unknown'
        if pipe.get('heartbeat_at'):
            try:
                raw = pipe['heartbeat_at']; parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
                basis = 'explicit_offset'
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=ZoneInfo(spec['business_timezone'])); basis = 'configured_business_timezone'
                age = (moment-parsed).total_seconds()
                out['heartbeat'] = {'raw_at': raw, 'interpreted_at': parsed.isoformat(), 'timezone_basis': basis,
                                    'business_timezone': spec['business_timezone'], 'age_seconds': age}
                if pipe['status'] not in TERMINAL:
                    health['heartbeat'] = 'clock_skew' if age < -30 else ('stale' if age > spec.get('heartbeat_stale_seconds', 180) else 'healthy')
            except Exception as exc:
                out['errors']['heartbeat'] = {'type': type(exc).__name__, 'detail': str(exc)[:400]}
        elif pipe['status'] not in TERMINAL:
            out['errors']['heartbeat'] = {'type': 'MissingHeartbeat', 'detail': 'running task has no heartbeat evidence'}
    if pipe and scene and (pipe['attempt'], pipe['status'], pipe['source_id']) != (scene['attempt'], scene['status'], scene['source_id']):
        out['errors']['backend_consistency'] = {'type': 'SnapshotMismatch', 'detail': 'two GETs disagree; no state inferred'}
    health['proxy'] = remote.get('proxy_health', {}).get('status', 'unknown')
    active = remote.get('proxy_health', {}).get('active_runs')
    health['proxy_run'] = 'not_required' if pipe.get('status') in TERMINAL else (
        ('present' if any(isinstance(row, dict) and row.get('pipeline_run_id') == spec['run_id'] for row in active) else 'absent')
        if isinstance(active, list) else 'unknown')
    health['container'] = remote.get('container', {}).get('state', {}).get('Status', 'unknown')
    health['disk'] = ('low' if remote['disk']['free'] < spec.get('min_free_bytes', 0) else 'healthy') if remote.get('disk') else 'unknown'
    process = remote.get('process', {})
    checkpoint = process.get('files', {}).get('checkpoint.json', {}).get('content', {}).get('steps', {})
    if not isinstance(checkpoint, dict):
        out['errors']['checkpoint'] = {'type': 'InvalidCheckpoint', 'detail': 'checkpoint steps must be an object'}; checkpoint = {}
    elif any(not isinstance(row, dict) for row in checkpoint.values()):
        out['errors']['checkpoint'] = {'type': 'InvalidCheckpoint', 'detail': 'checkpoint step must be an object'}
    semantic = {'identity': {k: out[k] for k in ('observation_id', 'task', 'run_id', 'notify_roles')},
                'attempt': actual, 'status': pipe.get('status'), 'steps': scene.get('steps'), 'source_id': pipe.get('source_id'),
                'config_hash': scene.get('effective_config_hash'), 'error': scene.get('error_message'), 'health': health,
                'process_identity': {k: process.get(k) for k in ('pid', 'start_ticks', 'boot_id')},
                'container_id': remote.get('container', {}).get('id'),
                'checkpoint_steps': {k: {f: v.get(f) for f in ('status', 'detail')} for k, v in checkpoint.items() if isinstance(v, dict)},
                'unknown': {k: v['type'] for k, v in out['errors'].items()}}
    out['health'], out['semantic'] = health, semantic
    out['semantic_fingerprint'] = hashlib.sha256(json.dumps(semantic, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return out


def _publish(base, observation):
    identity = observation['observation_id']; folder = base/'task-observations'/identity
    notice_path = base/'task-notices'/f'{identity}.json'
    # Notice is the stable semantic record. If a crash happens after publishing
    # it but before refreshing latest, the next sample does not announce it twice.
    previous = read_json(notice_path, {}) or {}
    changed = previous.get('semantic_fingerprint') != observation['semantic_fingerprint']
    observation['revision'] = previous.get('revision', 0) + int(changed)
    if changed:
        save_json(notice_path, {k: observation[k] for k in
            ('schema_version', 'observation_id', 'notify_roles', 'observed_at', 'revision', 'semantic', 'semantic_fingerprint')})
    save_json(folder/'latest.json', observation)
    return observation


def sample_registered(project):
    root = Path(project).resolve(); spec_path = root/'inbox/control/runtime/task-observation-spec.json'
    if not spec_path.exists():
        return None
    base = root/'inbox/control/runtime'
    with locked(base/'task-observation.lock'):
        try:
            observation = sample(read_json(spec_path))
            semantic = {'configuration': 'valid'}
        except Exception as exc:
            observation = None; semantic = {'unknown': type(exc).__name__+': '+str(exc)[:200]}
        if observation is None or (base/'task-notices/task-observation-config.json').exists():
            config = {'schema_version': 1, 'observation_id': 'task-observation-config', 'notify_roles': ['COORDINATOR'],
                      'observed_at': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'semantic': semantic,
                      'semantic_fingerprint': hashlib.sha256(json.dumps(semantic, sort_keys=True).encode()).hexdigest()}
            _publish(base, config)
            if observation is None:
                return config
        return _publish(base, observation)
