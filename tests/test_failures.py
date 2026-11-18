import hashlib
import json
import socket
import threading

import pytest

from session_bridge.delivery import send_peer
from session_bridge.formatting import redact
from session_bridge.scanner import Scanner, parse_record
from session_bridge.service import Service
from session_bridge.state import Store


def test_ingest_rollback_and_replay_does_not_lose_notification(tmp_path, monkeypatch):
    db = Store(tmp_path / "s.db")
    db.pair("t", "u", "c")
    service = Service(db, {})
    frame = {
        "sessions": [{"engine": "codex", "sid": "sid", "cwd": "/p"}],
        "events": [
            {
                "engine": "codex",
                "sid": "sid",
                "cwd": "/p",
                "event_id": "e1",
                "kind": "completed",
                "prompt": "task",
                "result": "done",
            }
        ],
    }
    original = service.enqueue

    def fail(*args, **kwargs):
        raise OSError("simulated crash before outbox commit")

    monkeypatch.setattr(service, "enqueue", fail)
    with pytest.raises(OSError):
        service.ingest("node", frame)
    assert db.db.execute("SELECT count(*) FROM events").fetchone()[0] == 0
    monkeypatch.setattr(service, "enqueue", original)
    assert service.ingest("node", frame) == ["e1"]
    assert db.db.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    assert db.db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1
    service.ingest("node", frame)
    assert db.db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1


def test_quoted_secret_redaction():
    text = '{"password": "example-password-123", "api_key": "example-key-123"}'
    assert "example-password" not in redact(text)
    assert "example-key" not in redact(text)


def test_empty_second_turn_does_not_reuse_first_result():
    meta = {"result": "old result", "completion": "old"}
    parse_record(
        "claude", {"type": "user", "uuid": "new", "message": {"content": "new task"}}, meta
    )
    parse_record(
        "claude",
        {
            "type": "assistant",
            "uuid": "new-final",
            "message": {"content": [], "stop_reason": "end_turn"},
        },
        meta,
    )
    assert "old" not in meta["result"]
    assert meta["prompt"] == "new task"


def test_claude_peer_is_a_distinct_turn_and_thinking_is_not_final():
    meta = {"turn": "old", "result": "old result"}
    parse_record(
        "claude",
        {
            "type": "user",
            "uuid": "peer-turn",
            "isMeta": True,
            "origin": {"kind": "peer", "from": "unknown"},
            "message": {
                "content": "Another Claude session sent a message:\n[request marker] New task\n\nThis came from another Claude session — metadata"
            },
        },
        meta,
    )
    assert meta["turn"] == "peer-turn"
    assert meta["prompt"] == "[request marker] New task"
    assert meta["result"] == ""
    event = parse_record(
        "claude",
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "thinking", "thinking": "private reasoning"}],
                "stop_reason": "end_turn",
            },
        },
        meta,
    )
    assert event is None and meta["result"] == ""
    event = parse_record(
        "claude",
        {
            "type": "assistant",
            "uuid": "final",
            "message": {
                "content": [{"type": "text", "text": "Expected result"}],
                "stop_reason": "end_turn",
            },
        },
        meta,
    )
    assert event == "completed" and meta["result"] == "Expected result"


def test_unknown_dispatch_recovery(tmp_path):
    db = Store(tmp_path / "s.db")
    alias = db.upsert_session("n", "codex", "sid", "/p")
    db.claim_command("m", alias, "run")
    db.recover()
    assert db.db.execute("SELECT status FROM commands").fetchone()[0] == "unknown"
    assert not db.claim_command("m", alias, "run")


def test_missing_snapshot_session_is_no_longer_live(tmp_path):
    db = Store(tmp_path / "s.db")
    service = Service(db, {})
    alias = db.upsert_session("n", "codex", "sid", "/p", {"live": True})
    service.ingest("n", {"sessions": [], "events": []})
    assert db.session(alias)["data"]["live"] is False


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows native peer key convention")
def test_windows_peer_key_casefold_and_process_identity(tmp_path):
    from session_bridge.delivery import peer_token

    folder = tmp_path / "sessions"
    folder.mkdir()
    address = r"\\\\.\\pipe\\LOCAL\\test-session"
    digest = hashlib.sha256(address.lower().encode()).hexdigest()
    key = folder / ("123." + digest + ".key")
    key.write_text(json.dumps({"peerToken": "test-peer-token", "procStartFt": "start-a"}))
    peer = {"pid": 123, "procStart": "start-a", "messagingSocketPath": address}
    assert peer_token(tmp_path, peer) == "test-peer-token"
    peer["procStart"] = "start-b"
    with pytest.raises(RuntimeError):
        peer_token(tmp_path, peer)


def test_claude_stop_hook_keeps_its_original_context(tmp_path):
    scanner = Scanner(tmp_path, tmp_path / "state")
    event_dir = tmp_path / "state/hook-events"
    event_dir.mkdir()
    event = {
        "id": "hook-1",
        "ts": 1,
        "session_id": "sid",
        "hook_event_name": "Stop",
        "context": {
            "prompt": "old prompt",
            "result": "old result",
            "turn": "old",
            "completion": "old-complete",
        },
    }
    (event_dir / "1.json").write_text(json.dumps(event))
    scanner.read_hooks(
        [
            {
                "engine": "claude",
                "sid": "sid",
                "prompt": "next prompt",
                "result": "next result",
                "turn": "next",
            }
        ]
    )
    row = json.loads(scanner.db.execute("SELECT body FROM pending").fetchone()[0])
    assert row["prompt"] == "old prompt" and row["result"] == "old result"


def test_hook_missing_completion_cannot_inherit_later_turn(tmp_path):
    scanner = Scanner(tmp_path, tmp_path / "state")
    event_dir = tmp_path / "state/hook-events"
    event_dir.mkdir()
    later = hashlib.sha256(json.dumps(["claude", "sid", "next", "completed"]).encode()).hexdigest()
    scanner.db.execute("INSERT INTO emitted VALUES(?)", (later,))
    scanner.db.commit()
    (event_dir / "1.json").write_text(
        json.dumps(
            {
                "id": "hook-a",
                "ts": 1,
                "session_id": "sid",
                "hook_event_name": "Stop",
                "context": {
                    "prompt": "task A",
                    "result": "result A",
                    "turn": "a",
                    "completion": None,
                },
            }
        )
    )
    scanner.read_hooks(
        [{"engine": "claude", "sid": "sid", "turn": "next", "completion": "b-final"}]
    )
    body = json.loads(scanner.db.execute("SELECT body FROM pending").fetchone()[0])
    assert body["turn"] == "a" and body["completion"] is None
    assert body["prompt"] == "task A"


def test_hook_and_late_transcript_share_turn_identity(tmp_path):
    scanner = Scanner(tmp_path, tmp_path / "state")
    scanner.initial = False
    transcript = tmp_path / "sid.jsonl"
    transcript.write_text(
        json.dumps(
            {"type": "user", "sessionId": "sid", "uuid": "turn-a", "message": {"content": "task"}}
        )
        + "\n"
    )
    meta = scanner.read_file("claude", transcript)
    folder = tmp_path / "state/hook-events"
    folder.mkdir()
    (folder / "1.json").write_text(
        json.dumps(
            {
                "id": "hook",
                "ts": 1,
                "session_id": "sid",
                "hook_event_name": "Stop",
                "context": dict(meta),
            }
        )
    )
    scanner.read_hooks([dict(meta, engine="claude")])
    with transcript.open("a") as output:
        output.write(
            json.dumps(
                {
                    "type": "assistant",
                    "uuid": "final-a",
                    "message": {"content": "done", "stop_reason": "end_turn"},
                }
            )
            + "\n"
        )
    scanner.read_file("claude", transcript)
    assert scanner.db.execute("SELECT count(*) FROM pending").fetchone()[0] == 1


@pytest.mark.skipif(
    __import__("os").name == "nt", reason="Unix socket integration runs on Linux node"
)
def test_unix_peer_auth_and_exact_message(tmp_path):
    address = str(tmp_path / "peer.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(address)
    server.listen(1)
    received = []

    def receive():
        conn, _ = server.accept()
        with conn, conn.makefile("r") as f:
            received.extend([json.loads(f.readline()), json.loads(f.readline())])

    thread = threading.Thread(target=receive)
    thread.start()
    status, _ = send_peer({"messagingSocketPath": address}, "peer-token", "exact text", "request-1")
    thread.join(timeout=3)
    server.close()
    assert status == "submitted"
    assert received[0] == {"type": "auth", "token": "peer-token"}
    assert received[1]["message"]["content"] == "exact text"
