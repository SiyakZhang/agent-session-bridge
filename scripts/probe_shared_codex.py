"""Disposable native protocol probe. Does not attach to a user's working thread."""

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from session_bridge.codex_control import CodexControl, desktop_environment, open_terminal
from session_bridge.config import state_dir
from session_bridge.delivery import cli_command


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--desktop", action="store_true")
    args = parser.parse_args()
    state_dir().mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="control-probe-", dir=state_dir()) as folder:
        control = CodexControl(folder)
        terminal = None
        sid = None
        try:
            rpc = control.connect(create=True)
            result = rpc.call(
                "thread/start",
                {
                    "cwd": str(Path(folder).resolve()),
                    "ephemeral": False,
                    "sandbox": "read-only",
                    "approvalPolicy": "on-request",
                },
            )
            sid = result["thread"]["id"]
            read = rpc.call("thread/read", {"threadId": sid, "includeTurns": False})
            rpc.call(
                "thread/inject_items",
                {
                    "threadId": sid,
                    "items": [
                        {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": "桥接连接测试：尚未提交任务。"}
                            ],
                        }
                    ],
                },
            )
            resume = rpc.call("thread/resume", {"threadId": sid, "excludeTurns": True})
            control.registry["sessions"][sid] = {"cwd": folder, "model": result["model"]}
            control.save()
            if args.desktop:
                server = control.registry["server"]
                env = desktop_environment()
                env["SESSION_BRIDGE_CODEX_TOKEN"] = Path(server["token_file"]).read_text().strip()
                terminal = open_terminal(
                    cli_command("codex")
                    + [
                        "resume",
                        sid,
                        "--remote",
                        server["endpoint"],
                        "--remote-auth-token-env",
                        "SESSION_BRIDGE_CODEX_TOKEN",
                    ],
                    folder,
                    env,
                )
                time.sleep(3)
                assert terminal.poll() is None or (os.name != "nt" and terminal.poll() == 0)
            models = rpc.call("model/list", {})
            print(
                json.dumps(
                    {
                        "initialize": True,
                        "created": bool(sid),
                        "read_same_thread": read["thread"]["id"] == sid,
                        "models_count": len(models.get("data", [])),
                        "native_resume_same_thread": resume["thread"]["id"] == sid,
                        "desktop_process_running": bool(terminal and terminal.poll() is None),
                        "history_path_available": bool(resume["thread"].get("path")),
                    }
                )
            )
            try:
                rpc.call("thread/settings/update", {"threadId": sid, "model": result["model"]})
                print("settings_update: supported")
            except RuntimeError as exc:
                print("settings_update:", str(exc)[:250])
            session = {"sid": sid}
            efforts = control.action(session, "effort", "").get("choices", [])
            if efforts:
                control.action(session, "effort", efforts[0]["value"])
                print("effort_update: supported")
            # No tools or filesystem changes requested; interrupt this disposable turn immediately.
            sent = control.action(session, "send", "只回复 BRIDGE-CONTROL-OK，不调用工具。")
            assert sent["status"] == "accepted"
            for _ in range(30):
                if control.snapshot_one(sid)["status"] == "busy":
                    break
                time.sleep(0.1)
            steered = control.action(session, "add", "补充：不修改文件。")
            print("steer_status:", steered["status"])
            stopped = control.action(session, "stop", "")
            print("interrupt_status:", stopped["status"])
            for _ in range(20):
                if control.snapshot_one(sid)["status"] != "busy":
                    print("interrupt_observed_idle: true")
                    break
                time.sleep(0.2)
        finally:
            if terminal and terminal.poll() is None:
                proc = psutil.Process(terminal.pid)
                if sid in proc.cmdline():
                    proc.terminate()
                    proc.wait(timeout=10)
            if control.rpc:
                control.rpc.close()
            server = control.registry.get("server")
            if server:
                proc = psutil.Process(server["pid"])
                # Only the uniquely configured probe server, never other Codex processes.
                if server["token_file"] in proc.cmdline():
                    proc.terminate()
                    proc.wait(timeout=10)


if __name__ == "__main__":
    main()
