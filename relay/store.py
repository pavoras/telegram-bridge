"""SQLite storage for the relay. The only module that contains SQL."""
import hmac
import logging
import json
import os
import random
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Callable, Optional

from relay.i18n import local_time, t

DAY = 86400
RETENTION = 7 * DAY
QUESTION_TTL = DAY
ASSIGNMENT_TTL = 600
DEFAULT_DONE_DELAY = 600
REGISTER_INTERVAL = 60
MAX_PENDING_DEVICES = 3
OPEN = ("queued", "sent")

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT UNIQUE NOT NULL,
    client_id TEXT NOT NULL,
    code INTEGER,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    session_key TEXT,
    title TEXT,
    text TEXT NOT NULL,
    buttons TEXT,
    origin TEXT,
    created_at REAL NOT NULL,
    expires_at REAL,
    answer TEXT,
    closed_reason TEXT,
    tg_message_id INTEGER
);
CREATE TABLE IF NOT EXISTS codes (
    code INTEGER NOT NULL,
    message_id TEXT NOT NULL,
    assigned_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS codes_code ON codes(code, assigned_at);
CREATE TABLE IF NOT EXISTS tg_links (
    tg_message_id INTEGER PRIMARY KEY,
    message_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_try_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS inbox_assign (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    inbound_tg_id INTEGER NOT NULL,
    answer TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency (
    client_id TEXT NOT NULL,
    key TEXT NOT NULL,
    message_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (client_id, key)
);
CREATE TABLE IF NOT EXISTS sessions (
    client_id TEXT NOT NULL,
    session_key TEXT NOT NULL,
    origin TEXT,
    title TEXT,
    done_delay INTEGER,
    ping_due REAL,
    ping_kind TEXT,
    ping_text TEXT,
    last_event_at REAL,
    PRIMARY KEY (client_id, session_key)
);
CREATE TABLE IF NOT EXISTS devices (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    host TEXT,
    status TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS media (
    id TEXT PRIMARY KEY,
    direction TEXT NOT NULL,
    file_id TEXT,
    path TEXT,
    mime TEXT,
    size INTEGER,
    client_id TEXT,
    message_id TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS albums (
    group_id TEXT PRIMARY KEY,
    inbound_tg_id INTEGER NOT NULL,
    reply_to INTEGER,
    text TEXT,
    media TEXT NOT NULL,
    first_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS albums_done (
    group_id TEXT PRIMARY KEY,
    done_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

_JSON_FIELDS = ("buttons", "origin", "answer", "images")

MAX_PAIRING_FAILURES = 5


class Store:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._lock = threading.RLock()
        self._depth = 0
        self._rand = random.SystemRandom()

    def init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            columns = {r[1] for r in self._conn.execute("PRAGMA table_info(messages)")}
            if "images" not in columns:  # databases from version 0.1
                self._conn.execute("ALTER TABLE messages ADD COLUMN images TEXT")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def tx(self):
        """Write transaction; nested calls join the outermost one."""
        with self._lock:
            outer = self._depth == 0
            if outer:
                self._conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield self._conn
            except BaseException:
                self._depth -= 1
                if outer:
                    self._conn.execute("ROLLBACK")
                raise
            self._depth -= 1
            if outer:
                self._conn.execute("COMMIT")

    def _query(self, sql: str, args=()) -> list:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    @staticmethod
    def _message(row) -> Optional[dict]:
        if row is None:
            return None
        msg = dict(row)
        for field in _JSON_FIELDS:
            if msg[field] is not None:
                msg[field] = json.loads(msg[field])
        return msg

    # --- messages ------------------------------------------------------
    def create_message(self, client_id: str, kind: str, text: str, origin: dict, idempotency_key: str,
                       now: float, title: Optional[str] = None, session_key: Optional[str] = None,
                       buttons: Optional[list] = None, images: Optional[list] = None) -> dict:
        with self.tx() as db:
            row = db.execute(
                "SELECT m.id, m.code FROM idempotency i JOIN messages m ON m.id = i.message_id"
                " WHERE i.client_id = ? AND i.key = ?", (client_id, idempotency_key)).fetchone()
            if row:
                return {"id": row["id"], "code": row["code"]}
            message_id = str(uuid.uuid4())
            question = kind == "question"
            code = self._allocate_code(db, now) if question else None
            db.execute(
                "INSERT INTO messages (id, client_id, code, kind, status, session_key, title, text, buttons,"
                " origin, created_at, expires_at, images) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)",
                (message_id, client_id, code, kind, session_key, title, text,
                 json.dumps(buttons) if buttons else None, json.dumps(origin or {}), now,
                 now + QUESTION_TTL if question else None, json.dumps(images) if images else None))
            if code is not None:
                db.execute("INSERT INTO codes (code, message_id, assigned_at) VALUES (?, ?, ?)",
                           (code, message_id, now))
            db.execute("INSERT INTO idempotency (client_id, key, message_id, created_at) VALUES (?, ?, ?, ?)",
                       (client_id, idempotency_key, message_id, now))
            self._enqueue(db, "send", {"message_id": message_id}, now)
            return {"id": message_id, "code": code}

    def _allocate_code(self, db, now: float) -> int:
        used = {r[0] for r in db.execute("SELECT code FROM codes WHERE assigned_at > ?", (now - RETENTION,))}
        for low, high in ((100, 1000), (1000, 10000)):
            free = [c for c in range(low, high) if c not in used]
            if free:
                return self._rand.choice(free)
        raise RuntimeError("no free codes")

    def get_message(self, message_id: str) -> Optional[dict]:
        rows = self._query("SELECT * FROM messages WHERE id = ?", (message_id,))
        return self._message(rows[0] if rows else None)

    def get_message_by_seq(self, seq: int) -> Optional[dict]:
        rows = self._query("SELECT * FROM messages WHERE seq = ?", (seq,))
        return self._message(rows[0] if rows else None)

    def mark_sent(self, message_id: str, tg_message_id: int) -> None:
        with self.tx() as db:
            db.execute("UPDATE messages SET tg_message_id = ?,"
                       " status = CASE status WHEN 'queued' THEN 'sent' ELSE status END WHERE id = ?",
                       (tg_message_id, message_id))
            db.execute("INSERT OR REPLACE INTO tg_links (tg_message_id, message_id) VALUES (?, ?)",
                       (tg_message_id, message_id))

    def _finish(self, message_id: str, status: str, now: float, answer=None, reason=None) -> bool:
        with self.tx() as db:
            cur = db.execute(
                f"UPDATE messages SET status = ?, answer = ?, closed_reason = ?"
                f" WHERE id = ? AND kind = 'question' AND status IN {OPEN}",
                (status, json.dumps(answer) if answer is not None else None, reason, message_id))
            if cur.rowcount != 1:
                return False
            for media_id in (answer or {}).get("images") or []:
                db.execute("UPDATE media SET message_id = ? WHERE id = ? AND direction = 'in'", (message_id, media_id))
            self._enqueue(db, "edit", {"message_id": message_id}, now)
            return True

    def answer_question(self, message_id: str, answer: dict, now: float) -> bool:
        return self._finish(message_id, "answered", now, answer=dict(answer, answered_at=now))

    def close_question(self, message_id: str, reason: str, now: float) -> bool:
        return self._finish(message_id, "closed", now, reason=reason)

    def expire_due(self, now: float) -> int:
        with self.tx() as db:
            ids = [r[0] for r in db.execute(
                f"SELECT id FROM messages WHERE kind = 'question' AND status IN {OPEN} AND expires_at <= ?",
                (now,))]
            return sum(self._finish(i, "expired", now, reason="expired") for i in ids)

    def open_questions(self) -> list:
        return [self._message(r) for r in self._query(
            f"SELECT * FROM messages WHERE kind = 'question' AND status IN {OPEN} ORDER BY created_at, seq")]

    def find_by_code(self, code: int) -> Optional[dict]:
        rows = self._query("SELECT m.* FROM codes c JOIN messages m ON m.id = c.message_id"
                           " WHERE c.code = ? ORDER BY c.assigned_at DESC LIMIT 1", (code,))
        return self._message(rows[0] if rows else None)

    def message_for_tg(self, tg_message_id: int) -> Optional[dict]:
        rows = self._query("SELECT m.* FROM tg_links l JOIN messages m ON m.id = l.message_id"
                           " WHERE l.tg_message_id = ?", (tg_message_id,))
        return self._message(rows[0] if rows else None)

    def link_tg(self, tg_message_id: int, message_id: str) -> None:
        with self.tx() as db:
            db.execute("INSERT OR REPLACE INTO tg_links (tg_message_id, message_id) VALUES (?, ?)",
                       (tg_message_id, message_id))

    def purge_old(self, now: float) -> int:
        cutoff = now - RETENTION
        with self.tx() as db:
            for (path,) in db.execute("SELECT path FROM media WHERE created_at < ? AND path IS NOT NULL", (cutoff,)):
                try:
                    os.remove(path)
                except OSError:
                    pass
            db.execute("DELETE FROM media WHERE created_at < ?", (cutoff,))
            db.execute("DELETE FROM albums WHERE first_at < ?", (cutoff,))
            db.execute("DELETE FROM albums_done WHERE done_at < ?", (cutoff,))
            removed = db.execute("DELETE FROM messages WHERE created_at < ?", (cutoff,)).rowcount
            db.execute("DELETE FROM tg_links WHERE message_id NOT IN (SELECT id FROM messages)")
            db.execute("DELETE FROM codes WHERE assigned_at < ?", (cutoff,))
            db.execute("DELETE FROM idempotency WHERE created_at < ?", (cutoff,))
            db.execute("DELETE FROM outbox WHERE status != 'pending' AND created_at < ?", (cutoff,))
            db.execute("DELETE FROM inbox_assign WHERE expires_at < ?", (now,))
            db.execute("DELETE FROM devices WHERE status IN ('pending', 'rejected') AND created_at < ?",
                       (now - DAY,))
            db.execute("DELETE FROM sessions WHERE last_event_at < ? AND ping_due IS NULL", (cutoff,))
            return removed

    def purge_orphan_files(self, media_dir: str, now: float, min_age: float = 3600) -> int:
        """Delete files in media_dir without a media row (left by a crash between file and row).
        Younger files are kept: an upload writes its file just before its row."""
        try:
            names = os.listdir(media_dir)
        except OSError:
            return 0
        known = {os.path.basename(r[0]) for r in self._query("SELECT path FROM media WHERE path IS NOT NULL")}
        removed = 0
        for name in names:
            path = os.path.join(media_dir, name)
            try:
                if name not in known and os.path.isfile(path) and now - os.path.getmtime(path) > min_age:
                    os.remove(path)
                    removed += 1
            except OSError:
                pass
        return removed

    # --- media ---------------------------------------------------------------
    def add_inbound_media(self, file_id: str, now: float) -> str:
        media_id = uuid.uuid4().hex
        with self.tx() as db:
            db.execute("INSERT INTO media (id, direction, file_id, created_at) VALUES (?, 'in', ?, ?)",
                       (media_id, file_id, now))
        return media_id

    def add_outbound_media(self, media_id: str, client_id: str, path: str, mime: str, size: int, now: float) -> None:
        with self.tx() as db:
            db.execute("INSERT INTO media (id, direction, path, mime, size, client_id, created_at)"
                       " VALUES (?, 'out', ?, ?, ?, ?, ?)", (media_id, path, mime, size, client_id, now))

    def get_media(self, media_id: str) -> Optional[dict]:
        rows = self._query("SELECT * FROM media WHERE id = ?", (media_id,))
        return dict(rows[0]) if rows else None

    def media_usage(self) -> int:
        return self._query("SELECT COALESCE(SUM(size), 0) FROM media WHERE direction = 'out'")[0][0]

    def add_album_part(self, group_id: str, inbound_tg_id: int, reply_to: Optional[int], text: Optional[str],
                       media_id: str, now: float) -> bool:
        """Collect an album part; False if the album was already routed (the part came too late)."""
        with self.tx() as db:
            if db.execute("SELECT 1 FROM albums_done WHERE group_id = ?", (group_id,)).fetchone():
                return False
            row = db.execute("SELECT * FROM albums WHERE group_id = ?", (group_id,)).fetchone()
            if row is None:
                db.execute("INSERT INTO albums (group_id, inbound_tg_id, reply_to, text, media, first_at)"
                           " VALUES (?, ?, ?, ?, ?, ?)", (group_id, inbound_tg_id, reply_to, text,
                                                          json.dumps([media_id]), now))
            else:
                db.execute("UPDATE albums SET inbound_tg_id = MIN(inbound_tg_id, ?), reply_to = COALESCE(reply_to, ?),"
                           " text = COALESCE(text, ?), media = ? WHERE group_id = ?",
                           (inbound_tg_id, reply_to, text, json.dumps(json.loads(row["media"]) + [media_id]),
                            group_id))
            return True

    def take_due_albums(self, cutoff: float) -> list:
        """Albums whose first part arrived at or before cutoff, as incoming messages; they are removed."""
        with self.tx() as db:
            rows = db.execute("SELECT * FROM albums WHERE first_at <= ? ORDER BY first_at", (cutoff,)).fetchall()
            db.execute("DELETE FROM albums WHERE first_at <= ?", (cutoff,))
            db.executemany("INSERT OR REPLACE INTO albums_done (group_id, done_at) VALUES (?, ?)",
                           [(r["group_id"], cutoff) for r in rows])
        return [{"message_id": r["inbound_tg_id"], "reply_to": r["reply_to"], "text": r["text"] or "",
                 "images": json.loads(r["media"])} for r in rows]

    # --- sessions and done pings ------------------------------------------------
    @staticmethod
    def _session(db, client_id: str, key: str, origin: Optional[dict]):
        """Fetch the session row, creating it on first contact. The origin is frozen then."""
        db.execute("INSERT OR IGNORE INTO sessions (client_id, session_key, origin) VALUES (?, ?, ?)",
                   (client_id, key, json.dumps(origin or {})))
        if origin:
            db.execute("UPDATE sessions SET origin = ? WHERE client_id = ? AND session_key = ? AND origin = '{}'",
                       (json.dumps(origin), client_id, key))
        return db.execute("SELECT * FROM sessions WHERE client_id = ? AND session_key = ?",
                          (client_id, key)).fetchone()

    def session_event(self, client_id: str, key: str, kind: str, ts: float, origin: Optional[dict] = None,
                      text: Optional[str] = None, title: Optional[str] = None, human: bool = False,
                      busy: bool = False) -> str:
        """Apply a hook event ("stop", "permission" or "prompt") and plan or cancel the session's ping.
        Returns "scheduled", "cancelled" or "stale" (older than the last event, ignored)."""
        with self.tx() as db:
            row = self._session(db, client_id, key, origin)
            if row["last_event_at"] is not None and ts < row["last_event_at"]:
                return "stale"
            db.execute("UPDATE sessions SET last_event_at = ?, title = COALESCE(?, title)"
                       " WHERE client_id = ? AND session_key = ?", (ts, title, client_id, key))
            open_questions = db.execute(
                f"SELECT id, created_at FROM messages WHERE client_id = ? AND session_key = ? AND kind = 'question'"
                f" AND status IN {OPEN}", (client_id, key)).fetchall()
            open_ids = [r["id"] for r in open_questions]
            delay = DEFAULT_DONE_DELAY if row["done_delay"] is None else row["done_delay"]
            if kind == "prompt" and human:
                for question in open_questions:
                    if question["created_at"] <= ts:
                        self.close_question(question["id"], "answered_in_app", ts)
            if kind == "prompt" or (kind == "stop" and busy) or open_ids or delay < 0:
                db.execute("UPDATE sessions SET ping_due = NULL WHERE client_id = ? AND session_key = ?",
                           (client_id, key))
                return "cancelled"
            if kind == "permission":
                text = t("waiting_since", time=local_time(ts).strftime("%H:%M"), message=text or t("approval_needed"))
            db.execute("UPDATE sessions SET ping_due = ?, ping_kind = ?, ping_text = ?"
                       " WHERE client_id = ? AND session_key = ?",
                       (ts + delay, "permission" if kind == "permission" else "done", text, client_id, key))
            return "scheduled"

    def set_done_delay(self, client_id: str, key: str, delay: Optional[int], now: float) -> None:
        """Seconds until the done ping; None = default, negative = no pings for this session."""
        with self.tx() as db:
            self._session(db, client_id, key, None)
            db.execute("UPDATE sessions SET done_delay = ?, last_event_at = COALESCE(last_event_at, ?),"
                       " ping_due = CASE WHEN ? < 0 THEN NULL ELSE ping_due END"
                       " WHERE client_id = ? AND session_key = ?",
                       (delay, now, -1 if delay is not None and delay < 0 else 0, client_id, key))

    def send_due_pings(self, now: float) -> int:
        with self.tx() as db:
            rows = db.execute("SELECT * FROM sessions WHERE ping_due IS NOT NULL AND ping_due <= ?",
                              (now,)).fetchall()
            sent = 0
            for r in rows:
                db.execute("UPDATE sessions SET ping_due = NULL WHERE client_id = ? AND session_key = ?",
                           (r["client_id"], r["session_key"]))
                if db.execute(f"SELECT 1 FROM messages WHERE client_id = ? AND session_key = ?"
                              f" AND kind = 'question' AND status IN {OPEN}",
                              (r["client_id"], r["session_key"])).fetchone():
                    continue  # the session asked on Telegram meanwhile: it is waiting there, not done
                sent += 1
                self.create_message(r["client_id"], r["ping_kind"], r["ping_text"] or "",
                                    json.loads(r["origin"] or "{}"), f"ping:{r['session_key']}:{r['ping_due']}",
                                    now, title=r["title"], session_key=r["session_key"])
            return sent

    # --- devices registered through Telegram ------------------------------------
    def register_device(self, name: str, sha256: str, host: Optional[str], now: float) -> str:
        """Ask the user to approve a new device. Returns a 4-digit confirmation code (shown on the device and
        in Telegram, so a request from someone else is recognisable), "exists" or "busy"."""
        with self.tx() as db:
            if db.execute("SELECT 1 FROM devices WHERE name = ? AND status = 'approved'"
                          " OR (name = ? AND status = 'pending' AND created_at > ?)",
                          (name, name, now - DAY)).fetchone():
                return "exists"
            last = db.execute("SELECT MAX(created_at) FROM devices").fetchone()[0]
            pending = db.execute("SELECT COUNT(*) FROM devices WHERE status = 'pending' AND created_at > ?",
                                 (now - DAY,)).fetchone()[0]
            if (last is not None and now - last < REGISTER_INTERVAL) or pending >= MAX_PENDING_DEVICES:
                return "busy"
            seq = db.execute("INSERT INTO devices (name, sha256, host, status, created_at)"
                             " VALUES (?, ?, ?, 'pending', ?)", (name, sha256, host, now)).lastrowid
            label = name + (f" ({host})" if host else "")
            code = f"{self._rand.randrange(10000):04d}"
            self._enqueue(db, "notice", {
                "text": t("device_request", label=label, code=code), "reply_to": None,
                "buttons": [[t("approve"), f"r:{seq}:1"], [t("reject"), f"r:{seq}:0"]]}, now)
            return code

    def decide_device(self, seq: int, approve: bool, now: float) -> Optional[dict]:
        """Approve or reject a pending device; None if it is not pending (any more)."""
        with self.tx() as db:
            row = db.execute("SELECT * FROM devices WHERE seq = ? AND status = 'pending' AND created_at > ?",
                             (seq, now - DAY)).fetchone()
            if row is None:
                return None
            db.execute("UPDATE devices SET status = ? WHERE seq = ?", ("approved" if approve else "rejected", seq))
            return dict(row)

    def approved_devices(self) -> dict:
        rows = self._query("SELECT name, sha256 FROM devices WHERE status = 'approved'")
        return {r["name"]: r["sha256"] for r in rows}

    # --- outbox ----------------------------------------------------------
    @staticmethod
    def _enqueue(db, kind: str, payload: dict, now: float) -> None:
        db.execute("INSERT INTO outbox (kind, payload, next_try_at, created_at) VALUES (?, ?, ?, ?)",
                   (kind, json.dumps(payload), now, now))

    def enqueue_notice(self, text: str, now: float, reply_to_tg_id: Optional[int] = None,
                       buttons: Optional[list] = None, chat_id: Optional[int] = None) -> None:
        payload = {"text": text, "reply_to": reply_to_tg_id,
                   "buttons": [list(b) for b in buttons] if buttons else None}
        if chat_id is not None:
            payload["chat_id"] = chat_id
        with self.tx() as db:
            self._enqueue(db, "notice", payload, now)

    def enqueue_notice_delete(self, tg_message_id: int, now: float) -> None:
        """Delete a bot notice (e.g. a used selection prompt)."""
        with self.tx() as db:
            self._enqueue(db, "notice_delete", {"tg_message_id": tg_message_id}, now)

    def enqueue_callback_answer(self, callback_query_id: str, text: Optional[str], now: float) -> None:
        with self.tx() as db:
            self._enqueue(db, "callback_answer", {"callback_query_id": callback_query_id, "text": text}, now)

    def outbox_due(self, now: float, limit: int) -> list:
        rows = self._query("SELECT id, kind, payload, attempts FROM outbox WHERE status = 'pending'"
                           " AND next_try_at <= ? ORDER BY id LIMIT ?", (now, limit))
        return [dict(r, payload=json.loads(r["payload"])) for r in rows]

    def complete_send(self, outbox_id: int, message_id: str, tg_message_id: int) -> None:
        """Record a delivered message and finish its outbox entry atomically, so a restart never resends it."""
        with self.tx() as db:
            self.mark_sent(message_id, tg_message_id)
            self.outbox_done(outbox_id)
            images = (self.get_message(message_id) or {}).get("images") or []
            for media_id in images:
                self._enqueue(db, "photo", {"message_id": message_id, "media_id": media_id,
                                            "reply_to": tg_message_id}, time.time())

    def outbox_done(self, outbox_id: int) -> None:
        with self.tx() as db:
            db.execute("UPDATE outbox SET status = 'sent' WHERE id = ?", (outbox_id,))

    def outbox_retry(self, outbox_id: int, next_try_at: float) -> None:
        with self.tx() as db:
            db.execute("UPDATE outbox SET attempts = attempts + 1, next_try_at = ? WHERE id = ?",
                       (next_try_at, outbox_id))

    def outbox_fail(self, outbox_id: int) -> None:
        with self.tx() as db:
            row = db.execute("SELECT kind, payload FROM outbox WHERE id = ?", (outbox_id,)).fetchone()
            db.execute("UPDATE outbox SET status = 'failed_permanent' WHERE id = ?", (outbox_id,))
            if row and row["kind"] == "send":
                db.execute(f"UPDATE messages SET status = 'failed' WHERE id = ? AND status IN {OPEN}",
                           (json.loads(row["payload"])["message_id"],))

    # --- inbound assignment ------------------------------------------------
    def create_assignment(self, inbound_tg_id: int, answer: dict, now: float) -> int:
        with self.tx() as db:
            return db.execute("INSERT INTO inbox_assign (inbound_tg_id, answer, expires_at) VALUES (?, ?, ?)",
                              (inbound_tg_id, json.dumps(answer), now + ASSIGNMENT_TTL)).lastrowid

    def take_assignment(self, seq: int, now: float) -> Optional[dict]:
        with self.tx() as db:
            row = db.execute("SELECT answer, expires_at FROM inbox_assign WHERE seq = ?", (seq,)).fetchone()
            if row is None:
                return None
            db.execute("DELETE FROM inbox_assign WHERE seq = ?", (seq,))
            return json.loads(row["answer"]) if row["expires_at"] >= now else None

    # --- telegram offset and key/value state ---------------------------------
    def process_update(self, update_id: int, handler: Callable[[], None]) -> bool:
        with self.tx() as db:
            row = db.execute("SELECT value FROM state WHERE key = 'tg_offset'").fetchone()
            if row is not None and update_id <= int(row["value"]):
                return False
            handler()
            db.execute("INSERT OR REPLACE INTO state (key, value) VALUES ('tg_offset', ?)", (str(update_id),))
            return True

    # --- pairing: the first private chat that sends /start <code> becomes the only chat ---------
    def pairing_code(self) -> str:
        """The current six-digit pairing code (created on first use, shown in the relay log)."""
        code = self.get_state("pairing_code")
        if code is None:
            code = f"{self._rand.randrange(10 ** 6):06d}"
            with self.tx() as db:
                db.execute("INSERT OR REPLACE INTO state (key, value) VALUES ('pairing_code', ?)", (code,))
                db.execute("DELETE FROM state WHERE key = 'pairing_failures'")
        return code

    def try_pair(self, chat_id: int, code: str) -> bool:
        """Bind chat_id if code is right. Five wrong codes replace the code."""
        if self.paired_chat() is not None:
            return False
        if hmac.compare_digest(code, self.pairing_code()):
            self.pair(chat_id)
            return True
        failures = int(self.get_state("pairing_failures") or 0) + 1
        if failures >= MAX_PAIRING_FAILURES:
            with self.tx() as db:
                db.execute("DELETE FROM state WHERE key IN ('pairing_code', 'pairing_failures')")
            logging.getLogger("relay").warning("too many wrong pairing codes; new code: /start %s",
                                               self.pairing_code())
        else:
            self.set_state("pairing_failures", str(failures))
        return False

    def pair(self, chat_id: int) -> None:
        with self.tx() as db:
            db.execute("INSERT OR REPLACE INTO state (key, value) VALUES ('chat_id', ?)", (str(chat_id),))
            db.execute("DELETE FROM state WHERE key IN ('pairing_code', 'pairing_failures')")

    def paired_chat(self) -> Optional[int]:
        value = self.get_state("chat_id")
        return int(value) if value else None

    def daily_enabled(self) -> bool:
        """The daily report and its 08:15 fallback are off until switched on (/daily an, tg daily --on)."""
        return self.get_state("daily") == "on"

    def set_daily(self, enabled: bool) -> None:
        self.set_state("daily", "on" if enabled else "off")

    def set_host_status(self, items: dict, now: float) -> None:
        self.set_state("host_status", json.dumps({"items": items, "received_at": now}))

    def get_host_status(self) -> Optional[dict]:
        value = self.get_state("host_status")
        return json.loads(value) if value else None

    def has_message_since(self, kind: str, since: float) -> bool:
        return bool(self._query("SELECT 1 FROM messages WHERE kind = ? AND created_at >= ? LIMIT 1", (kind, since)))

    def last_message_at(self, kind: str) -> Optional[float]:
        return self._query("SELECT MAX(created_at) FROM messages WHERE kind = ?", (kind,))[0][0]

    def failed_since(self, since: float) -> int:
        return self._query("SELECT COUNT(*) FROM outbox WHERE status = 'failed_permanent' AND created_at >= ?",
                           (since,))[0][0]

    def get_state(self, key: str) -> Optional[str]:
        rows = self._query("SELECT value FROM state WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None

    def set_state(self, key: str, value: str) -> None:
        with self.tx() as db:
            db.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (key, value))
