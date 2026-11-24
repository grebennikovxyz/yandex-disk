import socket
from urllib.parse import parse_qs, urlsplit

import httpx

from ydisk_plugin import account


def test_loopback_preconnection_does_not_block_and_wrong_state_is_rejected(monkeypatch):
    with socket.socket() as port:
        port.bind(("127.0.0.1", 0))
        addr = port.getsockname()
    callback = f"http://127.0.0.1:{addr[1]}/callback"
    monkeypatch.setattr(account, "CALLBACK", callback)

    def simulate_browser(start):
        with (
            socket.create_connection(addr),
            httpx.Client(trust_env=False, timeout=2) as browser,
        ):
            redirect = browser.get(start)
            assert redirect.status_code == 302
            auth = urlsplit(redirect.headers["location"])
            assert auth.hostname == "oauth.yandex.ru"
            params = parse_qs(auth.query)
            assert params["code_challenge_method"] == ["S256"]
            assert params["redirect_uri"] == [callback]
            assert (
                browser.get(
                    callback, params={"state": "wrong", "code": "invalid"}
                ).status_code
                == 400
            )
            resp = browser.get(
                callback, params={"state": params["state"][0], "code": "test-code"}
            )
            assert resp.status_code == 200

    from threading import Thread

    errors = []

    def launch(url):
        def visit():
            try:
                simulate_browser(url)
            except Exception as e:  # noqa: BLE001 - propagate browser thread failures
                errors.append(e)

        Thread(target=visit, daemon=True).start()

    monkeypatch.setattr(account.webbrowser, "open", launch)
    code = account.authorization_code(
        "public-test", ["cloud_api:disk.read"], "v" * 50, False, timeout=5
    )
    assert code == "test-code"
    assert not errors
