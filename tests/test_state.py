import pytest

from session_bridge.state import Store


def test_same_session_on_different_nodes_is_never_collapsed(tmp_path):
    db = Store(tmp_path / "state.db")
    a = db.upsert_session("work", "codex", "same-uuid", "/project")
    b = db.upsert_session("lab", "codex", "same-uuid", "/project")
    assert a != b
    assert db.session(a)["node"] == "work"
    assert db.session(b)["node"] == "lab"
    assert db.session("unknown") is None


def test_event_and_command_dedup_survives_restart(tmp_path):
    db = Store(tmp_path / "state.db")
    alias = db.upsert_session("work", "codex", "id", "/project")
    assert db.add_event("e1", alias, "completed", "request", "result")
    assert not db.add_event("e1", alias, "completed", "request", "result")
    assert db.claim_command("m1", alias, "test")
    db.close()
    db = Store(tmp_path / "state.db")
    assert not db.claim_command("m1", alias, "test")
    assert len(db.history(alias)) == 1


def test_authorization_bound_to_chat_sender_and_tenant(tmp_path):
    db = Store(tmp_path / "state.db")
    db.pair("tenant", "sender", "chat")
    assert db.authorized("tenant", "sender", "chat")
    assert not db.authorized("other", "sender", "chat")
    assert not db.authorized("tenant", "other", "chat")
    assert not db.authorized("tenant", "sender", "other")


def test_unknown_alias_cannot_be_claimed(tmp_path):
    db = Store(tmp_path / "state.db")
    with pytest.raises(ValueError):
        db.claim_command("m1", "unknown", "run")
