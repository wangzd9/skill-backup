"""记录提示/工具指纹与漂移，不保存正文、不改请求和缓存策略。"""
import hashlib
import json
from pathlib import Path
from runtime_common import locked, now, read_json, save_json


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


class PrefixAudit:
    def __init__(self,directory): self.directory=Path(directory)

    def record(self, *, session_id, request, system_prompt, **metadata):
        if not session_id: return None
        tools=request.get('tools')
        available=isinstance(tools,list) and all(isinstance(t,dict) and isinstance(t.get('function',t),dict) for t in tools)
        if not available: tools=None
        path=self.directory/(hashlib.sha256(session_id.encode()).hexdigest()+'.json')
        with locked(self.directory/'audit.lock'):
            old=read_json(path,{}) or {}
            system_hash=digest(system_prompt) if system_prompt is not None else None
            tools_hash=digest(tools) if available else None
            system_changed=bool(old.get('system_hash') and system_hash and old['system_hash']!=system_hash)
            tools_changed=bool(old.get('tools_hash') and tools_hash and old['tools_hash']!=tools_hash)
            row={'session_id':session_id,'observed_at':now(),'system_hash':system_hash,
                 'tools_hash':tools_hash,'system_chars':len(str(system_prompt)),
                 'measurement_scope':'sanitized_hook_payload_only',
                 'system_observed_bytes':len(str(system_prompt).encode()) if system_prompt is not None else None,
                 'tools_observed_bytes':len(json.dumps(tools,ensure_ascii=False,separators=(',',':')).encode()) if available else None,
                 'measured_section_tokens':None,
                 'tool_names':[(t.get('function') or t).get('name','') for t in (tools or [])],
                 'tools_evidence':'sanitized_hook_payload' if available else 'UNAVAILABLE',
                 'system_changed':system_changed,'tools_changed':tools_changed,
                 'calls_observed':old.get('calls_observed',0)+1,
                 'system_changes':old.get('system_changes',0)+int(system_changed),
                 'tool_changes':old.get('tool_changes',0)+int(tools_changed),
                 **{k:metadata[k] for k in ('api_request_id','turn_id','model','api_call_count') if k in metadata}}
            save_json(path,row)
            if row['system_changed'] or row['tools_changed']:
                changes=self.directory/'changes.jsonl'
                with changes.open('a') as stream: stream.write(json.dumps(row,ensure_ascii=False)+'\n')
            return row
