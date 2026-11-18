from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .task_state import TaskState

MODEL_REACTION_PREFIX = "model:"
SUMMARY_EMOTION_ID = "54054"
SUMMARY_EMOTION_BACKGROUND = "im_bg_5"


class Store(TaskState):
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
            CREATE TABLE IF NOT EXISTS reactions(
              id TEXT PRIMARY KEY, chat TEXT, open_id TEXT, robot TEXT,
              state TEXT, sent_state TEXT DEFAULT '', updated REAL DEFAULT 0);
        """)
        reaction_columns = {r[1] for r in self.db.execute("PRAGMA table_info(reactions)")}
        for name, definition in {
            "emotion_id": "TEXT DEFAULT '2659900'",
            "metric": "INTEGER DEFAULT 0",
            "attempts": "INTEGER DEFAULT 0",
            "retry_at": "REAL DEFAULT 0",
            "error": "TEXT",
        }.items():
            if name not in reaction_columns:
                self.db.execute(f"ALTER TABLE reactions ADD COLUMN {name} {definition}")
        self.db.commit()
        self.init_tasks()

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
            previous = self.session(alias)
            merged = dict(previous["data"] if previous else {})
            merged.update({k: v for k, v in (data or {}).items() if v is not None})
            self.db.execute(
                """INSERT INTO sessions(alias,node,engine,sid,cwd,data,updated)
              VALUES(?,?,?,?,?,?,?) ON CONFLICT(node,engine,sid) DO UPDATE SET
              cwd=excluded.cwd,data=excluded.data,updated=excluded.updated""",
                (
                    alias,
                    node,
                    engine,
                    sid,
                    cwd or (previous["cwd"] if previous else ""),
                    json.dumps(merged),
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

    def enqueue(self, chat, body, alias=None, payload=None, question_id=None):
        with self.transaction():
            task = self.task(alias, create=True) if alias else None
            return self.db.execute(
                "INSERT INTO outbox(chat,body,ts,alias,generation,payload,question_id) VALUES(?,?,?,?,?,?,?)",
                (
                    chat,
                    body,
                    time.time(),
                    alias,
                    task["generation"] if task else None,
                    json.dumps(payload) if payload else None,
                    question_id,
                ),
            ).lastrowid

    def next_outgoing(self):
        with self.transaction():
            row = self.db.execute(
                "SELECT * FROM outbox WHERE status='pending' ORDER BY id LIMIT 1"
            ).fetchone()
            if row and (row["not_before"] or 0) <= time.time():
                self.db.execute(
                    "UPDATE outbox SET status='sending',attempts=attempts+1 WHERE id=?",
                    (row["id"],),
                )
                return dict(row)
        return None

    def question_visible(self, chat, qid):
        """A bare answer must follow delivery of the entire current numbered prompt."""
        with self.lock:
            rows = self.db.execute(
                "SELECT status,payload,receipt FROM outbox WHERE chat=? AND question_id=? ORDER BY id",
                (chat, qid),
            ).fetchall()
        prompts = [
            r["status"] for r in rows if (json.loads(r["payload"] or "{}")).get("question_prompt")
        ]
        if prompts:
            return all(status == "sent" for status in prompts)
        # The original legacy row is the prompt. Later rows can be choice error messages.
        if not rows or rows[0]["status"] != "sent":
            return False
        if not json.loads(rows[0]["payload"] or "{}").get("card"):
            return False
        receipt = json.loads(rows[0]["receipt"] or "{}")
        return bool(receipt.get("processQueryKey")) and not (
            receipt.get("cardBizId") or receipt.get("outTrackId")
        )

    def sent(self, oid, status):
        with self.transaction():
            self.db.execute("UPDATE outbox SET status=? WHERE id=?", (status, oid))

    def retry_outgoing(self, oid, delay, error):
        with self.transaction():
            self.db.execute(
                "UPDATE outbox SET status='pending',not_before=?,error=? WHERE id=?",
                (time.time() + delay, error, oid),
            )

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
            self.db.execute("UPDATE task_queue SET status='unknown' WHERE status='dispatching'")

    def reaction(self, rid, state, actor=None):
        with self.transaction():
            if actor and (actor.get("open_msg_id") or actor.get("platform_msg_id")):
                self.db.execute(
                    "INSERT OR IGNORE INTO reactions(id,chat,open_id,robot,state) VALUES(?,?,?,?,?)",
                    (
                        rid,
                        actor["chat"],
                        actor.get("open_msg_id") or actor["platform_msg_id"],
                        actor.get("robot_code", ""),
                        state,
                    ),
                )
            self.db.execute(
                """UPDATE reactions SET attempts=CASE WHEN state!=? THEN 0 ELSE attempts END,
                retry_at=CASE WHEN state!=? THEN 0 ELSE retry_at END,state=? WHERE id=?""",
                (state, state, state, rid),
            )

    def pending_reaction(self):
        with self.lock:
            row = self.db.execute(
                """SELECT r.* FROM reactions AS r
                WHERE r.metric IN (0, 4) AND r.state NOT LIKE 'model:%'
                AND r.state!=r.sent_state AND r.updated<?
                AND r.retry_at<=? AND r.attempts<3 AND (
                  r.metric=0 OR NOT EXISTS (
                    SELECT 1 FROM reactions AS status WHERE status.chat=r.chat
                    AND status.open_id=r.open_id AND status.metric=0
                    AND status.state NOT LIKE 'model:%'
                    AND status.state!=status.sent_state AND status.attempts<3
                  )
                ) ORDER BY r.metric, r.updated, r.id LIMIT 1""",
                (time.time() - 15, time.time()),
            ).fetchone()
            return dict(row) if row else None

    def queue_input_metrics(self, rid, label):
        """Leave a receipt and append one summary on the same original message."""
        with self.transaction():
            self.reaction(rid, "received")
            self.db.execute(
                """INSERT OR IGNORE INTO reactions(id,chat,open_id,robot,state,emotion_id,metric)
                SELECT ?, chat, open_id, robot, ?, ?, 4
                FROM reactions WHERE id=? AND metric=0 AND open_id!=''""",
                (f"input-summary:{rid}", label, SUMMARY_EMOTION_ID, rid),
            )

    def reacted(self, rid, state):
        with self.transaction():
            self.db.execute(
                """UPDATE reactions SET sent_state=?,updated=?,attempts=0,error=NULL
                WHERE id=?""",
                (state, time.time(), rid),
            )

    def shorten_summary(self, row, model):
        with self.transaction():
            self.db.execute(
                "UPDATE reactions SET state=?,retry_at=0 WHERE id=? AND state=? AND sent_state=''",
                (model, row["id"], row["state"]),
            )

    def reaction_failed(self, row, error):
        with self.transaction():
            self.db.execute(
                """UPDATE reactions SET attempts=attempts+1,retry_at=?,error=?
                WHERE id=? AND state=?""",
                (
                    time.time() + (2, 10, 60)[min(row["attempts"], 2)],
                    error,
                    row["id"],
                    row["state"],
                ),
            )

    def reaction_recalled(self, rid, state):
        with self.transaction():
            self.db.execute(
                "UPDATE reactions SET sent_state='' WHERE id=? AND sent_state=?", (rid, state)
            )
