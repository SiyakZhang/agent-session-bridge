import json
import threading

import pytest

from session_bridge.codex_control import CodexControl, Rpc, terminal_failed
from session_bridge.delivery import DeliveryUnknown
from session_bridge.questions import parse_answer


class FakeRpc:
    alive = True
    epoch = "epoch"

    def __init__(self):
        self.lock = threading.RLock()
        self.questions = {}
        self.answering = set()
        self.turns = {}
        self.calls = []
        self.writes = []
        self.busy = False
        self.compactions = {}
        self.telemetry = {}
        self.events = []

    def call(self, method, params, timeout=25):
        self.calls.append((method, params))
        if method == "thread/read":
            return {
                "thread": {
                    "id": params["threadId"],
                    "status": {"type": "active" if self.busy else "idle"},
                    "model": "model",
                }
            }
        if method == "thread/start":
            return {"thread": {"id": "fresh"}, "model": "model", "modelProvider": "custom"}
        if method == "thread/resume":
            return {"thread": {"id": params["threadId"]}}
        if method == "turn/start":
            return {"turn": {"id": "new-turn"}}
        if method == "model/list":
            return {
                "data": [
                    {"id": "model", "supportedReasoningEfforts": [{"reasoningEffort": "high"}]}
                ]
            }
        return {}

    def write(self, frame):
        self.writes.append(frame)


@pytest.fixture
def control(tmp_path):
    control = CodexControl(tmp_path)
    control.registry = {"sessions": {"sid": {"cwd": str(tmp_path)}}}
    control.rpc = FakeRpc()
    return control


def test_busy_send_never_starts_another_turn(control):
    control.rpc.busy = True
    response = control.action({"sid": "sid"}, "send", "task")
    assert response["status"] == "busy"
    assert not any(c[0] == "turn/start" for c in control.rpc.calls)


def test_stop_and_add_bind_current_turn(control):
    control.rpc.busy = True
    control.rpc.turns["sid"] = "turn-a"
    control.action({"sid": "sid"}, "add", "new constraint")
    assert control.rpc.calls[-1] == (
        "turn/steer",
        {
            "threadId": "sid",
            "expectedTurnId": "turn-a",
            "input": [{"type": "text", "text": "new constraint"}],
        },
    )
    control.action({"sid": "sid"}, "stop", "")
    assert control.rpc.calls[-1] == ("turn/interrupt", {"threadId": "sid", "turnId": "turn-a"})


def test_model_effort_uses_catalog_and_native_settings(control):
    with pytest.raises(ValueError):
        control.action({"sid": "sid"}, "model", "invented")
    with pytest.raises(ValueError):
        control.action({"sid": "sid"}, "effort", "low")
    control.action({"sid": "sid"}, "effort", "high")
    assert control.rpc.calls[-1] == (
        "thread/settings/update",
        {"threadId": "sid", "effort": "high"},
    )


def test_approval_epoch_thread_and_duplicate_guard(control):
    control.rpc.questions["1"] = {
        "id": 1,
        "params": {"threadId": "sid", "availableDecisions": ["accept", "decline"]},
    }
    with pytest.raises(ValueError):
        control.action({"sid": "sid"}, "answer", json.dumps({"key": "old:1", "decision": "accept"}))
    with pytest.raises(ValueError):
        control.action(
            {"sid": "sid"}, "answer", json.dumps({"key": "epoch:1", "decision": "acceptForSession"})
        )
    control.action({"sid": "sid"}, "answer", json.dumps({"key": "epoch:1", "decision": "decline"}))
    assert control.rpc.writes == [{"id": 1, "result": {"decision": "decline"}}]
    with pytest.raises(ValueError):
        control.action(
            {"sid": "sid"}, "answer", json.dumps({"key": "epoch:1", "decision": "accept"})
        )


def test_locally_resolved_approval_cannot_be_answered(control):
    with pytest.raises(ValueError):
        control.action(
            {"sid": "sid"}, "answer", json.dumps({"key": "epoch:1", "decision": "accept"})
        )


def test_native_question_submits_numeric_option_label_without_decoding_twice(control):
    question = {
        "id": "size",
        "question": "选择尺寸",
        "isOther": False,
        "options": [{"label": label} for label in ("128", "256", "512")],
    }
    control.rpc.questions["1"] = {
        "id": 1,
        "method": "item/tool/requestUserInput",
        "params": {"threadId": "sid", "questions": [question]},
    }
    with pytest.raises(ValueError, match="只接受列出的选项"):
        control.action(
            {"sid": "sid"},
            "answer",
            json.dumps({"key": "epoch:1", "answers": {"size": {"answers": ["2"]}}}),
        )
    decoded = parse_answer(question, "2")
    answers = {"size": {"answers": decoded}}
    control.action({"sid": "sid"}, "answer", json.dumps({"key": "epoch:1", "answers": answers}))
    assert control.rpc.writes == [{"id": 1, "result": {"answers": {"size": {"answers": ["256"]}}}}]


def test_no_existing_session_takeover(control):
    with pytest.raises(ValueError):
        control.action({"sid": "another-user-session"}, "stop", "")
    assert not control.rpc.calls


def test_rpc_response_matching_timeout_and_cleanup():
    rpc = Rpc.__new__(Rpc)
    rpc.lock = threading.RLock()
    rpc.waiters = {}

    def write(frame):
        item = rpc.waiters[frame["id"]]
        item["response"] = {"id": frame["id"], "result": {"same": frame["params"]}}
        item["event"].set()

    rpc.write = write
    assert rpc.call("read", {"threadId": "a"}) == {"same": {"threadId": "a"}}
    assert not rpc.waiters
    rpc.write = lambda frame: None
    with pytest.raises(DeliveryUnknown):
        rpc.call("mutate", {}, timeout=0.001)
    assert not rpc.waiters


@pytest.mark.parametrize(
    "code,platform,expected",
    [
        (None, "nt", False),
        (1, "nt", True),
        (0, "nt", True),
        (0, "posix", False),
        (1, "posix", True),
        (None, "posix", False),
    ],
)
def test_terminal_failure_detection(code, platform, expected):
    assert terminal_failed(code, platform) == expected


def test_empty_creation_has_no_placeholder_or_desktop_until_real_input(control, monkeypatch):
    monkeypatch.setattr(
        "session_bridge.codex_control.desktop_environment", lambda: {"DISPLAY": ":1"}
    )
    windows = []
    monkeypatch.setattr(control, "open_desktop", lambda sid: windows.append(sid))
    response = control.new(str(control.state))
    assert response["session"]["sid"] == "fresh"
    assert "空会话" in response["detail"] and "???" not in response["detail"]
    assert not windows
    assert not any(call[0] == "thread/inject_items" for call in control.rpc.calls)
    with pytest.raises(ValueError, match="空会话"):
        control.action({"sid": "fresh"}, "compact", "")
    sent = control.action({"sid": "fresh"}, "send", "真实任务")
    assert sent["status"] == "accepted" and windows == ["fresh"]
    start = next(c for c in control.rpc.calls if c[0] == "turn/start")
    assert start[1]["input"] == [{"type": "text", "text": "真实任务"}]


def test_reconnect_resubscribes_owned_persisted_thread_but_not_empty(control):
    control.rpc.subscribed = set()
    control.registry["sessions"]["sid"].update(empty=True, path="/not-materialized.jsonl")
    control.snapshot_one("sid")
    assert not any(c[0] == "thread/resume" for c in control.rpc.calls)
    control.registry["sessions"]["sid"]["empty"] = False
    control.snapshot_one("sid")
    assert "sid" in control.rpc.subscribed
    assert ("thread/resume", {"threadId": "sid", "excludeTurns": True}) in control.rpc.calls
