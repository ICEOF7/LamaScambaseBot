"""One-time Telegram ID reconciliation for a pre-existing private database.

STOP the running bot service first: sharing one MTProto *.session across two
processes can cause SQLite locks. This script never exports user records.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


def main():
    with app.InstanceLock(app.DB_PATH):
        return run_sync()


def run_sync():
    parser = argparse.ArgumentParser(description='Scan existing username records into ID review candidates')
    parser.add_argument('--all', action='store_true', help='Keep checking in bounded batches')
    parser.add_argument('--max-batches', type=int, default=200,
                        help='Maximum batches when using --all (default: 200)')
    parser.add_argument('--batch-size', type=int, default=4, help='Targets per batch (1..10)')
    args = parser.parse_args()
    if not app.identity_resolver.enabled:
        print('MTProto disabled: set TELEGRAM_API_ID and TELEGRAM_API_HASH in a private .env')
        return 2
    if not (1 <= args.batch_size <= 10 and 1 <= args.max_batches <= 10000):
        parser.error('--batch-size must be 1..10 and --max-batches 1..10000')
    if app.community_store.setting('identity_scan_paused', '0') == '1':
        print('ID reconciliation is paused by the administrator. Resume it in the bot first.')
        return 3
    limited = False
    cumulative = {'targets': 0, 'linked': 0, 'candidates': 0, 'unavailable': 0, 'errors': 0}
    for _ in range(args.max_batches if args.all else 1):
        if not app.identity_store.scan_targets(limit=1):
            break
        result = app.run_identity_scan_batch(limit=args.batch_size)
        for key in cumulative:
            cumulative[key] += result[key]
        limited = bool(result.get('rate_limited'))
        if limited or result['errors'] or result['targets'] == 0:
            break
    print('Results (no personal data printed): ' + ', '.join(f'{k}={v}' for k,v in cumulative.items()))
    print('Candidates require approval in the private Telegram admin panel.')
    print('Unprocessed complaints:', app.identity_store.summary()['unscanned'])
    if limited:
        print('Telegram requested a pause. No automatic success claim for an unfinished scan.')
        return 3
    return 1 if cumulative['errors'] else 0


if __name__ == '__main__':
    sys.exit(main())
