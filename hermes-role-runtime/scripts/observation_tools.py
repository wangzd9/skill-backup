#!/usr/bin/env python3
"""Local observation inspection, regression and explicit human feedback.

No model, messages, business ACK, source cutover or authority changes.
"""
import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workflow_observation import digest, rule_registry, saved_health
from workflow_replay import run_corpus
from activity_signals import record_feedback, feedback_summary


def monitor_feedback(home, pool, start, end, *, rule_version=None):
    base = Path(home)/'cache/role-lease-monitor'/digest(str(Path(pool).resolve()))
    anomalies = set()
    for path in sorted((base/'outbox').glob('*.json')):
        event = json.loads(path.read_text())['event']
        if start <= event['generated_at'] < end and (rule_version is None or event.get('rule_version') == rule_version):
            anomalies.add(event['episode'])
    return feedback_summary(base/'feedback.sqlite3', start, end, anomalies=anomalies, rule_version=rule_version)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['rules', 'replay', 'health', 'dispatch-summary', 'feedback', 'feedback-summary'])
    parser.add_argument('--home', type=Path, default=Path.home()/'.hermes')
    parser.add_argument('--pool', type=Path)
    parser.add_argument('--input', type=Path, help='explicit feedback JSON: anomaly_id=notification episode, rule_version/label/reviewer/at/basis/scope')
    parser.add_argument('--start', type=float)
    parser.add_argument('--end', type=float)
    parser.add_argument('--rule-version')
    args = parser.parse_args()
    if args.action == 'rules':
        result = rule_registry()
    elif args.action == 'replay':
        result = run_corpus(Path(__file__).resolve().parents[1]/'fixtures/observation')
    else:
        if not args.pool:
            parser.error('--pool is required')
        if args.action == 'health':
            result = saved_health(args.home, args.pool, now=time.time())
        elif args.action == 'dispatch-summary':
            from workflow_dispatch import receipts
            from workflow_measurements import summarize_dispatch
            result = summarize_dispatch(receipts(args.home, args.pool), now=time.time())
        elif args.action == 'feedback':
            if not args.input:
                parser.error('--input is required; receiving a notification is not a feedback judgment')
            fields = json.loads(args.input.read_text())
            base = args.home/'cache/role-lease-monitor'/digest(str(args.pool.resolve()))
            result = record_feedback(base/'feedback.sqlite3', **fields)
        else:
            if args.start is None or args.end is None:
                parser.error('--start and --end are required Unix timestamps')
            result = monitor_feedback(args.home, args.pool, args.start, args.end, rule_version=args.rule_version)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
