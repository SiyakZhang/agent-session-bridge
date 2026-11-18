"""Native Markdown layout; preserve tables and fenced code across messages."""

import html
import re

from .formatting import chunks

TABLE_SEPARATOR = re.compile(r"\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*")


def message_parts(title, text):
    title = " ".join(title.split())
    safe_title = html.escape(title if len(title) <= 38 else title[:37] + "…")
    safe_title = re.sub(r"([\\`*_\[\]])", r"\\\1", safe_title)
    message_heading = "> " + safe_title
    parts = chunks(text, size=9000)
    result = []
    fence, table, previous_line = None, None, ""
    for i, part in enumerate(parts):
        if len(parts) > 1:
            part = part.split("｜", 1)[1]
        prefix = fence[1] + "\n" if fence else ""
        if not fence and table and "|" in part.splitlines()[0]:
            prefix = table
        elif not fence and TABLE_SEPARATOR.fullmatch(part.splitlines()[0]) and previous_line:
            # Header and separator may lie on opposite sides of a byte-budget boundary.
            prefix = previous_line + "\n"
        for line in part.splitlines():
            marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
            if marker:
                token, tail = marker.groups()
                if fence is None:
                    fence = (token, line)
                elif token[0] == fence[0][0] and len(token) >= len(fence[0]) and not tail.strip():
                    fence = None
                table = None
            elif not fence:
                if TABLE_SEPARATOR.fullmatch(line):
                    header = previous_line + "\n" + line + "\n"
                    table = header if len(header.encode("utf-8")) < 1200 else None
                elif "|" not in line:
                    table = None
            previous_line = line
        heading = message_heading
        if len(parts) > 1:
            heading += f" · {i + 1}/{len(parts)}"
        suffix = ("" if part.endswith("\n") else "\n") + fence[0] if fence else ""
        result.append(heading + "\n\n" + prefix + part + suffix)
    return result
