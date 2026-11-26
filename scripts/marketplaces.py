"""Keep installable manifests and the standalone skill on the Python release version."""

import argparse
import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins/yandex-disk"
SKILL = ROOT / "openclaw/skills/yandex-disk/SKILL.md"
MANIFESTS = [
    PLUGIN / "plugin.json",
    PLUGIN / ".codex-plugin/plugin.json",
    PLUGIN / ".claude-plugin/plugin.json",
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sync", action="store_true", help="Apply the version from pyproject.toml"
    )
    parser.add_argument("--tag", help="Check a release tag against pyproject.toml")
    args = parser.parse_args()
    version = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"][
        "version"
    ]
    if not re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", version):
        parser.error("Expected an X.Y.Z project version")
    if args.tag and args.tag != "v" + version:
        parser.error(f"{args.tag} does not match v{version}")
    stale = []
    for path in MANIFESTS:
        obj = json.loads(path.read_bytes())
        if obj.get("version") != version:
            stale.append(path)
            if args.sync:
                obj["version"] = version
                path.write_text(
                    json.dumps(obj, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
    text = SKILL.read_text("utf-8")
    first, frontmatter, body = text.split("---\n", 2)
    frontmatter, count = re.subn(
        r"(?m)^version: [^\n]+$", "version: " + version, frontmatter
    )
    if first or count != 1:
        parser.error("Expected one version in OpenClaw skill frontmatter")
    updated = f"---\n{frontmatter}---\n{body}"
    if text != updated:
        stale.append(SKILL)
        if args.sync:
            SKILL.write_text(updated, encoding="utf-8")
    if stale and not args.sync:
        parser.error(
            "Run python scripts/marketplaces.py --sync for: "
            + ", ".join(str(p.relative_to(ROOT)) for p in stale)
        )
    print(version)


if __name__ == "__main__":
    main()
