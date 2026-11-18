"""One readable layout for both cards and Markdown fallback."""

import html

from .formatting import chunks
from .telemetry import status_footer

COLORS = {
    "completed": "#148447",
    "failed": "#D93025",
    "aborted": "#D93025",
    "waiting": "#B26A00",
    "reconnecting": "#B26A00",
    "compacting": "#B26A00",
    "compacted": "#148447",
}


def message_parts(title, text, data, kind=None):
    safe_title = html.escape(title).replace("\n", " ")
    footer = status_footer(data)
    parts = chunks(text, size=10000)
    result = []
    for i, part in enumerate(parts):
        # chunks adds pagination only for multi-part output. Preserve every source character.
        if len(parts) > 1:
            part = part.split("｜", 1)[1]
        color = COLORS.get(kind, "#6B7280")
        heading = f"###### <font color='{color}'>{safe_title}</font>"
        if len(parts) > 1:
            heading += f" · {i + 1}/{len(parts)}"
        result.append(
            heading + "\n\n" + part + ("\n\n---\n" + footer if i == len(parts) - 1 else "")
        )
    return result
