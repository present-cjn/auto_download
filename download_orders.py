#!/usr/bin/env python3
"""Create order folders and download Google Drive design images into them."""

from __future__ import annotations

import argparse
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Optional

from app.core.downloader import download_design_images, import_gdown, is_google_drive_url
from app.core.excel_parser import (
    DEFAULT_SHEET,
    OrderLink,
    grouped_by_order,
    limited_grouped,
    order_links_from_items,
    parse_order_items,
    unique_order_links,
)


DEFAULT_EXCEL = "BT-6月1日订单总.xlsx"


def print_dry_run(grouped: OrderedDict[str, list[OrderLink]]) -> None:
    total_links = sum(len(links) for links in grouped.values())
    print(f"Orders: {len(grouped)}")
    print(f"Unique order/link pairs: {total_links}")
    for order_no, links in grouped.items():
        print(f"{order_no}/")
        for link in links:
            print(f"  row {link.row_number}: {link.design_link}")


def download_orders(grouped: OrderedDict[str, list[OrderLink]], output_dir: Path) -> int:
    import_gdown()
    output_dir.mkdir(parents=True, exist_ok=True)
    failed: list[tuple[str, str, str]] = []
    copied_total = 0

    for order_no, links in grouped.items():
        order_dir = output_dir / order_no
        order_dir.mkdir(parents=True, exist_ok=True)
        print(f"\n== {order_no} ==")

        for index, link in enumerate(links, start=1):
            if not is_google_drive_url(link.design_link):
                failed.append((order_no, link.design_link, "not a Google Drive URL"))
                print(f"skip non-drive URL: {link.design_link}")
                continue

            print(f"[{index}/{len(links)}] downloading {link.design_link}")
            try:
                copied = download_design_images(link.design_link, order_dir)
                copied_total += len(copied)
                print(f"copied {len(copied)} image(s) into {order_dir}")
            except Exception as exc:  # noqa: BLE001 - keep batch downloads running.
                failed.append((order_no, link.design_link, str(exc)))
                print(f"failed: {exc}", file=sys.stderr)

    print(f"\nDone. Copied {copied_total} image(s).")
    if failed:
        print(f"Failures: {len(failed)}", file=sys.stderr)
        for order_no, url, reason in failed:
            print(f"- {order_no}: {url} ({reason})", file=sys.stderr)
        return 1
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create folders from order numbers and download Design Link images."
    )
    parser.add_argument("--excel", default=DEFAULT_EXCEL, help="Path to the Excel file.")
    parser.add_argument("--sheet", default=DEFAULT_SHEET, help="Worksheet name to read.")
    parser.add_argument(
        "--output", default="orders", help="Directory where order folders are created."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned order folders and links without downloading.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only process the first N orders. Useful for smoke tests.",
    )
    return parser.parse_args()


def load_grouped_links(
    excel_path: Path, sheet_name: str, limit: Optional[int]
) -> tuple[int, list[OrderLink], OrderedDict[str, list[OrderLink]]]:
    items = parse_order_items(excel_path, sheet_name)
    unique_links = unique_order_links(order_links_from_items(items))
    grouped = limited_grouped(grouped_by_order(unique_links), limit)
    return len(items), unique_links, grouped


def main() -> int:
    args = parse_args()
    excel_path = Path(args.excel)
    output_dir = Path(args.output)

    if not excel_path.exists():
        print(f"Excel file not found: {excel_path}", file=sys.stderr)
        return 2
    if args.limit is not None and args.limit < 1:
        print("--limit must be greater than 0", file=sys.stderr)
        return 2

    row_count, unique_links, grouped = load_grouped_links(
        excel_path, args.sheet, args.limit
    )
    print(
        f"Read {row_count} row(s), {len(unique_links)} unique order/link pair(s), "
        f"{len(grouped)} order folder(s)."
    )

    if args.dry_run:
        print_dry_run(grouped)
        return 0

    return download_orders(grouped, output_dir)


if __name__ == "__main__":
    raise SystemExit(main())
