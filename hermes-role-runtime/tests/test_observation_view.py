"""Source-level bounds across legacy, ACTIVE and report consumers."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import sys
import types
import unittest
from unittest.mock import patch

from observation_view import health_view
from workflow_observation import digest, saved_health


class ObservationViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.pool = self.home/'role-pool.json'
        self.path = self.home/'cache/role-lease-monitor'/digest(str(self.pool.resolve()))/'snapshot.json'
        self.path.parent.mkdir(parents=True)

    def snapshot(self, **extra):
        value = {'at': 1000, 'health': 'unverified', 'source_states': {
            'native': {'state': 'UNKNOWN'}}, 'anomalies': [
            {'type': 'missing-native-row', 'reason_codes': ['NATIVE_EXECUTION_ROW_MISSING'],
             'observation_state': 'UNKNOWN', 'raw': '旧结论'*1000} for _ in range(500)], **extra}
        self.path.write_text(json.dumps(value, ensure_ascii=False))
        return value

    def test_saved_health_bounds_raw_anomalies_and_preserves_original_hash(self):
        original = self.snapshot()
        before = self.path.read_bytes()
        result = saved_health(self.home, self.pool, now=1001)
        self.assertLess(len(json.dumps(result, ensure_ascii=False).encode()), 8192)
        self.assertEqual('UNVERIFIED', result['state'])
        self.assertEqual(1000, result['observed_at'])
        self.assertEqual(500, result['anomaly_count'])
        self.assertEqual({'UNKNOWN': 1}, result['source_state_counts'])
        self.assertEqual(['NATIVE_EXECUTION_ROW_MISSING'], result['reason_codes'])
        ref = result['full_observation']; data = Path(ref['path']).read_bytes()
        self.assertEqual(ref['sha256'], hashlib.sha256(data).hexdigest())
        self.assertEqual(original['anomalies'], json.loads(data)['anomalies'])
        self.assertIn(b'\n', data)
        self.assertEqual(before, self.path.read_bytes())

    def test_unique_reasons_and_types_do_not_defeat_bound_or_hide_totals(self):
        original = self.snapshot(anomalies=[{'type': 'type-'+str(i),
            'reason_codes': ['UNKNOWN-'+str(i)]} for i in range(3000)])
        result = saved_health(self.home, self.pool, now=1000)
        self.assertLess(len(json.dumps(result, ensure_ascii=False).encode()), 8192)
        self.assertEqual(3000, result['anomaly_count'])
        self.assertEqual(3000, result['reason_code_count'])
        self.assertTrue(result['omitted_counts'])
        self.assertEqual(original['anomalies'], json.loads(
            Path(result['full_observation']['path']).read_text())['anomalies'])

    def test_corrupt_artifact_fails_closed_instead_of_publishing_health(self):
        self.snapshot()
        result = saved_health(self.home, self.pool, now=1000)
        Path(result['full_observation']['path']).write_text('{}')
        with self.assertRaisesRegex(ValueError, 'AUDIT_EVENT_VIEW_HASH_MISMATCH'):
            saved_health(self.home, self.pool, now=1000)
        with self.assertRaisesRegex(ValueError, 'AUDIT_EVENT_VIEW_HASH_MISMATCH'):
            health_view(result, self.home/'other-views')

    def test_legacy_progress_consumer_gets_same_bound_without_active_wrapper(self):
        from progress_query import collect_live
        self.snapshot()
        # Exercise the legacy consumer, stubbing only its independent document
        # sources. saved_health itself reads the real isolated snapshot.
        binding = {'role_pool': {'path': str(self.pool)}}
        packet = {'project': str(self.home), 'tasks': {}, 'roles': {}}
        snapshot = {'observed_at': '1970-01-01T00:16:40Z'}
        with patch('role_status.runtime_module') as runtime, \
             patch('role_observer.collect', return_value=(snapshot, None)), \
             patch('runtime_tools.refresh_progress_facts', return_value=packet), \
             patch('live_progress.collect_live_runtime', return_value={}), \
             patch('role_status.collect_role_statuses', return_value={}), \
             patch('progress_query.pending_requests', return_value=[]), \
             patch.dict('os.environ', {'HERMES_HOME': str(self.home)}), \
             patch('progress_query.datetime') as clock:
            runtime.return_value.binding.return_value = (self.home, binding)
            clock.now.return_value.timestamp.return_value = 1000
            result = collect_live(self.home)
        monitor = result['projection']['runtime_observation']['monitor']
        self.assertLess(len(json.dumps(monitor, ensure_ascii=False).encode()), 8192)
        self.assertEqual(500, monitor['anomaly_count'])
        self.assertEqual('UNVERIFIED', monitor['state'])

    def test_stale_or_unavailable_health_does_not_reuse_a_fresh_summary(self):
        self.snapshot()
        self.assertEqual('UNVERIFIED', saved_health(self.home, self.pool, now=1000)['state'])
        stale = saved_health(self.home, self.pool, now=1300)
        self.assertEqual('DEGRADED', stale['state'])
        self.assertEqual(['observer_snapshot_stale'], stale['reason_codes'])
        self.path.unlink()
        self.assertEqual(['observer_snapshot_unavailable'],
                         saved_health(self.home, self.pool, now=1000)['reason_codes'])

    def test_activation_refreshes_cached_manifest_and_module_top_level_alias(self):
        import workflow_observation
        root = Path(__file__).resolve().parents[1]
        name = 'observation_activation_test'
        spec = importlib.util.spec_from_file_location(name, root/'__init__.py',
            submodule_search_locations=[str(root)])
        plugin = importlib.util.module_from_spec(spec)
        sys.modules[name] = plugin; spec.loader.exec_module(plugin)
        helper = importlib.import_module(name+'.mechanism_activation')
        # Simulate a cached helper with yesterday's manifest and an already
        # imported consumer. No function-local import is involved in this test.
        def old_health(*args, **kwargs):
            yield 'old frame started'
            yield 'old frame finished'
        consumer = types.ModuleType('old_health_consumer')
        consumer.query = old_health
        sys.modules[consumer.__name__] = consumer
        self.addCleanup(sys.modules.pop, consumer.__name__, None)
        running = old_health(); self.assertEqual('old frame started', next(running))
        self.snapshot()
        with patch.object(helper, 'TARGETS', {}), \
             patch.object(helper, 'activate', side_effect=AssertionError('cached helper called')), \
             patch.object(workflow_observation, 'saved_health', old_health):
            receipt = plugin.activate_current_runtime()
            self.assertEqual('ACTIVATED', receipt['state'])
            self.assertIn('workflow_observation.py', receipt['source_sha256'])
            self.assertIn('observation_view.py', receipt['source_sha256'])
            self.assertIsNot(consumer.query, old_health)
            result = consumer.query(self.home, self.pool, now=1000)
            self.assertEqual(500, result['anomaly_count'])
            self.assertLess(len(json.dumps(result, ensure_ascii=False).encode()), 8192)
            self.assertEqual('old frame finished', next(running))
