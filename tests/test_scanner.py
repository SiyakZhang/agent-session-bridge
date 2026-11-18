import json

from session_bridge.scanner import Scanner, parse_record


def append(path, records):
    with path.open("a", encoding="utf8") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")


def test_startup_does_not_replay_but_restart_preserves_unacked_event(tmp_path):
    root = tmp_path / ".codex/sessions"
    root.mkdir(parents=True)
    path = root / "rollout.jsonl"
    append(
        path,
        [
            {"type": "session_meta", "payload": {"id": "sid", "cwd": "/project"}},
            {
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": "old", "last_agent_message": "old"},
            },
        ],
    )
    scanner = Scanner(tmp_path, tmp_path / "state")
    assert scanner.poll()[1] == []
    append(
        path,
        [
            {"type": "event_msg", "payload": {"type": "user_message", "message": "new task"}},
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "new",
                    "last_agent_message": "new result",
                },
            },
        ],
    )
    events = scanner.poll()[1]
    assert len(events) == 1 and events[0]["prompt"] == "new task"
    scanner.db.close()
    scanner = Scanner(tmp_path, tmp_path / "state")
    assert scanner.poll()[1] == events
    scanner.acknowledge([events[0]["event_id"]])
    assert scanner.poll()[1] == []


def test_partial_line_waits_until_completed(tmp_path):
    root = tmp_path / ".codex/sessions"
    root.mkdir(parents=True)
    path = root / "r.jsonl"
    append(path, [{"type": "session_meta", "payload": {"id": "sid", "cwd": "/project"}}])
    scanner = Scanner(tmp_path, tmp_path / "state")
    scanner.poll()
    line = json.dumps(
        {
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "t", "last_agent_message": "done"},
        }
    )
    with path.open("a") as f:
        f.write(line[:20])
    assert scanner.poll()[1] == []
    with path.open("a") as f:
        f.write(line[20:] + "\n")
    assert len(scanner.poll()[1]) == 1


def test_claude_tool_text_is_not_completion_and_local_turn_stays_local():
    meta = {}
    parse_record(
        "claude",
        {"type": "user", "uuid": "local", "message": {"content": "local instruction"}},
        meta,
    )
    assert (
        parse_record(
            "claude",
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "working"}],
                    "stop_reason": "tool_use",
                },
            },
            meta,
        )
        is None
    )
    assert (
        parse_record(
            "claude",
            {
                "type": "assistant",
                "uuid": "finish",
                "message": {
                    "content": [{"type": "text", "text": "done"}],
                    "stop_reason": "end_turn",
                },
            },
            meta,
        )
        == "completed"
    )
    assert meta["prompt"] == "local instruction"
    assert meta["turn"] == "local"


def test_subagent_is_hidden():
    meta = {}
    parse_record(
        "codex",
        {"type": "session_meta", "payload": {"id": "id", "parent_thread_id": "parent"}},
        meta,
    )
    assert meta["hidden"]
