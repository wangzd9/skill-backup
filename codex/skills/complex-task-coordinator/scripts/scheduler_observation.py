"""Record host observer capability separately from successful collection."""
import os
import sys
from importlib.util import find_spec
from pathlib import Path
import time
from runtime_common import save_json, read_json


def _live_receipt(home):
    path=Path(home)/'runtime/role-observation/hook-receipt.json'
    if not path.is_file():
        return None
    value=read_json(path)
    if (type(value.get('pid')) is int and isinstance(value.get('process_start'),str)
            and value.get('state')=='COLLECTED' and value.get('workflow_tick_submitted') is True
            and 0<=time.time()-value.get('checked_at',0)<=180
            and __import__('role_runtime').pid_identity(value.get('pid'))==value.get('process_start')):
        return value
    return None


def capability(home):
    result = {'schema_version': 1, 'checked_at': time.time(), 'pid': os.getpid(),
              'state': 'UNVERIFIED', 'required_contract': 1, 'adopted': False,
              'process_start': __import__('role_runtime').pid_identity(os.getpid())}
    try:
        scheduler_tick = sys.modules.get('cron.scheduler_tick')
        trigger = find_spec('cron.scheduler_tick')
        from hermes_cli.plugins import VALID_HOOKS
        from hermes_cli.plugins_dispatch import _hook_uses_callback_timeout
        result.update(host_contract=getattr(scheduler_tick, 'OBSERVER_CONTRACT_VERSION', None),
            trigger_source=str(Path(trigger.origin).resolve()) if trigger and trigger.origin else None,
            trigger_loaded=scheduler_tick is not None,
            registered_name='on_scheduler_tick' in VALID_HOOKS,
            callback_bounded=_hook_uses_callback_timeout('on_scheduler_tick', 0))
        if result['host_contract'] in (None,1) and result.get('trigger_source') and result['registered_name'] and result['callback_bounded']:
            result['state'] = 'READY_AWAITING_TICK'
        else:
            result['reason'] = 'OBSERVER_HOST_CONTRACT_MISMATCH'
    except (ImportError, AttributeError, OSError) as exc:
        result['reason'] = type(exc).__name__
    directory=Path(home)/'runtime/role-observation'
    save_json(directory/'capability-probe.json',result)
    accepted=_live_receipt(home)
    if accepted:
        result.update(state='ADOPTED',adopted=True,pid=accepted['pid'],
                      process_start=accepted['process_start'],adopted_at=accepted['checked_at'],
                      observation_at=accepted.get('observation_at'),workflow_tick_submitted=True)
    save_json(directory/'capability.json', result)
    return result


def receipt(home, state, **details):
    value = {'schema_version': 1, 'state': state, 'checked_at': time.time(),
             'pid': os.getpid(), 'process_start':__import__('role_runtime').pid_identity(os.getpid()), **details}
    directory=Path(home)/'runtime/role-observation'
    save_json(directory/'hook-latest.json',value)
    if state=='COLLECTED' or _live_receipt(home) is None:
        save_json(directory/'hook-receipt.json', value)
    if state=='COLLECTED' and details.get('workflow_tick_submitted') is True:
        capability(home)
    return value
