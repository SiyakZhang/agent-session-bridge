"""Incremental transcript monitor. This module and node work on Python 3.8+."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

from .formatting import redact


def text_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(x.get("text", ""))
            for x in content
            if isinstance(x, dict) and x.get("type") in ("text", "input_text", "output_text")
        )
    return ""


def parse_record(engine, record, meta):
    """Update context, yield only explicit lifecycle events, never tool output."""
    kind = record.get("type")
    if engine == "codex":
        data = record.get("payload", {})
        if not isinstance(data, dict):
            return None
        if kind == "session_meta":
            meta.update(
                sid=data.get("id") or data.get("session_id"),
                cwd=data.get("cwd", ""),
                hidden=bool(data.get("parent_thread_id")) or isinstance(data.get("source"), dict),
            )
        elif (
            kind == "response_item" and data.get("type") == "message" and data.get("role") == "user"
        ):
            text = text_content(data.get("content"))
            if text and not text.lstrip().startswith("<environment_context>"):
                meta["prompt"] = redact(text[-8000:])
        elif kind == "event_msg":
            subtype = data.get("type")
            if subtype == "user_message":
                meta["prompt"] = redact(data.get("message", "")[-8000:])
            elif subtype == "task_started":
                meta["turn"] = data.get("turn_id", record.get("timestamp", ""))
                meta["status"] = "busy"
                meta["result"] = ""
            elif subtype in ("task_complete", "turn_aborted"):
                meta["status"] = "idle" if subtype == "task_complete" else "aborted"
                meta["turn"] = data.get("turn_id") or meta.get("turn", record.get("timestamp", ""))
                meta["result"] = redact(
                    str(data.get("last_agent_message") or "（本轮未记录最终文本，请查看终端）")[
                        -16000:
                    ]
                )
                return "completed" if subtype == "task_complete" else "aborted"
    else:
        if record.get("isSidechain"):
            meta["hidden"] = True
            return None
        meta["sid"] = record.get("sessionId") or record.get("session_id") or meta.get("sid")
        meta["cwd"] = record.get("cwd") or meta.get("cwd", "")
        message = record.get("message", {})
        if not isinstance(message, dict):
            return None
        origin = record.get("origin") or {}
        peer_message = isinstance(origin, dict) and origin.get("kind") == "peer"
        if (
            kind == "user"
            and not record.get("toolUseResult")
            and (not record.get("isMeta") or peer_message)
        ):
            text = text_content(message.get("content"))
            if peer_message and text.startswith("Another Claude session sent a message:\n"):
                text = text.split("\n", 1)[1].split("\n\nThis came from another Claude session", 1)[
                    0
                ]
            if text:
                meta["prompt"] = redact(text[-8000:])
                meta["status"] = "busy"
                meta["turn"] = record.get("uuid", record.get("timestamp", ""))
                meta["result"] = ""
                meta.pop("completion", None)
        elif kind == "assistant":
            text = text_content(message.get("content"))
            if text:
                meta["result"] = redact(text[-16000:])
            if message.get("stop_reason") == "end_turn":
                # Some providers put end_turn on every block, including thinking-only blocks.
                # Wait for readable final text or the authoritative Stop hook.
                if message.get("content") and not text:
                    return None
                meta["status"] = "idle"
                meta["completion"] = record.get("uuid") or message.get("id")
                if not meta.get("result"):
                    meta["result"] = "（本轮未记录最终文本，请查看终端）"
                return "completed"
    return None


class Scanner:
    def __init__(self, home, state, codex_home=None, claude_home=None):
        self.home, self.state = Path(home), Path(state)
        self.codex_home = Path(codex_home or os.environ.get("CODEX_HOME", self.home / ".codex"))
        self.claude_home = Path(
            claude_home or os.environ.get("CLAUDE_CONFIG_DIR", self.home / ".claude")
        )
        self.state.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.state / "node.sqlite"))
        self.db.executescript("""PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS cursors(path TEXT PRIMARY KEY,offset INTEGER,meta TEXT);
            CREATE TABLE IF NOT EXISTS pending(id TEXT PRIMARY KEY,body TEXT);
            CREATE TABLE IF NOT EXISTS dispatch(id TEXT PRIMARY KEY,status TEXT,detail TEXT);
            CREATE TABLE IF NOT EXISTS emitted(id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS hooks_seen(id TEXT PRIMARY KEY);
        """)
        self.db.commit()
        self.started = time.time()
        self.initial = True
        self.files = []
        self.last_discovery = 0

    def discover(self):
        found = []
        cutoff = time.time() - 7 * 86400
        for root, pattern, engine in [
            (self.codex_home / "sessions", "*.jsonl", "codex"),
            (self.claude_home / "projects", "*.jsonl", "claude"),
        ]:
            if not root.exists():
                continue
            for p in root.rglob(pattern):
                if "subagents" in p.parts:
                    continue
                try:
                    if p.stat().st_mtime >= cutoff:
                        found.append((engine, p))
                except OSError:
                    continue
        self.files = sorted(found, key=lambda x: x[1].stat().st_mtime, reverse=True)[:150]
        self.last_discovery = time.time()

    def poll(self):
        if time.time() - self.last_discovery > 15:
            self.discover()
        sessions = []
        for engine, path in self.files:
            try:
                meta = self.read_file(engine, path)
                if meta.get("sid") and not meta.get("hidden"):
                    sessions.append(dict(meta, engine=engine, path=str(path)))
            except (OSError, ValueError):
                continue
        self.initial = False
        self.read_hooks(sessions)
        pending = [json.loads(r[0]) for r in self.db.execute("SELECT body FROM pending").fetchall()]
        return sessions, pending

    def read_hooks(self, sessions):
        by_id = {s["sid"]: s for s in sessions if s["engine"] == "claude"}
        for path in (self.state / "hook-events").glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                session = by_id.get(data.get("session_id"))
                if not session:
                    if time.time() - data.get("ts", 0) > 86400:
                        path.unlink()
                    continue
                session = dict(
                    session,
                    **{
                        k: data.get("context", {}).get(k)
                        for k in ("prompt", "result", "turn", "completion")
                    },
                )
                hook = data.get("hook_event_name")
                if hook not in ("Stop", "StopFailure", "Notification"):
                    path.unlink()
                    continue
                if time.time() - data.get("ts", 0) < 5:
                    continue  # Allow final transcript write to settle.
                kind = {"Stop": "completed", "StopFailure": "aborted", "Notification": "waiting"}[
                    hook
                ]
                token = session.get("turn") or session.get("completion") or data["id"]
                eid = hashlib.sha256(
                    json.dumps(["claude", session["sid"], token, kind]).encode()
                ).hexdigest()
                result = (
                    data.get("last_assistant_message")
                    or session.get("result")
                    or "（结束事件已收到，最终文本不完整，请查看终端）"
                )
                if kind == "waiting":
                    result = data.get("message") or "请返回原电脑查看权限或输入请求。"
                body = dict(session, kind=kind, event_id=eid, ts=data["ts"], result=redact(result))
                with self.db:
                    if not self.db.execute(
                        "SELECT 1 FROM hooks_seen WHERE id=?", (data["id"],)
                    ).fetchone():
                        if self.db.execute(
                            "INSERT OR IGNORE INTO emitted VALUES(?)", (eid,)
                        ).rowcount:
                            self.db.execute(
                                "INSERT OR IGNORE INTO pending VALUES(?,?)",
                                (eid, json.dumps(body, ensure_ascii=False)),
                            )
                        self.db.execute("INSERT OR IGNORE INTO hooks_seen VALUES(?)", (data["id"],))
                path.unlink()
            except (OSError, ValueError, KeyError):
                continue

    def read_file(self, engine, path):
        stat = path.stat()
        row = self.db.execute(
            "SELECT offset,meta FROM cursors WHERE path=?", (str(path),)
        ).fetchone()
        meta = json.loads(row[1]) if row else {"sid": path.stem if engine == "claude" else None}
        offset = row[0] if row else 0
        bootstrap = not row and (self.initial or stat.st_mtime < self.started)
        if offset > stat.st_size:
            offset, bootstrap = 0, True
            meta = {"sid": path.stem if engine == "claude" else None}
        with path.open("rb") as stream:
            # Initial large histories: header gives ID, bounded tail provides latest context.
            if bootstrap and stat.st_size > 1024 * 1024:
                try:
                    parse_record(engine, json.loads(stream.readline()), meta)
                except (ValueError, UnicodeError):
                    pass
                stream.seek(stat.st_size - 1024 * 1024)
                stream.readline()
            else:
                stream.seek(offset)
            with self.db:
                while True:
                    start = stream.tell()
                    line = stream.readline()
                    if not line:
                        break
                    if not line.endswith(b"\n"):
                        stream.seek(start)
                        break
                    try:
                        record = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    event = parse_record(engine, record, meta)
                    if event and not bootstrap and meta.get("sid") and not meta.get("hidden"):
                        token = meta.get("turn") or meta.get("completion")
                        eid = hashlib.sha256(
                            json.dumps([engine, meta["sid"], token, event]).encode()
                        ).hexdigest()
                        body = dict(meta, engine=engine, kind=event, event_id=eid, ts=time.time())
                        if self.db.execute(
                            "INSERT OR IGNORE INTO emitted VALUES(?)", (eid,)
                        ).rowcount:
                            self.db.execute(
                                "INSERT OR IGNORE INTO pending VALUES(?,?)",
                                (eid, json.dumps(body, ensure_ascii=False)),
                            )
                meta["modified"] = stat.st_mtime
                self.db.execute(
                    "INSERT OR REPLACE INTO cursors VALUES(?,?,?)",
                    (str(path), stream.tell(), json.dumps(meta, ensure_ascii=False)),
                )
        return meta

    def acknowledge(self, event_ids):
        with self.db:
            self.db.executemany("DELETE FROM pending WHERE id=?", [(x,) for x in event_ids])

    def dispatch_claim(self, mid):
        with self.db:
            return (
                self.db.execute(
                    "INSERT OR IGNORE INTO dispatch VALUES(?,'dispatching','')", (mid,)
                ).rowcount
                == 1
            )

    def dispatch_result(self, mid, status, detail):
        with self.db:
            self.db.execute(
                "UPDATE dispatch SET status=?,detail=? WHERE id=?", (status, detail, mid)
            )
