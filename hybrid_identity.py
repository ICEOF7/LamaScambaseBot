"""Passive Bot API identity observations; complaint attribution requires review."""
from datetime import datetime, timezone, timedelta

from identity_db import IdentityStore, normalized_username, utcnow

TRUSTED_SOURCES = {'bot_message', 'bot_callback', 'bot_forward', 'bot_users_shared',
                   'telegram', 'mtproto', 'r2_users_seen', 'r2_history'}


class HybridIdentityStore(IdentityStore):
    def initialize(self):
        super().initialize()
        with self.lock:
            self.conn.executescript('''
                CREATE TABLE IF NOT EXISTS identity_observations (
                    event_key TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                    username TEXT, observed_at TEXT NOT NULL, source TEXT NOT NULL,
                    actor_id INTEGER, received_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_identity_observed_name
                    ON identity_observations(username,user_id);
                CREATE INDEX IF NOT EXISTS idx_identity_observed_user ON identity_observations(user_id);
                CREATE INDEX IF NOT EXISTS idx_complaints_numeric_target ON complaints(TRIM(target));
                CREATE TABLE IF NOT EXISTS identity_candidates (
                    complaint_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                    source TEXT NOT NULL, observed_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'candidate',
                    PRIMARY KEY(complaint_id,user_id));
                CREATE TABLE IF NOT EXISTS identity_review_audit (
                    id INTEGER PRIMARY KEY, complaint_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL, actor_id INTEGER NOT NULL,
                    evidence TEXT NOT NULL, reviewed_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS identity_migrations (
                    version TEXT PRIMARY KEY, applied_at TEXT NOT NULL);
            ''')
            if not self.conn.execute("SELECT 1 FROM identity_migrations WHERE version='r3'").fetchone():
                self._migrating = True
                tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'users_seen' in tables:
                    for r in self.conn.execute('SELECT * FROM users_seen').fetchall():
                        if self._validate_user_id(r['user_id']):
                            self.observe(r['user_id'], r['username'], source='r2_users_seen',
                                         checked_at=r['last_seen'], event_key=f"r2:seen:{r['user_id']}")
                for r in self.conn.execute('SELECT * FROM telegram_username_history').fetchall():
                    if r['source'] in TRUSTED_SOURCES:
                        self.conn.execute('''INSERT OR IGNORE INTO identity_observations
                            VALUES (?,?,?,?,?,NULL,?)''',
                            (f"r2:history:{r['id']}", r['user_id'], r['username'],
                             r['last_seen'], 'r2_history', utcnow()))
                self._migrating = False
                self.reconcile()
                self.conn.execute("INSERT INTO identity_migrations VALUES ('r3',?)", (utcnow(),))
            self.conn.commit()

    def observe(self, user_id, username, *, extra_usernames=(), source='telegram',
                checked_at=None, event_key=None, actor_id=None):
        if source not in TRUSTED_SOURCES or not self._validate_user_id(user_id):
            raise ValueError('Untrusted identity observation')
        observed = checked_at or utcnow()
        dt = datetime.fromisoformat(observed.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        observed = dt.astimezone(timezone.utc).isoformat()
        alias = normalized_username(username)
        event_key = event_key or f'passive:{source}:{user_id}:{alias}:{observed}'
        with self.lock:
            cur = self.conn.execute('''INSERT OR IGNORE INTO identity_observations
                VALUES (?,?,?,?,?,?,?)''',
                (event_key,user_id,alias,observed,source,actor_id,utcnow()))
            if not cur.rowcount:
                return False
            account = self.account(user_id)
            name_latest = self.conn.execute('''SELECT MAX(observed_at) FROM identity_observations
                WHERE username=? AND event_key<>?''', (alias,event_key)).fetchone()[0]
            fresh = (not account or observed >= account['last_checked']) and (
                not name_latest or observed >= name_latest)
            if source == 'bot_users_shared' and not alias and account:
                self.conn.execute('''UPDATE telegram_identities SET
                    last_seen=MAX(last_seen, ?) WHERE user_id=?''',
                    (observed,user_id))
            elif fresh:
                super().observe(user_id,alias,extra_usernames=extra_usernames,
                                source=source,checked_at=observed)
            else:
                self.conn.execute('''INSERT OR IGNORE INTO telegram_identities
                    VALUES (?,NULL,?,?,?,?)''', (user_id,observed,observed,observed,source))
                if alias:
                    self.conn.execute('''INSERT INTO telegram_username_history
                        (user_id,username,first_seen,last_seen,is_current,source) VALUES (?,?,?,?,0,?)
                        ON CONFLICT(user_id,username) DO UPDATE SET
                        first_seen=MIN(first_seen,excluded.first_seen),
                        last_seen=MAX(last_seen,excluded.last_seen)''',
                        (user_id,alias,observed,observed,source))
            if not getattr(self, '_migrating', False):
                self.reconcile(alias=alias, user_id=user_id, event_key=event_key)
            self.conn.commit()
            return True

    def link_direct_report_subject(self, complaint_id, user_id, reporter_id, *,
                                   event_key, source, max_age_minutes=30):
        """Bind a *new report's chosen subject* to an ID supplied by Telegram.

        A report via Telegram's account picker or a visible forwarded sender
        explicitly identifies the account the reporter chose. This does NOT
        verify the allegation. Published status still requires moderation.

        Only accept an observation from the same reporter, same exact incoming
        Telegram event, and a report created within 30 minutes of that event.
        This is never applied to old username-only complaints.
        """
        if (source not in {'bot_users_shared','bot_forward'}
                or not self._validate_user_id(user_id)
                or not self._validate_user_id(reporter_id)
                or not isinstance(event_key,str) or not event_key
                or len(event_key)>160):
            return False
        with self.lock:
            observation=self.conn.execute('''SELECT username, observed_at, received_at FROM identity_observations
                WHERE event_key=? AND user_id=? AND actor_id=? AND source=?''',
                (event_key,user_id,reporter_id,source)).fetchone()
            complaint=self.conn.execute('''SELECT target,from_id,timestamp,identity_origin,target_user_id
                FROM complaints WHERE id=?''',(complaint_id,)).fetchone()
            if (not observation or not complaint or complaint['from_id']!=reporter_id
                    or complaint['identity_origin']!='new'
                    or complaint['target_user_id'] is not None):
                return False
            target=str(complaint['target']).strip()
            alias=normalized_username(target)
            if not (target==str(user_id) or
                    (alias and alias==observation['username'])):
                return False
            try:
                observed_at=datetime.fromisoformat(observation['received_at'].replace('Z','+00:00'))
                created_at=datetime.fromisoformat(complaint['timestamp'].replace('Z','+00:00'))
                if observed_at.tzinfo is None:
                    observed_at=observed_at.replace(tzinfo=timezone.utc)
                if created_at.tzinfo is None:
                    created_at=created_at.replace(tzinfo=timezone.utc)
                gap=created_at.astimezone(timezone.utc)-observed_at.astimezone(timezone.utc)
                if not timedelta(seconds=-2)<=gap<=timedelta(minutes=min(60,max_age_minutes)):
                    return False
            except (TypeError,ValueError,OverflowError):
                return False
            cursor=self.conn.execute('''UPDATE complaints SET target_user_id=?, identity_source=?,
                identity_checked_at=?, candidate_status='linked',
                candidate_user_id=NULL, candidate_username=NULL
                WHERE id=? AND target_user_id IS NULL AND identity_origin='new' ''',
                (user_id,source,observation['observed_at'],complaint_id))
            if cursor.rowcount!=1:
                return False
            self.conn.execute("UPDATE identity_candidates SET status='superseded' "
                              "WHERE complaint_id=? AND user_id<>?", (complaint_id,user_id))
            self.conn.execute('''INSERT INTO identity_candidates
                (complaint_id,user_id,source,observed_at,status)
                VALUES (?,?,?,?,'reported_target')
                ON CONFLICT(complaint_id,user_id) DO UPDATE SET
                    status='reported_target',source=excluded.source,
                    observed_at=excluded.observed_at''',
                (complaint_id,user_id,source,observation['observed_at']))
            self.conn.commit()
            return True

    def reconcile(self, *, alias=None, user_id=None, event_key=None, complaint_id=None):
        """Create review candidates without scanning all observations per update.

        An incoming Telegram event is reconciled by its unique event key;
        a newly submitted complaint is reconciled by its primary key.
        Full backfill runs only for migration and explicit administrator actions.
        """
        alias = normalized_username(alias)
        with self.lock:
            if event_key is not None:
                name_filter, name_params = 'AND o.event_key=?', (event_key,)
                numeric_filter, numeric_params = 'AND o.event_key=?', (event_key,)
                if alias and user_id is not None:
                    scope = "AND (LOWER('@'||LTRIM(TRIM(complaints.target),'@'))=? OR TRIM(complaints.target)=?)"
                    scope_params = (alias, str(user_id))
                elif user_id is not None:
                    scope, scope_params = 'AND TRIM(complaints.target)=?', (str(user_id),)
                else:
                    return
            elif complaint_id is not None:
                name_filter, name_params = 'AND c.id=?', (complaint_id,)
                numeric_filter, numeric_params = 'AND c.id=?', (complaint_id,)
                scope, scope_params = 'AND complaints.id=?', (complaint_id,)
            elif user_id is not None:
                if alias:
                    name_filter, name_params = 'AND o.user_id=? AND o.username=?', (user_id, alias)
                    scope = "AND (LOWER('@'||LTRIM(TRIM(complaints.target),'@'))=? OR TRIM(complaints.target)=?)"
                    scope_params = (alias, str(user_id))
                else:
                    name_filter, name_params = 'AND 0', ()
                    scope, scope_params = 'AND TRIM(complaints.target)=?', (str(user_id),)
                numeric_filter, numeric_params = 'AND o.user_id=?', (user_id,)
            elif alias:
                name_filter, name_params = 'AND o.username=?', (alias,)
                numeric_filter, numeric_params = 'AND 0', ()
                scope = "AND LOWER('@'||LTRIM(TRIM(complaints.target),'@'))=?"
                scope_params = (alias,)
            else:
                name_filter = numeric_filter = ''
                name_params = numeric_params = ()
                scope, scope_params = '', ()

            if complaint_id is not None:
                name_from = numeric_from = 'complaints c CROSS JOIN identity_observations o'
            else:
                name_from = 'identity_observations o CROSS JOIN complaints c INDEXED BY idx_complaints_target_normalized'
                numeric_from = 'identity_observations o CROSS JOIN complaints c INDEXED BY idx_complaints_numeric_target'
            numeric_join = 'TRIM(c.target)=CAST(o.user_id AS TEXT) AND o.user_id=CAST(TRIM(c.target) AS INTEGER)'
            self.conn.execute(f'''INSERT INTO identity_candidates
                (complaint_id,user_id,source,observed_at)
                SELECT id,user_id,source,MAX(observed_at) FROM (
                  SELECT c.id,o.user_id,o.source,o.observed_at
                  FROM {name_from}
                    ON LOWER('@'||LTRIM(TRIM(c.target),'@'))=o.username
                  WHERE c.target_user_id IS NULL {name_filter}
                  UNION ALL
                  SELECT c.id,o.user_id,o.source,o.observed_at
                  FROM {numeric_from}
                    ON {numeric_join}
                  WHERE c.target_user_id IS NULL {numeric_filter}
                ) GROUP BY id,user_id
                ON CONFLICT(complaint_id,user_id) DO UPDATE SET
                  observed_at=MAX(observed_at,excluded.observed_at)''',
                (*name_params, *numeric_params))
            affected = f'''SELECT c.id FROM {name_from}
                ON LOWER('@'||LTRIM(TRIM(c.target),'@'))=o.username
                WHERE c.target_user_id IS NULL {name_filter}
                UNION SELECT c.id FROM {numeric_from}
                ON {numeric_join}
                WHERE c.target_user_id IS NULL {numeric_filter}'''
            affected_params = (*name_params, *numeric_params)
            if complaint_id is not None:
                affected = 'SELECT ?'
                affected_params = (complaint_id,)
            self.conn.execute(f'''UPDATE complaints SET
                candidate_user_id=(SELECT user_id FROM identity_candidates x
                    WHERE x.complaint_id=complaints.id AND x.status='candidate' ORDER BY user_id LIMIT 1),
                candidate_username=CASE WHEN target LIKE '@%' THEN LOWER(target) ELSE NULL END,
                candidate_checked_at=(SELECT MAX(observed_at) FROM identity_candidates x
                    WHERE x.complaint_id=complaints.id AND x.status='candidate'),
                candidate_status='candidate'
                WHERE id IN ({affected}) AND target_user_id IS NULL
                AND EXISTS (SELECT 1 FROM identity_candidates x
                    WHERE x.complaint_id=complaints.id AND x.status='candidate')''', affected_params)
            self.conn.commit()

    def candidates(self, complaint_id):
        with self.lock:
            return [dict(r) for r in self.conn.execute('''SELECT * FROM identity_candidates
                WHERE complaint_id=? AND status='candidate' ORDER BY user_id''', (complaint_id,))]

    def history(self, user_id, limit=12, page=0):
        with self.lock:
            return [dict(r) for r in self.conn.execute('''SELECT * FROM identity_observations
                WHERE user_id=? ORDER BY observed_at DESC LIMIT ? OFFSET ?''', (user_id,limit,max(0,page)*limit))]

    def reviewed_link(self, complaint_id, user_id, actor_id, evidence):
        if not self._validate_user_id(user_id) or not 8 <= len(evidence.strip()) <= 1200:
            raise ValueError('Evidence required')
        with self.lock:
            row = self.conn.execute('SELECT target,target_user_id FROM complaints WHERE id=?',
                                    (complaint_id,)).fetchone()
            if not row or row['target_user_id'] is not None:
                return False
            self.conn.execute('''INSERT INTO identity_review_audit
                (complaint_id,user_id,actor_id,evidence,reviewed_at) VALUES (?,?,?,?,?)''',
                (complaint_id,user_id,actor_id,evidence.strip(),utcnow()))
            self.conn.execute('''UPDATE complaints SET target_user_id=?,identity_source='admin_evidence',
                identity_checked_at=?,candidate_status='linked' WHERE id=? AND target_user_id IS NULL''',
                (user_id,utcnow(),complaint_id))
            self.conn.execute("UPDATE identity_candidates SET status='reviewed' WHERE complaint_id=?", (complaint_id,))
            self.record_historical_alias(user_id,row['target'])
            self.conn.commit()
            return True

    def reject_candidate(self, complaint_id):
        with self.lock:
            self.conn.execute("UPDATE identity_candidates SET status='rejected' WHERE complaint_id=?", (complaint_id,))
            return super().reject_candidate(complaint_id)

    def manual_link(self, complaint_id, user_id):
        with self.lock:
            row = self.conn.execute('SELECT target_user_id FROM complaints WHERE id=?', (complaint_id,)).fetchone()
            if row and row['target_user_id'] is not None:
                return False
            return super().manual_link(complaint_id,user_id)

    def unlink(self, complaint_id):
        with self.lock:
            row = self.conn.execute('SELECT target_user_id FROM complaints WHERE id=?', (complaint_id,)).fetchone()
            if not row or row['target_user_id'] is None:
                return False
            self.conn.execute('''INSERT INTO identity_review_audit
                (complaint_id,user_id,actor_id,evidence,reviewed_at) VALUES (?,?,0,?,?)''',
                (complaint_id,row['target_user_id'],'Unlinked by administrator; prior association retained',utcnow()))
            self.conn.execute('''UPDATE complaints SET target_user_id=NULL,
                identity_source='legacy',candidate_status='unscanned',identity_checked_at=NULL,
                candidate_user_id=NULL,candidate_username=NULL,candidate_checked_at=NULL WHERE id=?''', (complaint_id,))
            self.conn.execute('''UPDATE telegram_username_history SET source='admin_revoked'
                WHERE user_id=? AND source='admin_verified' AND NOT EXISTS
                (SELECT 1 FROM complaints c WHERE c.target_user_id=telegram_username_history.user_id
                 AND LOWER(c.target)=telegram_username_history.username)''', (row['target_user_id'],))
            self.conn.commit()
            return True

    def record_historical_alias(self, user_id, username, *, source='admin_verified'):
        super().record_historical_alias(user_id,username,source=source)
        with self.lock:
            self.conn.execute('''UPDATE telegram_username_history SET source=?
                WHERE user_id=? AND username=? AND source='admin_revoked' ''',
                (source,user_id,normalized_username(username)))
            self.conn.commit()

    def lookup_observed(self, reference, limit=8):
        """Local admin lookup only: ID observations, never a username network resolver.

        A matching historic alias does not prove ownership today or establish
        that the account was the subject of any complaint.
        """
        value = str(reference or '').strip()
        alias = normalized_username(value)
        is_numeric = value.isdecimal()
        if is_numeric:
            numeric = int(value)
            if not self._validate_user_id(numeric):
                return []
        elif not alias:
            return []
        limit = max(1, min(int(limit), 20))
        with self.lock:
            if is_numeric:
                rows = self.conn.execute("""SELECT o.user_id,
                    MAX(o.observed_at) AS last_seen, COUNT(*) AS observations,
                    i.current_username
                    FROM identity_observations o
                    LEFT JOIN telegram_identities i ON i.user_id=o.user_id
                    WHERE o.user_id=? GROUP BY o.user_id LIMIT 1""", (numeric,)).fetchall()
            else:
                rows = self.conn.execute("""SELECT o.user_id,
                    MAX(o.observed_at) AS last_seen, COUNT(*) AS observations,
                    i.current_username
                    FROM identity_observations o
                    LEFT JOIN telegram_identities i ON i.user_id=o.user_id
                    WHERE o.username=? GROUP BY o.user_id
                    ORDER BY last_seen DESC, o.user_id LIMIT ?""", (alias,limit)).fetchall()
            return [dict(row) for row in rows]

    def summary(self):
        result = super().summary()
        with self.lock:
            result['observed_ids'] = self.conn.execute('SELECT COUNT(DISTINCT user_id) FROM identity_observations').fetchone()[0]
            result['observations'] = self.conn.execute('SELECT COUNT(*) FROM identity_observations').fetchone()[0]
            result['unknown'] = self.conn.execute('''SELECT COUNT(*) FROM complaints
                WHERE target_user_id IS NULL AND candidate_status!='candidate' ''').fetchone()[0]
            result['unscanned'] = self.conn.execute('''SELECT COUNT(*) FROM complaints
                WHERE target_user_id IS NULL AND identity_checked_at IS NULL
                  AND candidate_status!='candidate' ''').fetchone()[0]
        return result


def message_time(message):
    value = getattr(message, 'date', None)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return datetime.fromtimestamp(value,timezone.utc).isoformat() if value else utcnow()


def observe_message(store, message):
    user = getattr(message,'from_user',None)
    chat = getattr(message,'chat',None)
    if not user or getattr(message,'sender_chat',None):
        return
    key = f"message:{chat.id}:{message.message_id}"
    when = message_time(message)
    store.observe(user.id,getattr(user,'username',None),source='bot_message',
                  checked_at=when,event_key=key,actor_id=user.id)
    origin = getattr(message,'forward_origin',None)
    sender = getattr(origin,'sender_user',None) if getattr(origin,'type',None)=='user' else None
    if sender:
        store.observe(sender.id,getattr(sender,'username',None),source='bot_forward',
                      checked_at=when,event_key=key+':forward',actor_id=user.id)


def shared_user(message, expected_request_id):
    shared = getattr(message,'users_shared',None)
    if not shared or shared.request_id != expected_request_id:
        return None
    users = shared.users
    return users[0] if len(users)==1 else None
