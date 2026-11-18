"""Update configured remote nodes over pinned SSH; credentials stay outside source."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import paramiko

from session_bridge.config import load_config


def main():
    source = Path(__file__).resolve().parent.parent
    for node in load_config()["nodes"]:
        if not node.get("host"):
            continue
        client = paramiko.SSHClient()
        client.load_host_keys(node["known_hosts"])
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        client.connect(
            node["host"],
            port=node.get("port", 22),
            username=node["user"],
            password=node.get("password"),
            key_filename=node.get("key_file"),
            look_for_keys=False,
            allow_agent=False,
            timeout=15,
        )
        sftp = client.open_sftp()
        base = node["directory"]
        for folder in ("session_bridge", "tests"):
            try:
                sftp.mkdir(base + "/" + folder, 0o700)
            except OSError:
                sftp.stat(base + "/" + folder)
            for file in (source / folder).glob("*.py"):
                target = base + "/" + folder + "/" + file.name
                sftp.put(str(file), target + ".new")
                sftp.chmod(target + ".new", 0o600)
                sftp.posix_rename(target + ".new", target)
        for name in ("README.md", "THIRD_PARTY_NOTICES.md"):
            if (source / name).exists():
                sftp.put(str(source / name), base + "/" + name)
        command = (
            "cd "
            + shlex.quote(base)
            + " && "
            + shlex.join(
                [
                    node["python"],
                    "-m",
                    "session_bridge.setup_hooks",
                    "--python",
                    node["python"],
                    "--project",
                    base,
                    "--state",
                    node["state"],
                ]
            )
        )
        _, out, err = client.exec_command(command, timeout=20)
        code = out.channel.recv_exit_status()
        print(node["id"], "hook install exit", code)
        print(out.read().decode("utf8"))
        print(err.read().decode("utf8"))
        sftp.close()
        client.close()
        if code:
            raise SystemExit(code)


if __name__ == "__main__":
    main()
