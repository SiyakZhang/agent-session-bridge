"""Keep the upstream evidence; classification never substitutes for the original error."""

import json

from .formatting import redact


def error_text(error):
    return redact(error if isinstance(error, str) else json.dumps(error, ensure_ascii=False))


def failure_message(error, retrying=False):
    raw = error_text(error)
    value = raw.lower()
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
    elif any(
        x in value
        for x in ("401", "unauthorized", "authentication", "无效的令牌", "credentialinvalid")
    ):
        label = "认证失败"
    elif any(
        x in value
        for x in ("no available", "无可用渠道", "model_not_found", "modelnotfound", "modelnotopen")
    ):
        label = "模型或渠道不可用"
    elif any(x in value for x in ("429", "rate_limit", "ratelimit", "overloaded", "503", "限流")):
        label = "限流或服务暂时不可用"
    elif any(
        x in value
        for x in (
            "context_length",
            "context window",
            "contextwindow",
            "contextlength",
            "上下文",
            "400",
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
