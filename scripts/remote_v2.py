"""Pinned SSH maintenance for the existing node; no credentials printed."""

import argparse
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import paramiko

from session_bridge.config import load_config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=["inspect", "dependencies", "tests", "probe", "probe-desktop", "viewer", "screen"],
    )
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
        if args.action == "inspect":
            code = "import sys,importlib.util,os;print(sys.version);print({k:bool(importlib.util.find_spec(k)) for k in ('websocket','PIL','psutil')});print('display',bool(os.environ.get('DISPLAY')))"
            command = [node["python"], "-c", code]
        elif args.action == "screen":
            code = (
                "import os\nfrom session_bridge.codex_control import desktop_environment\n"
                "os.environ.update(desktop_environment())\n"
                "from session_bridge.node_actions import run_action\n"
                'r=run_action({},"screen","",None)\n'
                'print("screen_capture_ok",r["status"]=="ok","encoded_bytes",len(r["image_base64"]))'
            )
            command = [node["python"], "-c", code]
        elif args.action == "viewer":
            url = load_config()["viewer"]["public_url"] + "/session/invalid"
            code = (
                "import urllib.request,urllib.error\n"
                "try:\n urllib.request.build_opener(urllib.request.ProxyHandler({})).open("
                + repr(url)
                + ",timeout=8)\n"
                'except urllib.error.HTTPError as e:\n print("viewer_http_status",e.code)\n'
                "from session_bridge.codex_control import desktop_environment\n"
                'print("desktop_environment_available",bool(desktop_environment().get("DISPLAY")))'
            )
            command = [node["python"], "-c", code]
        elif args.action == "dependencies":
            command = [
                node["python"],
                "-m",
                "pip",
                "install",
                "--user",
                "--timeout",
                "20",
                "--retries",
                "1",
                "websocket-client==1.8.0",
                "Pillow==10.4.0",
            ]
        elif args.action == "tests":
            command = [
                node["python"],
                "-m",
                "pytest",
                "-q",
                "tests/test_scanner.py",
                "tests/test_state.py",
                "tests/test_failures.py",
                "tests/test_interaction.py",
                "tests/test_v2_service.py",
                "tests/test_codex_control.py",
                "tests/test_node_actions.py",
                "tests/test_subagent_policy.py",
                "tests/test_revision.py",
                "tests/test_revision_safety.py",
                "tests/test_interactive.py",
                "tests/test_mobile_regression.py",
            ]
        else:
            with client.open_sftp() as sftp:
                try:
                    sftp.mkdir(node["directory"] + "/scripts", 0o700)
                except OSError:
                    sftp.stat(node["directory"] + "/scripts")
                sftp.put(
                    str(Path(__file__).parent / "probe_shared_codex.py"),
                    node["directory"] + "/scripts/probe_shared_codex.py",
                )
            command = [node["python"], "scripts/probe_shared_codex.py"]
            if args.action == "probe-desktop":
                command.append("--desktop")
        _, out, err = client.exec_command(
            "cd " + shlex.quote(node["directory"]) + " && " + shlex.join(command), timeout=180
        )
        for line in out:
            print(node["id"] + ": " + line.rstrip(), flush=True)
        code = out.channel.recv_exit_status()
        print(err.read().decode("utf-8", "replace"), flush=True)
        client.close()
        if code:
            raise SystemExit(code)


if __name__ == "__main__":
    main()
