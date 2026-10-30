"""Include changed test sources in required OCR while preserving explicit filters."""
import hashlib
import json
from pathlib import Path


def freeze(repo, initial_preview, parent):
    if any(row.get('will_review') is True for row in initial_preview['files']):return None
    tests=[]
    for row in initial_preview['files']:
        path=Path(row['path']);name=path.name.lower()
        is_test=(name.startswith('test_') or name.endswith(('_test.go','_test.py','_spec.rb')) or
            any(mark in name for mark in ('.test.','.spec.')) or 'tests' in path.parts or 'test' in path.parts)
        if row.get('exclude_reason')=='default_path' and is_test and path.suffix.lower() in (
                '.py','.go','.rb','.js','.jsx','.ts','.tsx','.java','.kt','.rs','.cpp','.c','.cs'):
            if any(c in str(path) for c in ',\n*?[]{}!') or path.is_absolute() or '..' in path.parts:
                raise ValueError('OCR_TEST_SELECTION_PATH_UNVERIFIED')
            tests.append(str(path))
    if not tests:return None
    # OCR selects the first layer containing a filter. Preserve that filter
    # verbatim; custom rules are empty so existing rule-text layers still apply.
    selected={};sources=[]
    for path in (Path(repo)/'.opencodereview/rule.json',Path.home()/'.opencodereview/rule.json'):
        if not path.exists():continue
        raw=path.read_bytes();value=json.loads(raw)
        sources.append({'path':str(path),'sha256':hashlib.sha256(raw).hexdigest()})
        if not selected and (value.get('include') or value.get('exclude')):selected=value
    rules={'include':list(dict.fromkeys([*selected.get('include',[]),*tests])),
        'exclude':selected.get('exclude',[]),'rules':[]}
    raw=json.dumps(rules,ensure_ascii=False,sort_keys=True).encode();sha=hashlib.sha256(raw).hexdigest()
    path=Path(parent)/('ocr-test-selection-'+sha+'.json');path.parent.mkdir(parents=True,exist_ok=True)
    if not path.exists():
        with path.open('xb') as stream:stream.write(raw)
        path.chmod(0o400)
    if path.is_symlink() or path.read_bytes()!=raw:raise ValueError('OCR_TEST_SELECTION_CHANGED')
    return {'path':str(path.resolve()),'sha256':sha,'test_files':tests,'filter_sources':sources,
        'rule_policy':'仅精确纳入默认排除的测试源码；保留原显式过滤、项目/全局规则与系统语言规则。'}
