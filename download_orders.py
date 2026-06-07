#!/usr/bin/env python3
"""Create order folders and download Google Drive design images into them."""

from __future__ import annotations

import argparse
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Optional

from app.core.downloader import download_design_images, import_gdown, is_google_drive_url
from app.core.downloader import safe_filename
from app.core.excel_parser import (
    DEFAULT_SHEET,
    OrderItemRow,
    parse_order_items,
)


DEFAULT_EXCEL = "BT-6月1日订单总.xlsx"


def grouped_items_by_order(items: list[OrderItemRow]) -> OrderedDict[str, list[OrderItemRow]]:
    grouped: OrderedDict[str, list[OrderItemRow]] = OrderedDict()
    for item in items:
        if not item.design_link:
            continue
        grouped.setdefault(item.order_no, []).append(item)
    return grouped


def limited_items(
    grouped: OrderedDict[str, list[OrderItemRow]], limit: Optional[int]
) -> OrderedDict[str, list[OrderItemRow]]:
    if limit is None:
        return grouped
    limited: OrderedDict[str, list[OrderItemRow]] = OrderedDict()
    for index, (order_no, items) in enumerate(grouped.items()):
        if index >= limit:
            break
        limited[order_no] = items
    return limited


def print_dry_run(grouped: OrderedDict[str, list[OrderItemRow]]) -> None:
    total_links = sum(len(items) for items in grouped.values())
    print(f"Orders: {len(grouped)}")
    print(f"Order item links: {total_links}")
    for order_no, items in grouped.items():
        print(f"{order_no}/")
        for item in items:
            sku_dir = safe_filename(item.sku or f"row-{item.row_number}")
            print(f"  {sku_dir}/ row {item.row_number}: {item.design_link}")


def download_orders(grouped: OrderedDict[str, list[OrderItemRow]], output_dir: Path) -> int:
    import_gdown()
    output_dir.mkdir(parents=True, exist_ok=True)
    failed: list[tuple[str, str, str]] = []
    copied_total = 0

    for order_no, items in grouped.items():
        order_dir = output_dir / order_no
        order_dir.mkdir(parents=True, exist_ok=True)
        print(f"\n== {order_no} ==")

        for index, item in enumerate(items, start=1):
            if not is_google_drive_url(item.design_link):
                failed.append((order_no, item.design_link, "not a Google Drive URL"))
                print(f"skip non-drive URL: {item.design_link}")
                continue

            sku_dir = safe_filename(item.sku or f"row-{item.row_number}")
            target_dir = order_dir / sku_dir
            print(f"[{index}/{len(items)}] downloading {sku_dir}: {item.design_link}")
            try:
                copied = download_design_images(item.design_link, target_dir)
                copied_total += len(copied)
                print(f"copied {len(copied)} image(s) into {target_dir}")
            except Exception as exc:  # noqa: BLE001 - keep batch downloads running.
                failed.append((order_no, item.design_link, str(exc)))
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
) -> tuple[int, int, OrderedDict[str, list[OrderItemRow]]]:
    items = parse_order_items(excel_path, sheet_name)
    grouped = limited_items(grouped_items_by_order(items), limit)
    return len(items), sum(len(rows) for rows in grouped.values()), grouped


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

    row_count, item_link_count, grouped = load_grouped_links(
        excel_path, args.sheet, args.limit
    )
    print(
        f"Read {row_count} row(s), {item_link_count} order item link(s), "
        f"{len(grouped)} order folder(s)."
    )

    if args.dry_run:
        print_dry_run(grouped)
        return 0

    return download_orders(grouped, output_dir)


if __name__ == "__main__":
    raise SystemExit(main())
