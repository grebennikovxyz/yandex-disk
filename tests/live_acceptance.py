"""Opt-in live check. Authorize in your own browser; only fresh synthetic files are modified."""

import asyncio
import json
import logging
import secrets
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from ydisk_plugin import account, index
from ydisk_plugin.disk import Disk
from ydisk_plugin.server import make_server


async def check(owner):
    app = make_server()

    async def call(name, **kwargs):
        result = await app.call_tool(name, kwargs)
        return json.loads(result[0].text)

    folder = "/Yandex-Disk-Plugin-QA-" + secrets.token_hex(6)
    await call("manage_file", action="mkdir", path=folder)
    print("OK: private test directory created", flush=True)
    try:
        for name, content in [
            ("sample.docx", {"paragraphs": ["qa-marker-words"]}),
            ("sample.xlsx", {"sheets": {"Sheet1": [["qa-marker-words"]]}}),
            ("sample.pptx", {"slides": [{"title": "qa-marker-words"}]}),
        ]:
            path = folder + "/" + name
            await call("create_office", path=path, content=content)
            item = await call("read_office", path=path)
            record = next(r for r in item["records"] if r["text"] == "qa-marker-words")
            result = await call(
                "edit_office",
                path=path,
                sha256=item["sha256"],
                edits=[{"target": record["target"], "value": "qa-edited"}],
            )
            assert (
                "qa-edited"
                in (await call("fetch", id=result["edited_copy"]["id"]))["text"]
            )
            assert "qa-marker-words" in (await call("fetch", id=path))["text"]
            print("OK: create, edit copy, reopen", name, flush=True)
        with httpx.Client(trust_env=False, timeout=45) as client:
            index.rebuild(owner, Disk(client, owner.token), [folder])
        search = await call("search", query="qa-marker-words", limit=1)
        assert search["results"][0]["matched"] == "content"
        path = folder + "/sample.docx"
        assert (await call("publish_link", path=path)).get("public_url")
        await call("remove_public_link", path=path)
        with httpx.Client(trust_env=False, timeout=45) as client:
            disk = Disk(client, owner.token)
            assert not disk.api(
                "GET", "/resources", path=path, fields="public_url"
            ).get("public_url")
            await call("manage_file", action="trash", path=path)
            offset = 0
            while True:
                listing = disk.api(
                    "GET", "/trash/resources", path="trash:/", limit=100, offset=offset
                )["_embedded"]
                hit = next(
                    (
                        x
                        for x in listing["items"]
                        if x.get("origin_path") in (path, "disk:" + path)
                    ),
                    None,
                )
                if hit:
                    break
                offset += len(listing["items"])
                if offset >= listing["total"] or not listing["items"]:
                    raise RuntimeError("Test file was not found in Trash.")
            await call("restore_from_trash", trash_path=hit["path"])
        assert (await call("get_metadata", path=path))["type"] == "file"
        print("OK: content search, link revoked, restored from Trash", flush=True)
    finally:
        await call("manage_file", action="trash", path=folder)
        print("Test directory moved to Trash.", flush=True)


def main():
    logging.disable(logging.CRITICAL)
    account.SERVICE += ".qa." + secrets.token_hex(8)
    with TemporaryDirectory() as tmp:
        account.DATA = Path(tmp)
        if "--no-browser" in sys.argv:
            account.webbrowser.open = lambda _: True
        try:
            account.login(None)
            asyncio.run(check(account.current()))
        finally:
            account.clear()


if __name__ == "__main__":
    main()
