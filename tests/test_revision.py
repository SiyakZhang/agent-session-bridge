import json
import time

import pytest

from session_bridge.scanner import parse_record
from session_bridge.service import Service
from session_bridge.state import Store


def codex(meta, kind, **payload):
    return parse_record("codex", {"type": "event_msg", "payload": dict(type=kind, **payload)}, meta)


@pytest.mark.parametrize(
    "error",
    [
        {
            "message": "Usage limit exceeded; try again at 16:53",
            "codex_error_info": "usage_limit_exceeded",
        },
        {
            "message": "无效的令牌",
            "codex_error_info": {"http_connection_failed": {"http_status_code": 401}},
        },
        {"message": "network disconnected"},
    ],
)
def test_failed_completion_preserves_original_error(error):
    meta = {"sid": "main", "turn": "t"}
    assert (
        codex(meta, "task_complete", turn_id="t", error=error, last_agent_message=None) == "failed"
    )
    assert error["message"] in meta["result"]
    assert meta["status"] == "failed"


@pytest.mark.parametrize(
    "error,category",
    [
        ({"message": "connection timeout after 4000 ms"}, "网络或连接故障"),
        ({"message": "network timeout", "request_id": "req-400-xyz"}, "网络或连接故障"),
        ({"error": {"httpStatusCode": 400, "message": "invalid parameter"}}, "请求或上下文限制"),
        ("upstream returned HTTP 401", "认证失败"),
    ],
)
def test_http_status_comes_from_status_evidence_not_ids_or_timing(error, category):
    from session_bridge.errors import failure_message

    assert category in failure_message(error).splitlines()[0]


def test_claude_api_error_is_failure_even_without_end_turn():
    meta = {}
    assert (
        parse_record(
            "claude",
            {
                "type": "assistant",
                "isApiErrorMessage": True,
                "error": "rate_limit",
                "message": {"content": [{"type": "text", "text": "5小时套餐额度已耗尽"}]},
            },
            meta,
        )
        == "failed"
    )
    assert "5小时套餐额度已耗尽" in meta["result"]


def test_context_is_last_usage_not_lifetime_total():
    meta = {}
    codex(
        meta,
        "token_count",
        info={
            "total_token_usage": {"total_tokens": 9000000},
            "last_token_usage": {"input_tokens": 4000, "output_tokens": 1000, "total_tokens": 5000},
            "model_context_window": 100000,
        },
        rate_limits={"limit_id": "codex", "primary": {"used_percent": 20}},
    )
    assert meta["context"]["used"] == 5000
    assert meta["context"]["window"] == 100000


def test_compaction_completion_never_generates_empty_final():
    meta = {"sid": "main", "turn": "t"}
    assert codex(meta, "context_compacted") == "compacted"
    assert "压缩" in meta["result"]


@pytest.fixture
def system(tmp_path):
    db = Store(tmp_path / "s.db")
    svc = Service(db, {"nodes": [{"id": "win"}]})
    db.pair("t", "u", "chat")
    alias = db.upsert_session(
        "win",
        "codex",
        "same",
        str(tmp_path),
        {
            "live": True,
            "title": "修复钉钉桥接",
            "managed": True,
            "status": "idle",
        },
    )
    yield svc, db, alias
    db.close()


def test_failures_and_interruptions_notify_and_pause_queue(system):
    svc, db, alias = system
    svc.ingest(
        "win",
        {
            "events": [
                {
                    "engine": "codex",
                    "sid": "same",
                    "event_id": "failure",
                    "kind": "failed",
                    "result": "401 无效的令牌",
                }
            ]
        },
    )
    row = db.db.execute("SELECT body FROM outbox ORDER BY id DESC LIMIT 1").fetchone()
    assert row and "401 无效的令牌" in row[0]
    assert db.get("paused:" + alias)


@pytest.mark.parametrize("native_weekly", [False, True])
def test_final_result_adds_summary_next_to_receipt_on_matched_user_input_only(
    system, native_weekly
):
    svc, db, alias = system
    db.upsert_session(
        "win",
        "codex",
        "same",
        "",
        {
            "model": "gpt-6.2-sol",
            "model_provider": "custom",
            "context": {"used": 88000, "window": 100000},
            "quota": {
                "source": "codex_reported",
                "status": "ok",
                "checked_at": time.time(),
                "data": {
                    "rateLimits": {"primary": {"windowDurationMins": 10080, "usedPercent": 5}}
                },
            },
        },
    )
    db.reaction("input", "processing", {"chat": "chat", "open_msg_id": "person-id"})
    db.put("active-receipt:" + alias, {"rid": "input", "turn": "actual-turn", "generation": 1})
    event = {
        "engine": "codex",
        "sid": "same",
        "result": "结果正文",
        "kind": "completed",
        "event_id": "stale-result",
        "turn": "old-turn",
    }
    svc.ingest("win", {"events": [event]})
    assert db.db.execute("SELECT state FROM reactions").fetchone()[0] == "processing"
    event.update(
        event_id="actual-result",
        turn="actual-turn",
        effort="xhigh",
        model_provider="custom" if native_weekly else "volcengine",
    )
    if native_weekly:
        event["native_rate_limits"] = {
            "limit_id": "codex",
            "plan_type": "pro",
            "primary": {"window_minutes": 10080, "used_percent": 15},
        }
        event["rate_limits_at"] = time.time()
    svc.ingest("win", {"events": [event, event]})
    body = db.db.execute("SELECT body FROM outbox").fetchone()[0]
    assert "修复钉钉桥接" in body.splitlines()[0]
    assert body.splitlines()[-1] == "结果正文"
    metrics = db.db.execute(
        "SELECT open_id,state,emotion_id FROM reactions ORDER BY metric,id"
    ).fetchall()
    assert [tuple(row) for row in metrics] == [
        ("person-id", "received", "2659900"),
        ("person-id", "s6.2 xh 88/" + ("85" if native_weekly else "?"), "54054"),
    ]
    assert db.pending_reaction()["metric"] == 0
    db.reacted("input", "received")
    assert db.pending_reaction()["emotion_id"] == "54054"


def test_completed_desktop_turn_without_user_message_does_not_schedule_reactions(system):
    svc, db, alias = system
    db.put("active-receipt:" + alias, {"rid": "without-message-id", "turn": "turn"})
    svc.react_active(alias, "completed", {"turn": "turn"})
    assert db.db.execute("SELECT count(*) FROM reactions").fetchone()[0] == 0


def test_legacy_input_receipt_cannot_post_metrics_after_context_reset(system):
    svc, db, alias = system
    db.reaction("legacy", "processing", {"chat": "chat", "open_msg_id": "person-id"})
    db.queue_input("legacy", alias, "chat", "original question")
    db.put("active-receipt:" + alias, {"rid": "legacy", "turn": "legacy-turn"})
    db.advance_generation(alias)
    svc.react_active(alias, "completed", {"turn": "legacy-turn"})
    assert db.db.execute("SELECT state FROM reactions").fetchall()[0][0] == "processing"
    assert db.db.execute("SELECT count(*) FROM reactions").fetchone()[0] == 1


def test_subagent_command_removed(system):
    from session_bridge.commands import BY_NAME, help_text

    assert "subagent" not in BY_NAME
    assert "子代理" not in help_text()


def test_new_tasks_use_titles_without_consuming_name_pool(system):
    _svc, db, alias = system
    assert db.task(alias, True)["code"] == "修复钉钉桥接"


def test_rich_question_accepts_free_text_and_sequential_answers(system):
    svc, db, alias = system
    actor = {"tenant": "t", "sender": "u", "chat": "chat"}
    svc.offer(
        actor,
        "q",
        {
            "kind": "native",
            "command": "answer",
            "native_id": "native",
            "request_key": "epoch:1",
            "questions": [
                {
                    "id": "a",
                    "question": "选框架",
                    "options": [{"label": "React"}, {"label": "Vue"}],
                },
                {"id": "b", "question": "补充要求", "options": []},
            ],
            "options": [],
        },
        "问题",
        alias,
    )
    db.put("native-questions:" + alias, ["native"])
    calls = []
    svc.action = lambda *args: calls.append(args)
    svc.select("a", actor, "q", "2")
    assert not calls
    svc.select("b", actor, "q:next", "保留缩进和中文")
    answer = json.loads(calls[0][4])
    assert answer["answers"] == {"a": {"answers": ["Vue"]}, "b": {"answers": ["保留缩进和中文"]}}


def test_quota_zero_credits_does_not_mean_exhausted():
    from session_bridge.telemetry import quota_text

    quota = {
        "source": "codex",
        "checked_at": time.time(),
        "status": "ok",
        "data": {
            "ordinaryUsageAllowed": True,
            "rateLimits": {
                "planType": "pro",
                "primary": {"usedPercent": 1, "windowDurationMins": 10080},
                "credits": {"balance": "0"},
            },
        },
    }
    value = quota_text(quota)
    assert "1%" in value and "7天" in value
    assert "耗尽" not in value


def test_reaction_retains_platform_original_id():
    from session_bridge.dingtalk_messages import callback_data

    data = callback_data({"msgId": "m", "openMsgId": "original", "conversationId": "chat"})
    assert data["open_msg_id"] == "original"


@pytest.mark.parametrize(
    "code,label",
    [
        ("usageLimitExceeded", "额度或套餐限制"),
        ("rateLimitExceeded", "限流或服务暂时不可用"),
        ("contextWindowExceeded", "请求或上下文限制"),
        ("ModelNotFound", "模型或渠道不可用"),
        ("InsufficientBalance", "账户余额限制"),
    ],
)
def test_camel_case_provider_error_codes_keep_correct_classification(code, label):
    from session_bridge.errors import failure_message

    text = failure_message({"message": "request rejected", "codexErrorInfo": code})
    assert label in text and code in text
