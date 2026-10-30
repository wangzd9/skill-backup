"""纯 GET 指标快照，不发推理请求；分析问题留给模型。"""
from datetime import datetime
import math
import re
import time
import urllib.request
from runtime_common import locked, now, read_json, save_json

QUERIES=frozenset(['当前大模型负载情况','当前大模型负载','查看大模型负载','查看千问负载',
                  '当前千问负载','千问负载情况','大模型负载情况','模型负载怎么样','/llm-load'])


def load_query(text):
    return text.strip().rstrip('？?。！!') in QUERIES


def parse_metrics(body):
    values={}
    for line in body.splitlines():
        m=re.match(r'^(vllm:[a-zA-Z0-9_:]+)(?:\{.*\})?\s+([-+\d.eE]+)(?:\s|$)',line)
        if not m: continue
        value=float(m[2])
        if math.isfinite(value): values.setdefault(m[1].split(':',1)[1],[]).append(value)
    def total(name):
        return sum(values[name]) if name in values else None
    return {'running':total('num_requests_running'),'waiting':total('num_requests_waiting'),
            'kv_percent':100*max(values['kv_cache_usage_perc']) if 'kv_cache_usage_perc' in values else None,
            'completed':total('request_success_total'),'generated':total('generation_tokens_total'),
            'prompt':total('prompt_tokens_total'),'cached':total('prompt_tokens_cached_total'),
            'preemptions':total('num_preemptions_total')}


def collect(url, cache_path):
    # 本地部署的直连 GET；不受 shell 中的中转代理变量影响。
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url,timeout=5) as response:
        body=response.read(2_000_000).decode()
    current={'observed_at':now(),'epoch':time.time(),'endpoint':url,**parse_metrics(body)}
    if current['running'] is None or current['waiting'] is None:
        raise ValueError('指标缺少运行/等待数，不能伪装为空闲')
    with locked(str(cache_path)+'.lock'):
        previous=read_json(cache_path,{}) or {}
        seconds=current['epoch']-previous.get('epoch',current['epoch'])
        if 2<=seconds<=300 and previous.get('endpoint')==url:
            deltas={}
            for name in ('completed','generated','prompt','cached'):
                a,b=current.get(name),previous.get(name)
                if a is not None and b is not None and a>=b: deltas[name]=a-b
            current['interval']={'seconds':seconds,**deltas}
        save_json(cache_path,current)
    return current


def render(snapshot):
    show=lambda v:'未知' if v is None else f'{v:g}'
    text=[f"**千问负载｜{snapshot['observed_at']}**",
          f"- **运行 / 等待：** {show(snapshot.get('running'))} / {show(snapshot.get('waiting'))}",
          f"- **KV 使用率：** {show(snapshot.get('kv_percent'))}%"]
    interval=snapshot.get('interval',{})
    if 'generated' in interval:
        text.append(f"- **区间生成吞吐：** {interval['generated']/interval['seconds']:.1f} token/s；间隔 {interval['seconds']:.1f} 秒")
    else: text.append('- **生成吞吐：** 暂无同端点短时差分，不用累计量冒充速率')
    text.append('- **来源：** 服务端指标；本次未调用模型。需要原因分析时请明确提出。')
    return '\n'.join(text)
