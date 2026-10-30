"""Payload migration must preserve existing scheduling/lifecycle fields."""
import importlib.util
from pathlib import Path
import unittest


spec = importlib.util.spec_from_file_location(
    'configure_reporting', Path(__file__).resolve().parents[1]/'scripts/configure_reporting.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ConfigureReportingTests(unittest.TestCase):
    def test_summary_is_model_only_and_status_is_script_only(self):
        binding = {'project_root': '/project', 'roles': {
            'COORDINATOR': {'poll_job_id': 'cp', 'report_job_id': 'cs', 'morning_report_job_id': 'cm'},
            'A': {'poll_job_id': 'ap', 'report_job_id': 'as'},
        }}
        jobs = {key: {'prompt': 'existing', 'enabled': False, 'state': 'paused'}
                for key in ('cp', 'cs', 'cm', 'ap', 'as')}
        updates = module.job_updates(binding, jobs)
        self.assertTrue(updates['cs']['no_agent'])
        for key in ('cm', 'as'):
            self.assertFalse(updates[key]['no_agent'])
            self.assertEqual(updates[key]['skills'], [])
            self.assertEqual(updates[key]['context_from'], [])
            self.assertEqual(updates[key]['enabled_toolsets'], ['no_mcp'])
        protected = {'id', 'enabled', 'state', 'schedule', 'origin', 'deliver', 'next_run_at'}
        self.assertTrue(all(not protected.intersection(update) for update in updates.values()))
        self.assertTrue(all(job['enabled'] is False for job in jobs.values()))
        migrated = {key: {**jobs[key], **updates[key]} for key in jobs}
        self.assertEqual(module.job_updates(binding, migrated), updates)

    def test_missing_binding_refuses_partial_migration(self):
        with self.assertRaisesRegex(ValueError, 'missing'):
            module.job_updates({'project_root': '/project', 'roles': {
                'A': {'report_job_id': 'missing'}}}, {})


if __name__ == '__main__':
    unittest.main()
