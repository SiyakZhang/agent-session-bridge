"""Opt-in shared Codex app server for NEW bridge-created desktop sessions.

Never resume or seize a pre-existing thread owned by a different CLI process.
The native TUI and bridge use the same authenticated loopback server.
"""

import hashlib
import json
import logging
import os
import secrets
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from .config import private_write
from .delivery import DeliveryUnknown, cli_command
from .errors import failure_message
from .questions import (
    elicitation_content,
    elicitation_questions,
    normalize_questions,
    validate_answers,
)

LOG = logging.getLogger(__name__)


class Rpc:
    def __init__(self, endpoint, token):
        try:
            import websocket
        except ImportError:
            raise RuntimeError(
                "此电脑缺少 websocket-client，暂不能创建或控制共享 Codex 会话。"
            ) from None

        self.errors = (OSError, ValueError, websocket.WebSocketException)
        try:
            self.ws = websocket.create_connection(
                endpoint,
                timeout=10,
                header=["Authorization: Bearer " + token],
                suppress_origin=True,
                enable_multithread=True,
            )
        except self.errors:
            raise ConnectionError("共享 Codex 接口连接失败。") from None
        self.ws.settimeout(None)
        self.lock = threading.RLock()
        self.waiters = {}
        self.questions = {}
        self.answering = set()
        self.epoch = secrets.token_hex(8)
        self.turns = {}
        self.last_turns = {}
        self.subscribed = set()
        self.telemetry = {}
        self.events = []
        self.compactions = {}
        self.control_requests = {}
        self.auth_generation = 0
        self.alive = True
        threading.Thread(target=self.read, daemon=True).start()
        try:
            self.call(
                "initialize",
                {
                    "clientInfo": {"name": "dingtalk_session_bridge", "version": "0.3.7"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            self.write({"method": "initialized"})
        except (OSError, RuntimeError, ValueError):
            self.close()
            raise

    def write(self, frame):
        with self.lock:
            try:
                self.ws.send(json.dumps(frame, ensure_ascii=False))
            except self.errors:
                raise DeliveryUnknown("原生连接写入状态未知，未自动重试。") from None

    def read(self):
        try:
            while self.alive:
                raw = self.ws.recv()
                if not raw:
                    break
                frame = json.loads(raw)
                self.process_frame(frame)
        except self.errors:
            LOG.info("Shared Codex connection ended")
        finally:
            self.alive = False
            with self.lock:
                for item in self.waiters.values():
                    item["event"].set()

    def process_frame(self, frame):
        with self.lock:
            if "id" in frame and "method" not in frame:
                item = self.waiters.get(str(frame["id"]))
                if item:
                    item["response"] = frame
                    item["event"].set()
            elif frame.get("method") == "serverRequest/resolved":
                self.questions.pop(str(frame.get("params", {}).get("requestId")), None)
            elif frame.get("method") in ("turn/started", "turn/completed"):
                params = frame.get("params", {})
                if params.get("threadId"):
                    self.turns[params["threadId"]] = (
                        params.get("turn", {}).get("id")
                        if frame["method"] == "turn/started"
                        else None
                    )
                    turn = params.get("turn", {})
                    self.last_turns[params["threadId"]] = turn
                    if frame["method"] == "turn/completed":
                        for key, question in list(self.questions.items()):
                            p = question.get("params", {})
                            if (
                                p.get("threadId") == params["threadId"]
                                and p.get("turnId") == turn.get("id")
                                and p.get("isBlocking", True)
                            ):
                                self.questions.pop(key, None)
                        if turn.get("error"):
                            self.native_event(
                                params, "failed", failure_message(turn["error"]), turn.get("id")
                            )
                            self.compactions.pop(params["threadId"], None)
                        elif turn.get("status") in ("interrupted", "failed"):
                            self.compactions.pop(params["threadId"], None)
                            self.native_event(
                                params,
                                "aborted" if turn["status"] == "interrupted" else "failed",
                                "本轮已中断，可以继续对话。"
                                if turn["status"] == "interrupted"
                                else "原生回合失败，未提供更多错误信息。",
                                turn.get("id"),
                            )
                        elif params["threadId"] in self.compactions:
                            # Missing contextCompaction completion is an unknown result, never success.
                            self.compactions.pop(params["threadId"], None)
                            self.native_event(
                                params,
                                "failed",
                                "压缩回合已结束，但没有收到压缩完成事件；结果未知，未自动重试。",
                                turn.get("id"),
                            )
            elif frame.get("method") == "thread/tokenUsage/updated":
                params = frame.get("params", {})
                usage = params.get("tokenUsage") or {}
                last = usage.get("last") or {}
                self.telemetry[params.get("threadId")] = {
                    "used": last.get("totalTokens"),
                    "window": usage.get("modelContextWindow"),
                    "observed_at": time.time(),
                }
            elif frame.get("method") == "account/updated":
                self.auth_generation += 1
            elif frame.get("method") == "error":
                params = frame.get("params", {})
                retrying = params.get("willRetry", False)
                self.native_event(
                    params,
                    "reconnecting" if retrying else "failed",
                    failure_message(params.get("error") or params, retrying),
                    params.get("turnId"),
                )
                if not retrying:
                    self.compactions.pop(params.get("threadId"), None)
            elif frame.get("method") in ("item/started", "item/completed"):
                params = frame.get("params", {})
                if params.get("item", {}).get("type") == "contextCompaction":
                    complete = frame["method"] == "item/completed"
                    if complete:
                        self.compactions.pop(params.get("threadId"), None)
                        self.telemetry[params.get("threadId")] = {"invalidated": True}
                    self.native_event(
                        params,
                        "compacted" if complete else "compacting",
                        "上下文压缩已完成，可以继续对话。" if complete else "正在压缩上下文。",
                        params.get("item", {}).get("id"),
                    )
            elif "id" in frame and "method" in frame:
                self.questions[str(frame["id"])] = frame

    def native_event(self, params, kind, result, token):
        sid = params.get("threadId")
        turn = params.get("turnId") or (params.get("turn") or {}).get("id")
        requests = getattr(self, "control_requests", {})
        related = {}
        if kind in ("aborted", "failed"):
            related["stop"] = requests.pop((sid, "stop", turn), None)
        if kind in ("compacting", "compacted", "aborted", "failed"):
            key = (sid, "compact", None)
            related["compact"] = (
                requests.get(key) if kind == "compacting" else requests.pop(key, None)
            )
        self.events.append(
            {
                "sid": sid,
                "engine": "codex",
                "kind": kind,
                "result": result,
                "turn": turn,
                "token": token,
                "control_requests": related,
                "await_abort_reason": kind == "aborted",
                "event_id": self.epoch + ":" + secrets.token_hex(12),
                "ts": time.time(),
            }
        )

    def call(self, method, params, timeout=25):
        rid = secrets.token_hex(12)
        item = {"event": threading.Event()}
        with self.lock:
            self.waiters[rid] = item
        try:
            self.write({"id": rid, "method": method, "params": params})
            if not item["event"].wait(timeout) or "response" not in item:
                raise DeliveryUnknown("原生接口响应未知，没有重试操作。")
            frame = item["response"]
            if "error" in frame:
                raise RuntimeError(str(frame["error"].get("message", "Native request rejected")))
            return frame.get("result", {})
        finally:
            with self.lock:
                self.waiters.pop(rid, None)

    def close(self):
        self.alive = False
        self.ws.close()


def desktop_environment():
    env = os.environ.copy()
    if os.name == "nt" or env.get("DISPLAY"):
        return env
    import psutil

    for process in psutil.process_iter(["name", "username"]):
        if process.info["name"] not in (
            "xfce4-session",
            "mate-session",
            "gnome-session-binary",
            "ukui-session",
        ):
            continue
        try:
            if process.uids().real != os.getuid():
                continue
            source = process.environ()
            for key in (
                "DISPLAY",
                "XAUTHORITY",
                "DBUS_SESSION_BUS_ADDRESS",
                "WAYLAND_DISPLAY",
                "XDG_RUNTIME_DIR",
            ):
                if source.get(key):
                    env[key] = source[key]
            if env.get("DISPLAY"):
                break
        except (psutil.Error, OSError):
            continue
    return env


def open_terminal(command, cwd, env):
    if os.name == "nt":
        return subprocess.Popen(
            command, cwd=cwd, env=env, creationflags=subprocess.CREATE_NEW_CONSOLE
        )
    if not env.get("DISPLAY"):
        raise RuntimeError("这台电脑没有可用的已登录图形桌面，未声称打开窗口。")
    terminal = shutil.which("x-terminal-emulator") or shutil.which("xterm")
    if not terminal:
        raise RuntimeError("没有找到可启动的桌面终端。")
    return subprocess.Popen(
        [terminal, "-e"] + command,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def terminal_failed(code, platform):
    return (code is not None and platform == "nt") or code not in (None, 0)


class CodexControl:
    def __init__(self, state):
        self.state = Path(state)
        self.path = self.state / "shared-codex.json"
        self.rpc = None
        self.connection_lock = threading.RLock()
        self.retry_at = 0
        self.connection_error = ""
        self.registry = (
            json.loads(self.path.read_text(encoding="utf-8"))
            if self.path.exists()
            else {"sessions": {}}
        )

    def save(self):
        private_write(self.path, json.dumps(self.registry, ensure_ascii=False))

    def connect(self, create=False):
        with self.connection_lock:
            return self._connect(create)

    def _connect(self, create=False):
        if self.rpc and self.rpc.alive:
            return self.rpc
        if not create and self.retry_at > time.time():
            raise RuntimeError(self.connection_error)
        server = self.registry.get("server")
        if server:
            token = Path(server["token_file"]).read_text(encoding="utf-8").strip()
            try:
                self.rpc = Rpc(server["endpoint"], token)
                return self.rpc
            except (OSError, RuntimeError, ValueError) as exc:
                self.connection_error = "共享 Codex 服务离线，未自动重开原会话。原始错误：" + str(
                    exc
                )
                self.retry_at = time.time() + 15
                if not create:
                    raise RuntimeError(self.connection_error) from None
                # Explicit NEW may create a fresh server. Old threads stay attached to their old endpoint.
                for meta in self.registry["sessions"].values():
                    meta.setdefault("server_endpoint", server["endpoint"])
                self.registry.setdefault("retired_servers", []).append(server)
        if not create:
            raise RuntimeError("不是桥接创建的共享 Codex 会话。")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        token = secrets.token_urlsafe(32)
        token_file = (self.state / "codex-control.token").resolve()
        private_write(token_file, token)
        endpoint = f"ws://127.0.0.1:{port}"
        command = cli_command("codex") + [
            "app-server",
            "--listen",
            endpoint,
            "--ws-auth",
            "capability-token",
            "--ws-token-file",
            str(token_file),
        ]
        options = (
            {"creationflags": subprocess.CREATE_NO_WINDOW}
            if os.name == "nt"
            else {"start_new_session": True}
        )
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **options,
        )
        self.registry["server"] = {
            "endpoint": endpoint,
            "token_file": str(token_file),
            "pid": process.pid,
        }
        self.save()
        for _ in range(50):
            if process.poll() is not None:
                raise RuntimeError("共享 Codex 服务启动失败。")
            try:
                self.rpc = Rpc(endpoint, token)
                return self.rpc
            except (OSError, RuntimeError, ValueError):
                time.sleep(0.1)
        raise RuntimeError("共享 Codex 服务未能就绪。")

    def new(self, cwd):
        root = Path(cwd).resolve(strict=True)
        if not root.is_dir():
            raise ValueError("项目路径不是目录。")
        env = desktop_environment()
        if os.name != "nt" and not env.get("DISPLAY"):
            raise RuntimeError("目标电脑没有已登录图形桌面。")
        rpc = self.connect(create=True)
        result = rpc.call("thread/start", {"cwd": str(root), "ephemeral": False})
        thread = result["thread"]
        sid = thread["id"]
        if hasattr(rpc, "subscribed"):
            rpc.subscribed.add(sid)
        self.registry["sessions"][sid] = {
            "cwd": str(root),
            "path": thread.get("path"),
            "model": result.get("model"),
            "model_provider": result.get("modelProvider"),
            "created": time.time(),
            "quota_binding": self.quota_binding(rpc, result.get("modelProvider")),
            "terminal_pending": True,
            "empty": True,
        }
        self.save()
        return {
            "status": "ok",
            "detail": "空会话已创建。回复本条消息发送第一条实际任务，随后打开同一会话的桌面终端。",
            "session": self.snapshot_one(sid),
        }

    def open_desktop(self, sid):
        meta = self.registry["sessions"][sid]
        if meta.get("empty"):
            raise ValueError("空会话尚无可恢复记录，请先发送第一条实际任务。")
        server = self.registry["server"]
        env = desktop_environment()
        env["SESSION_BRIDGE_CODEX_TOKEN"] = Path(server["token_file"]).read_text().strip()
        command = cli_command("codex") + [
            "resume",
            sid,
            "--remote",
            server["endpoint"],
            "--remote-auth-token-env",
            "SESSION_BRIDGE_CODEX_TOKEN",
        ]
        process = open_terminal(command, meta["cwd"], env)
        time.sleep(0.3)
        if terminal_failed(process.poll(), os.name):
            raise RuntimeError("桌面终端立即退出；原会话及任务已保留。")
        meta.update(terminal_pid=process.pid, terminal_pending=False)
        self.save()
        return {"status": "ok", "detail": "已打开同一会话的桌面终端。"}

    @staticmethod
    def quota_binding(rpc, provider):
        if provider != "openai":
            return {}
        try:
            settings = rpc.call("config/read", {"includeLayers": False}).get("config") or {}
            config = (settings.get("model_providers") or {}).get("openai") or {}
            for url in (
                config.get("base_url"),
                settings.get("chatgpt_base_url"),
                os.environ.get("OPENAI_BASE_URL"),
            ):
                if url and urlparse(url).hostname not in ("api.openai.com", "chatgpt.com"):
                    return {}
            account = rpc.call("account/read", {"refreshToken": False}).get("account") or {}
            identity = account.get("id") or account.get("email")
            if account.get("type") != "chatgpt" or not identity:
                return {}
            return {
                "auth_mode": "chatgpt",
                "provider": provider,
                "account_fingerprint": hashlib.sha256(identity.encode()).hexdigest()[:16],
                "epoch": rpc.epoch,
                "auth_generation": getattr(rpc, "auth_generation", 0),
                "verified_at": time.time(),
            }
        except (OSError, ValueError, RuntimeError):
            return {}

    def owns(self, sid):
        return sid in self.registry["sessions"]

    def snapshot_one(self, sid):
        meta = self.registry["sessions"][sid]
        if meta.get("server_endpoint") and meta["server_endpoint"] != self.registry.get(
            "server", {}
        ).get("endpoint"):
            raise RuntimeError("该会话原共享服务已离线；保留原会话，未在新服务中接管。")
        rpc = self.connect()
        result = rpc.call("thread/read", {"threadId": sid, "includeTurns": False}, timeout=4)
        thread = result["thread"]
        if (
            hasattr(rpc, "subscribed")
            and sid not in rpc.subscribed
            and meta.get("empty") is not True
            and (thread.get("path") or meta.get("path") or meta.get("empty") is False)
        ):
            # Rejoin only an already-owned thread on the same authenticated server.
            resumed = rpc.call("thread/resume", {"threadId": sid, "excludeTurns": True}, timeout=4)
            if resumed.get("thread", {}).get("id") != sid:
                raise RuntimeError("原生恢复返回不同会话，拒绝控制。")
            rpc.subscribed.add(sid)
        busy = thread.get("status", {}).get("type") == "active"
        compacting = bool(getattr(rpc, "compactions", {}).get(sid))
        active = rpc.turns.get(sid) if busy else None
        self.registry["sessions"][sid]["active_turn"] = active
        questions = []
        with rpc.lock:
            for key, frame in rpc.questions.items():
                params = frame.get("params", {})
                if params.get("threadId") != sid or key in rpc.answering:
                    continue
                if frame.get("method") == "item/tool/requestUserInput":
                    questions.append(
                        {
                            "key": rpc.epoch + ":" + key,
                            "title": "等待你回答"
                            if params.get("isBlocking", True)
                            else "待补充，任务可继续",
                            "questions": normalize_questions(params.get("questions")),
                            "options": [],
                            "remote": True,
                        }
                    )
                    continue
                if frame.get("method") == "mcpServer/elicitation/request" and elicitation_questions(
                    params
                ):
                    questions.append(
                        {
                            "key": rpc.epoch + ":" + key,
                            "title": params.get("message") or "MCP 表单",
                            "questions": elicitation_questions(params),
                            "options": [],
                            "remote": True,
                        }
                    )
                    continue
                if frame.get("method") == "item/permissions/requestApproval":
                    questions.append(
                        {
                            "key": rpc.epoch + ":" + key,
                            "title": (params.get("reason") or "权限申请")
                            + "\n"
                            + json.dumps(params.get("permissions"), ensure_ascii=False),
                            "remote": True,
                            "options": [
                                {
                                    "label": label,
                                    "value": json.dumps(
                                        {"key": rpc.epoch + ":" + key, "grant": scope}
                                    ),
                                }
                                for label, scope in (
                                    ("仅本轮允许", "turn"),
                                    ("本会话允许", "session"),
                                    ("拒绝", "deny"),
                                )
                            ],
                        }
                    )
                    continue
                if frame.get("method") not in (
                    None,
                    "item/commandExecution/requestApproval",
                    "item/fileChange/requestApproval",
                ):
                    questions.append(
                        {
                            "key": rpc.epoch + ":" + key,
                            "title": "原生交互请求："
                            + frame["method"]
                            + "\n"
                            + json.dumps(params, ensure_ascii=False),
                            "options": [],
                            "remote": False,
                        }
                    )
                    continue
                allowed = params.get("availableDecisions")
                options = allowed if allowed is not None else ["accept", "decline"]
                questions.append(
                    {
                        "key": rpc.epoch + ":" + key,
                        "remote": True,
                        "title": str(
                            params.get("command") or params.get("reason") or "文件修改权限请求"
                        ),
                        "options": [
                            {
                                "label": {
                                    "accept": "本次允许",
                                    "acceptForSession": "本会话允许",
                                    "decline": "拒绝",
                                    "cancel": "取消本轮",
                                }.get(d, d)
                                if isinstance(d, str)
                                else "原生选项：" + json.dumps(d, ensure_ascii=False),
                                "value": json.dumps({"key": rpc.epoch + ":" + key, "decision": d}),
                            }
                            for d in options
                        ],
                    }
                )
        return {
            "sid": sid,
            "engine": "codex",
            "cwd": meta["cwd"],
            "path": thread.get("path") or meta.get("path"),
            "model": thread.get("model") or meta.get("model"),
            "model_provider": thread.get("modelProvider") or meta.get("model_provider"),
            "title": thread.get("name") or thread.get("preview"),
            "context": getattr(rpc, "telemetry", {}).get(sid),
            "quota_binding": meta.get("quota_binding")
            if (meta.get("quota_binding") or {}).get("epoch") == rpc.epoch
            and (meta.get("quota_binding") or {}).get("auth_generation")
            == getattr(rpc, "auth_generation", 0)
            else {},
            "live": True,
            "modified": thread.get("updatedAt", time.time()),
            "status": "compacting" if compacting else "busy" if busy else "idle",
            "active_turn": active,
            "capabilities": [
                "name",
                "stop",
                "add",
                "compact",
                "model",
                "effort",
                "queue",
                "continue",
                "resume",
            ],
            "managed": True,
            "control_online": True,
            "questions": questions,
            "questions_known": not hasattr(rpc, "subscribed") or sid in rpc.subscribed,
        }

    def snapshots(self):
        if not self.registry["sessions"]:
            return []
        rows = []
        for sid in list(self.registry["sessions"]):
            try:
                rows.append(self.snapshot_one(sid))
            except (OSError, RuntimeError, ValueError) as exc:
                LOG.info("Shared session unavailable: %s", sid)
                rows.append(
                    dict(
                        self.registry["sessions"][sid],
                        sid=sid,
                        engine="codex",
                        live=False,
                        managed=True,
                        control_online=False,
                        status="control-offline",
                        control_error=str(exc),
                        questions=[],
                    )
                )
        return rows

    def action(self, session, command, argument, request_id=None):
        sid = session["sid"]
        if not self.owns(sid):
            raise ValueError("不属于此共享服务的原会话，拒绝控制。")
        rpc = self.connect()
        current = self.snapshot_one(sid)
        if command == "answer":
            answer = json.loads(argument)
            epoch, key = answer["key"].split(":", 1)
            with rpc.lock:
                frame = rpc.questions.get(key)
                if (
                    epoch != rpc.epoch
                    or not frame
                    or key in rpc.answering
                    or frame.get("params", {}).get("threadId") != sid
                ):
                    raise ValueError("这个审批已处理或过期，没有重复作答。")
                if frame.get("method") == "item/tool/requestUserInput":
                    answers = answer.get("answers") or {}
                    validate_answers(normalize_questions(frame["params"].get("questions")), answers)
                    result = {"answers": answers}
                elif frame.get("method") == "mcpServer/elicitation/request":
                    result = {
                        "action": "accept",
                        "content": elicitation_content(
                            frame["params"], answer.get("answers") or {}
                        ),
                    }
                elif frame.get("method") == "item/permissions/requestApproval":
                    scope = answer.get("grant")
                    if scope not in ("turn", "session", "deny"):
                        raise ValueError("权限答案不在原请求范围内。")
                    result = {
                        "permissions": {} if scope == "deny" else frame["params"]["permissions"],
                        "scope": "turn" if scope == "deny" else scope,
                    }
                else:
                    if frame.get("method") not in (
                        None,
                        "item/commandExecution/requestApproval",
                        "item/fileChange/requestApproval",
                    ):
                        raise ValueError("此类原生交互需回电脑操作。")
                    decision = answer.get("decision")
                    allowed = frame.get("params", {}).get("availableDecisions")
                    if (allowed is None and decision not in ("accept", "decline")) or (
                        allowed is not None and decision not in allowed
                    ):
                        raise ValueError("审批选项不在原生允许范围内。")
                    result = {"decision": decision}
                rpc.answering.add(key)
                rpc.write({"id": frame["id"], "result": result})
            return {"status": "submitted", "detail": "审批答案已提交，是否生效以原生请求状态为准。"}
        if current["status"] == "busy" and not current["active_turn"]:
            current["active_turn"] = session.get("turn")
        if command in ("stop", "add"):
            if not current["active_turn"]:
                return {"status": "failed", "detail": "当前没有执行中的回合。"}
            if command == "stop":
                turn = current["active_turn"]
                key = (sid, "stop", turn)
                with rpc.lock:
                    if request_id:
                        rpc.control_requests.setdefault(key, request_id)
                try:
                    rpc.call("turn/interrupt", {"threadId": sid, "turnId": turn})
                except DeliveryUnknown:
                    raise
                except (OSError, RuntimeError, ValueError):
                    with rpc.lock:
                        if rpc.control_requests.get(key) == request_id:
                            rpc.control_requests.pop(key, None)
                    raise
                return {"status": "accepted", "detail": "已请求中断，正在等待原生状态更新。"}
            if not argument.strip():
                raise ValueError("请输入补充内容。")
            rpc.call(
                "turn/steer",
                {
                    "threadId": sid,
                    "expectedTurnId": current["active_turn"],
                    "input": [{"type": "text", "text": argument}],
                },
            )
            return {"status": "accepted", "detail": "补充已被当前回合接受。"}
        if command == "compact":
            if self.registry["sessions"][sid].get("empty"):
                raise ValueError("这个空会话没有可压缩的上下文，请先发送实际任务。")
            if current["status"] in ("busy", "compacting"):
                raise ValueError("请先停止当前执行，再压缩上下文。")
            rpc.compactions[sid] = time.time()
            key = (sid, "compact", None)
            with rpc.lock:
                if request_id:
                    rpc.control_requests[key] = request_id
            try:
                rpc.call("thread/compact/start", {"threadId": sid})
            except DeliveryUnknown:
                raise
            except (OSError, RuntimeError, ValueError):
                rpc.compactions.pop(sid, None)
                with rpc.lock:
                    if rpc.control_requests.get(key) == request_id:
                        rpc.control_requests.pop(key, None)
                raise
            return {"status": "accepted", "detail": "压缩请求已接受。"}
        if command == "model" and not argument.strip():
            models = rpc.call("model/list", {})["data"]
            return {
                "status": "ok",
                "detail": "\n".join(m["id"] for m in models),
                "choices": [
                    {"label": m.get("displayName") or m["id"], "value": m["id"]} for m in models
                ],
            }
        if command == "name":
            if not argument.strip() or len(argument) > 160:
                raise ValueError("请输入 1–160 字符的原生会话标题。")
            rpc.call("thread/name/set", {"threadId": sid, "name": argument.strip()})
            return {"status": "ok", "detail": "原会话标题已更新。"}
        if command in ("model", "effort"):
            if current["status"] == "busy":
                raise ValueError("请等待当前回合结束后调整模型设置。")
            value = argument.strip()
            if command == "model":
                models = rpc.call("model/list", {})["data"]
                if value not in {m["id"] for m in models}:
                    raise ValueError("模型不在当前账号可选列表中，请用 ！模型 查看。")
            else:
                models = rpc.call("model/list", {})["data"]
                model = next((m for m in models if m["id"] == current.get("model")), {})
                efforts = [e["reasoningEffort"] for e in model.get("supportedReasoningEfforts", [])]
                if not value:
                    return {
                        "status": "ok",
                        "detail": "\n".join(efforts),
                        "choices": [{"label": e, "value": e} for e in efforts],
                    }
                if value not in efforts:
                    raise ValueError("该模型不支持这个推理强度，请用 ！强度 查看。")
            rpc.call("thread/settings/update", {"threadId": sid, command: value})
            self.registry["sessions"][sid][command] = value
            self.save()
            return {"status": "ok", "detail": "已更新：" + value}
        if command == "send":
            if current["status"] in ("busy", "compacting"):
                return {"status": "busy", "detail": "当前回合仍在执行。"}
            result = rpc.call(
                "turn/start", {"threadId": sid, "input": [{"type": "text", "text": argument}]}
            )
            rpc.turns[sid] = result.get("turn", {}).get("id")
            meta = self.registry["sessions"][sid]
            meta["empty"] = False
            warning = None
            if hasattr(rpc, "subscribed") and sid not in rpc.subscribed:
                try:
                    resumed = rpc.call(
                        "thread/resume", {"threadId": sid, "excludeTurns": True}, timeout=4
                    )
                    if resumed.get("thread", {}).get("id") == sid:
                        rpc.subscribed.add(sid)
                except (OSError, RuntimeError, ValueError) as exc:
                    warning = "实际任务已提交，实时订阅暂未确认，仍监控原记录。原始错误：" + str(
                        exc
                    )
            try:
                if meta.get("terminal_pending"):
                    self.open_desktop(sid)
                else:
                    self.save()
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                warning = "实际任务已提交，但桌面窗口或状态保存未确认完成。原始错误：" + str(exc)
            return {
                "status": "accepted",
                "detail": "已提交到共享会话。",
                "turn_id": rpc.turns[sid],
                "warning": warning,
            }
        if command == "resume":
            import psutil

            pid = self.registry["sessions"][sid].get("terminal_pid")
            if pid:
                try:
                    if sid in psutil.Process(pid).cmdline():
                        return {"status": "ok", "detail": "同一会话的桌面终端仍在运行。"}
                except psutil.Error:
                    pass
            return self.open_desktop(sid)
        raise ValueError("该原生操作尚未实现。")
