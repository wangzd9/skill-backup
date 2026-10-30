import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cron import role_reporting_policy as policy
from cron import scheduler_delivery
from cron.scheduler import _classify_delivery_outcome


def test_role_identity_uses_skills_without_policy(tmp_path):
    with patch.object(policy, 'get_hermes_home', return_value=tmp_path):
        assert policy.is_role_job({'skills': 'complex-task-executor'})
        assert policy.is_role_job({'skills': ['complex-task-tester']})
        assert not policy.is_role_job({'id': 'ordinary', 'skills': ['unrelated']})


def test_role_identity_follows_current_profile_bindings(tmp_path):
    homes = [tmp_path / name for name in ('a', 'b')]
    for home, job_id in zip(homes, ('report-a', 'report-b')):
        home.mkdir()
        (home / 'role-reporting-policy.json').write_text(json.dumps({'role_job_ids': [job_id]}))
    for home, expected in ((homes[0], True), (homes[1], False), (homes[0], True)):
        with patch.object(policy, 'get_hermes_home', return_value=home):
            assert policy.is_role_job({'id': 'report-a', 'skills': []}) is expected


@pytest.mark.parametrize('role_fields', [
    {'skills': 'complex-task-executor'},
    {'skills': ['complex-task-tester']},
    {'skills': [], 'id': 'bound-report'},
])
def test_role_delivery_sends_only_report_body(tmp_path, role_fields):
    from gateway.config import Platform

    (tmp_path / 'role-reporting-policy.json').write_text(json.dumps({
        'timezone': 'Asia/Shanghai', 'quiet_start_hour': 21, 'quiet_end_hour': 8,
        'role_job_ids': ['bound-report']}))
    cfg = MagicMock()
    cfg.platforms = {Platform.TELEGRAM: MagicMock(enabled=True)}
    job = {'id': 'role-delivery', 'name': '角色时段总结', 'deliver': 'origin',
           'origin': {'platform': 'telegram', 'chat_id': '123'}, **role_fields}
    body = '执行者A（空闲）：本时段工作已完成。'
    with patch.object(policy, 'get_hermes_home', return_value=tmp_path), \
            patch.object(policy, 'suppress_role_delivery', return_value=False), \
            patch('cron.scheduler.load_config', return_value={'cron': {'wrap_response': True}}), \
            patch('gateway.config.load_gateway_config', return_value=cfg), \
            patch('tools.send_message_tool._send_to_platform',
                  new=AsyncMock(return_value={'success': True})) as send:
        assert scheduler_delivery._deliver_result(job, body) is None
    send.assert_awaited_once()
    assert send.await_args.args[3] == body


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


def test_bound_summary_jobs_without_skills_observe_quiet_hours(tmp_path):
    (tmp_path / 'role-reporting-policy.json').write_text(json.dumps({
        'timezone': 'Asia/Shanghai', 'quiet_start_hour': 21, 'quiet_end_hour': 8,
        'role_job_ids': ['executor-phase', 'coordinator-morning']}))
    with patch.object(policy, 'get_hermes_home', return_value=tmp_path):
        for time, expected in [('07:59:59', True), ('08:00:00', False),
                               ('20:59:59', False), ('21:00:00', True)]:
            at = datetime.fromisoformat('2026-10-13T' + time + '+08:00')
            assert policy.suppress_role_delivery(
                {'id': 'executor-phase', 'skills': []}, at) is expected
            assert policy.suppress_role_delivery(
                {'id': 'coordinator-morning'}, at) is expected
        assert not policy.suppress_role_delivery({'id': 'unrelated', 'skills': []}, at)


def test_bound_summary_delivery_is_suppressed_after_night_delay(tmp_path):
    (tmp_path / 'role-reporting-policy.json').write_text(json.dumps({
        'timezone': 'Asia/Shanghai', 'quiet_start_hour': 21, 'quiet_end_hour': 8,
        'role_job_ids': ['executor-phase']}))
    at = datetime.fromisoformat('2026-10-13T21:30:00+08:00')
    for failed in (False, True):
        job = {'id': 'executor-phase', 'skills': []}
        with patch.object(policy, 'get_hermes_home', return_value=tmp_path), \
                patch.object(policy, 'datetime') as clock, \
                patch.object(scheduler_delivery, '_record_delivery_verification') as verification, \
                patch.object(scheduler_delivery, '_resolve_delivery_targets') as targets:
            clock.now.return_value = at
            assert scheduler_delivery._deliver_result(job, 'late summary', for_failure=failed) is None
            assert job['_notification_all_targets_suppressed'] is True
            targets.assert_not_called()
            verification.assert_called_once_with(job, [])


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
