"""Incremental transcript monitor. This module and node work on Python 3.8+."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from .errors import failure_message
from .formatting import redact
from .questions import normalize_questions


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


def record_time(record):
    try:
        value = record.get("timestamp")
        if isinstance(value, (int, float)):
            return value
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, AttributeError):
        return 0


def parse_record(engine, record, meta):
    """Update context, yield only explicit lifecycle events, never tool output."""
    kind = record.get("type")
    if engine == "codex":
        data = record.get("payload", {})
        if not isinstance(data, dict):
            return None
        if kind == "session_meta":
            # Fork transcripts can embed the parent's complete history, including its
            # session_meta. Only the file's own first header establishes identity.
            if meta.get("_identity_bound"):
                return None
            meta.update(
                sid=data.get("id") or data.get("session_id"),
                cwd=data.get("cwd", ""),
                hidden=bool(data.get("parent_thread_id"))
                or isinstance(data.get("source"), dict)
                or data.get("thread_source") == "subagent",
                _identity_bound=True,
            )
        elif (
            kind == "response_item" and data.get("type") == "message" and data.get("role") == "user"
        ):
            text = text_content(data.get("content"))
            if text and not text.lstrip().startswith("<environment_context>"):
                meta["prompt"] = redact(text)
        elif kind == "turn_context":
            for key in ("model", "model_provider", "effort"):
                if data.get(key) is not None:
                    meta[key] = data[key]
        elif kind == "compacted":
            meta["compaction_completed"] = True
            meta["context"] = {"invalidated": True}
            meta["result"] = "上下文压缩已完成，可以继续对话。"
            meta["status"] = "idle"
            return "compacted"
        elif kind == "response_item" and data.get("type") == "function_call":
            if data.get("name", "").split(".")[-1] in (
                "request_user_input",
                "request_user_input_async",
            ):
                try:
                    args = json.loads(data.get("arguments") or "{}")
                except ValueError:
                    return None
                key = "transcript:" + str(data.get("call_id"))
                questions = meta.setdefault("questions", [])
                if not any(q["key"] == key for q in questions):
                    questions.append(
                        {
                            "key": key,
                            "title": "等待你回答"
                            if not data["name"].endswith("_async")
                            else "待补充，任务可继续",
                            "questions": normalize_questions(args.get("questions")),
                            "options": [],
                            "remote": False,
                            "async": data["name"].endswith("_async"),
                        }
                    )
        elif kind == "response_item" and data.get("type") == "function_call_output":
            key = "transcript:" + str(data.get("call_id"))
            output = data.get("output")
            try:
                output = json.loads(output) if isinstance(output, str) else output
            except ValueError:
                output = {}
            resolved = isinstance(output, dict) and ("answers" in output or "error" in output)
            meta["questions"] = [
                q
                for q in meta.get("questions", [])
                if q["key"] != key or (q.get("async") and not resolved)
            ]
        elif kind == "event_msg":
            subtype = data.get("type")
            if subtype in ("task_started", "task_complete", "turn_aborted"):
                attribution = data.get("turn_attribution") or {}
                root_turn = data.get("root_turn_id") or attribution.get("root_turn_id")
                turn = data.get("turn_id") or attribution.get("turn_id")
                if root_turn and turn and root_turn != turn:
                    return None
            if subtype == "user_message":
                meta["prompt"] = redact(data.get("message", ""))
            elif subtype == "task_started":
                meta.pop("compaction_completed", None)
                meta["turn"] = data.get("turn_id", record.get("timestamp", ""))
                meta["status"] = "busy"
                meta["result"] = ""
                meta.pop("failure", None)
            elif subtype == "token_count":
                info = data.get("info") or {}
                last = info.get("last_token_usage") or {}
                used = last.get("total_tokens")
                if used is None and last.get("input_tokens") is not None:
                    used = last["input_tokens"] + last.get("output_tokens", 0)
                if used is not None:
                    meta["context"] = {
                        "used": used,
                        "window": info.get("model_context_window"),
                        "observed_at": record_time(record),
                    }
                if data.get("rate_limits") is not None:
                    meta["native_rate_limits"] = data["rate_limits"]
                    meta["rate_limits_at"] = record_time(record)
            elif subtype in ("error", "stream_error"):
                error = data.get("error") or data
                retrying = bool(data.get("will_retry") or data.get("willRetry"))
                meta["failure"] = error
                meta["result"] = failure_message(error, retrying)
                meta["status"] = "reconnecting" if retrying else "failed"
                return "reconnecting" if retrying else "failed"
            elif subtype == "context_compacted":
                meta["compaction_completed"] = True
                meta["context"] = {"invalidated": True}
                meta["result"] = "上下文压缩已完成，可以继续对话。"
                meta["status"] = "idle"
                return "compacted"
            elif subtype in ("task_complete", "turn_aborted"):
                meta["turn"] = data.get("turn_id") or meta.get("turn", record.get("timestamp", ""))
                error = data.get("error")
                if error or (meta.get("failure") and not data.get("last_agent_message")):
                    meta["failure"] = error or meta["failure"]
                    meta["status"] = "failed"
                    meta["result"] = failure_message(meta["failure"])
                    return "failed"
                if (
                    subtype == "task_complete"
                    and meta.get("compaction_completed")
                    and not data.get("last_agent_message")
                ):
                    meta["status"] = "idle"
                    return None
                meta["status"] = "idle" if subtype == "task_complete" else "aborted"
                meta["turn"] = data.get("turn_id") or meta.get("turn", record.get("timestamp", ""))
                meta["result"] = redact(
                    str(
                        data.get("last_agent_message")
                        or (
                            "本轮已中断，可以继续对话。"
                            if subtype == "turn_aborted"
                            else "（本轮未记录最终文本，请查看终端）"
                        )
                    )
                )
                meta["questions"] = [q for q in meta.get("questions", []) if q.get("async")]
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
        if kind == "user":
            answered = {
                b.get("tool_use_id")
                for b in message.get("content", [])
                if isinstance(b, dict) and b.get("type") == "tool_result"
            }
            if answered:
                meta["pending_tools"] = [
                    x for x in meta.get("pending_tools", []) if x not in answered
                ]
                meta["questions"] = [
                    q
                    for q in meta.get("questions", [])
                    if q["key"][len("transcript:") :] not in answered
                ]
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
                meta["prompt"] = redact(text)
                meta["status"] = "busy"
                meta["turn"] = record.get("uuid", record.get("timestamp", ""))
                meta["result"] = ""
                meta.pop("completion", None)
        elif kind == "assistant":
            if message.get("model"):
                meta["model"] = message["model"]
            usage = message.get("usage") or {}
            if usage.get("input_tokens") is not None:
                meta["context"] = {
                    "used": sum(
                        usage.get(k, 0) or 0
                        for k in (
                            "input_tokens",
                            "cache_read_input_tokens",
                            "cache_creation_input_tokens",
                            "output_tokens",
                        )
                    ),
                    "window": meta.get("context", {}).get("window"),
                    "observed_at": record_time(record),
                }
            text = text_content(message.get("content"))
            if text:
                meta["result"] = redact(text)
            if record.get("isApiErrorMessage"):
                meta["status"] = "failed"
                meta["failure"] = {"error": record.get("error"), "message": text}
                meta["result"] = failure_message(meta["failure"])
                return "failed"
            for block in message.get("content") or []:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and block.get("id") not in meta.setdefault("pending_tools", [])
                ):
                    meta["pending_tools"].append(block.get("id"))
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and block.get("name") == "AskUserQuestion"
                ):
                    key = "transcript:" + block["id"]
                    if not any(q["key"] == key for q in meta.get("questions", [])):
                        meta.setdefault("questions", []).append(
                            {
                                "key": key,
                                "title": "等待你回答",
                                "questions": normalize_questions(
                                    block.get("input", {}).get("questions")
                                ),
                                "options": [],
                                "remote": False,
                            }
                        )
            if message.get("stop_reason") == "end_turn":
                # Some providers put end_turn on every block, including thinking-only blocks.
                # Wait for readable final text or the authoritative Stop hook.
                if message.get("content") and not text:
                    return None
                meta["status"] = "idle"
                meta["questions"] = []
                meta["pending_tools"] = []
                meta["completion"] = record.get("uuid") or message.get("id")
                if not meta.get("result"):
                    meta["result"] = "（本轮未记录最终文本，请查看终端）"
                return "completed"
        elif kind == "system" and record.get("subtype") == "compact_boundary":
            meta["context"] = {"invalidated": True}
            meta["result"] = "上下文压缩已完成，可以继续对话。"
            return "compacted"
    return None


class Scanner:
    def __init__(self, home, state, codex_home=None, claude_home=None):
        self.home, self.state = Path(home), Path(state)
        self.codex_home = Path(codex_home or os.environ.get("CODEX_HOME", self.home / ".codex"))
        self.claude_home = Path(
            claude_home or os.environ.get("CLAUDE_CONFIG_DIR", self.home / ".claude")
        )
        self.state.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(self.state / "node.sqlite"), check_same_thread=False)
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
        self.index_updated = 0
        self.index = {}

    def enrich(self, sessions):
        if time.time() - self.index_updated > 30:
            self.index_updated = time.time()
            try:
                databases = sorted(self.codex_home.glob("state_*.sqlite"), reverse=True)
                if databases:
                    with sqlite3.connect(
                        databases[0].resolve().as_uri() + "?mode=ro", uri=True, timeout=1
                    ) as db:
                        db.row_factory = sqlite3.Row
                        columns = {r[1] for r in db.execute("PRAGMA table_info(threads)")}
                        keys = [
                            k
                            for k in ("id", "name", "title", "model", "model_provider")
                            if k in columns
                        ]
                        if "id" in keys:
                            self.index = {
                                r["id"]: dict(r)
                                for r in db.execute("SELECT " + ",".join(keys) + " FROM threads")
                            }
            except (OSError, ValueError, sqlite3.Error):
                pass
        for session in sessions:
            if session["engine"] == "codex":
                row = self.index.get(session["sid"], {})
                if row.get("name"):
                    row = dict(row, title=row["name"])
                for key in ("title", "model", "model_provider"):
                    if row.get(key) and (key == "title" or not session.get(key)):
                        session[key] = row[key]
            if not session.get("title"):
                session["title"] = " ".join(str(session.get("prompt") or "").split())[:160]

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
        with self.lock:
            return self._poll()

    def _poll(self):
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
        self.enrich(sessions)
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
                if hook not in ("Stop", "StopFailure", "Notification", "PreCompact", "PostCompact"):
                    path.unlink()
                    continue
                if hook == "Stop" and time.time() - data.get("ts", 0) < 2:
                    continue  # Allow final transcript write to settle.
                kind = {
                    "Stop": "completed",
                    "StopFailure": "failed",
                    "Notification": "waiting",
                    "PreCompact": "compacting",
                    "PostCompact": "compacted",
                }[hook]
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
                elif kind == "failed":
                    result = failure_message(data.get("error") or data.get("message") or result)
                elif kind in ("compacting", "compacted"):
                    result = (
                        "正在压缩上下文。"
                        if kind == "compacting"
                        else "上下文压缩已完成，可以继续对话。"
                    )
                if kind != "waiting" and session.get("turn") == by_id[data["session_id"]].get(
                    "turn"
                ):
                    status = {
                        "completed": "idle",
                        "failed": "failed",
                        "compacting": "compacting",
                        "compacted": "idle",
                    }[kind]
                    by_id[data["session_id"]].update(status=status, result=redact(result))
                    if kind == "compacted":
                        by_id[data["session_id"]]["context"] = {"invalidated": True}
                    cursor_path = by_id[data["session_id"]].get("path")
                    if cursor_path:
                        row = self.db.execute(
                            "SELECT meta FROM cursors WHERE path=?", (cursor_path,)
                        ).fetchone()
                        if row:
                            saved = json.loads(row[0])
                            saved.update(status=status, result=redact(result))
                            if kind == "compacted":
                                saved["context"] = {"invalidated": True}
                            with self.db:
                                self.db.execute(
                                    "UPDATE cursors SET meta=? WHERE path=?",
                                    (json.dumps(saved), cursor_path),
                                )
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
            if engine == "codex" and not meta.get("_identity_bound"):
                # Repair persisted cursors from versions that accepted inherited headers.
                header = json.loads(stream.readline())
                if header.get("type") != "session_meta":
                    raise ValueError("Codex transcript has no authoritative header")
                identity = {}
                parse_record(engine, header, identity)
                meta.update(identity)
            if row and offset and not meta.get("_revision3"):
                # Hydrate recent telemetry/lifecycle metadata without replaying already-consumed events.
                stream.seek(max(0, offset - 262144))
                if stream.tell():
                    stream.readline()
                while stream.tell() < offset:
                    line = stream.readline()
                    if not line or stream.tell() > offset:
                        break
                    try:
                        parse_record(engine, json.loads(line), meta)
                    except (ValueError, UnicodeError):
                        continue
            meta["_revision3"] = True
            if meta.get("cwd"):
                try:
                    relative = Path(meta["cwd"]).resolve().relative_to(self.state.resolve())
                    if relative.parts and relative.parts[0].startswith("control-probe-"):
                        meta["hidden"] = True
                except (ValueError, OSError):
                    pass
            # Initial large histories: header gives ID, bounded tail provides latest context.
            if bootstrap and stat.st_size > 1024 * 1024:
                if engine != "codex":
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
                        evidence = (
                            meta.get("failure") if event in ("failed", "reconnecting") else None
                        )
                        identity = [engine, meta["sid"], token, event]
                        if evidence:
                            identity.append(evidence)
                        if event == "compacted":
                            identity.append(record.get("timestamp") or stream.tell())
                        eid = hashlib.sha256(
                            json.dumps(identity, sort_keys=True).encode()
                        ).hexdigest()
                        body = dict(
                            meta,
                            engine=engine,
                            kind=event,
                            event_id=eid,
                            ts=time.time(),
                            source_ts=record_time(record),
                        )
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
        with self.lock, self.db:
            self.db.executemany("DELETE FROM pending WHERE id=?", [(x,) for x in event_ids])

    def dispatch_claim(self, mid):
        with self.lock, self.db:
            return (
                self.db.execute(
                    "INSERT OR IGNORE INTO dispatch VALUES(?,'dispatching','')", (mid,)
                ).rowcount
                == 1
            )

    def dispatch_result(self, mid, status, detail):
        with self.lock, self.db:
            self.db.execute(
                "UPDATE dispatch SET status=?,detail=? WHERE id=?", (status, detail, mid)
            )
