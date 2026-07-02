from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Optional, Sequence

from app.core.downloader import detect_cloudflare_challenge, import_playwright_sync_api


DEFAULT_PROFILE_DIR = Path("data/browser-profiles/printerval-main")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Open a persistent Printerval browser session for manual Cloudflare verification.",
    )
    parser.add_argument(
        "--url",
        default="https://printerval.com/",
        help="Printerval URL to open before manual verification.",
    )
    parser.add_argument(
        "--profile",
        default=str(DEFAULT_PROFILE_DIR),
        help="Persistent Chromium user data directory used later by server downloads.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=600,
        help="How long to wait for manual verification.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run without a visible browser window. This is usually not useful for manual verification.",
    )
    return parser.parse_args(argv)


def page_is_verified(page) -> bool:
    try:
        html = page.content()
    except Exception:
        return False
    return not detect_cloudflare_challenge(html)


def bootstrap_printerval_session(url: str, profile_dir: Path, timeout_seconds: int, headless: bool) -> int:
    sync_playwright, _PlaywrightError, _PlaywrightTimeoutError = import_playwright_sync_api()
    profile_dir.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(1, timeout_seconds)

    print(f"Opening Printerval session profile: {profile_dir}", flush=True)
    print("Complete any Cloudflare verification in the opened browser window.", flush=True)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile_dir),
            headless=headless,
            accept_downloads=True,
        )
        try:
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            while time.monotonic() < deadline:
                current_url = getattr(page, "url", "")
                if page_is_verified(page):
                    print(f"Printerval session verified: {current_url}", flush=True)
                    return 0
                print("Printerval challenge is still active; waiting...", flush=True)
                time.sleep(3)
            print(f"Timed out after {timeout_seconds}s while waiting for verification.", flush=True)
            return 1
        finally:
            context.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    return bootstrap_printerval_session(
        url=args.url,
        profile_dir=Path(args.profile),
        timeout_seconds=args.timeout_seconds,
        headless=bool(args.headless),
    )


if __name__ == "__main__":
    raise SystemExit(main())
