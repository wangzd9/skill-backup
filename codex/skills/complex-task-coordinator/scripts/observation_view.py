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
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != reference['sha256']:
            raise ValueError('AUDIT_EVENT_VIEW_HASH_MISMATCH')
    if size(observation) < 8192:
        return observation

    from workflow_runtime import verified_input_view
    reference = verified_input_view(directory, 'health', observation, pretty=True)
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
        omitted_fields=sorted(set(observation)-set(result)), omitted_counts={})
    # Category cardinality and text can also be unbounded. Preserve exact
    # denominators plus a reference, and never turn omitted UNKNOWN into zero.
    fields = ('reason_codes', 'anomaly_types', 'source_state_counts', 'write_path_states',
              'omitted_fields')
    while size(result) >= 8192:
        field = max(fields, key=lambda key: size(result[key]))
        if not result[field]:
            raise ValueError('OBSERVATION_METADATA_EXCEEDS_BYTE_BUDGET')
        if isinstance(result[field], dict):
            result[field].pop(next(reversed(result[field])))
        else:
            result[field].pop()
        result['omitted_counts'][field] = result['omitted_counts'].get(field, 0) + 1
    return result
