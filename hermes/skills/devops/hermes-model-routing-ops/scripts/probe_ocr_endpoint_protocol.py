#!/usr/bin/env python3
"""探测云端 LLM 端点对非流式请求的 content-type，判断 OCR 能否使用该端点。

OCR 的 Go 客户端按 application/json 解析非流式响应；端点若对 stream:false
仍返回 text/event-stream，OCR 会以 "expected destination type of 'string' or
'[]byte' for responses with content-type 'text/event-stream'" 失败。

用法：python3 probe_ocr_endpoint_protocol.py <provider>
provider 取 ~/.opencodereview/config.json 的 custom_providers 键名（如 hermes-xcoding）。
只读探测；密钥经配置的 api_key_cmd 解析，不打印。
"""
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

CONFIG = Path.home() / '.opencodereview/config.json'


def resolve_key(command):
    if not command:
        return ''
    result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=60)
    return result.stdout.strip() if result.returncode == 0 else ''


def probe(item):
    endpoint = item['url'].rstrip('/')
    responses = item.get('protocol', 'openai') == 'openai-responses'
    path = '/responses' if responses else '/chat/completions'
    body = {'model': item['model'], 'stream': False}
    if responses:
        body['input'] = 'say OK'
    else:
        body['messages'] = [{'role': 'user', 'content': 'say OK'}]
    headers = {'Content-Type': 'application/json'}
    key = resolve_key(item.get('api_key_cmd', '')) or item.get('api_key', '')
    if key:
        headers['Authorization'] = 'Bearer ' + key
    request = urllib.request.Request(endpoint + path, data=json.dumps(body).encode(),
                                     headers=headers, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return path, response.status, response.headers.get('content-type', ''), response.read(200)
    except urllib.error.HTTPError as exc:
        return path, exc.code, exc.headers.get('content-type', ''), exc.read(200)
    except Exception as exc:  # 网络/URL 层失败也算不可用，不抛栈
        return path, 0, '', str(exc).encode()


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    name = sys.argv[1]
    providers = json.loads(CONFIG.read_text())['custom_providers']
    item = providers.get(name)
    if not item:
        print('未知 provider %s；可用：%s' % (name, ', '.join(providers)))
        return 2
    path, status, content_type, raw = probe(item)
    print('provider  :', name)
    print('url       :', item['url'].rstrip('/') + path)
    print('model     :', item['model'])
    print('status    :', status)
    print('ctype     :', content_type)
    print('body[:200]:', raw[:200].decode('utf-8', 'replace').replace('\n', ' '))
    if status == 200 and 'application/json' in content_type:
        print('VERDICT   : OK —— 非流式返回 application/json，OCR 可用')
    elif 'text/event-stream' in content_type:
        print('VERDICT   : UNUSABLE —— 非流式仍返回 SSE，OCR Go 客户端无法解析')
    else:
        print('VERDICT   : UNKNOWN —— 先看 status/body 再判断')
    return 0


if __name__ == '__main__':
    sys.exit(main())
