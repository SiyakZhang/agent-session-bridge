from __future__ import annotations

import argparse
import json
import logging
import os
import threading
import time
from logging.handlers import RotatingFileHandler

from . import __version__
from .config import load_config, state_dir
from .state import Store


class SingleInstance:
    def __init__(self, path):
        self.file = open(path, "a+b")  # noqa: SIM115 -- held open for daemon lifetime
        self.file.seek(0)
        self.file.write(b"0")
        self.file.flush()
        self.file.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)


def gateway():
    import dingtalk_stream

    from .bot import (
        DingAPI,
        Handler,
        SafeStreamClient,
        incoming_worker,
        outgoing_worker,
        reaction_worker,
    )
    from .service import Service, queue_worker
    from .transport import ManagedNode

    state = state_dir()
    state.mkdir(parents=True, exist_ok=True)
    instance_lock = SingleInstance(state / "gateway.lock")
    config = load_config()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    handler = RotatingFileHandler(
        state / "gateway.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    logging.getLogger("paramiko").setLevel(logging.WARNING)
    sdk_logger = logging.getLogger("session_bridge.stream")
    sdk_logger.setLevel(logging.WARNING)  # Official SDK INFO logs include short-lived tickets.
    store = Store(state / "gateway.sqlite")
    store.recover()
    service = Service(store, config)
    if config.get("viewer", {}).get("enabled"):
        from .viewer import HistoryViewer

        options = config["viewer"]
        service.viewer = HistoryViewer(
            service,
            options.get("host", "127.0.0.1"),
            options.get("port", 8766),
            options.get("public_url"),
        )
        service.viewer.start()
    stop = threading.Event()
    for node_config in config["nodes"]:
        node = ManagedNode(node_config, service.ingest)
        service.nodes[node.node_id] = node
        node.start()
    api = DingAPI(config)
    api.access_token()
    for target, args in [
        (incoming_worker, (service, stop)),
        (outgoing_worker, (service, api, stop)),
        (queue_worker, (service, stop)),
        (reaction_worker, (service, api, stop)),
    ]:
        threading.Thread(target=target, args=args, daemon=True).start()

    client = SafeStreamClient(
        dingtalk_stream.Credential(config["client_id"], config["client_secret"]), logger=sdk_logger
    )

    def heartbeat():
        while not stop.wait(5):
            connected = getattr(getattr(client, "websocket", None), "state", None) == 1
            store.put(
                "gateway",
                {
                    "pid": os.getpid(),
                    "version": __version__,
                    "time": time.time(),
                    "stream_connected": connected,
                    "nodes": {
                        name: {
                            "online": n.online,
                            "last_seen": n.last_seen,
                            "error": n.error,
                            "version": n.version,
                        }
                        for name, n in service.nodes.items()
                    },
                },
            )
            service.check_stream(connected)

    threading.Thread(target=heartbeat, daemon=True).start()
    client.register_callback_handler(dingtalk_stream.ChatbotMessage.TOPIC, Handler(service))
    logging.getLogger(__name__).info(
        "Gateway started, pid=%s, nodes=%s", os.getpid(), ",".join(service.nodes)
    )
    try:
        client.start_forever()
    finally:
        stop.set()
        for node in service.nodes.values():
            node.stop.set()
        instance_lock.file.close()


def main():
    parser = argparse.ArgumentParser(description="钉钉多电脑会话助手")
    parser.add_argument("command", choices=["run", "status", "pair", "doctor"])
    args = parser.parse_args()
    if hasattr(__import__("sys").stdout, "reconfigure"):
        __import__("sys").stdout.reconfigure(encoding="utf-8")
    if args.command == "run":
        gateway()
    elif args.command == "pair":
        from .service import Service

        service = Service(Store(state_dir() / "gateway.sqlite"), {})
        print("在目标钉钉聊天中发送（群内先 @机器人）： /pair " + service.new_pair_code())
        print("有效期 30 分钟；配对只绑定发送此代码的账号与聊天。")
    elif args.command == "status":
        store = Store(state_dir() / "gateway.sqlite")
        print(
            json.dumps(
                {
                    "gateway": store.get("gateway"),
                    "paired_chats": len(store.pairs()),
                    "sessions": len(store.sessions()),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        from .bot import DingAPI

        config = load_config()
        DingAPI(config).access_token()
        print("钉钉凭证：有效（令牌未输出）")
        print("节点：" + ", ".join(n["id"] for n in config["nodes"]))
        print("请运行 session-bridge status 查看节点连接和配对状态。")


if __name__ == "__main__":
    main()
