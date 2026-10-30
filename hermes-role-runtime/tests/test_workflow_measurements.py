import unittest
from workflow_measurements import summarize_dispatch


class DispatchMeasurementsTests(unittest.TestCase):
    def receipt(self, request, stage, at, outcome, **fields):
        return {'request_id': request, 'stage': stage, 'at': at, 'outcome': outcome,
                'pool_path': '/pool/one.json', 'generation': 'g1', 'run_id': 'r1',
                'role': 'COORDINATOR', 'work_id': None, 'job_id': 'native-job',
                'pid': 42, 'process_start': 'start-42', 'boot_id': 'boot-one',
                'monotonic_ns': int(at*1e9), **fields}

    def test_unknown_claim_and_missing_stages_are_not_in_measured_denominator(self):
        rows = [{'request_id': 'one', 'stage': 'commit', 'outcome': 'COMMITTED', 'at': 10},
                {'request_id': 'one', 'stage': 'request_entered', 'outcome': 'ENTERED', 'at': 11},
                {'request_id': 'one', 'stage': 'claim_decided', 'outcome': 'ERROR_OR_UNKNOWN', 'at': 12,
                 'result': {'claimed': True}},
                {'request_id': 'two', 'stage': 'deferred', 'outcome': 'DEFERRED', 'at': 13}]
        result = summarize_dispatch(rows, now=20)
        self.assertEqual(result['metrics']['commit_to_dispatch']['measured'], 1)
        self.assertEqual(result['metrics']['dispatch_to_claim']['measured'], 0)
        self.assertEqual(result['metrics']['commit_to_result_visible']['unknown_or_not_applicable'], 2)
        self.assertEqual(result['performance_gate'], 'TARGETS_NOT_FROZEN')
        self.assertEqual(result['deferred'], 1)

    def test_monotonic_pair_and_outstanding_samples_remain_explicit(self):
        rows = [{'request_id': 'one', 'stage': 'commit', 'outcome': 'COMMITTED', 'at': 10,
                 'pid': 1, 'monotonic_ns': 1_000_000_000},
                {'request_id': 'one', 'stage': 'request_entered', 'outcome': 'ENTERED', 'at': 9,
                 'pid': 1, 'monotonic_ns': 3_000_000_000},
                {'request_id': 'two', 'stage': 'prepared', 'outcome': 'PREPARED', 'at': 5}]
        result = summarize_dispatch(rows, now=20, targets={'commit_to_dispatch': 1})
        self.assertEqual(result['metrics']['commit_to_dispatch']['p95_seconds'], 2)
        self.assertEqual(result['metrics']['commit_to_dispatch']['over_target_count'], 1)
        self.assertEqual(result['outstanding'], 2)
        self.assertEqual(result['requests'][1]['outstanding_age_seconds'], 15)

    def test_new_joined_event_keeps_its_denominator_and_cannot_borrow_old_entry_or_model(self):
        # Mirrors the real native receipt shape: the new event joins the old
        # request after entry, and a later native return does not prove business end.
        rows = [self.receipt('native-one', 'commit', 10, 'COMMITTED', source='tick',
                             source_event_ids=[5], source_event_high_water=5),
                self.receipt('native-one', 'prepared', 10.1, 'PREPARED'),
                self.receipt('native-one', 'request_entered', 11, 'ENTERED'),
                self.receipt('native-one', 'claim_decided', 11.1, 'ERROR_OR_UNKNOWN', result={'claimed': True}),
                self.receipt('native-one', 'commit', 13, 'COMMITTED', source='control',
                             source_event_ids=[6], source_event_high_water=6),
                self.receipt('native-one', 'join', 13.25, 'COALESCED', source='control', source_event_high_water=6),
                self.receipt('native-one', 'agent_started', 14, 'STARTED'),
                self.receipt('native-one', 'result_visible', 15, 'VISIBLE'),
                self.receipt('native-one', 'native_return', 16, 'RETURNED'),
                self.receipt('native-one', 'finish', 17, 'DISPATCH_REQUESTED')]
        result = summarize_dispatch(rows, now=20)
        self.assertEqual(result['metrics']['commit_to_dispatch']['requests'], 1)
        self.assertEqual(result['metrics']['dispatch_to_claim']['measured'], 0)
        self.assertEqual(result['requests'][0]['claim_outcomes'], ['ERROR_OR_UNKNOWN'])
        self.assertEqual(result['requests'][0]['stages']['t_end']['state'], 'UNKNOWN')
        self.assertEqual(result['commit_denominators'], {'commit_receipts': 2, 'commit_members': 2,
                         'events': 2, 'coalesced_members': 1, 'coalesced_events': 1})
        by_event = {member['event_id']: member for member in result['commit_members']}
        self.assertEqual(by_event[5]['durations']['commit_to_dispatch']['seconds'], 1)
        self.assertEqual(by_event[6]['durations']['commit_to_join']['seconds'], .25)
        for metric in ('commit_to_dispatch', 'commit_to_agent_start', 'commit_to_result_visible'):
            self.assertEqual(by_event[6]['durations'][metric]['state'], 'UNKNOWN')
        self.assertEqual(by_event[6]['owner_read']['state'], 'UNKNOWN')
        self.assertEqual(result['event_metrics']['commit_to_dispatch']['samples'], 2)
        self.assertEqual(result['event_metrics']['commit_to_dispatch']['measured'], 1)
        self.assertEqual(result['event_metrics']['commit_to_dispatch']['unknown_or_not_applicable'], 1)
        self.assertEqual(result['event_metrics']['commit_to_join']['measured'], 1)

    def test_pool_event_identity_and_unknown_recipient_are_preserved_separately(self):
        rows = [self.receipt('one', 'commit', 10, 'COMMITTED', source_event_ids=[5]),
                self.receipt('one', 'request_entered', 11, 'ENTERED'),
                self.receipt('two', 'commit', 10, 'COMMITTED', source_event_ids=[5], pool_path='/pool/two.json'),
                self.receipt('three', 'commit', 10, 'COMMITTED', source_event_ids=[5], role='TESTER'),
                self.receipt('three', 'join', 10.5, 'COALESCED', role='TESTER')]
        result = summarize_dispatch(rows, now=20)
        self.assertEqual(result['commit_denominators']['events'], 2)
        self.assertEqual(result['commit_denominators']['commit_members'], 3)
        self.assertEqual(result['commit_member_metrics']['commit_to_dispatch']['measured'], 1)
        # One fast recipient cannot make the whole pool/event appear measured.
        self.assertEqual(result['event_metrics']['commit_to_dispatch']['measured'], 0)
        self.assertEqual(result['event_metrics']['commit_to_dispatch']['unknown_or_not_applicable'], 2)
        self.assertEqual(result['commit_denominators']['coalesced_events'], 1)
        self.assertEqual(result['outstanding'], 3)

    def test_unidentified_commit_receipts_and_ambiguous_join_remain_explicit(self):
        rows = [self.receipt('one', 'commit', 10, 'COMMITTED'),
                self.receipt('one', 'commit', 12, 'COMMITTED'),
                self.receipt('one', 'join', 13, 'COALESCED')]
        result = summarize_dispatch(rows, now=20)
        self.assertEqual(result['commit_denominators']['events'], 2)
        self.assertEqual(result['commit_denominators']['coalesced_members'], 0)
        self.assertEqual([event['unit'] for event in result['events']], ['commit_receipt', 'commit_receipt'])
        self.assertEqual(result['event_metrics']['commit_to_join']['measured'], 0)
        self.assertEqual(result['event_metrics']['commit_to_join']['unknown_or_not_applicable'], 2)

    def test_native_return_without_adapter_finish_stays_outstanding(self):
        rows = [self.receipt('one', 'commit', 10, 'COMMITTED', source_event_ids=[5]),
                self.receipt('one', 'prepared', 10.1, 'PREPARED'),
                self.receipt('one', 'request_entered', 11, 'ENTERED'),
                self.receipt('one', 'native_return', 12, 'RETURNED')]
        result = summarize_dispatch(rows, now=20)
        self.assertEqual(result['outstanding'], 1)
        self.assertAlmostEqual(result['requests'][0]['outstanding_age_seconds'], 9.9)
        self.assertEqual(result['metrics']['native_call']['measured'], 1)
        self.assertEqual(result['metrics']['adapter_call']['measured'], 0)
        self.assertEqual(result['event_metrics']['commit_to_result_visible']['measured'], 0)
        self.assertEqual(result['requests'][0]['stages']['t_end']['state'], 'UNKNOWN')

    def test_missing_new_commit_cannot_attach_its_join_to_the_old_event(self):
        rows = [self.receipt('one', 'commit', 10, 'COMMITTED', source_event_ids=[5],
                             source='tick', source_event_high_water=5),
                self.receipt('one', 'join', 12, 'COALESCED', source='control', source_event_high_water=6)]
        result = summarize_dispatch(rows, now=20)
        self.assertEqual(result['commit_denominators']['events'], 1)
        self.assertEqual(result['commit_denominators']['coalesced_events'], 0)
        self.assertEqual(result['event_metrics']['commit_to_join']['measured'], 0)
        # Explicit membership also takes precedence over the single-commit fallback.
        rows[1].update(source='tick', source_event_high_water=5, source_event_ids=[6])
        result = summarize_dispatch(rows, now=20)
        self.assertEqual(result['event_metrics']['commit_to_join']['measured'], 0)
