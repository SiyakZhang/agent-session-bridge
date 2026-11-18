"""JSON-lines worker, run as the user's own account locally or over SSH."""

from __future__ import annotations

import argparse
import json
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from .config import private_write, state_dir
from .delivery import DeliveryUnknown, codex_live, deliver, live_peers
from .formatting import redact
from .scanner import Scanner


def instance_id(state):
    path = Path(state) / "node-id"
    if not path.exists():
        private_write(path, str(uuid.uuid4()))
    return str(uuid.UUID(path.read_text().strip()))


def emit(data):
    print(json.dumps(data, ensure_ascii=False), flush=True)


def run(node_id, state):
    uid = instance_id(state)
    scanner = Scanner(Path.home(), state)
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
    emit({"type": "hello", "node": node_id, "instance": uid, "protocol": 1})
    sessions = {}
    next_poll = 0
    while True:
        if time.monotonic() >= next_poll:
            observed, events = scanner.poll()
            peers = live_peers(scanner.claude_home)
            for session in observed:
                live = (
                    codex_live(scanner.codex_home, session["sid"])
                    if session["engine"] == "codex"
                    else session["sid"] in peers
                )
                session["live"] = live
                session["delivery"] = "available" if live else "monitor-only"
                sessions[(session["engine"], session["sid"])] = session
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
            next_poll = time.monotonic() + 4
        try:
            message = incoming.get(timeout=0.5)
        except queue.Empty:
            continue
        if message.get("type") == "shutdown":
            return
        if message.get("node") != node_id or message.get("instance") != uid:
            emit({"type": "error", "error": "Node identity mismatch"})
            continue
        if message.get("type") == "ack":
            scanner.acknowledge(message.get("events", []))
        elif message.get("type") == "send":
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
                    detail = redact(str(exc))[:1200]
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
