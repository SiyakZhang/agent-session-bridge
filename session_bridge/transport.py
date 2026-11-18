from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import sys
import threading
import time

import paramiko

LOG = logging.getLogger(__name__)


class ManagedNode:
    def __init__(self, config, on_snapshot):
        self.config = config
        self.node_id = config["id"]
        self.instance = config["instance"]
        self.on_snapshot = on_snapshot
        self.online = False
        self.version = ""
        self.error = "尚未连接"
        self.last_seen = 0
        self.writer = None
        self.handle = None
        self.write_lock = threading.Lock()
        self.pending = {}
        self.pending_lock = threading.Lock()
        self.stop = threading.Event()

    def start(self):
        threading.Thread(target=self.loop, name="node-" + self.node_id, daemon=True).start()

    def connect(self):
        config = self.config
        if config.get("host"):
            client = paramiko.SSHClient()
            client.load_host_keys(config["known_hosts"])
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            client.connect(
                config["host"],
                port=config.get("port", 22),
                username=config["user"],
                password=config.get("password"),
                key_filename=config.get("key_file"),
                look_for_keys=False,
                allow_agent=False,
                timeout=12,
                banner_timeout=12,
                auth_timeout=12,
            )
            client.get_transport().set_keepalive(20)
            command = (
                "cd "
                + shlex.quote(config["directory"])
                + " && exec "
                + shlex.join(
                    [
                        config.get("python", "python3"),
                        "-u",
                        "-m",
                        "session_bridge.node",
                        "--id",
                        self.node_id,
                        "--state",
                        config["state"],
                    ]
                )
            )
            # Only configured executable/path arguments enter a shell. User text uses stdin JSON.
            stdin, stdout, stderr = client.exec_command(command)
            self.handle = client
            self.writer = stdin
            threading.Thread(target=self.drain_errors, args=(stderr,), daemon=True).start()
            return stdout
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = subprocess.Popen(
            [
                sys.executable,
                "-u",
                "-m",
                "session_bridge.node",
                "--id",
                self.node_id,
                "--state",
                config["state"],
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            creationflags=flags,
        )
        self.handle = process
        self.writer = process.stdin
        threading.Thread(target=self.drain_errors, args=(process.stderr,), daemon=True).start()
        return process.stdout

    def drain_errors(self, stream):
        from .formatting import redact

        for line in stream:
            if isinstance(line, bytes):
                line = line.decode("utf-8", "replace")
            LOG.warning("Node %s: %s", self.node_id, redact(line)[:500])

    def write(self, payload):
        with self.write_lock:
            if not self.writer:
                raise ConnectionError("Node disconnected")
            self.writer.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self.writer.flush()

    def identity_matches(self, frame):
        return frame.get("node") == self.node_id and frame.get("instance") == self.instance

    def loop(self):
        while not self.stop.is_set():
            try:
                stream = self.connect()
                hello = json.loads(stream.readline())
                if (
                    hello.get("type") != "hello"
                    or hello.get("protocol") != 1
                    or not self.identity_matches(hello)
                ):
                    raise ValueError("节点身份与已固定身份不一致，拒绝连接")
                self.online, self.error = True, ""
                self.version = str(hello.get("version") or "unknown")[:40]
                LOG.info("Node online: %s", self.node_id)
                for line in stream:
                    if self.stop.is_set():
                        break
                    frame = json.loads(line)
                    if not self.identity_matches(frame):
                        raise ValueError("节点响应身份不一致")
                    self.last_seen = time.time()
                    if frame.get("type") == "snapshot":
                        ack = self.on_snapshot(self.node_id, frame)
                        self.write(
                            {
                                "type": "ack",
                                "node": self.node_id,
                                "instance": self.instance,
                                "events": ack,
                            }
                        )
                    elif frame.get("type") == "receipt":
                        with self.pending_lock:
                            item = self.pending.get(frame.get("request_id"))
                            if (
                                item
                                and frame.get("sid") == item["sid"]
                                and frame.get("engine") == item["engine"]
                            ):
                                item["response"] = frame
                                item["event"].set()
            except (OSError, ValueError, RuntimeError, paramiko.SSHException) as exc:
                from .formatting import redact

                self.error = redact(str(exc))[:300]
                LOG.warning("Node %s disconnected: %s", self.node_id, self.error)
            finally:
                self.online = False
                self.writer = None
                if self.handle:
                    if isinstance(self.handle, subprocess.Popen):
                        self.handle.terminate()
                        try:
                            self.handle.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            self.handle.kill()
                    else:
                        self.handle.close()
                self.handle = None
            self.stop.wait(8)

    def action(self, request_id, engine, sid, command, argument):
        return self.dispatch(request_id, engine, sid, argument, command)

    def dispatch(self, request_id, engine, sid, text, action=None):
        if not self.online or time.time() - self.last_seen > 45:
            return {"status": "failed", "detail": "目标电脑离线或状态过期；没有转发到其他电脑。"}
        item = {"event": threading.Event(), "engine": engine, "sid": sid}
        with self.pending_lock:
            self.pending[request_id] = item
        try:
            self.write(
                {
                    "type": "action" if action else "send",
                    "node": self.node_id,
                    "instance": self.instance,
                    "request_id": request_id,
                    "engine": engine,
                    "sid": sid,
                    "text": text,
                    "action": action,
                }
            )
            if not item["event"].wait(35):
                return {
                    "status": "unknown",
                    "detail": "未收到投递回执，状态未知；为避免重复执行，没有自动重发。",
                }
            return item["response"]
        except (OSError, ValueError):
            return {"status": "unknown", "detail": "投递期间连接中断，状态未知；没有自动重发。"}
        finally:
            with self.pending_lock:
                self.pending.pop(request_id, None)
