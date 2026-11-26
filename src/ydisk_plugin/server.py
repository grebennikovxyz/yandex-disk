import asyncio
import base64
import io
import json
import os
import secrets
import tempfile
from contextvars import ContextVar
from itertools import islice
from pathlib import Path
from typing import Annotated, Literal, NotRequired

import httpx
from mcp import types
from mcp.server.fastmcp import FastMCP
from platformdirs import user_downloads_path
from pydantic import Field
from typing_extensions import TypedDict as FileDict

from . import advanced, index, snapshot, web
from .account import Accounts, Problem, account_lock, private_directory
from .disk import CONTENT_LIMIT, OPENAI_FILES, Disk, disk_url, new_version_path, path_of
from .office import Edit, OfficeContent, apply_edits, create, extension, records

SESSION = ContextVar("disk_session")
WRITES = {
    "advanced_write",
    "manage_file",
    "create_office",
    "edit_office",
    "upload_from_computer",
    "save_attachment",
    "restore_from_trash",
    "publish_link",
    "remove_public_link",
}
DESTRUCTIVE = {"manage_file", "publish_link", "remove_public_link", "advanced_write"}
Offset = Annotated[int, Field(ge=0)]
Limit = Annotated[int, Field(ge=1, le=100)]


class AttachedFile(FileDict):
    download_url: str
    file_id: str
    file_name: NotRequired[str]
    mime_type: NotRequired[str]


def public_record(data):
    return {
        k: data[k]
        for k in (
            "name",
            "type",
            "size",
            "mime_type",
            "modified",
            "revision",
            "sha256",
            "resource_id",
            "path",
        )
        if k in data
    }


def ref(path, **extra):
    path = path_of(path)
    return {"id": "disk:" + path, "url": disk_url(path), **extra}


def get_bytes(path, remember=False):
    account, disk = SESSION.get()
    buf = io.BytesIO()
    digest = disk.download(path, buf, CONTENT_LIMIT)
    data = buf.getvalue()
    if remember and extension(path) in {".docx", ".xlsx", ".pptx"}:
        snapshot.remember(account, path, digest, data)
    return data, digest


def transfer_file(name):
    if (
        not name
        or name in {".", ".."}
        or any(c in name for c in '/\\:*?"<>|')
        or any(ord(c) < 32 for c in name)
        or name.endswith((".", " "))
        or (hasattr(os.path, "isreserved") and os.path.isreserved(name))
    ):
        raise Problem("Нужно обычное имя файла в папке обмена.")
    if Path(name).name != name or name in {"", ".", ".."}:
        raise Problem("Недопустимое имя файла.")
    root = user_downloads_path() / "Yandex-Disk-Transfer"
    if root.is_symlink() or root.is_junction():
        raise Problem("Папка обмена должна быть обычной папкой.")
    target = private_directory(root) / name
    if target.is_symlink() or target.is_junction():
        raise Problem("Ссылки в папке обмена не поддерживаются.")
    return target


class PersonalServer(FastMCP):
    def __init__(self, tunnel, project, pin):
        self.tunnel = tunnel
        self.project = None if tunnel else Path(project or Path.cwd()).resolve()
        self.discover_project = not tunnel and project is None
        self.pin = pin
        self.call_lock = asyncio.Lock()
        super().__init__("yandex-disk", log_level="ERROR")

    async def client_projects(self):
        if not self.discover_project:
            return []
        try:
            session = self.get_context().request_context.session
        except ValueError:  # In-process calls have no MCP client.
            return []
        if not session.check_client_capability(
            types.ClientCapabilities(roots=types.RootsCapability())
        ):
            return []
        try:
            async with asyncio.timeout(5):
                roots = (await session.list_roots()).roots
            if any(root.uri.host not in (None, "", "localhost") for root in roots):
                raise ValueError("remote root")
            return [Path.from_uri(str(root.uri)) for root in roots]
        except Exception as exc:
            raise Problem(
                "Не удалось определить проект MCP. Укажите папку через serve --project."
            ) from exc

    async def list_tools(self):
        tools = await super().list_tools()
        if not self.tunnel:
            for tool in tools:
                if tool.name != "list_accounts":
                    tool.inputSchema = {
                        **tool.inputSchema,
                        "properties": {
                            **tool.inputSchema.get("properties", {}),
                            "account": {
                                "type": "string",
                                "description": "Disk profile ID from list_accounts. Use the same ID on every call for this chat.",
                            },
                        },
                    }
        return tools

    async def call_tool(self, name, arguments):
        # Cooperate with other stdio requests while a child MCP server is running.
        async with self.call_lock:
            return await self.dispatch(name, arguments)

    async def dispatch(self, name, arguments):
        try:
            roots = await self.client_projects()
            with account_lock():
                accounts = Accounts()
                project = self.project
                if roots:
                    bindings = {accounts.bound(path) for path in roots}
                    if len(bindings) != 1:
                        raise Problem(
                            "У папок клиента разные привязки. Выберите проект через serve --project."
                        )
                    client_pin = bindings.pop()
                    launch_pin = accounts.bound(self.project)
                    if client_pin and launch_pin and client_pin != launch_pin:
                        raise Problem(
                            "Диски папки запуска и проекта различаются. Укажите serve --project."
                        )
                    if client_pin:
                        project = roots[0]
                arguments = dict(arguments or {})
                if name == "list_accounts" and not self.tunnel:
                    fixed = self.pin or accounts.bound(project)
                    keys = (
                        [accounts.select(project=project, pin=self.pin)]
                        if fixed
                        else accounts.profiles
                    )
                    return [
                        types.TextContent(
                            type="text",
                            text=json.dumps(
                                {
                                    "accounts": [accounts.describe(k) for k in keys],
                                    "pinned": bool(fixed),
                                },
                                ensure_ascii=False,
                            ),
                        )
                    ]
                if self.tunnel and ("account" in arguments or name == "list_accounts"):
                    raise Problem("Туннель обслуживает только закреплённый Диск.")
                pin = self.pin or (accounts.data["web"] if self.tunnel else None)
                key = accounts.select(
                    arguments.pop("account", None),
                    project=project,
                    pin=pin,
                    explicit=not self.tunnel,
                )
                account = accounts.read(key)
                if self.tunnel:
                    if name == "complete_request":
                        name, arguments = web.consume(account, arguments["ticket"])
                    else:
                        payload = web.stage(account, name, arguments)
                        return [
                            types.TextContent(
                                type="text",
                                text=json.dumps(payload, ensure_ascii=False),
                            )
                        ]
                if name == "complete_request":
                    raise Problem("Билет не может подтверждать другой билет.")
                if name in WRITES and not account.writable:
                    raise Problem("Этот вход разрешает только чтение.")
                with httpx.Client(
                    timeout=httpx.Timeout(45, connect=10), trust_env=False
                ) as http:
                    handle = SESSION.set((account, Disk(http, account.token)))
                    try:
                        return await super().call_tool(name, arguments)
                    finally:
                        SESSION.reset(handle)
        except Exception as err:  # noqa: BLE001 - redact SDK errors at the MCP boundary
            cause = err
            while cause:
                if isinstance(cause, Problem):
                    raise Problem(str(cause)) from None
                cause = cause.__cause__
            # HTTP exceptions and validation errors may contain temporary URLs or tool arguments.
            raise Problem(
                "Операция не завершена. Проверьте аргументы, формат файла, сеть и локальное хранилище ключей."
            ) from None


def make_server(tunnel=False, *, project=None, pin=None):
    server = PersonalServer(tunnel, project, pin)

    def tool(fn=None, *, meta=None):
        def register(f):
            annotations = types.ToolAnnotations(
                readOnlyHint=f.__name__ not in WRITES
                and f.__name__
                not in {"complete_request", "download_to_computer", "sync_library"},
                destructiveHint=f.__name__ in DESTRUCTIVE
                or f.__name__ == "complete_request",
                openWorldHint=True,
            )
            return server.tool(
                annotations=annotations, meta=meta, structured_output=False
            )(f)

        return register(fn) if fn else register

    @tool
    def get_profile():
        """Show the connected Disk owner and access mode."""
        account, disk = SESSION.get()
        info = disk.api("GET", "", fields="user,total_space,used_space")
        return {
            "account_id": account.profile,
            "user": info.get("user"),
            "writable": account.writable,
            "total": info.get("total_space"),
            "used": info.get("used_space"),
        }

    @tool
    def list_folder(path: str = "/", offset: Offset = 0, limit: Limit = 50):
        """List a personal Disk directory. Use the returned offset for the next page."""
        _, disk = SESSION.get()
        items, more = disk.page(path, offset, limit)
        return {
            "items": [public_record(v) for v in items],
            "next_offset": offset + len(items) if more else None,
        }

    @tool
    def recent_uploads(limit: Limit = 30):
        """List recently uploaded files in Yandex's upload order."""
        _, disk = SESSION.get()
        return {
            "items": [
                public_record(x)
                for x in disk.api("GET", "/resources/last-uploaded", limit=limit).get(
                    "items", []
                )
            ]
        }

    @tool
    def get_metadata(path: str):
        """Get current file identity, size, type, hash and modification time."""
        return public_record(SESSION.get()[1].meta(path))

    @tool
    def search(query: str, limit: Limit = 20, offline: bool = False):
        """Find files by filename on Disk and by words in locally indexed folders."""
        account, disk = SESSION.get()
        if not query.strip() or len(query) > 400:
            raise Problem("Введите от 1 до 400 символов.")
        rows, roots, timestamp = index.matches(account, query, limit)
        hits = {}
        for path, sha, excerpt in rows:
            if offline:
                hits[path] = ref(
                    path,
                    title=path.rsplit("/", 1)[-1],
                    snippet=excerpt,
                    matched="cached_content",
                )
                continue
            try:
                item = disk.meta(path)
            except Problem as e:
                if str(e).startswith(("HTTP 404.", "HTTP 403.")):
                    continue
                raise
            if item.get("sha256") == sha:
                hits[path] = ref(
                    path, title=item["name"], snippet=excerpt, matched="content"
                )
        if not offline and len(hits) < limit:
            for item in disk.files("/"):
                path = path_of(item["path"])
                if query.casefold() in item["name"].casefold() and path not in hits:
                    hits[path] = ref(path, title=item["name"], matched="name")
                    if len(hits) >= limit:
                        break
        return {
            "results": list(hits.values()),
            "content_folders": roots,
            "indexed_at": timestamp,
            "offline": offline,
        }

    @tool
    def fetch(id: str, offset: Offset = 0, offline: bool = False):
        """Read text of a search result; offset is a character offset. Includes the private source URL."""
        if offline:
            return index.cached_page(SESSION.get()[0], id, offset)
        content, sha = get_bytes(id, remember=True)
        remaining = offset
        chunks = []
        used = 0
        more = False
        for item in records(extension(id), content):
            line = item["text"] + "\n"
            if remaining >= len(line):
                remaining -= len(line)
                continue
            line = line[remaining:]
            remaining = 0
            chunks.append(line[: 24000 - used])
            used += len(chunks[-1])
            if used == 24000 and len(line) > len(chunks[-1]):
                more = True
                break
        return ref(
            id,
            title=path_of(id).rsplit("/", 1)[-1],
            text="".join(chunks),
            sha256=sha,
            next_offset=offset + used if more else None,
        )

    @tool
    async def advanced_commands(command: str = ""):
        """List optional local engine operations, or return the named operation's argument schema."""
        return await advanced.run(SESSION.get()[0], "commands", command, {})

    @tool
    async def advanced_read(command: str, parameters: dict | None = None):
        """Read the extended library, change log, versions, Office ranges, mirrors or named Disk API."""
        return await advanced.run(SESSION.get()[0], "read", command, parameters or {})

    @tool
    async def advanced_preview(command: str, parameters: dict):
        """Validate and show a prospective write without executing it. For a mirror plan use mirrors preview."""
        return await advanced.run(SESSION.get()[0], "preview", command, parameters)

    @tool
    async def advanced_write(command: str, parameters: dict):
        """Edit or restore an original at its fingerprint, sync an owner's mirror, resolve conflicts or run a named Disk write."""
        return await advanced.run(SESSION.get()[0], "write", command, parameters)

    @tool
    async def sync_library():
        """Refresh the local versioned library from owner-selected folders. Repeat while meta.complete is false."""
        return await advanced.run(SESSION.get()[0], "sync", "sync", {})

    @tool
    def read_office(path: str, offset: Offset = 0, limit: Limit = 50):
        """Return numbered paragraphs, table cells, sheet cells or slide text blocks for edit_office."""
        content, sha = get_bytes(path, remember=True)
        page = list(
            islice(records(extension(path), content), offset, offset + limit + 1)
        )
        for row in page:
            if len(row["text"]) > 8000:
                row["text"] = row["text"][:8000]
                row["truncated"] = True
        return ref(
            path,
            records=page[:limit],
            sha256=sha,
            next_offset=offset + limit if len(page) > limit else None,
        )

    @tool
    def create_office(path: str, content: OfficeContent):
        """Create a new DOCX, XLSX or PPTX at an unused path. Content must match the extension."""
        _, disk = SESSION.get()
        item = disk.upload(path, create(extension(path), content))
        return ref(path, metadata=public_record(item))

    @tool
    def edit_office(
        path: str,
        sha256: Annotated[str, Field(pattern=r"^[a-fA-F0-9]{64}$")],
        edits: list[Edit],
    ):
        """Save edits to a private sibling copy of exactly the version read, including when the source has changed."""
        account, disk = SESSION.get()
        raw, digest = get_bytes(path)
        changed = digest != sha256.lower()
        if changed:
            raw = snapshot.recall(account, path, sha256.lower())
            if raw is None:
                raise Problem(
                    "Исходник изменился, а прежняя версия недоступна. Перечитайте файл."
                )
        patched = apply_edits(extension(path), raw, edits)
        target = new_version_path(path, secrets.token_hex(4))
        disk.upload(target, patched)
        return {
            "original": ref(path),
            "edited_copy": ref(target),
            "base_sha256": sha256.lower(),
            "source_changed_since_read": changed,
        }

    @tool
    def manage_file(
        action: Literal["mkdir", "copy", "move", "trash"],
        path: str,
        destination: str | None = None,
    ):
        """Create a folder, copy, move/rename, or send an item to Trash. Never overwrite."""
        _, disk = SESSION.get()
        disk.change(action, path, destination)
        return {
            "action": action,
            "source": ref(path),
            "target": ref(destination) if destination else None,
        }

    @tool
    def list_trash(offset: Offset = 0, limit: Limit = 50):
        """List trash entries. Pass the returned trash path unchanged to restore_from_trash."""
        _, disk = SESSION.get()
        data = disk.api(
            "GET", "/trash/resources", path="trash:/", limit=limit, offset=offset
        )
        listing = data.get("_embedded", {})
        items = [public_record(item) for item in listing.get("items", [])]
        return {
            "items": items,
            "next_offset": offset + len(items)
            if offset + len(items) < listing.get("total", 0)
            else None,
        }

    @tool
    def restore_from_trash(trash_path: str):
        """Restore one entry to its original location, preserving any existing file."""
        if not trash_path.startswith("trash:/") or path_of(trash_path[6:]) == "/":
            raise Problem("Выберите запись из list_trash.")
        SESSION.get()[1].api(
            "PUT", "/trash/resources/restore", path=trash_path, overwrite="false"
        )
        return {"restored": True}

    @tool
    def publish_link(path: str):
        """Create a public link accessible to anyone. Use only for an explicit public-sharing request."""
        return SESSION.get()[1].change("publish", path)

    @tool
    def remove_public_link(path: str):
        """Revoke public access by link."""
        SESSION.get()[1].change("unpublish", path)
        return {"public": False}

    @tool
    def open_in_disk(
        path: str,
        reason: Literal["open", "invite_person", "comments", "history"] = "open",
    ):
        """Open the private source in Yandex Disk to manage people, comments or native history."""
        SESSION.get()[1].meta(path)
        return ref(path, action=reason, sharing_changed=False)

    @tool(meta={"openai/fileParams": ["file"]})
    def save_attachment(path: str, file: AttachedFile):
        """Save a ChatGPT-provided file attachment as a new private file."""
        _, disk = SESSION.get()
        with tempfile.TemporaryFile() as stream:
            disk.stream(file["download_url"], stream, OPENAI_FILES)
            stream.seek(0)
            return ref(path, metadata=public_record(disk.upload(path, stream)))

    @tool
    def download_file(path: str):
        """Return a private binary file as an MCP embedded resource (up to 4 MiB)."""
        _, disk = SESSION.get()
        buf = io.BytesIO()
        disk.download(path, buf, limit=4 * 1024 * 1024)
        import mimetypes

        resource = types.BlobResourceContents(
            uri=disk_url(path),
            mimeType=mimetypes.guess_type(path)[0] or "application/octet-stream",
            blob=base64.b64encode(buf.getvalue()).decode(),
        )
        return types.EmbeddedResource(type="resource", resource=resource)

    if not tunnel:

        @tool
        def list_accounts():
            """Show available Disk profiles, owners and access before choosing a Disk in this chat."""
            # Dispatched before opening a Disk session, including when there are no accounts.
            raise Problem("Список Дисков запрашивается до подключения.")

        @tool
        def download_to_computer(path: str, filename: str):
            """Save to the user's Downloads/Yandex-Disk-Transfer directory. Refuse existing names."""
            target = transfer_file(filename)
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as tmp:
                name = tmp.name
            try:
                with open(name, "wb") as output:
                    digest = SESSION.get()[1].download(path, output)
                try:
                    os.link(name, target)
                except FileExistsError:
                    raise Problem("Локальный файл уже существует.") from None
            finally:
                Path(name).unlink(missing_ok=True)
            return {"local_file": str(target), "sha256": digest}

        @tool
        def upload_from_computer(filename: str, path: str):
            """Upload a regular file from Downloads/Yandex-Disk-Transfer. Refuse an existing Disk path."""
            target = transfer_file(filename)
            if not target.is_file() or target.stat().st_nlink != 1:
                raise Problem("Выберите обычный файл в папке обмена.")
            with target.open("rb") as stream:
                return ref(
                    path, metadata=public_record(SESSION.get()[1].upload(path, stream))
                )
    else:

        @tool
        def complete_request(ticket: str):
            """Execute the exact request approved by the owner in their local terminal, once."""
            raise Problem("Недопустимое вложенное подтверждение.")

    return server
