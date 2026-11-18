"""Keep the upstream evidence; classification never substitutes for the original error."""

import json
import re

from .formatting import redact

HTTP_ERROR = re.compile(
    r"\b(?:http(?:/\d(?:\.\d)?)?|status(?:[ _-]code)?|状态码)\s*[:=]?\s*([45]\d\d)\b", re.IGNORECASE
)
STATUS_FIELDS = {"status", "statuscode", "httpstatus", "httpstatuscode", "code", "errcode"}


def error_signals(value):
    """Exclude opaque IDs and extract status only from explicit error evidence."""
    if isinstance(value, dict):
        signals, statuses = [], set()
        for key, item in value.items():
            field = re.sub(r"[^a-z]", "", key.lower())
            if field.endswith("id"):
                continue
            if field in STATUS_FIELDS and re.fullmatch(r"[45]\d\d", str(item)):
                statuses.add(str(item))
                continue
            messages, codes = error_signals(item)
            signals.extend([key, *messages])
            statuses.update(codes)
        return signals, statuses
    if isinstance(value, list):
        signals, statuses = [], set()
        for item in value:
            messages, codes = error_signals(item)
            signals.extend(messages)
            statuses.update(codes)
        return signals, statuses
    if not isinstance(value, str):
        return [], set()
    codes = set(HTTP_ERROR.findall(value))
    leading = re.match(r"^\s*(401|403|429|503|400)\s*(?:$|[：:]|\s+\D)", value)
    if leading:
        codes.add(leading[1])
    return [value], codes


def error_text(error):
    return redact(error if isinstance(error, str) else json.dumps(error, ensure_ascii=False))


def failure_message(error, retrying=False):
    raw = error_text(error)
    messages, codes = error_signals(error)
    value = " ".join(messages).lower()
    if any(
        x in value
        for x in (
            "usage_limit",
            "usage limit",
            "usagelimit",
            "quota",
            "额度",
            "套餐",
            "insufficient_quota",
        )
    ):
        label = "额度或套餐限制"
    elif any(
        x in value
        for x in (
            "accountoverdue",
            "insufficient_balance",
            "insufficientbalance",
            "insufficient credits",
            "余额",
            "欠费",
        )
    ):
        label = "账户余额限制"
    elif "401" in codes or any(
        x in value for x in ("unauthorized", "authentication", "无效的令牌", "credentialinvalid")
    ):
        label = "认证失败"
    elif any(
        x in value
        for x in ("no available", "无可用渠道", "model_not_found", "modelnotfound", "modelnotopen")
    ):
        label = "模型或渠道不可用"
    elif codes & {"429", "503"} or any(
        x in value for x in ("rate_limit", "ratelimit", "overloaded", "限流")
    ):
        label = "限流或服务暂时不可用"
    elif "400" in codes or any(
        x in value
        for x in (
            "context_length",
            "context window",
            "contextwindow",
            "contextlength",
            "上下文",
        )
    ):
        label = "请求或上下文限制"
    elif any(
        x in value
        for x in ("network", "connection", "timeout", "timed out", "网络", "连接", "dns", "tls")
    ):
        label = "网络或连接故障"
    else:
        label = "执行失败"
    state = "正在重连" if retrying else "本轮失败"
    return f"{state} · {label}\n\n原始错误：\n{raw}\n\n可回复本条消息继续与原会话对话。"
