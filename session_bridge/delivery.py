from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
from pathlib import Path

import psutil


class DeliveryUnknown(RuntimeError):
    """An external write may already have happened. Never automatically retry."""


def cli_command(name):
    """Resolve native CLI entrypoints; never pass untrusted text through cmd.exe."""
    exe = shutil.which(name)
    if os.name == "nt":
        if name == "codex":
            npm = Path(os.environ.get("APPDATA", "")) / "npm/node_modules/@openai/codex"
            binaries = list(npm.glob("**/codex.exe")) if npm.exists() else []
            if binaries:
                return [str(binaries[0])]
        if exe and exe.lower().endswith((".cmd", ".bat", ".ps1")):
            raise RuntimeError("Native CLI executable unavailable; shell wrapper refused")
    if not exe:
        candidate = Path.home() / ".local/bin" / name
        if candidate.exists():
            exe = str(candidate)
    if not exe:
        raise RuntimeError(f"{name} CLI not found")
    return [exe]


def live_peers(claude_home):
    peers = {}
    for path in (Path(claude_home) / "sessions").glob("*.json"):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
            pid = int(row["pid"])
            if pid <= 0 or row.get("peerProtocol") != 1 or not row.get("messagingSocketPath"):
                continue
            process = psutil.Process(pid)
            if not process.is_running():
                continue
            # startedAt is epoch milliseconds. Reject recycled PIDs even if names match.
            started = float(row.get("startedAt", 0)) / 1000
            if not started or abs(process.create_time() - started) > 120:
                continue
            if "claude" not in " ".join(process.cmdline()).lower():
                continue
            row["process_created"] = process.create_time()
            peers[row["sessionId"]] = row
        except (OSError, ValueError, KeyError, psutil.Error):
            continue
    return peers


def codex_live(codex_home, sid):
    path = Path(codex_home) / "thread-writer-locks" / f"{sid}.lock"
    if not path.exists():
        return False
    try:
        with path.open("r+b") as file:
            if os.name == "nt":
                import msvcrt

                try:
                    msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    return True
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                try:
                    fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)
    except OSError:
        return False
    return False


def peer_token(claude_home, peer):
    # Match socket hash as well as process start metadata, never choose arbitrary pid key.
    socket_path = peer["messagingSocketPath"]
    paths = {socket_path, os.path.realpath(socket_path)}
    if os.name == "nt":
        # Native Windows Claude 2.1.322 hashes the case-folded registry address.
        paths.update(p.lower() for p in list(paths))
    candidates = {hashlib.sha256(p.encode()).hexdigest() for p in paths}
    for path in (Path(claude_home) / "sessions").glob(f"{peer['pid']}.*.key"):
        if path.name.split(".")[1] not in candidates:
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if (data.get("procStartFt") or data.get("procStart")) != peer.get("procStart"):
            continue
        return data["peerToken"]
    raise RuntimeError("Exact Claude peer authentication capability not found")


def send_peer(peer, token, text, request_id):
    frame = {
        "type": "user",
        "message": {"role": "user", "content": text},
        "priority": "next",
        "msgV": 1,
        "msg_id": "cc-msg-" + hashlib.md5(request_id.encode()).hexdigest(),
    }
    payload = (
        json.dumps({"type": "auth", "token": token})
        + "\n"
        + json.dumps(frame, ensure_ascii=False)
        + "\n"
    ).encode("utf-8")
    address = peer["messagingSocketPath"]
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        if address.startswith("\\\\\\\\.\\\\pipe\\\\"):
            address = address.replace("\\\\", "\\")
        if not address.startswith("\\\\.\\pipe\\"):
            raise RuntimeError("Unexpected Windows peer address")
        wait_pipe = ctypes.windll.kernel32.WaitNamedPipeW
        wait_pipe.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
        wait_pipe.restype = wintypes.BOOL
        if not wait_pipe(address, 3000):
            raise RuntimeError("Claude named pipe unavailable")
        with open(address, "r+b", buffering=0) as pipe:
            try:
                pipe.write(payload)
            except OSError as exc:
                raise DeliveryUnknown("Claude pipe write uncertain") from exc
    else:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(3)
            connection.connect(address)
            try:
                connection.sendall(payload)
            except OSError as exc:
                raise DeliveryUnknown("Claude socket write uncertain") from exc
    return "submitted", "已写入指定 Claude 会话消息管道；是否接受或等待批准请看后续会话记录。"


def deliver(session, text, request_id, scanner):
    sid, engine = session["sid"], session["engine"]
    if not re.fullmatch(r"[a-fA-F0-9-]{36}", sid):
        raise ValueError("Invalid full session UUID")
    if not text.strip() or len(text) > 16000:
        raise ValueError("Message must contain 1–16000 characters")
    marker = hashlib.sha256(request_id.encode()).hexdigest()[:12]
    prompt = text
    if engine == "codex":
        if not codex_live(scanner.codex_home, sid):
            raise RuntimeError(
                "原 Codex 会话未确认处于运行状态。请先在原电脑打开此会话，不会自动新建或切换。"
            )
        command = cli_command("codex") + ["queue", "--thread", sid, "--message", prompt]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        done = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=25,
            creationflags=flags,
        )
        if done.returncode:
            raise RuntimeError("Codex queue failed: " + done.stderr[-500:])
        return "queued", f"请求 {marker} 已进入原 Codex 会话队列；尚不代表已开始执行。"
    if engine == "claude":
        peer = live_peers(scanner.claude_home).get(sid)
        if not peer:
            raise RuntimeError("原 Claude 会话没有可验证的活动消息管道；保持监控，不新建会话。")
        return send_peer(peer, peer_token(scanner.claude_home, peer), prompt, request_id)
    raise ValueError("Unknown engine")
