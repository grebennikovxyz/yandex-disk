import argparse
import json
import logging
import os
import shutil
import subprocess
from importlib.metadata import version
from pathlib import Path

import httpx

from . import account, web
from .account import Problem
from .disk import Disk, path_of

PRODUCT_NAME = "Plaugin for Ya-Di"
GUIDE = "https://developers.openai.com/api/docs/guides/secure-mcp-tunnels"


def print_account(info, bound=False):
    parts = [
        info["alias"],
        info["login"] or info["owner"],
        f"ID Яндекса {info['uid']}",
        {True: "чтение и запись", False: "только чтение", None: "нет входа"}[
            info["writable"]
        ],
    ]
    if info["expired"]:
        parts.append("нужен повторный вход")
    if info["default"]:
        parts.append("по умолчанию")
    if bound:
        parts.append("Диск проекта")
    print(" · ".join(parts), f"[{info['id']}]")


def connect_window(client_id):
    import tkinter as tk
    from tkinter import messagebox, ttk

    root = tk.Tk()
    root.title(PRODUCT_NAME)
    assets = Path(__file__).with_name("assets")
    root.iconphoto(True, tk.PhotoImage(file=assets / "plaugin-window.png"))
    frame = tk.Frame(root, padx=24, pady=18)
    frame.pack()
    tk.Label(frame, text=PRODUCT_NAME, font=("", 15)).pack(anchor="w")
    tk.Label(
        frame,
        text="Неофициальный ИИ-плагин. Использует API Яндекс Диска.\n"
        "Товарный знак «Яндекс» принадлежит ООО «ЯНДЕКС».\n\n"
        "Вход проходит на странице Яндекса. Токен хранится в системе.\n"
        "OpenAI получает результаты, которые вы запрашиваете в чате.",
        justify="left",
    ).pack(pady=12, anchor="w")
    with account.account_lock():
        accounts = account.Accounts()
        choices = {p["alias"]: k for k, p in accounts.profiles.items()}
        default = accounts.profiles.get(accounts.data["default"], {}).get("alias", "")
    if choices:
        tk.Label(frame, text="Диск по умолчанию для терминала").pack(anchor="w")
        choice = tk.StringVar(value=default)
        ttk.Combobox(
            frame, textvariable=choice, values=list(choices), state="readonly"
        ).pack(fill="x")

        def use_selected():
            try:
                with account.account_lock():
                    saved = account.Accounts()
                    saved.use(choices[choice.get()])
            except (KeyError, Problem):
                messagebox.showerror(PRODUCT_NAME, "Выберите подключённый Диск.")
                return
            messagebox.showinfo(PRODUCT_NAME, f"Диск по умолчанию: {choice.get()}")
            root.destroy()

        ttk.Button(
            frame, text="Использовать выбранный Диск", command=use_selected
        ).pack(pady=4)
    tk.Label(frame, text="Название добавляемого Диска (по желанию)").pack(anchor="w")
    alias = tk.StringVar()
    ttk.Entry(frame, textvariable=alias).pack(fill="x")
    client = tk.StringVar(
        value=client_id
        or os.getenv("YANDEX_DISK_CLIENT_ID")
        or account.PUBLIC_CLIENT_ID
    )
    tk.Label(frame, text="Публичный Client ID приложения").pack(anchor="w")
    tk.Entry(frame, textvariable=client, width=48).pack(fill="x")
    read_only = tk.BooleanVar(value=False)
    tk.Checkbutton(frame, text="Только чтение", variable=read_only).pack(
        anchor="w", pady=8
    )

    def start():
        try:
            name = account.login(
                client.get(), read_only.get(), alias=alias.get().strip() or None
            )
        except Exception as e:  # noqa: BLE001 - never show OAuth URLs in a GUI error
            messagebox.showerror(
                "Вход",
                str(e) if isinstance(e, Problem) else "Не удалось завершить вход.",
            )
            return
        messagebox.showinfo(PRODUCT_NAME, f"Подключён Диск: {name}")
        root.destroy()

    service_logo = tk.PhotoImage(file=assets / "yandex-disk-service.png")
    tk.Button(
        frame,
        text="Подключить через Яндекс ID",
        image=service_logo,
        compound="top",
        bg="white",
        fg="black",
        padx=12,
        pady=6,
        command=start,
    ).pack(pady=8)
    root.mainloop()


def setup_web(tunnel_id, pin=None):
    executable = shutil.which("tunnel-client")
    if not executable:
        raise Problem(
            "Установите официальный tunnel-client из настроек OpenAI Platform. " + GUIDE
        )
    if not tunnel_id.startswith("tunnel_") or not tunnel_id.replace("_", "").isalnum():
        raise Problem("Введите действительный tunnel_id из OpenAI Platform.")
    if not shutil.which("yandex-disk-plugin"):
        raise Problem("Добавьте yandex-disk-plugin в PATH.")
    result = subprocess.run(
        [
            executable,
            "init",
            "--sample",
            "sample_mcp_stdio_local",
            "--profile",
            "yandex-disk-personal",
            "--tunnel-id",
            tunnel_id,
            "--mcp-command",
            "yandex-disk-plugin serve-web" + (f" --account {pin}" if pin else ""),
            "--health-listen-addr",
            "127.0.0.1:0",
        ],
        check=False,
    )
    if result.returncode:
        raise Problem(
            "tunnel-client не создал профиль. Проверьте права и версию клиента."
        )
    if subprocess.run(
        [executable, "doctor", "--profile", "yandex-disk-personal", "--explain"],
        check=False,
    ).returncode:
        raise Problem(
            "Диагностика туннеля не прошла. Проверьте настройки OpenAI Platform."
        )
    print("Запустите: tunnel-client run --profile yandex-disk-personal")
    print(
        "В ChatGPT → Plugins → Add custom MCP server → Tunnel выберите этот личный туннель."
    )


def main():
    # HTTPX includes full download URLs at INFO; protocol diagnostics must stay free of secrets.
    logging.disable(logging.CRITICAL)
    p = argparse.ArgumentParser(
        prog="yandex-disk-plugin",
        description=(
            f"{PRODUCT_NAME} — неофициальный ИИ-плагин для работы с Яндекс Диском. "
            "Исключительное право на товарный знак «Яндекс» принадлежит ООО «ЯНДЕКС»."
        ),
    )
    p.add_argument(
        "--version",
        action="version",
        version="%(prog)s " + version("grebennikov-yandex-disk"),
    )
    sub = p.add_subparsers(dest="action", required=True)
    ext = sub.add_parser(
        "extension", help="Локальный модуль истории, правок и синхронизации"
    )
    ext.add_argument("--account", help="Название или ID Диска")
    ext.add_argument(
        "--timeout",
        type=int,
        default=45,
        help="Время вызова движка в секундах (1–3600)",
    )
    actions = ext.add_subparsers(dest="operation", required=True)
    actions.add_parser("install", help="Установить движок из npm")
    for op in ("folder-add", "folder-remove", "mirror-add", "mirror-remove"):
        action = actions.add_parser(op)
        action.add_argument(
            "value", help="Путь на Диске при добавлении, ID при удалении"
        )
        if op == "mirror-add":
            action.add_argument("local", help="Локальная папка для зеркала")
    actions.add_parser("sync", help="Обновить библиотеку выбранных папок")
    purge = actions.add_parser(
        "trash-purge", help="Окончательно удалить выбранную запись корзины"
    )
    purge.add_argument("trash_path", help="Путь trash:/... из списка корзины")
    for op in ("commands", "read", "preview", "write"):
        cmd = actions.add_parser(op)
        if op == "commands":
            cmd.add_argument("command", nargs="?", default="")
        else:
            cmd.add_argument("command")
            cmd.add_argument(
                "--params", default="{}", help="Аргументы операции в формате JSON"
            )
    form = sub.add_parser("connect", help="Открыть окно входа через Яндекс")
    form.add_argument("--client-id")
    auth = sub.add_parser("login", help="Войти через Яндекс из терминала")
    auth.add_argument("--client-id")
    auth.add_argument("--read-only", action="store_true")
    auth.add_argument("--name", help="Имя профиля, которое будет видно в списке Дисков")
    auth.add_argument(
        "--manual-code",
        action="store_true",
        help="Ввести код с сайта Яндекса в терминале",
    )
    for name, text in (
        ("serve", "Локальный MCP-сервер"),
        ("serve-web", "MCP для личного туннеля"),
        ("status", "Показать текущий Диск"),
        ("logout", "Отключить один Диск"),
    ):
        cmd = sub.add_parser(name, help=text)
        cmd.add_argument("--account", help="Имя или ID Диска")
        if name == "serve":
            cmd.add_argument(
                "--project", type=Path, help="Папка проекта для этого MCP-сервера"
            )
    sub.add_parser("accounts", help="Список подключённых Дисков и прав")
    for name, text in (
        ("use", "Выбрать Диск по умолчанию"),
        ("rename", "Переименовать Диск"),
        ("bind", "Закрепить Диск за папкой проекта"),
    ):
        cmd = sub.add_parser(name, help=text)
        cmd.add_argument("account")
        if name == "rename":
            cmd.add_argument("new_name")
        if name == "bind":
            cmd.add_argument("--project", type=Path, default=Path.cwd())
    unbind = sub.add_parser("unbind", help="Снять привязку папки проекта")
    unbind.add_argument("--project", type=Path, default=Path.cwd())
    idx = sub.add_parser(
        "index",
        help="Полностью обновить индекс выбранных папок. Без папок — очистить индекс.",
    )
    idx.add_argument("folders", nargs="*")
    idx.add_argument("--account", help="Имя или ID Диска")
    tunnel = sub.add_parser(
        "web-setup", help="Подготовить и проверить профиль официального туннеля"
    )
    tunnel.add_argument("tunnel_id")
    tunnel.add_argument("--account", help="Закрепить туннель за Диском")
    approve = sub.add_parser(
        "approve", help="Подтвердить конкретный запрос из своего веб-чата"
    )
    approve.add_argument("code")
    approve.add_argument("--account", help="Диск, за которым закреплён туннель")
    args = p.parse_args()
    try:
        if args.action == "extension":
            import asyncio

            from . import advanced

            if args.operation == "install":
                advanced.install()
                print("Локальное расширение установлено.")
                return
            with account.account_lock():
                catalog = account.Accounts()
                owner = catalog.read(catalog.select(args.account, project=Path.cwd()))
                if args.operation == "trash-purge":
                    path = args.trash_path
                    if (
                        not owner.writable
                        or not path.startswith("trash:/")
                        or path_of(path[6:]) == "/"
                    ):
                        raise Problem(
                            "Выберите запись корзины на Диске с правом записи."
                        )
                    print_account(catalog.describe(owner.profile))
                    print("Эта запись будет окончательно удалена:", ascii(path))
                    if input("Введите полный путь для подтверждения: ") != path:
                        raise Problem("Удаление отменено.")
                    advanced.owner_command(
                        owner,
                        ["api", "trash-resources-delete", "--path", path, "--yes"],
                    )
                elif args.operation in {
                    "folder-add",
                    "folder-remove",
                    "mirror-add",
                    "mirror-remove",
                }:
                    words = advanced.owner_words(
                        args.operation, args.value, getattr(args, "local", None)
                    )
                    advanced.owner_command(owner, words)
                else:
                    try:
                        params = json.loads(getattr(args, "params", "{}"))
                    except ValueError:
                        raise Problem("Передайте объект JSON в --params.") from None
                    result = asyncio.run(
                        advanced.run(
                            owner,
                            args.operation,
                            getattr(args, "command", "sync"),
                            params,
                            timeout=args.timeout,
                        )
                    )
                    print(json.dumps(result, ensure_ascii=False))
        elif args.action in {"serve", "serve-web"}:
            from .server import make_server

            pin = None
            if args.account:
                with account.account_lock():
                    pin = account.Accounts().lookup(args.account)
            make_server(
                tunnel=args.action == "serve-web",
                project=getattr(args, "project", None),
                pin=pin,
            ).run()
        elif args.action == "login":
            account.login(args.client_id, args.read_only, args.manual_code, args.name)
        elif args.action == "connect":
            connect_window(args.client_id)
        elif args.action == "web-setup":
            with account.account_lock():
                catalog = account.Accounts()
                pin = (
                    catalog.select(args.account, pin=None)
                    if args.account
                    else catalog.select(pin=catalog.data["web"])
                )
            setup_web(args.tunnel_id, pin)
        else:
            with account.account_lock():
                catalog = account.Accounts()
                if args.action == "accounts":
                    bound = catalog.bound(Path.cwd())
                    for key in catalog.profiles:
                        print_account(catalog.describe(key), bound == key)
                    if not catalog.profiles:
                        print(
                            "Подключённых Дисков пока нет. Добавьте первый через login или connect."
                        )
                    return
                if args.action == "unbind":
                    catalog.binding(args.project, None)
                    print("Привязка этой папки снята.")
                    return
                if args.action in {"bind", "use", "rename"}:
                    key = catalog.lookup(args.account)
                    if args.action == "bind":
                        catalog.binding(args.project, key)
                        print(
                            "Диск проекта:",
                            catalog.profiles[key]["alias"],
                            "·",
                            args.project.resolve(),
                        )
                    elif args.action == "use":
                        catalog.use(key)
                        print("Диск по умолчанию:", catalog.profiles[key]["alias"])
                    else:
                        catalog.profiles[key]["alias"] = catalog.unique_name(
                            args.new_name, key
                        )
                        catalog.save()
                        print("Новое имя Диска:", catalog.profiles[key]["alias"])
                    return
                if args.action == "approve":
                    key = (
                        catalog.lookup(args.account)
                        if args.account
                        else catalog.select(pin=catalog.data["web"])
                    )
                    print_account(catalog.describe(key))
                    web.approve(catalog.read(key), args.code)
                    return
                key = catalog.select(args.account, project=Path.cwd())
                if args.action == "status":
                    print_account(
                        catalog.describe(key), key == catalog.bound(Path.cwd())
                    )
                elif args.action == "logout":
                    name = catalog.profiles[key]["alias"]
                    catalog.remove(key)
                    print(
                        "Отключён Диск:",
                        name,
                        "· Отозвать разрешение: https://id.yandex.ru/profile/access",
                    )
                elif args.action == "index":
                    from .index import rebuild

                    a = catalog.read(key)
                    with httpx.Client(trust_env=False, timeout=45) as http:
                        print(
                            json.dumps(
                                rebuild(a, Disk(http, a.token), args.folders),
                                ensure_ascii=False,
                            )
                        )
    except (KeyboardInterrupt, EOFError):
        p.exit(1, "Отменено.\n")
    except Problem as e:
        p.exit(1, str(e) + "\n")
    except Exception as e:  # noqa: BLE001 - diagnostics must not reveal signed URLs
        p.exit(
            1,
            f"Не удалось завершить операцию ({type(e).__name__}). Проверьте сеть, системную связку ключей и README.\n",
        )


if __name__ == "__main__":
    main()
