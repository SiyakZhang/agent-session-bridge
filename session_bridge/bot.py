"""DingTalk transport, based on open-dingtalk/dingtalk-stream-sdk-python (MIT)."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time

import dingtalk_stream
import requests

from .formatting import redact

LOG = logging.getLogger(__name__)


class DingAPI:
    def __init__(self, config):
        self.config = config
        self.token = ""
        self.expires = 0
        self.lock = threading.Lock()

    def access_token(self):
        with self.lock:
            if self.expires > time.time():
                return self.token
            response = requests.post(
                "https://api.dingtalk.com/v1.0/oauth2/accessToken",
                json={
                    "appKey": self.config["client_id"],
                    "appSecret": self.config["client_secret"],
                },
                timeout=(8, 15),
            )
            data = response.json()
            if response.status_code != 200 or not data.get("accessToken"):
                raise RuntimeError(
                    "DingTalk authentication failed: " + str(data.get("code", response.status_code))
                )
            self.token = data["accessToken"]
            self.expires = time.time() + data.get("expireIn", 7200) - 120
            return self.token

    def send(self, pair, text):
        payload = {
            "robotCode": self.config.get("robot_code", self.config["client_id"]),
            "msgKey": "sampleMarkdown",
            "msgParam": json.dumps({"title": "会话助手", "text": text}, ensure_ascii=False),
        }
        if pair["kind"] == "2":
            url = "https://api.dingtalk.com/v1.0/robot/groupMessages/send"
            payload["openConversationId"] = pair["chat"]
        else:
            if not pair["staff"]:
                raise RuntimeError("No senderStaffId for private chat")
            url = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
            payload["userIds"] = [pair["staff"]]
        response = requests.post(
            url,
            headers={"x-acs-dingtalk-access-token": self.access_token()},
            json=payload,
            timeout=(8, 20),
        )
        data = response.json()
        if response.status_code not in (200, 201) or data.get("code") or data.get("errcode", 0):
            raise RuntimeError(
                f"DingTalk send rejected HTTP {response.status_code}: "
                + str(data.get("code") or data.get("errcode"))
            )
        return data.get("processQueryKey", "accepted")


class SafeStreamClient(dingtalk_stream.DingTalkStreamClient):
    """Keep official SDK routing/reconnect, add request timeout and avoid logging tickets."""

    def open_connection(self):
        response = requests.post(
            self.OPEN_CONNECTION_API,
            json={
                "clientId": self.credential.client_id,
                "clientSecret": self.credential.client_secret,
                "subscriptions": [
                    {"type": "CALLBACK", "topic": topic} for topic in self.callback_handler_map
                ],
                "ua": "session-bridge/0.1.0",
                "localIp": self.get_host_ip(),
            },
            timeout=(8, 20),
        )
        if response.status_code != 200:
            LOG.warning("Stream handshake rejected: HTTP %s", response.status_code)
            return None
        result = response.json()
        if not result.get("endpoint") or not result.get("ticket"):
            raise RuntimeError("Stream handshake missing endpoint or ticket")
        LOG.info("DingTalk Stream handshake succeeded")
        return result


class Handler(dingtalk_stream.ChatbotHandler):
    def __init__(self, service):
        super().__init__()
        self.service = service

    async def process(self, callback):
        data = callback.data
        LOG.info(
            "Robot callback: type=%s conversation=%s mention=%s",
            data.get("msgtype"),
            data.get("conversationType"),
            data.get("isInAtList"),
        )
        if data.get("msgtype") != "text":
            return dingtalk_stream.AckMessage.STATUS_OK, "text only"
        if str(data.get("conversationType")) == "2" and data.get("isInAtList") is not True:
            return dingtalk_stream.AckMessage.STATUS_OK, "mention required"
        text = data.get("text", {}).get("content", "").strip()
        mid = data.get("msgId") or callback.headers.message_id
        minimal = {
            "text": text,
            "tenant": data.get("senderCorpId") or data.get("chatbotCorpId", ""),
            "sender": data.get("senderId", ""),
            "staff": data.get("senderStaffId", ""),
            "chat": data.get("conversationId", ""),
            "kind": str(data.get("conversationType", "2")),
        }
        if mid and len(text) <= 20000:
            # Fast durable receipt, acknowledge Stream before slow CLI/network work.
            self.service.store.receive(str(mid), minimal)
        return dingtalk_stream.AckMessage.STATUS_OK, "received"


def incoming_worker(service, stop):
    while not stop.is_set():
        row = service.store.incoming()
        if not row:
            stop.wait(0.3)
            continue
        try:
            service.handle(row["id"], json.loads(row["body"]))
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            LOG.error("Incoming command failed: %s", redact(str(exc), service.secret_values))
            data = json.loads(row["body"])
            if service.store.authorized(
                data.get("tenant", ""), data.get("sender", ""), data.get("chat", "")
            ):
                service.enqueue(
                    data["chat"], "处理消息时发生错误，请查看本机日志；没有自动改投其他会话。"
                )
        finally:
            service.store.processed(row["id"])


def outgoing_worker(service, api, stop):
    while not stop.is_set():
        row = service.store.next_outgoing()
        if not row:
            stop.wait(0.3)
            continue
        pair = next((p for p in service.store.pairs() if p["chat"] == row["chat"]), None)
        if not pair:
            service.store.sent(row["id"], "failed")
            continue
        try:
            api.send(pair, row["body"])
            service.store.sent(row["id"], "sent")
        except (requests.Timeout, requests.ConnectionError):
            service.store.sent(row["id"], "unknown")
            LOG.warning("Outgoing message delivery uncertain; not automatically duplicated")
        except (OSError, ValueError, RuntimeError, requests.RequestException) as exc:
            service.store.sent(row["id"], "failed")
            LOG.error("Outgoing message failed: %s", redact(str(exc), service.secret_values))
        stop.wait(0.5)
