"""Local read-only deployment check. No tokens, account IDs, usernames or notes printed.

Run under the service's Python/environment; this describes that configuration,
not an independently discovered process. No app import, migration or network call.
"""
from __future__ import annotations
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from urllib.parse import quote


def inspect(project: Path, environment: dict) -> dict:
    project = project.resolve()
    from dotenv import dotenv_values
    values = {**dotenv_values(project / '.env'), **environment}
    api_id = str(values.get('TELEGRAM_API_ID') or '').strip()
    api_hash = str(values.get('TELEGRAM_API_HASH') or '').strip()
    result = {
        'project': str(project),
        'python': sys.version.split()[0],
        'api_token_present': bool(values.get('API_TOKEN') and values['API_TOKEN'] != 'CHANGE_ME'),
        'mtproto_api_id_valid': api_id.isascii() and api_id.isdigit() and int(api_id) > 0,
        'mtproto_api_hash_valid': bool(re.fullmatch('[A-Fa-f0-9]{32}', api_hash)),
        'custom_emoji_enabled': str(values.get('CUSTOM_EMOJI_ENABLED', 'true')).lower() in {'1','true','yes','on'},
        'database_read_only': True,
        'packages': {},
    }
    for name in ('pyTelegramBotAPI', 'Telethon', 'python-dotenv'):
        try:
            result['packages'][name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result['packages'][name] = 'not_installed'
    try:
        content = (project / 'app.py').read_text(encoding='utf-8')
        match = re.search(r'^BUILD_VERSION = "([A-Za-z0-9._-]+)"$', content, re.M)
        result['source_build'] = match[1] if match else 'no_build_marker'
    except OSError:
        result['source_build'] = 'app_not_found'
    path = Path(str(values.get('DB_PATH') or 'data/reports.sqlite3')).expanduser()
    if not path.is_absolute():
        path = project / path
    result['database_path'] = str(path.resolve())
    if not path.is_file():
        result['database_status'] = 'not_found_no_file_created'
        return result
    try:
        conn = sqlite3.connect('file:' + quote(str(path.resolve()), safe='/') + '?mode=ro', uri=True, timeout=3)
        try:
            conn.execute('PRAGMA query_only=ON')
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'complaints' not in tables:
                result['database_status'] = 'complaints_table_missing'
                return result
            columns = {r[1] for r in conn.execute('PRAGMA table_info(complaints)')}
            result['complaints_total'] = conn.execute('SELECT COUNT(*) FROM complaints').fetchone()[0]
            if {'target_user_id','candidate_status','identity_checked_at'} <= columns:
                row = conn.execute("""SELECT
                    SUM(target_user_id IS NOT NULL),
                    SUM(target_user_id IS NULL AND identity_checked_at IS NULL),
                    SUM(target_user_id IS NULL AND candidate_status='candidate')
                    FROM complaints""").fetchone()
                result['identity_counts'] = dict(zip(('confirmed','unscanned','candidates'),(int(x or 0) for x in row)))
            if 'identity_observations' in tables:
                known = conn.execute('''SELECT COUNT(*),COUNT(DISTINCT user_id)
                    FROM identity_observations''').fetchone()
                result['identity_observations_total'] = known[0]
                result['distinct_ids_observed'] = known[1]
            if 'identity_candidates' in tables:
                result['identity_candidate_links'] = conn.execute('''SELECT COUNT(*)
                    FROM identity_candidates WHERE status='candidate' ''').fetchone()[0]
            if 'ui_settings' in tables:
                data = dict(conn.execute("SELECT key,value FROM ui_settings WHERE key IN ('emoji_catalog','identity_job_status','identity_scan_paused')"))
                try:
                    catalog = json.loads(data.get('emoji_catalog','[]'))
                    result['emoji_cached_count'] = len(catalog) if isinstance(catalog,list) else 0
                except (ValueError,TypeError):
                    result['emoji_cached_count'] = 0
                try:
                    status = json.loads(data.get('identity_job_status','{}'))
                    state = status.get('state') if isinstance(status,dict) else None
                    allowed = {'disabled','running','queued','idle','paused','pausing','rate_limited','retry_wait','error'}
                    result['identity_worker_state'] = state if state in allowed else 'unknown'
                except (ValueError,TypeError):
                    result['identity_worker_state'] = 'unknown'
                result['identity_paused'] = data.get('identity_scan_paused','0') == '1'
            result['database_status'] = 'read_ok'
        finally:
            conn.close()
    except sqlite3.Error as exc:
        result['database_status'] = 'read_failed_' + type(exc).__name__
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    result = inspect(args.project, dict(os.environ))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print('No Telegram calls or application migrations performed. Emoji delivery is verified in Admin / Appearance.')
    return 0 if result.get('database_status') == 'read_ok' else 2


if __name__ == '__main__':
    raise SystemExit(main())
