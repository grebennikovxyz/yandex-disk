import asyncio
import base64
import hashlib
import io
import json
import os
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from ydisk_plugin import account, advanced


@pytest.fixture
def profile(tmp_path, monkeypatch):
    monkeypatch.setattr(account, "DATA", tmp_path / "profiles")
    return account.Account("synthetic-account", "123", "Example", True, "a" * 32, 0)


def test_extension_boundaries(profile):
    def call(mode, command, args, owner=profile):
        return asyncio.run(advanced.run(owner, mode, command, args))

    assert call("commands", "", {})["installed"] is False
    for mode, cmd, args in [
        ("read", "files update", {}),
        ("write", "mirrors add", {}),
        ("write", "api trash-resources-delete", {}),
        ("read", "files download", {"output": "/tmp/x"}),
        ("write", "api resources-delete", {"permanently": True}),
        ("write", "api resources-delete", {"--permanently": True}),
        ("write", "mirrors conflicts resolve", {"file": "private.txt"}),
        ("write", "mirrors sync", {"replace": True}),
        ("write", "files upload", {"file": "../report.txt"}),
    ]:
        with pytest.raises(account.Problem):
            call(mode, cmd, args)
    with pytest.raises(account.Problem, match="чтение"):
        call("write", "mirrors sync", {}, replace(profile, writable=False))
    with pytest.raises(account.Problem, match="extension install"):
        call("read", "status", {})


def test_profile_scoped_storage(profile, monkeypatch):
    root = advanced.installation() / "node_modules" / advanced.PACKAGE
    root.mkdir(parents=True)
    (root / "package.json").write_text(
        json.dumps({"name": advanced.PACKAGE, "version": advanced.VERSION})
    )
    monkeypatch.setattr(advanced, "node", lambda: "/runtime/node")
    monkeypatch.setenv("OPENAI_API_KEY", "unused-fixture")
    monkeypatch.setenv("NODE_OPTIONS", "unused-fixture")
    _, _, env, home = advanced.launch(profile)
    _, _, other, _ = advanced.launch(replace(profile, profile="b" * 32, writable=False))
    assert env["YDISK_TOKEN"] == profile.token
    assert not {"OPENAI_API_KEY", "NODE_OPTIONS"} & env.keys()
    assert other["YDISK_READ_ONLY"] == "1"
    for key in ("YDISK_CONFIG_DIR", "YDISK_STATE_DIR", "MESSAGING_STORE"):
        assert Path(env[key]).is_relative_to(home) and env[key] != other[key]
    assert (
        profile.token not in (Path(env["YDISK_CONFIG_DIR"]) / "config.json").read_text()
    )


@pytest.fixture
def local_engine(profile, tmp_path, monkeypatch):
    if not os.getenv("YDISK_TEST_ENGINE"):
        pytest.skip("optional installed WireCat engine")
    root = Path(os.environ["YDISK_TEST_ENGINE"])
    monkeypatch.setattr(advanced, "installation", lambda: root)
    launch = advanced.launch
    state = tmp_path / "disk.json"

    def fixture_launch(owner):
        runtime, entry, env, home = launch(owner)
        env.update(
            FIXTURE_DISK=str(state),
            FIXTURE_PROGRAM=entry,
        )
        return (
            runtime,
            str(Path(__file__).with_name("advanced_disk_fixture.mjs")),
            env,
            home,
        )

    monkeypatch.setattr(advanced, "launch", fixture_launch)

    def call(mode, command, **params):
        return asyncio.run(advanced.run(profile, mode, command, params))

    return state, call


def test_actual_engine_without_network(local_engine, profile, tmp_path):
    state, call = local_engine
    remote = "/Docs/note.txt"
    state.write_text(json.dumps({"/": None, "/Docs": None, remote: "original text"}))
    advanced.owner_command(profile, advanced.owner_words("folder-add", "/Docs"))
    call("sync", "sync")
    assert call("read", "status")["item"]["documents"] == 1
    hit = call("read", "search", query="original", exact=True)["item"]["hits"][0]
    assert hit["excerpt"] == "original text"
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    advanced.owner_command(profile, advanced.owner_words("mirror-add", "/Docs", mirror))
    mid = call("read", "mirrors list")["items"][0]["id"]
    call("read", "mirrors preview", id=mid)
    assert not (mirror / "note.txt").exists()
    call("write", "mirrors sync", id=mid)
    assert (mirror / "note.txt").read_text() == "original text"
    original = hashlib.sha256(b"original text").hexdigest()
    args = {"path": remote, "text": "updated text", "expected_fingerprint": original}
    assert call("preview", "files update", **args)["executed"] is False
    assert json.loads(state.read_bytes())[remote] == "original text"
    call("write", "files update", **args)
    assert json.loads(state.read_bytes())[remote] == "updated text"
    call("sync", "sync")
    assert "note.txt" in json.dumps(call("read", "changes", since="1d"))
    assert "original text" in json.dumps(
        call("read", "documents history", document=hit["id"], revision=1)
    )
    with pytest.raises(account.Problem):
        call("write", "files update", **args)
    assert original in json.dumps(call("read", "files history", path=remote))
    call(
        "write",
        "files restore",
        path=remote,
        version=original,
        expected_fingerprint=hashlib.sha256(b"updated text").hexdigest(),
    )
    assert json.loads(state.read_bytes())[remote] == "original text"
    # The two sides now diverge from the last successful mirror sync.
    (mirror / "note.txt").write_text("local edit")
    state.write_text(
        json.dumps({**json.loads(state.read_bytes()), remote: "disk edit"})
    )
    try:
        call("write", "mirrors sync", id=mid)
    except account.Problem as error:
        assert "mirror.partial" in str(error), str(error)
    conflicts = call("read", "mirrors conflicts list", id=mid)["items"]
    assert [entry["path"] for entry in conflicts] == ["note.txt"]
    conflict = call("read", "mirrors conflicts show", id=mid, path="note.txt")["item"]
    assert conflict["local"]["text"] == "local edit"
    assert conflict["remote"]["text"] == "disk edit"
    call("write", "mirrors conflicts resolve", id=mid, path="note.txt", keep="disk")
    assert (mirror / "note.txt").read_text() == "disk edit"
    assert call("read", "mirrors conflicts list", id=mid)["items"] == []


def test_office_originals_and_formula_protection(local_engine):
    from docx import Document
    from openpyxl import Workbook, load_workbook

    state, call = local_engine
    doc_path = "/Docs/example.docx"
    sheet_path = "/Docs/example.xlsx"
    document = Document()
    document.add_paragraph("Original paragraph")
    document.add_paragraph("Second paragraph")
    workbook = Workbook()
    workbook.active["A1"] = "=2+2"
    files = {"/": None, "/Docs": None}
    for path, obj in [(doc_path, document), (sheet_path, workbook)]:
        stream = io.BytesIO()
        obj.save(stream)
        files[path] = {"base64": base64.b64encode(stream.getvalue()).decode()}
    state.write_text(json.dumps(files))

    def stored(path):
        return io.BytesIO(
            base64.b64decode(json.loads(state.read_bytes())[path]["base64"])
        )

    item = call("read", "files documents show", path=doc_path)["item"]
    edits = [{"index": item["paragraphs"][0]["index"], "text": "Revised paragraph"}]
    call(
        "write",
        "files documents update",
        path=doc_path,
        payload=json.dumps(edits),
        expected_fingerprint=item["sha256"],
    )
    assert [p.text for p in Document(stored(doc_path)).paragraphs] == [
        "Revised paragraph",
        "Second paragraph",
    ]

    with pytest.raises(account.Problem, match="edit_office"):
        call("read", "files sheets show", path=sheet_path)
    # Model the equivalent package-relative relationships emitted by Excel.
    excel = io.BytesIO()
    with zipfile.ZipFile(stored(sheet_path)) as src, zipfile.ZipFile(excel, "w") as dst:
        for entry in src.infolist():
            data = src.read(entry)
            if entry.filename == "xl/_rels/workbook.xml.rels":
                data = data.replace(b'Target="/xl/worksheets/', b'Target="worksheets/')
            dst.writestr(entry, data)
    files = json.loads(state.read_bytes())
    files[sheet_path] = {"base64": base64.b64encode(excel.getvalue()).decode()}
    state.write_text(json.dumps(files))
    item = call("read", "files sheets show", path=sheet_path, range="Sheet!A1:B2")[
        "item"
    ]
    changes = {
        "path": sheet_path,
        "values": "[[3,4]]",
        "expected_fingerprint": item["sha256"],
    }
    with pytest.raises(account.Problem, match="formula"):
        call("write", "files sheets update", range="Sheet!A1", **changes)
    assert hashlib.sha256(stored(sheet_path).read()).hexdigest() == item["sha256"]
    call("write", "files sheets update", range="Sheet!A2:B2", **changes)
    sheet = load_workbook(stored(sheet_path)).active
    assert (sheet["A1"].value, sheet["A2"].value, sheet["B2"].value) == ("=2+2", 3, 4)
