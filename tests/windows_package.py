"""Verify the Windows release ZIP with only its bundled Python and MCP server."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from zipfile import ZipFile


async def probe(folder):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from ydisk_plugin.account import vault
    from ydisk_plugin.office import OfficeContent, create

    config = json.loads((folder / "mcp.json").read_bytes())["mcpServers"]["yandex-disk"]
    assert config["command"].startswith("./")
    command = str((folder / config["command"]).resolve())
    assert Path(command) == Path(sys.executable).resolve()
    assert vault().priority > 0
    assert create(".docx", OfficeContent(paragraphs=["Package check"])).startswith(
        b"PK"
    )
    params = StdioServerParameters(command=command, args=config["args"])
    async with (
        stdio_client(params) as (reader, writer),
        ClientSession(reader, writer) as session,
    ):
        await session.initialize()
        tools = {tool.name for tool in (await session.list_tools()).tools}
        assert {"get_profile", "search", "fetch", "edit_office"} <= tools
        print(f"Standalone Windows MCP returned {len(tools)} tools.")


def check_zip():
    root = Path(__file__).resolve().parents[1]
    version = json.loads((root / "plugins/yandex-disk/plugin.json").read_bytes())[
        "version"
    ]
    archive = root / "dist" / f"yandex-disk-plugin-{version}-windows-x64.zip"
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / "Plugin with spaces"
        with ZipFile(archive) as z:
            assert {"connect.cmd", "NOTICE", "runtime/LICENSE.txt"} <= set(z.namelist())
            z.extractall(folder)
        env = {**os.environ, "PATH": str(Path(os.environ["SystemRoot"]) / "System32")}
        subprocess.run(
            [folder / "runtime/python.exe", "-I", "-B", __file__, folder],
            env=env,
            cwd=tmp,
            check=True,
            timeout=90,
        )


if __name__ == "__main__":
    if len(sys.argv) == 2:
        asyncio.run(probe(Path(sys.argv[1])))
    else:
        check_zip()
