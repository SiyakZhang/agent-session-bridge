import json

import pytest

from session_bridge import interactive


@pytest.mark.parametrize("event", ["PreToolUse", "PermissionRequest"])
def test_observer_never_blocks_or_approves_desktop_input(tmp_path, event, monkeypatch):
    def forbidden_sleep(_seconds):
        raise AssertionError("The original desktop must never wait for a phone answer")

    monkeypatch.setattr(interactive.time, "sleep", forbidden_sleep)
    data = {
        "session_id": "sid",
        "hook_event_name": event,
        "tool_name": "AskUserQuestion" if event == "PreToolUse" else "Bash",
        "tool_use_id": "native-call",
        "tool_input": {
            "questions": [{"question": "Choose?", "options": [{"label": "A"}, {"label": "B"}]}]
        },
    }
    assert interactive.collect(tmp_path, data) == {}
    path = next((tmp_path / "interactive-requests").glob("*.json"))
    row = json.loads(path.read_text(encoding="utf8"))
    assert interactive.snapshot(row)["remote"] is False
    with pytest.raises(ValueError):
        interactive.answer(
            tmp_path, "sid", json.dumps({"key": "hook:" + row["key"], "decision": "accept"})
        )
    assert not (tmp_path / "interactive-answers").exists()
    if event == "PermissionRequest":
        notice = json.loads(
            next((tmp_path / "hook-events").glob("*.json")).read_text(encoding="utf8")
        )
        assert notice["hook_event_name"] == "Notification"
