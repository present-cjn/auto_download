from __future__ import annotations

from urllib.error import HTTPError

from scripts import start_local_app


def test_wait_for_health_bypasses_proxy(monkeypatch) -> None:
    proxy_handlers = []

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

    class FakeOpener:
        def open(self, url, timeout):
            return FakeResponse()

    def fake_proxy_handler(proxies):
        proxy_handlers.append(proxies)
        return proxies

    monkeypatch.setattr(start_local_app, "ProxyHandler", fake_proxy_handler)
    monkeypatch.setattr(start_local_app, "build_opener", lambda *_handlers: FakeOpener())

    assert start_local_app.wait_for_health("http://127.0.0.1:8000") is True
    assert proxy_handlers == [{}]


def test_wait_for_health_accepts_local_redirect(monkeypatch) -> None:
    calls = []

    class FakeOpener:
        def open(self, url, timeout):
            calls.append(url)
            if url.endswith("/health"):
                raise OSError("health not ready")
            raise HTTPError(url, 303, "See Other", hdrs=None, fp=None)

    monkeypatch.setattr(start_local_app, "build_opener", lambda *_handlers: FakeOpener())

    assert start_local_app.wait_for_health("http://127.0.0.1:8000") is True
    assert calls == ["http://127.0.0.1:8000/health", "http://127.0.0.1:8000"]


def test_cli_calls_multiprocessing_freeze_support_before_main(monkeypatch) -> None:
    calls = []

    monkeypatch.setattr(start_local_app.multiprocessing, "freeze_support", lambda: calls.append("freeze"))
    monkeypatch.setattr(start_local_app, "main", lambda: calls.append("main") or 0)

    assert start_local_app.cli() == 0
    assert calls == ["freeze", "main"]
