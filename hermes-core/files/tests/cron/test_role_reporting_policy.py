import json
from datetime import datetime
from unittest.mock import patch

from cron import role_reporting_policy as policy
from cron import scheduler_delivery
from cron.scheduler import _classify_delivery_outcome


def test_local_policy_boundaries_and_unrelated_jobs(tmp_path):
    (tmp_path / 'role-reporting-policy.json').write_text(json.dumps({
        'timezone': 'Asia/Shanghai', 'quiet_start_hour': 21, 'quiet_end_hour': 8}))
    job = {'skills': ['complex-task-executor']}
    with patch.object(policy, 'get_hermes_home', return_value=tmp_path):
        for time, expected in [('07:59:59', True), ('08:00:00', False),
                               ('20:59:59', False), ('21:00:00', True)]:
            at = datetime.fromisoformat('2026-10-13T' + time + '+08:00')
            assert policy.suppress_role_delivery(job, at) is expected
        assert not policy.suppress_role_delivery({'skills': ['unrelated']}, at)


def test_missing_policy_keeps_existing_delivery(tmp_path):
    with patch.object(policy, 'get_hermes_home', return_value=tmp_path):
        assert not policy.suppress_role_delivery({'skills': ['complex-task-tester']})


def test_quiet_hours_suppress_actual_delivery_even_for_failed_or_delayed_runs():
    for failed in (False, True):
        job = {'id': 'quiet-test', 'skills': ['complex-task-coordinator']}
        with patch.object(policy, 'suppress_role_delivery', return_value=True), \
                patch.object(scheduler_delivery, '_record_delivery_verification') as verification, \
                patch.object(scheduler_delivery, '_resolve_delivery_targets') as targets:
            assert scheduler_delivery._deliver_result(job, 'late report', for_failure=failed) is None
            assert job['_notification_all_targets_suppressed'] is True
            targets.assert_not_called()
            verification.assert_called_once_with(job, [])


def test_suppression_is_not_recorded_as_sent():
    result = _classify_delivery_outcome(delivery_error=None, should_deliver=True,
                                       unresolved_origin=False, normalized_deliver='feishu:test',
                                       incident_acked=False, success=True, notification_suppressed=True)
    assert result == 'suppressed'


def test_queued_daytime_result_is_suppressed_when_drained_at_night():
    from cron import delivery_queue
    job = {'id': 'quiet-queued', 'skills': ['complex-task-executor']}
    delivery_queue.enqueue('one-delivery', job, 'daytime report')
    with patch.object(policy, 'suppress_role_delivery', return_value=True), \
            patch.object(scheduler_delivery, '_record_delivery_verification'), \
            patch.object(scheduler_delivery, '_resolve_delivery_targets') as targets:
        assert delivery_queue.drain(lambda queued, content, failed:
            scheduler_delivery._deliver_result(queued, content, for_failure=failed)) == 1
        targets.assert_not_called()
    assert delivery_queue.get_status('one-delivery')['status'] == 'suppressed'
    assert delivery_queue.enqueue('one-delivery', job, 'retry')['status'] == 'suppressed'
