import io
import json
import sqlite3
import time
from contextlib import closing
from zipfile import BadZipFile

from lxml.etree import XMLSyntaxError
from pypdf.errors import PdfReadError

from .account import Problem, private_directory
from .disk import CONTENT_LIMIT, disk_url, path_of
from .office import MAX_TEXT, SUPPORTED, extension, records


def db(account):
    path = private_directory(account.directory) / "search.sqlite3"
    con = sqlite3.connect(path)
    con.execute("PRAGMA secure_delete=ON")
    con.execute("CREATE TABLE IF NOT EXISTS settings(roots TEXT, updated REAL)")
    con.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(path UNINDEXED, sha UNINDEXED, body)"
    )
    return con


def rebuild(account, disk, roots):
    roots = sorted({path_of(root) for root in roots})
    roots = [
        root
        for root in roots
        if not any(
            root.startswith(parent.rstrip("/") + "/") and root != parent
            for parent in roots
        )
    ]
    indexed = skipped = truncated = 0
    with closing(db(account)) as con, con:
        con.execute("DELETE FROM docs")
        for root in roots:
            if disk.meta(root)["type"] != "dir":
                raise Problem("Выберите папку для индекса.")
            for item in disk.files(root):
                ext = extension(item["path"])
                if ext not in SUPPORTED or item.get("size", 0) > CONTENT_LIMIT:
                    skipped += 1
                    continue
                content = io.BytesIO()
                digest = disk.download(item["path"], content, CONTENT_LIMIT)
                if item.get("sha256") and digest != item["sha256"]:
                    skipped += 1
                    continue
                parts = []
                size = 0
                try:
                    for record in records(ext, content.getvalue()):
                        value = record["text"] + "\n"
                        parts.append(value[: MAX_TEXT - size])
                        size += len(value)
                        if size >= MAX_TEXT:
                            truncated += 1
                            break
                except (
                    ValueError,
                    KeyError,
                    OSError,
                    RuntimeError,
                    Problem,
                    EOFError,
                    BadZipFile,
                    PdfReadError,
                    XMLSyntaxError,
                ):
                    skipped += 1
                    continue
                con.execute(
                    "INSERT INTO docs VALUES (?,?,?)",
                    (path_of(item["path"]), digest, "".join(parts)),
                )
                indexed += 1
        con.execute("DELETE FROM settings")
        con.execute(
            "INSERT INTO settings VALUES (?,?)", (json.dumps(roots), time.time())
        )
    return {
        "folders": roots,
        "indexed": indexed,
        "skipped": skipped,
        "truncated": truncated,
    }


def matches(account, query, limit):
    expression = " AND ".join('"' + w.replace('"', '""') + '"' for w in query.split())
    if not expression:
        raise Problem("Введите поисковый запрос.")
    with closing(db(account)) as con:
        config = con.execute("SELECT roots,updated FROM settings").fetchone()
        rows = con.execute(
            "SELECT path,sha,snippet(docs,2,'[',']','…',18) FROM docs "
            "WHERE docs MATCH ? ORDER BY rank LIMIT ?",
            (expression, limit),
        ).fetchall()
    return rows, json.loads(config[0]) if config else [], config[1] if config else None


def cached_page(account, path, offset):
    path = path_of(path)
    size = 24000
    with closing(db(account)) as con:
        row = con.execute(
            "SELECT substr(body, ?, ?), length(body) FROM docs WHERE path = ?",
            (offset + 1, size, path),
        ).fetchone()
        if row is None:
            raise Problem("Документа нет в локальном индексе этого Диска.")
        updated = con.execute("SELECT updated FROM settings").fetchone()
    return {
        "id": "disk:" + path,
        "url": disk_url(path),
        "text": row[0],
        "offline": True,
        "indexed_at": updated[0] if updated else None,
        "next_offset": offset + size if offset + size < row[1] else None,
    }
