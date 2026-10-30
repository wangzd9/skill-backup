"""Lossless, model-only input de-duplication; never a ledger or ACK projection."""
import json


def _pointer(path, part):
    return path + '/' + str(part).replace('~', '~0').replace('/', '~1')


def deduplicate(packet):
    """Keep gates/identities structured; repeated bodies point inside this packet.

    Anchors are complete current facts, not memory from an earlier session.
    Only equal JSON values are shared. References can be expanded recursively;
    no event is consumed and no result, authorization or phase is inferred.
    """
    from workflow_views import size
    seen = {}
    count = 0

    def visit(value, path, *, replace=False, strings_only=False):
        nonlocal count
        if isinstance(value, (dict, list, str)) and size(value) >= 256:
            encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            previous = seen.get(encoded)
            if replace and previous is not None and previous != path and (
                    not strings_only or isinstance(value, str)):
                count += 1
                return {'same_as': previous}
            seen.setdefault(encoded, path)
        if isinstance(value, dict):
            return {key: visit(child, _pointer(path, key), replace=replace,
                               strings_only=strings_only) for key, child in value.items()}
        if isinstance(value, list):
            return [visit(child, _pointer(path, index), replace=replace,
                          strings_only=strings_only) for index, child in enumerate(value)]
        return value

    # These remain structured for both model decisions and existing consumers.
    for name in ('work', 'task', 'tasks', 'run', 'candidates', 'verifications',
                 'accepted_requirements', 'historical_failures',
                 'local_development_automation', 'next_actions', 'supplemental_verifications'):
        if name in packet:
            visit(packet[name], '/' + name)
    # All phase rows, owners, applicability and predecessor edges stay inline.
    # Repeated long contract text uses an earlier, exact in-packet occurrence.
    for name in ('completion', 'integration_validation', 'plans', 'verification_retirements'):
        if name in packet:
            packet[name] = visit(packet[name], '/' + name, replace=True, strings_only=True)
    for index, event in enumerate(packet.get('events', [])):
        if 'payload' in event:
            event['payload'] = visit(event['payload'], '/events/' + str(index) + '/payload', replace=True)
    return count


def project(ledger, packet):
    from workflow_views import archive, size
    before = size(packet)
    actions = packet.get('external_actions', [])
    terminal = [i for i, row in enumerate(actions)
                if row.get('state') in ('ENDED', 'REJECTED') and size(row.get('authorization')) > 256]
    if terminal:
        # Reuse an existing full archive; do not archive an already thin view.
        if 'external_actions' not in packet.get('detail_artifacts', {}):
            archive(ledger, packet, 'external_actions', actions)
        packet['external_actions'] = [
            {**row, 'authorization': {'detail_source': 'external_actions', 'index': i,
                'field': 'authorization', 'expanded': False}}
            if i in terminal else row for i, row in enumerate(actions)]
    references = deduplicate(packet)
    packet['input_projection'] = {
        'version': 1, 'before_bytes': before, 'after_bytes_without_metrics': size(packet),
        'same_packet_references': references, 'terminal_authorizations_archived': len(terminal),
        'measurement': 'compact UTF-8 JSON bytes; not measured tokens',
        'policy': 'same_as为本包JSON Pointer，可递归展开；detail_source为完整哈希归档，未展开不等于已读。'
                  '保留每条事件及结果、全部阶段义务、FAIL/UNKNOWN和非终态动作授权；只由原工具显式ACK。'}
    return packet
