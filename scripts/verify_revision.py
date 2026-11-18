"""Isolated protocol/compaction verification; never touches a user's working thread."""

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from session_bridge.codex_control import CodexControl
from session_bridge.config import state_dir
from session_bridge.formatting import redact


def report(**values):
    print(json.dumps(values, ensure_ascii=False), flush=True)


def wait_turn(control, sid, turn_id, limit=75):
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        with control.rpc.lock:
            turn = getattr(control.rpc, "last_turns", {}).get(sid, {})
        if turn.get("id") == turn_id and turn.get("status") in (
            "completed",
            "failed",
            "interrupted",
        ):
            return turn
        time.sleep(0.3)
    return {"status": "unknown"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", action="store_true")
    args = parser.parse_args()
    root = state_dir()
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="control-probe-revision-", dir=root) as folder:
        control = CodexControl(folder)
        try:
            rpc = control.connect(create=True)
            created = rpc.call(
                "thread/start",
                {
                    "cwd": folder,
                    "ephemeral": False,
                    "sandbox": "read-only",
                    "approvalPolicy": "on-request",
                },
            )
            sid = created["thread"]["id"]
            rpc.subscribed.add(sid)
            control.registry["sessions"][sid] = {
                "cwd": folder,
                "model": created.get("model"),
                "model_provider": created.get("modelProvider"),
            }
            control.save()
            read = rpc.call("thread/read", {"threadId": sid, "includeTurns": False})
            report(
                empty_session_read_same_id=read["thread"]["id"] == sid,
                no_placeholder=True,
                model=created.get("model"),
            )
            if not args.model:
                return
            sent = control.action(
                {"sid": sid}, "send", "只回复 BRIDGE-REVISION-OK，不调用任何工具。"
            )
            first = wait_turn(control, sid, sent["turn_id"])
            resumed = rpc.call("thread/resume", {"threadId": sid, "excludeTurns": True})
            report(real_input_resume_same_id=resumed["thread"]["id"] == sid)
            report(
                first_turn=first.get("status"),
                error=redact(first.get("error")) if first.get("error") else None,
            )
            if first.get("status") != "completed":
                return
            result = control.action({"sid": sid}, "compact", "")
            report(compaction_request=result["status"])
            deadline = time.monotonic() + 100
            compact = None
            while time.monotonic() < deadline:
                with rpc.lock:
                    compact = next(
                        (
                            e
                            for e in reversed(rpc.events)
                            if e["kind"] in ("compacted", "failed", "aborted")
                        ),
                        None,
                    )
                if compact:
                    break
                time.sleep(0.2)
            report(
                compaction_result=compact["kind"] if compact else "unknown",
                error=compact["result"] if compact and compact["kind"] != "compacted" else None,
            )
            if not compact or compact["kind"] != "compacted":
                return
            sent = control.action(
                {"sid": sid}, "send", "只回复 BRIDGE-AFTER-COMPACT-OK，不调用任何工具。"
            )
            after = wait_turn(control, sid, sent["turn_id"])
            report(
                after_compaction_request=sent["status"],
                after_compaction_turn=after.get("status"),
                same_thread=True,
            )
        finally:
            if control.rpc:
                control.rpc.close()
            server = control.registry.get("server")
            if server:
                try:
                    process = psutil.Process(server["pid"])
                    if server["token_file"] in process.cmdline():
                        process.terminate()
                        process.wait(timeout=10)
                except psutil.NoSuchProcess:
                    pass


if __name__ == "__main__":
    main()
