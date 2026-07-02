from __future__ import annotations

from app.tools.printerval_session import page_is_verified, parse_args


class FakeSessionPage:
    def __init__(self, html: str) -> None:
        self.html = html

    def content(self) -> str:
        return self.html


def test_printerval_session_parse_args_defaults() -> None:
    args = parse_args([])

    assert args.url == "https://printerval.com/"
    assert args.profile == "data/browser-profiles/printerval-main"
    assert args.timeout_seconds == 600
    assert args.headless is False


def test_printerval_session_page_is_verified() -> None:
    assert not page_is_verified(FakeSessionPage("<title>Just a moment...</title>Cloudflare"))
    assert page_is_verified(FakeSessionPage("<title>Printerval folder design</title>"))
