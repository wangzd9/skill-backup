#!/usr/bin/env python3
"""Read actual OCR findings without model calls, mutations, or gate adjudication.

Coverage proves which files were reviewed; it never proves zero findings. A ready
result requires a frozen run and matching state, session, diagnosis and JSON log.
Missing evidence produces an unknown total, retaining any observed findings.
"""

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re


SEVERITIES = ('critical', 'high', 'medium', 'low', 'info')


class EvidenceError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise EvidenceError(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def execution_spec(spec, entry):
    """Dynamic runs bind each batch to an explicitly allowed, recorded route."""
    policy = spec.get('model_routing')
    if not policy:
        return spec
    route = entry.get('execution_route')
    require(isinstance(route, dict), 'BATCH_EXECUTION_ROUTE_MISSING')
    identity = {key: route.get(key) for key in ('provider', 'model', 'endpoint')}
    require(all(isinstance(v, str) and v for v in identity.values()), 'BATCH_EXECUTION_ROUTE_INVALID')
    require(identity in policy['allowed_routes'], 'BATCH_EXECUTION_ROUTE_NOT_ALLOWED')
    return {**spec, **identity}


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value, 'DUPLICATE_JSON_KEY')
        value[key] = item
    return value


def parse_json(text):
    return json.loads(text, object_pairs_hook=unique_object)


class Evidence:
    def __init__(self):
        self.files = {}
        self.specs = {}

    def raw(self, path):
        path = path.resolve()
        if path not in self.files:
            self.files[path] = path.read_bytes()
        return self.files[path]

    def text(self, path):
        return self.raw(path).decode('utf-8')

    def json(self, path):
        value = parse_json(self.text(path))
        require(isinstance(value, dict), 'JSON_OBJECT_REQUIRED')
        return value

    def spec(self, directory):
        directory = directory.resolve()
        if directory not in self.specs:
            raw = self.raw(directory / 'spec.json')
            require(hashlib.sha256(raw).hexdigest() == self.text(directory / 'spec.sha256').strip(),
                    'SPEC_HASH_MISMATCH')
            spec = self.json(directory / 'spec.json')
            require(spec.get('schema') == 'coordinator.ocr-run/v1', 'SPEC_SCHEMA_UNSUPPORTED')
            require(all(isinstance(spec.get(key), str) and spec[key] for key in
                        ('run_id', 'review_identity', 'repo', 'base', 'head', 'provider', 'model',
                         'ocr_version')), 'SPEC_IDENTITY_MISSING')
            require(isinstance(spec.get('artifacts'), dict), 'ARTIFACTS_REQUIRED')
            for name, expected in spec['artifacts'].items():
                require(isinstance(name, str) and Path(name).name == name, 'ARTIFACT_PATH_INVALID')
                require(hashlib.sha256(self.raw(directory / name)).hexdigest() == expected,
                        'ARTIFACT_HASH_MISMATCH')
            batches = spec.get('batches')
            require(isinstance(batches, list) and batches, 'BATCHES_REQUIRED')
            require(all(isinstance(batch, dict) and re.fullmatch(r'b\d+', batch.get('id', ''))
                        and isinstance(batch.get('files'), list) and batch['files']
                        and all(isinstance(p, str) and p for p in batch['files']) for batch in batches),
                    'BATCH_SCHEMA_INVALID')
            require(len({b['id'] for b in batches}) == len(batches), 'DUPLICATE_BATCH_ID')
            paths = [p for b in batches for p in b['files']]
            require(len(paths) == len(set(paths)), 'OVERLAPPING_BATCH_FILES')
            self.specs[directory] = spec
        return self.specs[directory]

    def sources(self):
        return [{'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}
                for path, raw in sorted(self.files.items())]


def matching_entry(state, batch):
    entries = state.get('batches')
    require(isinstance(entries, list), 'STATE_BATCHES_MISSING')
    matches = [entry for entry in entries if isinstance(entry, dict) and entry.get('id') == batch['id']]
    require(len(matches) == 1, 'STATE_BATCH_MISSING_OR_DUPLICATE')
    entry = matches[0]
    require(entry.get('files') == batch['files'], 'STATE_FILES_MISMATCH')
    require(isinstance(entry.get('session_file'), str) and entry['session_file'], 'SESSION_FILE_MISSING')
    return entry


def coverage(manifest, spec, batch, sid):
    require(isinstance(manifest, dict) and manifest.get('schema_version') == 'ocr.run-manifest/v1',
            'MANIFEST_SCHEMA_UNSUPPORTED')
    require(manifest.get('run_id') == sid and manifest.get('operation') == 'review',
            'MANIFEST_SESSION_MISMATCH')
    require(manifest.get('terminal_state') == 'complete', 'MANIFEST_INCOMPLETE')
    identity = manifest.get('input', {})
    require(identity.get('resolved_base') == spec['base'] and identity.get('resolved_head') == spec['head'],
            'COMMIT_IDENTITY_MISMATCH')
    execution = manifest.get('execution', {})
    version = re.search(r'\bv\d+\.\d+\.\d+\b', spec['ocr_version'])
    require(version and version.group() == 'v1.13.0', 'OCR_VERSION_UNSUPPORTED')
    require(execution.get('provider') == spec['provider'] and execution.get('model') == spec['model']
            and execution.get('ocr_version') == version.group(), 'EXECUTION_IDENTITY_MISMATCH')
    cov = manifest.get('coverage', {})
    require(isinstance(cov, dict) and all(isinstance(cov.get(key), list) for key in
            ('selected', 'completed', 'reused', 'failed', 'waived')), 'COVERAGE_SCHEMA_INVALID')
    require(not cov['failed'] and not cov['waived'], 'COVERAGE_FAILED_OR_WAIVED')
    groups = {}
    for key in ('selected', 'completed', 'reused'):
        group = cov[key]
        require(all(isinstance(i, dict) and all(isinstance(i.get(k), str) and i[k]
                    for k in ('item_id', 'path', 'fingerprint')) for i in group), 'ITEM_IDENTITY_INVALID')
        identities = [(i['item_id'], i['path'], i['fingerprint']) for i in group]
        require(len(identities) == len(set(identities)), 'DUPLICATE_COVERAGE_ITEM')
        groups[key] = set(identities)
    require(sorted(i['path'] for i in cov['selected']) == sorted(batch['files']), 'SELECTED_FILES_MISMATCH')
    require(not (groups['completed'] & groups['reused']) and
            groups['completed'] | groups['reused'] == groups['selected'], 'COVERAGE_INCOMPLETE')
    return cov


def validate_comments(value, files):
    require(isinstance(value, list), 'COMMENTS_ARRAY_REQUIRED')
    for comment in value:
        require(isinstance(comment, dict) and comment.get('path') in files
                and isinstance(comment.get('content'), str) and comment['content'].strip()
                and comment.get('severity') in SEVERITIES, 'COMMENT_SCHEMA_UNSUPPORTED')
        for field in ('start_line', 'end_line'):
            if field in comment:
                require(type(comment[field]) is int and comment[field] > 0, 'COMMENT_LINE_INVALID')
        if 'start_line' in comment and 'end_line' in comment:
            require(comment['start_line'] <= comment['end_line'], 'COMMENT_RANGE_INVALID')
    return value


def session_evidence(evidence, path, spec, batch, comments):
    sid = path.stem
    starts, ends, done = [], [], []
    # Discard prompts/tool payloads immediately; return only actual final comments.
    last_type = None
    for line in evidence.text(path).splitlines():
        if not line.strip():
            continue
        event = parse_json(line)
        require(isinstance(event, dict) and event.get('sessionId') == sid, 'SESSION_IDENTITY_MISMATCH')
        last_type = event.get('type')
        if last_type == 'session_start':
            starts.append(event)
        elif last_type == 'session_end':
            ends.append(event)
        elif last_type == 'review_item_done':
            done.append(event)
            comments.extend(validate_comments(event.get('comments'), batch['files']))
    require(len(starts) == len(ends) == 1 and last_type == 'session_end', 'SESSION_TERMINAL_REQUIRED')
    start = starts[0]
    require(Path(start.get('cwd', '')).resolve() == Path(spec['repo']).resolve()
            and start.get('diffFrom') == spec['base'] and start.get('diffTo') == spec['head'],
            'SESSION_START_IDENTITY_MISMATCH')
    manifest = ends[0].get('run_manifest')
    cov = coverage(manifest, spec, batch, sid)
    require(sorted((d.get('filePath'), d.get('fingerprint')) for d in done) ==
            sorted((i['path'], i['fingerprint']) for i in cov['completed'] + cov['reused']),
            'SESSION_REVIEW_ITEMS_MISMATCH')
    return sid, manifest, comments


def log_evidence(evidence, path):
    text = evidence.text(path)
    start = re.search(r'(?m)^[ \t]*\{', text)
    require(start is not None, 'RESULT_JSON_MISSING')
    require(all(not line.strip() or line.startswith('[ocr]') for line in text[:start.start()].splitlines()),
            'RESULT_PREAMBLE_UNSUPPORTED')
    decoder = json.JSONDecoder(object_pairs_hook=unique_object)
    begin = start.start()
    while text[begin].isspace():
        begin += 1
    value, end = decoder.raw_decode(text, begin)
    require(all(not line.strip() or line.startswith('[ocr]') for line in text[end:].splitlines()),
            'RESULT_TRAILER_OR_DUPLICATE')
    require(isinstance(value, dict), 'RESULT_OBJECT_REQUIRED')
    return value


def log_source(evidence, directory, spec, batch, entry, seen=None):
    if not entry.get('reused'):
        require(not batch.get('reuse_session'), 'REUSE_STATE_MISMATCH')
        return directory, batch
    require(batch.get('reuse_session') == entry['session_file'], 'REUSE_SESSION_MISMATCH')
    previous = spec.get('reuse_run')
    require(isinstance(previous, str) and previous, 'REUSE_SOURCE_MISSING')
    seen = (seen or set()) | {directory.resolve()}
    previous = Path(previous).resolve()
    require(previous not in seen, 'REUSE_CYCLE')
    previous_spec = evidence.spec(previous)
    require(all(previous_spec.get(key) == spec.get(key) for key in
                ('review_identity', 'repo', 'base', 'head', 'provider', 'model')), 'REUSE_IDENTITY_MISMATCH')
    previous_state = evidence.json(previous / 'state.json')
    require(previous_state.get('run_id') == previous_spec['run_id'], 'REUSE_RUN_ID_MISMATCH')
    candidates = [b for b in previous_spec['batches'] if b['files'] == batch['files']]
    require(len(candidates) == 1, 'REUSE_BATCH_MISSING_OR_DUPLICATE')
    old_batch = candidates[0]
    old_entry = matching_entry(previous_state, old_batch)
    require(old_entry.get('state') == 'REVIEW_COMPLETE' and old_entry.get('returncode') == 0
            and old_entry['session_file'] == entry['session_file'], 'REUSE_BATCH_NOT_COMPLETE')
    return log_source(evidence, previous, previous_spec, old_batch, old_entry, seen)


def check_batch(evidence, directory, spec, state, batch):
    result = {'id': batch['id'], 'files': batch['files'], 'coverage_complete': False,
              'evidence_state': 'INSUFFICIENT_EVIDENCE', 'errors': [], 'findings': []}
    comments = result['findings']
    try:
        entry = matching_entry(state, batch)
        effective = execution_spec(spec, entry)
        if 'execution_route' in entry:
            result['execution_route'] = entry['execution_route']
        session = Path(entry['session_file']).resolve()
        result['session_file'] = str(session)
        sid, manifest, comments = session_evidence(evidence, session, effective, batch, comments)
        result['coverage_complete'] = True
        # Preserve actual findings even if a later log/diagnosis check fails.
        result['findings'] = comments
        require(entry.get('state') == 'REVIEW_COMPLETE' and type(entry.get('returncode')) is int
                and entry['returncode'] == 0, 'BATCH_NOT_COMPLETE')
        diagnosis_path = directory / (batch['id'] + '-diagnosis.json')
        diagnosis = evidence.json(diagnosis_path)
        require(Path(diagnosis.get('session_file', '')).resolve() == session
                and diagnosis.get('session_ids') == [sid] and diagnosis.get('coverage_complete') is True
                and diagnosis.get('batch_identity_matches') is True and diagnosis.get('parse_errors') == []
                and diagnosis.get('manifest_schema') == manifest['schema_version']
                and diagnosis.get('terminal_state') == manifest['terminal_state']
                and diagnosis.get('input') == manifest['input']
                and diagnosis.get('coverage_counts') == {k: len(v) for k, v in manifest['coverage'].items()},
                'DIAGNOSIS_IDENTITY_OR_COVERAGE_MISMATCH')
        source, source_batch = log_source(evidence, directory, spec, batch, entry)
        result.update(source_run=str(source), log_file=str(source / (source_batch['id'] + '.log')),
                      diagnosis_file=str(diagnosis_path))
        output = log_evidence(evidence, Path(result['log_file']))
        require(output.get('status') == 'complete', 'RESULT_NOT_COMPLETE')
        require(output.get('session_id') == sid and output.get('manifest') == manifest,
                'RESULT_SESSION_OR_MANIFEST_MISMATCH')
        require(output.get('llm') == {'provider': effective['provider'], 'model': effective['model']},
                'RESULT_PROVIDER_OR_MODEL_MISMATCH')
        log_comments = validate_comments(output.get('comments'), batch['files'])
        session_counts = Counter(canonical(c) for c in comments)
        log_counts = Counter(canonical(c) for c in log_comments)
        if session_counts != log_counts:
            # A disagreement must not hide a blocker present in either source.
            remaining = log_counts - session_counts
            for comment in log_comments:
                key = canonical(comment)
                if remaining[key]:
                    comments.append(comment)
                    remaining[key] -= 1
            raise EvidenceError('RESULT_SESSION_FINDINGS_MISMATCH')
        summary = output.get('summary', {})
        require(type(summary.get('comments')) is int and summary['comments'] == len(log_comments)
                and type(summary.get('files_reviewed')) is int
                and summary['files_reviewed'] == len(batch['files']), 'RESULT_SUMMARY_MISMATCH')
        result['evidence_state'] = 'READY_FOR_ADJUDICATION'
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        result['errors'].append(str(exc) if isinstance(exc, EvidenceError) else type(exc).__name__)
    return result


def inspect_results(directory):
    """Return frozen-snapshot evidence; never writes files or emits PASS."""
    directory = Path(directory).resolve()
    evidence = Evidence()
    result = {'schema': 'coordinator.ocr-results/v1', 'run': str(directory),
              'inspected_at': datetime.now(timezone.utc).isoformat(),
              'evidence_state': 'INSUFFICIENT_EVIDENCE', 'coverage_complete': False,
              'requires_adjudication': True, 'gate_decision': 'NOT_EVALUATED',
              'errors': [], 'batches': [], 'findings': []}
    try:
        spec = evidence.spec(directory)
        result.update({key: spec.get(key) for key in
                       ('run_id', 'review_identity', 'task', 'attempt', 'base', 'head')})
        state = evidence.json(directory / 'state.json')
        require(state.get('run_id') == spec['run_id'], 'STATE_RUN_ID_MISMATCH')
        result['recorded_state'] = state.get('state')
        require(isinstance(state.get('batches'), list), 'STATE_BATCHES_MISSING')
        ids = [b.get('id') for b in state['batches'] if isinstance(b, dict)]
        if len(ids) != len(state['batches']) or len(ids) != len(set(ids)) or set(ids) != {
                batch['id'] for batch in spec['batches']}:
            result['errors'].append('STATE_BATCH_SET_MISMATCH')
        if state.get('state') != 'AWAITING_DECISION':
            result['errors'].append('RUN_NOT_AWAITING_DECISION')
        for batch in spec['batches']:
            inspected = check_batch(evidence, directory, spec, state, batch)
            result['batches'].append(inspected)
            for index, finding in enumerate(inspected.pop('findings'), 1):
                result['findings'].append(dict(finding, finding_id=f"{batch['id']}-{index:03}",
                                              batch_id=batch['id'],
                                              session_file=inspected.get('session_file'),
                                              log_file=inspected.get('log_file')))
        result['coverage_complete'] = all(b['coverage_complete'] for b in result['batches'])
        if not result['errors'] and all(b['evidence_state'] == 'READY_FOR_ADJUDICATION'
                                       for b in result['batches']):
            result['evidence_state'] = 'READY_FOR_ADJUDICATION'
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        result['errors'].append(str(exc) if isinstance(exc, EvidenceError) else type(exc).__name__)
    ready = result['evidence_state'] == 'READY_FOR_ADJUDICATION'
    counts = Counter(finding['severity'] for finding in result['findings'])
    observed = {severity: counts[severity] for severity in SEVERITIES}
    result['finding_counts'] = {'total': len(result['findings']) if ready else None,
                                'severity': observed if ready else None,
                                'observed_total': len(result['findings']), 'observed_severity': observed}
    result['sources'] = evidence.sources()
    result['next_action'] = ('逐项裁决真实问题并核对测试门禁；覆盖完整不代表无问题或可接受。' if ready else
                             '补齐或核对缺失证据；问题总数未知，保持接受门禁关闭，不自动重跑OCR。')
    return result
