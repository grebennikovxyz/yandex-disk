"""Optional local WireCat engine. Only named Disk operations cross the MCP boundary."""

import asyncio
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from jsonschema import Draft7Validator
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from . import account
from .account import Problem, private_directory
from .disk import path_of

PACKAGE = "@wirecat/yandex-disk-cli"
VERSION = "0.7.1"
READ = {
    "status",
    "folders list",
    "folders browse",
    "search",
    "documents list",
    "documents show",
    "documents history",
    "changes",
    "files show",
    "files download",
    "files sheets show",
    "files documents show",
    "files history",
    "mirrors list",
    "mirrors status",
    "mirrors preview",
    "mirrors conflicts list",
    "mirrors conflicts show",
    "api disk-get",
    "api resources-list",
    "api resources-files-list",
    "api resources-last-uploaded-list",
    "api resources-public-list",
    "api public-resources-get",
    "api trash-resources-list",
    "api operations-get",
}
WRITE = {
    "files update",
    "files sheets create",
    "files sheets update",
    "files documents create",
    "files documents update",
    "files restore",
    "files copy",
    "files move",
    "files upload",
    "files create",
    "files delete",
    "files publish",
    "files unpublish",
    "mirrors sync",
    "mirrors conflicts resolve",
    "api resources-create",
    "api resources-update",
    "api resources-delete",
    "api resources-copy",
    "api resources-move",
    "api resources-publish",
    "api resources-unpublish",
    "api resources-upload-url",
    "api public-settings-update",
    "api public-resources-save-to-disk",
    "api trash-resources-restore",
}


def installation():
    return account.DATA / "extensions" / f"wirecat-{VERSION}"


def node():
    # Ignore project-relative PATH entries when launching a credential-bearing process.
    filename = "node.exe" if os.name == "nt" else "node"
    for directory in map(Path, os.get_exec_path()):
        if directory.is_absolute() and directory.resolve() != Path.cwd():
            executable = directory / filename
            if executable.is_file() and os.access(executable, os.X_OK):
                return str(executable.resolve())
    raise Problem("Для расширения установите Node.js 22.16+ (22.x) или 24+.")


def install():
    runtime = Path(node())
    npm = shutil.which("npm")
    candidates = [runtime.parent / "node_modules/npm/bin/npm-cli.js"]
    if npm and Path(npm).resolve().name == "npm-cli.js":
        candidates.append(Path(npm).resolve())
    entry = next((p for p in candidates if p.is_file()), None)
    if not entry:
        raise Problem("Для установки расширения нужен npm из комплекта Node.js.")
    root = private_directory(installation())
    subprocess.run(
        [
            str(runtime),
            str(entry),
            "install",
            "--prefix",
            str(root),
            "--ignore-scripts",
            "--registry=https://registry.npmjs.org/",
            "--no-fund",
            "--no-audit",
            "--save-exact",
            f"{PACKAGE}@{VERSION}",
        ],
        cwd=root,
        check=True,
    )


def launch(owner):
    package = installation() / "node_modules" / PACKAGE
    manifest = package / "package.json"
    info = json.loads(manifest.read_bytes()) if manifest.is_file() else {}
    if (info.get("name"), info.get("version")) != (PACKAGE, VERSION):
        raise Problem(
            "Установите локальный модуль: yandex-disk-plugin extension install"
        )
    home = private_directory(owner.directory / "advanced")
    config = private_directory(home / "config") / "config.json"
    if not config.exists():
        # This is an isolated config: root selections made by the owner live here too.
        tmp = config.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "defaults": {
                        "update_check": False,
                        "skill_hint": False,
                        "record": False,
                        "record_input": False,
                        "read_scans": "local",
                        "embedding_provider": "local",
                    }
                }
            ),
            encoding="utf-8",
        )
        tmp.replace(config)
    runtime = node()
    env = {
        k: os.environ[k]
        for k in ("SystemRoot", "COMSPEC", "LANG", "LC_ALL")
        if k in os.environ
    }
    env.update(
        HOME=str(home),
        USERPROFILE=str(home),
        APPDATA=str(home),
        LOCALAPPDATA=str(home),
        XDG_DATA_HOME=str(home),
        XDG_CONFIG_HOME=str(home),
        XDG_CACHE_HOME=str(home / "cache"),
        YDISK_CONFIG_DIR=str(config.parent),
        YDISK_STATE_DIR=str(home / "state"),
        YDISK_CACHE_DIR=str(home / "cache"),
        MESSAGING_STORE=str(home / "library.sqlite"),
        TMP=str(private_directory(home / "tmp")),
        TEMP=str(home / "tmp"),
        TMPDIR=str(home / "tmp"),
        NODE_COMPILE_CACHE=str(home / "cache/node"),
        YDISK_TOKEN=owner.token,
        PATH=os.pathsep.join(
            [
                str(Path(runtime).parent),
                *[d for d in os.defpath.split(os.pathsep) if Path(d).is_absolute()],
            ]
        ),
        NODE_NO_WARNINGS="1",
    )
    if not owner.writable:
        env["YDISK_READ_ONLY"] = "1"
    return runtime, str(package / "dist/bin/ydisk.js"), env, home


def owner_command(owner, words):
    """Owner-only folder and mirror configuration, never registered with MCP."""
    runtime, script, env, home = launch(owner)
    result = subprocess.run(
        [
            runtime,
            script,
            "disk",
            *words,
            "--no-input",
            "--timeout",
            "45s",
            "--no-record",
            "--json",
        ],
        env=env,
        cwd=home,
        stdin=subprocess.DEVNULL,
        timeout=65,
        check=False,
    )
    if result.returncode:
        raise Problem("Команда расширения не завершена. См. сообщение выше.")


async def _request(session, tool, payload):
    response = await session.call_tool(tool, payload)
    value = json.loads(response.content[0].text)
    if response.isError:
        error = value.get("error", {})
        if str(error.get("message", "")).endswith("(external_part)"):
            raise Problem(
                "Движок не поддерживает ссылки на части этого офисного файла. "
                "Для правки в отдельной копии используйте read_office и edit_office."
            )
        reason = str(error.get("reason") or error.get("code") or "")
        label = (
            reason
            if re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}", reason)
            else "operation_failed"
        )
        raise Problem(
            f"Движок Диска: {label}. Проверьте аргументы и текущее состояние перед повтором."
        )
    return value


async def run(owner, mode, command, arguments, *, timeout=45):
    # Both MCP context managers use task groups; propagate our safe application errors.
    try:
        return await _run(owner, mode, command, arguments, timeout)
    except BaseExceptionGroup as group:
        safe = group.subgroup(Problem)
        if safe:
            error = safe
            while isinstance(error, BaseExceptionGroup):
                error = error.exceptions[0]
            raise error from None
        raise Problem(
            "Локальный движок завершился без ответа. Проверьте Node.js и установку расширения."
        ) from None


async def _run(owner, mode, command, arguments, timeout):
    if mode == "commands" and not command:
        return {
            "installed": (
                installation() / "node_modules" / PACKAGE / "dist/bin/ydisk.js"
            ).is_file(),
            "read": sorted(READ),
            "write": sorted(WRITE),
            "sync": "Синхронизация выбранных папок",
        }
    allowed = {
        "read": READ,
        "write": WRITE,
        "preview": WRITE,
        "commands": READ | WRITE | {"sync"},
        "sync": {"sync"},
    }
    if command not in allowed.get(mode, ()):
        raise Problem("Операция недоступна в этом режиме. См. advanced_commands.")
    if mode == "write" and not owner.writable:
        raise Problem("Этот вход разрешает только чтение.")
    if not isinstance(arguments, dict):
        raise Problem("Для аргументов нужен объект JSON.")
    if not 1 <= timeout <= 3600:
        raise Problem("Задайте время выполнения от 1 до 3600 секунд.")
    # Raw API file flags and background services must never give an agent access to host paths.
    blocked = {
        "body_file",
        "output",
        "allow_any_file",
        "allow_deletions",
        "allow_replacements",
        "repin",
        "profile",
        "permanently",
    }
    if command == "mirrors conflicts resolve":
        blocked.add("file")
    if command == "mirrors sync":
        blocked.add("replace")
    if any(
        not isinstance(k, str)
        or not re.fullmatch(r"[a-z][a-z0-9_]*", k)
        or k in blocked
        for k in arguments
    ):
        raise Problem("Этот параметр недоступен из чата.")
    supplied = arguments
    if command == "files upload" and mode != "commands":
        from .server import transfer_file

        filename = arguments.get("file")
        if not isinstance(filename, str):
            raise Problem("Укажите имя файла в папке обмена плагина.")
        source = transfer_file(filename)
        if not source.is_file() or source.stat().st_nlink != 1:
            raise Problem("Выберите обычный файл в папке обмена плагина.")
        arguments = {**arguments, "file": str(source)}
    runtime, script, env, home = launch(owner)
    params = StdioServerParameters(
        command=runtime,
        args=[script, "disk", "mcp", "serve", "--timeout", f"{timeout}s"],
        cwd=str(home),
        env=env,
    )
    with open(os.devnull, "w") as diagnostics:  # noqa: ASYNC230 - local null device for child stderr
        async with (
            asyncio.timeout(timeout + 20),
            stdio_client(params, errlog=diagnostics) as streams,
            ClientSession(*streams) as session,
        ):
            await session.initialize()
            found = await _request(session, "ydisk_tools_search", {"query": command})
            contract = next(
                (c for c in found["items"] if c["command"] == command), None
            )
            if not contract:
                raise Problem("В этой версии движка нет такой операции в MCP.")
            schema = contract["arguments"]
            props = {
                k: v
                for k, v in schema.get("properties", {}).items()
                if k not in blocked
            }
            required = set(schema.get("required", []))
            if command in {
                "files update",
                "files restore",
                "files documents update",
                "files sheets update",
            }:
                required.add("expected_fingerprint")
            schema = {
                **schema,
                "properties": props,
                "required": sorted(required),
                "additionalProperties": False,
            }
            if mode == "commands":
                return {
                    "command": command,
                    "description": contract["description"],
                    "arguments": schema,
                }
            if not Draft7Validator(schema).is_valid(arguments):
                raise Problem("Аргументы не соответствуют описанию advanced_commands.")
            if mode == "preview":
                return {
                    "executed": False,
                    "profile": owner.profile,
                    "command": command,
                    "availability": contract.get("availability"),
                    "arguments": supplied,
                }
            writes = mode == "write" or (mode == "sync" and contract["writes"])
            value = await _request(
                session,
                "ydisk_write" if writes else "ydisk_read",
                {"command": command, "arguments": arguments},
            )
            if owner.token in json.dumps(value, ensure_ascii=False):
                raise Problem("Ответ содержит данные входа и скрыт.")
            return value


def mirror_path(value):
    path = Path(value).resolve(strict=True)
    protected = [
        account.DATA,
        Path.home() / ".ssh",
        Path.home() / ".aws",
        Path.home() / ".codex",
    ]
    if (
        not path.is_dir()
        or path == Path(path.anchor)
        or path == Path.home()
        or any(path.is_relative_to(p) or p.is_relative_to(path) for p in protected)
    ):
        raise Problem("Выберите отдельную пользовательскую папку для синхронизации.")
    return str(path)


def owner_words(action, value, local=None):
    if action == "mirror-add":
        if not local:
            raise Problem("Укажите папку Диска и локальную папку.")
        return ["mirrors", "add", path_of(value), mirror_path(local)]
    if action == "folder-add":
        return ["folders", "add", path_of(value)]
    if not value or value.startswith("-"):
        raise Problem("Укажите ID из списка.")
    return ["mirrors" if action == "mirror-remove" else "folders", "remove", value]
