"""Two-channel approval for a tunnel that carries no authenticated end-user identity over stdio."""

import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import closing

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .account import Problem, private_directory

TTL = 300


def database(account):
    conn = sqlite3.connect(
        private_directory(account.directory) / "requests.sqlite3", timeout=10
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS requests "
        "(ticket TEXT PRIMARY KEY, code TEXT UNIQUE, payload BLOB, preview TEXT, expires REAL, approved INTEGER)"
    )
    return conn


def stage(account, tool, args):
    key = secrets.token_bytes(32)
    ticket = key.hex()
    nonce = secrets.token_bytes(12)
    body = json.dumps([tool, args], ensure_ascii=False, allow_nan=False).encode()
    if len(body) > 2_000_000:
        raise Problem("Аргументы запроса слишком велики.")
    payload = nonce + AESGCM(key).encrypt(nonce, body, account.profile.encode())
    code = secrets.token_hex(5).upper()
    # The ticket holds the decryption key. Only its digest is persisted locally.
    preview = {"tool": tool}
    for k in (
        "path",
        "id",
        "destination",
        "action",
        "query",
        "reason",
        "filename",
        "command",
    ):
        if k in args and isinstance(args[k], str):
            preview[k] = args[k][:300]
    if isinstance(args.get("parameters"), dict):
        params = args["parameters"]
        if len(params) > 50 or any(len(k) > 80 for k in params):
            raise Problem("Слишком много параметров или слишком длинное имя параметра.")
        preview["parameters"] = {
            k: v
            if isinstance(v, (bool, int))
            or (
                k
                in {
                    "path",
                    "file",
                    "folder",
                    "from",
                    "id",
                    "keep",
                    "range",
                    "version",
                    "expected_fingerprint",
                }
                and isinstance(v, str)
                and len(v) <= 300
            )
            else {
                "sha256": hashlib.sha256(
                    json.dumps(v, ensure_ascii=False).encode()
                ).hexdigest()
            }
            for k, v in params.items()
        }
    if "edits" in args:
        preview["targets"] = [
            str(e.get("target", ""))[:80]
            for e in args["edits"][:20]
            if isinstance(e, dict)
        ]
    with closing(database(account)) as conn, conn:
        conn.execute("DELETE FROM requests WHERE expires<=?", (time.time(),))
        if conn.execute("SELECT count(*) FROM requests").fetchone()[0] >= 100:
            raise Problem("Очередь подтверждений заполнена.")
        conn.execute(
            "INSERT INTO requests VALUES (?,?,?,?,?,0)",
            (
                hashlib.sha256(ticket.encode()).hexdigest(),
                code,
                payload,
                json.dumps(preview, ensure_ascii=False),
                time.time() + TTL,
            ),
        )
    return {
        "approval_code": code,
        "ticket": ticket,
        "expires_seconds": TTL,
        "tool": tool,
        "instruction": "Покажите владельцу код. После локального подтверждения вызовите complete_request "
        "с билетом этого запроса.",
    }


def approve(account, code):
    with closing(database(account)) as conn, conn:
        row = conn.execute(
            "SELECT preview FROM requests WHERE code=? AND expires>? AND approved=0",
            (code.upper(), time.time()),
        ).fetchone()
        if not row:
            raise Problem("Код отсутствует, уже подтверждён или просрочен.")
        # ascii() escapes control characters in attacker-supplied filenames.
        print("Сверьте код с запросом в своём чате:", code.upper())
        print(json.dumps(json.loads(row[0]), ensure_ascii=True))
        if input("Разрешить эту операцию? Введите YES: ").strip() != "YES":
            raise Problem("Подтверждение отменено.")
        conn.execute("UPDATE requests SET approved=1 WHERE code=?", (code.upper(),))


def consume(account, ticket):
    with closing(database(account)) as conn, conn:
        row = conn.execute(
            "DELETE FROM requests WHERE ticket=? AND approved=1 AND expires>? RETURNING payload",
            (hashlib.sha256(ticket.encode()).hexdigest(), time.time()),
        ).fetchone()
        if row is None:
            raise Problem(
                "Нужен действующий билет, локально подтверждённый именно для этого запроса."
            )
        payload = row[0]
        return json.loads(
            AESGCM(bytes.fromhex(ticket)).decrypt(
                payload[:12], payload[12:], account.profile.encode()
            )
        )
