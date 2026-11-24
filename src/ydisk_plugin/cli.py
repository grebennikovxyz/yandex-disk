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
from .disk import Disk

PRODUCT_NAME = "Plaugin for Ya-Di"
GUIDE = "https://developers.openai.com/api/docs/guides/secure-mcp-tunnels"


def connect_window(client_id):
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.title(PRODUCT_NAME)
    assets = Path(__file__).with_name("assets")
    root.iconphoto(True, tk.PhotoImage(file=assets / "plaugin-window.png"))
    frame = tk.Frame(root, padx=24, pady=18)
    frame.pack()
    tk.Label(frame, text=PRODUCT_NAME, font=("", 15)).pack(
        anchor="w"
    )
    tk.Label(
        frame,
        text="Неофициальный ИИ-плагин. Использует API Яндекс Диска.\n"
        "Товарный знак «Яндекс» принадлежит ООО «ЯНДЕКС».\n\n"
        "Вход проходит на странице Яндекса. Токен хранится в системе.\n"
        "OpenAI получает результаты, которые вы запрашиваете в чате.",
        justify="left",
    ).pack(pady=12, anchor="w")
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
            account.login(client.get(), read_only.get())
        except Exception as e:  # noqa: BLE001 - never show OAuth URLs in a GUI error
            messagebox.showerror(
                "Вход",
                str(e) if isinstance(e, Problem) else "Не удалось завершить вход.",
            )
            return
        messagebox.showinfo(PRODUCT_NAME, "Диск подключён.")
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


def setup_web(tunnel_id):
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
            "yandex-disk-plugin serve-web",
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
    form = sub.add_parser("connect", help="Открыть окно входа через Яндекс")
    form.add_argument("--client-id")
    auth = sub.add_parser("login", help="Войти через Яндекс из терминала")
    auth.add_argument("--client-id")
    auth.add_argument("--read-only", action="store_true")
    auth.add_argument(
        "--manual-code",
        action="store_true",
        help="Ввести код с сайта Яндекса в терминале",
    )
    for name in ("serve", "serve-web", "status", "logout"):
        sub.add_parser(name)
    idx = sub.add_parser(
        "index",
        help="Полностью обновить индекс выбранных папок. Без папок — очистить индекс.",
    )
    idx.add_argument("folders", nargs="*")
    tunnel = sub.add_parser(
        "web-setup", help="Подготовить и проверить профиль официального туннеля"
    )
    tunnel.add_argument("tunnel_id")
    approve = sub.add_parser(
        "approve", help="Подтвердить конкретный запрос из своего веб-чата"
    )
    approve.add_argument("code")
    args = p.parse_args()
    try:
        if args.action in {"serve", "serve-web"}:
            from .server import make_server

            make_server(tunnel=args.action == "serve-web").run()
        elif args.action == "login":
            account.login(args.client_id, args.read_only, args.manual_code)
        elif args.action == "connect":
            connect_window(args.client_id)
        elif args.action == "web-setup":
            setup_web(args.tunnel_id)
        elif args.action == "approve":
            web.approve(account.current(), args.code)
        else:
            with account.account_lock():
                if args.action == "logout":
                    account.clear()
                    print(
                        "Локальный вход и индекс удалены. Отозвать разрешение: https://id.yandex.ru/profile/access"
                    )
                else:
                    a = account.current()
                    if args.action == "status":
                        print(
                            f"{a.name} · {a.uid} · "
                            + ("чтение и запись" if a.writable else "только чтение")
                        )
                    elif args.action == "index":
                        from .index import rebuild

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
