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
from dataclasses import asdict, dataclass, replace
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
    login: str = ""

    @property
    def directory(self):
        return DATA / self.profile


def private_directory(path: Path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def account_lock():
    return FileLock(private_directory(DATA) / "account.lock", timeout=30)


class Accounts:
    """Local routing metadata. Callers hold account_lock for the entire operation."""

    def __init__(self):
        self.file = private_directory(DATA) / "accounts.json"
        self.keys = vault()
        if self.file.exists():
            self.data = json.loads(self.file.read_bytes())
        else:
            self.data = {"profiles": {}, "projects": {}, "default": None, "web": None}
        self.profiles = self.data["profiles"]
        legacy = self.keys.get_password(SERVICE, "active")
        if legacy:
            owner = Account(**json.loads(legacy))
            if owner.profile not in self.profiles or not self.keys.get_password(
                f"{SERVICE}.{owner.profile}", "account"
            ):
                self.add(owner)
            self.keys.delete_password(SERVICE, "active")

    def save(self):
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.file)

    def lookup(self, selector):
        if isinstance(selector, str):
            for key, info in self.profiles.items():
                if (
                    key == selector
                    or info["alias"].casefold() == selector.strip().casefold()
                ):
                    return key
        raise Problem("Диск не найден. Посмотрите доступные имена в accounts.")

    def unique_name(self, name, key):
        name = name.strip()
        if (
            not name
            or len(name) > 100
            or not name.isprintable()
            or re.fullmatch("[0-9a-fA-F]{32}", name)
        ):
            raise Problem("Укажите читаемое имя Диска длиной до 100 символов.")
        if any(
            k != key and p["alias"].casefold() == name.casefold()
            for k, p in self.profiles.items()
        ):
            raise Problem("Это имя уже занято другим Диском.")
        return name

    def add(self, owner, alias=None):
        key = next(
            (k for k, p in self.profiles.items() if p["uid"] == owner.uid),
            owner.profile,
        )
        if not re.fullmatch("[0-9a-f]{32}", key):
            raise Problem("Некорректный идентификатор локального профиля.")
        previous = self.profiles.get(key, {})
        name = (
            alias
            if alias is not None
            else previous.get("alias") or owner.login or owner.name or owner.uid
        )
        if (
            alias is None
            and not previous
            and any(
                p["alias"].casefold() == name.casefold() for p in self.profiles.values()
            )
        ):
            name = f"{name} ({owner.uid})"
        name = self.unique_name(name, key)
        owner = replace(owner, profile=key)
        self.profiles[key] = {"alias": name, "uid": owner.uid}
        if not self.data["default"]:
            self.data["default"] = key
        if not self.data["web"]:
            self.data["web"] = key
        # Reserve the ID first. A failed vault write can then be retried by UID.
        self.save()
        self.keys.set_password(f"{SERVICE}.{key}", "account", json.dumps(asdict(owner)))
        return key

    def bound(self, project):
        path = Path(project).resolve()
        for parent in (path, *path.parents):
            key = self.data["projects"].get(os.path.normcase(str(parent)))
            if key:
                return key
        return None

    def binding(self, project, key):
        path = Path(project).resolve()
        if not path.is_dir():
            raise Problem("Для привязки нужна папка проекта.")
        location = os.path.normcase(str(path))
        if key:
            self.data["projects"][location] = key
        elif self.data["projects"].pop(location, None) is None:
            raise Problem("У этой папки нет собственной привязки.")
        self.save()

    def use(self, key):
        self.read(key)
        self.data["default"] = key
        self.save()

    def select(self, selector=None, *, project=None, pin=None, explicit=False):
        bound = self.bound(project) if project is not None else None
        if bound and bound not in self.profiles:
            raise Problem(
                "Диск проекта отключён. Обновите привязку командой bind или unbind."
            )
        if pin and pin not in self.profiles:
            raise Problem("Закреплённый Диск отключён. Настройте подключение заново.")
        if bound and pin and bound != pin:
            raise Problem("У сервера и проекта закреплены разные Диски.")
        fixed = bound or pin
        if selector is not None:
            key = self.lookup(selector)
            if fixed and key != fixed:
                raise Problem("Для этого подключения закреплён другой Диск.")
            return key
        if not fixed and explicit and len(self.profiles) > 1:
            raise Problem(
                "Подключено несколько Дисков. Вызовите list_accounts и передайте account в запросе."
            )
        key = fixed or self.data["default"]
        if not key and len(self.profiles) == 1:
            key = next(iter(self.profiles))
        if not key:
            raise Problem("Выберите Диск через use или подключите его через login.")
        return key

    def read(self, key, *, allow_expired=False):
        record = self.keys.get_password(f"{SERVICE}.{key}", "account")
        if not record:
            raise Problem("Для этого Диска нужен повторный вход через login.")
        owner = Account(**json.loads(record))
        if owner.profile != key or owner.uid != self.profiles[key]["uid"]:
            raise Problem("Владелец сохранённого входа не совпадает с профилем.")
        if not allow_expired and owner.expires and time.time() >= owner.expires:
            raise Problem("Срок входа истёк. Повторите login для этого аккаунта.")
        return owner

    def describe(self, key):
        try:
            owner = self.read(key, allow_expired=True)
        except Problem:
            return {
                "id": key,
                **self.profiles[key],
                "owner": "",
                "login": "",
                "writable": None,
                "expired": True,
                "default": key == self.data["default"],
            }
        return {
            "id": key,
            "alias": self.profiles[key]["alias"],
            "owner": owner.name,
            "login": owner.login,
            "uid": owner.uid,
            "writable": owner.writable,
            "expired": bool(owner.expires and time.time() >= owner.expires),
            "default": key == self.data["default"],
        }

    def remove(self, key):
        if key not in self.profiles or not re.fullmatch("[0-9a-f]{32}", key):
            raise Problem("Диск не найден.")
        if self.keys.get_password(f"{SERVICE}.{key}", "account"):
            self.keys.delete_password(f"{SERVICE}.{key}", "account")
        folder = DATA / key
        if folder.exists():
            shutil.rmtree(folder)
        del self.profiles[key]
        if self.data["default"] == key:
            self.data["default"] = (
                next(iter(self.profiles)) if len(self.profiles) == 1 else None
            )
        # Retain project and tunnel pins. Removing a profile must never redirect them.
        self.save()


def current():
    with account_lock():
        accounts = Accounts()
        return accounts.read(accounts.select(project=Path.cwd()))


def clear():
    with account_lock():
        accounts = Accounts()
        if accounts.profiles:
            accounts.remove(accounts.select(project=Path.cwd()))


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


def login(client_id, read_only=False, manual=False, alias=None):
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
        user.get("login") or "",
    )
    with account_lock():
        accounts = Accounts()
        if alias is not None:
            existing = next(
                (p for p in accounts.profiles.values() if p["uid"] == account.uid), None
            )
            if existing and existing["alias"].casefold() != alias.strip().casefold():
                raise Problem(
                    f"Яндекс вернул уже подключённый Диск «{existing['alias']}». "
                    "Выберите другую учётную запись у Яндекса или используйте rename для смены названия."
                )
        key = accounts.add(account, alias)
        name = accounts.profiles[key]["alias"]
    print(
        "Подключён Диск:",
        name,
        "·",
        account.login or account.name,
        "·",
        "чтение и запись" if writable else "чтение",
    )
    return name
