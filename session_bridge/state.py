from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path


class Store:
    """Gateway state. One lock/transaction across each compound state transition."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.depth = 0
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS sessions(
              alias TEXT PRIMARY KEY, node TEXT, engine TEXT, sid TEXT, cwd TEXT,
              data TEXT NOT NULL, watched INTEGER NOT NULL DEFAULT 1, updated REAL,
              UNIQUE(node,engine,sid));
            CREATE TABLE IF NOT EXISTS events(
              id TEXT PRIMARY KEY, alias TEXT, kind TEXT, prompt TEXT, result TEXT, ts REAL);
            CREATE TABLE IF NOT EXISTS commands(
              id TEXT PRIMARY KEY, alias TEXT, body TEXT, status TEXT, detail TEXT, ts REAL);
            CREATE TABLE IF NOT EXISTS pairs(
              tenant TEXT, sender TEXT, chat TEXT, kind TEXT, staff TEXT,
              PRIMARY KEY(tenant,sender,chat));
            CREATE TABLE IF NOT EXISTS inbox(
              id TEXT PRIMARY KEY, body TEXT, done INTEGER DEFAULT 0, ts REAL);
            CREATE TABLE IF NOT EXISTS outbox(
              id INTEGER PRIMARY KEY AUTOINCREMENT, chat TEXT, body TEXT,
              status TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0, ts REAL);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
        """)
        self.db.commit()

    @contextmanager
    def transaction(self):
        with self.lock:
            outer = self.depth == 0
            if outer:
                self.db.execute("BEGIN IMMEDIATE")
            self.depth += 1
            try:
                yield
                if outer:
                    self.db.commit()
            except BaseException:
                if outer:
                    self.db.rollback()
                raise
            finally:
                self.depth -= 1

    def close(self):
        self.db.close()

    def upsert_session(self, node, engine, sid, cwd, data=None):
        raw = json.dumps([node, engine, sid], separators=(",", ":"))
        alias = f"{node}-{engine[0].upper()}{hashlib.sha256(raw.encode()).hexdigest()[:8]}"
        with self.transaction():
            self.db.execute(
                """INSERT INTO sessions(alias,node,engine,sid,cwd,data,updated)
              VALUES(?,?,?,?,?,?,?) ON CONFLICT(node,engine,sid) DO UPDATE SET
              cwd=excluded.cwd,data=excluded.data,updated=excluded.updated""",
                (
                    alias,
                    node,
                    engine,
                    sid,
                    cwd,
                    json.dumps(data or {}),
                    (data or {}).get("modified") or time.time(),
                ),
            )
        return alias

    def session(self, alias):
        with self.lock:
            row = self.db.execute("SELECT * FROM sessions WHERE alias=?", (alias,)).fetchone()
        if row:
            result = dict(row)
            result["data"] = json.loads(result["data"])
            return result
        return None

    def clear_live(self, node):
        """A full snapshot must not leave vanished sessions marked live."""
        with self.transaction():
            self.db.execute(
                "UPDATE sessions SET data=json_set(data, '$.live', json('false')) WHERE node=?",
                (node,),
            )

    def sessions(self):
        with self.lock:
            return [
                self.session(r[0])
                for r in self.db.execute(
                    "SELECT alias FROM sessions ORDER BY json_extract(data, '$.live') DESC, updated DESC LIMIT 100"
                ).fetchall()
            ]

    def watch(self, alias, enabled):
        with self.transaction():
            self.db.execute("UPDATE sessions SET watched=? WHERE alias=?", (int(enabled), alias))

    def add_event(self, eid, alias, kind, prompt, result, ts=None):
        with self.transaction():
            return (
                self.db.execute(
                    "INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?)",
                    (eid, alias, kind, prompt, result, ts or time.time()),
                ).rowcount
                == 1
            )

    def history(self, alias, limit=6):
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM events WHERE alias=? ORDER BY ts DESC LIMIT ?", (alias, limit)
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def claim_command(self, mid, alias, body):
        with self.transaction():
            if self.session(alias) is None:
                raise ValueError("Unknown session alias")
            return (
                self.db.execute(
                    "INSERT OR IGNORE INTO commands VALUES(?,?,?,?,?,?)",
                    (mid, alias, body, "dispatching", "", time.time()),
                ).rowcount
                == 1
            )

    def command_result(self, mid, status, detail):
        with self.transaction():
            self.db.execute(
                "UPDATE commands SET status=?,detail=? WHERE id=?", (status, detail, mid)
            )

    def pair(self, tenant, sender, chat, kind="2", staff=""):
        with self.transaction():
            self.db.execute(
                "INSERT OR REPLACE INTO pairs VALUES(?,?,?,?,?)",
                (tenant, sender, chat, kind, staff),
            )

    def authorized(self, tenant, sender, chat):
        with self.lock:
            return (
                self.db.execute(
                    "SELECT 1 FROM pairs WHERE tenant=? AND sender=? AND chat=?",
                    (tenant, sender, chat),
                ).fetchone()
                is not None
            )

    def pairs(self):
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM pairs").fetchall()]

    def receive(self, mid, data):
        with self.transaction():
            return (
                self.db.execute(
                    "INSERT OR IGNORE INTO inbox(id,body,ts) VALUES(?,?,?)",
                    (mid, json.dumps(data, ensure_ascii=False), time.time()),
                ).rowcount
                == 1
            )

    def incoming(self):
        with self.lock:
            row = self.db.execute("SELECT * FROM inbox WHERE done=0 ORDER BY ts LIMIT 1").fetchone()
            return dict(row) if row else None

    def processed(self, mid):
        with self.transaction():
            self.db.execute("UPDATE inbox SET done=1 WHERE id=?", (mid,))

    def enqueue(self, chat, body):
        with self.transaction():
            self.db.execute(
                "INSERT INTO outbox(chat,body,ts) VALUES(?,?,?)", (chat, body, time.time())
            )

    def next_outgoing(self):
        with self.transaction():
            row = self.db.execute(
                "SELECT * FROM outbox WHERE status='pending' ORDER BY id LIMIT 1"
            ).fetchone()
            if row:
                self.db.execute(
                    "UPDATE outbox SET status='sending',attempts=attempts+1 WHERE id=?",
                    (row["id"],),
                )
                return dict(row)
        return None

    def sent(self, oid, status):
        with self.transaction():
            self.db.execute("UPDATE outbox SET status=? WHERE id=?", (status, oid))

    def get(self, key, default=None):
        with self.lock:
            row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def put(self, key, value):
        with self.transaction():
            self.db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, json.dumps(value)))

    def recover(self):
        with self.transaction():
            self.db.execute("UPDATE commands SET status='unknown' WHERE status='dispatching'")
            self.db.execute("UPDATE outbox SET status='unknown' WHERE status='sending'")
