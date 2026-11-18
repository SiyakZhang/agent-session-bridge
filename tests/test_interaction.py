import time

import pytest

from session_bridge.commands import parse_command, split_target
from session_bridge.state import Store


@pytest.mark.parametrize("text", ["松继续", "松 继续", "松\n继续", "松\u3000继续"])
def test_compact_prefix(text):
    assert split_target(text, ["松"]) == ("松", "继续")


def test_text_whitespace_preserved():
    assert split_target("a\n检查\n  x = 1\n", ["a"]) == ("a", "检查\n  x = 1\n")
    assert split_target("apple", ["a"]) == (None, "apple")
    assert split_target("a继续", ["a"]) == ("a", "继续")


@pytest.mark.parametrize("text", ["！清理", "／clear", "/clear", "!清理"])
def test_chinese_commands(text):
    assert parse_command(text) == ("clear", "")
    assert parse_command("继续！不要停止") is None


def test_unknown_extended_alias_does_not_fall_back():
    assert split_target("松·a·继续", ["松", "松·a·"]) == ("松·a·", "继续")
    with pytest.raises(ValueError):
        split_target("松·b·继续", ["松"])
    assert parse_command("！") == ("help", "")


def test_title_codes_persistent_disjoint_and_legacy_pool_compatible(tmp_path):
    path = tmp_path / "s.db"
    db = Store(path)
    a = db.upsert_session("windows", "codex", "same", "/x")
    b = db.upsert_session("linux", "codex", "same", "/x")
    c = db.upsert_session("windows", "claude", "same", "/x")
    tasks = [db.task(x, create=True) for x in (a, b, c)]
    assert len({t["code"] for t in tasks}) == 3
    legacy = db.register_pool("windows")
    assert legacy["codex"].startswith("松")
    with db.transaction():
        db.db.execute("UPDATE tasks SET code='松' WHERE alias=?", (a,))
    db.close()
    db = Store(path)
    assert db.task(a)["code"] == "松"
    assert db.register_pool("linux")["codex"].startswith("梅")
    with pytest.raises(ValueError):
        db.register_pool("other", {"codex": "松", "claude": "狐"})


def test_quote_same_chat_known_id_or_exact_bot_body(tmp_path):
    db = Store(tmp_path / "s.db")
    alias = db.upsert_session("windows", "codex", "sid", "/x")
    oid = db.enqueue("chat", "松｜完成", alias)
    db.sent(oid, "sent")
    db.link_sent(oid, {"msgId": "m1", "processQueryKey": "not-message-id"})
    assert db.resolve_reply("chat", {"id": "m1"})["alias"] == alias
    assert (
        db.resolve_reply("chat", {"id": "platform-other-id", "process_key": "not-message-id"})[
            "alias"
        ]
        == alias
    )
    assert db.resolve_reply("chat", {"from_bot": True, "text": "松｜完成"})["alias"] == alias
    for chat, reply in [
        ("other", {"id": "m1"}),
        ("chat", {"id": "not-message-id"}),
        ("chat", {"text": "松｜完成"}),
        ("chat", {"from_bot": True, "text": "松｜伪造"}),
    ]:
        with pytest.raises(ValueError):
            db.resolve_reply(chat, reply)
    with pytest.raises(ValueError):
        db.resolve_reply("chat", {"id": "unknown", "from_bot": True, "text": "松｜完成"})
    db.db.execute("UPDATE tasks SET generation=generation+1")
    db.db.commit()
    with pytest.raises(ValueError, match="旧对话"):
        db.resolve_reply("chat", {"id": "m1"})


def test_throttle_keeps_order(tmp_path):
    db = Store(tmp_path / "s.db")
    first = db.enqueue("chat", "one")
    db.enqueue("chat", "two")
    assert db.next_outgoing()["id"] == first
    db.retry_outgoing(first, 20, "throttled")
    assert db.next_outgoing() is None
    db.db.execute("UPDATE outbox SET not_before=?", (time.time() - 1,))
    db.db.commit()
    assert db.next_outgoing()["id"] == first
