"""Optional event diagnostics cannot change the original claimed execution."""
import contextlib
from unittest.mock import Mock, patch

import pytest

from tools import cronjob_tools as cron


@pytest.mark.parametrize('case', ['unknown', 'ambiguous', 'not_claimed', 'claim_error',
    'registration_rejected', 'registration_error', 'run_error', 'delivery_failed', 'success'])
@pytest.mark.parametrize('observer_mode', ['absent', 'collect', 'raises'])
def test_event_observer_preserves_native_results_and_cleanup(case, observer_mode):
    job = {'id': 'event-observer-job', 'fire_claim': {'by': 'owner'}}
    stages = []

    def observe(**row):
        stages.append(row)
        if observer_mode == 'raises':
            raise RuntimeError('diagnostics unavailable')

    observer = None if observer_mode == 'absent' else observe
    resolver = Mock(return_value=None if case == 'unknown' else job)
    if case == 'ambiguous':
        resolver.side_effect = cron.AmbiguousJobReference('job', [{'id': 'a'}, {'id': 'b'}])
    claim = Mock(return_value=False if case == 'not_claimed' else job)
    if case == 'claim_error':
        claim.side_effect = OSError('claim unavailable')
    register = Mock(return_value=case != 'registration_rejected')
    if case == 'registration_error':
        register.side_effect = OSError('registration unavailable')
    run = Mock(return_value=True)
    if case == 'run_error':
        run.side_effect = OSError('runner unavailable')
    refreshed = {'last_status': 'delivery_failed' if case == 'delivery_failed' else 'ok',
                 'last_delivery_error': 'delivery unavailable' if case == 'delivery_failed' else None}
    with patch.object(cron, 'resolve_job_ref', resolver), \
            patch.object(cron, 'claim_job_for_fire', claim), \
            patch.object(cron, 'get_job', return_value=refreshed), \
            patch.object(cron, 'is_job_runnable', return_value=True), \
            patch.object(cron, 'mark_job_run') as mark, \
            patch.object(cron.logger, 'warning', side_effect=RuntimeError('diagnostic logger unavailable')), \
            patch.object(cron, '_run_heartbeat', return_value=contextlib.nullcontext()), \
            patch('cron.scheduler.try_register_running_job', register), \
            patch('cron.scheduler.release_running_job') as release, \
            patch('cron.scheduler.run_one_job', run):
        result = cron.execute_job_for_event('job', observer=observer)
    assert result['success'] is (case == 'success')
    assert result['claimed'] is (case not in {'unknown', 'ambiguous', 'not_claimed'})
    assert mark.call_count == (1 if case in {'claim_error', 'registration_error', 'run_error'} else 0)
    assert release.call_count == (1 if case in {'run_error', 'delivery_failed', 'success'} else 0)
    assert run.call_count == (1 if case in {'run_error', 'delivery_failed', 'success'} else 0)
    if observer is None:
        assert stages == []
        return
    assert stages[0]['stage'] == 'request_entered'
    assert stages[-1]['stage'] == 'native_return'
    assert all(isinstance(row['at'], float) and row['snapshot']['monotonic_ns'] > 0 for row in stages)
    claims = [row['snapshot']['outcome'] for row in stages if row['stage'] == 'claim_decided']
    expected = [] if case in {'unknown', 'ambiguous'} else [
        'ERROR_OR_UNKNOWN' if case == 'claim_error' else 'NOT_ACQUIRED' if case == 'not_claimed' else 'ACQUIRED']
    assert claims == expected
    assert all('prompt' not in row['snapshot'] and 'error' not in row['snapshot'] for row in stages)


def test_capability_is_readonly_and_legacy_call_does_not_pass_optional_keyword():
    with pytest.raises(TypeError):
        cron.EVENT_OBSERVER_CAPABILITIES['api_major'] = 2
    job = {'id': 'legacy-event-job'}
    expected = {'claimed': True, 'success': True, 'error': None}
    with patch.object(cron, 'resolve_job_ref', return_value=job), \
            patch.object(cron, '_execute_job_now', return_value=expected) as execute:
        assert cron.execute_job_for_event('legacy', extra_prompt='event context') is expected
    execute.assert_called_once_with(job, extra_prompt='event context')
