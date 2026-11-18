"""Opt-in live Claude peer probe; only targets its own disposable test session."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from session_bridge.config import state_dir
from session_bridge.delivery import cli_command, live_peers, peer_token, send_peer


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--wait", type=int, default=25)
    parser.add_argument(
        "--resume", help="Resume only a previously created disposable probe session"
    )
    parser.add_argument("--node", default="windows" if os.name == "nt" else "linux")
    parser.add_argument(
        "--accept-peer",
        action="store_true",
        help="Explicitly accept peers in this tool-free disposable test session only",
    )
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    home = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
    exe = (
        (Path(os.environ["APPDATA"]) / "npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe")
        if os.name == "nt"
        else cli_command("claude")[0]
    )
    sid = str(uuid.UUID(args.resume)) if args.resume else str(uuid.uuid4())
    workspace = state_dir() / "smoke"
    workspace.mkdir(parents=True, exist_ok=True)
    results = []
    proc = subprocess.Popen(
        [
            str(exe),
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--resume" if args.resume else "--session-id",
            sid,
            "--tools",
            "",
        ]
        + (["--settings", '{"crossSessionInbound":"accept"}'] if args.accept_peer else []),
        cwd=workspace,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
    )

    def read():
        for line in proc.stdout:
            try:
                item = json.loads(line)
                if item.get("type") != "result":
                    continue
                results.append(item.get("result", ""))
                print(
                    json.dumps(
                        {
                            "type": item.get("type"),
                            "subtype": item.get("subtype"),
                            "session_id": item.get("session_id"),
                            "result": item.get("result", "")[:300],
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            except ValueError:
                pass

    threading.Thread(target=read, daemon=True).start()
    try:
        proc.stdin.write(
            json.dumps(
                {
                    "type": "user",
                    "message": {"role": "user", "content": "Reply only READY. Do not use tools."},
                }
            )
            + "\n"
        )
        proc.stdin.flush()
        for _ in range(40):
            peer = live_peers(home).get(sid)
            if peer:
                print("Verified live peer " + sid, flush=True)
                raw = json.dumps([args.node, "claude", sid], separators=(",", ":"))
                print(
                    "DingTalk alias: "
                    + args.node
                    + "-C"
                    + hashlib.sha256(raw.encode()).hexdigest()[:8],
                    flush=True,
                )
                print(
                    send_peer(
                        peer,
                        peer_token(home, peer),
                        "Bridge routing probe. Reply only BRIDGE-PEER-OK. Do not use tools.",
                        "probe-" + sid,
                    ),
                    flush=True,
                )
                time.sleep(args.wait)
                if not any("BRIDGE-PEER-OK" in r for r in results):
                    raise RuntimeError(
                        "Peer transport submitted but expected model result not observed"
                    )
                return
            if proc.poll() is not None:
                raise RuntimeError(
                    "Claude exited before peer registration: " + str(proc.returncode)
                )
            time.sleep(1)
        registry = home / "sessions" / (str(proc.pid) + ".json")
        if registry.exists():
            row = json.loads(registry.read_text())
            print("Registry keys: " + ",".join(row), flush=True)
            print(
                json.dumps(
                    {k: row.get(k) for k in ("pid", "sessionId", "startedAt", "peerProtocol")}
                ),
                flush=True,
            )
        raise RuntimeError("No verified peer after 40 seconds")
    finally:
        proc.terminate()
        proc.wait(timeout=10)


if __name__ == "__main__":
    main()
