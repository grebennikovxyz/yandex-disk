from subprocess import CompletedProcess
from unittest.mock import Mock

from ydisk_plugin import cli


def test_web_setup_uses_path_for_stdio_child(monkeypatch):
    def which(name):
        return rf"C:\User Tools\bin\{name}.exe"

    run = Mock(return_value=CompletedProcess([], 0))
    monkeypatch.setattr(cli.shutil, "which", which)
    monkeypatch.setattr(cli.subprocess, "run", run)
    cli.setup_web("tunnel_example")

    init, doctor = [call.args[0] for call in run.call_args_list]
    assert init[init.index("--mcp-command") + 1] == "yandex-disk-plugin serve-web"
    assert init[init.index("--health-listen-addr") + 1] == "127.0.0.1:0"
    assert doctor == [
        which("tunnel-client"),
        "doctor",
        "--profile",
        "yandex-disk-personal",
        "--explain",
    ]
