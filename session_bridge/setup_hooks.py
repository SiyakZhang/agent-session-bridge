"""Install only this bridge's hooks, preserve existing settings, create a backup."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

from .config import private_write, state_dir


def install(python, project, state, claude_home=None):
    home = Path(claude_home or os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
    path = home / "settings.json"
    settings = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    backup = None
    if path.exists():
        backup = path.with_name("settings.json.bridge-backup-" + str(int(time.time())))
        shutil.copy2(path, backup)
    hook = Path(project) / "session_bridge/hook.py"
    command = f'"{Path(python).as_posix()}" "{hook.as_posix()}" --state "{Path(state).as_posix()}"'
    hooks = settings.setdefault("hooks", {})
    for event in (
        "SessionStart",
        "Stop",
        "StopFailure",
        "Notification",
        "PreCompact",
        "PostCompact",
        "PreToolUse",
        "PermissionRequest",
    ):
        entries = hooks.setdefault(event, [])
        # Replace only this exact installed command, preserving unrelated hooks in mixed groups.
        for existing in entries:
            existing["hooks"] = [
                h for h in existing.get("hooks", []) if h.get("command") != command
            ]
        entries[:] = [e for e in entries if e.get("hooks")]
        entry = {
            "hooks": [
                {
                    "type": "command",
                    "command": command,
                    "timeout": 10,
                }
            ]
        }
        if event == "Notification":
            entry["matcher"] = "elicitation_dialog"
        if event == "PreToolUse":
            entry["matcher"] = "AskUserQuestion"
        entries.append(entry)
    private_write(path, json.dumps(settings, ensure_ascii=False, indent=2))
    return str(path), str(backup) if backup else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--project", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--state", default=str(state_dir()))
    args = parser.parse_args()
    path, backup = install(args.python, args.project, args.state)
    print(json.dumps({"settings": path, "backup": backup}))


if __name__ == "__main__":
    main()
