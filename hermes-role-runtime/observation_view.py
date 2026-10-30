"""Bound observer query views at the source; preserve complete evidence by hash."""
from collections import Counter
import hashlib
import json
from pathlib import Path


def health_view(observation, directory):
    """Return <8 KiB, with exact totals and explicit omissions, never recovery."""
    def size(value):
        return len(json.dumps(value, ensure_ascii=False).encode())

    reference = observation.get('full_observation')
    if observation.get('projection') == 'observation-health-reference-v2' and reference:
        path = Path(reference['path'])
        raw = path.read_bytes()
        if path.is_symlink() or hashlib.sha256(raw).hexdigest() != reference['sha256']:
            raise ValueError('AUDIT_EVENT_VIEW_HASH_MISMATCH')
        if size(observation) >= 8192:
            # Reproject the original evidence, never summarize a summary and
            # accidentally reset its anomaly totals to zero.
            return health_view(json.loads(raw), directory)
    if size(observation) < 8192:
        return observation

    from workflow_runtime import verified_input_view
    reference = verified_input_view(directory, 'health', observation, pretty=True)

    def metadata_reference(value, pointer):
        result = {'projection': 'observation-metadata-reference-v1',
                  'detail_source': 'full_observation', 'json_pointer': pointer,
                  'value_type': ('object' if isinstance(value, dict) else
                                 'array' if isinstance(value, list) else 'string')}
        if isinstance(value, (dict, list, str)):
            result['item_count'] = len(value)
        return result

    def compact_metadata(value, pointer, budget):
        if size(value) <= budget:
            return value
        if not isinstance(value, dict):
            return metadata_reference(value, pointer)
        # Keep small aggregate counts/statuses visible; large samples or future
        # nested metadata remain reachable in the same verified original.
        result = {}
        pointers = {}
        for key, child in value.items():
            child_pointer = pointer + '/' + str(key).replace('~', '~0').replace('/', '~1')
            pointers[key] = child_pointer
            result[key] = compact_metadata(child, child_pointer, min(budget, 1536))
        while size(result) > budget:
            replacements = {key: metadata_reference(value[key], pointers[key]) for key in result}
            savings = {key: size(result[key]) - size(replacements[key]) for key in result}
            key = max(savings, key=savings.get) if savings else None
            if key is None or savings[key] <= 0:
                return metadata_reference(value, pointer)
            result[key] = replacements[key]
        return result
    anomalies = observation.get('anomalies', [])
    sources = observation.get('source_states', {})
    diagnostics = observation.get('write_diagnostics') or {}
    result = {key: observation[key] for key in ('state', 'source', 'checked_at',
        'observed_at', 'last_verified_at', 'age_seconds', 'error') if key in observation}
    result.update(projection='observation-health-reference-v2',
        authority='保存的观察证据；不证明当前业务门禁、恢复、授权或测试能力。',
        anomaly_count=len(anomalies), source_count=len(sources),
        anomaly_types=dict(Counter(row.get('type', 'UNKNOWN') for row in anomalies)),
        source_state_counts=dict(Counter(row.get('state', 'UNKNOWN') for row in sources.values())),
        reason_codes=list(observation.get('reason_codes', [])),
        reason_code_count=len(observation.get('reason_codes', [])),
        write_path_count=len(diagnostics.get('write_paths', [])),
        write_path_states=dict(Counter(row.get('write_path_state', 'UNKNOWN')
                                      for row in diagnostics.get('write_paths', []))),
        full_observation=reference,
        health_layers=compact_metadata(observation.get('health_layers'), '/health_layers', 4096),
        activity_policy=compact_metadata(observation.get('activity_policy'), '/activity_policy', 1024),
        metadata_policy='引用字段须校验full_observation后按json_pointer读取；item_count为精确项数，不代表健康或已恢复。',
        omitted_fields=sorted(set(observation)-set(result)), omitted_counts={})
    # Category cardinality and text can also be unbounded. Preserve exact
    # denominators plus a reference, and never turn omitted UNKNOWN into zero.
    fields = ('reason_codes', 'anomaly_types', 'source_state_counts', 'write_path_states',
              'omitted_fields')
    while size(result) >= 8192:
        field = max(fields, key=lambda key: size(result[key]))
        if not result[field]:
            # Provenance/error text and combinations of nested metadata must
            # obey the envelope too. Offload whole values, never label unknown
            # health as zero or swallow an evidence-integrity failure.
            candidates = {key: metadata_reference(observation[key], '/' + key)
                          for key in ('health_layers', 'activity_policy', 'source', 'error')
                          if key in observation}
            savings = {key: size(result[key]) - size(value) for key, value in candidates.items()}
            key = max(savings, key=savings.get) if savings else None
            if key is None or savings[key] <= 0:
                raise ValueError('OBSERVATION_METADATA_EXCEEDS_BYTE_BUDGET')
            result[key] = candidates[key]
            continue
        if isinstance(result[field], dict):
            result[field].pop(next(reversed(result[field])))
        else:
            result[field].pop()
        result['omitted_counts'][field] = result['omitted_counts'].get(field, 0) + 1
    return result
