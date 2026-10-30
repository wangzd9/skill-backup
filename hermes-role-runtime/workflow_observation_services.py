"""Verify native and explicitly adopted service identities without new grants."""
import hashlib
import json


def verify(invocation, action, candidate, facts, raw_spec):
    reference=invocation['reference'];frozen=invocation['frozen']
    authorization=action.get('authorization') or {}
    spec=json.loads(raw_spec)
    expected=frozen['candidate']
    if (invocation['action_id']!=action['action_id'] or invocation['candidate_id']!=action['candidate_id']
            or invocation['kind']!=action['kind']
            or any(expected.get(key)!=candidate.get(key) for key in
                ('candidate_id','run_id','task_id','attempt','base','head','ac_version'))
            or hashlib.sha256(raw_spec).hexdigest()!=reference['spec']['sha256']
            or spec.get('unit')!=reference['unit']
            or spec.get('run_id',spec.get('call_id'))!=reference['native_id']):
        raise ValueError('SERVICE_REFERENCE_IDENTITY_MISMATCH')
    legacy=invocation.get('legacy_adoption')
    if legacy is None:
        if (frozen['action_id']!=action['action_id']
                or frozen['authorization_id']!=authorization.get('authorization_id')):
            raise ValueError('SERVICE_FROZEN_AUTHORIZATION_MISMATCH')
        return 'NATIVE_ACTION'
    # Adoption proves an existing service, never permission to dispatch again.
    imported=next(row for row in facts['imports'] if row['import_id']==legacy['import_id'])
    manifest=imported['manifest']
    source=next(row for row in manifest['source_manifest']['sources'] if row['path']==reference['spec']['path'])
    service=invocation['service_identity']
    mapping=next(row for row in manifest['review']['task_states'] if row['task_id']==candidate['task_id'])
    if (legacy!=authorization.get('legacy_adoption') or legacy.get('dispatch_replay_allowed') is not False
            or authorization.get('source_spec')!=reference['spec']
            or source['sha256']!=reference['spec']['sha256']
            or manifest['state']!='RECONCILED' or manifest['run_id']!=candidate['run_id']
            or authorization.get('service_id')!=service['service_id']
            or authorization.get('service_token_sha256')!=hashlib.sha256(service['token'].encode()).hexdigest()
            or mapping['legacy_task']!=legacy['original_task'] or spec['task']!=legacy['original_task']
            or mapping['legacy_attempt']!=legacy['original_attempt'] or spec['attempt']!=legacy['original_attempt']
            or any(spec[key]!=candidate[key] for key in ('repo','base','head'))):
        raise ValueError('SERVICE_LEGACY_ADOPTION_UNVERIFIED')
    return 'EXISTING_SERVICE_ADOPTED_NO_REPLAY'
