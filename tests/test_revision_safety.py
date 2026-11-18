import json
import threading
import time

import pytest

from session_bridge.codex_control import Rpc
from session_bridge.questions import elicitation_content, elicitation_questions
from session_bridge.scanner import parse_record
from session_bridge.service import Service
from session_bridge.state import Store
from session_bridge.telemetry import session_quota


class Node:
    online = True
    error = ""

    def __init__(self):
        self.last_seen = time.time()
        self.calls = []

    def action(self, *args):
        self.calls.append(args)
        return {"status": "accepted", "detail": "accepted", "turn_id": "new-turn"}

    def dispatch(self, *args):
        self.calls.append(args)
        return {"status": "submitted", "detail": "submitted"}


@pytest.fixture
def system(tmp_path):
    db = Store(tmp_path / "db.sqlite")
    svc = Service(db, {"nodes": [{"id": "win"}], "client_secret": "configured-placeholder-secret"})
    svc.nodes = {"win": Node()}
    db.pair("t", "u", "chat")
    alias = db.upsert_session(
        "win", "codex", "sid", str(tmp_path), {"title": "修复桥接", "live": True}
    )
    db.task(alias, True)
    yield svc, db, alias
    db.close()


def actor(text="", **kwargs):
    return {"tenant": "t", "sender": "u", "chat": "chat", "text": text, **kwargs}


def rpc():
    value = Rpc.__new__(Rpc)
    value.lock = threading.RLock()
    value.questions, value.waiters, value.turns, value.compactions, value.telemetry = (
        {},
        {},
        {},
        {},
        {},
    )
    value.last_turns = {}
    value.answering = set()
    value.events = []
    value.epoch = "epoch"
    return value


def test_compaction_native_lifecycle_and_context_invalidated():
    value = rpc()
    value.compactions["sid"] = time.time()
    value.process_frame(
        {
            "method": "item/started",
            "params": {
                "threadId": "sid",
                "turnId": "t",
                "item": {"id": "c", "type": "contextCompaction"},
            },
        }
    )
    value.process_frame(
        {
            "method": "item/completed",
            "params": {
                "threadId": "sid",
                "turnId": "t",
                "item": {"id": "c", "type": "contextCompaction"},
            },
        }
    )
    value.process_frame(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "sid",
                "turn": {"id": "t", "status": "completed", "error": None},
            },
        }
    )
    assert [e["kind"] for e in value.events] == ["compacting", "compacted"]
    assert not value.compactions
    assert value.telemetry["sid"] == {"invalidated": True}


@pytest.mark.parametrize(
    "status,kind", [("interrupted", "aborted"), ("failed", "failed"), ("completed", "failed")]
)
def test_compaction_missing_item_or_interrupted_releases_state(status, kind):
    value = rpc()
    value.compactions["sid"] = time.time()
    value.process_frame(
        {
            "method": "turn/completed",
            "params": {"threadId": "sid", "turn": {"id": "t", "status": status, "error": None}},
        }
    )
    assert not value.compactions
    assert value.events[-1]["kind"] == kind
    assert value.events[-1]["turn"] == "t"
    assert "已完成" not in value.events[-1]["result"]


def test_async_question_survives_turn_and_desktop_resolution():
    value = rpc()
    frame = {
        "id": 7,
        "method": "item/tool/requestUserInput",
        "params": {"threadId": "sid", "turnId": "t", "isBlocking": False},
    }
    value.process_frame(frame)
    value.process_frame(
        {
            "method": "turn/completed",
            "params": {"threadId": "sid", "turn": {"id": "t", "status": "completed"}},
        }
    )
    assert value.questions["7"] == frame
    value.process_frame({"method": "serverRequest/resolved", "params": {"requestId": 7}})
    assert not value.questions


def test_native_stop_receipt_matches_before_gateway_has_turn_snapshot(system):
    svc, db, alias = system
    value = rpc()
    value.control_requests = {("sid", "stop", "actual-turn"): "stop-rid"}
    db.reaction("stop-rid", "received", actor(platform_msg_id="stop-input"))

    def action(*args):
        value.process_frame(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "sid",
                    "turn": {"id": "actual-turn", "status": "interrupted"},
                },
            }
        )
        assert value.events[-1]["control_requests"]["stop"] == "stop-rid"
        value.events[-1]["ts"] -= 30  # The node's clock is slower than the gateway clock.
        svc.ingest("win", {"events": value.events})
        return {"status": "accepted", "detail": "stop accepted"}

    svc.nodes["win"].action = action
    svc.action("stop-rid", "chat", db.session(alias), "stop", "")
    assert db.pending_reaction()["state"] == "stopped"
    assert db.db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


def test_rpc_abort_waits_for_transcript_reason_and_notifies_only_unresolved(system):
    svc, db, _alias = system
    value = rpc()
    for turn in ("user", "unknown"):
        value.process_frame(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "sid",
                    "turn": {"id": turn, "status": "interrupted"},
                },
            }
        )
    svc.ingest("win", {"events": value.events})
    svc.ingest(
        "win",
        {
            "events": [
                {
                    "engine": "codex",
                    "sid": "sid",
                    "event_id": "transcript",
                    "kind": "aborted",
                    "turn": "user",
                    "user_initiated": True,
                }
            ]
        },
    )
    assert db.db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    with db.transaction():
        db.db.execute(
            "UPDATE settings SET value=json_set(value,'$.due',0) WHERE key LIKE 'abort-notice:%' AND json_extract(value,'$.due') IS NOT NULL"
        )
    svc.flush_interruption_notices()
    assert db.db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1


def test_native_failure_retains_401_and_retry_state():
    value = rpc()
    value.process_frame(
        {
            "method": "error",
            "params": {
                "threadId": "sid",
                "turnId": "t",
                "willRetry": True,
                "error": {"message": "401 无效的令牌", "codexErrorInfo": "unauthorized"},
            },
        }
    )
    assert value.events[0]["kind"] == "reconnecting"
    assert "401 无效的令牌" in value.events[0]["result"]


def offer(svc, db, alias, qid="q", questions=None):
    active = db.get("native-questions:" + alias, [])
    db.put("native-questions:" + alias, active + [qid])
    svc.offer(
        actor(),
        qid,
        {
            "kind": "native",
            "command": "answer",
            "native_id": qid,
            "request_key": "epoch:" + qid,
            "questions": questions
            or [{"id": "one", "question": "选一个", "options": [{"label": "A"}, {"label": "B"}]}],
            "options": [],
        },
        "问题",
        alias,
    )


def test_many_pending_questions_never_pick_latest(system):
    svc, db, alias = system
    offer(svc, db, alias, "q1")
    offer(svc, db, alias, "q2")
    with pytest.raises(ValueError, match="多个"):
        svc.task_input("input", "chat", db.session(alias), "1", actor())
    assert not svc.nodes["win"].calls


def test_multi_question_old_quote_and_replayed_answer_cannot_answer_next(system):
    svc, db, alias = system
    questions = [
        {"id": "one", "question": "第一题", "options": [{"label": "A"}]},
        {"id": "two", "question": "第二题", "options": [{"label": "B"}]},
    ]
    offer(svc, db, alias, questions=questions)
    svc.select("answer-1", actor(), "q", "1")
    svc.select("answer-1", actor(), "q", "1")
    assert not svc.nodes["win"].calls
    with pytest.raises(ValueError, match="过期"):
        svc.select("new-but-old-quote", actor(), "q", "1")
    svc.select("answer-2", actor(), "q:next", "1")
    assert json.loads(svc.nodes["win"].calls[0][-1])["answers"] == {
        "one": {"answers": ["A"]},
        "two": {"answers": ["B"]},
    }


def test_resolved_question_does_not_intercept_new_conversation(system):
    svc, db, alias = system
    offer(svc, db, alias)
    svc.ingest(
        "win", {"sessions": [{"engine": "codex", "sid": "sid", "questions": [], "live": True}]}
    )
    svc.task_input("new", "chat", db.session(alias), "新的要求", actor())
    assert svc.nodes["win"].calls[-1][-1] == "新的要求"


def test_duplicate_title_rejects_prefix_and_send_but_quote_stays_exact(system):
    svc, db, alias = system
    other = db.upsert_session("win", "codex", "other-sid", "/tmp", {"title": "修复桥接"})
    db.task(other, True)
    svc.handle("ambiguous", actor("修复桥接 继续"))
    assert not svc.nodes["win"].calls
    with pytest.raises(ValueError, match="同名"):
        svc.resolve("修复桥接")
    oid = db.enqueue("chat", "known", alias)
    db.sent(oid, "sent")
    db.link_sent(oid, {"msgId": "known"})
    svc.handle("exact", actor("继续", reply={"id": "known"}))
    assert svc.nodes["win"].calls[-1][2] == "sid"


def test_title_footer_and_body_all_redacted(system):
    svc, db, _alias = system
    alias = db.upsert_session(
        "win",
        "codex",
        "sensitive",
        "/tmp",
        {"title": "password=title-placeholder-value", "model": "configured-placeholder-secret"},
    )
    svc.enqueue("chat", "Bearer eyJhbGciOiJIUzI1NiJ9.placeholder.signature", alias)
    body = db.db.execute("SELECT body FROM outbox").fetchone()[0]
    assert "title-placeholder-value" not in body
    assert "configured-placeholder-secret" not in body
    assert "eyJhbGci" not in body


def test_replayed_failure_does_not_pause_again_and_new_failure_not_suppressed(system):
    svc, db, alias = system
    frame = {
        "events": [
            {
                "engine": "codex",
                "sid": "sid",
                "event_id": "first",
                "turn": "t1",
                "kind": "failed",
                "result": "401",
            }
        ]
    }
    svc.ingest("win", frame)
    db.put("paused:" + alias, False)
    svc.ingest("win", frame)
    assert db.get("paused:" + alias) is False
    frame["events"][0].update(event_id="second", turn="t2")
    svc.ingest("win", frame)
    assert db.db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 2


def test_control_offline_and_recovery_have_distinct_notifications(system):
    svc, db, alias = system

    def frame(status):
        return {
            "sessions": [
                {
                    "engine": "codex",
                    "sid": "sid",
                    "status": status,
                    "live": status != "control-offline",
                    "control_error": "Connection reset",
                }
            ]
        }

    svc.ingest("win", frame("control-offline"))
    svc.ingest("win", frame("control-offline"))
    assert "控制" in svc.status(db.session(alias))
    assert db.db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1
    svc.ingest("win", frame("idle"))
    assert db.db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 2
    assert db.get("paused:" + alias)  # recovery never replays the saved queue


def test_receipt_only_uses_original_reaction_and_old_turn_cannot_complete_it(system):
    svc, db, alias = system
    svc.handle("msg", actor("修复桥接 继续", open_msg_id="original"))
    assert db.db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    assert db.db.execute("SELECT open_id FROM reactions").fetchone()[0] == "original"
    active = db.get("active-receipt:" + alias)
    active["turn"] = "new-turn"
    db.put("active-receipt:" + alias, active)
    svc.react_active(alias, "completed", {"turn": "old-turn"})
    assert db.db.execute("SELECT state FROM reactions").fetchone()[0] == "received"


def test_provider_label_does_not_bind_default_account_quota():
    for provider in ("openai", "custom", "ark", "volcengine"):
        quota = session_quota(
            {
                "model_provider": provider,
                "native_rate_limits": {"primary": {"used_percent": 1}},
                "rate_limits_at": time.time(),
            }
        )
        assert quota["status"] == "unsupported"


def test_claude_desktop_tool_result_clears_matching_question():
    meta = {}
    parse_record(
        "claude",
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call",
                        "name": "AskUserQuestion",
                        "input": {"questions": [{"question": "框架?"}]},
                    }
                ]
            },
        },
        meta,
    )
    assert meta["questions"]
    parse_record(
        "claude",
        {
            "type": "user",
            "toolUseResult": {"answers": {}},
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "call", "content": "A"}]
            },
        },
        meta,
    )
    assert not meta["questions"]


def test_mcp_form_answers_preserve_types_and_validate_constraints():
    params = {
        "mode": "form",
        "requestedSchema": {
            "type": "object",
            "properties": {
                "lang": {
                    "type": "string",
                    "enum": ["py", "ts"],
                    "enumNames": ["Python", "TypeScript"],
                },
                "count": {"type": "integer", "minimum": 1, "maximum": 10},
                "ok": {"type": "boolean"},
                "targets": {"type": "array", "items": {"enum": ["Windows", "Linux"]}},
            },
        },
    }
    assert len(elicitation_questions(params)) == 4
    answers = {
        "lang": {"answers": ["Python"]},
        "count": {"answers": ["3"]},
        "ok": {"answers": ["否"]},
        "targets": {"answers": ["Windows", "Linux"]},
    }
    assert elicitation_content(params, answers) == {
        "lang": "py",
        "count": 3,
        "ok": False,
        "targets": ["Windows", "Linux"],
    }
    answers["count"] = {"answers": ["300"]}
    with pytest.raises(ValueError, match="范围"):
        elicitation_content(params, answers)


def test_explicit_other_task_and_launcher_are_not_captured_as_free_answers(system):
    svc, db, alias = system
    offer(svc, db, alias, questions=[{"id": "free", "question": "补充?", "options": []}])
    other = db.upsert_session("win", "claude", "other", "/tmp", {"title": "另一个任务"})
    db.task(other, True)
    svc.handle("explicit", actor("另一个任务 继续"))
    assert svc.nodes["win"].calls[-1][1:3] == ("claude", "other")
    before = len(svc.nodes["win"].calls)
    svc.handle("launcher", actor("wincodex！新建"))
    assert len(svc.nodes["win"].calls) == before
    assert db.db.execute("SELECT question_id FROM outbox ORDER BY id DESC LIMIT 1").fetchone()[0]


def test_native_title_collision_with_historical_code_is_rejected(system):
    svc, db, alias = system
    with db.transaction():
        db.db.execute("UPDATE tasks SET code='Legacy' WHERE alias=?", (alias,))
    other = db.upsert_session("win", "claude", "other", "/tmp", {"title": "Legacy"})
    db.task(other, True)
    svc.handle("ambiguous", actor("Legacy continue"))
    assert not svc.nodes["win"].calls
    with pytest.raises(ValueError):
        svc.resolve("Legacy")
    assert svc.resolve(alias)["alias"] == alias


def test_observation_only_question_does_not_capture_new_requirements(system):
    svc, db, alias = system
    offer(svc, db, alias)
    value = db.get("choice:q")
    value["remote"] = False
    db.put("choice:q", value)
    svc.handle("new", actor("修复桥接 新的实际要求"))
    assert svc.nodes["win"].calls[-1][-1] == "新的实际要求"


def test_directory_pagination_and_custom_input_invalidate_old_cards(system):
    svc, db, _alias = system
    db.upsert_session(
        "win",
        "codex",
        "sid",
        "/tmp",
        {"recent_directories": ["/work/" + str(i) for i in range(12)]},
    )
    svc.handle("new", actor("wincodex！新建"))
    first = db.db.execute("SELECT question_id FROM outbox ORDER BY id DESC LIMIT 1").fetchone()[0]
    svc.select("page", actor(), first, "9")
    with pytest.raises(ValueError):
        svc.select("old", actor(), first, "1")
    second = db.db.execute("SELECT question_id FROM outbox ORDER BY id DESC LIMIT 1").fetchone()[0]
    choice = db.get("choice:" + second)
    custom = next(i + 1 for i, o in enumerate(choice["options"]) if o["value"] == "__custom__")
    svc.select("custom", actor(), second, str(custom))
    stage = db.db.execute("SELECT question_id FROM outbox ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert stage != second
    svc.select("path", actor(), stage, "/work/directory with spaces")
    assert svc.nodes["win"].calls[-1][-1] == "/work/directory with spaces"


def test_failed_saved_input_retries_only_explicitly_with_new_attempt(system):
    svc, db, alias = system
    node = svc.nodes["win"]
    node.dispatch = lambda *args: (
        node.calls.append(args) or {"status": "failed", "detail": "network rejected before write"}
    )
    svc.send("input", "chat", db.session(alias), "task")
    assert db.queued(alias)[0]["status"] == "saved"
    svc.drain_queue()
    assert len(node.calls) == 1
    db.put("paused:" + alias, False)
    svc.drain_queue()
    assert node.calls[0][0] != node.calls[1][0]


def test_stream_outage_reports_once_and_recovers_without_replaying(system):
    svc, db, _alias = system
    db.put("stream-outage", {"started": time.time() - 31, "notified": False})
    svc.check_stream(False)
    svc.check_stream(False)
    assert db.db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1
    svc.check_stream(True)
    assert db.db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 2
    assert not svc.nodes["win"].calls
