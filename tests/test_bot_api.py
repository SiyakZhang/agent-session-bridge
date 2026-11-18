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


def test_advanced_card_damaged_success_response_is_unknown(monkeypatch):
    class Response:
        status_code = 200

        def json(self):
            raise ValueError("damaged")

    api = DingAPI({"client_id": "test", "card_template_id": "owned-template.schema"})
    monkeypatch.setattr(api, "access_token", lambda: "test")
    calls = []
    monkeypatch.setattr(
        "session_bridge.bot.requests.post", lambda *a, **kw: calls.append(kw) or Response()
    )
    with pytest.raises(SendUncertain):
        api.send({"kind": "2", "chat": "chat"}, "question", {"card": True})
    assert len(calls) == 1


def test_standard_card_private_receiver_is_json_string(monkeypatch):
    class Response:
        status_code = 200

        def json(self):
            return {"processQueryKey": "verified-receipt"}

    api = DingAPI({"client_id": "test"})
    monkeypatch.setattr(api, "access_token", lambda: "test")
    calls = []
    monkeypatch.setattr(
        "session_bridge.bot.requests.post", lambda *a, **kw: calls.append(kw["json"]) or Response()
    )
    receipt = api.send({"kind": "1", "chat": "chat", "staff": "user"}, "question", {"card": True})
    assert isinstance(calls[0]["singleChatReceiver"], str)
    assert receipt["processQueryKey"] == "verified-receipt"
