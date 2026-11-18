"""One registry for Chinese/English command parsing and honest help output."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Command:
    name: str
    chinese: str
    description: str
    scope: str = "task"
    capability: str = ""


COMMANDS = (
    Command("help", "帮助", "完整日常用法；all 追加详细参数和限制", "both"),
    Command("list", "任务", "查看活动任务；all 包含历史记录", "global"),
    Command("machines", "电脑", "电脑状态和新建启动入口", "global"),
    Command("quota", "额度", "查询官方 Codex / 火山套餐额度快照", "both"),
    Command("status", "状态", "当前任务状态"),
    Command("info", "详情", "机器、项目、模型及完整会话标识"),
    Command("history", "历史", "最近记录；可带条数"),
    Command("session", "记录", "查看完整会话", capability="history"),
    Command("name", "命名", "修改原会话标题", capability="name"),
    Command("watch", "提醒", "开启完成提醒"),
    Command("unwatch", "静音", "关闭完成提醒"),
    Command("say", "发送", "将后面的内容作为普通文字发送"),
    Command("stop", "停止", "中断当前轮，暂停待发队列", capability="stop"),
    Command("add", "补充", "给当前执行补充要求", capability="add"),
    Command("continue", "继续", "恢复桥接队列或继续任务", capability="continue"),
    Command("queue", "队列", "查看待发输入；remove 标识 删除", capability="queue"),
    Command("model", "模型", "查看或选择模型", capability="model"),
    Command("effort", "强度", "查看或设置推理强度", capability="effort"),
    Command("compact", "压缩", "压缩当前上下文", capability="compact"),
    Command("clear", "清理", "清空上下文；保留项目文件和旧历史", capability="clear"),
    Command("new", "新建", "任务内清理上下文；启动入口新建窗口", "both", "clear"),
    Command("screen", "截图", "获取任务电脑当前桌面", capability="screen"),
    Command("image", "图片", "查看项目内图片文件", capability="image"),
    Command("file", "文件", "查看项目内文件", capability="file"),
    Command("diff", "改动", "查看项目 Git 改动", capability="diff"),
    Command("close", "关闭", "关闭会话并保留历史", capability="close"),
    Command("resume", "恢复", "恢复已关闭会话", capability="resume"),
)
BY_NAME = {c.name: c for c in COMMANDS}
ALIASES = {c.chinese: c.name for c in COMMANDS}
ALIASES.update(
    {
        "sessions": "list",
        "nodes": "machines",
        "show": "info",
        "start": "help",
        "新对话": "new",
        "记录": "session",
        "停止提醒": "unwatch",
    }
)
MARKERS = "/／！!"


def parse_command(text):
    """Only normalize command syntax, never user text or arguments."""
    text = text.lstrip()
    if not text or text[0] not in MARKERS:
        return None
    tokens = text[1:].lstrip().split(maxsplit=1)
    if not tokens:
        return "help", ""
    head, *tail = tokens
    return ALIASES.get(head.lower(), head.lower()), tail[0] if tail else ""


def split_target(text, codes):
    text = text.lstrip()
    # Reserved extension grammar: 松·a·. Never fall back to 松 for malformed/unknown extensions.
    if len(text) > 1 and text[1] == "·":
        end = text.find("·", 2)
        if end < 0 or text[: end + 1] not in codes:
            raise ValueError("未知或不完整的扩展代号，请用 ！任务 查看。")
        return text[: end + 1], text[end + 1 :].lstrip()
    for code in sorted(codes, key=len, reverse=True):
        if text.startswith(code):
            rest = text[len(code) :]
            # Latin codes can abut Chinese/punctuation, but not another ASCII word.
            if code.isascii() and rest and rest[0].isascii() and rest[0].isalnum():
                continue
            return code, rest.lstrip()
    return None, text


def help_text(code=None, capabilities=(), full=False):
    lines = [
        f"{code}｜可用命令" if code else "会话助手 · 日常用法",
        (
            "回复对应会话的机器人消息直接说话；也可用 `/send 会话标识 正文`。标题见 ！任务，旧代号继续有效。\n"
            "普通收件只贴原消息表情；失败、中断、待回答问题会单独提示。群内仍需 @机器人。\n"
            "`/`、`／`、`！`、`!` 等价，命令支持中英文。"
        ),
    ]
    for c in COMMANDS:
        if code and c.scope == "global":
            continue
        available = not c.capability or c.capability in capabilities
        if code and not full and not available:
            continue
        suffix = "" if available or not code else "（当前连接不支持）"
        prefix = code or ("〈会话标识〉" if c.scope == "task" else "")
        lines.append(f"`{prefix}！{c.chinese}` / `/{c.name}`：{c.description}{suffix}")
    if code:
        unavailable = [
            c.chinese for c in COMMANDS if c.capability and c.capability not in capabilities
        ]
        if unavailable:
            lines.append("当前连接未开放：" + "、".join(unavailable) + "。不会当普通文字假执行。")
    else:
        lines.append(
            "`windowscodex /new`：显示常用目录选择；`windowscodex /new D:\\Code`：直接在指定目录创建。Linux 使用对应电脑启动入口，目录可含空格。入口见 ！电脑。"
        )
    lines.append(
        "原生问题请回复那条问题消息：单选 1/2/3，多选可用 1,3，无选项直接输入；多道题逐题回答。过期答案不会发给新问题。"
    )
    lines.append(
        "消息末尾：🤖 实际模型 · 🧠 最近上下文占用 · 📊 当前服务额度；未知、过期、查询失败会明确标注。"
    )
    if full:
        lines.extend(
            [
                "队列：`！队列` 查看稳定标识，`！队列 remove 标识` 撤回尚未提交项；`！继续` 明确恢复。故障后不会自动重跑。",
                "压缩：`！压缩` 在原连接支持且空闲时发起；请求接受、压缩中、完成、失败分别反馈，完成后可继续说话。",
                "历史：`！历史 10` 查看最近 10 条，`！记录` 获取完整可见记录页（需手机可达电脑网络）；截图/文件/图片/diff 依各连接实际能力。",
                "401、余额/套餐耗尽、网络/渠道故障会转发脱敏原文；服务恢复后你可自行发新要求。状态未知不盲目重发。",
                "额度：官方套餐与 credits、OpenAI API 货币余额、火山套餐和中转余额互相独立。credits=0 不代表套餐耗尽；火山管理查询需要有效 SSO，普通推理 Key 不一定具备权限。",
                "旧桌面会话没有安全控制通道时，会提示回电脑；不会模拟键盘、夺取会话或更改权限。清理/关闭/恢复仅在原生适配支持时开放。",
                "卡片不可用时保留富文本/数字回答；共享接口额度耗尽或网关完全断网时，无法保证当时送达，连接恢复后补报仍有意义的故障。",
            ]
        )
    return "\n\n".join(lines)
