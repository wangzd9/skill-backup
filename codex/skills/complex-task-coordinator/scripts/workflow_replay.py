"""Deterministic, isolated temporal regression runner. No sender or business API.

Fixtures reference specification judgments, not fabricated historical evidence.
The same pure step is called by the production monitor. Results include the
denominators and differences; a fixture PASS is not a production quality rate.
"""
import argparse
import hashlib
import json
from pathlib import Path
from workflow_observation import RULE_VERSION, rule_registry, step


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def replay(fixture):
    if fixture.get('schema_version') != 1 or fixture.get('rule_version') != RULE_VERSION:
        raise ValueError('REPLAY_SCHEMA_OR_RULE_VERSION_MISMATCH')
    if (not fixture.get('fixture_id') or not fixture.get('basis_refs') or not fixture.get('config_version') or
            fixture.get('label') not in {'BENIGN', 'FAULT', 'MIXED'} or
            not fixture.get('identity') or not isinstance(fixture.get('frames'), list)):
        raise ValueError('REPLAY_PROVENANCE_REQUIRED')
    state, outputs, differences, previous = {}, [], [], float('-inf')
    for index, frame in enumerate(fixture['frames']):
        now = frame['now']
        if now < previous:
            raise ValueError('REPLAY_TIME_ORDER_INVALID')
        previous = now
        state, events = step(state, frame['observation'], now)
        actual = [{'type': row['type'], 'transition': row['transition'],
                   'notification_state': row['notification_state']} for row in events]
        expected = frame['expected_events']
        if actual != expected:
            differences.append({'frame': index, 'field': 'events', 'expected': expected, 'actual': actual})
        for condition in frame.get('expected_reasons', []):
            reasons = {reason for row in state['active'].values() for reason in row.get('reason_codes', [])}
            if condition not in reasons:
                differences.append({'frame': index, 'field': 'reason_codes', 'missing': condition})
        for identity, expected_gate in frame.get('expected_gate_blocks', {}).items():
            actual_gate = state['process_states'].get(identity, {}).get('blocks_new_owner')
            if actual_gate is not expected_gate:
                differences.append({'frame': index, 'field': 'gate:'+identity,
                                    'expected': expected_gate, 'actual': actual_gate})
        outputs.append({'frame': index, 'at': now, 'events': events, 'state': state})
    return {'fixture_id': fixture['fixture_id'], 'label': fixture['label'],
            'basis_refs': fixture['basis_refs'], 'rule_version': RULE_VERSION,
            'config_version': fixture['config_version'], 'identity': fixture['identity'],
            'judgment': fixture.get('judgment'), 'coverage_gaps': fixture.get('coverage_gaps', []),
            'fixture_sha256': hashlib.sha256(canonical(fixture).encode()).hexdigest(),
            'passed': not differences, 'differences': differences, 'outputs': outputs,
            'frames': len(fixture['frames']), 'real_sends': 0, 'business_acks': 0,
            'model_requests': 0, 'production_history_claim': False}


def run_corpus(directory):
    files = sorted(Path(directory).glob('*.json'))
    if not files:
        raise ValueError('EMPTY_REPLAY_CORPUS')
    results = [replay(json.loads(path.read_text())) for path in files]
    from workflow_migration import code_version
    return {'schema_version': 1, 'code_version': code_version(), 'rules': rule_registry(), 'results': results,
            'denominators': {'fixtures': len(results), 'frames': sum(row['frames'] for row in results),
                             'benign_fixtures': sum(row['label'] == 'BENIGN' for row in results),
                             'fault_or_mixed_fixtures': sum(row['label'] != 'BENIGN' for row in results)},
            'passed': all(row['passed'] for row in results),
            'reviewed_production_false_positive_rate': None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, default=Path(__file__).parent/'fixtures/observation')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = run_corpus(args.corpus)
    raw = json.dumps(result, ensure_ascii=False, indent=2)+'\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(raw)
    else:
        print(raw)
    raise SystemExit(0 if result['passed'] else 1)


if __name__ == '__main__':
    main()
