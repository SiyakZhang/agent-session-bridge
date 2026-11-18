"""Read-only quota snapshots; polling is independent of message delivery."""

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from .formatting import redact


def provider_source(session):
    provider = str(session.get("model_provider") or session.get("provider") or "").lower()
    if provider in ("openai", "codex", "chatgpt"):
        return "codex"
    if any(x in provider for x in ("volc", "ark", "火山")):
        return "ark"
    return "unknown"


def session_quota(session):
    """Only an observation made inside this session is eligible for its footer."""
    limits = session.get("native_rate_limits")
    binding = session.get("quota_binding") or {}
    if (
        not isinstance(limits, dict)
        or provider_source(session) != "codex"
        or binding.get("auth_mode") != "chatgpt"
        or not binding.get("account_fingerprint")
        or binding.get("provider") != session.get("model_provider")
        or session.get("rate_limits_at", 0) < binding.get("verified_at", float("inf"))
    ):
        return {"source": "unknown", "status": "unsupported", "checked_at": time.time()}

    def camel(value):
        if isinstance(value, dict):
            return {
                k.split("_")[0] + "".join(x.title() for x in k.split("_")[1:]): camel(v)
                for k, v in value.items()
            }
        return value

    if limits.get("limit_id", limits.get("limitId")) not in ("codex", None):
        return {"source": "unknown", "status": "unsupported", "checked_at": time.time()}
    return {
        "source": "codex",
        "status": "ok",
        "checked_at": session.get("rate_limits_at", 0),
        "data": {"rateLimits": camel(limits)},
    }


def quota_text(quota):
    if not quota:
        return "额度未知"
    source = {"codex": "官方Codex", "ark": "火山套餐"}.get(quota.get("source"), "当前服务")
    age = time.time() - quota.get("checked_at", 0)
    stamp = datetime.fromtimestamp(quota.get("checked_at", time.time())).strftime("%H:%M")
    if quota.get("status") != "ok":
        if quota.get("status") == "pending":
            return source + "查询中/未知 " + stamp
        return source + "查询失败/未知 " + stamp
    if age > 600:
        return source + "缓存已过期 " + stamp
    data = quota.get("data") or {}
    if quota.get("source") == "codex":
        limits = data.get("rateLimits") or data
        windows = []
        for name in ("primary", "secondary"):
            window = limits.get(name)
            if not window or window.get("usedPercent") is None:
                continue
            minutes = window.get("windowDurationMins")
            period = (
                f"{minutes // 1440}天"
                if minutes and minutes % 1440 == 0
                else f"{minutes // 60}小时"
                if minutes and minutes % 60 == 0
                else f"{minutes}分钟"
                if minutes
                else "窗口"
            )
            text = f"{period}已用{window['usedPercent']:g}%"
            if window.get("resetsAt"):
                text += " 重置" + datetime.fromtimestamp(window["resetsAt"]).strftime("%m-%d %H:%M")
            windows.append(text)
        return source + (" " + "; ".join(windows) if windows else " 窗口未知") + " " + stamp
    if quota.get("source") == "ark":
        periods = []

        def walk(value):
            if isinstance(value, dict):
                if "percent" in value and value["percent"] is not None:
                    periods.append(
                        str(value.get("label") or "窗口")
                        + "已用"
                        + str(value["percent"])
                        + "%"
                        + (" 重置" + str(value["reset_at"]) if value.get("reset_at") else "")
                    )
                else:
                    for child in value.values():
                        walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(data)
        return source + (" " + "; ".join(periods) if periods else " 额度字段未知") + " " + stamp
    return "当前服务额度未接入/未知"


def status_footer(data):
    context = data.get("context") or {}
    used, window = context.get("used"), context.get("window")
    ctx = f"{used:,}/{window:,} ({used / window:.0%})" if used is not None and window else "未知"
    if context.get("invalidated"):
        ctx = "压缩后待更新"
    elif not context.get("observed_at") and used is not None:
        ctx += "（时间未知）"
    elif context.get("observed_at") and time.time() - context["observed_at"] > 600:
        ctx += "（最近记录）"
    return (
        f"🤖 {data.get('model') or '模型未知'}  ·  🧠 {ctx}  ·  📊 {quota_text(data.get('quota'))}"
    )


def codex_quota():
    from .delivery import cli_command

    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    process = subprocess.Popen(
        cli_command("codex") + ["app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        **options,
    )
    responses = queue.Queue()

    def reader():
        for line in process.stdout:
            try:
                responses.put(json.loads(line))
            except ValueError:
                pass

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()

    def call(rid, method, params):
        process.stdin.write(json.dumps({"id": rid, "method": method, "params": params}) + "\n")
        process.stdin.flush()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            result = responses.get(timeout=max(0.01, deadline - time.monotonic()))
            if result.get("id") == rid:
                if "error" in result:
                    raise RuntimeError(redact(result["error"].get("message", "额度查询失败")))
                return result.get("result", {})
        raise TimeoutError("额度查询超时")

    try:
        call(
            1,
            "initialize",
            {
                "clientInfo": {"name": "bridge_quota", "version": "0.3.0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        process.stdin.write('{"method":"initialized"}\n')
        process.stdin.flush()
        return call(2, "account/rateLimits/read", {"excludeResetCreditDetails": True})
    finally:
        process.stdin.close()
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        process.stdout.close()


def ark_command():
    exe = shutil.which("arkcli")
    if exe and not exe.lower().endswith((".cmd", ".ps1", ".bat")):
        return [exe]
    if os.name == "nt":
        root = Path(os.environ.get("APPDATA", "")) / "npm/node_modules"
        for path in (
            list(root.glob("arkcli/package.json"))
            + list(root.glob("@*/ark-cli/package.json"))
            + list(root.glob("@*/arkcli/package.json"))
        ):
            package = json.loads(path.read_text(encoding="utf-8"))
            binary = package.get("bin")
            entry = binary.get("arkcli") if isinstance(binary, dict) else binary
            node = shutil.which("node")
            if entry and node:
                return [node, str(path.parent / entry)]
    raise RuntimeError("本机未安装可用的 ArkCLI；火山额度未知。")


def ark_quota():
    env = dict(
        os.environ,
        ARKCLI_NO_UPDATE_NOTIFIER="1",
        ARKCLI_CALLER_TYPE="ai_agent",
        ARKCLI_CALLER_NAME="codex",
        ARKCLI_SKILL_NAME="arkcli-usage",
    )
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    for args in (("auth", "status"), ("usage", "plan")):
        result = subprocess.run(
            ark_command() + list(args) + ["--format", "json"],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=25,
            check=False,
            **options,
        )
        if result.returncode:
            # Do not expose any serialized credentials from management commands.
            detail = (
                redact(result.stderr)
                if result.stderr.strip()
                else "管理认证或接口失败，详细原因未返回"
            )
            raise RuntimeError("火山管理查询失败，额度未知。原始错误：" + detail)
        value = json.loads(result.stdout)
    return value


class QuotaPoller:
    def __init__(self):
        self.lock = threading.Lock()
        self.cache = {}
        self.pending = set()

    def snapshot(self, source, refresh=False):
        if source not in ("codex", "ark"):
            return {"source": "unknown", "status": "unsupported", "checked_at": time.time()}
        with self.lock:
            cached = self.cache.get(source)
            if source not in self.pending and (
                refresh or not cached or time.time() - cached["checked_at"] > 180
            ):
                self.pending.add(source)
                threading.Thread(target=self._refresh, args=(source,), daemon=True).start()
            return cached or {"source": source, "status": "pending", "checked_at": time.time()}

    def _refresh(self, source):
        value = {"source": source, "checked_at": time.time()}
        try:
            value.update(status="ok", data=codex_quota() if source == "codex" else ark_quota())
        except (OSError, RuntimeError, ValueError, queue.Empty, subprocess.SubprocessError) as exc:
            value.update(status="error", error=redact(str(exc)))
        with self.lock:
            self.cache[source] = value
            self.pending.discard(source)
