import json
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_marketplaces_share_the_installed_server_and_skill():
    codex = json.loads((ROOT / ".agents/plugins/marketplace.json").read_bytes())
    claude = json.loads((ROOT / ".claude-plugin/marketplace.json").read_bytes())
    (codex_entry,) = codex["plugins"]
    (claude_entry,) = claude["plugins"]
    folder = ROOT / claude_entry["source"]
    assert folder == ROOT / codex_entry["source"]["path"]
    assert codex_entry["name"] == claude_entry["name"] == "yandex-disk"
    for kind in ["codex", "claude"]:
        manifest = json.loads((folder / f".{kind}-plugin/plugin.json").read_bytes())
        mcp = json.loads((folder / manifest["mcpServers"]).read_bytes())["mcpServers"]
        assert mcp["yandex-disk"]["command"] == "yandex-disk-plugin"
        assert mcp["yandex-disk"]["args"] == ["serve"]
        assert (
            folder / manifest.get("skills", "skills") / "yandex-disk/SKILL.md"
        ).is_file()
    text = (ROOT / "openclaw/skills/yandex-disk/SKILL.md").read_text("utf-8")
    assert (
        "openclaw mcp add yandex-disk --command yandex-disk-plugin --arg serve" in text
    )


def test_release_tag_and_manifests_agree():
    version = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"][
        "version"
    ]
    script = [sys.executable, ROOT / "scripts/marketplaces.py", "--tag"]
    subprocess.run([*script, f"v{version}"], check=True)
    assert subprocess.run([*script, "v0.0.0-wrong"], check=False).returncode != 0
