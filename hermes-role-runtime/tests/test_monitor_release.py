import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import monitor_queue
import workflow_migration
from scripts.build_release import source_bytes
from scripts.candidate_profile import create, SKILLS, ROOT


class MonitorReleaseTests(unittest.TestCase):
    def test_candidate_layout_uses_checkout_without_changing_source_home(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'live'; source.mkdir()
            for name in SKILLS:
                skill=source/'.codex/skills'/name
                skill.mkdir(parents=True)
                (skill/'SKILL.md').write_text('# fixture '+name)
            core=source/'.hermes/hermes-agent'
            (core/'.git').mkdir(parents=True)
            secret=source/'.hermes/config.yaml'; secret.write_text('api_key: must-not-copy')
            old_entry=source/'.codex/skills/complex-task-coordinator/scripts'
            old_entry.mkdir(); (old_entry/'role_runtime.py').write_text('# live-only')
            original={str(p):p.read_bytes() for p in source.rglob('*') if p.is_file()}
            profile=Path(folder)/'candidate'
            result=create(profile,source)
            self.assertTrue(result['valid'])
            self.assertFalse((profile/'.hermes/config.yaml').exists())
            for entry in result['skill_entries']['entries']:
                self.assertTrue(Path(entry['entry']).resolve().is_relative_to(ROOT))
            version=workflow_migration.code_version(profile_home=profile)
            self.assertFalse(any(row['path'].startswith(str(source/'.codex')) for row in version['files']))
            self.assertEqual(original,{str(p):p.read_bytes() for p in source.rglob('*') if p.is_file()})
            (profile/'.codex/skills/complex-task-coordinator/SKILL.md').write_text('# changed')
            self.assertNotEqual(workflow_migration.code_version(profile_home=profile)['digest'],version['digest'])
            with self.assertRaises(FileExistsError):
                create(source,source)
            with self.assertRaises(ValueError):
                workflow_migration.code_version([],profile_home=profile)

    def test_default_and_overridden_delivery_policy_are_bound_to_release_hash(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'delivery-policy.json'
            item={'path':str(path),'kind':'monitor_delivery_semantics'}
            first=workflow_migration.code_version([item])
            self.assertFalse(path.exists())
            self.assertEqual(hashlib.sha256(source_bytes(item)).hexdigest(),first['files'][0]['sha256'])
            path.write_text(json.dumps({'pool_limit':7}))
            changed=workflow_migration.code_version([item])
            self.assertNotEqual(changed['digest'],first['digest'])
            self.assertEqual(hashlib.sha256(source_bytes(item)).hexdigest(),changed['files'][0]['sha256'])

    def test_unknown_policy_fields_rejected_without_serializing_values(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'delivery-policy.json'
            path.write_text(json.dumps({'unknown':'private-value'}))
            with self.assertRaisesRegex(ValueError,'INVALID_PERSISTENT') as caught:
                monitor_queue.policy(Path(folder)/'outbox')
            self.assertNotIn('private-value',str(caught.exception))
