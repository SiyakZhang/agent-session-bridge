import json

from session_bridge.scanner import Scanner, parse_record


def append(path, records):
    with path.open("a", encoding="utf8") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")


def test_revision_backfills_old_cursor_telemetry_without_replaying_events(tmp_path):
    root = tmp_path / ".codex/sessions"
    root.mkdir(parents=True)
    path = root / "old.jsonl"
    append(
        path,
        [
            {"type": "session_meta", "payload": {"id": "sid", "cwd": "/p"}},
            {
                "type": "event_msg",
                "timestamp": "2026-11-17T12:00:00Z",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {"total_tokens": 12345},
                        "model_context_window": 100000,
                    },
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "old",
                    "last_agent_message": "old final",
                },
            },
        ],
    )
    scanner = Scanner(tmp_path, tmp_path / "state")
    with scanner.db:
        scanner.db.execute(
            "INSERT INTO cursors VALUES(?,?,?)",
            (str(path), path.stat().st_size, json.dumps({"sid": "sid", "status": "idle"})),
        )
    sessions, events = scanner.poll()
    assert not events
    assert sessions[0]["context"]["used"] == 12345
    assert sessions[0]["context"]["observed_at"] < __import__("time").time()


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


def test_subagent_inherited_parent_header_cannot_replace_identity(tmp_path):
    root = tmp_path / ".codex/sessions"
    root.mkdir(parents=True)
    scanner = Scanner(tmp_path, tmp_path / "state")
    scanner.poll()
    path = root / "child.jsonl"
    append(
        path,
        [
            {
                "type": "session_meta",
                "payload": {
                    "id": "child",
                    "session_id": "parent",
                    "parent_thread_id": "parent",
                    "source": {"subagent": {}},
                    "thread_source": "subagent",
                    "cwd": "/p",
                },
            },
            {"type": "session_meta", "payload": {"id": "parent", "source": "cli", "cwd": "/p"}},
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "child-turn",
                    "root_turn_id": "root-turn",
                    "last_agent_message": "Internal English review report",
                },
            },
        ],
    )
    meta = scanner.read_file("codex", path)
    assert meta["hidden"] and meta["sid"] == "child"
    assert scanner.db.execute("SELECT count(*) FROM pending").fetchone()[0] == 0


def test_existing_bad_cursor_is_rebound_from_file_header(tmp_path):
    root = tmp_path / ".codex/sessions"
    root.mkdir(parents=True)
    path = root / "child.jsonl"
    append(
        path,
        [
            {
                "type": "session_meta",
                "payload": {
                    "id": "child",
                    "parent_thread_id": "parent",
                    "source": {"subagent": {}},
                    "cwd": "/p",
                },
            },
            {"type": "session_meta", "payload": {"id": "parent", "source": "cli", "cwd": "/p"}},
        ],
    )
    scanner = Scanner(tmp_path, tmp_path / "state")
    with scanner.db:
        scanner.db.execute(
            "INSERT INTO cursors VALUES(?,?,?)",
            (str(path), path.stat().st_size, json.dumps({"sid": "parent", "hidden": False})),
        )
    append(
        path,
        [
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "child-turn",
                    "last_agent_message": "English report",
                },
            }
        ],
    )
    sessions, events = scanner.poll()
    assert sessions == [] and events == []
    meta = json.loads(scanner.db.execute("SELECT meta FROM cursors").fetchone()[0])
    assert meta["hidden"] and meta["sid"] == "child"


def test_child_completion_in_main_transcript_does_not_finish_main_turn():
    meta = {"sid": "root", "status": "busy", "turn": "main"}
    event = parse_record(
        "codex",
        {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "child",
                "root_turn_id": "main",
                "last_agent_message": "Internal report",
            },
        },
        meta,
    )
    assert event is None and meta["status"] == "busy" and meta["turn"] == "main"
    event = parse_record(
        "codex",
        {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "main",
                "root_turn_id": "main",
                "last_agent_message": "Final user-facing summary",
            },
        },
        meta,
    )
    assert event == "completed" and meta["result"] == "Final user-facing summary"


def test_large_fork_bootstrap_keeps_child_identity(tmp_path):
    root = tmp_path / ".codex/sessions"
    root.mkdir(parents=True)
    path = root / "child.jsonl"
    append(
        path,
        [
            {
                "type": "session_meta",
                "payload": {"id": "child", "parent_thread_id": "parent", "cwd": "/p"},
            },
            {
                "type": "response_item",
                "payload": {"type": "function_call_output", "output": "x" * 1100000},
            },
            {"type": "session_meta", "payload": {"id": "parent", "source": "cli", "cwd": "/p"}},
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "child-turn",
                    "last_agent_message": "Review",
                },
            },
        ],
    )
    scanner = Scanner(tmp_path, tmp_path / "state")
    assert scanner.poll() == ([], [])
    meta = json.loads(scanner.db.execute("SELECT meta FROM cursors").fetchone()[0])
    assert meta["hidden"] and meta["sid"] == "child"


def test_private_native_probe_does_not_notify(tmp_path):
    root = tmp_path / ".codex/sessions"
    root.mkdir(parents=True)
    state = tmp_path / "state"
    scanner = Scanner(tmp_path, state)
    scanner.poll()
    path = root / "probe.jsonl"
    append(
        path,
        [
            {
                "type": "session_meta",
                "payload": {"id": "probe", "cwd": str(state / "control-probe-example")},
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "test",
                    "last_agent_message": "BRIDGE-TEST",
                },
            },
        ],
    )
    meta = scanner.read_file("codex", path)
    assert meta["hidden"]
    assert scanner.db.execute("SELECT count(*) FROM pending").fetchone()[0] == 0
