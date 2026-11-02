"""Read-only status utility: no runtime database creation or secret output."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('runtime_status', Path(__file__).resolve().parents[1] / 'scripts/runtime_status.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class RuntimeStatusTests(unittest.TestCase):
    def test_missing_database_is_not_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = module.inspect(root, {})
            self.assertEqual(result['database_status'], 'not_found_no_file_created')
            self.assertFalse((root/'data').exists())

    def test_existing_database_is_not_modified_and_no_user_rows_are_printed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root/'existing.sqlite3'
            conn = sqlite3.connect(path)
            conn.execute('CREATE TABLE complaints(id INTEGER, target TEXT, target_user_id INTEGER, identity_checked_at TEXT, candidate_status TEXT)')
            conn.execute("INSERT INTO complaints VALUES(1,'@private_synthetic_record',NULL,NULL,'unscanned')")
            conn.commit(); conn.close()
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            result = module.inspect(root, {'DB_PATH':str(path)})
            self.assertEqual(result['database_status'],'read_ok')
            self.assertEqual(result['identity_counts']['unscanned'],1)
            self.assertNotIn('private_synthetic_record',json.dumps(result))
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),before)

    def test_credentials_are_only_reported_as_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = module.inspect(Path(tmp), {'API_TOKEN':'SYNTHETIC_SENSITIVE_VALUE',
                                    'TELEGRAM_API_ID':'123456', 'TELEGRAM_API_HASH':'f'*32})
            wire = json.dumps(result)
            self.assertNotIn('SYNTHETIC_SENSITIVE_VALUE',wire)
            self.assertNotIn('f'*32,wire)
            self.assertTrue(result['mtproto_api_id_valid'])
            self.assertTrue(result['mtproto_api_hash_valid'])

    def test_corrupt_settings_are_not_printed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); path=root/'example.sqlite3'
            conn=sqlite3.connect(path)
            conn.execute('CREATE TABLE complaints(id INTEGER)')
            conn.execute('CREATE TABLE ui_settings(key TEXT,value TEXT)')
            conn.execute("INSERT INTO ui_settings VALUES('identity_job_status',?)", ('{"state":"private-sensitive"}',))
            conn.execute("INSERT INTO ui_settings VALUES('emoji_catalog',?)", ('broken',))
            conn.commit();conn.close()
            result=module.inspect(root, {'DB_PATH':str(path)})
            self.assertEqual(result['emoji_cached_count'],0)
            self.assertEqual(result['identity_worker_state'],'unknown')
            self.assertNotIn('private-sensitive',json.dumps(result))


if __name__ == '__main__': unittest.main()
