"""Back up local private state; configure short-code launchers and the LAN history view."""

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from session_bridge.config import private_write, state_dir


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--viewer-host", default="127.0.0.1")
    args = parser.parse_args()
    state = state_dir()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    raw = (state / "config.json").read_text(encoding="utf-8")
    private_write(state / ("config.pre-v2-" + stamp + ".json"), raw)
    with (
        sqlite3.connect(str(state / "gateway.sqlite")) as source,
        sqlite3.connect(str(state / ("gateway.pre-v2-" + stamp + ".sqlite"))) as backup,
    ):
        source.backup(backup)
    config = json.loads(raw)  # Keep encrypted credentials exactly as stored.
    for node in config["nodes"]:
        if node.get("host"):
            node.setdefault(
                "launchers",
                {
                    "codex": [node["id"] + "codex", "linuxcodex", "远码"],
                    "claude": [node["id"] + "claude", "linuxclaude", "远克"],
                },
            )
            node.setdefault(
                "projects",
                [
                    {"name": "用户目录", "path": "/home/" + node["user"]},
                    {"name": "会话助手项目", "path": node["directory"]},
                ],
            )
        else:
            node.setdefault(
                "launchers",
                {
                    "codex": [node["id"] + "codex", "窗码"],
                    "claude": [node["id"] + "claude", "窗克"],
                },
            )
            node.setdefault(
                "projects",
                [
                    {"name": "会话助手项目", "path": str(Path(__file__).resolve().parent.parent)},
                    {"name": "用户目录", "path": str(Path.home())},
                ],
            )
    config["viewer"] = {
        "enabled": True,
        "host": args.viewer_host,
        "port": 8766,
        "public_url": f"http://{args.viewer_host}:8766",
    }
    private_write(state / "config.json", json.dumps(config, ensure_ascii=False, indent=2))
    print("Private backup saved; launchers configured; viewer:", config["viewer"]["public_url"])


if __name__ == "__main__":
    main()
