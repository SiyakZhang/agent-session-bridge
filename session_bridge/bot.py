"""DingTalk transport, based on open-dingtalk/dingtalk-stream-sdk-python (MIT)."""

from __future__ import annotations

import base64
import json
import logging
import sqlite3
import threading
import time

import dingtalk_stream
import requests

from .dingtalk_messages import callback_data
from .formatting import redact
from .state import MODEL_REACTION_PREFIX, SUMMARY_EMOTION_BACKGROUND, SUMMARY_EMOTION_ID

LOG = logging.getLogger(__name__)


class RateLimited(RuntimeError):
    """Explicit platform rejection; retrying this request cannot duplicate acceptance."""


class ReactionRejected(RuntimeError):
    def __init__(self, code):
        self.code = str(code)
        super().__init__("Reaction rejected: " + self.code)


class SendUncertain(RuntimeError):
    """Server failure after a write, acceptance cannot be inferred."""


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

    def send(self, pair, text, attachment=None):
        plain = bool((attachment or {}).get("plain_text"))
        # Presentation metadata must never be interpreted as an image attachment.
        if attachment and not (attachment.get("image_base64") or attachment.get("media_id")):
            attachment = None
        payload = {
            "robotCode": self.config.get("robot_code", self.config["client_id"]),
            "msgKey": "sampleText" if plain else "sampleMarkdown",
            "msgParam": json.dumps(
                {"content": text} if plain else {"title": "会话助手", "text": text},
                ensure_ascii=False,
            ),
        }
        if attachment:
            media = attachment.get("media_id") or self.upload_image(attachment)
            payload["msgKey"] = "sampleImageMsg"
            payload["msgParam"] = json.dumps({"photoURL": media})
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
        if response.status_code == 429:
            raise RateLimited("HTTP 429")
        if response.status_code >= 500:
            raise SendUncertain("DingTalk server error after send")
        try:
            data = response.json()
        except ValueError:
            if 200 <= response.status_code < 300:
                raise SendUncertain("Unparseable success response") from None
            raise RuntimeError(
                "DingTalk rejected request HTTP " + str(response.status_code)
            ) from None
        code = str(data.get("code") or data.get("errcode") or "")
        if response.status_code == 429 or code in ("90018", "90002") or "QpsLimit" in code:
            raise RateLimited(code or "HTTP 429")
        if response.status_code >= 500:
            raise SendUncertain("DingTalk server error after send")
        if response.status_code not in (200, 201) or data.get("code") or data.get("errcode", 0):
            raise RuntimeError(
                f"DingTalk send rejected HTTP {response.status_code}: "
                + str(data.get("code") or data.get("errcode"))
            )
        receipt = {
            k: data[k]
            for k in ("processQueryKey", "msgId", "messageId", "openMessageId", "openMsgId")
            if data.get(k)
        }
        if plain and not receipt:
            raise SendUncertain("Question send returned no message receipt")
        return receipt

    def react(self, row, state, recall=False):
        if row.get("metric") and not row.get("emotion_id"):
            raise RuntimeError("尚未配置经过验证的文字表情ID")
        labels = {
            "received": "收到",
            "queued": "排队",
            "processing": "处理中",
            "waiting": "待答",
            "completed": "完成",
            "failed": "异常",
            "stopping": "停止中",
            "stopped": "已停",
            "compacting": "压缩中",
            "compacted": "已压缩",
        }
        icons = {
            "received": "👌",
            "queued": "⏳",
            "processing": "🤔",
            "waiting": "❓",
            "completed": "✅",
            "failed": "⚠️",
            "stopping": "⏹️",
            "stopped": "⏹️",
            "compacting": "🗜️",
            "compacted": "🗜️",
        }
        if row.get("metric"):
            label = state
        elif state.startswith(MODEL_REACTION_PREFIX):
            label = state[len(MODEL_REACTION_PREFIX) :]
        else:
            label = icons.get(state, "🤔") + labels.get(state, "思考中")
        emotion_id = row.get("emotion_id") or "2659900"
        background_id = (
            SUMMARY_EMOTION_BACKGROUND if emotion_id == SUMMARY_EMOTION_ID else "im_bg_1"
        )
        response = requests.post(
            "https://api.dingtalk.com/v1.0/robot/emotion/" + ("recall" if recall else "reply"),
            headers={"x-acs-dingtalk-access-token": self.access_token()},
            json={
                "robotCode": row["robot"]
                or self.config.get("robot_code", self.config["client_id"]),
                "openMsgId": row["open_id"],
                "openConversationId": row["chat"],
                "emotionType": 2,
                "emotionName": label,
                "textEmotion": {
                    "emotionId": emotion_id,
                    "emotionName": label,
                    "text": label,
                    "backgroundId": background_id,
                },
            },
            timeout=(3, 5),
        )
        data = response.json()
        if (
            response.status_code >= 400
            or data.get("code")
            or data.get("errcode")
            or data.get("success") is not True
        ):
            raise ReactionRejected(data.get("code") or data.get("errcode") or response.status_code)

    def upload_image(self, attachment):
        raw = base64.b64decode(attachment["image_base64"], validate=True)
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError("Image too large")
        response = requests.post(
            "https://oapi.dingtalk.com/media/upload",
            params={"access_token": self.access_token()},
            data={"type": "image"},
            files={"media": ("image.jpg", raw, "image/jpeg")},
            timeout=(8, 30),
        )
        if response.status_code == 429:
            raise RateLimited("Media upload throttled")
        data = response.json()
        if response.status_code != 200 or not data.get("media_id"):
            raise RuntimeError("图片上传失败：" + str(data.get("errcode", response.status_code)))
        return data["media_id"]


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
                "ua": "session-bridge/0.3.7",
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
        if str(data.get("conversationType")) == "2" and data.get("isInAtList") is not True:
            return dingtalk_stream.AckMessage.STATUS_OK, "mention required"
        mid = data.get("msgId") or callback.headers.message_id
        minimal = callback_data(data)
        if mid and len(minimal["text"]) <= 20000:
            # Fast durable receipt, acknowledge Stream before slow CLI/network work.
            inbox_id = self.service.store.message_key(minimal, str(mid))
            if self.service.store.receive(inbox_id, minimal) and self.service.store.authorized(
                minimal["tenant"], minimal["sender"], minimal["chat"]
            ):
                self.service.store.reaction(
                    self.service.store.message_key(minimal, inbox_id), "received", minimal
                )
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
                service.store.reaction(service.store.message_key(data, row["id"]), "failed")
                service.enqueue(
                    data["chat"],
                    "处理消息失败，未自动重发。\n原始错误："
                    + redact(str(exc), service.secret_values),
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
            attachment = json.loads(row["payload"]) if row.get("payload") else None
            receipt = api.send(pair, row["body"], attachment)
            with service.store.transaction():
                service.store.link_sent(row["id"], receipt)
                service.store.sent(row["id"], "sent")
        except RateLimited as exc:
            if row["attempts"] >= 7:
                service.store.sent(row["id"], "failed")
                LOG.error("Outgoing throttled repeatedly; retained for inspection")
            else:
                service.store.retry_outgoing(
                    row["id"], min(60, 2 ** (row["attempts"] + 1)), str(exc)
                )
        except (requests.Timeout, requests.ConnectionError, SendUncertain):
            service.store.sent(row["id"], "unknown")
            LOG.warning("Outgoing message delivery uncertain; not automatically duplicated")
        except (OSError, ValueError, RuntimeError, requests.RequestException) as exc:
            service.store.sent(row["id"], "failed")
            LOG.error("Outgoing message failed: %s", redact(str(exc), service.secret_values))
        stop.wait(0.5)


def reaction_worker(service, api, stop):
    while not stop.wait(0.3):
        row = service.store.pending_reaction()
        if not row:
            continue
        try:
            if row["sent_state"]:
                api.react(row, row["sent_state"], recall=True)
                service.store.reaction_recalled(row["id"], row["sent_state"])
            api.react(row, row["state"])
        except (OSError, ValueError, RuntimeError, requests.RequestException) as exc:
            if (
                isinstance(exc, ReactionRejected)
                and exc.code == "system.err"
                and row["metric"] == 4
                and len(row["state"]) > 8
                and not row["sent_state"]
            ):
                model, separator, readings = row["state"].rpartition(" ")
                if separator and 0 < len(model) <= 8 and "/" in readings:
                    service.store.shorten_summary(row, model)
                    continue
            LOG.info(
                "Reaction unavailable; conversation delivery continues: %s",
                redact(str(exc), service.secret_values),
            )
            service.store.reaction_failed(row, redact(str(exc), service.secret_values))
        else:
            service.store.reacted(row["id"], row["state"])
