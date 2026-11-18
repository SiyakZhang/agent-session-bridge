import json

import pytest
import requests

from session_bridge.node_actions import project_path, read_transcript, visible_record
from session_bridge.service import Service
from session_bridge.state import Store
from session_bridge.viewer import HistoryViewer


def test_history_pagination_and_partial_record(tmp_path):
    path = tmp_path / "s.jsonl"
    records = [{"type": "user", "message": {"content": f"line {i}"}} for i in range(205)]
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + '\n{"incomplete":', encoding="utf-8"
    )
    session = {"path": str(path), "engine": "claude"}
    page = read_transcript(session, "")
    assert page["more"] and "line 99" in page["detail"] and "line 100" not in page["detail"]
    page2 = read_transcript(session, str(page["cursor"]))
    assert "line 100" in page2["detail"]
    page3 = read_transcript(session, str(page2["cursor"]))
    assert "line 204" in page3["detail"] and "incomplete" not in page3["detail"]
    with pytest.raises(ValueError):
        read_transcript(session, "2")


def test_private_reasoning_filtered():
    value = visible_record(
        "claude",
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "PRIVATE"},
                    {"type": "text", "text": "visible"},
                ]
            },
        },
    )
    assert "PRIVATE" not in value and "visible" in value
    assert (
        visible_record("codex", {"type": "response_item", "payload": {"type": "reasoning"}}) is None
    )
    assert (
        visible_record(
            "codex",
            {"type": "event_msg", "payload": {"type": "agent_reasoning", "text": "PRIVATE"}},
        )
        is None
    )
    assert (
        visible_record(
            "codex",
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "channel": "analysis",
                    "content": "PRIVATE",
                },
            },
        )
        is None
    )


def test_project_file_boundary(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "ok.txt").write_text("hi")
    (tmp_path / "outside.txt").write_text("outside")
    assert project_path({"cwd": str(root)}, "ok.txt") == root / "ok.txt"
    with pytest.raises(ValueError):
        project_path({"cwd": str(root)}, "../outside.txt")
    (root / ".env").write_text("secret")
    with pytest.raises(ValueError):
        project_path({"cwd": str(root)}, ".env")
    state = root / "custom-state"
    state.mkdir()
    (state / "codex-control.token").write_text("a-secret-token")
    with pytest.raises(ValueError):
        project_path({"cwd": str(root)}, "custom-state/codex-control.token", state)


def test_viewer_auth_and_escaping(tmp_path):
    db = Store(tmp_path / "s.db")
    svc = Service(db, {})
    alias = db.upsert_session("n", "codex", "sid", "/p")

    class Node:
        def action(self, *args):
            return {"status": "ok", "detail": "<script>alert(1)</script>", "more": False}

    svc.nodes["n"] = Node()
    viewer = HistoryViewer(svc, port=0)
    viewer.start()
    try:
        link = viewer.link(alias)
        response = requests.get(link, timeout=3)
        assert response.status_code == 200
        assert "&lt;script&gt;" in response.text and "<script>" not in response.text
        assert response.headers["Cache-Control"] == "no-store"
        assert requests.get(viewer.public_url + "/session/invalid", timeout=3).status_code == 403
    finally:
        viewer.server.shutdown()
        viewer.server.server_close()
        db.close()
