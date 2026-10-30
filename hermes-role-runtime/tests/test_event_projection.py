"""Large real activation events stay auditable without filling model context."""
import hashlib
import json
from pathlib import Path
import stat
import tempfile
import unittest

from business_ledger import BusinessLedger
from workflow_reports import build_facts
from workflow_runtime import current_input, ledger_path, project_events


class AuditEventProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pool = self.root/'pool.json'
        self.ledger = BusinessLedger(ledger_path(self.pool), clock=lambda: 1000)
        self.run = self.ledger.create_run('req', str(self.root), str(self.root), ['T1'], command_id='run')['run_id']
        self.ledger.select_run(self.run, command_id='select')
        self.manifest = {'validated': True, 'ready': True, 'version': {'digest': 'f'*64},
            'live_source_observations': [{'raw': 'x'*2800000}], 'gates': {'P0': 'PASS'}}
        self.ledger.set_mode('ACTIVE', command_id='active', migration_manifest=self.manifest)
        self.ledger.open_admission(command_id='open', expected_generation=1,
                                   migration_manifest=self.manifest)

    def test_real_activation_input_is_bounded_and_full_events_are_hash_recoverable(self):
        original = self.ledger.events()
        authority = self.ledger.mode()
        for role in ('COORDINATOR', 'A', 'TESTER'):
            packet = current_input(self.pool, role)
            self.assertLess(packet['input_bytes'], 16000)
            self.assertFalse(packet['requires_paging'])
            self.assertEqual([e['event_id'] for e in original], [e['event_id'] for e in packet['events']])
            for source, event in zip(original, packet['events']):
                if source['kind'] not in {'MODE_CHANGED', 'ADMISSION_OPENED'}:
                    self.assertEqual(source, event)
                    continue
                manifest = event['payload']['manifest']
                self.assertEqual('authority-audit-reference-v1', manifest['projection'])
                self.assertTrue(manifest['summary']['validated'])
                ref = manifest['full_event']
                data = Path(ref['path']).read_bytes()
                self.assertEqual(ref['sha256'], hashlib.sha256(data).hexdigest())
                self.assertEqual(ref['bytes'], len(data))
                self.assertEqual(source, json.loads(data))
                self.assertEqual(str(self.ledger.path), ref['database'])
                self.assertEqual(0o600, stat.S_IMODE(Path(ref['path']).stat().st_mode))
                self.assertEqual(0o700, stat.S_IMODE(Path(ref['path']).parent.stat().st_mode))
        self.assertEqual(original, self.ledger.events())
        self.assertEqual(authority, self.ledger.mode())

    def test_report_uses_same_projection_without_ack_or_fact_mutation(self):
        before = self.ledger.snapshot()
        facts = build_facts(self.ledger, 'COORDINATOR', 999, 1001,
                           usage_index=self.root/'usage.db', project=self.root)
        self.assertLess(len(json.dumps(facts).encode()), 24000)
        self.assertEqual(before, self.ledger.snapshot())
        self.assertEqual(['event:'+str(e['event_id']) for e in facts['events']], facts['evidence_ids'])

    def test_modified_view_fails_closed(self):
        events = project_events(self.ledger, self.ledger.events())
        ref = next(e['payload']['manifest']['full_event'] for e in events if e['kind']=='MODE_CHANGED')
        Path(ref['path']).write_text('{}')
        with self.assertRaisesRegex(ValueError, 'AUDIT_EVENT_VIEW_HASH_MISMATCH'):
            current_input(self.pool, 'COORDINATOR')

    def test_actual_large_observation_snapshot_is_bounded_and_unknown_is_preserved(self):
        from workflow_observation import digest
        home=self.root/'home'
        source=home/'cache/role-lease-monitor'/digest(str(self.pool.resolve()))/'snapshot.json'
        source.parent.mkdir(parents=True)
        faults=[{'type':'observer_frozen_execution_unverified','observation_state':'UNKNOWN',
                 'reason_codes':['NATIVE_EXECUTION_ROW_MISSING'],'identity':str(i),'raw':'x'*1500}
                for i in range(1381)]
        snapshot={'at':1000,'health':'unverified','anomalies':faults,
                  'source_states':{'native':{'state':'UNVERIFIED'}}}
        original=json.dumps(snapshot).encode();source.write_bytes(original)
        from workflow_reports import _observation_facts
        from workflow_observation import saved_health
        raw=saved_health(home,self.pool,now=1000)
        observation=_observation_facts(self.ledger,raw)
        self.assertLess(len(json.dumps(observation).encode()),8192)
        self.assertEqual('UNVERIFIED',observation['state'])
        self.assertEqual(['NATIVE_EXECUTION_ROW_MISSING'],observation['reason_codes'])
        self.assertEqual(1381,observation['anomaly_count'])
        ref=observation['full_observation'];data=Path(ref['path']).read_bytes()
        self.assertEqual(ref['sha256'],hashlib.sha256(data).hexdigest())
        self.assertEqual(faults,json.loads(data)['anomalies'])
        self.assertEqual(raw,observation)
        self.assertEqual(original,source.read_bytes())
        facts=build_facts(self.ledger,'COORDINATOR',999,1001,
                         usage_index=home/'runtime/role-usage.db',project=self.root,pool_path=self.pool)
        self.assertLess(len(json.dumps(facts,ensure_ascii=False).encode()),80*1024)
        self.assertEqual(1381,facts['observation']['anomaly_count'])
        Path(ref['path']).write_text('{}')
        with self.assertRaisesRegex(ValueError,'AUDIT_EVENT_VIEW_HASH_MISMATCH'):
            _observation_facts(self.ledger,raw)

    def test_large_write_diagnostics_keep_exact_source_and_unknown_reason_codes(self):
        from workflow_reports import _write_diagnostic_facts
        diagnostics={'source_state':'UNVERIFIED','source':'source.sqlite3',
            'write_paths':[{'write_path_state':'WRITE_AUTHORITY_ANOMALY','reason_codes':['ROLE_OWNER_MISMATCH'],
                            'execution_id':str(i),'raw':'x'*1000} for i in range(100)],
            'anomalies':[],'closures':[]}
        projected=_write_diagnostic_facts(self.ledger,diagnostics)
        self.assertLess(len(json.dumps(projected).encode()),4096)
        self.assertEqual('UNVERIFIED',projected['source_state'])
        self.assertEqual({'WRITE_AUTHORITY_ANOMALY':100},projected['write_path_states'])
        self.assertEqual(['ROLE_OWNER_MISMATCH'],projected['reason_codes'])
        ref=projected['full_diagnostics'];data=Path(ref['path']).read_bytes()
        self.assertEqual(ref['sha256'],hashlib.sha256(data).hexdigest())
        self.assertEqual(diagnostics,json.loads(data))

    def test_business_contracts_failures_and_checkpoints_are_never_trimmed(self):
        # Projection is explicitly restricted to the two authority audit kinds.
        for kind in ('PLAN_PUBLISHED', 'CONTROL_ACCEPTED', 'CHECKPOINT_SAVED', 'VERIFICATION_RECORDED'):
            event = {'event_id': 999, 'kind': kind, 'payload': {
                'manifest': self.manifest, 'FAIL': 'unresolved', 'AC': 'x'*40000}}
            self.assertEqual([event], project_events(self.ledger, [event]))


if __name__ == '__main__':
    unittest.main()
