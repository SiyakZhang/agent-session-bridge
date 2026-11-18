"""Gateway-only API tests: remote nodes do not install the DingTalk SDK."""

import pytest

from session_bridge.bot import DingAPI, RateLimited, SendUncertain


@pytest.mark.parametrize(
    "status,exception", [(429, RateLimited), (502, SendUncertain), (200, SendUncertain)]
)
def test_non_json_send_classification(monkeypatch, status, exception):
    class Response:
        status_code = status

        def json(self):
            raise ValueError("not json")

    api = DingAPI({"client_id": "test"})
    monkeypatch.setattr(api, "access_token", lambda: "test")
    monkeypatch.setattr("session_bridge.bot.requests.post", lambda *a, **k: Response())
    with pytest.raises(exception):
        api.send({"kind": "2", "chat": "chat"}, "hello")


@pytest.mark.parametrize("plain", [True, False])
def test_send_uses_only_chat_messages_even_with_a_legacy_card_template(monkeypatch, plain):
    import json

    class Response:
        status_code = 200

        def json(self):
            return {"processQueryKey": "receipt"}

    api = DingAPI({"client_id": "test", "card_template_id": "old-template"})
    monkeypatch.setattr(api, "access_token", lambda: "test")
    calls = []
    monkeypatch.setattr(
        "session_bridge.bot.requests.post",
        lambda url, **kw: calls.append((url, kw["json"])) or Response(),
    )
    body = "原问题\n\n1. 甲\n\n2. 乙\n\n3. 丙" if plain else "| A | B |"
    receipt = api.send({"kind": "2", "chat": "chat"}, body, {"card": True, "plain_text": plain})
    url, payload = calls[0]
    assert url.endswith("/groupMessages/send") and receipt["processQueryKey"] == "receipt"
    assert payload["msgKey"] == ("sampleText" if plain else "sampleMarkdown")
    assert json.loads(payload["msgParam"])["content" if plain else "text"] == body


def test_plain_question_with_missing_receipt_is_uncertain(monkeypatch):
    class Response:
        status_code = 200

        def json(self):
            return {}

    api = DingAPI({"client_id": "test"})
    monkeypatch.setattr(api, "access_token", lambda: "test")
    monkeypatch.setattr("session_bridge.bot.requests.post", lambda *a, **kw: Response())
    with pytest.raises(SendUncertain):
        api.send({"kind": "2", "chat": "chat"}, "1. 甲", {"plain_text": True})


@pytest.mark.parametrize("success", [False, None])
def test_reaction_requires_explicit_success(monkeypatch, success):
    class Response:
        status_code = 200

        def json(self):
            return {"success": success}

    api = DingAPI({"client_id": "test"})
    monkeypatch.setattr(api, "access_token", lambda: "test")
    monkeypatch.setattr("session_bridge.bot.requests.post", lambda *a, **kw: Response())
    with pytest.raises(RuntimeError):
        api.react({"robot": "test", "open_id": "bot-id", "chat": "chat"}, "completed")


@pytest.mark.parametrize(
    "initial_state,reject_long",
    [("received", False), ("processing", False), ("received", True)],
)
def test_summary_and_receipt_use_distinct_reaction_ids(
    monkeypatch, tmp_path, initial_state, reject_long
):
    from types import SimpleNamespace

    from session_bridge.bot import reaction_worker
    from session_bridge.state import Store

    class Response:
        status_code = 200

        def json(self):
            return {"success": True}

    class Rejected:
        status_code = 500

        def json(self):
            return {"code": "system.err"}

    api = DingAPI({"client_id": "test"})
    monkeypatch.setattr(api, "access_token", lambda: "test")
    initial_label = {"received": "👌收到", "processing": "🤔处理中"}[initial_state]
    visible = {"2659900": (initial_label, "im_bg_1")}
    full_label = "s6.2 xh 69/?"

    def post(url, *, json, **kwargs):
        assert json["openMsgId"] == "user-message"
        assert json["openConversationId"] == "chat"
        emotion = json["textEmotion"]
        emotion_id = emotion["emotionId"]
        value = (emotion["text"], emotion["backgroundId"])
        if url.endswith("/emotion/recall"):
            assert initial_state == "processing"
            assert visible.pop(emotion_id) == value
        else:
            assert url.endswith("/emotion/reply")
            if reject_long and emotion["text"] == full_label:
                return Rejected()
            visible[emotion_id] = value
        return Response()

    monkeypatch.setattr("session_bridge.bot.requests.post", post)
    db = Store(tmp_path / "db.sqlite")
    db.reaction("r", initial_state, {"platform_msg_id": "user-message", "chat": "chat"})
    db.reacted("r", initial_state)
    with db.transaction():
        db.db.executemany(
            "INSERT INTO reactions(id,chat,open_id,state,metric) VALUES(?,?,?,?,?)",
            [
                ("legacy-1", "chat", "user-message", "旧重试", 2),
                ("legacy-2", "chat", "user-message", "旧指标", 3),
                ("legacy-3", "chat", "user-message", "model:S", 0),
            ],
        )
    db.queue_input_metrics("r", full_label)
    db.queue_input_metrics("r", "s6.2 xh 70/?")

    class Stop:
        iterations = 0

        def wait(self, _seconds):
            self.iterations += 1
            with db.transaction():
                db.db.execute("UPDATE reactions SET updated=0")
            return self.iterations > 3

    try:
        reaction_worker(SimpleNamespace(store=db, secret_values=[]), api, Stop())
        assert db.pending_reaction() is None
        final_label = "s6.2 xh" if reject_long else full_label
        summary = db.db.execute("SELECT state,sent_state FROM reactions WHERE metric=4").fetchone()
        assert tuple(summary) == (final_label, final_label)
    finally:
        db.close()
    assert visible == {
        "2659900": ("👌收到", "im_bg_1"),
        "54054": (final_label, "im_bg_5"),
    }


def test_reaction_retry_does_not_recall_already_removed_state(tmp_path):
    from types import SimpleNamespace

    from session_bridge.bot import reaction_worker
    from session_bridge.state import Store

    db = Store(tmp_path / "db.sqlite")
    db.reaction("r", "received", {"platform_msg_id": "m", "chat": "chat"})
    db.reacted("r", "received")
    db.reaction("r", "queued")
    calls = []

    class API:
        def react(self, row, state, recall=False):
            calls.append((state, recall))
            if len(calls) == 2:
                raise RuntimeError("temporary reply failure after successful recall")

    class Stop:
        iterations = 0

        def wait(self, _seconds):
            self.iterations += 1
            with db.transaction():
                db.db.execute("UPDATE reactions SET retry_at=0,updated=0")
            return self.iterations > 2

    try:
        reaction_worker(SimpleNamespace(store=db, secret_values=[]), API(), Stop())
        assert calls == [("received", True), ("queued", False), ("queued", False)]
        assert db.db.execute("SELECT sent_state FROM reactions").fetchone()[0] == "queued"
    finally:
        db.close()
