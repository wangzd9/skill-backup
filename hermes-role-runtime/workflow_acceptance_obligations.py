"""Phase obligations derived from immutable accepted contracts, never PASS facts.

The MN contract explicitly divides MN-07/08/09/10. Other contracts retain their
original all-AC test gate; phase partition is never inferred from an AC number.
"""
import hashlib
import re


def enrich(spec, texts):
    if spec.get('obligations'):return spec
    acceptance='\n'.join(text for ref,text in texts if ref['path'].upper().endswith('ACCEPTANCE.MD'))
    mn=all(token in acceptance for token in ('MN-07','MN-08','MN-09','MN-10','MN-AC-10','固定 SHA','收口'))
    rows={}
    for line in acceptance.splitlines():
        columns=[c.strip() for c in line.strip().strip('|').split('|')]
        if len(columns)>=3 and columns[0] in spec['checks']:
            rows[columns[0]]={'scenario':columns[1],'evidence_description':columns[2]}
    obligations=[]
    for ac in spec['checks']:
        common={'ac_id':ac,**rows.get(ac,{'scenario':ac,'evidence_description':'Accepted contract evidence'})}
        index=mn and ac=='MN-AC-10'
        obligations.append({**common,'id':ac+':local','phase':'local_validation','owner':'TESTER',
            'evidence_type':'report_index' if index else 'test','applicable':True,
            'predecessors':list(spec['dependencies']),
            'assertion_scope':('固定 SHA、供应方独立验证与本单报告索引；协调审查和收口由下一阶段完成。' if index else common['scenario'])})
        if mn:
            obligations.append({**common,'id':ac+':closure','phase':'local_closure','owner':'COORDINATOR',
                'evidence_type':'review_index','applicable':True,'predecessors':[ac+':local'],
                'assertion_scope':'逐项核对场景、断言、环境与证据语义；AC10聚合审查及收口回执。'})
            for phase,owner in [('real_nodes','TESTER'),('production','COORDINATOR')]:
                obligations.append({**common,'id':ac+':'+phase,'phase':phase,'owner':owner,
                    'evidence_type':'authorized_environment','applicable':False,
                    'applicability':'Requires separately accepted stage and concrete environment authorization.',
                    'predecessors':[ac+':closure'],'state':'NOT_AUTHORIZED_NOT_COMPLETED'})
    return {**spec,'obligation_schema_version':1,'obligations':obligations,
        'coverage_matrix_required':bool(mn),'phase_basis':'accepted_MN_07_08_09_10' if mn else 'original_all_AC_validation',
        'contract_text_sha256':hashlib.sha256(acceptance.encode()).hexdigest()}


def effective(spec):
    if spec is None or spec.get('obligations'):return spec
    from pathlib import Path
    texts=[]
    for ref in spec['sources']:
        raw=Path(ref['frozen_path']).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=ref['sha256']:raise ValueError('COMPLETION_ACCEPTED_SOURCE_CHANGED')
        texts.append((ref,raw.decode()))
    return enrich(spec,texts)


def validate_matrix(ledger, ctx, checks, runs):
    """Locate actual cases in raw JUnit and bind each proof to contract text.

Mechanical validation cannot establish semantic coverage. Coordinator review
at closure must attest each matrix row explicitly, with evidence retained.
"""
    from business_ledger import _deny
    if not ctx['plan'].get('coverage_matrix_required'):return
    import xml.etree.ElementTree as ET
    from pathlib import Path
    obligations={o['ac_id']:o for o in ctx['plan']['obligations'] if o['phase']=='local_validation'}
    for check in checks:
        obligation=obligations[check['id']]
        if check['result']!='PASS':continue
        proofs=check.get('coverage',[])
        if not proofs:_deny('INTEGRATION_AC_COVERAGE_MATRIX_REQUIRED',ac_id=check['id'],obligation=obligation)
        for proof in proofs:
            if (proof.get('obligation_id')!=obligation['id'] or proof.get('phase')!='local_validation'
                    or proof.get('environment') not in ('local','substitute','isolated_mysql')
                    or not proof.get('scenario') or not proof.get('assertion')):
                _deny('INTEGRATION_AC_COVERAGE_BINDING_INVALID',ac_id=check['id'])
            if proof.get('contract_sha256')!=ctx['plan']['contract_text_sha256']:
                _deny('INTEGRATION_AC_CONTRACT_REFERENCE_REQUIRED',ac_id=check['id'])
            if proof.get('scenario')!=obligation['scenario']:
                _deny('INTEGRATION_AC_SCENARIO_MISMATCH',ac_id=check['id'],expected=obligation['scenario'])
            if obligation['evidence_type']=='report_index':
                refs=proof.get('event_ids',[])
                if not refs or not set(refs)<=set(runs):_deny('INTEGRATION_REPORT_INDEX_REQUIRED')
                from delivery_evidence import check_candidate_test_evidence
                if any(not check_candidate_test_evidence(runs[eid]['payload']['evidence'],runs[eid]['payload']['target'])['valid'] for eid in refs):
                    _deny('INTEGRATION_REPORT_INDEX_EVIDENCE_CHANGED')
                continue
            eid=proof.get('test_event_id');case=proof.get('case_id')
            if eid not in check['test_event_ids'] or eid not in runs or not case:
                _deny('INTEGRATION_AC_TEST_CASE_REFERENCE_REQUIRED',ac_id=check['id'])
            record=runs[eid]['payload']
            if proof['environment']!=check['mode']:
                _deny('INTEGRATION_AC_ENVIRONMENT_MISMATCH')
            if proof['environment']=='isolated_mysql' and record['evidence'].get('verification_profile')!='integration_mysql':
                _deny('INTEGRATION_AC_ENVIRONMENT_MISMATCH')
            root=ET.fromstring(Path(record['evidence']['path']).read_bytes())
            matches=[c for c in root.iter('testcase') if case in (c.get('name'),c.get('classname','')+'::'+str(c.get('name','')))]
            if len(matches)!=1 or any(matches[0].find(t) is not None for t in ('failure','error','skipped')):
                _deny('INTEGRATION_AC_CASE_NOT_UNIQUE_PASS',ac_id=check['id'],case_id=case)


def closure_review(ledger, state, payload):
    from business_ledger import _deny
    spec=state['plan']
    if not spec.get('coverage_matrix_required'):return
    checks=state['report']['payload']['checks']
    if any(not c.get('coverage') for c in checks):_deny('REQUIREMENT_LEGACY_REPORT_REQUIRES_COVERAGE_REVALIDATION')
    rows=payload.get('coverage_review',[])
    expected={o['id'] for o in spec['obligations'] if o['phase']=='local_closure'}
    if len(rows)!=len(expected) or {r.get('obligation_id') for r in rows}!=expected:
        _deny('REQUIREMENT_PER_AC_SEMANTIC_REVIEW_REQUIRED')
    for row in rows:
        if row.get('result')!='PASS' or not row.get('reason') or row.get('validation_event_id')!=state['report']['event_id']:
            _deny('REQUIREMENT_COVERAGE_REVIEW_NOT_PASSED')
    # Original leaf review gates are still enforced by completed_head and
    # frozen_target; this attestation cannot substitute for OCR/strong review.
