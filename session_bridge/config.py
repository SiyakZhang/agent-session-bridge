from __future__ import annotations

import base64
import ctypes
import json
import os
import tempfile
from pathlib import Path


def state_dir():
    return Path(
        os.environ.get("SESSION_BRIDGE_HOME", str(Path.home() / ".dingtalk-session-bridge"))
    )


def private_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".write-")
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def protect(value, decrypt=False):
    if os.name != "nt":
        return value

    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_byte))]

    raw = base64.b64decode(value[6:]) if decrypt else value.encode("utf-8")
    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    target = Blob()
    function = (
        ctypes.windll.crypt32.CryptUnprotectData
        if decrypt
        else ctypes.windll.crypt32.CryptProtectData
    )
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise OSError("Windows credential protection failed")
    try:
        result = ctypes.string_at(target.data, target.size)
        return result.decode("utf-8") if decrypt else "dpapi:" + base64.b64encode(result).decode()
    finally:
        ctypes.windll.kernel32.LocalFree(target.data)


def reveal(value):
    return protect(value, decrypt=True) if value.startswith("dpapi:") else value


def load_config(path=None):
    target = Path(path) if path else state_dir() / "config.json"
    config = json.loads(target.read_text(encoding="utf-8"))
    config["client_secret"] = reveal(config["client_secret"])
    for node in config.get("nodes", []):
        if node.get("password"):
            node["password"] = reveal(node["password"])
    names = [n["id"] for n in config["nodes"]]
    if len(names) != len(set(names)):
        raise ValueError("Duplicate node names")
    return config
