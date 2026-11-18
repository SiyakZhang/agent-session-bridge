"""Persistent short-code and reply association layer; no guessing from quoted codes."""

import hashlib
import json
import time

POOLS = (
    {"codex": "松柏竹杉柳枫榆桐", "claude": "猫犬兔鼠牛羊马猪"},
    {"codex": "梅兰菊荷莲桃杏梨", "claude": "虎鹿狐狼狮豹熊象"},
    {"codex": "桂槐榕棕椰棠樟梧", "claude": "鹰鹤燕雀鸥鹭鹊鸽"},
)


class TaskState:
    def init_tasks(self):
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS code_pools(node TEXT PRIMARY KEY, pool TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS tasks(
            code TEXT PRIMARY KEY, alias TEXT UNIQUE NOT NULL, name TEXT NOT NULL DEFAULT '',
            generation INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS sent_links(
            chat TEXT NOT NULL, message_id TEXT NOT NULL, outbox_id INTEGER NOT NULL,
            PRIMARY KEY(chat,message_id));
          CREATE TABLE IF NOT EXISTS task_queue(
            id TEXT PRIMARY KEY, alias TEXT NOT NULL, chat TEXT NOT NULL, body TEXT NOT NULL,
            generation INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created REAL NOT NULL);
        """)
        queue_columns = {r[1] for r in self.db.execute("PRAGMA table_info(task_queue)")}
        if "attempt" not in queue_columns:
            self.db.execute("ALTER TABLE task_queue ADD COLUMN attempt INTEGER NOT NULL DEFAULT 0")
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(outbox)")}
        for name, definition in {
            "alias": "TEXT",
            "generation": "INTEGER",
            "receipt": "TEXT",
            "not_before": "REAL DEFAULT 0",
            "error": "TEXT",
            "payload": "TEXT",
            "question_id": "TEXT",
        }.items():
            if name not in cols:
                self.db.execute(f"ALTER TABLE outbox ADD COLUMN {name} {definition}")
        self.db.commit()

    def register_pool(self, node, pool=None):
        with self.transaction():
            row = self.db.execute("SELECT pool FROM code_pools WHERE node=?", (node,)).fetchone()
            if row:
                current = json.loads(row[0])
                if pool and pool != current:
                    raise ValueError("已登记电脑的代号池不可自动替换")
                return current
            used = {
                ch
                for r in self.db.execute("SELECT pool FROM code_pools")
                for value in json.loads(r[0]).values()
                for ch in value
            }
            if pool is None:
                pool = next((p for p in POOLS if not used.intersection("".join(p.values()))), None)
            if not pool or set(pool) != {"codex", "claude"}:
                raise ValueError("请为新电脑配置独立的 Codex/Claude 中文代号池")
            if not all(isinstance(v, str) and v for v in pool.values()):
                raise ValueError("代号池必须是非空字符串")
            chars = "".join(pool.values())
            if (
                not all(isinstance(v, str) and v for v in pool.values())
                or len(set(chars)) != len(chars)
                or used.intersection(chars)
                or any(not ch.isalpha() or ch.isnumeric() for ch in chars)
            ):
                raise ValueError("代号池必须是互不重复的非数字字母/汉字")
            self.db.execute("INSERT INTO code_pools VALUES(?,?)", (node, json.dumps(pool)))
            return pool

    def task(self, alias, create=False):
        with self.transaction():
            row = self.db.execute(
                "SELECT * FROM tasks WHERE alias=? OR code=?", (alias, alias)
            ).fetchone()
            if row:
                return dict(row)
            session = self.session(alias)
            if not create or not session:
                return None
            used = {r[0] for r in self.db.execute("SELECT code FROM tasks")}
            title = session["data"].get("title") or session["data"].get("prompt") or ""
            title = " ".join(title.split())[:48].strip()
            code = title or session["engine"] + "·" + session["sid"][:8]
            if code in used:
                code += "·" + hashlib.sha256(alias.encode()).hexdigest()[:8]
            self.db.execute(
                "INSERT INTO tasks(code,alias,created) VALUES(?,?,?)", (code, alias, time.time())
            )
            return self.task(alias)

    def session_title(self, alias):
        session = self.session(alias)
        if not session:
            return "会话"
        title = session["data"].get("title") or session["data"].get("prompt") or ""
        return " ".join(title.split()) or session["engine"] + "·" + session["sid"][:8]

    def tasks(self):
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM tasks ORDER BY created")]

    def task_name(self, alias, name):
        if not name.strip() or len(name) > 80 or "\n" in name:
            raise ValueError("任务名称应为 1–80 个字符且不换行")
        with self.transaction():
            self.task(alias, create=True)
            self.db.execute("UPDATE tasks SET name=? WHERE alias=?", (name, alias))

    def link_sent(self, oid, receipt):
        """A processQueryKey is a receipt, NOT a platform message ID."""
        with self.transaction():
            row = self.db.execute("SELECT chat FROM outbox WHERE id=?", (oid,)).fetchone()
            self.db.execute("UPDATE outbox SET receipt=? WHERE id=?", (json.dumps(receipt), oid))
            if row and isinstance(receipt, dict):
                if receipt.get("processQueryKey"):
                    self.db.execute(
                        "INSERT OR IGNORE INTO sent_links VALUES(?,?,?)",
                        (row[0], "process:" + str(receipt["processQueryKey"]), oid),
                    )
                for key in ("msgId", "messageId", "openMessageId", "openMsgId"):
                    if receipt.get(key):
                        self.db.execute(
                            "INSERT OR IGNORE INTO sent_links VALUES(?,?,?)",
                            (row[0], str(receipt[key]), oid),
                        )

    def resolve_reply(self, chat, reply):
        with self.lock:
            rows = []
            if reply.get("process_key"):
                rows = self.db.execute(
                    """SELECT o.* FROM outbox o JOIN sent_links l
                    ON l.outbox_id=o.id WHERE l.chat=? AND l.message_id=?
                    AND o.status IN ('sent','unknown')""",
                    (chat, "process:" + reply["process_key"]),
                ).fetchall()
                if not rows:
                    raise ValueError("无法确认被引用的机器人消息，请另发带代号的新消息。")
            if reply.get("id"):
                id_rows = self.db.execute(
                    """SELECT o.* FROM outbox o JOIN sent_links l
                    ON l.outbox_id=o.id WHERE l.chat=? AND l.message_id=?
                    AND o.status IN ('sent','unknown')""",
                    (chat, reply["id"]),
                ).fetchall()
                if rows and id_rows and {r["id"] for r in rows} != {r["id"] for r in id_rows}:
                    raise ValueError("引用标识不一致，没有转发。")
                if not rows:
                    rows = id_rows
            # Fallback only to an exact sent body from the authenticated callback's bot sender.
            # Never extract a code from quotation text; repeated text with different bindings is ambiguous.
            if (
                not reply.get("id")
                and not reply.get("process_key")
                and not rows
                and reply.get("from_bot")
                and reply.get("text")
            ):
                rows = self.db.execute(
                    """SELECT * FROM outbox WHERE chat=? AND body=?
                    AND status IN ('sent','unknown')""",
                    (chat, reply["text"]),
                ).fetchall()
            bindings = {(r["alias"], r["generation"], r["question_id"]) for r in rows}
            if len(bindings) != 1:
                raise ValueError("无法确认这条引用属于哪个任务，请用代号发送；没有猜测目标。")
            alias, generation, question = next(iter(bindings))
            if question and not alias:
                return {"_selection": question}
            if not alias:
                raise ValueError("这条消息没有绑定单个任务，请另发带代号的消息。")
            task = self.task(alias)
            if not task or task["generation"] != generation:
                raise ValueError("引用属于清理前的旧对话，请查看历史或明确指定当前任务。")
            if reply.get("id") and len(rows) == 1:
                # A signed, paired callback supplies the genuine quoted bot message ID.
                # Reject contradictions above before remembering it.
                with self.transaction():
                    self.db.execute(
                        "INSERT OR IGNORE INTO sent_links VALUES(?,?,?)",
                        (chat, reply["id"], rows[0]["id"]),
                    )
            session = self.session(alias)
            if question:
                session["_selection"] = question
            session["_quoted"] = True
            return session

    def cancel_queued(self, rid):
        with self.transaction():
            return (
                self.db.execute(
                    "UPDATE task_queue SET status='cancelled' WHERE id=? AND status IN ('pending','saved')",
                    (rid,),
                ).rowcount
                == 1
            )

    def advance_generation(self, alias):
        with self.transaction():
            self.db.execute("UPDATE tasks SET generation=generation+1 WHERE alias=?", (alias,))
            self.db.execute(
                "UPDATE task_queue SET status='cancelled' WHERE alias=? AND status='pending'",
                (alias,),
            )

    def queue_input(self, rid, alias, chat, body):
        with self.transaction():
            task = self.task(alias, create=True)
            return (
                self.db.execute(
                    "INSERT OR IGNORE INTO task_queue(id,alias,chat,body,generation,created) VALUES(?,?,?,?,?,?)",
                    (rid, alias, chat, body, task["generation"], time.time()),
                ).rowcount
                == 1
            )

    def queued(self, alias):
        with self.lock:
            return [
                dict(r)
                for r in self.db.execute(
                    "SELECT * FROM task_queue WHERE alias=? AND status IN ('pending','saved') ORDER BY created",
                    (alias,),
                )
            ]

    def claim_queued(self, rid):
        with self.transaction():
            return (
                self.db.execute(
                    "UPDATE task_queue SET status='dispatching',attempt=attempt+1 WHERE id=? AND status IN ('pending','saved')",
                    (rid,),
                ).rowcount
                == 1
            )

    def queue_result(self, rid, status):
        with self.transaction():
            self.db.execute("UPDATE task_queue SET status=? WHERE id=?", (status, rid))

    def unknown_queue(self, alias):
        with self.lock:
            return [
                dict(r)
                for r in self.db.execute(
                    "SELECT * FROM task_queue WHERE alias=? AND status='unknown' ORDER BY created",
                    (alias,),
                )
            ]

    @staticmethod
    def message_key(data, mid):
        return hashlib.sha256(
            json.dumps(
                [data.get("tenant"), data.get("chat"), data.get("sender"), mid], ensure_ascii=False
            ).encode()
        ).hexdigest()
