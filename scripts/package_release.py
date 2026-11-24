"""Build the portable plugin ZIP from an allowlist and record checksums beside it."""

import hashlib
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parents[1]
plugin = root / "plugins" / "yandex-disk"
out = root / "dist"
out.mkdir(exist_ok=True)
version = json.loads((plugin / "plugin.json").read_text(encoding="utf-8"))["version"]
(wheel,) = out.glob(f"grebennikov_yandex_disk-{version}-*.whl")
zip_path = out / f"yandex-disk-plugin-{version}.zip"
with ZipFile(zip_path, "w", ZIP_DEFLATED) as z:
    for path in [
        plugin / "plugin.json",
        plugin / "mcp.json",
        plugin / ".codex-plugin/plugin.json",
        plugin / "assets/yandex-disk-codex.png",
        plugin / "skills/yandex-disk/SKILL.md",
    ]:
        z.write(path, path.relative_to(plugin).as_posix())
    for name in ["LICENSE", "NOTICE", "README.md"]:
        z.write(root / name, name)
digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
(out / "SHA256SUMS.txt").write_text(
    "".join(f"{digest(p)}  {p.name}\n" for p in (zip_path, wheel)), encoding="utf-8"
)
print(zip_path.name, wheel.name)
