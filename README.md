# Agent Session Bridge

通过聊天继续多台电脑上的 **Codex 和 Claude Code 原会话**。接收任务结果、提问和故障提醒，引用机器人答复即可把后续要求发回同一个会话。当前接入钉钉。

Bridge Codex and Claude Code sessions across machines to your chat app. Get task results, questions, and error alerts, then reply to continue the original session. DingTalk is the first supported integration.

## 功能

- 一个钉钉机器人服务本机及 SSH 节点。引用回复绑定实际的电脑、引擎和会话。
- 正文、表格和代码直接发在聊天里，长答复分段。普通收件和任务状态用原消息上的文字表情。
- 提问和真实选项直接发到聊天里，运行错误会附上脱敏原文。
- 支持查看会话、创建任务和明确控制已连接的回合。额度表情使用对应 Codex 回合上报的 7 天窗口。

## 开始使用

需要 Python 3.10+、已经安装并登录的 Codex 或 Claude Code，以及启用 Stream 模式的钉钉企业内部应用机器人。下面是 Windows 单机起步流程。Linux SSH 节点和后续维护见 [安装指南](docs/setup.md)。

```powershell
git clone https://github.com/SiyakZhang/agent-session-bridge.git
Set-Location agent-session-bridge
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .

$bridgeState = Join-Path $env:USERPROFILE '.dingtalk-session-bridge'
New-Item -ItemType Directory -Force $bridgeState | Out-Null
$bridgeConfig = Join-Path $bridgeState 'config.json'
if (-not (Test-Path -LiteralPath $bridgeConfig)) {
    Copy-Item .\config.example.json $bridgeConfig
}
.\.venv\Scripts\python.exe -m session_bridge.node --state "$bridgeState" --init
```

在私有 `config.json` 中填入机器人 AppKey、AppSecret 和机器人编码。将上一步输出的节点 UUID 写入 `nodes[0].instance`，将 `$bridgeState` 的实际绝对路径写入 `nodes[0].state`。JSON 路径可使用正斜杠，例如 `C:/Users/your-name/.dingtalk-session-bridge`。

```powershell
.\.venv\Scripts\session-bridge.exe doctor
.\.venv\Scripts\session-bridge.exe run
```

在另一个终端运行 `session-bridge pair`（使用同一虚拟环境），把输出的 `/pair` 命令发到目标钉钉会话。群聊里先 @ 机器人。配对后，可以用 `！任务` 查看会话，或用 `/help all` 查看命令。

## 常用操作

| 输入 | 用途 |
| --- | --- |
| 引用机器人答复并输入文字 | 继续对应会话 |
| `！任务` / `！电脑` | 列出会话或节点 |
| `/send 会话标识 正文` | 明确指定会话 |
| `windowscodex！新建` | 列出 Windows 节点的常用项目目录 |
| 引用题目并回复 `2`、`1,3` 或文字 | 根据题目提示作答，`1,3` 适用于多选题 |
| 引用任务回复 `！额度` | 查看对应会话的额度和重置时间 |
| `/help all` | 查看详细命令与连接限制 |

**共享 Codex** 可以把答案直接提交给原生提问，也支持停止和压缩。**独立桌面 Codex** 的异步追问会把原题和答案一起送回同一会话。原会话忙时答案先排队，确认开始处理后，更新你原消息上的表情。独立桌面上的阻塞题由原电脑处理。Claude Code 桌面连接能转发原题，阻塞题在原电脑作答。

可选的完整历史页需要手机能够访问网关。钉钉聊天中的普通结果直接显示正文。

## 开发

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
```

项目实现和文档见 [安装指南](docs/setup.md) 与 [第三方说明](THIRD_PARTY_NOTICES.md)。运行配置、凭证、聊天数据库和原始日志存放在仓库外的私有状态目录。
