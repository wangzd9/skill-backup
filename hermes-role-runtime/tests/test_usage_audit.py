from pathlib import Path
import tempfile
import unittest

import usage_accounting as usage
from usage_audit import audit


class UsageAuditTests(unittest.TestCase):
    def test_recorded_cache_and_verified_coverage_keep_all_physical_requests(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'usage.db'
            common=dict(role='A',project=folder,session_id='s',started_at=100,ended_at=110)
            for attempt,cache in [('legacy-zero',0),('legacy-positive',7)]:
                usage.record(path,'end',**common,api_request_id=attempt,physical_attempt_id=attempt,
                    usage={'input_tokens':5,'output_tokens':2,'cache_read_tokens':cache,'cache_write_tokens':0})
            # Input and cache can both be observable on a single request.
            usage.record(path,'end',**common,api_request_id='canonical',physical_attempt_id='canonical',
                usage={'input_tokens':5,'output_tokens':2,'cache_read_tokens':11,'cache_write_tokens':0,
                    'normalization_version':'hermes-canonical-v1',
                    'field_presence':{k:True for k in usage.ALL_TOKEN_FIELDS}})
            original=path.read_bytes()
            result=audit(path,90,120)
            total=result['total'];raw=total['recorded_json']
            self.assertEqual(total['physical_requests'],3)
            self.assertEqual(total['usage_present_calls'],3)
            self.assertEqual(total['field_coverage_calls']['cache_read_tokens'],1)
            self.assertEqual(total['known_tokens']['cache_read_tokens'],11)
            self.assertEqual(raw['field_calls']['cache_read_tokens'],3)
            self.assertEqual(raw['tokens']['cache_read_tokens'],18)
            self.assertEqual(raw['unverified_field_calls']['cache_read_tokens'],2)
            self.assertEqual(raw['unverified_tokens']['cache_read_tokens'],7)
            tiers=raw['cache_read_evidence']
            self.assertEqual(tiers['field_presence'],{'calls':1,'tokens':11})
            self.assertEqual(tiers['legacy_nonzero'],{'calls':1,'tokens':7})
            self.assertEqual(tiers['legacy_zero_unresolved'],{'calls':1,'tokens':0})
            self.assertEqual(tiers['numerically_supported'],{'calls':2,'tokens':18})
            self.assertEqual(result['groups'][0]['recorded_json']['cache_read_evidence'],tiers)
            self.assertEqual(sum(g['physical_requests'] for g in result['groups']),3)
            self.assertEqual(original,path.read_bytes())

    def test_zero_presence_and_unknown_provenance_do_not_become_legacy_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'usage.db'
            for attempt,cache,version,presence in [('explicit-zero',0,'hermes-canonical-v1',True),
                    ('unknown-positive',9,'future-unrecognized',False)]:
                usage.record(path,'end',role='A',project=folder,session_id='s',started_at=100,ended_at=110,
                    api_request_id=attempt,physical_attempt_id=attempt,
                    usage={'input_tokens':0,'output_tokens':0,'cache_read_tokens':cache,
                           'normalization_version':version,
                           **({'field_presence':{'cache_read_tokens':True}} if presence else {})})
            total=audit(path,90,120)['total']
            tiers=total['recorded_json']['cache_read_evidence']
            self.assertEqual(tiers['field_presence'],{'calls':1,'tokens':0})
            self.assertEqual(tiers['numerically_supported'],{'calls':1,'tokens':0})
            self.assertEqual(tiers['other_presence_unknown'],{'calls':1,'tokens':9})
            self.assertEqual(tiers['legacy_nonzero']['calls'],0)
            self.assertEqual(total['physical_requests'],2)

    def test_canonical_prompt_is_not_lost_when_cache_split_is_unknown(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'usage.db'
            common=dict(role='COORDINATOR',project=folder,session_id='s',api_request_id='logical',
                session_kind='business_cron',model='fixture',provider='fixture',run_id='r',work_id='w',execution_id='e')
            for n in range(2):
                usage.record(path,'end',**common,physical_attempt_id=str(n),started_at=100,ended_at=110+n,
                    usage={'input_tokens':None,'output_tokens':2,'prompt_tokens':100,
                        'field_presence':{'prompt_tokens':True,'output_tokens':True}},provider_dispatched=True)
            result=audit(path,90,120,time_basis='started_at')
            self.assertEqual(result['total']['physical_requests'],2)
            self.assertEqual(result['total']['logical_requests'],1)
            self.assertEqual(result['total']['known_tokens']['prompt_tokens'],200)
            self.assertIsNone(result['total']['known_tokens']['input_tokens'])
            self.assertIsNone(result['total']['known_tokens']['cache_read_tokens'])
            self.assertEqual(result['total']['business_attributed_calls'],2)
            self.assertEqual(sum(g['physical_requests'] for g in result['groups']),2)
            summary=usage.summarize(path,'COORDINATOR',90,120,project=folder)
            self.assertEqual(summary['business_attributed_calls'],2)
            self.assertEqual(summary['routes']['unknown']['known_tokens']['prompt_tokens'],200)

    def test_window_basis_filters_and_half_open_boundary_do_not_mix(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'usage.db'
            fields=dict(role='A',project=folder,session_id='s',api_request_id='logical',physical_attempt_id='p',
                session_kind='business_cron',model='fixture',provider='fixture',started_at=100,ended_at=120,
                usage={'input_tokens':5,'output_tokens':1})
            usage.record(path,'end',**fields)
            self.assertEqual(audit(path,100,120,time_basis='started_at')['total']['physical_requests'],1)
            self.assertEqual(audit(path,100,120,time_basis='ended_at')['total']['physical_requests'],0)
            self.assertEqual(audit(path,100,121,time_basis='ended_at',role='B')['total']['physical_requests'],0)
            with self.assertRaises(ValueError):audit(path,100,120,time_basis='guessed')

    def test_missing_source_remains_unknown_and_does_not_create_database(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'missing.db'
            self.assertEqual(audit(path,100,120)['state'],'UNKNOWN')
            self.assertFalse(path.exists())
