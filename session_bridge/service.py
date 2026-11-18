from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import threading
import time

from .commands import BY_NAME, help_text, parse_command, split_target
from .formatting import chunks, clip, redact
from .presentation import message_parts
from .questions import normalize_questions, parse_answer, question_text
from .telemetry import quota_text

LOG = logging.getLogger(__name__)
HELP = help_text()


class Service:
    def __init__(self, store, config):
        self.store, self.config = store, config
        self.nodes = {}
        self.viewer = None
        self.launchers = {}
        self._task_locks = {}
        self._task_locks_guard = threading.Lock()
        self.secret_values = [config.get("client_secret", "")]
        self.started = time.time()
        self.secret_values.extend(n.get("password", "") for n in config.get("nodes", []))
        for n in config.get("nodes", []):
            for engine in ("codex", "claude"):
                for entry in n.get("launchers", {}).get(engine, [n["id"] + engine]):
                    if entry in self.launchers or self.store.task(entry):
                        raise ValueError("启动入口重复或与任务代号冲突")
                    self.launchers[entry] = (n["id"], engine)

    def new_pair_code(self):
        code = secrets.token_hex(4).upper()
        self.store.put(
            "pairing",
            {"hash": hashlib.sha256(code.encode()).hexdigest(), "expires": time.time() + 1800},
        )
        return code

    def enqueue(self, chat, text, alias=None, question_id=None, kind=None):
        task = self.store.task(alias, create=True) if alias else None
        clean = redact(text, self.secret_values)
        title = redact(self.store.session_title(alias), self.secret_values)[:160] if task else ""
        parts = (
            message_parts(title, clean, self.store.session(alias)["data"], kind)
            if task
            else chunks(clean)
        )
        for part in parts:
            self.store.enqueue(
                chat,
                redact(part, self.secret_values),
                alias,
                payload={"card": True},
                question_id=question_id,
            )

    def ingest(self, node, frame):
        aliases = {}
        with self.store.transaction():
            self.store.clear_live(node)
            for session in frame.get("sessions", []):
                data = {k: v for k, v in session.items() if k != "path"}
                data["prompt"] = redact(data.get("prompt", ""), self.secret_values)
                data["result"] = redact(data.get("result", ""), self.secret_values)
                alias = self.store.upsert_session(
                    node, session["engine"], session["sid"], session.get("cwd", ""), data
                )
                aliases[(session["engine"], session["sid"])] = alias
                if data.get("status") in ("failed", "reconnecting"):
                    fault = [data.get("turn"), data["status"]]
                    if self.store.get("fault-observed:" + alias) != fault:
                        self.store.put("fault-observed:" + alias, fault)
                        self.store.put("paused:" + alias, True)
                        self.store.put("fault-paused:" + alias, True)
                offline = (
                    data.get("control_online") is False or data.get("status") == "control-offline"
                )
                previous_offline = self.store.get("control-health:" + alias, False)
                if offline != previous_offline:
                    self.store.put("control-health:" + alias, offline)
                    if offline:
                        self.store.put("paused:" + alias, True)
                        self.store.put("fault-paused:" + alias, True)
                    text = (
                        "共享控制连接中断，仍保留原会话。\n原始错误："
                        + str(data.get("control_error") or "连接不可用")
                        if offline
                        else "共享控制连接已恢复。保存的输入不会自动重跑，你可以继续发要求。"
                    )
                    for pair in self.store.pairs():
                        self.enqueue(pair["chat"], text, alias, kind="failed" if offline else None)
                if data.get("live"):
                    self.store.task(alias, create=True)
                if data.get("questions") is not None:
                    questions = data.get("questions", [])
                    qids = [
                        hashlib.sha256((alias + ":" + q["key"]).encode()).hexdigest()
                        for q in questions
                    ]
                    old_ids = self.store.get("native-questions:" + alias, [])
                    for old_id in set(old_ids) - set(qids):
                        for pair in self.store.pairs():
                            self.store.put("choice:" + old_id + ":" + pair["chat"], {})
                            self.store.finish_question_card(old_id + ":" + pair["chat"])
                        staged = self.store.db.execute(
                            "SELECT key,value FROM settings WHERE key LIKE 'choice:%'"
                        ).fetchall()
                        for entry in staged:
                            value = json.loads(entry[1])
                            if value.get("alias") == alias and value.get("native_id") == old_id:
                                self.store.put(entry[0], {})
                                self.store.finish_question_card(entry[0][7:])
                    pointer = self.store.get("task-choice:" + alias)
                    pointed = (
                        self.store.get("choice:" + pointer, {}) if isinstance(pointer, str) else {}
                    )
                    if (
                        pointed.get("kind") in ("approval", "native")
                        and pointed.get("native_id") not in qids
                    ):
                        self.store.put("choice:" + pointer, {})
                        self.store.finish_question_card(pointer)
                        self.store.put("task-choice:" + alias, None)
                    for q, qid in zip(questions, qids):
                        if qid not in old_ids:
                            for pair in self.store.pairs():
                                self.offer(
                                    pair,
                                    qid + ":" + pair["chat"],
                                    {
                                        "kind": "native" if q.get("questions") else "approval",
                                        "command": "answer",
                                        "native_id": qid,
                                        "options": q.get("options", []),
                                        "questions": normalize_questions(q.get("questions")),
                                        "request_key": q["key"],
                                        "remote": q.get("remote", True),
                                    },
                                    redact(q["title"], self.secret_values)
                                    + (
                                        ""
                                        if q.get("remote", True)
                                        else "\n此连接需回原电脑操作；下列问题仍完整转发。"
                                    ),
                                    alias,
                                )
                    self.store.put("native-questions:" + alias, qids)
            ack = []
            for event in frame.get("events", []):
                alias = aliases.get((event["engine"], event["sid"]))
                if alias is None:
                    alias = self.store.upsert_session(
                        node, event["engine"], event["sid"], event.get("cwd", ""), event
                    )
                prompt = redact(event.get("prompt", ""), self.secret_values)
                result = redact(event.get("result", ""), self.secret_values)
                fresh = self.store.add_event(
                    node + ":" + event["event_id"],
                    alias,
                    event["kind"],
                    prompt,
                    result,
                    event.get("ts"),
                )
                session = self.store.session(alias)
                kind = event["kind"]
                if fresh and kind in ("failed", "aborted", "reconnecting"):
                    self.store.put("paused:" + alias, True)
                    self.store.put("fault-paused:" + alias, True)
                    self.react_active(alias, "failed", event)
                elif fresh and kind == "completed":
                    self.react_active(alias, "completed", event)
                if (
                    fresh
                    and (
                        session["watched"]
                        or kind in ("failed", "aborted", "waiting", "reconnecting", "compacted")
                    )
                    and kind
                    in (
                        "completed",
                        "waiting",
                        "failed",
                        "aborted",
                        "reconnecting",
                        "compacting",
                        "compacted",
                    )
                ):
                    fingerprint = hashlib.sha256(
                        json.dumps(
                            [
                                kind,
                                result,
                                event.get("turn") or event.get("token") or event.get("event_id"),
                            ],
                            ensure_ascii=False,
                        ).encode()
                    ).hexdigest()
                    old = self.store.get("notice:" + alias, {})
                    if old.get("fingerprint") == fingerprint and time.time() - old.get("at", 0) < 8:
                        ack.append(event["event_id"])
                        continue
                    self.store.put(
                        "notice:" + alias, {"fingerprint": fingerprint, "at": time.time()}
                    )
                    text = result or "本轮没有记录最终文本，请查看会话。"
                    for chat in {p["chat"] for p in self.store.pairs()}:
                        self.enqueue(chat, text, alias, kind=kind)
                ack.append(event["event_id"])
        return ack

    def handle(self, mid, data):
        text = data.get("text", "").lstrip()
        tenant, sender, chat = (data.get(k, "") for k in ("tenant", "sender", "chat"))
        if not tenant or not sender or not chat:
            return
        parsed = parse_command(text)
        if parsed and parsed[0] == "pair":
            code = parsed[1].strip().upper()
            with self.store.transaction():
                pair = self.store.get("pairing", {})
                valid = pair.get("expires", 0) > time.time() and hmac.compare_digest(
                    pair.get("hash", ""), hashlib.sha256(code.encode()).hexdigest()
                )
                if valid:
                    old = self.store.pairs()
                    if old and not any(
                        p["tenant"] == tenant and p["sender"] == sender for p in old
                    ):
                        return
                    self.store.pair(
                        tenant, sender, chat, data.get("kind", "2"), data.get("staff", "")
                    )
                    self.store.put("pairing", {})
                    self.enqueue(chat, "配对成功。\n\n" + HELP)
            return
        if not self.store.authorized(tenant, sender, chat):
            return
        if data.get("message_type", "text") != "text":
            self.enqueue(chat, "当前尚未接入这种附件输入，未将空消息发送给 AI。请先用文字描述。")
            return
        rid = self.store.message_key(data, mid)
        self.store.reaction(rid, "received", data)
        target_alias = data.get("_target_alias")
        target_question = data.get("_card_selection")
        try:
            if data.get("_card_selection"):
                self.select(rid, data, data["_card_selection"], text)
                return
            if data.get("reply") is not None:
                session = self.store.resolve_reply(chat, data["reply"])
                target_alias = session.get("alias")
                target_question = session.get("_selection")
                if session.get("_selection"):
                    self.select(rid, data, session["_selection"], text)
                elif session.get("alias"):
                    self.task_input(rid, chat, session, text, data)
                else:
                    raise ValueError("请对这个选择问题回复选项数字。")
                return
            pending = self.pending_choices(data)
            if text.strip().isdigit():
                if len(pending) != 1:
                    raise ValueError("没有唯一的有效选择问题，请回复对应问题消息。")
                target_alias = pending[0][1].get("alias")
                target_question = pending[0][0]
                self.select(rid, data, pending[0][0], text)
                return
            for entry in sorted(self.launchers, key=len, reverse=True):
                if text.startswith(entry) and (
                    not text[len(entry) :]
                    or text[len(entry) :][0].isspace()
                    or text[len(entry) :][0] in "/／！!"
                ):
                    command = parse_command(text[len(entry) :])
                    if not command or command[0] != "new":
                        raise ValueError("新开会话请使用 " + entry + "！新建")
                    node_id, engine = self.launchers[entry]
                    if command[1].strip():
                        self.create_session(
                            rid, chat, node_id, engine, command[1].strip().strip('"')
                        )
                        return
                    projects = self.directories(node_id, engine)
                    self.offer(
                        data,
                        rid,
                        {
                            "kind": "create",
                            "node": node_id,
                            "engine": engine,
                            "directories": [p["path"] for p in projects],
                            "page": 0,
                            "options": self.directory_options([p["path"] for p in projects], 0),
                        },
                        "选择常用项目（回复数字，10 分钟内有效）：",
                    )
                    return
            if parsed:
                command, args = parsed
                if command in ("help", "list", "machines", "quota"):
                    self.global_command(chat, command, args)
                    return
                if command in ("send", "info", "history", "watch", "unwatch"):
                    bits = args.split(maxsplit=1)
                    session = self.resolve(bits[0]) if bits else None
                    if session:
                        target_alias = session["alias"]
                        rest = bits[1] if len(bits) > 1 else ""
                        if command == "send":
                            self.send(rid, chat, session, rest)
                        else:
                            self.task_command(rid, chat, session, command, rest)
                        return
                if command in ("new", "clear"):
                    self.enqueue(
                        chat, "清理当前对话：松！清理；新开桌面会话：启动入口 /new。入口见 ！电脑。"
                    )
                    return
                self.enqueue(chat, "未知命令或缺少任务代号，请用 ！帮助；没有转发给 AI。")
                return
            codes = {t["code"]: t["alias"] for t in self.store.tasks()}
            codes.update({s["alias"]: s["alias"] for s in self.store.sessions()})
            titles = {}
            for s in self.store.sessions():
                title = self.store.session_title(s["alias"])
                titles.setdefault(title, []).append(s["alias"])
            for title, aliases in titles.items():
                if len(aliases) == 1 and (title not in codes or codes[title] == aliases[0]):
                    codes[title] = aliases[0]
                elif len(aliases) > 1 or (title in codes and codes[title] != aliases[0]):
                    codes.pop(title, None)
                    if text.startswith(title):
                        raise ValueError(
                            "有多个同名会话，请引用对应消息，或用 ！任务 中的完整会话标识。"
                        )
            code, body = split_target(text, codes)
            if code:
                target_alias = codes[code]
                self.task_input(rid, chat, self.store.session(codes[code]), body, data)
            elif len(pending) == 1 and (
                pending[0][1].get("kind") == "directory"
                or (pending[0][1].get("kind") == "native" and pending[0][1].get("remote", True))
            ):
                self.select(rid, data, pending[0][0], text)
            else:
                self.enqueue(
                    chat, "请回复一条任务消息，或在开头写任务代号，例如 松继续修改。命令见 ！帮助。"
                )
        except ValueError as exc:
            self.store.reaction(rid, "failed")
            self.enqueue(chat, str(exc), target_alias, question_id=target_question, kind="failed")

    def directories(self, node, engine):
        paths = []
        for session in self.store.sessions():
            if session["node"] == node and session["engine"] == engine:
                paths.extend(session["data"].get("recent_directories") or [])
                if session["cwd"] and "recent_directories" not in session["data"]:
                    paths.append(session["cwd"])
        paths.extend(
            p["path"] for p in getattr(self.nodes.get(node), "config", {}).get("projects", [])
        )
        return [
            {"path": p}
            for p in dict.fromkeys(paths)
            if not any(
                part == ".dingtalk-session-bridge" or part.startswith("control-probe-")
                for part in p.replace("\\", "/").split("/")
            )
        ][:80]

    @staticmethod
    def directory_options(paths, page):
        options = [{"label": p, "value": p} for p in paths[page : page + 8]]
        if len(paths) > 8:
            options.append(
                {
                    "label": "更多目录" if page + 8 < len(paths) else "返回第一页",
                    "value": "__more__",
                }
            )
        options.append({"label": "输入其他目录", "value": "__custom__"})
        return options

    def resolve(self, code):
        canonical = self.store.session(code)
        if canonical:
            return canonical
        matches = [s for s in self.store.sessions() if self.store.session_title(s["alias"]) == code]
        if len(matches) > 1:
            raise ValueError("有多个同名会话，请引用对应消息或使用完整会话标识。")
        task = self.store.task(code)
        if task and matches and task["alias"] != matches[0]["alias"]:
            raise ValueError("标题与另一会话的历史代号相同，请引用消息或使用完整会话标识。")
        return self.store.session(task["alias"] if task else code) or (
            matches[0] if matches else None
        )

    def pending_choices(self, actor, alias=None):
        result = []
        with self.store.lock:
            rows = self.store.db.execute(
                "SELECT key,value FROM settings WHERE key LIKE 'choice:%'"
            ).fetchall()
        for row in rows:
            choice = json.loads(row[1])
            if choice.get("expires", 0) < time.time() or any(
                choice.get(k) != actor.get(k) for k in ("tenant", "sender", "chat")
            ):
                continue
            if alias and choice.get("alias") != alias:
                continue
            if choice.get("kind") in ("approval", "native") and choice.get(
                "native_id"
            ) not in self.store.get("native-questions:" + choice["alias"], []):
                continue
            result.append((row[0][7:], choice))
        return result

    def global_command(self, chat, command, args):
        if command == "help":
            self.enqueue(chat, help_text(full=args.strip().lower() in ("all", "全部")))
        elif command == "quota":
            lines = []
            for name, node in self.nodes.items():
                if node.online and hasattr(node, "action"):
                    response = node.action(
                        "quota-" + secrets.token_hex(12),
                        "codex",
                        "__quota__",
                        "quota",
                        args.strip(),
                    )
                    lines.append(name + " · " + response.get("detail", response["status"]))
            seen = set()
            for s in self.store.sessions():
                quota = s["data"].get("quota")
                key = (s["node"], json.dumps(quota, sort_keys=True))
                if key not in seen:
                    seen.add(key)
                    lines.append(s["node"] + " · " + quota_text(quota))
            self.enqueue(chat, "\n\n".join(lines) or "额度尚未读取；查询不会阻塞对话。")
        elif command == "machines":
            lines = []
            for name, node in self.nodes.items():
                online = node.online and time.time() - node.last_seen < 45
                lines.append(
                    f"{name}：{'在线' if online else '离线'}\n"
                    "启动入口：" + "、".join(k for k, v in self.launchers.items() if v[0] == name)
                )
            self.enqueue(chat, "\n\n".join(lines) or "没有配置电脑。")
        else:
            rows = self.store.sessions()
            all_rows = args.strip().lower() in ("all", "全部")
            selected = [
                s for s in rows if all_rows or s["data"].get("live") or self.store.task(s["alias"])
            ]
            lines = []
            for s in selected:
                task = self.store.task(s["alias"], create=bool(s["data"].get("live")))
                code = task["code"] if task else s["alias"]
                title = self.store.session_title(s["alias"])
                lines.append(
                    f"{title} · {self.status(s)}\n{s['node']} / {s['engine']} · 标识 `{s['alias']}`"
                    + (f" · 兼容 `{code}`" if code != title else "")
                )
            self.enqueue(
                chat, "\n\n".join(lines) or "未发现活动任务。可用 /list all 查看历史记录。"
            )

    def status(self, session):
        node = self.nodes.get(session["node"])
        if (
            not node
            or not getattr(node, "online", False)
            or time.time() - getattr(node, "last_seen", 0) > 45
        ):
            return "电脑离线"
        if session["data"].get("status") == "control-offline":
            return "共享控制连接离线"
        if not session["data"].get("live"):
            return "历史记录（未确认运行）"
        return {
            "busy": "执行中",
            "idle": "等待输入",
            "aborted": "已中断",
            "failed": "执行失败",
            "reconnecting": "正在重连",
            "compacting": "压缩中",
            "control-offline": "共享控制连接离线",
        }.get(session["data"].get("status"), "活动")

    def task_input(self, rid, chat, session, text, actor=None):
        if not text.strip():
            self.task_command(rid, chat, session, "status", "")
            return
        command = parse_command(text)
        if command:
            self.task_command(rid, chat, session, *command, actor=actor)
        else:
            if actor and not session.get("_quoted"):
                pending = self.pending_choices(actor, session["alias"])
                if len(pending) > 1 and text.strip().isdigit():
                    raise ValueError("此会话有多个待回答问题，请引用具体问题消息。")
                if len(pending) == 1 and (
                    text.strip().isdigit()
                    or (pending[0][1].get("kind") == "native" and pending[0][1].get("remote", True))
                ):
                    self.select(rid, actor, pending[0][0], text)
                    return
            self.send(rid, chat, session, text)

    def task_lock(self, alias):
        with self._task_locks_guard:
            return self._task_locks.setdefault(alias, threading.RLock())

    def task_command(self, rid, chat, session, command, args, actor=None):
        with self.task_lock(session["alias"]):
            return self._task_command(rid, chat, session, command, args, actor)

    def _task_command(self, rid, chat, session, command, args, actor=None):
        alias = session["alias"]
        task = self.store.task(alias, create=True)
        caps = session["data"].get("capabilities", [])
        if command == "quota":
            self.enqueue(chat, quota_text(session["data"].get("quota")), alias)
            return
        if command == "queue" and "queue" in caps:
            rows = self.store.queued(alias)
            display_rows = rows + self.store.unknown_queue(alias)
            if args.startswith("remove "):
                key = args.split(maxsplit=1)[1].strip()
                found = [r for r in rows if r["id"][:12] == key]
                if len(found) != 1:
                    raise ValueError("队列标识不存在或不唯一，请重新查看队列。")
                if not self.store.cancel_queued(found[0]["id"]):
                    raise ValueError("该输入已开始提交，不能再撤回。")
                self.enqueue(chat, "已撤回尚未执行的输入。", alias)
            else:
                self.enqueue(
                    chat,
                    "\n".join(
                        f"{r['id'][:12]} [{r['status']}]：{clip(r['body'], 160)}"
                        for r in display_rows
                    )
                    or "没有待处理输入。",
                    alias,
                )
            return
        if command == "continue" and "continue" in caps:
            self.store.put("paused:" + alias, False)
            self.store.put("fault-paused:" + alias, False)
            if not self.store.queued(alias):
                self.send(rid, chat, session, "继续处理上一次任务。")
            else:
                self.enqueue(chat, "已恢复待发队列。", alias)
            return
        if command not in BY_NAME:
            self.enqueue(
                chat, "未知命令。使用 ！帮助 查看；需要原样发文字可用 ！发送 内容。", alias
            )
        elif command == "help":
            self.enqueue(chat, help_text(task["code"], caps, full=args.strip() in ("all", "全部")))
        elif command == "say":
            self.send(rid, chat, session, args)
        elif command == "name":
            if "name" not in caps:
                self.enqueue(chat, "此连接未开放原生改名，请在原电脑修改会话标题。", alias)
            else:
                self.action(rid, chat, session, "name", args.strip(), actor)
        elif command == "status":
            self.enqueue(chat, self.status(session), alias)
        elif command == "info":
            d = session["data"]
            self.enqueue(
                chat,
                f"{self.store.session_title(alias)}\n电脑：{session['node']}\n"
                f"工具：{session['engine']}\n目录：{session['cwd']}\n"
                f"模型：{d.get('model') or '当前记录未提供'}\n状态：{self.status(session)}\n"
                f"会话：{session['sid']}\n兼容编号：{alias}",
                alias,
            )
        elif command == "history":
            count = int(args) if args.strip().isdigit() else 6
            rows = self.store.history(alias, max(1, min(count, 50)))
            if not rows:
                rows = [
                    {
                        "prompt": session["data"].get("prompt", ""),
                        "result": session["data"].get("result", ""),
                    }
                ]
            for i, row in enumerate(rows, 1):
                self.enqueue(
                    chat,
                    f"历史 {i}/{len(rows)}\n\n你的要求：\n{row['prompt']}\n\n结果：\n{row['result']}",
                    alias,
                )
        elif command in ("watch", "unwatch"):
            self.store.watch(alias, command == "watch")
            self.enqueue(chat, "提醒已" + ("开启" if command == "watch" else "关闭"), alias)
        elif command == "session" and "history" in caps and self.viewer:
            link = self.viewer.link(alias)
            self.enqueue(chat, f"[查看完整会话]({link})\n链接有效期 24 小时。", alias)
        elif BY_NAME[command].capability:
            capability = BY_NAME[command].capability
            if capability not in caps:
                self.enqueue(
                    chat,
                    f"当前 {session['engine']} 连接不支持远程“{BY_NAME[command].chinese}”。"
                    "请在原电脑操作；没有将命令作为文字发给 AI。",
                    alias,
                )
            else:
                self.action(
                    rid, chat, session, "clear" if command == "new" else command, args, actor
                )
        else:
            self.enqueue(chat, "该命令请不带任务代号单独发送。", alias)

    def action(self, rid, chat, session, command, args, actor=None):
        with self.task_lock(session["alias"]):
            return self._action(rid, chat, session, command, args, actor)

    def _action(self, rid, chat, session, command, args, actor=None):
        node = self.nodes.get(session["node"])
        if not node or not hasattr(node, "action"):
            self.enqueue(chat, "电脑连接暂不支持该操作。", session["alias"])
            return
        if not self.store.claim_command(
            rid, session["alias"], "/" + command + " " + redact(args, self.secret_values)
        ):
            return
        if command == "stop":
            self.store.put("paused:" + session["alias"], True)
            self.store.put("fault-paused:" + session["alias"], False)
        response = node.action(rid, session["engine"], session["sid"], command, args)
        if command == "clear" and response["status"] in ("ok", "completed"):
            self.store.advance_generation(session["alias"])
        self.store.command_result(
            rid, response["status"], redact(response.get("detail", ""), self.secret_values)
        )
        if response.get("choices") and actor:
            self.offer(
                actor,
                rid,
                {
                    "kind": "action",
                    "alias": session["alias"],
                    "command": command,
                    "options": response["choices"],
                },
                "请选择：",
                session["alias"],
            )
        elif response.get("image_base64"):
            task = self.store.task(session["alias"], create=True)
            self.enqueue(chat, response.get("detail", "图片"), session["alias"])
            self.store.enqueue(
                chat,
                task["code"] + "｜" + response.get("detail", "图片"),
                session["alias"],
                {"image_base64": response["image_base64"], "mime": response.get("mime")},
            )
        else:
            self.enqueue(
                chat,
                response.get("detail") or response["status"],
                session["alias"],
                kind="failed" if response["status"] in ("failed", "unknown") else None,
            )
        return response

    def offer(self, actor, qid, choice, title, alias=None):
        choice.update(
            tenant=actor["tenant"],
            sender=actor["sender"],
            chat=actor["chat"],
            expires=time.time() + (30 * 86400 if choice["kind"] in ("native", "approval") else 600),
            alias=alias or choice.get("alias"),
        )
        if alias:
            task = self.store.task(alias, create=True)
            choice["generation"] = task["generation"]
            old = self.store.get("task-choice:" + alias)
            if old and choice["kind"] not in ("approval", "native"):
                self.store.put("choice:" + old, {})
            self.store.put("task-choice:" + alias, qid)
        pointer = "selection:" + self.store.message_key(actor, "selection")
        if choice["kind"] == "create":
            old = self.store.get(pointer)
            if isinstance(old, str):
                self.store.put("choice:" + old, {})
            self.store.put(pointer, qid)
        self.store.put("choice:" + qid, choice)
        self.enqueue(
            actor["chat"],
            title + "\n" + question_text(choice),
            alias,
            qid,
        )

    def select(self, rid, actor, qid, text):
        choice = self.store.get("choice:" + qid, {})
        if choice.get("alias"):
            with self.task_lock(choice["alias"]):
                return self._select(rid, actor, qid, text)
        return self._select(rid, actor, qid, text)

    def _select(self, rid, actor, qid, text):
        with self.store.transaction():
            if self.store.get("selection-answer:" + rid):
                return
            choice = self.store.get("choice:" + qid, {})
            if choice.get("expires", 0) < time.time() or any(
                choice.get(k) != actor.get(k) for k in ("tenant", "sender", "chat")
            ):
                raise ValueError("这个问题已过期、已处理或不属于当前用户。")
            if choice.get("alias"):
                task = self.store.task(choice["alias"])
                if not task or task["generation"] != choice.get("generation"):
                    raise ValueError("这个问题属于旧对话，不能应用到当前任务。")
                if choice["kind"] in ("approval", "native") and choice.get(
                    "native_id"
                ) not in self.store.get("native-questions:" + choice["alias"], []):
                    raise ValueError("这个原生问题已处理或过期。")
            if choice.get("remote") is False:
                raise ValueError(
                    "此原生问题没有安全远程作答通道，请回原电脑操作；未把答案当普通文字发送。"
                )
            if choice["kind"] == "native":
                questions = choice["questions"]
                index = choice.get("question_index", 0)
                answer = parse_answer(questions[index], text)
                choice.setdefault("answers", {})[questions[index]["id"]] = {"answers": answer}
                if index + 1 < len(questions):
                    choice["question_index"] = index + 1
                    next_qid = qid + ":next"
                    self.store.put("choice:" + qid, {})
                    self.store.finish_question_card(qid)
                    self.store.put("choice:" + next_qid, choice)
                    self.store.put("selection-answer:" + rid, qid)
                    self.store.put("task-choice:" + choice["alias"], next_qid)
                    self.enqueue(actor["chat"], question_text(choice), choice["alias"], next_qid)
                    return
                option = json.dumps(
                    {"key": choice["request_key"], "answers": choice["answers"]}, ensure_ascii=False
                )
            elif choice["kind"] == "directory":
                option = text.strip().strip('"')
                if not option:
                    raise ValueError("请输入完整目录。")
            else:
                if not text.strip().isdigit():
                    raise ValueError("请回复对应选项数字。")
                index = int(text.strip()) - 1
                if index < 0 or index >= len(choice["options"]):
                    raise ValueError("选项编号超出范围。")
                option = choice["options"][index]["value"]
                if choice["kind"] == "create" and option == "__more__":
                    page = choice.get("page", 0) + 8
                    if page >= len(choice["directories"]):
                        page = 0
                    choice.update(
                        page=page, options=self.directory_options(choice["directories"], page)
                    )
                    next_qid = qid + ":page:" + secrets.token_hex(4)
                    self.store.put("choice:" + qid, {})
                    self.store.finish_question_card(qid)
                    self.store.put("choice:" + next_qid, choice)
                    self.store.put("selection-answer:" + rid, qid)
                    self.enqueue(
                        actor["chat"],
                        "请选择目录：\n" + question_text(choice),
                        question_id=next_qid,
                    )
                    return
                if choice["kind"] == "create" and option == "__custom__":
                    choice.update(kind="directory", options=[])
                    next_qid = qid + ":directory"
                    self.store.put("choice:" + qid, {})
                    self.store.finish_question_card(qid)
                    self.store.put("choice:" + next_qid, choice)
                    self.store.put("selection-answer:" + rid, qid)
                    self.enqueue(
                        actor["chat"],
                        "请输入该电脑上的完整目录（回复本条消息）：",
                        question_id=next_qid,
                    )
                    return
            self.store.put("choice:" + qid, {})
            self.store.put("selection-answer:" + rid, qid)
            self.store.finish_question_card(qid)
        if choice["kind"] in ("create", "directory"):
            self.create_session(rid, actor["chat"], choice["node"], choice["engine"], option)
        else:
            response = self.action(
                rid,
                actor["chat"],
                self.store.session(choice["alias"]),
                choice["command"],
                option,
                actor,
            )
            if (
                isinstance(response, dict)
                and response.get("status") == "failed"
                and choice["kind"] in ("native", "approval")
            ):
                with self.store.transaction():
                    if choice.get("native_id") in self.store.get(
                        "native-questions:" + choice["alias"], []
                    ):
                        self.store.put("choice:" + qid, choice)

    def send(self, mid, chat, session, body):
        with self.task_lock(session["alias"]):
            return self._send(mid, chat, session, body)

    def _send(self, mid, chat, session, body):
        alias = session["alias"]
        if not body.strip() or len(body) > 16000:
            self.enqueue(chat, "请输入 1–16000 字符的内容。", alias)
            return
        prepared = body
        if session["data"].get("managed"):
            if self.store.queue_input(mid, alias, chat, body):
                self.store.reaction(mid, "queued")
                # A new explicit message after failure may be tried once; older inputs remain saved.
                if self.store.get("fault-paused:" + alias) and session["data"].get(
                    "status"
                ) not in ("busy", "compacting", "control-offline"):
                    self._dispatch_saved(
                        session, next(r for r in self.store.queued(alias) if r["id"] == mid)
                    )
            return
        if not self.store.queue_input(mid, alias, chat, body):
            return
        if not self.store.claim_queued(mid):
            return
        if not self.store.claim_command(mid, alias, redact(body, self.secret_values)):
            return
        node = self.nodes.get(session["node"])
        response = (
            node.dispatch(mid, session["engine"], session["sid"], prepared)
            if node
            else {"status": "failed", "detail": "目标电脑未配置。"}
        )
        detail = redact(response["detail"], self.secret_values)
        self.store.command_result(mid, response["status"], detail)
        self.store.queue_result(
            mid,
            "submitted"
            if response["status"] in ("submitted", "accepted", "queued")
            else "unknown"
            if response["status"] == "unknown"
            else "saved",
        )
        self.store.add_event(
            "command:" + mid, alias, "钉钉后续要求", redact(body, self.secret_values), detail
        )
        self.store.reaction(
            mid,
            "queued"
            if response["status"] == "queued"
            else "received"
            if response["status"] in ("submitted", "accepted")
            else "failed",
        )
        if response["status"] in ("submitted", "accepted", "queued"):
            self.store.put(
                "active-receipt:" + alias,
                {
                    "rid": mid,
                    "body_hash": hashlib.sha256(
                        redact(body, self.secret_values).encode()
                    ).hexdigest(),
                    "sent_at": time.time(),
                },
            )
        else:
            self.store.put("paused:" + alias, True)
            self.store.put("fault-paused:" + alias, True)
            self.enqueue(chat, detail, alias, kind="failed")

    def create_session(self, rid, chat, node_id, engine, cwd):
        node = self.nodes.get(node_id)
        with self.store.transaction():
            if self.store.get("create:" + rid):
                return
            self.store.put("create:" + rid, "dispatching")
        if not node:
            response = {"status": "failed", "detail": "目标电脑未配置。"}
        else:
            response = node.action(rid, engine, "__new__", "new", cwd)
        self.store.put("create:" + rid, response["status"])
        if response.get("session"):
            s = response["session"]
            alias = self.store.upsert_session(node_id, engine, s["sid"], s["cwd"], s)
            self.enqueue(chat, response["detail"], alias)
        else:
            self.enqueue(chat, response.get("detail", "创建状态未知；未重复打开窗口。"))

    def drain_queue(self):
        for task in self.store.tasks():
            with self.task_lock(task["alias"]):
                self._drain_task(task["alias"])

    def _drain_task(self, alias):
        # Lock spans pause/generation checks, claim and native dispatch. Stop cannot
        # finish between a stale pause check and a new turn/start request.
        task = self.store.task(alias)
        if not task or self.store.get("paused:" + alias):
            return
        session = self.store.session(alias)
        if not session or session["data"].get("status") in (
            "busy",
            "compacting",
            "control-offline",
        ):
            return
        rows = self.store.queued(alias)
        if not rows:
            return
        row = rows[0]
        node = self.nodes.get(session["node"])
        if not node or not node.online or time.time() - node.last_seen > 45:
            return
        if row["generation"] != task["generation"]:
            self.store.queue_result(row["id"], "cancelled")
            return
        self._dispatch_saved(session, row)

    def _dispatch_saved(self, session, row):
        alias = session["alias"]
        node = self.nodes.get(session["node"])
        if not node or not node.online or time.time() - node.last_seen > 45:
            return
        if not self.store.claim_queued(row["id"]):
            return
        with self.store.lock:
            attempt = self.store.db.execute(
                "SELECT attempt FROM task_queue WHERE id=?", (row["id"],)
            ).fetchone()[0]
        request_id = row["id"] if attempt == 1 else row["id"] + ":retry:" + str(attempt)
        response = (
            node.action(request_id, session["engine"], session["sid"], "send", row["body"])
            if session["data"].get("managed")
            else node.dispatch(request_id, session["engine"], session["sid"], row["body"])
        )
        self.store.queue_result(
            row["id"],
            "pending"
            if response["status"] == "busy"
            else "saved"
            if response["status"] == "failed"
            else response["status"],
        )
        if response["status"] not in ("accepted", "submitted", "queued", "busy"):
            self.store.put("paused:" + alias, True)
            self.store.put("fault-paused:" + alias, True)
            self.store.reaction(row["id"], "failed")
            self.enqueue(row["chat"], response.get("detail", "投递失败"), alias, kind="failed")
        elif response["status"] != "busy":
            self.store.put(
                "active-receipt:" + alias,
                {
                    "rid": row["id"],
                    "turn": response.get("turn_id"),
                    "sent_at": time.time(),
                    "body_hash": hashlib.sha256(
                        redact(row["body"], self.secret_values).encode()
                    ).hexdigest(),
                },
            )
            self.store.reaction(row["id"], "processing")
            current = dict(session["data"], status="busy")
            self.store.upsert_session(
                session["node"], session["engine"], session["sid"], session["cwd"], current
            )
            if response.get("warning"):
                self.enqueue(row["chat"], response["warning"], alias, kind="failed")

    def react_active(self, alias, state, event):
        active = self.store.get("active-receipt:" + alias, {})
        if not isinstance(active, dict) or not active.get("rid"):
            return
        turn = event.get("turn") or event.get("token")
        if active.get("turn"):
            if turn != active["turn"]:
                return
        elif event.get("source_ts", 0) < active.get("sent_at", 0) or hashlib.sha256(
            event.get("prompt", "").encode()
        ).hexdigest() != active.get("body_hash"):
            return
        self.store.reaction(active["rid"], state)

    def check_health(self):
        if time.time() - self.started < 30:
            return
        for name, node in self.nodes.items():
            key = "node-health:" + name
            online = bool(node.online and time.time() - node.last_seen < 30)
            previous = self.store.get(key)
            if previous is None and online:
                self.store.put(key, True)
                continue
            if previous == online:
                continue
            self.store.put(key, online)
            text = (
                name + " 已恢复连接。保存的输入不会因恢复而自动重跑。"
                if online
                else name
                + " 连接中断，暂时无法确认原会话状态。\n原始错误："
                + redact(node.error or "节点心跳超过 30 秒未更新", self.secret_values)
            )
            for pair in self.store.pairs():
                self.enqueue(pair["chat"], text)
            if not online:
                for session in self.store.sessions():
                    if session["node"] == name:
                        self.store.put("paused:" + session["alias"], True)
                        self.store.put("fault-paused:" + session["alias"], True)

    def check_stream(self, connected):
        now = time.time()
        outage = self.store.get("stream-outage", {})
        if not connected:
            if not outage:
                outage = {"started": now, "notified": False}
            if not outage["notified"] and now - outage["started"] >= 30:
                for pair in self.store.pairs():
                    self.enqueue(
                        pair["chat"],
                        "钉钉消息接收连接已中断超过 30 秒，消息可能暂时无法接收。原始状态：Stream 未连接，未提供更多错误详情。",
                    )
                outage["notified"] = True
            self.store.put("stream-outage", outage)
        elif outage:
            if outage.get("notified") or now - outage["started"] >= 30:
                with self.store.lock:
                    missed = self.store.db.execute(
                        "SELECT count(*) FROM outbox WHERE ts>=? AND status IN ('failed','unknown')",
                        (outage["started"],),
                    ).fetchone()[0]
                text = f"钉钉接收连接已恢复，中断约 {int(now - outage['started'])} 秒。"
                if missed:
                    text += f"有 {missed} 条消息投递失败或结果未知，未自动重复发送；可在对应会话用 ！历史 / ！记录 核对。"
                for pair in self.store.pairs():
                    self.enqueue(pair["chat"], text)
            self.store.put("stream-outage", {})


def queue_worker(service, stop):
    while not stop.wait(1):
        try:
            service.drain_queue()
            service.check_health()
        except (OSError, RuntimeError, ValueError):
            LOG.exception("Queue worker failed")
