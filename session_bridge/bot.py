"""DingTalk transport, based on open-dingtalk/dingtalk-stream-sdk-python (MIT)."""

from __future__ import annotations

import base64
import json
import logging
import sqlite3
import threading
import time
import uuid

import dingtalk_stream
import requests

from .dingtalk_messages import callback_data
from .formatting import redact

LOG = logging.getLogger(__name__)


class RateLimited(RuntimeError):
    """Explicit platform rejection; retrying this request cannot duplicate acceptance."""


class SendUncertain(RuntimeError):
    """Server failure after a write, acceptance cannot be inferred."""


class DingAPI:
    def __init__(self, config):
        self.config = config
        self.token = ""
        self.expires = 0
        self.lock = threading.Lock()
        self.cards_disabled_until = 0

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
        if attachment and attachment.get("card"):
            if time.time() >= self.cards_disabled_until and self.config.get("cards", True):
                try:
                    return self.send_card(pair, text, attachment.get("choice"))
                except RateLimited:
                    self.cards_disabled_until = time.time() + 300
                except CardRejected as exc:
                    self.cards_disabled_until = time.time() + 3600
                    LOG.info("Card unavailable, using Markdown: %s", str(exc))
            attachment = None
        payload = {
            "robotCode": self.config.get("robot_code", self.config["client_id"]),
            "msgKey": "sampleMarkdown",
            "msgParam": json.dumps({"title": "会话助手", "text": text}, ensure_ascii=False),
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
        return {
            k: data[k]
            for k in ("processQueryKey", "msgId", "messageId", "openMessageId")
            if data.get(k)
        }

    def send_card(self, pair, text, choice=None):
        if self.config.get("card_template_id"):
            return self.send_advanced_card(pair, text, choice)
        # StandardCard needs no user-created template. Numbered questions remain answerable by reply.
        card_id = "bridge-" + uuid.uuid4().hex
        body = {
            "cardTemplateId": "StandardCard",
            "cardBizId": card_id,
            "robotCode": self.config.get("robot_code", self.config["client_id"]),
            "cardData": json.dumps(
                {
                    "config": {"autoLayout": True, "enableForward": False},
                    "header": {"title": {"type": "text", "text": ""}},
                    "contents": [{"type": "markdown", "text": text, "id": "body"}],
                },
                ensure_ascii=False,
            ),
            "pullStrategy": False,
        }
        if pair["kind"] == "2":
            body["openConversationId"] = pair["chat"]
        else:
            body["singleChatReceiver"] = json.dumps({"userId": pair["staff"]})
        response = requests.post(
            "https://api.dingtalk.com/v1.0/im/v1.0/robot/interactiveCards/send",
            headers={"x-acs-dingtalk-access-token": self.access_token()},
            json=body,
            timeout=(8, 20),
        )
        if response.status_code >= 500:
            raise SendUncertain("Card delivery uncertain after server error")
        if response.status_code == 429:
            raise RateLimited("Card HTTP 429")
        try:
            data = response.json()
        except ValueError:
            if response.status_code < 300:
                raise SendUncertain("Card response could not be parsed") from None
            raise CardRejected("Card HTTP " + str(response.status_code)) from None
        if (
            response.status_code >= 400
            or data.get("code")
            or data.get("errcode")
            or data.get("success") is False
        ):
            raise CardRejected(str(data.get("code") or data.get("errcode") or response.status_code))
        result = data.get("result") or data
        receipt = {
            k: result[k]
            for k in ("processQueryKey", "msgId", "messageId", "openMessageId")
            if result.get(k)
        }
        receipt["cardBizId"] = card_id
        # Without a platform identifier a quoted numeric answer cannot be bound reliably.
        # Keep the card itself as the delivery; do not send a duplicate text on ambiguous success.
        return receipt

    def send_advanced_card(self, pair, text, choice):
        track_id = "bridge-" + uuid.uuid4().hex
        questions = (choice or {}).get("questions") or []
        options = (
            questions[(choice or {}).get("question_index", 0)].get("options", [])
            if questions
            else (choice or {}).get("options", [])
        )
        params = {
            "body": text,
            "answered": "false",
            "options": json.dumps(
                [{"label": o["label"], "answer": str(i + 1)} for i, o in enumerate(options)],
                ensure_ascii=False,
            ),
        }
        body = {
            "cardTemplateId": self.config["card_template_id"],
            "outTrackId": track_id,
            "cardData": {"cardParamMap": params},
            "callbackType": "STREAM",
            "userIdType": 1,
        }
        if pair["kind"] == "2":
            body.update(
                openSpaceId="dtv1.card//IM_GROUP." + pair["chat"],
                imGroupOpenDeliverModel={
                    "robotCode": self.config.get("robot_code", self.config["client_id"])
                },
            )
        else:
            body.update(
                openSpaceId="dtv1.card//IM_ROBOT." + pair["staff"],
                imRobotOpenDeliverModel={
                    "spaceType": "IM_ROBOT",
                    "robotCode": self.config.get("robot_code", self.config["client_id"]),
                },
            )
        response = requests.post(
            "https://api.dingtalk.com/v1.0/card/instances/createAndDeliver",
            headers={"x-acs-dingtalk-access-token": self.access_token()},
            json=body,
            timeout=(8, 20),
        )
        if response.status_code >= 500:
            raise SendUncertain("Advanced card delivery uncertain")
        if response.status_code == 429:
            raise RateLimited("Advanced card HTTP 429")
        try:
            data = response.json()
        except ValueError:
            if response.status_code < 300:
                raise SendUncertain("Advanced card success response could not be parsed") from None
            raise CardRejected("Advanced card HTTP " + str(response.status_code)) from None
        result = data.get("result") or data
        outcomes = result.get("deliverResults", [])
        if any(r.get("success") is True for r in outcomes) and any(
            r.get("success") is False for r in outcomes
        ):
            raise SendUncertain("Advanced card partially delivered")
        if (
            response.status_code >= 400
            or data.get("code")
            or data.get("success") is False
            or result.get("success") is False
            or any(r.get("success") is False for r in result.get("deliverResults", []))
        ):
            raise CardRejected(str(data.get("code") or response.status_code))
        return dict(
            {
                k: result[k]
                for k in ("processQueryKey", "msgId", "messageId", "openMessageId")
                if result.get(k)
            },
            outTrackId=track_id,
        )

    def update_card(self, row):
        if row["advanced"]:
            url = "https://api.dingtalk.com/v1.0/card/instances"
            body = {
                "outTrackId": row["card_id"],
                "cardData": {
                    "cardParamMap": {"body": row["body"], "answered": "true", "options": "[]"}
                },
                "cardUpdateOptions": {"updateCardDataByKey": True},
            }
        else:
            url = "https://api.dingtalk.com/v1.0/im/robots/interactiveCards"
            body = {
                "cardBizId": row["card_id"],
                "cardData": json.dumps(
                    {
                        "config": {"autoLayout": True, "enableForward": False},
                        "contents": [{"type": "markdown", "text": row["body"], "id": "body"}],
                    },
                    ensure_ascii=False,
                ),
            }
        response = requests.put(
            url,
            headers={"x-acs-dingtalk-access-token": self.access_token()},
            json=body,
            timeout=(3, 8),
        )
        data = response.json()
        if response.status_code >= 400 or data.get("code") or data.get("success") is False:
            raise RuntimeError(
                "Card update rejected: " + str(data.get("code") or response.status_code)
            )

    def react(self, row, state, recall=False):
        labels = {
            "received": "收到",
            "queued": "排队中",
            "processing": "思考中",
            "waiting": "等你回答",
            "completed": "已完成",
            "failed": "遇到问题",
        }
        icons = {
            "received": "👌",
            "queued": "⏳",
            "processing": "🤔",
            "waiting": "❓",
            "completed": "✅",
            "failed": "⚠️",
        }
        label = icons.get(state, "🤔") + labels.get(state, "思考中")
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
                    "emotionId": "2659900",
                    "emotionName": label,
                    "text": label,
                    "backgroundId": "im_bg_1",
                },
            },
            timeout=(3, 5),
        )
        data = response.json()
        if response.status_code >= 400 or data.get("code") or data.get("errcode"):
            raise RuntimeError(
                "Reaction rejected: "
                + str(data.get("code") or data.get("errcode") or response.status_code)
            )

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
                "ua": "session-bridge/0.3.0",
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


class CardHandler(dingtalk_stream.CallbackHandler):
    def __init__(self, service):
        super().__init__()
        self.service = service

    async def process(self, callback):
        data = callback.data
        from .dingtalk_messages import object_value

        content = object_value(data.get("content"))
        params = object_value(content.get("cardPrivateData")).get("params", {})
        actor = self.service.store.card_callback_actor(
            str(data.get("outTrackId") or content.get("outTrackId") or ""),
            str(data.get("userId") or ""),
        )
        answer = params.get("answer") if isinstance(params, dict) else None
        if actor and isinstance(answer, str) and 0 < len(answer) <= 16000:
            actor.update(text=answer, message_type="text")
            mid = str(callback.headers.message_id)
            self.service.store.receive(self.service.store.message_key(actor, "card:" + mid), actor)
        return dingtalk_stream.AckMessage.STATUS_OK, {"status": "received"}


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
        update = service.store.next_card_update()
        if update:
            try:
                api.update_card(update)
            except (OSError, ValueError, RuntimeError, requests.RequestException) as exc:
                LOG.info(
                    "Card state update unavailable: %s", redact(str(exc), service.secret_values)
                )
            service.store.card_updated(update["card_id"])
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
            if attachment and attachment.get("card") and row.get("question_id"):
                choice = service.store.get("choice:" + row["question_id"], {})
                attachment["choice"] = choice
                if not choice or choice.get("expires", 0) < time.time():
                    row["body"] += "\n\n此问题已处理或失效，旧选项不可再次提交。"
                    with service.store.transaction():
                        service.store.db.execute(
                            "UPDATE outbox SET body=? WHERE id=?", (row["body"], row["id"])
                        )
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


class CardRejected(RuntimeError):
    """Explicit card rejection: safe to use the text fallback."""


def reaction_worker(service, api, stop):
    while not stop.wait(0.3):
        row = service.store.pending_reaction()
        if not row:
            continue
        try:
            if row["sent_state"]:
                api.react(row, row["sent_state"], recall=True)
            api.react(row, row["state"])
        except (OSError, ValueError, RuntimeError, requests.RequestException) as exc:
            LOG.info(
                "Reaction unavailable; conversation delivery continues: %s",
                redact(str(exc), service.secret_values),
            )
        # Best effort; never send another message or repeatedly hammer a rejected reaction.
        service.store.reacted(row["id"], row["state"])
