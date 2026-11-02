"""Read-only local coverage report; prints only aggregate counts, never IDs or usernames.

This intentionally does not infer IDs from nicknames or open the SQLite database
for writing. Running the program does not start the Telegram bot or its workers.
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path


def coverage(db_file: Path) -> dict:
    db_file = Path(db_file).expanduser().resolve(strict=True)
    if not db_file.is_file():
        raise ValueError('Not a database file')
    conn = sqlite3.connect(db_file.as_uri()+'?mode=ro', uri=True)
    try:
        conn.execute('PRAGMA query_only=ON')
        tables = {name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'complaints' not in tables:
            raise ValueError('Missing complaints table')
        cols = {r[1] for r in conn.execute('PRAGMA table_info(complaints)')}
        if 'target_user_id' not in cols:
            raise ValueError('Identity schema not installed: target_user_id missing')
        result = {'total_reports': conn.execute('SELECT COUNT(*) FROM complaints').fetchone()[0]}
        if 'status' in cols:
            result['published_reports'] = conn.execute("SELECT COUNT(*) FROM complaints WHERE status='accepted'").fetchone()[0]
        result['reports_with_linked_id'] = conn.execute('SELECT COUNT(*) FROM complaints WHERE target_user_id IS NOT NULL').fetchone()[0]
        result['reports_without_linked_id'] = result['total_reports']-result['reports_with_linked_id']
        result['reports_with_numeric_target_only'] = conn.execute('''SELECT COUNT(*) FROM complaints
            WHERE target_user_id IS NULL AND TRIM(target) NOT GLOB '*[^0-9]*'
              AND LENGTH(TRIM(target)) BETWEEN 1 AND 16''').fetchone()[0]
        result['unique_username_targets_without_id'] = conn.execute('''SELECT COUNT(DISTINCT LOWER(TRIM(target)))
            FROM complaints WHERE target_user_id IS NULL AND TRIM(target) LIKE '@%' ''').fetchone()[0]
        if 'candidate_status' in cols:
            result['complaints_with_pending_candidate'] = conn.execute('''SELECT COUNT(*) FROM complaints
                WHERE target_user_id IS NULL AND candidate_status='candidate' ''').fetchone()[0]
        if 'identity_source' in cols:
            sources=conn.execute('''SELECT identity_source,COUNT(*) FROM complaints
                WHERE target_user_id IS NOT NULL GROUP BY identity_source''').fetchall()
            result['linked_by_source'] = {k or 'unknown':v for k,v in sources}
        if 'identity_observations' in tables:
            result['ids_observed_by_bot'] = conn.execute('SELECT COUNT(DISTINCT user_id) FROM identity_observations').fetchone()[0]
        if 'identity_candidates' in tables:
            result['candidate_links_total'] = conn.execute("SELECT COUNT(*) FROM identity_candidates WHERE status='candidate'").fetchone()[0]
            result['telegram_selected_review_rows'] = conn.execute('''SELECT COUNT(DISTINCT complaint_id) FROM identity_candidates
                WHERE source IN ('bot_users_shared','bot_forward') AND status='candidate' ''').fetchone()[0]
        result['limitations'] = 'Unknown former owners of username-only complaints cannot be reconstructed by Bot API.'
        return result
    finally:
        conn.close()


def main():
    parser=argparse.ArgumentParser(description='Local read-only Telegram-ID coverage counts')
    parser.add_argument('--db',required=True,type=Path,help='Existing SQLite path (never created or modified)')
    args=parser.parse_args()
    try:
        report=coverage(args.db)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print('ERROR: cannot read existing identity database ('+type(exc).__name__+')',file=sys.stderr)
        return 2
    print(json.dumps(report,indent=2,ensure_ascii=False,sort_keys=True))
    return 0


if __name__=='__main__':
    sys.exit(main())
