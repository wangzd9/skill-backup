"""只读统计角色物理请求用量；不导出提示、工具正文、凭据或推理内容。"""
from collections import Counter, defaultdict
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import statistics
import sys
import time
from zoneinfo import ZoneInfo

sys.path.insert(0, '/home/wang/work/hermes-role-runtime')
from usage_accounting import ALL_TOKEN_FIELDS, known_usage, business_attributed

OUT = Path(__file__).resolve().parent
ZONE = ZoneInfo('Asia/Shanghai')
END = time.time()
START = datetime(2026, 10, 27, tzinfo=ZONE).timestamp()


def epoch(value):
    return datetime.fromisoformat(value).timestamp()


def stamp(value):
    return datetime.fromtimestamp(value, ZONE).isoformat()


def distribution(values):
    values = sorted(values)
    return {'n': len(values), 'p50': statistics.median(values) if values else None,
            'p90': values[int((len(values)-1)*.9)] if values else None,
            'max': max(values) if values else None,
            'mean': sum(values)/len(values) if values else None}


def aggregate(rows):
    amounts, coverage = Counter(), Counter()
    prompts, outputs, durations = [], [], []
    cache_prompt = cache_read = 0
    sessions, logical = set(), set()
    statuses, models, routes, normalizations = Counter(), Counter(), Counter(), Counter()
    attributed = 0
    short_calls = short_prompt = 0
    executions = defaultdict(int)
    for row in rows:
        values = row['known']
        sessions.add(row['session_id'])
        logical.add((row['session_id'], row['logical_request_id']))
        statuses[row['status']] += 1
        models[row['model_label']] += 1
        routes[row['route']] += 1
        normalizations[row['normalization_version']] += 1
        attributed += business_attributed(row)
        if row['execution_id']:
            executions[row['execution_id']] += 1
        for key in ALL_TOKEN_FIELDS:
            if values[key] is not None:
                coverage[key] += 1
                amounts[key] += values[key]
        if values['prompt_tokens'] is not None:
            prompts.append(values['prompt_tokens'])
        if values['output_tokens'] is not None:
            outputs.append(values['output_tokens'])
        if values['prompt_tokens'] is not None and values['cache_read_tokens'] is not None:
            cache_prompt += values['prompt_tokens']
            cache_read += values['cache_read_tokens']
        if values['prompt_tokens'] is not None and values['output_tokens'] is not None and values['output_tokens'] <= 256:
            short_calls += 1
            short_prompt += values['prompt_tokens']
        if row['ended_at'] is not None:
            durations.append(max(0, row['ended_at']-row['started_at']))
    return {'physical_requests': len(rows), 'logical_requests': len(logical),
            'extra_physical_attempts': len(rows)-len(logical), 'sessions': len(sessions),
            'known_token_sums': {k: amounts[k] if coverage[k] else None for k in ALL_TOKEN_FIELDS},
            'field_coverage_calls': {k: coverage[k] for k in ALL_TOKEN_FIELDS},
            'prompt_per_request': distribution(prompts), 'output_per_request': distribution(outputs),
            'duration_seconds': distribution(durations), 'business_attributed_requests': attributed,
            'statuses': dict(statuses), 'models': dict(models), 'routes': dict(routes),
            'normalizations': dict(normalizations),
            'cache_paired_prompt_tokens': cache_prompt if cache_prompt else None,
            'cache_paired_read_tokens': cache_read if cache_prompt else None,
            'cache_read_ratio_on_paired_requests': cache_read/cache_prompt if cache_prompt else None,
            'requests_per_execution_in_window': distribution(list(executions.values())),
            'output_le256': {'calls': short_calls, 'prompt_tokens': short_prompt,
                             'meaning': '短输出请求候选，不是浪费判定'}}


def group(rows, fields):
    buckets = defaultdict(list)
    for row in rows:
        buckets[tuple(row[k] for k in fields)].append(row)
    return sorted([{**dict(zip(fields, key)), **aggregate(value)} for key, value in buckets.items()],
                  key=lambda r: r['known_token_sums']['prompt_tokens'] or 0, reverse=True)


def main():
    path = Path('/home/wang/.hermes/runtime/role-usage.db')
    with sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=1) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        rows = []
        for raw in db.execute('SELECT * FROM role_usage WHERE started_at>=? AND started_at<?', (START, END)):
            row = dict(raw)
            row['known'], _ = known_usage(row)
            row['day'] = datetime.fromtimestamp(row['started_at'], ZONE).strftime('%Y-%m-%d')
            row['model_label'] = Path(row['model']).name
            rows.append(row)
        firsts = dict(db.execute("SELECT session_id,min(started_at) FROM role_usage WHERE task_kind='business_cron' GROUP BY session_id"))
    windows = {
        'pre_three_full_days': (START, epoch('2026-10-30T00:00:00+08:00')),
        'pre_oct29_full': (epoch('2026-10-29T00:00:00+08:00'), epoch('2026-10-30T00:00:00+08:00')),
        'post_oct31_to_now': (epoch('2026-10-31T00:00:00+08:00'), END),
        'post_nov01_full': (epoch('2026-11-01T00:00:00+08:00'), epoch('2026-11-02T00:00:00+08:00')),
        'last_24h': (END-86400, END),
    }
    result = {'collected_at': stamp(END), 'source': str(path),
              'semantics': '角色会话物理请求，按started_at归窗；缓存是prompt子集；缺字段保留null；独立审查另采，不包含于此。',
              'comparison_limits': '前后任务、角色阶段、版本和时间窗长度不同，非因果实验；缺失用量不补零；所有结果非费用估算。',
              'windows': {}}
    for name, (left, right) in windows.items():
        selected = [r for r in rows if left <= r['started_at'] < right]
        business = [r for r in selected if r['task_kind'] == 'business_cron']
        local = [r for r in business if r['route'] == 'local']
        result['windows'][name] = {
            'start': stamp(left), 'end': stamp(right), 'hours': (right-left)/3600,
            'all': aggregate(selected), 'by_role': group(selected, ['role']),
            'by_kind': group(selected, ['task_kind']), 'by_model_route': group(selected, ['model_label', 'route']),
            'business_by_role': group(business, ['role']),
            'local_business_by_role': group(local, ['role']),
            'local_business_first_request_by_role': group([r for r in local if r['started_at'] == firsts.get(r['session_id'])], ['role']),
            'local_business_continuation_by_role': group([r for r in local if r['started_at'] != firsts.get(r['session_id'])], ['role'])}
    result['daily'] = group(rows, ['day'])
    post = [r for r in rows if r['started_at'] >= windows['post_oct31_to_now'][0]]
    result['daily_local_business_by_role'] = group([r for r in rows if r['task_kind']=='business_cron' and r['route']=='local'], ['day', 'role'])
    result['top_work_costs_post'] = group([r for r in post if r['task_kind']=='business_cron' and r['work_id']], ['run_id','work_id','role'])[:12]
    result['top_execution_costs_post'] = group([r for r in post if r['task_kind']=='business_cron' and r['execution_id']], ['execution_id','role'])[:10]
    (OUT/'token-usage-current.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    brief = {}
    for name, window in result['windows'].items():
        brief[name] = {'hours': window['hours'], 'all': window['all'],
                       'roles': [{k:r[k] for k in ('role','physical_requests','known_token_sums','prompt_per_request','field_coverage_calls')} for r in window['by_role']]}
    print(json.dumps({'collected_at':result['collected_at'],'windows':brief}, ensure_ascii=False))


if __name__ == '__main__':
    main()
