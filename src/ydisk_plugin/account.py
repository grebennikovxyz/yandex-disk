"""The only component that reads persistent credentials."""

import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import sys
import time
import webbrowser
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from filelock import FileLock
from platformdirs import user_data_path

SERVICE = "org.grebennikov.yandex-disk"
CALLBACK = "http://127.0.0.1:53681/callback"
PUBLIC_CLIENT_ID = "572ac9bed1ad4651b5ac38a3503a18d7"
YANDEX_CODE = "https://oauth.yandex.ru/verification_code"
DATA = user_data_path("YandexDiskPlugin", "Grebennikov")
SCOPES = ("cloud_api:disk.read", "cloud_api:disk.info")


class Problem(Exception):
    """An error safe to return to the tool caller."""


def vault():
    if sys.platform == "win32":
        from keyring.backends.Windows import WinVaultKeyring as Backend
    elif sys.platform == "darwin":
        from keyring.backends.macOS import Keyring as Backend
    else:
        from keyring.backends.SecretService import Keyring as Backend
    return Backend()


@dataclass(frozen=True)
class Account:
    token: str
    uid: str
    name: str
    writable: bool
    profile: str
    expires: float

    @property
    def directory(self):
        return DATA / self.profile


def private_directory(path: Path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def account_lock():
    return FileLock(private_directory(DATA) / "account.lock", timeout=30)


def current():
    record = vault().get_password(SERVICE, "active")
    if not record:
        raise Problem("Сначала выполните yandex-disk-plugin login на своём компьютере.")
    account = Account(**json.loads(record))
    if account.expires and time.time() >= account.expires:
        raise Problem("Срок входа истёк. Повторите yandex-disk-plugin login.")
    return account


def clear():
    keyring = vault()
    record = keyring.get_password(SERVICE, "active")
    if record:
        account = Account(**json.loads(record))
        keyring.delete_password(SERVICE, "active")
        if account.directory.exists():
            shutil.rmtree(account.directory)


def authorization_code(client_id, scopes, verifier, manual, *, timeout=600):
    state = secrets.token_urlsafe(32)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    redirect = YANDEX_CODE if manual else CALLBACK
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect,
            "scope": " ".join(scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "force_confirm": "yes",
        }
    )
    url = "https://oauth.yandex.ru/authorize?" + query
    if manual:
        import getpass

        print("Войдите в Яндекс в браузере:", url)
        webbrowser.open(url)
        code = getpass.getpass("Код со страницы Яндекса (скрытый ввод): ").strip()
        if not code.isdigit():
            raise Problem("Яндекс не вернул числовой код.")
        return code
    outcome = {}
    local = urlsplit(CALLBACK)

    class Callback(BaseHTTPRequestHandler):
        timeout = 5

        def log_message(self, *args):
            pass

        def do_GET(self):
            parsed = urlsplit(self.path)
            params = parse_qs(parsed.query)
            if self.headers.get("Host") == local.netloc and self.path == "/start":
                self.send_response(302)
                self.send_header("Location", url)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                return
            if (
                self.headers.get("Host") != local.netloc
                or parsed.path != local.path
                or not secrets.compare_digest(params.get("state", [""])[0], state)
            ):
                self.send_error(400)
                return
            outcome.setdefault("code", params.get("code", [""])[0])
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(
                (
                    "Яндекс вернул ответ в локальное приложение. Результат входа появится в терминале. "
                    "Эта страница одноразовая. Вкладку можно закрыть."
                ).encode()
            )

    # Browsers can preconnect before the socket carrying GET /start is ready.
    with ThreadingHTTPServer((local.hostname, local.port), Callback) as server:
        server.timeout = 1
        start = "http://" + local.netloc + "/start"
        print("Войти через Яндекс:", start, flush=True)
        webbrowser.open(start)
        deadline = time.monotonic() + timeout
        while not outcome and time.monotonic() < deadline:
            server.handle_request()
    if not outcome.get("code"):
        raise Problem("Вход отменён или время ожидания истекло.")
    return outcome["code"]


def login(client_id, read_only=False, manual=False):
    from .disk import Disk

    client_id = client_id or os.getenv("YANDEX_DISK_CLIENT_ID") or PUBLIC_CLIENT_ID
    if not client_id:
        raise Problem(
            "Нужен публичный Client ID. Используйте login --client-id ID. См. README."
        )
    if not re.fullmatch("[a-zA-Z0-9_-]{1,128}", client_id):
        raise Problem("Некорректный Client ID.")
    # An unregistered redirect_uri can be ignored in favor of the first registered URI.
    with httpx.Client(trust_env=False, timeout=15) as preflight:
        info = preflight.get(f"https://oauth.yandex.ru/client/{client_id}/info")
        if info.status_code != 200:
            raise Problem("Яндекс не подтвердил настройки OAuth-приложения.")
        default_redirect = info.json().get("callback")
        if default_redirect not in (CALLBACK, YANDEX_CODE):
            raise Problem(
                f"Задайте первым Redirect URI {CALLBACK} или {YANDEX_CODE} в настройках Яндекса."
            )
        if default_redirect == YANDEX_CODE:
            manual = True
    verifier = secrets.token_urlsafe(64)
    scopes = SCOPES if read_only else (*SCOPES, "cloud_api:disk.write")
    code = authorization_code(client_id, scopes, verifier, manual)
    with httpx.Client(trust_env=False, timeout=30) as http:
        response = http.post(
            "https://oauth.yandex.ru/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "code_verifier": verifier,
            },
        )
        if response.status_code != 200:
            raise Problem(f"Яндекс отклонил вход (HTTP {response.status_code}).")
        data = response.json()
        token = data["access_token"]
        info = Disk(http, token).api("GET", "", fields="user")
    user = info.get("user", {})
    if not user.get("uid"):
        raise Problem("Яндекс не вернул идентификатор владельца Диска.")
    provided = data.get("scope")
    writable = not read_only and (
        provided is None or "cloud_api:disk.write" in provided.split()
    )
    lifetime = int(data.get("expires_in") or 0)
    account = Account(
        token,
        str(user["uid"]),
        user.get("display_name") or user.get("login") or "",
        writable,
        secrets.token_hex(16),
        time.time() + lifetime if lifetime else 0,
    )
    with account_lock():
        clear()
        vault().set_password(SERVICE, "active", json.dumps(asdict(account)))
    print(
        "Подключён Диск:",
        account.name,
        "·",
        "чтение и запись" if writable else "чтение",
    )
