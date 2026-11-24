import hashlib
import posixpath
import time
from pathlib import PurePosixPath
from urllib.parse import quote, urljoin, urlsplit

from .account import Problem

API = "https://cloud-api.yandex.net/v1/disk"
CONTENT_LIMIT = 32 * 1024 * 1024
FIELDS = "name,path,type,size,mime_type,modified,revision,sha256,resource_id"
YANDEX = ("yandex.ru", "yandex.net", "yandex.com", "yandexcloud.net")
OPENAI_FILES = ("oaiusercontent.com", "files.openai.com")


def path_of(value):
    if value.startswith("disk:/"):
        value = value[5:]
    if not value.startswith("/"):
        value = "/" + value
    if ".." in value.split("/") or "\\" in value or any(ord(c) < 32 for c in value):
        raise Problem("Недопустимый путь на Диске.")
    return "/" + str(PurePosixPath(value)).lstrip("/")


def disk_url(path):
    return "https://disk.yandex.ru/client/disk" + quote(path_of(path))


def safe_host(url, domains):
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    if (
        p.scheme != "https"
        or p.username is not None
        or p.password is not None
        or p.port not in (443, None)
        or not any(host == d or host.endswith("." + d) for d in domains)
    ):
        raise Problem("Отклонён неожиданный адрес передачи файла.")
    return url


class Disk:
    def __init__(self, http, token):
        self.http = http
        self.token = token

    @staticmethod
    def check(response):
        if not response.is_success:
            messages = {
                401: "Повторите вход в Яндекс.",
                403: "Недостаточно прав.",
                404: "Объект не найден.",
                409: "Конфликт имён или путей.",
                412: "Версия изменилась.",
                429: "Превышен лимит запросов.",
                507: "Диск заполнен.",
            }
            raise Problem(
                f"HTTP {response.status_code}. "
                + messages.get(response.status_code, "Операция не завершена.")
            )

    def api(self, method, endpoint, *, body=None, **params):
        r = self.http.request(
            method,
            API + endpoint,
            params=params,
            headers={"Authorization": "OAuth " + self.token},
            json=body,
        )
        self.check(r)
        if r.status_code == 202:
            target = r.json().get("href", "")
            if not target.startswith(API + "/operations/"):
                raise Problem("Яндекс вернул неизвестный адрес операции.")
            for delay in (0.1, 0.3, 0.6, 1, 2, 3, 4, 5, 8):
                time.sleep(delay)
                status = self.http.get(
                    target, headers={"Authorization": "OAuth " + self.token}
                )
                self.check(status)
                state = status.json().get("status")
                if state == "success":
                    return {}
                if state == "failed":
                    raise Problem("Яндекс сообщил об ошибке фоновой операции.")
            raise Problem(
                "Операция ещё выполняется. Проверьте состояние объекта перед повтором."
            )
        return r.json() if r.content else {}

    def meta(self, path):
        return self.api("GET", "/resources", path=path_of(path), fields=FIELDS)

    def page(self, path, offset, limit):
        data = self.api(
            "GET",
            "/resources",
            path=path_of(path),
            offset=offset,
            limit=limit,
            fields="type,_embedded.total,"
            + ",".join("_embedded.items." + s for s in FIELDS.split(",")),
        )
        if data.get("type") != "dir":
            raise Problem("Ожидалась папка.")
        listing = data.get("_embedded", {})
        items = listing.get("items", [])
        return items, offset + len(items) < listing.get("total", 0)

    def files(self, root):
        if path_of(root) == "/":
            offset = 0
            while True:
                data = self.api(
                    "GET",
                    "/resources/files",
                    limit=200,
                    offset=offset,
                    fields=",".join("items." + s for s in FIELDS.split(",")),
                )
                items = data.get("items", [])
                yield from items
                if len(items) < 200:
                    break
                offset += len(items)
        else:
            dirs = [path_of(root)]
            while dirs:
                folder = dirs.pop()
                offset = 0
                while True:
                    items, more = self.page(folder, offset, 200)
                    for item in items:
                        if item["type"] == "dir":
                            dirs.append(item["path"])
                        else:
                            yield item
                    if not more or not items:
                        break
                    offset += len(items)

    def stream(self, url, dest, domains=YANDEX, limit=None):
        for _ in range(5):
            with self.http.stream("GET", safe_host(url, domains)) as r:
                if r.is_redirect and "location" in r.headers:
                    url = urljoin(url, r.headers["location"])
                    continue
                self.check(r)
                count = 0
                digest = hashlib.sha256()
                for chunk in r.iter_bytes(65536):
                    count += len(chunk)
                    if limit is not None and count > limit:
                        raise Problem("Превышен предел размера для этой операции.")
                    dest.write(chunk)
                    digest.update(chunk)
                return digest.hexdigest()
        raise Problem("Слишком много перенаправлений.")

    def download(self, path, dest, limit=None):
        link = self.api("GET", "/resources/download", path=path_of(path))
        return self.stream(link["href"], dest, limit=limit)

    def upload(self, path, data):
        path = path_of(path)
        link = self.api("GET", "/resources/upload", path=path, overwrite="false")
        url = safe_host(link["href"], YANDEX)
        r = self.http.put(url, content=data)
        self.check(r)
        return self.meta(path)

    def change(self, action, path, destination=None):
        path = path_of(path)
        if path == "/":
            raise Problem("Выберите объект внутри Диска.")
        if action in ("copy", "move"):
            if not destination:
                raise Problem("Нужен путь назначения.")
            return self.api(
                "POST",
                "/resources/" + action,
                **{"from": path, "path": path_of(destination), "overwrite": "false"},
            )
        if action == "mkdir":
            return self.api("PUT", "/resources", path=path)
        if action == "trash":
            return self.api("DELETE", "/resources", path=path, permanently="false")
        if action in ("publish", "unpublish"):
            self.api(
                "PUT",
                "/resources/" + action,
                path=path,
                body={} if action == "publish" else None,
            )
            if action == "publish":
                return self.api("GET", "/resources", path=path, fields="public_url")
            return {}
        raise Problem("Неизвестная операция.")


def new_version_path(path, suffix):
    stem, ext = posixpath.splitext(path_of(path))
    return f"{stem} (ред. {suffix}){ext}"
