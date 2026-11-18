"""Fast observation of original Claude hooks. Never occupy the desktop input UI."""

import json
import secrets
import time
from pathlib import Path

import psutil

from .config import private_write
from .questions import normalize_questions


def pending(state):
    result = []
    for path in (Path(state) / "interactive-requests").glob("*.json"):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
            if row["expires"] < time.time():
                path.unlink()
                continue
            process = psutil.Process(row["pid"])
            if abs(process.create_time() - row["process_created"]) > 1:
                continue
            result.append(row)
        except (OSError, ValueError, KeyError, psutil.Error):
            continue
    return result


def snapshot(row):
    return {
        "key": "transcript:" + row["tool_use_id"]
        if row.get("tool_use_id")
        else "hook:" + row["key"],
        "title": row.get("title") or "原生交互请求",
        "questions": row.get("questions") or [],
        "options": [],
        "remote": False,
    }


def answer(state, sid, argument):
    raise ValueError(
        "此 Claude 桌面连接没有可并行验证的原生作答通道，请回原电脑回答；没有把答案当新消息发给 AI。"
    )


def collect(state, data):
    event = data.get("hook_event_name")
    if event == "PreToolUse" and data.get("tool_name") != "AskUserQuestion":
        return None
    if event not in ("PreToolUse", "PermissionRequest"):
        return None
    process = psutil.Process()
    # Keep observation alive with the original CLI, not the short-lived hook or a shell wrapper.
    try:
        process = next(
            (
                p
                for p in getattr(process, "parents", list)()
                if "claude" in " ".join(p.cmdline()).lower()
            ),
            process,
        )
    except psutil.Error:
        pass
    tool_input = data.get("tool_input") or {}
    key = secrets.token_hex(20)
    row = {
        "key": key,
        "sid": data["session_id"],
        "event": event,
        "pid": process.pid,
        "process_created": process.create_time(),
        "expires": time.time() + 300,
        "remote": False,
        "tool_use_id": data.get("tool_use_id"),
        "questions": normalize_questions(tool_input.get("questions"))
        if event == "PreToolUse"
        else [],
        "title": "原生请求："
        + str(data.get("tool_name") or "")
        + (
            "\n" + json.dumps(tool_input, ensure_ascii=False)
            if event == "PermissionRequest"
            else ""
        ),
    }
    private_write(
        Path(state) / "interactive-requests" / (key + ".json"), json.dumps(row, ensure_ascii=False)
    )
    if event == "PermissionRequest":
        notice = {
            "id": key,
            "session_id": data["session_id"],
            "hook_event_name": "Notification",
            "ts": time.time(),
            "message": row["title"] + "\n此权限请求需在原电脑回答；桥接没有阻塞桌面提示。",
        }
        private_write(
            Path(state) / "hook-events" / (key + ".json"), json.dumps(notice, ensure_ascii=False)
        )
    return {}  # No permission decision, updatedInput, waiting, or replacement AI turn.
