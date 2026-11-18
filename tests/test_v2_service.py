import json
import threading
import time

import pytest

from session_bridge.dingtalk_messages import callback_data
from session_bridge.formatting import chunks
from session_bridge.service import Service
from session_bridge.state import Store


class Node:
    online = True
    last_seen = time.time()
    error = ""

    def __init__(self):
        self.config = {"projects": [{"name": "项目", "path": "/p"}]}
        self.sent = []
        self.actions = []
        self.result = {"status": "ok", "detail": "done"}

    def dispatch(self, *args):
        self.sent.append(args)
        return {"status": "submitted", "detail": "submitted"}

    def action(self, *args):
        self.actions.append(args)
        return self.result


@pytest.fixture
def system(tmp_path):
    db = Store(tmp_path / "s.db")
    svc = Service(db, {"nodes": [{"id": "win"}, {"id": "linux"}]})
    svc.nodes = {"win": Node(), "linux": Node()}
    db.pair("t", "u", "chat")
    a = db.upsert_session("win", "codex", "same", "/p", {"live": True})
    b = db.upsert_session("linux", "claude", "same", "/p", {"live": True})
    db.task(a, True)
    db.task(b, True)
    with db.transaction():
        db.db.execute("UPDATE tasks SET code='松' WHERE alias=?", (a,))
        db.db.execute("UPDATE tasks SET code='虎' WHERE alias=?", (b,))
    yield svc, db, a, b
    db.close()


def incoming(text, **extra):
    return dict(tenant="t", sender="u", chat="chat", text=text, **extra)


def test_prefixes_go_to_exact_node_and_keep_text(system):
    svc, _db, _a, _b = system
    svc.handle("1", incoming("松继续\n  保留缩进\n"))
    svc.handle("2", incoming("虎 继续"))
    assert svc.nodes["win"].sent[0][1:] == ("codex", "same", "继续\n  保留缩进\n")
    assert svc.nodes["linux"].sent[0][1:] == ("claude", "same", "继续")
    svc.handle("1", incoming("松继续\n  保留缩进\n"))
    assert len(svc.nodes["win"].sent) == 1


def test_known_quote_ignores_other_alias_in_body(system):
    svc, db, a, _b = system
    oid = db.enqueue("chat", "done", a)
    db.sent(oid, "sent")
    db.link_sent(oid, {"processQueryKey": "receipt"})
    svc.handle(
        "1", incoming("虎这个名字更好", reply={"process_key": "receipt", "id": "platform-id"})
    )
    assert svc.nodes["win"].sent[0][-1] == "虎这个名字更好"
    assert not svc.nodes["linux"].sent


def test_invalid_quote_never_falls_back_to_prefix(system):
    svc, _, _, _ = system
    svc.handle("1", incoming("松继续", reply={"id": "unknown"}))
    assert not svc.nodes["win"].sent


def test_commands_never_become_model_prompts(system):
    svc, _db, _a, _ = system
    for i, text in enumerate(("松！清理", "松/model", "松！未知")):
        svc.handle(str(i), incoming(text))
    assert not svc.nodes["win"].sent
    svc.handle("s", incoming("松！发送 /model 是什么意思\n"))
    assert svc.nodes["win"].sent[0][-1] == "/model 是什么意思\n"


def test_clear_invalidates_old_replies_and_queue(system):
    svc, db, a, _ = system
    db.upsert_session("win", "codex", "same", "/p", {"capabilities": ["clear"]})
    oid = db.enqueue("chat", "old", a)
    db.sent(oid, "sent")
    db.link_sent(oid, {"msgId": "old"})
    db.queue_input("queued", a, "chat", "pending")
    svc.handle("clear", incoming("松！清理"))
    assert db.task(a)["generation"] == 2
    assert not db.queued(a)
    svc.handle("old-reply", incoming("继续", reply={"id": "old"}))
    assert not svc.nodes["win"].sent


def test_new_menu_quote_bound_to_owner_and_consumed_once(system):
    svc, db, _, _ = system
    svc.handle("new", incoming("wincodex！新建"))
    row = dict(db.db.execute("SELECT * FROM outbox ORDER BY id DESC LIMIT 1").fetchone())
    assert row["question_id"]
    db.sent(row["id"], "sent")
    db.link_sent(row["id"], {"msgId": "menu"})
    svc.handle("select", incoming("1", reply={"id": "menu"}))
    assert svc.nodes["win"].actions[0][1:] == ("codex", "__new__", "new", "/p")
    svc.handle("select-again", incoming("1", reply={"id": "menu"}))
    assert len(svc.nodes["win"].actions) == 1


def test_model_choice_exact_request(system):
    svc, db, _a, _ = system
    db.upsert_session("win", "codex", "same", "/p", {"capabilities": ["model"]})
    svc.nodes["win"].result = {
        "status": "ok",
        "detail": "models",
        "choices": [{"label": "模型", "value": "model-a"}],
    }
    svc.handle("model", incoming("松！模型"))
    svc.nodes["win"].result = {"status": "ok", "detail": "changed"}
    svc.handle("choose", incoming("松1"))
    assert svc.nodes["win"].actions[-1][-2:] == ("model", "model-a")
    svc.handle("again", incoming("松1"))
    assert len(svc.nodes["win"].actions) == 2


def test_full_output_utf8_json_budget(system):
    svc, db, a, _ = system
    content = ('中文🙂\\"\n' * 5000) + "保留最后一行"
    parts = chunks(content, code="松")
    assert "".join(p.split("｜", 1)[1] for p in parts) == content
    assert all(
        len(json.dumps({"title": "会话助手", "text": p}, ensure_ascii=False).encode()) < 15000
        for p in parts
    )
    svc.ingest(
        "win",
        {
            "sessions": [{"engine": "codex", "sid": "same", "live": True}],
            "events": [
                {
                    "engine": "codex",
                    "sid": "same",
                    "event_id": "long",
                    "kind": "completed",
                    "result": content,
                }
            ],
        },
    )
    assert db.history(a)[-1]["result"].endswith("保留最后一行")
    assert (
        db.db.execute("SELECT body FROM outbox ORDER BY id DESC LIMIT 1")
        .fetchone()[0]
        .split("\n\n---\n")[0]
        .endswith("保留最后一行")
    )


def test_callback_preserves_quote_receipt_and_newlines():
    data = callback_data(
        {
            "msgtype": "text",
            "chatbotUserId": "bot",
            "originalProcessQueryKey": "pq",
            "text": {
                "content": "继续\n  x\n",
                "isReplyMsg": True,
                "repliedMsg": {"msgId": "m", "senderId": "bot", "content": {"text": "old"}},
            },
        }
    )
    assert data["reply"] == {"id": "m", "process_key": "pq", "text": "old", "from_bot": True}
    assert data["text"].endswith("\n")


def test_queue_stop_and_atomic_cancel(system):
    svc, db, a, _ = system
    db.upsert_session(
        "win",
        "codex",
        "same",
        "/p",
        {"managed": True, "status": "busy", "capabilities": ["stop", "queue"]},
    )
    svc.handle("send", incoming("松待办"))
    svc.handle("stop", incoming("松！停止"))
    assert db.get("paused:" + a)
    pending = db.queued(a)[0]
    assert db.claim_queued(pending["id"])
    assert not db.cancel_queued(pending["id"])
    db.recover()
    assert db.db.execute("SELECT status FROM task_queue").fetchone()[0] == "unknown"


def test_unpaired_menu_answer_rejected(system):
    svc, _db, _, _ = system
    svc.handle("menu", incoming("wincodex/new"))
    svc.handle("evil", {"tenant": "t", "sender": "other", "chat": "chat", "text": "1"})
    assert not svc.nodes["win"].actions


def test_main_interruption_is_recorded_and_notified(system):
    svc, db, alias, _ = system
    svc.ingest(
        "win",
        {
            "events": [
                {
                    "engine": "codex",
                    "sid": "same",
                    "event_id": "interrupt",
                    "kind": "aborted",
                    "result": "Internal interruption details",
                }
            ]
        },
    )
    assert db.history(alias)[-1]["kind"] == "aborted"
    assert db.db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1


def test_stop_and_dispatch_are_serialized(system, monkeypatch):
    svc, db, alias, _ = system
    db.upsert_session("win", "codex", "same", "/p", {"managed": True, "status": "idle"})
    db.queue_input("pending", alias, "chat", "must not start after stop")
    pause_checked = threading.Event()
    stop_attempted = threading.Event()
    stop_done = threading.Event()
    get = db.get

    def intercepted_get(key, *args):
        result = get(key, *args)
        if key == "paused:" + alias and threading.current_thread().name == "test-drain":
            pause_checked.set()
            assert stop_attempted.wait(2)
            # Before the fix stop could finish in this window, then stale dispatch ran.
            stop_done.wait(0.2)
        return result

    def stop():
        assert pause_checked.wait(2)
        stop_attempted.set()
        svc.action("stop-race", "chat", db.session(alias), "stop", "")
        stop_done.set()

    monkeypatch.setattr(db, "get", intercepted_get)
    svc.nodes["win"].result = {"status": "accepted", "detail": "accepted"}
    draining = threading.Thread(target=svc.drain_queue, name="test-drain")
    stopping = threading.Thread(target=stop)
    draining.start()
    stopping.start()
    draining.join(5)
    stopping.join(5)
    assert not draining.is_alive() and not stopping.is_alive()
    assert [call[-2] for call in svc.nodes["win"].actions] == ["send", "stop"]
    assert db.get("paused:" + alias)
    db.queue_input("later", alias, "chat", "remain paused")
    svc.drain_queue()
    assert len(svc.nodes["win"].actions) == 2
    assert db.queued(alias)[0]["id"] == "later"
