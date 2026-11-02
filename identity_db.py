"""Versioned Telegram user ID and username history for complaint records.

Never infer that a historic complaint is about the *current* owner of its
username. Legacy data receives an admin-review candidate, not an automatic ID.
"""

import re
import sqlite3
from datetime import datetime, timedelta, timezone

USERNAME_RE = re.compile(r'^@[A-Za-z0-9_]{3,32}$')
MAX_USER_ID = 2**52 - 1


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def normalized_username(value):
    if not value:
        return None
    candidate = '@' + str(value).strip().lstrip('@').lower()
    return candidate if USERNAME_RE.fullmatch(candidate) else None


class IdentityStore:
    def __init__(self, conn, lock):
        self.conn = conn
        self.lock = lock

    def initialize(self):
        with self.lock:
            columns = {r['name'] for r in self.conn.execute('PRAGMA table_info(complaints)')}
            additions = {
                'target_user_id': 'INTEGER',
                'identity_source': "TEXT NOT NULL DEFAULT 'legacy'",
                'identity_origin': "TEXT NOT NULL DEFAULT 'legacy'",
                'identity_checked_at': 'TEXT',
                'candidate_user_id': 'INTEGER',
                'candidate_username': 'TEXT',
                'candidate_checked_at': 'TEXT',
                'candidate_status': "TEXT NOT NULL DEFAULT 'unscanned'",
            }
            for name, definition in additions.items():
                if name not in columns:
                    self.conn.execute(f'ALTER TABLE complaints ADD COLUMN {name} {definition}')
            self.conn.executescript('''
                CREATE TABLE IF NOT EXISTS telegram_identities (
                    user_id INTEGER PRIMARY KEY,
                    current_username TEXT,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    last_checked TEXT NOT NULL,
                    source TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS telegram_username_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES telegram_identities(user_id),
                    username TEXT NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    is_current INTEGER NOT NULL DEFAULT 1,
                    source TEXT NOT NULL,
                    UNIQUE(user_id, username)
                );
                CREATE INDEX IF NOT EXISTS idx_telegram_username_history_name
                    ON telegram_username_history(username, user_id);
                CREATE INDEX IF NOT EXISTS idx_complaints_target_user_id
                    ON complaints(target_user_id, status);
                CREATE INDEX IF NOT EXISTS idx_complaints_target_lower
                    ON complaints(LOWER(target));
                CREATE INDEX IF NOT EXISTS idx_complaints_target_normalized
                    ON complaints(LOWER('@' || LTRIM(TRIM(target), '@')));
                CREATE INDEX IF NOT EXISTS idx_complaints_identity_queue
                    ON complaints(target_user_id, candidate_status, id);
            ''')
            self.conn.commit()

    @staticmethod
    def _validate_user_id(user_id):
        return isinstance(user_id, int) and not isinstance(user_id, bool) and 0 < user_id <= MAX_USER_ID

    def observe(self, user_id, username, *, extra_usernames=(), source='telegram', checked_at=None):
        """Observe ID/aliases from an actual Telegram user object (not a guessed ID)."""
        if not self._validate_user_id(user_id):
            raise ValueError('Invalid Telegram user ID')
        checked_at = checked_at or utcnow()
        primary = normalized_username(username)
        active_names = {u for value in (primary, *extra_usernames)
                        if (u := normalized_username(value)) is not None}
        with self.lock:
            self.conn.execute('''INSERT INTO telegram_identities
                (user_id,current_username,first_seen,last_seen,last_checked,source)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET
                  current_username=excluded.current_username,
                  last_seen=excluded.last_seen, last_checked=excluded.last_checked,
                  source=excluded.source''',
                (user_id, primary, checked_at, checked_at, checked_at, source))
            if active_names:
                placeholders = ','.join('?' for _ in active_names)
                self.conn.execute(f'''UPDATE telegram_username_history SET is_current=0
                    WHERE user_id = ? AND username NOT IN ({placeholders})''',
                    (user_id, *sorted(active_names)))
                self.conn.execute(f'''UPDATE telegram_username_history SET is_current=0
                    WHERE user_id <> ? AND username IN ({placeholders})''',
                    (user_id, *sorted(active_names)))
                self.conn.execute(f'''UPDATE telegram_identities SET current_username=NULL
                    WHERE user_id <> ? AND current_username IN ({placeholders})''',
                    (user_id, *sorted(active_names)))
                for name in sorted(active_names):
                    self.conn.execute('''INSERT INTO telegram_username_history
                        (user_id,username,first_seen,last_seen,is_current,source)
                        VALUES (?,?,?,?,1,?)
                        ON CONFLICT(user_id,username) DO UPDATE SET
                          last_seen=excluded.last_seen, is_current=1,
                          source=excluded.source''',
                        (user_id, name, checked_at, checked_at, source))
            else:
                self.conn.execute('''UPDATE telegram_username_history
                                     SET is_current=0 WHERE user_id=?''', (user_id,))
            self.conn.commit()

    def record_historical_alias(self, user_id, username, *, source='admin_verified'):
        alias = normalized_username(username)
        if not alias or not self._validate_user_id(user_id):
            return
        with self.lock:
            now = utcnow()
            self.conn.execute('''INSERT OR IGNORE INTO telegram_identities
                (user_id,current_username,first_seen,last_seen,last_checked,source)
                VALUES (?,NULL,?,?,?,?)''', (user_id, now, now, now, source))
            self.conn.execute('''INSERT OR IGNORE INTO telegram_username_history
                (user_id, username, first_seen, last_seen, is_current, source)
                VALUES (?,?,?,?,0,?)''', (user_id, alias, now, now, source))
            self.conn.commit()

    def scan_targets(self, limit=5):
        with self.lock:
            rows = self.conn.execute('''SELECT LOWER('@' || LTRIM(TRIM(target), '@')) AS target, MIN(id) AS first_id,
                   MAX(id) AS last_id,
                   MAX(CASE WHEN status='accepted' THEN 1 ELSE 0 END) AS published,
                   MAX(CASE WHEN identity_origin='new' THEN 1 ELSE 0 END) AS urgent
                FROM complaints WHERE target_user_id IS NULL AND identity_checked_at IS NULL
                GROUP BY LOWER('@' || LTRIM(TRIM(target), '@'))
                ORDER BY urgent DESC, published DESC, last_id DESC LIMIT ?''',
                (max(1, min(limit, 50)),)).fetchall()
            return [dict(row) for row in rows]

    def apply_scan(self, target, resolved, *, now=None):
        """Username resolution supplies candidates; it never proves the report subject.

        Resolved is a *fresh*, username-checked TelegramIdentity or None.
        """
        target = normalized_username(target)
        if not target:
            return {'linked': 0, 'candidates': 0, 'unavailable': 0}
        now = now or utcnow()
        valid = (resolved is not None
                 and self._validate_user_id(resolved.user_id)
                 and target in resolved.usernames)
        if valid:
            self.observe(resolved.user_id, resolved.username, extra_usernames=resolved.usernames,
                         source='mtproto', checked_at=resolved.checked_at)
        result = {'linked': 0, 'candidates': 0, 'unavailable': 0}
        with self.lock:
            rows = self.conn.execute('''SELECT id,identity_origin,timestamp FROM complaints
                WHERE LOWER('@' || LTRIM(TRIM(target), '@'))=? AND target_user_id IS NULL AND identity_checked_at IS NULL''',
                (target,)).fetchall()
            for row in rows:
                if valid:
                    self.conn.execute('''UPDATE complaints
                        SET candidate_user_id=?,candidate_username=?,candidate_checked_at=?,
                            identity_checked_at=?,candidate_status='candidate'
                        WHERE id=? AND target_user_id IS NULL''',
                        (resolved.user_id, resolved.username, now, now, row['id']))
                    result['candidates'] += 1
                else:
                    self.conn.execute('''UPDATE complaints
                        SET identity_checked_at=?,candidate_status='unavailable'
                        WHERE id=? AND target_user_id IS NULL''', (now, row['id']))
                    result['unavailable'] += 1
            self.conn.commit()
        return result

    def mark_invalid_target(self, raw_target):
        with self.lock:
            self.conn.execute('''UPDATE complaints SET identity_checked_at=?,candidate_status='invalid'
                WHERE LOWER('@' || LTRIM(TRIM(target), '@'))=? AND target_user_id IS NULL
                  AND identity_checked_at IS NULL''', (utcnow(), '@' + raw_target.strip().lstrip('@').lower()))
            self.conn.commit()

    def pending_refresh_ids(self, limit=4, *, stale_hours=24):
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=stale_hours)).isoformat()
        with self.lock:
            rows = self.conn.execute('''SELECT c.target_user_id AS user_id
                FROM complaints AS c
                LEFT JOIN telegram_identities AS i ON i.user_id=c.target_user_id
                WHERE c.target_user_id IS NOT NULL
                  AND (i.last_checked IS NULL OR i.last_checked < ?)
                GROUP BY c.target_user_id ORDER BY MIN(c.id) LIMIT ?''',
                (cutoff, max(1, min(limit, 50)))).fetchall()
            return [int(row['user_id']) for row in rows]

    def mark_refresh_unavailable(self, user_id):
        with self.lock:
            self.conn.execute('''UPDATE telegram_identities SET last_checked=?
                WHERE user_id=?''', (utcnow(), user_id))
            self.conn.commit()

    def approve_candidate(self, complaint_id, *, max_age_days=7):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
        with self.lock:
            row = self.conn.execute('''SELECT target,candidate_user_id FROM complaints
                WHERE id=? AND target_user_id IS NULL AND candidate_status='candidate'
                  AND candidate_checked_at >= ? AND candidate_user_id IS NOT NULL''',
                (complaint_id, cutoff)).fetchone()
            if not row:
                return False
            cursor = self.conn.execute('''UPDATE complaints
                SET target_user_id=candidate_user_id,identity_source='admin_confirmed',
                    candidate_status='linked',identity_checked_at=?
                WHERE id=? AND target_user_id IS NULL AND candidate_status='candidate' ''',
                (utcnow(), complaint_id))
            changed = cursor.rowcount == 1
            self.conn.commit()
            if changed:
                self.record_historical_alias(row['candidate_user_id'], row['target'])
            return changed

    def reject_candidate(self, complaint_id):
        with self.lock:
            cursor = self.conn.execute('''UPDATE complaints
                SET candidate_user_id=NULL,candidate_username=NULL,candidate_status='rejected'
                WHERE id=? AND target_user_id IS NULL AND candidate_status='candidate' ''',
                (complaint_id,))
            self.conn.commit()
            return cursor.rowcount == 1

    def reset_scan(self, complaint_id):
        with self.lock:
            cursor = self.conn.execute('''UPDATE complaints
                SET candidate_user_id=NULL,candidate_username=NULL,candidate_checked_at=NULL,
                    identity_checked_at=NULL,candidate_status='unscanned'
                WHERE id=? AND target_user_id IS NULL''', (complaint_id,))
            self.conn.commit()
            return cursor.rowcount == 1

    def manual_link(self, complaint_id, user_id):
        if not self._validate_user_id(user_id):
            raise ValueError('Invalid Telegram user ID')
        with self.lock:
            row = self.conn.execute('SELECT target FROM complaints WHERE id=?',
                                    (complaint_id,)).fetchone()
            if not row:
                return False
            self.conn.execute('''UPDATE complaints SET target_user_id=?,
                identity_source='admin_manual', identity_checked_at=?,
                candidate_status='linked',candidate_user_id=NULL,candidate_username=NULL
                WHERE id=?''', (user_id, utcnow(), complaint_id))
            self.conn.commit()
            self.record_historical_alias(user_id, row['target'])
            return True

    def unlink(self, complaint_id):
        with self.lock:
            prior = self.conn.execute('SELECT target,target_user_id FROM complaints WHERE id=?',
                                      (complaint_id,)).fetchone()
            cursor = self.conn.execute('''UPDATE complaints SET target_user_id=NULL,
                identity_source='legacy', candidate_status='unscanned',
                identity_checked_at=NULL,candidate_user_id=NULL,
                candidate_username=NULL,candidate_checked_at=NULL
                WHERE id=? AND target_user_id IS NOT NULL''', (complaint_id,))
            changed = cursor.rowcount == 1
            if changed and prior:
                username = normalized_username(prior['target'])
                if username:
                    self.conn.execute('''DELETE FROM telegram_username_history
                        WHERE user_id=? AND username=? AND source='admin_verified'
                        AND NOT EXISTS (SELECT 1 FROM complaints
                            WHERE target_user_id=? AND LOWER('@' || LTRIM(TRIM(target), '@'))=?)''',
                        (prior['target_user_id'], username, prior['target_user_id'], username))
            self.conn.commit()
            return changed

    def public_matches(self, raw_query, limit=6):
        """Only published records; ID joins use confirmed subject links only."""
        requested_id = None
        alias = None
        raw_value = str(raw_query).strip()
        if raw_value.isdecimal():
            n = int(raw_value)
            requested_id = n if self._validate_user_id(n) else None
        else:
            alias = normalized_username(raw_value)
        if alias is None and requested_id is None:
            raise ValueError('Expected @username or positive user ID')
        with self.lock:
            rows = self.conn.execute('''SELECT DISTINCT c.id,c.target,c.target_user_id,
                       c.candidate_status,c.note,c.timestamp,c.status,c.identity_source
                FROM complaints AS c
                WHERE c.status='accepted'
                  AND ((? IS NOT NULL AND (c.target_user_id = ? OR (c.target_user_id IS NULL AND TRIM(c.target)=?)))
                    OR (? IS NOT NULL AND (
                      LOWER('@' || LTRIM(TRIM(c.target), '@')) = ? OR (c.target_user_id IS NOT NULL AND EXISTS (
                         SELECT 1 FROM telegram_username_history AS h
                         WHERE h.user_id=c.target_user_id AND h.username=? AND h.source!='admin_revoked')))))
                ORDER BY c.id DESC LIMIT ?''',
                (requested_id, requested_id, str(requested_id) if requested_id else None,
                 alias, alias, alias, max(1, min(limit, 30)))).fetchall()
            total = self.conn.execute('''SELECT COUNT(*) AS n FROM complaints AS c
                WHERE c.status='accepted'
                  AND ((? IS NOT NULL AND (c.target_user_id=? OR (c.target_user_id IS NULL AND TRIM(c.target)=?)))
                    OR (? IS NOT NULL AND (LOWER('@' || LTRIM(TRIM(c.target), '@'))=? OR (
                     c.target_user_id IS NOT NULL AND EXISTS(
                      SELECT 1 FROM telegram_username_history h
                      WHERE h.user_id=c.target_user_id AND h.username=? AND h.source!='admin_revoked')))))''',
                (requested_id, requested_id, str(requested_id) if requested_id else None,
                 alias, alias, alias)).fetchone()['n']
            aliases = []
            if alias:
                aliases = [dict(r) for r in self.conn.execute('''SELECT h.user_id,h.is_current,
                    h.last_seen,i.current_username,i.last_checked
                    FROM telegram_username_history h
                    JOIN telegram_identities i ON i.user_id=h.user_id
                    WHERE h.username=? AND h.source!='admin_revoked' AND EXISTS(
                      SELECT 1 FROM complaints c
                      WHERE c.target_user_id=h.user_id AND c.status='accepted')
                    ORDER BY h.last_seen DESC LIMIT 8''', (alias,)).fetchall()]
        return {'total': total, 'rows': [dict(r) for r in rows], 'aliases': aliases,
                'query_alias': alias, 'query_id': requested_id}

    def account(self, user_id):
        with self.lock:
            row = self.conn.execute('SELECT * FROM telegram_identities WHERE user_id=?',
                                    (user_id,)).fetchone()
            return dict(row) if row else None

    def summary(self):
        with self.lock:
            row = self.conn.execute('''SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN target_user_id IS NOT NULL THEN 1 ELSE 0 END) AS linked,
                SUM(CASE WHEN candidate_status='candidate' AND target_user_id IS NULL THEN 1 ELSE 0 END) AS candidates,
                SUM(CASE WHEN identity_checked_at IS NULL AND target_user_id IS NULL THEN 1 ELSE 0 END) AS unscanned,
                SUM(CASE WHEN target_user_id IS NULL AND identity_checked_at IS NOT NULL
                         AND candidate_status!='candidate' THEN 1 ELSE 0 END) AS unresolved
                FROM complaints''').fetchone()
            return {k: int(row[k] or 0) for k in row.keys()}

    def queue(self, page=0, page_size=5):
        with self.lock:
            rows = self.conn.execute('''SELECT id,target,status,target_user_id,
                 candidate_user_id,candidate_username,candidate_status,candidate_checked_at,
                 identity_origin,identity_checked_at
                 FROM complaints WHERE target_user_id IS NULL
                 ORDER BY CASE candidate_status WHEN 'candidate' THEN 0
                        WHEN 'unscanned' THEN 1 ELSE 2 END,id ASC
                 LIMIT ? OFFSET ?''', (page_size, page * page_size)).fetchall()
            total = self.conn.execute('''SELECT COUNT(*) AS total FROM complaints
                 WHERE target_user_id IS NULL''').fetchone()['total']
            return [dict(x) for x in rows], total
