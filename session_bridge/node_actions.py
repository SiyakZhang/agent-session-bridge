"""Read-only node capabilities. All paths come from an exact discovered session."""

import base64
import io
import json
import os
import subprocess
from pathlib import Path

from .formatting import redact

MAX_FILE = 8 * 1024 * 1024


def capabilities(session):
    result = ["diff", "file", "queue", "continue"]
    if session.get("path"):
        result.append("history")
    try:
        from PIL import Image  # noqa: F401

        result.append("image")
    except ImportError:
        pass
    if os.name == "nt" or os.environ.get("DISPLAY"):
        try:
            from PIL import ImageGrab  # noqa: F401

            result.append("screen")
        except ImportError:
            pass
    return result


def project_path(session, argument, state=None):
    root = Path(session["cwd"]).resolve(strict=True)
    target = Path(argument)
    target = (
        (root / target).resolve(strict=True)
        if not target.is_absolute()
        else target.resolve(strict=True)
    )
    try:
        target.relative_to(root)
    except ValueError:
        raise ValueError("只能读取当前项目目录内的文件。") from None
    if state:
        try:
            target.relative_to(Path(state).resolve())
        except ValueError:
            pass
        else:
            raise ValueError("桥接私有状态不可通过机器人导出。")
    if not target.is_file():
        raise ValueError("目标不是文件。")
    if target.stat().st_size > MAX_FILE:
        raise ValueError("文件超过 8 MiB，请在电脑上查看。")
    # Explicit attachment requests must not disclose credential stores or private bridge state.
    if any(
        p.lower() in (".git", ".ssh", ".codex", ".claude", ".dingtalk-session-bridge")
        for p in target.parts
    ):
        raise ValueError("该目录包含私有状态，不通过机器人导出。")
    if target.name.lower().startswith(".env") or target.suffix.lower() in (".pem", ".key"):
        raise ValueError("凭证文件不通过机器人导出。")
    return target


def visible_record(engine, record):
    kind = record.get("type", "")
    if engine == "codex":
        payload = record.get("payload", {})
        if not isinstance(payload, dict) or payload.get("channel") in ("analysis", "reasoning"):
            return None
        if kind == "response_item" and isinstance(payload, dict):
            if payload.get("type") in ("reasoning", "compaction"):
                return None
            if payload.get("role") in ("system", "developer"):
                return None
        elif kind == "event_msg":
            if payload.get("type") not in {
                "user_message",
                "agent_message",
                "task_started",
                "task_complete",
                "turn_aborted",
                "exec_command_begin",
                "exec_command_end",
                "exec_command_output_delta",
                "apply_patch_begin",
                "apply_patch_end",
                "mcp_tool_call_begin",
                "mcp_tool_call_end",
                "web_search_begin",
                "web_search_end",
                "warning",
                "error",
                "token_count",
                "context_compacted",
                "session_configured",
            }:
                return None
        elif kind == "turn_context":
            record = {
                "type": kind,
                "timestamp": record.get("timestamp"),
                "payload": {k: payload[k] for k in ("cwd", "model", "effort") if k in payload},
            }
        else:
            return None
    elif kind not in ("user", "assistant", "system", "progress"):
        return None

    # Hide private reasoning fields while retaining visible messages, tool calls and outputs.
    def clean(value):
        if isinstance(value, list):
            return [
                clean(x)
                for x in value
                if not (
                    isinstance(x, dict)
                    and x.get("type") in ("thinking", "redacted_thinking", "reasoning")
                )
            ]
        if isinstance(value, dict):
            return {
                k: clean(v)
                for k, v in value.items()
                if k not in ("encrypted_content", "signature", "thinking")
            }
        return value

    return redact(json.dumps(clean(record), ensure_ascii=False, indent=2))


def read_transcript(session, argument):
    if not session.get("path"):
        raise ValueError("此会话尚无可读取的记录文件。")
    path = Path(session["path"])
    offset = int(argument or 0)
    if offset < 0 or offset > path.stat().st_size:
        raise ValueError("历史位置无效，请从首页重新打开。")
    records = []
    size = 0
    with path.open("rb") as stream:
        if offset:
            stream.seek(offset - 1)
            if stream.read(1) != b"\n":
                raise ValueError("历史位置必须位于记录边界。")
        else:
            stream.seek(0)
        while len(records) < 100 and size < 300000:
            start = stream.tell()
            line = stream.readline()
            if not line or not line.endswith(b"\n"):
                stream.seek(start)
                break
            try:
                text = visible_record(session["engine"], json.loads(line))
            except (ValueError, UnicodeError):
                continue
            if text:
                records.append(text)
                size += len(text)
        cursor = stream.tell()
        more = stream.readline().endswith(b"\n")
    return {
        "status": "ok",
        "detail": "\n\n".join(records) or "本页暂无可显示记录。",
        "cursor": cursor,
        "more": more,
    }


def run_action(session, command, argument, state):
    if command == "session":
        return read_transcript(session, argument)
    if command == "diff":
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        result = subprocess.run(
            ["git", "--no-pager", "diff", "--no-ext-diff", "--no-textconv", "HEAD", "--"],
            cwd=session["cwd"],
            capture_output=True,
            check=False,
            timeout=20,
            creationflags=flags,
        )
        if result.returncode:
            raise ValueError(
                "无法读取 Git 差异：" + redact(result.stderr.decode("utf-8", "replace")[-500:])
            )
        return {
            "status": "ok",
            "detail": redact(result.stdout.decode("utf-8", "replace"))
            or "没有已跟踪文件的未提交改动。",
        }
    if command == "file":
        target = project_path(session, argument, state)
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeError:
            raise ValueError("该文件不是 UTF-8 文本；图片请用 ！图片。") from None
        return {"status": "ok", "detail": redact(text)}
    if command in ("image", "screen"):
        from PIL import Image, ImageGrab

        if command == "screen":
            picture = ImageGrab.grab(all_screens=True)
        else:
            target = project_path(session, argument, state)
            picture = Image.open(target)
            picture.load()
        with picture:
            stream = io.BytesIO()
            picture.convert("RGB").save(stream, format="JPEG", quality=85)
        content = stream.getvalue()
        if len(content) > MAX_FILE:
            raise ValueError("图片超过上传预算，未自动压缩或丢弃内容。")
        return {
            "status": "ok",
            "detail": "桌面截图" if command == "screen" else target.name,
            "image_base64": base64.b64encode(content).decode("ascii"),
            "mime": "image/jpeg",
        }
    raise ValueError("该节点没有实现这个原生操作，没有转发给模型。")
