"""Verify discovery by the installed Codex binary in a clean local profile."""

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


def run():
    root = Path(__file__).resolve().parents[1]
    codex = shutil.which("codex.exe") or shutil.which("codex")
    if not codex:
        raise RuntimeError("Codex CLI is required")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as profile:
        env = {**os.environ, "CODEX_HOME": profile}
        for args in (
            ["plugin", "marketplace", "add", str(root)],
            ["plugin", "add", "yandex-disk@grebennikov-plugins"],
        ):
            subprocess.run(
                [codex, *args], env=env, check=True, stdout=subprocess.DEVNULL
            )
        proc = subprocess.Popen(
            [codex, "app-server"],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
        )
        incoming = queue.Queue()

        def reader():
            for line in proc.stdout:
                incoming.put(json.loads(line))

        threading.Thread(target=reader, daemon=True).start()
        sequence = 0

        def request(method, params):
            nonlocal sequence
            sequence += 1
            proc.stdin.write(
                json.dumps({"id": sequence, "method": method, "params": params}) + "\n"
            )
            proc.stdin.flush()
            while True:
                msg = incoming.get(timeout=45)
                if msg.get("id") != sequence:
                    continue
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error'].get('message')}")
                return msg["result"]

        try:
            request(
                "initialize",
                {
                    "clientInfo": {"name": "ydisk-acceptance", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            proc.stdin.write('{"method":"initialized"}\n')
            proc.stdin.flush()
            plugin = request(
                "plugin/read",
                {
                    "marketplacePath": str(root / ".agents/plugins/marketplace.json"),
                    "pluginName": "yandex-disk",
                },
            )["plugin"]
            assert plugin["mcpServers"], (
                "Codex did not find the plugin MCP configuration"
            )
            assert any(
                s["name"] == "yandex-disk:yandex-disk" and s["enabled"]
                for s in plugin["skills"]
            ), f"Unexpected plugin skills: {[(s['name'], s['enabled']) for s in plugin['skills']]}"
            print("Codex discovered the enabled yandex-disk skill.", flush=True)
            print("Codex recognized:", plugin["mcpServers"], flush=True)
            session = request("thread/start", {"ephemeral": True, "cwd": str(root)})
            tid = session["thread"]["id"]
            deadline = time.monotonic() + 35
            while True:
                statuses = request("mcpServerStatus/list", {"threadId": tid})["data"]
                for status in statuses:
                    if status.get("toolsError"):
                        raise RuntimeError(f"{status['name']}: {status['toolsError']}")
                ydisk = next(
                    (
                        s
                        for s in statuses
                        if "yandex" in s["name"] and "fetch" in s["tools"]
                    ),
                    None,
                )
                if ydisk:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "Codex MCP inventory: "
                        + str(
                            [
                                (s["name"], s.get("runtimeStatus"), len(s["tools"]))
                                for s in statuses
                            ]
                        )
                    )
                time.sleep(1)
            print("Tools discovered by Codex:", len(ydisk["tools"]), flush=True)
            if "--profile-check" in sys.argv:
                result = request(
                    "mcpServer/tool/call",
                    {
                        "threadId": tid,
                        "server": ydisk["name"],
                        "tool": "get_profile",
                        "arguments": {},
                    },
                )
                if result.get("isError") or not result.get("content"):
                    raise RuntimeError("The account profile call failed")
                payload = json.loads(result["content"][0]["text"])
                assert payload["user"].get("uid")
                print("Codex read the authorized Disk profile.", flush=True)
        finally:
            proc.stdin.close()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.terminate()
                proc.wait(timeout=5)


if __name__ == "__main__":
    run()
