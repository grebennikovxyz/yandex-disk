"""Build the portable plugin ZIP from an allowlist and record checksums beside it."""

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument(
    "--windows-x64",
    action="store_true",
    help="Also bundle a clean Windows Python runtime and dependencies (requires uv)",
)
args = parser.parse_args()
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
artifacts = [zip_path, wheel]
if args.windows_x64:
    if sys.platform != "win32":
        raise SystemExit("Build the Windows package on Windows x64.")
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        subprocess.run(
            [
                "uv",
                "python",
                "install",
                "cpython-3.13-windows-x86_64-none",
                "--install-dir",
                str(work),
                "--no-bin",
                "--no-registry",
            ],
            check=True,
        )
        (runtime,) = work.glob("cpython-3.13.*-windows-x86_64-none")
        packages = work / "packages"
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(runtime / "python.exe"),
                "--target",
                str(packages),
                "--only-binary",
                ":all:",
                str(wheel),
            ],
            check=True,
        )
        windows_zip = out / f"yandex-disk-plugin-{version}-windows-x64.zip"
        with (
            ZipFile(zip_path) as portable,
            ZipFile(windows_zip, "w", ZIP_DEFLATED) as z,
        ):
            for name in portable.namelist():
                content = portable.read(name)
                if name == "mcp.json":
                    config = json.loads(content)
                    server = config["mcpServers"]["yandex-disk"]
                    server.update(
                        command="./runtime/python.exe",
                        args=["-I", "-B", "-m", "ydisk_plugin.cli", "serve"],
                    )
                    content = json.dumps(config, indent=2).encode()
                z.writestr(name, content)
            for path in sorted(runtime.rglob("*")):
                rel = path.relative_to(runtime)
                if rel.parts[0] in {"include", "libs", "Scripts"} or rel.parts[:2] == (
                    "Lib",
                    "site-packages",
                ):
                    continue
                if (
                    path.is_file()
                    and "__pycache__" not in rel.parts
                    and path.suffix != ".pyc"
                ):
                    z.write(path, f"runtime/{rel.as_posix()}")
            for path in sorted(packages.rglob("*")):
                rel = path.relative_to(packages)
                if rel.parts[0] in {"Scripts", "bin"} or "__pycache__" in rel.parts:
                    continue
                if (
                    path.is_file()
                    and path.name
                    not in {"direct_url.json", "uv_cache.json", "uv_build.json"}
                    and path.suffix != ".pyc"
                ):
                    z.write(path, f"runtime/Lib/site-packages/{rel.as_posix()}")
            z.writestr(
                "connect.cmd",
                '@echo off\r\n"%~dp0runtime\\python.exe" -I -B -m ydisk_plugin.cli login\r\nif errorlevel 1 pause\r\n',
            )
        artifacts.append(windows_zip)


def digest(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


(out / "SHA256SUMS.txt").write_text(
    "".join(f"{digest(p)}  {p.name}\n" for p in artifacts), encoding="utf-8"
)
print(*(p.name for p in artifacts))
