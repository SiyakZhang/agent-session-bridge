"""Normalize raw Stream callbacks without discarding quote metadata."""

import json


def object_value(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            result = json.loads(value)
            return result if isinstance(result, dict) else {}
        except ValueError:
            pass
    return {}


def quoted_reply(data):
    containers = [object_value(data.get("text")), object_value(data.get("content")), data]
    for container in containers:
        if (
            container.get("isReplyMsg")
            or container.get("repliedMsg")
            or data.get("originalProcessQueryKey")
            or data.get("originalMsgId")
        ):
            quoted = object_value(container.get("repliedMsg"))
            content = quoted.get("content", {})
            if isinstance(content, dict):
                text = content.get("text") or content.get("content") or ""
            else:
                text = content if isinstance(content, str) else ""
            return {
                "id": str(
                    quoted.get("msgId")
                    or quoted.get("messageId")
                    or data.get("originalMsgId")
                    or ""
                ),
                "process_key": str(
                    data.get("originalProcessQueryKey")
                    or container.get("originalProcessQueryKey")
                    or ""
                ),
                "text": text if isinstance(text, str) else "",
                "from_bot": bool(
                    data.get("chatbotUserId") and quoted.get("senderId") == data["chatbotUserId"]
                ),
            }
    return None


def callback_data(data):
    text = object_value(data.get("text")).get("content", "")
    return {
        "text": text if isinstance(text, str) else "",
        "tenant": data.get("senderCorpId") or data.get("chatbotCorpId", ""),
        "sender": data.get("senderId", ""),
        "staff": data.get("senderStaffId", ""),
        "chat": data.get("conversationId", ""),
        "kind": str(data.get("conversationType", "2")),
        "reply": quoted_reply(data),
        "message_type": data.get("msgtype", ""),
        "open_msg_id": data.get("openMsgId") or "",
        "platform_msg_id": data.get("msgId") or "",
        "robot_code": data.get("robotCode") or "",
    }
