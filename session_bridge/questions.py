"""Shared, deterministic question rendering/validation, Python 3.8 compatible."""

import math
import re

NUMBERED_ANSWER = re.compile(r"\d+(?:(?:\s+|\s*[,，、]\s*)\d+)*")


def is_numbered_answer(text):
    return NUMBERED_ANSWER.fullmatch(text.strip()) is not None


def normalize_questions(questions):
    result = []
    for i, q in enumerate(questions or []):
        result.append(
            dict(
                q,
                id=q.get("id") or q.get("question") or str(i),
                question=q.get("question") or q.get("title") or "请补充",
                options=[
                    ({"label": option} if isinstance(option, str) else option)
                    for option in (q.get("options") or [])
                ],
            )
        )
    return result


def question_text(choice):
    questions = choice.get("questions") or []
    if not questions:
        options = choice.get("options") or []
        lines = [
            f"{i + 1}. {o['label']}" + (" — " + o["description"] if o.get("description") else "")
            for i, o in enumerate(options)
        ]
        if choice.get("remote") is False:
            lines.append("请在原电脑处理。")
        elif options:
            lines.append("引用这条消息，回复选项数字。")
        return "\n\n".join(lines)
    index = choice.get("question_index", 0)
    q = questions[index]
    lines = [(f"问题 {index + 1}/{len(questions)}：" if len(questions) > 1 else "") + q["question"]]
    if q.get("header") and q["header"] != q["question"]:
        lines.insert(0, q["header"])
    for i, option in enumerate(q.get("options") or []):
        lines.append(
            f"{i + 1}. {option['label']}"
            + (" — " + option["description"] if option.get("description") else "")
        )
    if q.get("isSecret"):
        lines.append("此题涉及敏感输入，请回原电脑填写。")
    elif choice.get("remote") is False and choice.get("async") is not True:
        lines.append("请在原电脑回答。")
    elif not q.get("options"):
        lines.append("引用这条消息，直接输入答案。")
    else:
        lines.append(
            "引用这条消息，可回复多个选项数字，例如 1,3。"
            if q.get("multiSelect")
            else "引用这条消息，回复选项数字。"
            if q.get("isOther") is False
            else "引用这条消息，回复选项数字或直接输入答案。"
        )
    if choice.get("async") and choice.get("remote") is False and not q.get("isSecret"):
        lines.append("原会话忙时，答案会排队。")
    return "\n\n".join(lines)


def parse_answer(question, text):
    if question.get("isSecret"):
        raise ValueError("敏感输入请回原电脑填写，未通过聊天提交。")
    value = text.strip()
    if not value:
        raise ValueError("请输入答案。")
    options = question.get("options") or []
    if options and is_numbered_answer(value):
        indexes = list(dict.fromkeys(int(x) - 1 for x in re.split(r"[,，、\s]+", value)))
        if any(x < 0 or x >= len(options) for x in indexes):
            raise ValueError("选项编号超出范围。")
        if len(indexes) > 1 and not question.get("multiSelect"):
            raise ValueError("此题为单选，请回复一个数字。")
        return [options[x]["label"] for x in indexes]
    # Newer protocol versions accept free text; explicitly closed schemas remain closed.
    if options and question.get("isOther") is False and value not in [x["label"] for x in options]:
        raise ValueError("此题只接受列出的选项，请回复对应数字。")
    return [value]


def validate_answers(questions, answers):
    if set(answers) != {q["id"] for q in questions}:
        raise ValueError("答案与原问题不匹配。")
    for q in questions:
        values = answers[q["id"]].get("answers")
        if (
            not isinstance(values, list)
            or not values
            or not all(isinstance(v, str) and v.strip() for v in values)
        ):
            raise ValueError("原生答案格式无效。")
        if len(values) > 1 and not q.get("multiSelect"):
            raise ValueError("单选问题不能提交多个答案。")
        if q.get("isSecret"):
            raise ValueError("敏感输入请回原电脑填写，未通过聊天提交。")
        # These values have already been resolved from chat positions to native labels.
        options = {option["label"] for option in (q.get("options") or [])}
        if options and q.get("isOther") is False and any(value not in options for value in values):
            raise ValueError("此题只接受列出的选项，请回复对应数字。")


def elicitation_questions(params):
    if params.get("mode") != "form":
        return None
    schema = params.get("requestedSchema") or {}
    questions = []
    for key, prop in schema.get("properties", {}).items():
        kind = prop.get("type")
        if kind not in ("string", "number", "integer", "boolean", "array"):
            return None
        values = prop.get("items", {}) if kind == "array" else prop
        choices = values.get("oneOf") or values.get("anyOf") or []
        enum = values.get("enum") or []
        options = [
            {"label": o.get("title") or str(o["const"]), "value": o["const"]} for o in choices
        ]
        if enum:
            names = values.get("enumNames") or enum
            options = [{"label": str(names[i]), "value": value} for i, value in enumerate(enum)]
        if kind == "boolean":
            options = [{"label": "是", "value": True}, {"label": "否", "value": False}]
        if kind == "array" and not options:
            return None
        questions.append(
            {
                "id": key,
                "question": str(prop.get("title") or key)
                + ("\n" + prop["description"] if prop.get("description") else ""),
                "options": options,
                "isOther": not bool(options),
                "multiSelect": kind == "array",
                "isSecret": prop.get("format") == "password" or bool(prop.get("writeOnly")),
            }
        )
    return questions or None


def elicitation_content(params, answers):
    questions = elicitation_questions(params)
    if not questions:
        raise ValueError("此表单需回电脑填写。")
    validate_answers(questions, answers)
    properties = params["requestedSchema"]["properties"]
    result = {}
    for q in questions:
        prop = properties[q["id"]]
        values = answers[q["id"]]["answers"]
        mapping = {o["label"]: o["value"] for o in q["options"]}
        values = [mapping[v] if mapping else v for v in values]
        kind = prop["type"]
        value = values if kind == "array" else values[0]
        try:
            if kind == "integer":
                value = int(value)
            elif kind == "number":
                value = float(value)
                if not math.isfinite(value):
                    raise ValueError("数字必须为有限值。")
        except (TypeError, ValueError):
            raise ValueError("数字格式无效，请重新填写。") from None
        if kind in ("integer", "number") and (
            (prop.get("minimum") is not None and value < prop["minimum"])
            or (prop.get("maximum") is not None and value > prop["maximum"])
        ):
            raise ValueError("数字不在原表单允许范围内。")
        if kind == "string" and (
            (prop.get("minLength") is not None and len(value) < prop["minLength"])
            or (prop.get("maxLength") is not None and len(value) > prop["maxLength"])
        ):
            raise ValueError("文本长度不在原表单允许范围内。")
        if kind == "array" and (
            (prop.get("minItems") is not None and len(value) < prop["minItems"])
            or (prop.get("maxItems") is not None and len(value) > prop["maxItems"])
        ):
            raise ValueError("选择数量不在原表单允许范围内。")
        result[q["id"]] = value
    return result
