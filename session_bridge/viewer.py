"""Expiring per-session links. History stays on its source machine."""

import hashlib
import html
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

from .formatting import redact


class HistoryViewer:
    def __init__(self, service, host="127.0.0.1", port=8766, public_url=None):
        self.service = service
        self.public_url = (public_url or f"http://127.0.0.1:{port}").rstrip("/")
        viewer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # URL paths contain bearer capabilities: never log them.

            def do_GET(self):
                split = urlsplit(self.path)
                path = split.path.split("/")
                if len(path) != 3 or path[0] or path[1] not in ("session", "result") or not path[2]:
                    self.respond(404, "Not found")
                    return
                _, route, token = path
                entry = viewer.service.store.get(
                    "view:" + hashlib.sha256(token.encode()).hexdigest()
                )
                if not entry or entry.get("expires", 0) < time.time():
                    self.respond(403, "链接已过期，请重新发送任务的 ！记录。")
                    return
                if route == "result":
                    self.show_result(entry)
                    return
                if entry.get("event_id") or "document" in entry:
                    self.respond(403, "此链接只用于打开对应结果。")
                    return
                session = viewer.service.store.session(entry["alias"])
                node = viewer.service.nodes.get(session["node"]) if session else None
                if not node:
                    self.respond(503, "电脑未连接。")
                    return
                try:
                    offset = int(parse_qs(split.query).get("offset", ["0"])[0])
                    response = node.action(
                        "view-" + secrets.token_hex(16),
                        session["engine"],
                        session["sid"],
                        "session",
                        str(offset),
                    )
                    if response["status"] != "ok":
                        self.respond(503, response.get("detail", "读取失败"))
                        return
                    content = response["detail"]
                    content = redact(content, viewer.service.secret_values)
                    task = viewer.service.store.task(entry["alias"])
                    title = (
                        (task["code"] + "｜" + (task["name"] or "完整会话")) if task else "历史会话"
                    )
                    next_link = ""
                    if response.get("more"):
                        next_link = (
                            '<a href="?'
                            + urlencode({"offset": response["cursor"]})
                            + '">下一页</a>'
                        )
                    body = (
                        '<!doctype html><html lang="zh"><meta charset="utf-8">'
                        '<meta name="viewport" content="width=device-width,initial-scale=1">'
                        "<title>" + html.escape(title) + "</title>"
                        "<style>body{max-width:1000px;margin:24px auto;padding:0 16px;font:16px system-ui;}"
                        "pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f5f7;padding:16px;}"
                        "nav{display:flex;gap:24px;position:sticky;top:0;background:white;padding:16px;}</style>"
                        "<h1>"
                        + html.escape(title)
                        + "</h1><p>原始记录中的可见消息、工具调用与输出；"
                        "已过滤私有推理和可识别凭证。按时间从早到晚分页。</p>"
                        '<nav><a href="?offset=0">首页</a><a href="">刷新</a>'
                        + next_link
                        + "</nav>"
                        "<pre>" + html.escape(content) + "</pre>" + next_link + "</html>"
                    )
                    self.respond(200, body, raw=True)
                except (ValueError, OSError, KeyError):
                    self.respond(400, "无法读取此页，请重新打开会话链接。")

            def show_result(self, entry):
                from .result_view import result_page

                if "document" in entry:
                    title = redact(entry["title"], viewer.service.secret_values)
                    text = redact(entry["document"], viewer.service.secret_values)
                    self.respond(200, result_page(title, "document", text), raw=True)
                    return
                if not entry.get("event_id"):
                    self.respond(403, "此链接只用于打开会话历史。")
                    return
                with viewer.service.store.lock:
                    row = viewer.service.store.db.execute(
                        "SELECT kind,result FROM events WHERE id=? AND alias=?",
                        (entry["event_id"], entry["alias"]),
                    ).fetchone()
                if not row:
                    self.respond(404, "未找到这条结果。")
                    return
                title = redact(
                    viewer.service.store.session_title(entry["alias"]), viewer.service.secret_values
                )
                result = redact(row["result"], viewer.service.secret_values)
                self.respond(200, result_page(title, row["kind"], result), raw=True)

            def respond(self, code, body, raw=False):
                payload = (body if raw else '<meta charset="utf-8">' + html.escape(body)).encode(
                    "utf-8"
                )
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'",
                )
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer((host, port), Handler)
        if port == 0 and not public_url:
            self.public_url = f"http://127.0.0.1:{self.server.server_port}"

    def start(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def link(self, alias):
        return self._link("session", alias)

    def result_link(self, alias, event_id):
        return self._link("result", alias, event_id=event_id)

    def document_link(self, title, text):
        return self._link("result", None, title=title, document=text)

    def _link(self, route, alias, **scope):
        token = secrets.token_hex(20)
        now = time.time()
        store = self.service.store
        with store.transaction():
            if now - store.get("viewer-last-cleanup", 0) >= 3600:
                store.db.execute(
                    "DELETE FROM settings WHERE key LIKE 'view:%' AND json_extract(value,'$.expires')<?",
                    (now,),
                )
                store.put("viewer-last-cleanup", now)
            store.put(
                "view:" + hashlib.sha256(token.encode()).hexdigest(),
                dict(scope, alias=alias, expires=now + 86400),
            )
        return f"{self.public_url}/{route}/{token}"
