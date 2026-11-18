import json
import time

import pytest

from session_bridge.presentation import message_parts
from session_bridge.service import Service
from session_bridge.state import Store
from session_bridge.telemetry import metric_reaction_text, quota_text, session_quota, status_footer


class Node:
    online = True
    last_seen = time.time()

    def __init__(self):
        self.calls = []

    def dispatch(self, *args):
        self.calls.append(args)
        return {"status": "submitted", "detail": "submitted"}


@pytest.fixture
def system(tmp_path):
    db = Store(tmp_path / "db.sqlite")
    svc = Service(
        db,
        {
            "nodes": [{"id": "win"}],
            "metric_emotion_ids": ["verified-model", "verified-context", "verified-quota"],
        },
    )
    svc.nodes["win"] = Node()
    db.pair("t", "u", "chat")
    alias = db.upsert_session(
        "win",
        "codex",
        "sid",
        "/tmp",
        {
            "title": "桥接程序",
            "capabilities": ["queue", "continue"],
        },
    )
    db.task(alias, True)
    yield svc, db, alias
    db.close()


def actor(text="", **extra):
    return dict(tenant="t", sender="u", chat="chat", text=text, **extra)


def observe(svc, asynchronous=True):
    svc.ingest(
        "win",
        {
            "sessions": [
                {
                    "engine": "codex",
                    "sid": "sid",
                    "live": True,
                    "questions": [
                        {
                            "key": "transcript:call",
                            "title": "补充问题",
                            "remote": False,
                            "async": asynchronous,
                            "questions": [
                                {
                                    "id": "q",
                                    "question": "文字表情贴在哪里？",
                                    "options": [
                                        {"label": "机器人回答下面"},
                                        {"label": "我的消息下面"},
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ]
        },
    )
    row = dict(svc.store.db.execute("SELECT * FROM outbox ORDER BY id DESC LIMIT 1").fetchone())
    svc.store.sent(row["id"], "sent")
    svc.store.link_sent(row["id"], {"processQueryKey": "process"})
    return row


def test_normal_result_restores_markdown_and_keeps_table(system):
    svc, db, alias = system
    table = "| 项目 | 状态 |\n| --- | --- |\n| 网络 | 正常 |"
    svc.enqueue("chat", table, alias, kind="completed")
    row = db.db.execute("SELECT body,payload FROM outbox").fetchone()
    assert row[0] == "> 桥接程序\n\n" + table
    assert row[1] is None


def test_failure_still_contains_exact_error_under_short_title():
    raw = "HTTP 401\n\n原始错误：`无效令牌`"
    heading, body = message_parts("很长的会话标题" * 10, raw)[0].split("\n\n", 1)
    assert heading == "> " + ("很长的会话标题" * 10)[:37] + "…"
    assert body == raw


def test_small_question_shows_full_question_and_separate_options(system):
    svc, _db, _alias = system
    row = observe(svc)
    assert "文字表情贴在哪里？\n\n1. 机器人回答下面\n\n2. 我的消息下面" in row["body"]
    assert "回原电脑" not in row["body"]


def test_real_numbered_payload_and_answer_wait_for_delivery(system):
    svc, db, alias = system
    question = {
        "id": "output",
        "header": "输出格式",
        "question": "Codex 原问题：需要哪种格式？",
        "options": [
            {"label": "甲", "description": "说明甲"},
            {"label": "乙", "description": "说明乙"},
            {"label": "丙", "description": "说明丙"},
        ],
        "isOther": False,
    }
    db.put("native-questions:" + alias, ["original-request"])
    svc.offer(
        actor(),
        "numbered",
        {
            "native_id": "original-request",
            "request_key": "original-key",
            "command": "answer",
            "kind": "native",
            "questions": [question],
        },
        "等待你回答",
        alias,
    )
    oid, body, payload = db.db.execute("SELECT id,body,payload FROM outbox").fetchone()
    assert json.loads(payload)["plain_text"] is True
    assert (
        "Codex 原问题：需要哪种格式？\n\n1. 甲 — 说明甲\n\n2. 乙 — 说明乙\n\n3. 丙 — 说明丙" in body
    )
    responses = []
    svc.action = lambda *args: responses.append(args)
    svc.handle("before-delivery", actor("3"))
    assert responses == []
    db.sent(oid, "sent")
    svc.handle("after-delivery", actor("3"))
    assert json.loads(responses[0][4]) == {
        "key": "original-key",
        "answers": {"output": {"answers": ["丙"]}},
    }


def test_retired_known_async_question_keeps_free_followup_and_does_not_reopen(system):
    svc, db, alias = system
    row = observe(svc)
    qid = row["question_id"]
    assert not svc.dismiss_observed_question(dict(actor(), sender="wrong"), qid)
    assert svc.dismiss_observed_question(actor(), qid)
    assert not svc.pending_choices(actor(), alias)
    db.put("native-questions:" + alias, [])
    observe(svc)
    assert not svc.pending_choices(actor(), alias)
    svc.handle("old-numeric", actor("2", reply={"process_key": "process"}))
    assert svc.nodes["win"].calls == []
    svc.handle("continue", actor("继续对话", reply={"process_key": "process"}))
    assert svc.nodes["win"].calls[-1][-1] == "继续对话"


@pytest.mark.parametrize(
    "command,kind,state",
    [
        ("compact", "compacted", "compacted"),
        ("stop", "aborted", "stopped"),
    ],
)
def test_intentional_controls_log_history_and_update_reaction_without_text(
    system, command, kind, state
):
    svc, db, alias = system
    db.upsert_session("win", "codex", "sid", "", {"active_turn": "turn"})
    db.reaction("control-rid", "received", actor(platform_msg_id="control-message"))

    def action(*args):
        # The source event can arrive before the acknowledgement from node.action.
        svc.ingest(
            "win",
            {
                "events": [
                    {
                        "engine": "codex",
                        "sid": "sid",
                        "event_id": "terminal",
                        "kind": kind,
                        "turn": "turn",
                        "result": "operation finished",
                    }
                ]
            },
        )
        return {"status": "accepted", "detail": "operation accepted"}

    svc.nodes["win"].action = action
    svc.action("control-rid", "chat", db.session(alias), command, "")
    assert db.history(alias)[-1]["kind"] == kind
    assert db.db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    assert db.pending_reaction()["state"] == state


def test_desktop_interruption_uses_reason_and_keeps_real_errors_visible(system):
    from session_bridge.scanner import parse_record

    svc, db, alias = system
    meta = {"failure": {"message": "earlier retry"}}
    kind = parse_record(
        "codex",
        {
            "type": "event_msg",
            "payload": {
                "type": "turn_aborted",
                "turn_id": "native-turn",
                "reason": "interrupted",
            },
        },
        meta,
    )
    assert kind == "aborted" and meta["user_initiated"] is True
    svc.ingest(
        "win",
        {
            "events": [
                dict(meta, engine="codex", sid="sid", kind=kind, event_id="user-stopped"),
                {
                    "engine": "codex",
                    "sid": "sid",
                    "kind": "failed",
                    "event_id": "real-error",
                    "result": "HTTP 401",
                },
            ]
        },
    )
    bodies = db.db.execute("SELECT body FROM outbox").fetchall()
    assert len(bodies) == 1 and "HTTP 401" in bodies[0][0]
    assert {r["kind"] for r in db.history(alias)} == {"aborted", "failed"}


@pytest.mark.parametrize(
    "extra,expected",
    [
        ({}, "直接输入答案。"),
        ({"remote": False}, "请在原电脑回答。"),
        ({"remote": False, "async": True}, "直接输入答案。"),
    ],
)
def test_free_input_question_never_asks_for_nonexistent_numbers(extra, expected):
    from session_bridge.questions import question_text

    text = question_text(
        dict({"questions": [{"id": "q", "question": "补充要求", "options": []}]}, **extra)
    )
    assert "补充要求" in text and expected in text
    assert "数字" not in text


def test_observed_blocking_options_show_question_but_do_not_invite_phone_answer():
    from session_bridge.questions import question_text

    text = question_text(
        {
            "remote": False,
            "questions": [
                {
                    "id": "q",
                    "question": "是否执行？",
                    "options": [{"label": "允许"}, {"label": "拒绝"}],
                }
            ],
        }
    )
    assert "是否执行？\n\n1. 允许\n\n2. 拒绝" in text
    assert "请在原电脑回答。" in text and "回复选项数字" not in text


def test_secret_question_only_invites_original_computer_input():
    from session_bridge.questions import question_text

    text = question_text({"questions": [{"id": "q", "question": "输入密钥", "isSecret": True}]})
    assert "请回原电脑填写" in text
    assert "直接输入答案" not in text and "回复选项数字" not in text


def test_async_question_quote_free_text_continues_exact_original_session(system):
    svc, db, _alias = system
    observe(svc)
    text = "只有需要我选择才用交互卡片，而且请修好表格"
    svc.handle("new-instruction", actor(text, reply={"process_key": "process", "id": "bot-id"}))
    assert svc.nodes["win"].calls[-1][1:] == ("codex", "sid", text)
    assert db.get("choice:" + db.get("task-choice:" + _alias))


def test_async_numeric_reply_maps_label_and_question_without_native_approval(system):
    svc, db, _alias = system
    row = observe(svc)
    svc.handle("answer", actor("1", reply={"process_key": "process"}))
    sent = svc.nodes["win"].calls[-1][-1]
    assert "文字表情贴在哪里？" in sent and "机器人回答下面" in sent
    assert "钉钉" not in sent
    svc.handle("duplicate-new-id", actor("2", reply={"process_key": "process"}))
    assert len(svc.nodes["win"].calls) == 1
    assert not db.get("choice:" + row["question_id"])


def test_blocking_observed_question_keeps_guard(system):
    svc, _db, _alias = system
    observe(svc, asynchronous=False)
    svc.handle("blocked", actor("1", reply={"process_key": "process"}))
    assert not svc.nodes["win"].calls


def test_existing_async_choice_is_upgraded_without_duplicate_notification(system):
    svc, db, _alias = system
    row = observe(svc)
    choice = db.get("choice:" + row["question_id"])
    choice.pop("async", None)
    db.put("choice:" + row["question_id"], choice)
    count = db.db.execute("SELECT count(*) FROM outbox").fetchone()[0]
    observe(svc)
    assert db.db.execute("SELECT count(*) FROM outbox").fetchone()[0] == count
    svc.handle("after-upgrade", actor("1", reply={"process_key": "process"}))
    assert svc.nodes["win"].calls


def test_context_idle_is_not_labeled_recent_or_query_failure():
    text = status_footer(
        {
            "model": "gpt-6.1-sol",
            "context": {
                "used": 4000,
                "window": 100000,
                "observed_at": time.time() - 3600,
            },
            "quota": {"source": "unknown", "status": "unsupported", "checked_at": time.time()},
        }
    )
    assert "最近记录" not in text and "查询失败" not in text


def test_quote_learns_real_message_id_without_queuing_unsupported_reactions(system):
    svc, db, alias = system
    svc.enqueue("chat", "回答", alias, kind="completed")
    oid = db.db.execute("SELECT id FROM outbox").fetchone()[0]
    db.sent(oid, "sent")
    db.link_sent(oid, {"processQueryKey": "receipt-only"})
    db.resolve_reply("chat", {"process_key": "receipt-only", "id": "genuine-bot-message"})
    assert db.resolve_reply("chat", {"id": "genuine-bot-message"})["alias"] == alias
    assert db.pending_reaction() is None


def test_stale_or_foreign_async_answer_is_not_delivered(system):
    svc, db, alias = system
    observe(svc)
    qid = db.get("task-choice:" + alias)
    with pytest.raises(ValueError):
        svc.select("foreign", dict(actor(), sender="other"), qid, "1")
    db.advance_generation(alias)
    svc.handle("stale", actor("1", reply={"process_key": "process"}))
    assert not svc.nodes["win"].calls


def test_resolved_async_quote_accepts_new_text_but_rejects_old_number(system):
    svc, _db, _alias = system
    observe(svc)
    svc.handle("first", actor("1", reply={"process_key": "process"}))
    svc.handle("second", actor("再修一下排版", reply={"process_key": "process"}))
    assert svc.nodes["win"].calls[-1][-1] == "再修一下排版"
    svc.handle("stale-number", actor("2", reply={"process_key": "process"}))
    assert len(svc.nodes["win"].calls) == 2


def test_free_text_without_quote_when_only_one_async_question(system):
    svc, _db, _alias = system
    observe(svc)
    svc.handle("next", actor("继续修好表格"))
    assert svc.nodes["win"].calls[-1][-1] == "继续修好表格"


def test_duplicate_async_callback_is_not_delivered_twice(system):
    svc, _db, _alias = system
    observe(svc)
    for _ in range(2):
        svc.handle("same-id", actor("1", reply={"process_key": "process"}))
    assert len(svc.nodes["win"].calls) == 1


@pytest.mark.parametrize("status", ["failed", "unknown"])
def test_async_submission_failure_or_uncertainty_is_preserved(system, status):
    svc, db, _alias = system
    row = observe(svc)
    svc.nodes["win"].dispatch = lambda *a: {"status": status, "detail": "原始投递错误"}
    svc.handle("answer", actor("1", reply={"process_key": "process"}))
    queued = db.db.execute("SELECT status FROM task_queue").fetchone()[0]
    assert queued == ("saved" if status == "failed" else "unknown")
    assert bool(db.get("choice:" + row["question_id"])) is (status == "failed")


def test_failed_reaction_is_not_recorded_as_success_and_retry_is_bounded(system):
    _svc, db, _alias = system
    db.reaction("r", "received", actor(platform_msg_id="inbound-id"))
    row = db.pending_reaction()
    for _ in range(3):
        db.reaction_failed(row, "system.err")
        row = dict(db.db.execute("SELECT * FROM reactions").fetchone())
    assert row["sent_state"] == "" and row["attempts"] == 3 and row["error"] == "system.err"
    assert db.pending_reaction() is None
    db.reaction("r", "failed")
    assert db.pending_reaction()["state"] == "failed"


def test_summary_reaction_includes_model_version_effort_and_fresh_quota():
    data = {
        "model": "gpt-6.2-astra",
        "effort": "xhigh",
        "context": {"used": 100, "window": 100},
        "quota": {
            "source": "codex",
            "status": "ok",
            "checked_at": time.time(),
            "data": {"rateLimits": {"primary": {"usedPercent": 0, "windowDurationMins": 10080}}},
        },
    }
    assert metric_reaction_text(data) == "a6.2 xh 100/100"
    data["context"]["used"] = 42
    limits = data["quota"]["data"]["rateLimits"]
    limits["primary"]["usedPercent"] = 20
    limits["secondary"] = {"usedPercent": 95, "windowDurationMins": 300}
    assert metric_reaction_text(data) == "a6.2 xh 42/80"
    data["quota"]["checked_at"] -= 601
    assert metric_reaction_text(data) == "a6.2 xh 42/?"
    data["context"]["invalidated"] = True
    assert metric_reaction_text(data) == "a6.2 xh ?/?"
    data.update(model="claude-sonnet-4-6", effort=None)
    assert metric_reaction_text(data) == "n4.6 ?/?"


def test_session_native_weekly_quota_works_when_provider_label_is_custom():
    now = time.time()
    session = {
        "engine": "codex",
        "model": "gpt-6.2-sol",
        "effort": "xhigh",
        "model_provider": "custom",
        "context": {"used": 74, "window": 100},
        "rate_limits_at": now,
        "native_rate_limits": {
            "limit_id": "codex",
            "plan_type": "pro",
            "primary": {"window_minutes": 300, "used_percent": 90},
            "secondary": {"window_minutes": 10080, "used_percent": 15, "resets_at": now + 600},
            "credits": {"balance": "0"},
        },
    }
    quota = session_quota(session)
    assert quota["source"] == "codex_reported"
    assert "本会话上报的Codex额度 7天已用15%" in quota_text(quota)
    assert metric_reaction_text(dict(session, quota=quota)) == "s6.2 xh 74/85"
    assert session_quota(dict(session, engine="claude"))["status"] == "unsupported"
    assert session_quota(dict(session, model_provider="volcengine"))["status"] == "unsupported"
    session["native_rate_limits"]["secondary"]["resets_at"] = now - 1
    assert session_quota(session)["status"] == "unsupported"


def test_async_answer_is_durable_if_dispatch_is_interrupted(system):
    svc, db, alias = system
    row = observe(svc)

    def interrupted(*args, **kwargs):
        raise RuntimeError("simulated gateway shutdown")

    svc._send = interrupted
    with pytest.raises(RuntimeError):
        svc.handle("answer", actor("1", reply={"process_key": "process"}))
    assert not db.get("choice:" + row["question_id"])
    pending = db.queued(alias)
    assert len(pending) == 1 and "机器人回答下面" in pending[0]["body"]
    db.recover()
    assert db.queued(alias)[0]["status"] == "pending"


def test_reanswer_after_definite_failure_replaces_saved_answer_atomically(system):
    svc, db, alias = system
    observe(svc)
    node = svc.nodes["win"]
    calls = node.calls

    def dispatch(*args):
        calls.append(args)
        return {"status": "failed" if len(calls) == 1 else "submitted", "detail": "delivery"}

    node.dispatch = dispatch
    svc.handle("first-answer", actor("1", reply={"process_key": "process"}))
    assert db.queued(alias)[0]["status"] == "saved"
    svc.handle("second-answer", actor("2", reply={"process_key": "process"}))
    assert not db.queued(alias)
    svc.task_command("continue", "chat", db.session(alias), "continue", "")
    svc.drain_queue()
    assert len(calls) == 3  # Two answers, then the explicit ordinary "continue" instruction.
    assert sum("机器人回答下面" in c[-1] for c in calls) == 1
    assert sum("我的消息下面" in c[-1] for c in calls) == 1


def test_explicit_continue_of_failed_answer_invalidates_old_numeric_choice(system):
    svc, db, alias = system
    observe(svc)
    node = svc.nodes["win"]

    def dispatch(*args):
        node.calls.append(args)
        return {"status": "failed" if len(node.calls) == 1 else "submitted", "detail": "delivery"}

    node.dispatch = dispatch
    svc.handle("first-answer", actor("1", reply={"process_key": "process"}))
    svc.task_command("continue", "chat", db.session(alias), "continue", "")
    svc.drain_queue()
    assert len(node.calls) == 2
    svc.handle("late-answer", actor("2", reply={"process_key": "process"}))
    assert len(node.calls) == 2


def test_long_code_fences_close_and_reopen_without_losing_content():
    code = "```python\n" + "print('中文')\n" * 1400 + "```\n结束"
    parts = message_parts("会话", code)
    assert len(parts) > 1
    assert sum(p.count("print('中文')") for p in parts) == 1400
    assert all(p.count("```") % 2 == 0 for p in parts)
    assert parts[-1].endswith("结束")


def test_long_table_repeats_header_and_preserves_every_row():
    header = "| 项目 | 状态 |\n| --- | --- |\n"
    parts = message_parts("会话", header + "".join(f"| 第{i}行 | 正常 |\n" for i in range(1200)))
    assert len(parts) > 1
    assert all(header in p for p in parts)
    assert sum(p.count("| 正常 |") for p in parts) == 1200


def test_table_header_separator_split_at_exact_byte_boundary():
    header = "| H | V |\n| --- | --- |\n"
    text = "p\n" * 2931 + header + "| A | B |\n" * 20
    parts = message_parts("会话", text)
    assert len(parts) > 1
    assert all(not p.split("\n\n", 1)[1].startswith("| ---") for p in parts)
    assert header in parts[-1]


def test_long_table_without_outer_pipes_repeats_header():
    header = "Name | Status\n--- | ---\n"
    parts = message_parts("会话", header + "row | valid\n" * 1600)
    assert len(parts) > 1 and all(header in part for part in parts)
    assert sum(part.count("row | valid") for part in parts) == 1600
