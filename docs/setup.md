# 安装与维护

Windows 网关收发钉钉消息，节点以原桌面用户身份读写各自的 AI 会话。启动前先在节点上安装并登录相应的 CLI。

## 钉钉配置

在钉钉开放平台创建企业内部应用，添加机器人能力并选择 Stream 接收方式，为应用开通企业内机器人发送消息权限。将应用发布到自己的使用范围，再把机器人加入目标群。用 AppKey、AppSecret 和机器人编码填充仓库模板复制出来的私有配置。

配置默认位于 `~/.dingtalk-session-bridge/config.json`。可通过 `SESSION_BRIDGE_HOME` 指定其他状态目录。使用同一状态目录运行 `doctor`、`run`、`pair` 和 `status`。

- `doctor` 检查钉钉凭证和已配置节点。
- `status` 查看网关心跳、Stream 连接和节点状态。
- `pair` 生成一次性配对命令。配对绑定发命令时的账号和聊天。

## 接入 Linux 节点

在 Linux 上用运行桌面 AI 会话的用户克隆同一仓库，并准备 Python 3.10+ 环境。下面使用示例目录。网关配置中的远程路径应使用实际绝对路径。

```bash
git clone https://github.com/SiyakZhang/agent-session-bridge.git
cd agent-session-bridge
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m session_bridge.node --state "$HOME/.dingtalk-session-bridge" --init
```

将下列对象加入 **Windows 网关私有配置**的 `nodes` 数组，替换所有示例值。节点 UUID 取自上条命令，`known_hosts` 使用已经核实指纹的 SSH 主机公钥，`key_file` 指向网关上获准登录这个节点的本地 SSH 私钥。

```json
{
  "id": "linux",
  "instance": "REPLACE_WITH_REMOTE_NODE_UUID",
  "host": "worker.example.org",
  "port": 22,
  "user": "bridge-user",
  "directory": "/home/bridge-user/agent-session-bridge",
  "python": "/home/bridge-user/agent-session-bridge/.venv/bin/python",
  "state": "/home/bridge-user/.dingtalk-session-bridge",
  "known_hosts": "C:/path/to/verified_known_hosts",
  "key_file": "C:/path/to/ssh_key"
}
```

重启网关后，SSH 连接会按需启动远程 worker。远程 worker 需要与网关使用兼容的仓库版本以及相同的节点标识。

## 观察 Claude Code 生命周期

在运行 Claude Code 的节点执行 `python -m session_bridge.setup_hooks`。该命令保留其他 Hook，并备份已有设置。运行中的 Claude Code 是否立即加载 Hook 取决于其版本。Hook 观察提问和结束等事件，桌面阻塞题由桌面端处理。

## Windows 自启动与诊断

在完成一次手动运行和配对后，可使用仓库里的 `scripts/install-startup.ps1` 注册当前用户登录时的计划任务。使用 `scripts/status.ps1` 查看状态，`scripts/stop.ps1` 停止本仓库的网关进程。运行日志在私有状态目录内。

如要打开可选历史页，可在私有配置中启用 `viewer`，并填写手机可达的 `viewer.public_url`。页面用有时限的访问链接打开。默认监听回环地址，钉钉普通正文可以直接在聊天中读取。
