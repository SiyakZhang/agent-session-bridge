"""Fast local Claude lifecycle capture; no network and no model calls."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from session_bridge.config import private_write
from session_bridge.formatting import redact
from session_bridge.scanner import parse_record


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True)
    args = parser.parse_args()
    data = json.loads(sys.stdin.read(2_000_000))
    sid = str(uuid.UUID(data["session_id"]))
    state = Path(args.state)
    record = {
        k: data.get(k)
        for k in ("hook_event_name", "session_id", "cwd", "transcript_path", "notification_type")
    }
    record.update(id=str(uuid.uuid4()), ts=time.time())
    for key in ("last_assistant_message", "message"):
        record[key] = redact(data.get(key, ""))[-16000:]
    context = {}
    transcript = data.get("transcript_path")
    if transcript and data.get("hook_event_name") != "SessionStart":
        try:
            with open(transcript, "rb") as stream:
                size = os.fstat(stream.fileno()).st_size
                if size > 1024 * 1024:
                    stream.seek(size - 1024 * 1024)
                    stream.readline()
                for line in stream:
                    try:
                        parse_record("claude", json.loads(line), context)
                    except (ValueError, UnicodeError):
                        continue
            record["context"] = {
                k: context.get(k) for k in ("prompt", "result", "turn", "completion")
            }
        except OSError:
            record["context"] = {"prompt": "（Hook 未能读取本轮要求）", "result": ""}
    if data.get("hook_event_name") == "SessionStart":
        record["socket"] = os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET", "")
        # Never export a child capability to another process; normal peer policy remains in effect.
        private_write(
            state / "claude-capabilities" / f"{sid}.json", json.dumps(record, ensure_ascii=False)
        )
    else:
        private_write(
            state / "hook-events" / f"{record['id']}.json", json.dumps(record, ensure_ascii=False)
        )


if __name__ == "__main__":
    main()
