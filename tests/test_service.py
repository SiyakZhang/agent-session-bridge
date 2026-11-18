from session_bridge.formatting import chunks, redact
from session_bridge.service import Service
from session_bridge.state import Store
from session_bridge.transport import ManagedNode


class FakeNode:
    def __init__(self):
        self.sent = []

    def dispatch(self, *args):
        self.sent.append(args)
        return {"status": "queued", "detail": "queued, not completed"}


def test_two_machine_routing_and_replayed_message(tmp_path):
    db = Store(tmp_path / "s.db")
    service = Service(db, {})
    db.pair("t", "u", "chat")
    a = db.upsert_session("win", "codex", "same", "/project")
    b = db.upsert_session("linux", "codex", "same", "/project")
    service.nodes = {"win": FakeNode(), "linux": FakeNode()}
    data = {"tenant": "t", "sender": "u", "chat": "chat", "text": f"/send {b} check tests"}
    service.handle("m1", data)
    service.handle("m1", data)
    assert service.nodes["win"].sent == []
    assert len(service.nodes["linux"].sent) == 1
    assert service.nodes["linux"].sent[0][2] == "same"
    assert len(db.history(b)) == 1 and db.history(a) == []


def test_unpaired_cannot_dispatch_or_steal_pairing(tmp_path):
    db = Store(tmp_path / "s.db")
    service = Service(db, {})
    alias = db.upsert_session("win", "codex", "same", "/p")
    service.nodes = {"win": FakeNode()}
    data = {
        "tenant": "t",
        "sender": "attacker",
        "chat": "chat",
        "text": f"/send {alias} do something",
    }
    service.handle("m1", data)
    assert not service.nodes["win"].sent
    db.pair("t", "owner", "ownerchat")
    code = service.new_pair_code()
    data["text"] = "/pair " + code
    service.handle("m2", data)
    assert not db.authorized("t", "attacker", "chat")


def test_pairing_code_single_use(tmp_path):
    db = Store(tmp_path / "s.db")
    service = Service(db, {})
    code = service.new_pair_code()
    data = {"tenant": "t", "sender": "u", "chat": "c", "text": "/pair " + code}
    service.handle("m1", data)
    assert db.authorized("t", "u", "c")
    data["chat"] = "other"
    service.handle("m2", data)
    assert not db.authorized("t", "u", "other")


def test_wrong_node_rejected():
    node = ManagedNode({"id": "win", "instance": "instance-1"}, lambda *args: [])
    assert node.identity_matches({"node": "win", "instance": "instance-1"})
    assert not node.identity_matches({"node": "win", "instance": "instance-2"})
    assert not node.identity_matches({"node": "linux", "instance": "instance-1"})


def test_secrets_redacted_and_unicode_chunk_size():
    assert "SECRET" not in redact("appSecret=SECRET")
    synthetic_token = "sk-" + "a" * 24
    assert synthetic_token not in redact(synthetic_token)
    assert all(len(p.encode()) < 16000 for p in chunks("中" * 16000))
