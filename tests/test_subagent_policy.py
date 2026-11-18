"""Regression: removed bridge preferences must never decorate agent input."""

import time

from session_bridge.service import Service
from session_bridge.state import Store


class Node:
    online = True
    last_seen = time.time()

    def __init__(self):
        self.calls = []

    def dispatch(self, *args):
        self.calls.append(args)
        return {"status": "submitted", "detail": "submitted"}

    def action(self, *args):
        self.calls.append(args)
        return {"status": "accepted", "detail": "accepted"}


def test_old_subagent_preferences_and_commands_have_no_effect(tmp_path):
    db = Store(tmp_path / "s.db")
    svc = Service(db, {})
    node = Node()
    svc.nodes = {"win": node}
    db.pair("t", "u", "chat")
    alias = db.upsert_session("win", "codex", "sid", "/p", {"title": "Test session"})
    db.task(alias, True)
    db.put("subagent:" + alias, "on")
    db.put("subagent-policy:" + alias, "off")
    actor = {"tenant": "t", "sender": "u", "chat": "chat"}
    svc.handle("old-command", dict(actor, text="/send " + alias + " /subagent on"))
    assert node.calls[0][-1] == "/subagent on"  # explicit send is literal user text
    node.calls.clear()
    svc.task_command("command", "chat", db.session(alias), "subagent", "on", actor)
    assert not node.calls
    svc.send("normal", "chat", db.session(alias), "  literal task\n  indent\n")
    assert node.calls[0][-1] == "  literal task\n  indent\n"


def test_pending_queue_does_not_apply_legacy_policy(tmp_path):
    db = Store(tmp_path / "s.db")
    svc = Service(db, {})
    node = Node()
    svc.nodes = {"win": node}
    alias = db.upsert_session("win", "codex", "sid", "/p", {"managed": True, "status": "idle"})
    db.task(alias, True)
    db.put("subagent:" + alias, "on")
    db.queue_input("queued", alias, "chat", "literal text")
    svc.drain_queue()
    assert node.calls[0][-2:] == ("send", "literal text")
