import asyncio
import hashlib
import io
import json
from dataclasses import asdict, replace
from pathlib import Path

import httpx
import pytest

from ydisk_plugin import account, index, server, web
from ydisk_plugin.disk import YANDEX, Disk, path_of, safe_host
from ydisk_plugin.office import OfficeContent, create


@pytest.fixture
def owner(monkeypatch, tmp_path):
    monkeypatch.setattr(account, "DATA", tmp_path)
    from unittest.mock import Mock

    entries = {}
    keys = Mock()
    keys.get_password.side_effect = lambda service, name: entries.get((service, name))
    keys.set_password.side_effect = lambda service, name, value: entries.update(
        {(service, name): value}
    )
    keys.delete_password.side_effect = lambda service, name: entries.pop(
        (service, name)
    )
    monkeypatch.setattr(account, "vault", lambda: keys)
    a = account.Account("test-only", "123", "Test", True, "a" * 32, 0)
    account.Accounts().add(a)
    return a


class MemoryDisk:
    def __init__(self):
        self.items = {
            "/Docs/Документ.docx": create(
                ".docx", OfficeContent(paragraphs=["черника"])
            )
        }

    def meta(self, path):
        path = path_of(path)
        if path == "/Docs":
            return {"type": "dir", "path": "disk:/Docs"}
        if path not in self.items:
            raise account.Problem("HTTP 404.")
        return {
            "name": Path(path).name,
            "path": "disk:" + path,
            "type": "file",
            "size": len(self.items[path]),
            "sha256": hashlib.sha256(self.items[path]).hexdigest(),
        }

    def files(self, root):
        return (
            self.meta(p) for p in self.items if p.startswith(root.rstrip("/") + "/")
        )

    def download(self, path, dest, limit=None):
        dest.write(self.items[path_of(path)])
        return self.meta(path)["sha256"]

    def upload(self, path, content):
        assert path not in self.items
        self.items[path] = content
        return self.meta(path)


def invoke(srv, name, args):
    result = asyncio.run(srv.call_tool(name, args))
    return json.loads(result[0].text)


def test_offline_search_and_read_never_use_disk(owner, monkeypatch):
    index.rebuild(owner, MemoryDisk(), ["/Docs"])
    monkeypatch.setattr(server, "Disk", lambda *_: object())
    app = server.make_server()
    results = invoke(app, "search", {"query": "черника", "offline": True})
    assert results["offline"] and results["indexed_at"]
    item = results["results"][0]
    cached = invoke(app, "fetch", {"id": item["id"], "offline": True})
    assert "черника" in cached["text"] and "sha256" not in cached
    assert not cached["next_offset"]


def test_search_fetch_conflicting_edit(owner, monkeypatch):
    disk = MemoryDisk()
    monkeypatch.setattr(server, "Disk", lambda *_: disk)
    assert index.rebuild(owner, disk, ["/Docs"])["indexed"] == 1
    app = server.make_server()
    hit = invoke(app, "search", {"query": "черника"})["results"][0]
    assert hit["matched"] == "content"
    file = invoke(app, "fetch", {"id": hit["id"]})
    assert "черника" in file["text"]
    disk.items["/Docs/Документ.docx"] = create(
        ".docx", OfficeContent(paragraphs=["Чужая правка"])
    )
    out = invoke(
        app,
        "edit_office",
        {
            "path": hit["id"],
            "sha256": file["sha256"],
            "edits": [{"target": "p:1", "value": "Моя правка"}],
        },
    )
    assert out["source_changed_since_read"]
    assert (
        "Моя правка" in invoke(app, "fetch", {"id": out["edited_copy"]["id"]})["text"]
    )
    assert "Чужая правка" in invoke(app, "fetch", {"id": hit["id"]})["text"]
    assert not invoke(app, "search", {"query": "черника"})["results"]
    assert index.matches(replace(owner, profile="other"), "черника", 5)[0] == []


def test_read_only_and_tunnel(owner, monkeypatch):
    disk = MemoryDisk()
    monkeypatch.setattr(server, "Disk", lambda *_: disk)
    app = server.make_server(True)
    account.Accounts().add(replace(owner, writable=False))
    staged = invoke(app, "fetch", {"id": "/Docs/Документ.docx"})
    assert "черника" not in str(staged)
    with pytest.raises(account.Problem):
        invoke(app, "complete_request", {"ticket": staged["ticket"]})
    other = replace(owner, profile="other")
    with pytest.raises(account.Problem):
        web.consume(other, staged["ticket"])
    monkeypatch.setattr("builtins.input", lambda prompt: "YES")
    web.approve(owner, staged["approval_code"])
    assert (
        "черника"
        in invoke(app, "complete_request", {"ticket": staged["ticket"]})["text"]
    )
    with pytest.raises(account.Problem):
        invoke(app, "complete_request", {"ticket": staged["ticket"]})
    with pytest.raises(account.Problem, match="чтение"):
        invoke(server.make_server(), "publish_link", {"path": "/Docs"})


def test_web_advanced_write_requires_owner_approval(owner, monkeypatch):
    calls = []

    async def record(profile, mode, command, parameters):
        calls.append((profile.profile, mode, command, parameters))
        return {"done": True}

    monkeypatch.setattr(server.advanced, "run", record)
    app = server.make_server(True)
    params = {"path": "/Docs/example.txt", "text": "private example"}
    staged = invoke(
        app, "advanced_write", {"command": "files update", "parameters": params}
    )
    with pytest.raises(account.Problem):
        invoke(app, "complete_request", {"ticket": staged["ticket"]})
    assert not calls
    monkeypatch.setattr("builtins.input", lambda _: "YES")
    web.approve(owner, staged["approval_code"])
    assert invoke(app, "complete_request", {"ticket": staged["ticket"]})["done"]
    assert calls == [(owner.profile, "write", "files update", params)]
    with pytest.raises(account.Problem):
        invoke(app, "complete_request", {"ticket": staged["ticket"]})


def test_stream_upload_and_links(owner):
    seen = []

    def endpoint(req):
        seen.append(req)
        q = req.url.params
        if req.url.host == "cloud-api.yandex.net":
            assert req.headers["Authorization"] == "OAuth test-only"
            if req.url.path.endswith("/upload"):
                assert q["overwrite"] == "false"
                return httpx.Response(
                    200, json={"href": "https://uploader.disk.yandex.net/put"}
                )
            if req.url.path.endswith("/download"):
                return httpx.Response(
                    200, json={"href": "https://downloader.disk.yandex.ru/get"}
                )
            if req.method == "DELETE":
                assert q["permanently"] == "false"
            if q.get("fields") == "public_url":
                return httpx.Response(
                    200, json={"public_url": "https://disk.yandex.ru/d/example"}
                )
            return httpx.Response(200, json={})
        assert "Authorization" not in req.headers
        if req.method == "PUT":
            assert req.read() == b"file"
        return httpx.Response(200, content=b"file")

    with httpx.Client(transport=httpx.MockTransport(endpoint)) as http:
        d = Disk(http, owner.token)
        d.upload("/file", b"file")
        buf = io.BytesIO()
        d.download("/file", buf)
        assert buf.getvalue() == b"file"
        assert d.change("publish", "/file")["public_url"]
        d.change("unpublish", "/file")
        d.change("trash", "/file")
        d.api(
            "PUT",
            "/trash/resources/restore",
            path="trash:/deleted-file",
            overwrite="false",
        )
    assert any(r.url.path.endswith("/restore") for r in seen)


@pytest.mark.parametrize(
    "url",
    [
        "http://disk.yandex.ru/x",
        "https://yandex.ru.bad.example/x",
        "https://evil.example@disk.yandex.ru/x",
        "https://127.0.0.1/x",
    ],
)
def test_transfer_host(url):
    with pytest.raises(account.Problem):
        safe_host(url, YANDEX)


def test_block_mismatched_redirect(monkeypatch):
    real = httpx.Client
    handler = httpx.MockTransport(
        lambda req: httpx.Response(
            200, json={"callback": "https://unrelated.example/return"}
        )
    )
    monkeypatch.setattr(account.httpx, "Client", lambda **kw: real(transport=handler))
    with pytest.raises(account.Problem, match="Redirect URI"):
        account.login("public-id")


def test_stdio_discovery():
    import sys

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def run():
        for mode in ("serve", "serve-web"):
            params = StdioServerParameters(
                command=sys.executable, args=["-m", "ydisk_plugin.cli", mode]
            )
            async with stdio_client(params) as streams, ClientSession(*streams) as sess:
                await sess.initialize()
                desc = {t.name: t for t in (await sess.list_tools()).tools}
                assert {
                    "fetch",
                    "search",
                    "edit_office",
                    "save_attachment",
                } <= desc.keys()
                assert ("complete_request" in desc) == (mode == "serve-web")
                assert ("upload_from_computer" in desc) == (mode == "serve")
                assert ("list_accounts" in desc) == (mode == "serve")
                assert ("account" in desc["fetch"].inputSchema["properties"]) == (
                    mode == "serve"
                )
                assert desc["save_attachment"].meta["openai/fileParams"] == ["file"]

    asyncio.run(run())


def test_legacy_login_and_reauthorization_preserve_index(owner, monkeypatch, tmp_path):
    monkeypatch.setattr(account, "DATA", tmp_path / "migration")
    old = replace(owner, profile="c" * 32)
    index.rebuild(old, MemoryDisk(), ["/Docs"])
    fields = asdict(old)
    del fields["login"]  # Format written by previously released versions.
    keys = account.vault()
    keys.set_password(account.SERVICE, "active", json.dumps(fields))
    catalog = account.Accounts()
    assert catalog.read(catalog.select()) == old
    assert not keys.get_password(account.SERVICE, "active")
    new_token = "replacement-secret"
    key = catalog.add(replace(old, token=new_token, profile="d" * 32), "Диск A")
    assert key == old.profile
    assert index.matches(catalog.read(key), "черника", 1)[0]
    assert new_token not in catalog.file.read_text(encoding="utf-8")
    assert "test-only" not in catalog.file.read_text(encoding="utf-8")


def test_accounts_projects_and_interleaved_chats(owner, tmp_path, monkeypatch):
    class IdentityDisk:
        def __init__(self, http, token):
            self.token = token

        def meta(self, path):
            return {"path": path, "name": self.token}

    monkeypatch.setattr(server, "Disk", IdentityDisk)
    other = replace(
        owner, profile="b" * 32, uid="456", token="other-test", writable=False
    )
    catalog = account.Accounts()
    catalog.add(other, "Диск B")
    with pytest.raises(account.Problem, match="занято"):
        catalog.add(replace(other, uid="789", profile="c" * 32), "диск b")
    project = tmp_path / "Work"
    sub = project / "sub"
    sub.mkdir(parents=True)
    catalog.binding(project, owner.profile)
    work = server.make_server(project=sub)
    chat = server.make_server(project=tmp_path)
    args = {"path": "/same.txt"}
    assert [p["id"] for p in invoke(work, "list_accounts", {})["accounts"]] == [
        owner.profile
    ]
    assert len(invoke(chat, "list_accounts", {})["accounts"]) == 2
    catalog.use(other.profile)
    assert invoke(work, "get_metadata", args)["name"] == owner.token
    with pytest.raises(account.Problem, match="закреплён"):
        invoke(work, "get_metadata", {**args, "account": "Диск B"})
    with pytest.raises(account.Problem, match="несколько"):
        invoke(chat, "get_metadata", args)
    for selected in (owner, other, owner):
        assert (
            invoke(chat, "get_metadata", {**args, "account": selected.profile})["name"]
            == selected.token
        )
    with pytest.raises(account.Problem, match="чтение"):
        invoke(chat, "publish_link", {**args, "account": other.profile})
    app = server.make_server(True)
    staged = invoke(app, "get_metadata", args)
    with pytest.raises(account.Problem):
        web.approve(other, staged["approval_code"])
    monkeypatch.setattr("builtins.input", lambda _: "YES")
    web.approve(owner, staged["approval_code"])
    assert (
        invoke(app, "complete_request", {"ticket": staged["ticket"]})["name"]
        == owner.token
    )
    with pytest.raises(account.Problem):
        invoke(app, "get_metadata", {**args, "account": other.profile})
    catalog.remove(owner.profile)
    for srv in (work, app):
        with pytest.raises(account.Problem, match="отключён"):
            invoke(srv, "get_metadata", args)
    assert account.Accounts().read(other.profile) == other


def test_account_cli(owner, tmp_path, monkeypatch, capsys):
    import sys

    from ydisk_plugin import cli

    def run(*args):
        monkeypatch.setattr(sys, "argv", ["yandex-disk-plugin", *args])
        cli.main()
        return capsys.readouterr().out

    monkeypatch.chdir(tmp_path)
    other = replace(owner, profile="b" * 32, uid="456", name="Other")
    account.Accounts().add(other)
    assert "Диск A" in run("rename", "Test", "Диск A")
    assert "Other" in run("use", "other")
    assert "Other" in run("status")
    run("bind", "Диск A")
    assert "Диск A" in run("status")
    rows = run("accounts").splitlines()
    assert len(rows) == 2 and any("Диск проекта" in row for row in rows)
    run("unbind")
    assert "Other" in run("status")
    run("logout", "--account", "Other")
    assert "Диск A" in run("status")


def test_mcp_workspace_roots_select_project(owner, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from mcp import types

    other = replace(owner, profile="b" * 32, uid="456", name="Other")
    catalog = account.Accounts()
    catalog.add(other)
    project = tmp_path / "project"
    project.mkdir()
    catalog.binding(project, other.profile)
    session = Mock()
    roots = [types.Root(uri=project.as_uri())]
    session.list_roots = AsyncMock(return_value=types.ListRootsResult(roots=roots))
    app = server.make_server()
    monkeypatch.setattr(
        app,
        "get_context",
        lambda: SimpleNamespace(request_context=SimpleNamespace(session=session)),
    )
    assert [p["id"] for p in invoke(app, "list_accounts", {})["accounts"]] == [
        other.profile
    ]
    session.list_roots.return_value = types.ListRootsResult(
        roots=roots + [types.Root(uri=tmp_path.as_uri())]
    )
    with pytest.raises(account.Problem, match="разные привязки"):
        invoke(app, "list_accounts", {})
