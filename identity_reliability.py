"""Transactional identity ledger and durable, replay-safe complaint forms."""
import hashlib
import json
import secrets
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from hybrid_identity import HybridIdentityStore
from identity_db import normalized_username, utcnow


class _Connection:
    """Legacy store helpers may commit; outer identity transactions own commit."""
    def __init__(self, real, owner):
        self.real, self.owner = real, owner

    def __getattr__(self, name):
        return getattr(self.real, name)

    def commit(self):
        if not self.owner._depth:
            self.real.commit()


class ReliableIdentityStore(HybridIdentityStore):
    def __init__(self, conn, lock, admin_id=None):
        super().__init__(conn, lock)
        self._real, self._depth, self.admin_id = conn, 0, admin_id
        self.conn = _Connection(conn, self)

    @contextmanager
    def transaction(self):
        with self.lock:
            name = 'identity_' + str(self._depth)
            self._real.execute('SAVEPOINT '+name)
            self._depth += 1
            try:
                yield
                self._real.execute('RELEASE SAVEPOINT '+name)
            except BaseException:
                self._real.execute('ROLLBACK TO SAVEPOINT '+name)
                self._real.execute('RELEASE SAVEPOINT '+name)
                raise
            finally:
                self._depth -= 1

    def initialize(self):
        super().initialize()
        with self.lock:
            self._real.executescript('''
                CREATE INDEX IF NOT EXISTS idx_identity_observed_time
                    ON identity_observations(username,observed_at);
                CREATE INDEX IF NOT EXISTS idx_identity_observed_user_time
                    ON identity_observations(user_id,username,observed_at);
                CREATE INDEX IF NOT EXISTS idx_identity_history_user
                    ON telegram_username_history(user_id);
                CREATE INDEX IF NOT EXISTS idx_identity_history_current
                    ON telegram_username_history(username,is_current);
                CREATE INDEX IF NOT EXISTS idx_identity_history_current_name
                    ON telegram_identities(current_username);
                CREATE TABLE IF NOT EXISTS identity_forms (
                    token TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                    request_id INTEGER NOT NULL, opened_at REAL NOT NULL,
                    expires_at REAL NOT NULL, state_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active', complaint_id INTEGER);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_identity_forms_active
                    ON identity_forms(user_id) WHERE status='active';
                CREATE TABLE IF NOT EXISTS identity_event_claims (
                    event_key TEXT PRIMARY KEY, form_token TEXT,
                    user_id INTEGER NOT NULL, selected_id INTEGER NOT NULL,
                    source TEXT NOT NULL, claimed_at REAL NOT NULL,
                    complaint_id INTEGER);
                CREATE TABLE IF NOT EXISTS identity_subject_ledger (
                    complaint_id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
                    source TEXT NOT NULL, event_key TEXT, subject_fingerprint TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1, saved_at TEXT NOT NULL);
            ''')
            cols = {r['name'] for r in self.conn.execute('PRAGMA table_info(identity_review_audit)')}
            for name, definition in {'action': "TEXT NOT NULL DEFAULT 'link'",
                                     'previous_user_id': 'INTEGER'}.items():
                if name not in cols:
                    self.conn.execute(f'ALTER TABLE identity_review_audit ADD COLUMN {name} {definition}')
            self._real.commit()
        with self.transaction():
            if not self.conn.execute("SELECT 1 FROM identity_migrations WHERE version='r34'").fetchone():
                self.reconcile()
                self.conn.execute("INSERT INTO identity_migrations VALUES ('r34',?)",(utcnow(),))
        self.recover_known_links()

    def observe(self, user_id, username, **kwargs):
        with self.transaction():
            changed = super().observe(user_id, username, **kwargs)
            if changed:
                self.conn.execute('''UPDATE telegram_identities SET first_seen=MIN(first_seen,
                    (SELECT MIN(observed_at) FROM identity_observations WHERE user_id=?)) WHERE user_id=?''',
                    (user_id,user_id))
                self.conn.execute('''UPDATE telegram_username_history SET first_seen=MIN(first_seen,
                    (SELECT MIN(observed_at) FROM identity_observations o
                     WHERE o.user_id=telegram_username_history.user_id AND o.username=telegram_username_history.username))
                    WHERE user_id=? AND username=?''',(user_id,normalized_username(username)))
            if not changed:
                key = kwargs.get('event_key')
                event = self.conn.execute('SELECT * FROM identity_observations WHERE event_key=?', (key,)).fetchone()
                if event:
                    self.reconcile(alias=event['username'],user_id=event['user_id'],event_key=key)
            return changed

    def reconcile(self, **kwargs):
        with self.transaction():
            return super().reconcile(**kwargs)

    def open_form(self, user_id, request_id, state):
        with self.transaction():
            self.close_form(user_id)
            token = secrets.token_hex(16)
            state['_form_token'] = token
            state['request_id'] = request_id
            now = time.time()
            self.conn.execute('INSERT INTO identity_forms VALUES (?,?,?,?,?,?,?,NULL)',
                (token,user_id,request_id,now,now+3600,self._payload(state),'active'))
            return token

    @staticmethod
    def _payload(state):
        return json.dumps({k:v for k,v in state.items() if k!='_created'},ensure_ascii=False)

    def close_form(self, user_id):
        with self.transaction():
            if self.conn.execute("SELECT 1 FROM identity_forms WHERE user_id=? AND status='active'",(user_id,)).fetchone():
                self.conn.execute("UPDATE identity_forms SET status='cancelled' WHERE user_id=? AND status='active'",(user_id,))

    def save_form(self, user_id, state):
        token=state.get('_form_token')
        if token:
            with self.transaction():
                self.conn.execute('''UPDATE identity_forms SET state_json=?
                    WHERE token=? AND user_id=? AND status='active' AND expires_at>?''',
                    (self._payload(state),token,user_id,time.time()))

    def load_form(self, user_id):
        with self.transaction():
            row=self.conn.execute("SELECT * FROM identity_forms WHERE user_id=? AND status='active'",(user_id,)).fetchone()
            if not row:return {}
            try:
                data=json.loads(row['state_json'])
                valid=(isinstance(data,dict) and data.get('_form_token')==row['token']
                       and data.get('state') in {'waiting_for_report_target','waiting_for_report_note','waiting_for_report_evidence'}
                       and row['expires_at']>time.time())
            except (ValueError,TypeError):valid=False
            if not valid:
                self.conn.execute("UPDATE identity_forms SET status='expired' WHERE token=?",(row['token'],))
                return {}
            data['_created']=time.monotonic()-(3600-(row['expires_at']-time.time()))
            return data

    def claim_selection(self, user_id, state, event_key, selected_id, source, request_id=None):
        with self.transaction():
            form=self.conn.execute("SELECT * FROM identity_forms WHERE token=? AND user_id=? AND status='active'",
                                   (state.get('_form_token'),user_id)).fetchone()
            event=self.conn.execute('''SELECT * FROM identity_observations
                WHERE event_key=? AND user_id=? AND actor_id=? AND source=?''',
                (event_key,selected_id,user_id,source)).fetchone()
            if (not form or not event or form['expires_at']<=time.time()
                    or (source=='bot_users_shared' and form['request_id']!=request_id)):
                return False
            try:
                received=datetime.fromisoformat(event['received_at']).timestamp()
                emitted=datetime.fromisoformat(event['observed_at']).timestamp()
            except (ValueError,TypeError):return False
            if received<form['opened_at']-0.001 or emitted<form['opened_at']-2:return False
            prior=self.conn.execute('SELECT * FROM identity_event_claims WHERE event_key=?',(event_key,)).fetchone()
            if prior:
                return prior['form_token']==form['token'] and prior['complaint_id'] is None and prior['selected_id']==selected_id
            self.conn.execute('INSERT INTO identity_event_claims VALUES (?,?,?,?,?,?,NULL)',
                (event_key,form['token'],user_id,selected_id,source,time.time()))
            return True

    @staticmethod
    def _fingerprint(row):
        return hashlib.sha256(json.dumps([row['target'],row['from_id'],row['timestamp']],ensure_ascii=False).encode()).hexdigest()

    def _ledger(self, complaint_id, user_id, source, event_key=None):
        row=self.conn.execute('SELECT * FROM complaints WHERE id=?',(complaint_id,)).fetchone()
        self.conn.execute('''INSERT INTO identity_subject_ledger VALUES (?,?,?,?,?,1,?)
            ON CONFLICT(complaint_id) DO UPDATE SET user_id=excluded.user_id,
            source=excluded.source,event_key=excluded.event_key,
            subject_fingerprint=excluded.subject_fingerprint,active=1,saved_at=excluded.saved_at''',
            (complaint_id,user_id,source,event_key,self._fingerprint(row),utcnow()))

    def link_direct_report_subject(self, complaint_id, user_id, reporter_id, *, event_key, source):
        with self.transaction():
            claim=self.conn.execute('SELECT * FROM identity_event_claims WHERE event_key=?',(event_key,)).fetchone()
            if claim and claim['complaint_id'] not in (None,complaint_id):return False
            max_age=30
            if claim and claim['form_token']:
                form=self.conn.execute("SELECT * FROM identity_forms WHERE token=? AND status='active'",(claim['form_token'],)).fetchone()
                if not form or form['expires_at']<time.time():return False
                max_age=60
            linked=super().link_direct_report_subject(complaint_id,user_id,reporter_id,
                         event_key=event_key,source=source,max_age_minutes=max_age)
            if linked:
                self.conn.execute('''INSERT INTO identity_event_claims VALUES (?,NULL,?,?,?,?,?)
                    ON CONFLICT(event_key) DO UPDATE SET complaint_id=excluded.complaint_id''',
                    (event_key,reporter_id,user_id,source,time.time(),complaint_id))
                self._ledger(complaint_id,user_id,source,event_key)
            return linked

    def create_report(self, data, user_id, username, note, evidence, status):
        with self.transaction():
            token=data.get('_form_token')
            if token:
                form=self.conn.execute('SELECT * FROM identity_forms WHERE token=? AND user_id=?',(token,user_id)).fetchone()
                if form and form['status']=='submitted':return form['complaint_id'],False
                if not form or form['status']!='active' or form['expires_at']<time.time():
                    raise ValueError('Form expired')
            timestamp=utcnow()
            cur=self.conn.execute('''INSERT INTO complaints
                (target,from_user,from_id,note,evidence,timestamp,status,identity_origin)
                VALUES (?,?,?,?,?,?,?,'new')''',
                (data['target'],username,user_id,note,json.dumps(evidence),timestamp,status))
            cid=cur.lastrowid
            if data.get('selected_id'):
                claim=self.conn.execute('''SELECT * FROM identity_event_claims
                    WHERE event_key=? AND form_token=? AND user_id=? AND selected_id=? AND complaint_id IS NULL''',
                    (data.get('selection_event_key'),token,user_id,data['selected_id'])).fetchone()
                if not claim or not self.link_direct_report_subject(cid,data['selected_id'],user_id,
                    event_key=claim['event_key'],source=claim['source']):
                    raise ValueError('Selection could not be verified; complaint not saved')
            self.reconcile(complaint_id=cid)
            if token:
                self.conn.execute("UPDATE identity_forms SET status='submitted',complaint_id=? WHERE token=?",(cid,token))
            return cid,True

    def _admin(self, actor_id):
        if self.admin_id is not None and actor_id!=self.admin_id:
            raise PermissionError('Administrator required')

    def _audit(self, cid, uid, actor_id, evidence, action, previous=None):
        self.conn.execute('''INSERT INTO identity_review_audit
            (complaint_id,user_id,actor_id,evidence,reviewed_at,action,previous_user_id)
            VALUES (?,?,?,?,?,?,?)''',(cid,uid,actor_id or 0,evidence,utcnow(),action,previous))

    def reviewed_link(self, complaint_id, user_id, actor_id, evidence):
        self._admin(actor_id)
        with self.transaction():
            linked=super().reviewed_link(complaint_id,user_id,actor_id,evidence)
            if linked:
                self.conn.execute('UPDATE complaints SET candidate_user_id=NULL,candidate_username=NULL WHERE id=?',(complaint_id,))
                self._ledger(complaint_id,user_id,'admin_evidence')
            return linked

    def manual_link(self, complaint_id, user_id, *, actor_id=None, evidence=None):
        if actor_id is None or evidence is None:raise PermissionError('Administrator and evidence required')
        return self.reviewed_link(complaint_id,user_id,actor_id,evidence)

    def approve_candidate(self, complaint_id, *, actor_id=None, evidence=None, max_age_days=7):
        if actor_id is None or evidence is None:raise PermissionError('Administrator and evidence required')
        self._admin(actor_id)
        with self.transaction():
            row=self.conn.execute("SELECT candidate_user_id FROM complaints WHERE id=? AND target_user_id IS NULL AND candidate_status='candidate'",(complaint_id,)).fetchone()
            return self.reviewed_link(complaint_id,row['candidate_user_id'],actor_id,evidence) if row and row['candidate_user_id'] else False

    def record_historical_alias(self, user_id, username, **kwargs):
        if str(username or '').strip().isdecimal():return
        with self.transaction():return super().record_historical_alias(user_id,username,**kwargs)

    def apply_scan(self, target, resolved, **kwargs):
        with self.transaction():return super().apply_scan(target,resolved,**kwargs)

    def reject_candidate(self, complaint_id, actor_id=None, evidence='Кандидат отклонён'):
        self._admin(actor_id)
        if actor_id is not None and not 8<=len(evidence.strip())<=1200:raise ValueError('Evidence required')
        with self.transaction():
            choices=self.candidates(complaint_id)
            current=self.conn.execute('SELECT candidate_user_id FROM complaints WHERE id=?',(complaint_id,)).fetchone()
            changed=super().reject_candidate(complaint_id)
            if changed:
                if not choices:choices=[{'user_id':current['candidate_user_id'] or 0}]
                for row in choices:self._audit(complaint_id,row['user_id'],actor_id,evidence,'reject')
            return changed

    def unlink(self, complaint_id, actor_id=None, evidence='Ошибочная связь отменена', expected_user_id=None):
        self._admin(actor_id)
        if actor_id is not None and not 8<=len(evidence.strip())<=1200:raise ValueError('Evidence required')
        with self.transaction():
            row=self.conn.execute('SELECT target_user_id FROM complaints WHERE id=?',(complaint_id,)).fetchone()
            if not row or not row['target_user_id'] or (expected_user_id is not None and row['target_user_id']!=expected_user_id):return False
            changed=super().unlink(complaint_id)
            if changed:
                self.conn.execute('''UPDATE identity_review_audit SET actor_id=?,evidence=?,action='unlink',previous_user_id=?
                    WHERE id=(SELECT MAX(id) FROM identity_review_audit WHERE complaint_id=?)''',
                    (actor_id or 0,evidence,row['target_user_id'],complaint_id))
                self.conn.execute('UPDATE identity_subject_ledger SET active=0 WHERE complaint_id=?',(complaint_id,))
                self.conn.execute("UPDATE identity_candidates SET status='revoked' WHERE complaint_id=?",(complaint_id,))
            return changed

    def reset_scan(self, complaint_id, actor_id=None, evidence='Повторная проверка сохранённых данных'):
        self._admin(actor_id)
        if actor_id is not None and not 8<=len(evidence.strip())<=1200:raise ValueError('Evidence required')
        with self.transaction():
            changed=super().reset_scan(complaint_id)
            if changed:
                self.conn.execute("UPDATE identity_candidates SET status='candidate' WHERE complaint_id=? AND status='rejected'",(complaint_id,))
                self.reconcile(complaint_id=complaint_id)
                self._audit(complaint_id,0,actor_id,evidence,'recheck')
            return changed

    def recover_known_links(self):
        restored=0
        with self.transaction():
            rows=self.conn.execute('''SELECT c.*,l.user_id AS ledger_user,l.source AS ledger_source,
                l.subject_fingerprint FROM identity_subject_ledger l JOIN complaints c ON c.id=l.complaint_id
                WHERE l.active=1 AND c.target_user_id IS NULL''').fetchall()
            for row in rows:
                if self._fingerprint(row)!=row['subject_fingerprint']:continue
                self.conn.execute('''UPDATE complaints SET target_user_id=?,identity_source=?,candidate_status='linked',
                    identity_checked_at=? WHERE id=? AND target_user_id IS NULL''',
                    (row['ledger_user'],row['ledger_source'],utcnow(),row['id']))
                self._audit(row['id'],row['ledger_user'],0,'Восстановлено из сохранённой привязки этой жалобы','recover')
                restored+=1
        return restored

    def clarify(self, complaint_id, user_id, state):
        with self.transaction():
            row=self.conn.execute('SELECT * FROM complaints WHERE id=?',(complaint_id,)).fetchone()
            if not row or row['target_user_id'] or user_id not in (row['from_id'],self.admin_id):return False
            claim=self.conn.execute('''SELECT * FROM identity_event_claims WHERE event_key=?
                AND form_token=? AND user_id=? AND complaint_id IS NULL''',
                (state['selection_event_key'],state['_form_token'],user_id)).fetchone()
            if not claim:return False
            self.conn.execute('''INSERT INTO identity_candidates VALUES (?,?,?,?,'candidate')
                ON CONFLICT(complaint_id,user_id) DO UPDATE SET status='candidate',source=excluded.source,
                observed_at=excluded.observed_at''',(complaint_id,claim['selected_id'],'reporter_clarification',utcnow()))
            self.reconcile(complaint_id=complaint_id)
            self.conn.execute('UPDATE identity_event_claims SET complaint_id=? WHERE event_key=?',(complaint_id,claim['event_key']))
            self.conn.execute("UPDATE identity_forms SET status='submitted',complaint_id=? WHERE token=?",(complaint_id,state['_form_token']))
            self._audit(complaint_id,claim['selected_id'],user_id,'Автор уточнил аккаунт; требуется модерация','clarify')
            return True
