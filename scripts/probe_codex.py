"""Opt-in live CLI smoke test, creates disposable isolated test threads."""

import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from session_bridge.config import state_dir
from session_bridge.delivery import cli_command, deliver
from session_bridge.scanner import Scanner


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    workspace = state_dir() / "smoke"
    workspace.mkdir(exist_ok=True)
    proc = subprocess.Popen(
        cli_command("codex") + ["app-server", "--listen", "stdio://"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
    )
    messages = queue.Queue()

    def read():
        for line in proc.stdout:
            try:
                messages.put(json.loads(line))
            except ValueError:
                pass

    threading.Thread(target=read, daemon=True).start()

    def send(method, params, rid=None):
        data = {"method": method, "params": params}
        if rid is not None:
            data["id"] = rid
        proc.stdin.write(json.dumps(data) + "\n")
        proc.stdin.flush()

    def response(rid):
        deadline = time.time() + 30
        while time.time() < deadline:
            msg = messages.get(timeout=30)
            if msg.get("id") == rid:
                if "error" in msg:
                    raise RuntimeError(str(msg["error"]))
                return msg["result"]
        raise TimeoutError("app-server response")

    try:
        send("initialize", {"clientInfo": {"name": "bridge_smoke", "version": "0.1"}}, 1)
        response(1)
        send("initialized", {})
        ids = []
        for index in (2, 3):
            send(
                "thread/start",
                {
                    "cwd": str(workspace),
                    "approvalPolicy": "never",
                    "sandbox": "read-only",
                    "ephemeral": False,
                },
                index,
            )
            ids.append(response(index)["thread"]["id"])
        for index, sid in enumerate(ids):
            send(
                "turn/start",
                {
                    "threadId": sid,
                    "input": [{"type": "text", "text": "Reply only READY. Do not use tools."}],
                },
                10 + index,
            )
            response(10 + index)
        time.sleep(3)
        scanner = Scanner(Path.home(), workspace / "scan")
        for index, sid in enumerate(ids):
            result = deliver(
                {"sid": sid, "engine": "codex"},
                "Bridge routing smoke test " + str(index) + ". Reply only OK. Do not use tools.",
                "smoke-" + str(index),
                scanner,
            )
            print(json.dumps({"thread": sid, "delivery": result}, ensure_ascii=False), flush=True)
        time.sleep(40)
        # Read only test thread summaries; never print account configuration.
        for index, sid in enumerate(ids):
            send("thread/read", {"threadId": sid, "includeTurns": True}, 20)
            result = response(20)
            turns = result["thread"].get("turns", [])
            text = json.dumps(turns, ensure_ascii=False)
            own = "Bridge routing smoke test " + str(index)
            other = "Bridge routing smoke test " + str(1 - index)
            assert own in text and other not in text, "Queue did not reach exactly its target"
            assert len(turns) >= 2 and turns[-1]["status"] == "completed", (
                "Queued turn did not complete"
            )
            print(
                json.dumps({"thread": sid, "routing": "passed", "completed_turns": len(turns)}),
                flush=True,
            )
    finally:
        proc.terminate()
        proc.wait(timeout=10)


if __name__ == "__main__":
    main()
