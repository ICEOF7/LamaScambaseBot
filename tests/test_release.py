from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release_check', ROOT / 'scripts' / 'check_release.py')
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)


class ReleaseSafetyTest(unittest.TestCase):
    def test_repo_has_no_runtime_files_or_credentials(self):
        self.assertEqual(scanner.scan(ROOT), [])

    def test_scanner_detects_database_and_env(self):
        with tempfile.TemporaryDirectory() as name:
            temp = Path(name)
            (temp / 'reports.sqlite3').write_text('private')
            (temp / '.env').write_text('API_TOKEN=a_private_token')
            failures = scanner.scan(temp)
            self.assertTrue(any('.env' in value for value in failures))
            self.assertTrue(any('reports.sqlite3' in value for value in failures))

    def test_scanner_detects_token_and_private_key(self):
        with tempfile.TemporaryDirectory() as name:
            temp = Path(name)
            token = '123456789:' + 'A' * 35
            (temp / 'credentials.txt').write_text('API_TOKEN=' + token)
            failures = scanner.scan(temp)
            self.assertTrue(any('telegram_bot_token' in v for v in failures))

    def test_gitignore_hides_sqlite_wal_and_env(self):
        gi = (ROOT / '.gitignore').read_text()
        for fragment in ['.env', '*.sqlite3', '*.sqlite3-*', '*.db-*', 'data/', 'backups/', 'storage/']:
            self.assertIn(fragment, gi)


if __name__ == '__main__':
    unittest.main()
