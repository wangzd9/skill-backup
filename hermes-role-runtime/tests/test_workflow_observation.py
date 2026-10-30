"""Temporal behavior and independent source recovery using production rules."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workflow_observation import digest, saved_health, step
from workflow_replay import replay, run_corpus


class ObservationReplayTests(unittest.TestCase):
    def test_diagnostic_source_flapping_is_debounced_but_health_fact_remains(self):
        fault = {'type': 'observer_write_diagnostics_unverified',
                 'source': 'diagnostics.sqlite3', 'severity': 1,
                 'identity': 'diagnostics-episode',
                 'reason_codes': ['WRITE_DIAGNOSTICS_SOURCE_DEGRADED']}
        state, events = step({}, {'faults': [fault], 'anomalies': [fault]}, 100)
        self.assertEqual([], events)
        state, events = step(state, {'faults': [fault], 'anomalies': [fault]}, 150)
        self.assertEqual([], events)
        self.assertIn(next(iter(state['active'])), state['active'])
        state, events = step(state, {'faults': [fault], 'anomalies': [fault]}, 220)
        self.assertEqual(['first'], [row['transition'] for row in events])
        healthy = {'source_states': {'diagnostics.sqlite3': {'state': 'OBSERVED', 'checked_at': 230}}}
        state, events = step(state, healthy, 230)
        self.assertEqual([], events)
        state, events = step(state, healthy, 300)
        self.assertEqual([], events)
        state, events = step(state, healthy, 360)
        self.assertEqual(['recovered'], [row['transition'] for row in events])

    def test_diagnostic_blip_before_debounce_emits_no_notification(self):
        fault = {'type': 'observer_write_diagnostics_unverified',
                 'source': 'diagnostics.sqlite3', 'severity': 1,
                 'identity': 'diagnostics-blip',
                 'reason_codes': ['WRITE_DIAGNOSTICS_SOURCE_DEGRADED']}
        state, _ = step({}, {'faults': [fault], 'anomalies': [fault]}, 100)
        healthy = {'source_states': {'diagnostics.sqlite3': {'state': 'OBSERVED', 'checked_at': 110}}}
        state, events = step(state, healthy, 110)
        self.assertEqual([], events)
        state, events = step(state, healthy, 230)
        self.assertEqual([], events)
        self.assertEqual({}, state['active'])

    def test_projection_and_observer_faults_share_one_debounced_episode(self):
        observer_fault = {'type': 'observer_write_diagnostics_unverified',
                          'source': 'diagnostics.sqlite3', 'severity': 1,
                          'reason_codes': ['WRITE_DIAGNOSTICS_SOURCE_DEGRADED'],
                          'identity': 'observer-identity'}
        projection_fault = {'type': 'write_diagnostics_source_unverified',
                            'source': 'diagnostics.sqlite3', 'severity': 1,
                            'reason_codes': ['DIAGNOSTIC_SOURCE_UNAVAILABLE'],
                            'identity': 'projection-identity'}
        state, events = step({}, {'faults': [observer_fault],
                                  'anomalies': [observer_fault, projection_fault]}, 100)
        self.assertEqual([], events)
        self.assertEqual(1, len(state['active']))
        state, events = step(state, {'faults': [observer_fault],
                                     'anomalies': [observer_fault, projection_fault]}, 220)
        self.assertEqual(1, len(events))
        self.assertEqual('observer_write_diagnostics_unverified', events[0]['type'])

    def test_versioned_corpus_is_deterministic_and_expected(self):
        corpus = Path(__file__).resolve().parents[1]/'fixtures/observation'
        first, second = run_corpus(corpus), run_corpus(corpus)
        self.assertEqual(first, second)
        failures = {row['fixture_id']: row['differences'] for row in first['results'] if not row['passed']}
        self.assertFalse(failures, failures)
        self.assertGreaterEqual(first['denominators']['fixtures'], 15)
        self.assertIsNone(first['reviewed_production_false_positive_rate'])
        self.assertTrue(all(row['real_sends'] == row['business_acks'] == row['model_requests'] == 0
                            for row in first['results']))

    def test_replay_does_not_mutate_input_and_reports_exact_difference(self):
        path = Path(__file__).resolve().parents[1]/'fixtures/observation/heartbeat-is-not-activity.json'
        fixture = json.loads(path.read_text())
        fixture['frames'][1]['expected_events'] = []
        original = copy.deepcopy(fixture)
        result = replay(fixture)
        self.assertEqual(fixture, original)
        self.assertFalse(result['passed'])
        self.assertEqual(result['differences'][0]['frame'], 1)
        self.assertEqual(result['differences'][0]['actual'][0]['type'], 'NO_ACTIVITY')

    def test_production_source_adapter_preserves_activity_recovery(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from role_observer import source_states
        fixture = json.loads((Path(__file__).resolve().parents[1]/
            'fixtures/observation/heartbeat-is-not-activity.json').read_text())
        state, events = {}, []
        for frame in fixture['frames']:
            observation = copy.deepcopy(frame['observation'])
            observation['source_states'] = source_states(observation, state, frame['now'])
            state, outputs = step(state, observation, frame['now'])
            events.extend((row['type'], row['transition']) for row in outputs)
        self.assertEqual(events, [('NO_ACTIVITY', 'first'), ('NO_ACTIVITY', 'recovered'), ('NO_ACTIVITY', 'first')])

    def test_source_adapter_recovers_one_role_while_another_is_blind(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from role_observer import source_states
        facts = [{'identity': role, 'role': role, 'type': 'observer_execution_identity_unverified',
                  'source': 'business.sqlite3', 'severity': 1} for role in ['A', 'B']]
        observation = {'sources': ['business.sqlite3'], 'faults': facts, 'anomalies': facts}
        observation['source_states'] = source_states(observation, {}, 100)
        state, _ = step({}, observation, 100)
        observation = {'sources': ['business.sqlite3'], 'faults': [facts[1]], 'anomalies': [facts[1]]}
        observation['source_states'] = source_states(observation, state, 200)
        _, events = step(state, observation, 200)
        self.assertEqual([(row['identity'], row['transition']) for row in events], [('A', 'recovered')])

    def test_unverified_write_source_cannot_certify_recovery(self):
        row = {'identity': 'write-one', 'type': 'write_authority_anomaly',
               'source': 'write-source', 'severity': 1, 'reason_codes': ['ROLE_OWNER_MISMATCH']}
        state, first = step({}, {'anomalies': [row]}, 100)
        state, events = step(state, {'anomalies': [], 'source_states': {
            'write-source': {'state': 'NOT_COLLECTED', 'checked_at': 200}}}, 200)
        self.assertEqual(events, [])
        self.assertIn('write-one', state['active'])
        state, events = step(state, {'source_states': {
            'write-source': {'state': 'OBSERVED', 'checked_at': 300}}}, 300)
        self.assertEqual(events, [])
        self.assertIn('write-one', state['active'])

    def test_truncated_terminal_proofs_and_partial_rebuild_never_mean_write_recovery(self):
        from workflow_diagnostics import projection_anomalies, projection_closures
        identity = {'generation':1,'run_id':'run','role':'A','work_id':'work',
            'execution_id':'execution','execution_generation':1,'task_id':'task','attempt':1}
        paths = [{'identity':'path-'+str(i),'pool':'/fixture/pool.json', **identity,
            'capability':'capability-'+str(i),'write_path_state':'WRITE_AUTHORITY_ANOMALY',
            'first_seen':100,'last_seen':100,'last_decision_id':'denied-'+str(i),
            'last_operation_id':'operation-'+str(i),'operation_count':1,'reason_codes':['ROLE_OWNER_MISMATCH']}
            for i in range(65)]
        prior, _ = step({}, {'anomalies':projection_anomalies(paths,'diagnostics')},100)
        # A healthy but partially reconstructed source is not a success proof.
        partial, events = step(prior, {'source_states':{
            'diagnostics':{'state':'OBSERVED','checked_at':200}}},200)
        self.assertEqual([],events)
        self.assertEqual(65,len(partial['active']))
        ended = [{**row,'write_path_state':'CLOSED_WITH_TERMINATION','closed_at':200,
            'termination_evidence_ref':{'terminal_verified':True,'terminal_kind':'EXECUTION'}} for row in paths]
        observation = {'source_states':{'diagnostics':{'state':'OBSERVED','checked_at':200}},
            'observation_closures':projection_closures(ended[:64])}
        remaining, events = step(prior,observation,200)
        self.assertEqual(64,len(events))
        self.assertTrue(all(row['transition']=='closed_with_execution' for row in events))
        self.assertEqual(1,len(remaining['active']))

    def test_write_recovery_requires_new_committed_exact_identity_and_capability_proof(self):
        from workflow_diagnostics import projection_anomalies, projection_closures
        path = {'identity':'path','pool':'/fixture/pool.json','generation':1,'run_id':'run',
            'role':'A','work_id':'work','execution_id':'execution','execution_generation':1,
            'capability':'checkpoint','write_path_state':'WRITE_AUTHORITY_ANOMALY',
            'first_seen':100,'last_seen':100,'last_decision_id':'denied','last_operation_id':'denied-op',
            'operation_count':1,'reason_codes':['ROLE_OWNER_MISMATCH']}
        prior, _ = step({}, {'anomalies':projection_anomalies([path],'diagnostics')},100)
        proof = projection_closures([{**path,'write_path_state':'RECOVERED','closed_at':200,
            'last_success_decision_id':'new-success','last_success_operation_id':'new-success-op'}])[0]
        for mutation in ({'capability':'deliver'},{'execution_generation':2},{'status':'DUPLICATE'},
                         {'closed_at':99},{'operation_id':'denied-op'},{'decision_phase':'TOOL_PREFLIGHT'}):
            _, events = step(prior,{'observation_closures':[{**proof,**mutation}]},200)
            self.assertEqual([],events,mutation)
        state, events = step(prior,{'observation_closures':[proof]},200)
        self.assertEqual(['recovered'],[row['transition'] for row in events])
        self.assertEqual({},state['active'])

    def test_stale_observer_file_is_detected_by_independent_read(self):
        with tempfile.TemporaryDirectory() as directory:
            home, pool = Path(directory), Path(directory)/'pool.json'
            path = home/'cache/role-lease-monitor'/digest(str(pool.resolve()))/'snapshot.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'at': 100, 'health': 'observed', 'anomalies': []}))
            self.assertEqual(saved_health(home, pool, now=200)['state'], 'OBSERVED')
            result = saved_health(home, pool, now=400)
            self.assertEqual(result['state'], 'DEGRADED')
            self.assertEqual(result['reason_codes'], ['observer_snapshot_stale'])
            path.write_text('{broken')
            self.assertEqual(saved_health(home, pool, now=400)['reason_codes'], ['observer_snapshot_unavailable'])

    def test_scope_change_cannot_use_old_source_check_as_recovery(self):
        fixture = json.loads((Path(__file__).resolve().parents[1]/
            'fixtures/observation/heartbeat-is-not-activity.json').read_text())
        state, _ = step({}, fixture['frames'][1]['observation'], 601)
        next_observation = copy.deepcopy(fixture['frames'][1]['observation'])
        next_observation['activities'][0]['identity']['execution_id'] = 'new-execution'
        for item in next_observation['activities'][0]['sources']:
            item['last_verified_at'] = 700
        state, events = step(state, next_observation, 700)
        self.assertFalse(any(row['transition'] == 'recovered' for row in events))
        state, events = step(state, {**next_observation, 'closed_execution_ids': ['exec-one']}, 700)
        self.assertEqual([row['transition'] for row in events], ['closed_with_execution'])

    def test_feedback_summary_has_episode_denominator_and_no_ack_labels(self):
        import importlib.util
        path = Path(__file__).resolve().parents[1]/'scripts/observation_tools.py'
        spec = importlib.util.spec_from_file_location('observation_cli_fixture', path)
        cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
        with tempfile.TemporaryDirectory() as directory:
            home, pool = Path(directory), Path(directory)/'pool.json'
            outbox = home/'cache/role-lease-monitor'/digest(str(pool.resolve()))/'outbox'
            outbox.mkdir(parents=True)
            for number, phase in enumerate(['first', 'escalated', 'recovered']):
                (outbox/(str(number)+'.json')).write_text(json.dumps({'event': {
                    'episode': 'same-episode', 'transition': phase, 'generated_at': 100+number,
                    'rule_version': 'rule-one'}}))
            summary = cli.monitor_feedback(home, pool, 0, 200)
            self.assertEqual(summary['total'], 1)
            self.assertEqual(summary['counts']['UNREVIEWED'], 1)
            self.assertIsNone(summary['false_positive_rate'])


if __name__ == '__main__':
    unittest.main()
