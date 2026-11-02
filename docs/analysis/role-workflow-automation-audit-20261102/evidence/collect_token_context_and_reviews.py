"""读取限定样本，导出上下文大小及审查用量元数据，不导出正文。"""
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3

OUT = Path(__file__).resolve().parent
USAGE = json.loads((OUT/'token-usage-current.json').read_text())
LEFT = datetime.fromisoformat(USAGE['windows']['post_oct31_to_now']['start']).timestamp()
RIGHT = datetime.fromisoformat(USAGE['collected_at']).timestamp()


def connect(path):
    db = sqlite3.connect(Path(path).as_uri()+'?mode=ro', uri=True, timeout=1)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    return db


def size(value):
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True).encode())


def extract(text):
    decoder = json.JSONDecoder()
    for marker in ('{"accepted_requirements"', '{"candidate_approvals"', '{"candidates"'):
        offset = text.find(marker)
        if offset < 0:
            continue
        try:
            packet, _ = decoder.raw_decode(text[offset:])
            if isinstance(packet, dict) and packet.get('kind') == 'current-work-input':
                return packet
        except ValueError:
            pass
    return None


def contexts():
    usage = connect('/home/wang/.hermes/runtime/role-usage.db')
    rows = list(usage.execute("SELECT session_id,role,min(started_at) first_at FROM role_usage WHERE started_at>=? AND started_at<? AND task_kind='business_cron' GROUP BY session_id,role ORDER BY first_at DESC", (LEFT, RIGHT)))
    usage.close()
    selected = []
    for role in ('COORDINATOR', 'A', 'B', 'C', 'TESTER'):
        selected.extend([r for r in rows if r['role'] == role][:8])
    state = connect('/home/wang/.hermes/state.db')
    samples = []
    for row in selected:
        first = state.execute("SELECT content FROM messages WHERE session_id=? AND role='user' ORDER BY id LIMIT 1", (row['session_id'],)).fetchone()
        text = first['content'] or '' if first else ''
        packet = extract(text)
        entry = {'session_ref': hashlib.sha256(row['session_id'].encode()).hexdigest()[:16],
                 'role': row['role'], 'first_at': row['first_at'],
                 'initial_message_bytes': len(text.encode()), 'packet_found': bool(packet)}
        if packet:
            entry.update(packet_bytes=size(packet), projection=packet.get('input_projection'),
                         task_count=len(packet.get('tasks', [])), work_memory_present=bool(packet.get('work_memory_validation')),
                         fields=sorted([{'field': k, 'bytes': size(v)} for k,v in packet.items()], key=lambda x:x['bytes'], reverse=True)[:12])
        samples.append(entry)
    state.close()
    value = {'window': {'start': LEFT, 'end': RIGHT},
             'sampling': '每个角色在窗口内最近八个业务会话；非随机样本，字节不换算成token。',
             'samples': samples}
    (OUT/'token-context-samples.json').write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')
    return {'samples': len(samples), 'packets_found': sum(x['packet_found'] for x in samples),
            'coordinator': [x for x in samples if x['role']=='COORDINATOR'][:2]}


def reviews():
    base = Path('/home/wang/.hermes/role-pool.runtime/services')
    allowed = Path('/home/wang/.opencodereview/sessions').resolve()
    refs, failures = {}, Counter()
    files = list(base.glob('*/native/*-diagnosis.json'))
    for file in files:
        try:
            value = json.loads(file.read_text())
            name = value.get('session_file')
            if name:
                path = Path(name).resolve()
                if path.is_relative_to(allowed):
                    refs[str(path)] = refs.get(str(path), 0)+1
                else:
                    failures['outside_expected_root'] += 1
        except (OSError, ValueError):
            failures['invalid_diagnosis'] += 1
    windows = {'post_oct31_to_now': (LEFT, RIGHT),
               'last_24h': (RIGHT-86400, RIGHT),
               'pre_three_full_days': (datetime.fromisoformat(USAGE['windows']['pre_three_full_days']['start']).timestamp(),
                                       datetime.fromisoformat(USAGE['windows']['pre_three_full_days']['end']).timestamp())}
    totals = {k: Counter() for k in windows}
    sessions = []
    bytes_read = 0
    for name, count in refs.items():
        path = Path(name)
        if not path.is_file():
            failures['missing_session'] += 1
            continue
        bytes_read += path.stat().st_size
        used = set()
        local = {k: Counter() for k in windows}
        for number, line in enumerate(path.open(errors='replace')):
            try:
                event = json.loads(line)
            except ValueError:
                failures['invalid_session_line'] += 1
                continue
            if event.get('type') != 'llm_response':
                continue
            key = event.get('uuid') or 'line:'+str(number)
            if key in used:
                continue
            used.add(key)
            try:
                at = datetime.fromisoformat(event['timestamp'].replace('Z','+00:00')).timestamp()
            except (ValueError, KeyError, TypeError):
                failures['invalid_response_time'] += 1
                continue
            for window, (start, end) in windows.items():
                if not start <= at < end:
                    continue
                target = local[window]
                target['responses'] += 1
                usage = event.get('usage')
                if not isinstance(usage, dict):
                    target['usage_missing'] += 1
                    continue
                for field in ('prompt_tokens','completion_tokens'):
                    amount = usage.get(field)
                    if type(amount) is int and amount >= 0:
                        target[field] += amount
                        target[field+'_calls'] += 1
        for window in windows:
            totals[window].update(local[window])
        if any(c.get('responses') for c in local.values()):
            sessions.append({'session_ref': hashlib.sha256(name.encode()).hexdigest()[:16],
                             'diagnosis_reference_count': count, 'windows': {k:dict(v) for k,v in local.items()}})
    value = {'window_basis': '独立审查llm_response时间；角色账本按请求started_at，边界不同，不作完整全系统总量。',
             'scope': '仅当前诊断文件可定位的独立审查会话；会话路径及response身份去重，包含失败尝试已报告用量，不保证覆盖在途、未报告或其他强审。',
             'diagnosis_files': len(files), 'unique_session_files': len(refs), 'session_bytes_scanned': bytes_read,
             'failures': dict(failures), 'totals': {k:dict(v) for k,v in totals.items()}, 'sessions': sessions}
    (OUT/'token-review-usage.json').write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')
    return {k:v for k,v in value.items() if k!='sessions'}


if __name__ == '__main__':
    print(json.dumps({'contexts':contexts(), 'reviews':reviews()}, ensure_ascii=False))
