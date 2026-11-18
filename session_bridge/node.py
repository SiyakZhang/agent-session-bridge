"""JSON-lines worker, run as the user's own account locally or over SSH."""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from . import __version__
from .codex_control import CodexControl, desktop_environment, open_terminal
from .config import private_write, state_dir
from .delivery import DeliveryUnknown, cli_command, codex_live, deliver, live_peers
from .formatting import redact
from .interactive import answer as hook_answer
from .interactive import pending
from .interactive import snapshot as question_snapshot
from .node_actions import capabilities, run_action
from .scanner import Scanner
from .telemetry import QuotaPoller, quota_text, session_quota


def instance_id(state):
    path = Path(state) / "node-id"
    if not path.exists():
        private_write(path, str(uuid.uuid4()))
    return str(uuid.UUID(path.read_text().strip()))


EMIT_LOCK = threading.Lock()


def emit(data):
    with EMIT_LOCK:
        print(json.dumps(data, ensure_ascii=False), flush=True)


def run(node_id, state):
    uid = instance_id(state)
    # SSH lacks desktop variables. Import only the same user's verified GUI environment.
    desktop = desktop_environment()
    for key in (
        "DISPLAY",
        "XAUTHORITY",
        "DBUS_SESSION_BUS_ADDRESS",
        "WAYLAND_DISPLAY",
        "XDG_RUNTIME_DIR",
    ):
        if desktop.get(key):
            os.environ[key] = desktop[key]
    scanner = Scanner(Path.home(), state)
    control = CodexControl(state)
    quotas = QuotaPoller()
    incoming = queue.Queue()

    def read_input():
        for line in sys.stdin:
            try:
                if len(line) <= 100000:
                    incoming.put(json.loads(line))
            except ValueError:
                continue
        incoming.put({"type": "shutdown"})

    threading.Thread(target=read_input, daemon=True).start()
    emit({"type": "hello", "node": node_id, "instance": uid, "protocol": 1, "version": __version__})
    sessions = {}
    poll_stop = threading.Event()

    def monitor():
        while not poll_stop.is_set():
            try:
                observed, events = scanner.poll()
                shared = {s["sid"]: s for s in control.snapshots()}
                for session in observed:
                    if session["engine"] == "codex" and session["sid"] in shared:
                        managed = shared.pop(session["sid"])
                        if not managed.get("questions_known", True) and not managed.get(
                            "questions"
                        ):
                            managed.pop("questions", None)
                        observed_status = session.get("status", "idle")
                        observed_questions = session.get("questions", [])
                        session.update({k: v for k, v in managed.items() if v is not None})
                        if managed.get("status") == "control-offline" and codex_live(
                            scanner.codex_home, session["sid"]
                        ):
                            session.update(
                                managed=False, status=observed_status, questions=observed_questions
                            )
                            session["capabilities"] = []
                observed.extend(shared.values())
                directories = {}
                for session in sorted(observed, key=lambda s: s.get("modified", 0), reverse=True):
                    if session.get("cwd") and Path(session["cwd"]).is_dir():
                        directories.setdefault(session["engine"], []).append(session["cwd"])
                peers = live_peers(scanner.claude_home)
                native = pending(state)
                for session in observed:
                    live = (
                        codex_live(scanner.codex_home, session["sid"])
                        if session["engine"] == "codex"
                        else session["sid"] in peers
                    )
                    session["live"] = live
                    session["delivery"] = "available" if live else "monitor-only"
                    session["capabilities"] = list(
                        set(session.get("capabilities", [])) | set(capabilities(session))
                    )
                    if session.get("managed"):
                        session["live"] = session.get("status") != "control-offline"
                    session["recent_directories"] = list(
                        dict.fromkeys(directories.get(session["engine"], []))
                    )[:16]
                    if session["engine"] == "claude":
                        active = [
                            r
                            for r in native
                            if r["sid"] == session["sid"]
                            and r.get("tool_use_id") in session.get("pending_tools", [])
                        ]
                        if active:
                            session["questions"] = [question_snapshot(r) for r in active]
                    session["quota"] = session_quota(session)
                    sessions[(session["engine"], session["sid"])] = session
                if control.rpc:
                    with control.rpc.lock:
                        for event in getattr(control.rpc, "events", []):
                            if event.get("sid") and control.owns(event["sid"]):
                                with scanner.lock, scanner.db:
                                    scanner.db.execute(
                                        "INSERT OR IGNORE INTO pending VALUES(?,?)",
                                        (event["event_id"], json.dumps(event, ensure_ascii=False)),
                                    )
                                events.append(event)
                        control.rpc.events = []
                emit(
                    {
                        "type": "snapshot",
                        "node": node_id,
                        "instance": uid,
                        "sessions": observed,
                        "events": events,
                        "ts": time.time(),
                    }
                )
            except (OSError, RuntimeError, ValueError) as exc:
                print(redact(str(exc)), file=sys.stderr, flush=True)
            poll_stop.wait(2)

    threading.Thread(target=monitor, daemon=True).start()
    while True:
        try:
            message = incoming.get(timeout=0.5)
        except queue.Empty:
            continue
        if message.get("type") == "shutdown":
            poll_stop.set()
            return
        if message.get("node") != node_id or message.get("instance") != uid:
            emit({"type": "error", "error": "Node identity mismatch"})
            continue
        if message.get("type") == "ack":
            scanner.acknowledge(message.get("events", []))
        elif message.get("type") in ("send", "action"):
            rid = str(message.get("request_id", ""))
            response = {
                "type": "receipt",
                "node": node_id,
                "instance": uid,
                "request_id": rid,
                "engine": message.get("engine"),
                "sid": message.get("sid"),
            }
            session = sessions.get((message.get("engine"), message.get("sid")))
            if message.get("type") == "action" and message.get("action") == "quota":
                sources = (
                    (message.get("text"),)
                    if message.get("text") in ("codex", "ark")
                    else ("codex", "ark")
                )
                values = [quotas.snapshot(source) for source in sources]
                deadline = time.monotonic() + 10
                while any(q["status"] == "pending" for q in values) and time.monotonic() < deadline:
                    time.sleep(0.1)
                    values = [quotas.snapshot(source) for source in sources]
                response.update(
                    status="ok",
                    detail="本机默认登录账号的独立查询（不等于当前会话的服务余额）：\n"
                    + "\n".join(
                        quota_text(q) + ("\n" + q["error"] if q.get("error") else "")
                        for q in values
                    )
                    + (
                        "\n部分查询仍在后台进行，稍后再发 ！额度 查看。"
                        if any(q["status"] == "pending" for q in values)
                        else ""
                    ),
                    quotas=values,
                )
                emit(response)
                continue
            if message.get("type") == "action" and message.get("action") == "new":
                if not scanner.dispatch_claim(rid):
                    response.update(status="unknown", detail="创建请求已处理过，没有重复打开窗口。")
                else:
                    try:
                        cwd = str(Path(message.get("text", "")).resolve(strict=True))
                        if message.get("engine") == "codex":
                            created = control.new(cwd)
                        elif message.get("engine") == "claude":
                            sid = str(uuid.uuid4())
                            process = open_terminal(
                                cli_command("claude") + ["--session-id", sid],
                                cwd,
                                desktop_environment(),
                            )
                            time.sleep(0.3)
                            exit_code = process.poll()
                            if exit_code not in (None, 0) or (
                                os.name == "nt" and exit_code is not None
                            ):
                                raise RuntimeError("Claude 桌面终端启动失败，未确认创建会话。")
                            created = {
                                "status": "ok",
                                "detail": "已启动 Claude 桌面终端，等待原生会话就绪。",
                                "session": {
                                    "sid": sid,
                                    "engine": "claude",
                                    "cwd": cwd,
                                    "live": False,
                                    "status": "starting",
                                    "terminal_pid": process.pid,
                                },
                            }
                        else:
                            raise ValueError("Unknown engine")
                        response.update(created)
                        fresh = created["session"]
                        sessions[(fresh["engine"], fresh["sid"])] = fresh
                    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                        response.update(
                            status="unknown" if isinstance(exc, DeliveryUnknown) else "failed",
                            detail=redact(str(exc)),
                        )
                    scanner.dispatch_result(rid, response["status"], response.get("detail", ""))
                emit(response)
                continue
            if not session:
                response.update(status="failed", detail="Unknown exact session on this node")
            elif not scanner.dispatch_claim(rid):
                old = scanner.db.execute(
                    "SELECT status,detail FROM dispatch WHERE id=?", (rid,)
                ).fetchone()
                response.update(
                    status="unknown" if old[0] == "dispatching" else old[0],
                    detail=old[1] or "Duplicate request was not resent",
                )
            else:
                try:
                    if message.get("type") == "action":
                        if (
                            session["engine"] == "codex"
                            and control.owns(session["sid"])
                            and message.get("action")
                            in (
                                "name",
                                "stop",
                                "add",
                                "model",
                                "effort",
                                "compact",
                                "send",
                                "answer",
                                "resume",
                            )
                        ):
                            action_result = control.action(
                                session, message["action"], message.get("text", ""), request_id=rid
                            )
                        elif session["engine"] == "claude" and message.get("action") == "answer":
                            action_result = hook_answer(
                                state, session["sid"], message.get("text", "")
                            )
                        else:
                            action_result = run_action(
                                session, message.get("action"), message.get("text", ""), state
                            )
                        response.update(action_result)
                        status, detail = action_result["status"], action_result.get("detail", "")
                    else:
                        status, detail = deliver(session, message["text"], rid, scanner)
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                    # A timed-out external write may have succeeded; do not retry it.
                    status = (
                        "unknown"
                        if isinstance(
                            exc, (TimeoutError, subprocess.TimeoutExpired, DeliveryUnknown)
                        )
                        else "failed"
                    )
                    detail = redact(str(exc))
                if status == "busy":
                    with scanner.lock, scanner.db:
                        scanner.db.execute("DELETE FROM dispatch WHERE id=?", (rid,))
                else:
                    scanner.dispatch_result(rid, status, detail)
                response.update(status=status, detail=detail)
            emit(response)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", default="local")
    parser.add_argument("--state", default=str(state_dir()))
    parser.add_argument("--init", action="store_true")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
    if args.init:
        print(instance_id(args.state))
    else:
        run(args.id, Path(args.state))


if __name__ == "__main__":
    main()
