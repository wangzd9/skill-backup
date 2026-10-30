"""Offline B1 cost comparison against the exact pre-increment backup source."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import statistics
import sys
import tarfile
import time
import types

ROOT = next(parent/'hermes-role-runtime' for parent in Path(__file__).resolve().parents
            if (parent/'hermes-role-runtime'/'business_ledger.py').is_file())
sys.path[:0] = [str(ROOT),str(ROOT/'tests')]
from test_business_ledger import LedgerFixture
import workflow_diagnostics as diagnostics


def measure(backup, output):
    with tarfile.open(backup) as archive:
        body = archive.extractfile('home/wang/work/hermes-role-runtime/business_ledger.py').read()
    baseline = types.ModuleType('baseline_diagnostic_cost_ledger')
    exec(compile(body,'pre-increment-business_ledger.py','exec'),baseline.__dict__)
    fixture = LedgerFixture()
    fixture.setUp()
    try:
        _, identity = fixture.bind('A')
        stale = {**identity,'generation':identity['generation']-1}
        original = baseline.BusinessLedger(fixture.path)
        fixture.ledger.clock = time.time
        measurements = {}
        for name, ledger in [('baseline',original),('increment',fixture.ledger)]:
            selects, elapsed, receipt_delays = [], [], []
            old_connection = ledger._connection
            statements = []
            @contextmanager
            def traced():
                with old_connection() as db:
                    db.set_trace_callback(statements.append)
                    yield db
            ledger._connection = traced
            for index in range(30):
                statements.clear()
                start = time.perf_counter()
                result = ledger.checkpoint(stale, {'next':'offline rejected write'}, command_id=name+'-'+str(index))
                elapsed.append((time.perf_counter()-start)*1000)
                assert result['status']=='REJECTED' and result['code']=='STALE_MODE_GENERATION'
                selects.append(sum(item.lstrip().upper().startswith('SELECT') for item in statements))
                if result.get('decision'):
                    receipt = result['decision']
                    receipt_delays.append(max(0,receipt['receipt_recorded_at']-receipt['decision_at'])*1000)
            ledger._connection = old_connection
            measurements[name] = {'samples':len(elapsed), 'business_selects_per_denial':sorted(set(selects)),
                'elapsed_milliseconds':{'median':statistics.median(elapsed),'max':max(elapsed)},
                'receipt_delay_milliseconds':{'median':statistics.median(receipt_delays),'max':max(receipt_delays)} if receipt_delays else None}
        scope = {**identity,'pool_path':str(fixture.root/'isolated.json'),'kind':'business'}
        with diagnostics.operation_context(scope,operation='terminal',capability='workspace_write'):
            pending = diagnostics.record_result(fixture.path,
                {'status':'UNVERIFIED','code':'BUSINESS_SOURCE_UNAVAILABLE'})
        cold = diagnostics.observe(scope['pool_path'])
        # Measure one targeted check separately from the cold command-receipt
        # recovery. Both use only local temporary Git/SQLite resources.
        diagnostics.rebuild_projection(scope['pool_path'],budget_seconds=2)
        start = time.perf_counter()
        observed = diagnostics.observe(scope['pool_path'])
        verification = observed['verification'][0]
        result = {'schema_version':1,'kind':'OFFLINE_COST_MEASUREMENT','real_model':False,'real_channel':False,
            'production_activated':False,'baseline_source_sha256':hashlib.sha256(body).hexdigest(),
            'current_source_sha256':hashlib.sha256((ROOT/'business_ledger.py').read_bytes()).hexdigest(),
            'measurement_script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'source_hashes':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in
                ('business_ledger.py','workflow_diagnostics.py','workflow_runtime.py','workflow_sandbox.py')},
            'measurements':measurements,
            'incremental_business_selects_per_denial':measurements['increment']['business_selects_per_denial'][0]-measurements['baseline']['business_selects_per_denial'][0],
            'pending_supplement':{'indexed_rows_read':observed['pending_rows_read'],
                'elapsed_milliseconds':(time.perf_counter()-start)*1000,
                'collection_budget_seconds':observed['collection_budget_seconds'],
                'collection_budget_exhausted':observed['collection_budget_exhausted'],
                'receipt_to_verification_milliseconds':(verification['verification_at']-pending['decision_at'])*1000,
                'state':verification['state'],'historical_classification_unchanged':True},
            'cold_observation':{'elapsed_milliseconds':cold['collection_elapsed_seconds']*1000,
                'collection_budget_seconds':cold['collection_budget_seconds'],
                'collection_budget_exhausted':cold['collection_budget_exhausted'],
                'rebuild':cold['rebuild'], 'verification_states':[v['state'] for v in cold['verification']]},
            'limitations':['Local temporary Git/SQLite fixture; timings are not production latency guarantees.',
                'Cache persistence and one commands-result update add local writes; no full-pool snapshot is queried.',
                'The collection budget stops new steps; an in-flight local SQLite/process read can finish after its deadline.',
                'Current supplementation cannot prove past authority and preserves historical UNKNOWN.']}
        Path(output).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps(result,ensure_ascii=False,indent=2))
    finally:
        fixture.tearDown()


if __name__=='__main__':
    measure(Path(sys.argv[1]),Path(sys.argv[2]))
