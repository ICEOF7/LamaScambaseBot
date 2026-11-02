"""Additive, restart-safe SQLite persistence for broadcasts and private tickets."""
from __future__ import annotations
import json
import time
from contextlib import contextmanager


class CommunityStore:
    def __init__(self, conn, lock, admin_id: int):
        self.conn, self.lock, self.admin_id = conn, lock, admin_id

    @contextmanager
    def transaction(self):
        with self.lock:
            with self.conn:
                yield self.conn

    def initialize(self):
        with self.lock:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS ui_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS users_seen (
                    user_id INTEGER PRIMARY KEY, username TEXT, first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL, last_chat_id INTEGER);
                CREATE TABLE IF NOT EXISTS broadcast_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER NOT NULL,
                    payload TEXT, buttons TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'draft',
                    revision INTEGER NOT NULL DEFAULT 0, preview_revision INTEGER NOT NULL DEFAULT -1,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    not_before REAL NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS broadcast_deliveries (
                    job_id INTEGER NOT NULL REFERENCES broadcast_jobs(id), user_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL DEFAULT 0, telegram_message_id INTEGER,
                    last_error TEXT NOT NULL DEFAULT '', PRIMARY KEY(job_id,user_id));
                CREATE INDEX IF NOT EXISTS broadcast_ready ON broadcast_deliveries(status,next_attempt,job_id);
                CREATE TABLE IF NOT EXISTS support_tickets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER NOT NULL, subject TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'general', status TEXT NOT NULL DEFAULT 'waiting_admin',
                    created_at REAL NOT NULL, updated_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS tickets_by_owner ON support_tickets(owner_id,id);
                CREATE TABLE IF NOT EXISTS support_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER NOT NULL REFERENCES support_tickets(id),
                    author_id INTEGER NOT NULL, role TEXT NOT NULL, payload TEXT NOT NULL,
                    source_chat INTEGER NOT NULL, source_message INTEGER NOT NULL, created_at REAL NOT NULL,
                    UNIQUE(source_chat,source_message));
                CREATE INDEX IF NOT EXISTS ticket_history ON support_messages(ticket_id,id);
                CREATE TABLE IF NOT EXISTS support_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER NOT NULL REFERENCES support_tickets(id),
                    actor_id INTEGER NOT NULL, action TEXT NOT NULL, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS support_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, message_id INTEGER NOT NULL REFERENCES support_messages(id),
                    recipient_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                    telegram_message_id INTEGER, last_error TEXT NOT NULL DEFAULT '',
                    UNIQUE(message_id,recipient_id));
            """)
            existing = {r[1] for r in self.conn.execute("PRAGMA table_info(users_seen)")}
            for name in ("notify_enabled", "broadcast_blocked"):
                if name not in existing:
                    default = 1 if name == "notify_enabled" else 0
                    self.conn.execute(f"ALTER TABLE users_seen ADD COLUMN {name} INTEGER NOT NULL DEFAULT {default}")
            self.conn.commit()

    def setting(self, key, default=""):
        with self.lock:
            row = self.conn.execute("SELECT value FROM ui_settings WHERE key=?", (key,)).fetchone()
            return row[0] if row else default

    def set_setting(self, key, value):
        with self.transaction() as c:
            c.execute("INSERT INTO ui_settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def touch_user(self, uid: int):
        with self.transaction() as c:
            c.execute("UPDATE users_seen SET broadcast_blocked=0 WHERE user_id=? AND last_chat_id=?", (uid, uid))

    def notifications(self, uid: int) -> bool:
        with self.lock:
            row = self.conn.execute("SELECT notify_enabled FROM users_seen WHERE user_id=?", (uid,)).fetchone()
            return bool(row[0]) if row else True

    def set_notifications(self, uid: int, enabled: bool):
        with self.transaction() as c:
            c.execute("UPDATE users_seen SET notify_enabled=? WHERE user_id=?", (int(enabled), uid))

    def mark_blocked(self, uid: int, blocked=True):
        with self.transaction() as c:
            c.execute("UPDATE users_seen SET broadcast_blocked=? WHERE user_id=?", (int(blocked), uid))

    def audience_count(self) -> int:
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM users_seen WHERE user_id>0 AND last_chat_id=user_id "
                                     "AND notify_enabled=1 AND broadcast_blocked=0").fetchone()[0]

    def _admin(self, actor):
        if actor != self.admin_id:
            raise PermissionError("Нет доступа.")

    @staticmethod
    def _job(row):
        if not row:
            raise ValueError("Рассылка не найдена.")
        result = dict(row)
        result["payload"] = json.loads(result["payload"]) if result["payload"] else None
        result["buttons"] = json.loads(result["buttons"])
        return result

    def create_broadcast(self, actor: int) -> int:
        self._admin(actor)
        now = time.time()
        with self.transaction() as c:
            if c.execute("SELECT COUNT(*) FROM broadcast_jobs WHERE status='draft'").fetchone()[0] >= 20:
                raise ValueError("Сначала отмени ненужные черновики: можно хранить до 20 одновременно.")
            return c.execute("INSERT INTO broadcast_jobs(owner_id,created_at,updated_at) VALUES (?,?,?)",
                             (actor, now, now)).lastrowid

    def job(self, jid: int, actor: int) -> dict:
        self._admin(actor)
        with self.lock:
            result = self._job(self.conn.execute("SELECT * FROM broadcast_jobs WHERE id=?", (jid,)).fetchone())
            result["counts"] = {r[0]: r[1] for r in self.conn.execute(
                "SELECT status,COUNT(*) FROM broadcast_deliveries WHERE job_id=? GROUP BY status", (jid,))}
            result["total"] = sum(result["counts"].values())
            return result

    def jobs(self, actor: int, page=0, size=6):
        self._admin(actor)
        page = max(0, min(int(page), 100000))
        with self.lock:
            return [dict(r) for r in self.conn.execute("SELECT id,status,created_at FROM broadcast_jobs ORDER BY id DESC LIMIT ? OFFSET ?",
                                                      (size, page*size))]

    def save_draft(self, jid: int, actor: int, *, payload=None, buttons=None):
        self._admin(actor)
        with self.transaction() as c:
            current = self._job(c.execute("SELECT * FROM broadcast_jobs WHERE id=?", (jid,)).fetchone())
            if current["status"] != "draft":
                raise ValueError("Рассылка уже запущена. Изменять можно только черновик.")
            c.execute("UPDATE broadcast_jobs SET payload=?,buttons=?,revision=revision+1,preview_revision=-1,updated_at=? WHERE id=?",
                      (json.dumps(payload if payload is not None else current["payload"], ensure_ascii=False),
                       json.dumps(buttons if buttons is not None else current["buttons"], ensure_ascii=False), time.time(), jid))

    def mark_preview(self, jid, actor, revision):
        self._admin(actor)
        with self.transaction() as c:
            return c.execute("UPDATE broadcast_jobs SET preview_revision=? WHERE id=? AND status='draft' AND revision=?",
                             (revision, jid, revision)).rowcount == 1

    def enqueue(self, jid: int, actor: int, revision: int) -> int:
        self._admin(actor)
        with self.transaction() as c:
            job = self._job(c.execute("SELECT * FROM broadcast_jobs WHERE id=?", (jid,)).fetchone())
            if job["status"] != "draft":
                raise ValueError("Эта рассылка уже запущена или отменена.")
            if not job["payload"] or job["preview_revision"] != job["revision"] or revision != job["revision"]:
                raise ValueError("Сначала открой предпросмотр текущего текста и кнопок.")
            c.execute("INSERT INTO broadcast_deliveries(job_id,user_id) SELECT ?,user_id FROM users_seen "
                      "WHERE user_id>0 AND last_chat_id=user_id AND notify_enabled=1 AND broadcast_blocked=0", (jid,))
            total = c.execute("SELECT COUNT(*) FROM broadcast_deliveries WHERE job_id=?", (jid,)).fetchone()[0]
            if not total:
                raise ValueError("Пока нет доступных получателей. Пользователи должны открыть личный чат с ботом.")
            c.execute("UPDATE broadcast_jobs SET status='queued',updated_at=?,last_error='' WHERE id=?", (time.time(), jid))
            return total

    def control_job(self, jid: int, actor: int, action: str):
        self._admin(actor)
        allowed = {"pause": {"queued", "running"}, "resume": {"paused"},
                   "cancel": {"draft", "queued", "running", "paused"}}
        new = {"pause":"paused", "resume":"queued", "cancel":"cancelled"}
        if action not in allowed:
            raise ValueError("Неизвестное действие.")
        with self.transaction() as c:
            row = c.execute("SELECT status FROM broadcast_jobs WHERE id=?", (jid,)).fetchone()
            if not row or row[0] not in allowed[action]:
                raise ValueError("Статус уже изменился. Обнови экран рассылки.")
            c.execute("UPDATE broadcast_jobs SET status=?,updated_at=? WHERE id=?", (new[action], time.time(), jid))
            if action == "cancel":
                c.execute("UPDATE broadcast_deliveries SET status='skipped',last_error='cancelled' WHERE job_id=? AND status='pending'", (jid,))

    def recover(self):
        """A crash after send but before COMMIT is ambiguous, never auto-resend it."""
        with self.transaction() as c:
            c.execute("UPDATE broadcast_jobs SET status='paused',last_error='restart_ambiguous' WHERE status IN ('running','queued') "
                      "AND id IN (SELECT job_id FROM broadcast_deliveries WHERE status='inflight')")
            c.execute("UPDATE broadcast_deliveries SET status='uncertain',last_error='restart_ambiguous' WHERE status='inflight'")
            c.execute("UPDATE broadcast_jobs SET status='queued' WHERE status='running'")
            c.execute("UPDATE support_outbox SET status='uncertain',last_error='restart_ambiguous' WHERE status='inflight'")

    def claim_broadcast(self, now=None):
        now = time.time() if now is None else now
        with self.transaction() as c:
            c.execute("UPDATE broadcast_deliveries SET status='skipped',last_error='opted_out_or_blocked' WHERE status='pending' "
                      "AND user_id IN (SELECT user_id FROM users_seen WHERE notify_enabled=0 OR broadcast_blocked=1)")
            c.execute("UPDATE broadcast_jobs SET status='completed',updated_at=? WHERE status IN ('queued','running') "
                      "AND NOT EXISTS (SELECT 1 FROM broadcast_deliveries d WHERE d.job_id=broadcast_jobs.id AND d.status IN ('pending','inflight'))", (now,))
            row = c.execute("SELECT d.job_id,d.user_id,d.attempts,j.payload,j.buttons FROM broadcast_deliveries d "
                            "JOIN broadcast_jobs j ON j.id=d.job_id WHERE d.status='pending' AND d.next_attempt<=? "
                            "AND j.status IN ('queued','running') AND j.not_before<=? ORDER BY j.id,d.user_id LIMIT 1", (now, now)).fetchone()
            if not row:
                return None
            c.execute("UPDATE broadcast_deliveries SET status='inflight',attempts=attempts+1 WHERE job_id=? AND user_id=?",
                      (row["job_id"], row["user_id"]))
            c.execute("UPDATE broadcast_jobs SET status='running' WHERE id=?", (row["job_id"],))
            result = dict(row)
            result["attempts"] += 1
            result["payload"], result["buttons"] = json.loads(result["payload"]), json.loads(result["buttons"])
            return result

    def broadcast_still_allowed(self, job):
        with self.lock:
            row = self.conn.execute("SELECT j.status,u.notify_enabled,u.broadcast_blocked FROM broadcast_jobs j,users_seen u "
                                    "WHERE j.id=? AND u.user_id=?", (job["job_id"], job["user_id"])).fetchone()
            return bool(row and row[0] in {"queued", "running"} and row[1] and not row[2])

    def finish_broadcast(self, item, status, *, error="", message_id=None, retry_at=0, pause=False):
        if status not in {"sent", "pending", "blocked", "failed", "uncertain", "skipped"}:
            raise ValueError("Bad delivery status")
        with self.transaction() as c:
            c.execute("UPDATE broadcast_deliveries SET status=?,last_error=?,telegram_message_id=?,next_attempt=? "
                      "WHERE job_id=? AND user_id=? AND status='inflight'",
                      (status, error, message_id, retry_at, item["job_id"], item["user_id"]))
            if pause:
                c.execute("UPDATE broadcast_jobs SET status='paused',last_error=? WHERE id=? AND status IN ('running','queued')", (error, item["job_id"]))
            if status == "blocked":
                c.execute("UPDATE users_seen SET broadcast_blocked=1 WHERE user_id=?", (item["user_id"],))
            if retry_at:
                c.execute("UPDATE broadcast_jobs SET not_before=MAX(not_before,?) WHERE id=?", (retry_at, item["job_id"]))

    def release_broadcast(self, item):
        with self.transaction() as c:
            row = c.execute("SELECT status FROM broadcast_jobs WHERE id=?", (item["job_id"],)).fetchone()
            status = "skipped" if not row or row[0] == "cancelled" else "pending"
            c.execute("UPDATE broadcast_deliveries SET status=?,attempts=MAX(0,attempts-1) WHERE job_id=? AND user_id=? AND status='inflight'",
                      (status, item["job_id"], item["user_id"]))

    def _ticket(self, c, tid, actor):
        row = c.execute("SELECT * FROM support_tickets WHERE id=?", (tid,)).fetchone()
        if not row or (actor != self.admin_id and row["owner_id"] != actor):
            raise PermissionError("Обращение не найдено или недоступно.")
        return dict(row)

    def _limits(self, c, owner, *, creating=False):
        if owner == self.admin_id:
            return
        if creating:
            opened = c.execute("SELECT COUNT(*) FROM support_tickets WHERE owner_id=? AND status<>'closed'", (owner,)).fetchone()[0]
            today = c.execute("SELECT COUNT(*) FROM support_tickets WHERE owner_id=? AND created_at>?", (owner, time.time()-86400)).fetchone()[0]
            if opened >= 3 or today >= 5:
                raise ValueError("У тебя уже есть открытые обращения. Допиши в существующее: до 3 открытых и 5 новых за сутки.")
        hourly = c.execute("SELECT COUNT(*) FROM support_messages WHERE author_id=? AND created_at>?", (owner, time.time()-3600)).fetchone()[0]
        if hourly >= 50:
            raise ValueError("Лимит поддержки — 50 сообщений в час. Отправь продолжение позже.")

    def _append(self, c, ticket, actor, payload, source_chat, source_message):
        role = "admin" if actor == self.admin_id else "user"
        mid = c.execute("INSERT INTO support_messages(ticket_id,author_id,role,payload,source_chat,source_message,created_at) VALUES (?,?,?,?,?,?,?)",
                        (ticket["id"], actor, role, json.dumps(payload, ensure_ascii=False), source_chat, source_message, time.time())).lastrowid
        status = "waiting_user" if role == "admin" else "waiting_admin"
        c.execute("UPDATE support_tickets SET status=?,updated_at=? WHERE id=?", (status, time.time(), ticket["id"]))
        recipient = ticket["owner_id"] if role == "admin" else self.admin_id
        if recipient != actor:
            c.execute("INSERT INTO support_outbox(message_id,recipient_id) VALUES (?,?)", (mid, recipient))
        return mid

    def create_ticket(self, owner, subject, payload, source_chat, source_message, *, category="general"):
        subject = str(subject).strip()
        if not 3 <= len(subject) <= 80 or any(ord(ch) < 32 for ch in subject):
            raise ValueError("Тема обращения: от 3 до 80 символов, одной строкой.")
        if source_chat != owner or int(source_message) <= 0:
            raise ValueError("Обращение можно отправить только из своего личного чата.")
        now = time.time()
        with self.transaction() as c:
            duplicate = c.execute("SELECT ticket_id,author_id FROM support_messages WHERE source_chat=? AND source_message=?",
                                  (source_chat, source_message)).fetchone()
            if duplicate:
                if duplicate["author_id"] != owner:
                    raise PermissionError("Нет доступа.")
                return duplicate["ticket_id"], False
            self._limits(c, owner, creating=True)
            tid = c.execute("INSERT INTO support_tickets(owner_id,subject,category,created_at,updated_at) VALUES (?,?,?,?,?)",
                            (owner, subject, category, now, now)).lastrowid
            self._append(c, {"id":tid, "owner_id":owner}, owner, payload, source_chat, source_message)
            c.execute("INSERT INTO support_events(ticket_id,actor_id,action,created_at) VALUES (?,?,?,?)", (tid, owner, "created", now))
            return tid, True

    def add_reply(self, tid, actor, payload, source_chat, source_message):
        if source_chat != actor or int(source_message) <= 0:
            raise ValueError("Ответ можно отправить только из своего личного чата.")
        with self.transaction() as c:
            ticket = self._ticket(c, tid, actor)
            duplicate = c.execute("SELECT id,ticket_id,author_id FROM support_messages WHERE source_chat=? AND source_message=?",
                                  (source_chat, source_message)).fetchone()
            if duplicate:
                if duplicate["ticket_id"] != tid or duplicate["author_id"] != actor:
                    raise ValueError("Сообщение уже прикреплено к другому обращению.")
                return duplicate["id"], False
            if ticket["status"] == "closed":
                raise ValueError("Обращение закрыто. Сначала нажми «Открыть снова».")
            self._limits(c, actor)
            return self._append(c, ticket, actor, payload, source_chat, source_message), True

    def ticket(self, tid, actor, page=0, size=5):
        page = max(0, min(int(page), 100000))
        with self.lock:
            ticket = self._ticket(self.conn, tid, actor)
            total = self.conn.execute("SELECT COUNT(*) FROM support_messages WHERE ticket_id=?", (tid,)).fetchone()[0]
            rows = self.conn.execute("SELECT m.*,o.status AS delivery_status FROM support_messages m LEFT JOIN support_outbox o ON o.message_id=m.id "
                                     "WHERE m.ticket_id=? ORDER BY m.id DESC LIMIT ? OFFSET ?", (tid, size, page*size)).fetchall()
            ticket["messages"] = []
            for row in reversed(rows):
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                ticket["messages"].append(value)
            ticket["total"] = total
            return ticket

    def ticket_message(self, mid, actor):
        with self.lock:
            row = self.conn.execute("SELECT * FROM support_messages WHERE id=?", (mid,)).fetchone()
            if not row:
                raise PermissionError("Сообщение недоступно.")
            self._ticket(self.conn, row["ticket_id"], actor)
            result = dict(row)
            result["payload"] = json.loads(result["payload"])
            return result

    def tickets(self, actor, *, admin_view=False, closed=False, page=0, size=6):
        if admin_view:
            self._admin(actor)
        page = max(0, min(int(page), 100000))
        clause = "1=1" if admin_view else "owner_id=?"
        values = [] if admin_view else [actor]
        clause += " AND status='closed'" if closed else " AND status<>'closed'"
        with self.lock:
            total = self.conn.execute("SELECT COUNT(*) FROM support_tickets WHERE " + clause, values).fetchone()[0]
            rows = self.conn.execute("SELECT * FROM support_tickets WHERE " + clause + " ORDER BY updated_at DESC LIMIT ? OFFSET ?", (*values, size, page*size))
            return [dict(r) for r in rows], total

    def close_ticket(self, tid, actor, *, reopen=False):
        with self.transaction() as c:
            ticket = self._ticket(c, tid, actor)
            if reopen and ticket["status"] == "closed":
                self._limits(c, ticket["owner_id"], creating=True)
            new_status = "waiting_admin" if reopen else "closed"
            if (reopen and ticket["status"] != "closed") or (not reopen and ticket["status"] == "closed"):
                return False
            c.execute("UPDATE support_tickets SET status=?,updated_at=? WHERE id=?", (new_status, time.time(), tid))
            c.execute("INSERT INTO support_events(ticket_id,actor_id,action,created_at) VALUES (?,?,?,?)",
                      (tid, actor, "reopened" if reopen else "closed", time.time()))
            return True

    def claim_support(self, now=None):
        now = time.time() if now is None else now
        with self.transaction() as c:
            row = c.execute("SELECT o.*,m.ticket_id,m.payload,m.role,t.subject FROM support_outbox o "
                            "JOIN support_messages m ON m.id=o.message_id JOIN support_tickets t ON t.id=m.ticket_id "
                            "WHERE o.status='pending' AND o.next_attempt<=? ORDER BY o.id LIMIT 1", (now,)).fetchone()
            if not row:
                return None
            c.execute("UPDATE support_outbox SET status='inflight',attempts=attempts+1 WHERE id=?", (row["id"],))
            result = dict(row)
            result["payload"] = json.loads(result["payload"])
            result["attempts"] += 1
            return result

    def finish_support(self, item, status, *, error="", message_id=None, retry_at=0):
        with self.transaction() as c:
            c.execute("UPDATE support_outbox SET status=?,last_error=?,telegram_message_id=?,next_attempt=? WHERE id=? AND status='inflight'",
                      (status, error, message_id, retry_at, item["id"]))

    def replied_ticket(self, recipient, telegram_message_id):
        with self.lock:
            row = self.conn.execute("SELECT m.ticket_id FROM support_outbox o JOIN support_messages m ON m.id=o.message_id "
                                    "WHERE o.recipient_id=? AND o.telegram_message_id=? AND o.status='sent' LIMIT 1",
                                    (recipient, telegram_message_id)).fetchone()
            return row[0] if row else None
