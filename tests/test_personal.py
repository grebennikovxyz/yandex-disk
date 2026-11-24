import asyncio
import hashlib
import io
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from ydisk_plugin import account, index, server, web
from ydisk_plugin.disk import YANDEX, Disk, path_of, safe_host
from ydisk_plugin.office import OfficeContent, create


@pytest.fixture
def owner(monkeypatch, tmp_path):
    monkeypatch.setattr(account, "DATA", tmp_path)
    a = account.Account("test-only", "123", "Test", True, "profile-a", 0)
    monkeypatch.setattr(server, "current", lambda: a)
    return a


class MemoryDisk:
    def __init__(self):
        self.items = {
            "/Docs/Заказ.docx": create(".docx", OfficeContent(paragraphs=["черника"]))
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


def test_search_fetch_conflicting_edit(owner, monkeypatch):
    disk = MemoryDisk()
    monkeypatch.setattr(server, "Disk", lambda *_: disk)
    assert index.rebuild(owner, disk, ["/Docs"])["indexed"] == 1
    app = server.make_server()
    hit = invoke(app, "search", {"query": "черника"})["results"][0]
    assert hit["matched"] == "content"
    file = invoke(app, "fetch", {"id": hit["id"]})
    assert "черника" in file["text"]
    disk.items["/Docs/Заказ.docx"] = create(
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
    monkeypatch.setattr(server, "current", lambda: replace(owner, writable=False))
    staged = invoke(app, "fetch", {"id": "/Docs/Заказ.docx"})
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
                assert desc["save_attachment"].meta["openai/fileParams"] == ["file"]

    asyncio.run(run())
