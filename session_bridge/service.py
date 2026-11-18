from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time

from .formatting import chunks, clip, context_message, redact

LOG = logging.getLogger(__name__)
HELP = """### 多电脑会话助手

- `/nodes`：电脑连接状态
- `/sessions`：会话列表（含电脑和项目）
- `/show 会话编号`：当前任务、最近结果、完整会话 ID
- `/history 会话编号`：最近几轮的要求、结果和后续回复
- `/send 会话编号 你的要求`：继续那个原会话
- `/watch 会话编号`、`/unwatch 会话编号`：开启/关闭提醒

群内请先 @机器人。每次发送都明确指定通知中的会话编号；单独发送“继续”不会自动选择会话。
“已入队/已提交”不等于“执行结束”。原 CLI 的权限审批仍在原电脑处理。
"""


class Service:
    def __init__(self, store, config):
        self.store, self.config = store, config
        self.nodes = {}
        self.secret_values = [config.get("client_secret", "")]
        self.secret_values.extend(n.get("password", "") for n in config.get("nodes", []))

    def new_pair_code(self):
        code = secrets.token_hex(4).upper()
        self.store.put(
            "pairing",
            {"hash": hashlib.sha256(code.encode()).hexdigest(), "expires": time.time() + 1800},
        )
        return code

    def enqueue(self, chat, text):
        for part in chunks(redact(text, self.secret_values)):
            self.store.enqueue(chat, part)

    def ingest(self, node, frame):
        aliases = {}
        with self.store.transaction():
            self.store.clear_live(node)
            for session in frame.get("sessions", []):
                data = {k: v for k, v in session.items() if k not in ("path",)}
                data["prompt"] = redact(data.get("prompt", ""), self.secret_values)
                data["result"] = redact(data.get("result", ""), self.secret_values)
                alias = self.store.upsert_session(
                    node, session["engine"], session["sid"], session.get("cwd", ""), data
                )
                aliases[(session["engine"], session["sid"])] = alias
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
                if fresh and session["watched"]:
                    text = context_message(session, event["kind"], prompt, result, event.get("ts"))
                    for chat in {p["chat"] for p in self.store.pairs()}:
                        self.enqueue(chat, text)
                ack.append(event["event_id"])
        return ack

    def handle(self, mid, data):
        text = data.get("text", "").strip()
        tenant, sender, chat = data.get("tenant", ""), data.get("sender", ""), data.get("chat", "")
        if not tenant or not sender or not chat:
            return
        if text.startswith("/pair "):
            code = text.split(maxsplit=1)[1].strip().upper()
            with self.store.lock:
                pair = self.store.get("pairing", {})
                valid = pair.get("expires", 0) > time.time() and hmac.compare_digest(
                    pair.get("hash", ""), hashlib.sha256(code.encode()).hexdigest()
                )
                if valid:
                    # Existing owner must authorize additional chats; a fresh code alone cannot change owner.
                    old = self.store.pairs()
                    if old and not any(
                        p["tenant"] == tenant and p["sender"] == sender for p in old
                    ):
                        return
                    self.store.pair(
                        tenant, sender, chat, data.get("kind", "2"), data.get("staff", "")
                    )
                    self.store.put("pairing", {})
                    self.enqueue(
                        chat,
                        "### 配对成功\n\n这个聊天已绑定你的身份，接收所有已监控电脑的新提醒。\n\n"
                        + HELP,
                    )
            return
        if not self.store.authorized(tenant, sender, chat):
            LOG.info("Ignored unpaired incoming message")
            return
        parts = text.split(maxsplit=2)
        command = parts[0].lower() if parts else "/help"
        if command in ("/help", "帮助", "/start"):
            self.enqueue(chat, HELP)
        elif command == "/nodes":
            lines = ["### 已配置电脑"]
            for name, node in self.nodes.items():
                live = node.online and time.time() - node.last_seen < 45
                lines.append(
                    f"- **{name}**："
                    + ("在线" if live else "离线")
                    + (" — " + node.error if node.error else "")
                )
            self.enqueue(chat, "\n\n".join(lines))
        elif command == "/sessions":
            rows = self.store.sessions()
            lines = [
                "### 最近观察到的会话\n“活动”表示确认了运行中的会话入口；“记录”仅表示发现近期会话文件。"
            ]
            for row in rows[:40]:
                node = self.nodes.get(row["node"])
                live = (
                    node
                    and node.online
                    and time.time() - node.last_seen < 45
                    and row["data"].get("live")
                )
                lines.append(
                    f"- **{row['alias']}** · {row['engine']} · "
                    + ("活动" if live else "记录")
                    + f" · {row['data'].get('status', 'unknown')}\n  {row['cwd']}\n  "
                    + clip(row["data"].get("prompt", ""), 120).replace("\n", " ")
                )
            self.enqueue(
                chat,
                "\n\n".join(lines)
                if rows
                else "尚未发现会话；请在已连接电脑上打开 Codex 或 Claude。",
            )
        elif command in ("/show", "/history", "/send", "/watch", "/unwatch"):
            if len(parts) < 2 or not (session := self.store.session(parts[1])):
                self.enqueue(
                    chat,
                    "未找到这个会话编号。请用 `/sessions` 查看并复制完整编号；没有发送到其他会话。",
                )
                return
            alias = session["alias"]
            if command == "/show":
                self.enqueue(
                    chat,
                    context_message(
                        session,
                        "activity",
                        session["data"].get("prompt", ""),
                        session["data"].get("result", ""),
                    )
                    + f"\n\n完整会话 ID：`{session['sid']}`\n\n提醒："
                    + ("开启" if session["watched"] else "关闭"),
                )
            elif command == "/history":
                history = self.store.history(alias)
                if not history:
                    self.enqueue(
                        chat,
                        context_message(
                            session,
                            "activity",
                            session["data"].get("prompt", ""),
                            session["data"].get("result", ""),
                        ),
                    )
                for index, row in enumerate(history, 1):
                    self.enqueue(
                        chat,
                        f"**{alias} · 最近记录 {index}/{len(history)}**\n\n"
                        + context_message(
                            session, row["kind"], row["prompt"], row["result"], row["ts"]
                        ),
                    )
            elif command in ("/watch", "/unwatch"):
                self.store.watch(alias, command == "/watch")
                self.enqueue(chat, f"{alias} 提醒已" + ("开启" if command == "/watch" else "关闭"))
            elif len(parts) < 3 or not parts[2].strip():
                self.enqueue(chat, f"请输入 `/send {alias} 你的要求`")
            else:
                self.send(mid, chat, session, parts[2])
        else:
            self.enqueue(
                chat,
                "请明确指定会话：`/send 会话编号 你的要求`。\n\n引用回复暂不作为自动选会话的依据，避免串会话。\n\n"
                + HELP,
            )

    def send(self, mid, chat, session, body):
        if len(body) > 16000:
            self.enqueue(chat, "输入超过 16000 字符，请缩短后发送。")
            return
        if not self.store.claim_command(mid, session["alias"], redact(body, self.secret_values)):
            self.enqueue(chat, "这条消息已处理过，没有重复执行。")
            return
        node = self.nodes.get(session["node"])
        if node is None:
            response = {"status": "failed", "detail": "目标电脑未配置。"}
        else:
            response = node.dispatch(mid, session["engine"], session["sid"], body)
        detail = redact(response["detail"], self.secret_values)
        self.store.command_result(mid, response["status"], detail)
        self.store.add_event(
            "command:" + mid,
            session["alias"],
            "钉钉后续要求",
            redact(body, self.secret_values),
            detail,
        )
        self.enqueue(
            chat,
            f"### {session['alias']} · {response['status']}\n\n"
            f"电脑：{session['node']}\n\n项目：{session['cwd']}\n\n"
            f"你的要求：{clip(body, 1000)}\n\n{detail}",
        )
