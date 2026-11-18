from __future__ import annotations

import re
from datetime import datetime

SECRET_PATTERNS = [
    (
        r"""(?i)(["']?(?:client[_ ]?secret|app[_ ]?secret|password|密码|access[_ ]?token|api[_ ]?key)["']?\s*[:=：]\s*)(["'])(.*?)\2""",
        r'\1"[已隐藏]"',
    ),
    (r"(?i)(?:sk-|sk_)[a-z0-9_-]{16,}", "[密钥已隐藏]"),
    (
        r'(?i)((?:client[_ ]?secret|app[_ ]?secret|password|密码|access[_ ]?token|api[_ ]?key)\s*[=:：]\s*)[^\s,;"\x27]+',
        r"\1[已隐藏]",
    ),
    (r"(?i)([?&](?:access_token|token|sign|key)=)[^\s&]+", r"\1[已隐藏]"),
    (r"(?<![a-zA-Z0-9])[A-Za-z0-9_-]{48,}(?![a-zA-Z0-9])", "[长凭证已隐藏]"),
]


def redact(text, secrets=()):
    value = str(text or "")
    for secret in secrets:
        if secret and len(secret) > 5:
            value = value.replace(secret, "[已隐藏]")
    for pattern, replacement in SECRET_PATTERNS:
        value = re.sub(pattern, replacement, value)
    return value


def clip(text, limit=1200):
    value = redact(text)
    return value if len(value) <= limit else value[:limit] + "\n…（已截断，可用 /history 查看）"


def quote(text):
    return "\n".join("> " + line for line in clip(text).splitlines()) or "> （无可读文本）"


def context_message(session, kind, prompt, result, ts=None):
    labels = {
        "completed": "本轮结束",
        "aborted": "本轮中止",
        "waiting": "等待处理",
        "started": "开始执行",
        "activity": "会话动态",
    }
    stamp = datetime.fromtimestamp(ts or __import__("time").time()).strftime("%m-%d %H:%M:%S")
    return (
        f"### {labels.get(kind, kind)} · {session['alias']}\n\n"
        f"**电脑**：{session['node']}　**工具**：{session['engine']}\n\n"
        f"**项目**：{session['cwd']}\n\n**时间**：{stamp}\n\n"
        f"**本轮要求**\n{quote(prompt)}\n\n**执行结果**\n{quote(result)}\n\n"
        f"继续此会话：`/send {session['alias']} 你的要求`\n\n"
        f"查看前后文：`/history {session['alias']}`"
    )


def chunks(text, size=4500):
    # Well below DingTalk's UTF-8 message size limit, even for Chinese text.
    text = redact(text)
    parts = [text[i : i + size] for i in range(0, len(text), size)] or ["（空）"]
    if len(parts) == 1:
        return parts
    return [f"**消息 {i + 1}/{len(parts)}**\n\n{part}" for i, part in enumerate(parts)]
