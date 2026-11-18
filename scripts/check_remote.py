"""Run regression tests and opt-in real CLI probes on configured Linux nodes."""

import argparse
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import paramiko

from session_bridge.config import load_config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", choices=["claude", "codex"])
    parser.add_argument("--accept-peer", action="store_true")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
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
        base = node["directory"]
        if args.probe:
            with client.open_sftp() as sftp:
                try:
                    sftp.mkdir(base + "/scripts", 0o700)
                except OSError:
                    sftp.stat(base + "/scripts")
                filename = "probe_" + args.probe + ".py"
                sftp.put(str(Path(__file__).parent / filename), base + "/scripts/" + filename)
                command = [node["python"], "scripts/" + filename]
                if args.probe == "claude":
                    command += ["--wait", "60"]
                    if args.accept_peer:
                        command.append("--accept-peer")
        else:
            command = [
                node["python"],
                "-m",
                "pytest",
                "-q",
                "tests/test_scanner.py",
                "tests/test_state.py",
                "tests/test_failures.py",
            ]
        _, out, err = client.exec_command(
            "cd " + shlex.quote(base) + " && " + shlex.join(command), timeout=90
        )
        for line in out:
            print(node["id"] + ": " + line.rstrip(), flush=True)
        code = out.channel.recv_exit_status()
        print(err.read().decode("utf-8"), flush=True)
        client.close()
        if code:
            raise SystemExit(code)


if __name__ == "__main__":
    main()
