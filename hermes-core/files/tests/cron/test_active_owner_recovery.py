"""Long active cron workers retain ownership; pending-slot recovery cannot race them."""
from datetime import timedelta
import subprocess
import sys


def test_real_live_owner_survives_age_sweep_across_profiles_until_process_exit(tmp_path, monkeypatch):
    from cron import executions
    from hermes_time import now

    holder = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.buffer.read()'],
                              stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    rows = {}
    try:
        for name in ('a', 'b'):
            database = tmp_path/name/'cron/executions.db'
            monkeypatch.setattr(executions, 'EXECUTIONS_FILE', database)
            row = executions.create_execution('same-job', source='test')
            with executions._transaction() as conn:
                conn.execute("UPDATE executions SET status='running', process_id='external-worker', "
                    'pid=?, process_started_at=?, claimed_at=? WHERE id=?',
                    (holder.pid, executions._process_start_time(holder.pid),
                     (now()-timedelta(hours=4)).isoformat(), row['id']))
            rows[name] = (database, row['id'])
        for name in ('a', 'b', 'a'):
            database, identity = rows[name]
            monkeypatch.setattr(executions, 'EXECUTIONS_FILE', database)
            assert executions.recover_interrupted_executions() == 0
            assert executions.get_execution(identity)['status'] == 'running'
        holder.stdin.close()
        holder.wait(timeout=10)
        for database, identity in rows.values():
            monkeypatch.setattr(executions, 'EXECUTIONS_FILE', database)
            assert executions.recover_interrupted_executions() == 1
            assert executions.get_execution(identity)['status'] == 'unknown'
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=10)


def test_pending_slot_does_not_restore_during_real_fire_claim(tmp_path):
    from cron import jobs
    from cron.occurrences import pending_slot_stamp, unclaimed_pending_slot
    from hermes_time import now

    with jobs.use_cron_store(tmp_path/'cron'):
        job = jobs.create_job(prompt='read-only test', schedule='every 1h')
        claim = jobs.claim_job_for_fire(job['id'], return_job=True)
        assert claim
        at = now()
        slot = (at-timedelta(minutes=10)).isoformat()
        # A following due-scan stamp may be orphaned while the earlier fire's
        # claim is still live. It must not be presented as an unclaimed fire.
        claim['pending_slot'] = pending_slot_stamp(slot, at-timedelta(minutes=10))
        assert unclaimed_pending_slot(claim, at) is None
        claim.pop('fire_claim')
        assert unclaimed_pending_slot(claim, at) == slot
