"""Technical role-session limits preserve smaller user limits and report behavior."""
from unittest.mock import patch

from cron import scheduler


def test_business_session_limit_is_scoped_and_never_raises_user_limit():
    for kind, global_limit, role_limit, expected in (
        ('business', 500, 64, 64), ('business', 10, 64, 10),
        ('report', 500, 64, 500), ('business', 500, None, 500),
    ):
        setup=scheduler._CronAgentSetup(model='fixture',runtime={},max_iterations=global_limit)
        job={'_role_pool_scope':{'kind':kind,'session_policy':{'request_limit':role_limit}}}
        with (patch.object(scheduler,'_resolve_cron_enabled_toolsets',return_value=[]),
              patch.object(scheduler,'_resolve_cron_disabled_toolsets',return_value=[])):
            result=scheduler._construct_cron_agent(lambda **kwargs:kwargs,job,{},setup,
                workdir=None,session_id='isolated',session_db=None)
        assert result['max_iterations']==expected
