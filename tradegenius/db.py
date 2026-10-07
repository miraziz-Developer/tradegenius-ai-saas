"""SQLite persistence. One connection shared across threads, guarded by a lock."""

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY,          -- Telegram user id
    username        TEXT,
    first_name      TEXT,
    created_at      TEXT NOT NULL,
    accepted_terms  INTEGER NOT NULL DEFAULT 0,
    state           TEXT NOT NULL DEFAULT 'idle',
    state_data      TEXT NOT NULL DEFAULT '{}',
    sub_expires_at  TEXT,
    is_blocked      INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS strategies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    name        TEXT NOT NULL,
    spec_json   TEXT NOT NULL,
    source_text TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id),
    login         TEXT NOT NULL,
    server        TEXT NOT NULL,
    password_enc  TEXT NOT NULL,
    strategy_id   INTEGER REFERENCES strategies(id),
    desired_state TEXT NOT NULL DEFAULT 'stopped',
    allow_real    INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    created_at    TEXT NOT NULL,
    UNIQUE(user_id, login, server)
);
CREATE TABLE IF NOT EXISTS payments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    amount      INTEGER NOT NULL,
    currency    TEXT NOT NULL,
    charge_id   TEXT UNIQUE,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(s):
    return datetime.fromisoformat(s) if s else None


class DB:
    def __init__(self, path):
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def _exec(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    def _one(self, sql, args=()):
        with self.lock:
            r = self.conn.execute(sql, args).fetchone()
            return dict(r) if r else None

    def _all(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    # ------------------------------------------------------------- users
    def upsert_user(self, uid, username=None, first_name=None):
        self._exec(
            "INSERT INTO users(id, username, first_name, created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name",
            (uid, username, first_name, now_iso()))
        return self.get_user(uid)

    def get_user(self, uid):
        u = self._one("SELECT * FROM users WHERE id=?", (uid,))
        if u:
            u["state_data"] = json.loads(u["state_data"] or "{}")
        return u

    def set_state(self, uid, state, data=None):
        self._exec("UPDATE users SET state=?, state_data=? WHERE id=?",
                   (state, json.dumps(data or {}), uid))

    def accept_terms(self, uid):
        self._exec("UPDATE users SET accepted_terms=1 WHERE id=?", (uid,))

    def set_sub_expiry(self, uid, iso):
        self._exec("UPDATE users SET sub_expires_at=? WHERE id=?", (iso, uid))

    def set_blocked(self, uid, blocked):
        self._exec("UPDATE users SET is_blocked=? WHERE id=?", (1 if blocked else 0, uid))

    def list_users(self, limit=50):
        return self._all("SELECT * FROM users ORDER BY created_at DESC LIMIT ?", (limit,))

    # -------------------------------------------------------- strategies
    def add_strategy(self, uid, spec, source_text=None):
        cur = self._exec(
            "INSERT INTO strategies(user_id, name, spec_json, source_text, created_at) VALUES(?,?,?,?,?)",
            (uid, spec["name"], json.dumps(spec), source_text, now_iso()))
        return cur.lastrowid

    def get_strategy(self, sid, uid=None):
        sql, args = "SELECT * FROM strategies WHERE id=?", [sid]
        if uid is not None:
            sql += " AND user_id=?"
            args.append(uid)
        s = self._one(sql, args)
        if s:
            s["spec"] = json.loads(s["spec_json"])
        return s

    def list_strategies(self, uid):
        rows = self._all("SELECT * FROM strategies WHERE user_id=? ORDER BY id DESC", (uid,))
        for r in rows:
            r["spec"] = json.loads(r["spec_json"])
        return rows

    def delete_strategy(self, sid, uid):
        in_use = self._one("SELECT id FROM accounts WHERE strategy_id=? AND user_id=?", (sid, uid))
        if in_use:
            return False
        self._exec("DELETE FROM strategies WHERE id=? AND user_id=?", (sid, uid))
        return True

    # ---------------------------------------------------------- accounts
    def add_account(self, uid, login, server, password_enc):
        self._exec(
            "INSERT INTO accounts(user_id, login, server, password_enc, created_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(user_id, login, server) DO UPDATE SET password_enc=excluded.password_enc, "
            "last_error=NULL",
            (uid, login, server, password_enc, now_iso()))
        return self._one("SELECT * FROM accounts WHERE user_id=? AND login=? AND server=?",
                         (uid, login, server))["id"]

    def get_account(self, aid, uid=None):
        sql, args = "SELECT * FROM accounts WHERE id=?", [aid]
        if uid is not None:
            sql += " AND user_id=?"
            args.append(uid)
        return self._one(sql, args)

    def list_accounts(self, uid):
        return self._all("SELECT * FROM accounts WHERE user_id=? ORDER BY id", (uid,))

    def count_accounts(self, uid):
        return self._one("SELECT COUNT(*) AS n FROM accounts WHERE user_id=?", (uid,))["n"]

    def running_accounts(self):
        return self._all("SELECT * FROM accounts WHERE desired_state='running'")

    def all_accounts(self):
        return self._all("SELECT * FROM accounts ORDER BY id")

    def update_account(self, aid, **fields):
        allowed = {"strategy_id", "desired_state", "allow_real", "last_error", "password_enc"}
        keys = [k for k in fields if k in allowed]
        if not keys:
            return
        sets = ", ".join(f"{k}=?" for k in keys)
        self._exec(f"UPDATE accounts SET {sets} WHERE id=?", [fields[k] for k in keys] + [aid])

    def delete_account(self, aid, uid):
        self._exec("DELETE FROM accounts WHERE id=? AND user_id=?", (aid, uid))

    # ---------------------------------------------------------- payments
    def add_payment(self, uid, amount, currency, charge_id):
        try:
            self._exec("INSERT INTO payments(user_id, amount, currency, charge_id, created_at) "
                       "VALUES(?,?,?,?,?)", (uid, amount, currency, charge_id, now_iso()))
            return True
        except sqlite3.IntegrityError:
            return False  # duplicate delivery of the same payment

    # ---------------------------------------------------------------- kv
    def kv_get(self, key, default=None):
        r = self._one("SELECT value FROM kv WHERE key=?", (key,))
        return r["value"] if r else default

    def kv_set(self, key, value):
        self._exec("INSERT INTO kv(key, value) VALUES(?,?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def stats(self):
        def n(sql):
            return self._one(sql)["n"]
        return {
            "users": n("SELECT COUNT(*) AS n FROM users"),
            "strategies": n("SELECT COUNT(*) AS n FROM strategies"),
            "accounts": n("SELECT COUNT(*) AS n FROM accounts"),
            "running": n("SELECT COUNT(*) AS n FROM accounts WHERE desired_state='running'"),
            "payments": n("SELECT COUNT(*) AS n FROM payments"),
        }
