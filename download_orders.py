#!/usr/bin/env python3
"""Create order folders and download Google Drive design images into them."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import tempfile
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlparse
from zipfile import ZipFile
import xml.etree.ElementTree as ET


EXCEL_NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
DEFAULT_EXCEL = "BT-6月1日订单总.xlsx"
DEFAULT_SHEET = "12"
IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
}


@dataclass(frozen=True)
class OrderLink:
    order_no: str
    design_link: str
    row_number: int


def column_letters(cell_ref: str) -> str:
    return "".join(re.findall(r"[A-Z]+", cell_ref))


def column_index(letters: str) -> int:
    index = 0
    for char in letters:
        index = index * 26 + ord(char) - ord("A") + 1
    return index


def load_shared_strings(zip_file: ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zip_file.namelist():
        return []

    root = ET.fromstring(zip_file.read("xl/sharedStrings.xml"))
    strings: list[str] = []
    for shared_item in root.findall("m:si", EXCEL_NS):
        strings.append(
            "".join(text.text or "" for text in shared_item.findall(".//m:t", EXCEL_NS))
        )
    return strings


def cell_text(cell: ET.Element, shared_strings: list[str]) -> str:
    value = cell.find("m:v", EXCEL_NS)
    if value is None or value.text is None:
        inline = cell.find("m:is", EXCEL_NS)
        if inline is None:
            return ""
        return "".join(text.text or "" for text in inline.findall(".//m:t", EXCEL_NS))

    raw_value = value.text
    if cell.attrib.get("t") == "s":
        return shared_strings[int(raw_value)]
    return raw_value


def relationship_targets(zip_file: ZipFile, rel_path: str) -> dict[str, str]:
    root = ET.fromstring(zip_file.read(rel_path))
    targets: dict[str, str] = {}
    for relationship in root.findall(f"{{{REL_NS}}}Relationship"):
        targets[relationship.attrib["Id"]] = relationship.attrib["Target"]
    return targets


def worksheet_path(zip_file: ZipFile, sheet_name: str) -> str:
    workbook = ET.fromstring(zip_file.read("xl/workbook.xml"))
    targets = relationship_targets(zip_file, "xl/_rels/workbook.xml.rels")

    for sheet in workbook.findall("m:sheets/m:sheet", EXCEL_NS):
        if sheet.attrib.get("name") != sheet_name:
            continue
        rel_id = sheet.attrib[f"{{{EXCEL_NS['r']}}}id"]
        target = targets[rel_id]
        return target if target.startswith("xl/") else f"xl/{target}"

    available = [
        sheet.attrib.get("name", "")
        for sheet in workbook.findall("m:sheets/m:sheet", EXCEL_NS)
    ]
    raise ValueError(
        f"Worksheet {sheet_name!r} was not found. Available sheets: {', '.join(available)}"
    )


def row_values(row: ET.Element, shared_strings: list[str]) -> dict[int, str]:
    values: dict[int, str] = {}
    for cell in row.findall("m:c", EXCEL_NS):
        ref = cell.attrib.get("r", "")
        letters = column_letters(ref)
        if not letters:
            continue
        values[column_index(letters)] = cell_text(cell, shared_strings).strip()
    return values


def read_order_links(excel_path: Path, sheet_name: str) -> list[OrderLink]:
    with ZipFile(excel_path) as zip_file:
        shared_strings = load_shared_strings(zip_file)
        sheet_path = worksheet_path(zip_file, sheet_name)
        root = ET.fromstring(zip_file.read(sheet_path))

        rows = root.findall("m:sheetData/m:row", EXCEL_NS)
        if not rows:
            return []

        header = row_values(rows[0], shared_strings)
        if header.get(1) != "订单号" or header.get(3) != "Design Link":
            raise ValueError(
                "Unexpected header in worksheet "
                f"{sheet_name!r}: column A={header.get(1)!r}, column C={header.get(3)!r}"
            )

        links: list[OrderLink] = []
        for row in rows[1:]:
            values = row_values(row, shared_strings)
            order_no = values.get(1, "")
            design_link = values.get(3, "")
            if not order_no or not design_link:
                continue
            links.append(
                OrderLink(
                    order_no=order_no,
                    design_link=design_link,
                    row_number=int(row.attrib.get("r", "0") or 0),
                )
            )
        return links


def unique_order_links(links: Iterable[OrderLink]) -> list[OrderLink]:
    seen: set[tuple[str, str]] = set()
    unique: list[OrderLink] = []
    for link in links:
        key = (link.order_no, link.design_link)
        if key in seen:
            continue
        seen.add(key)
        unique.append(link)
    return unique


def grouped_by_order(links: Iterable[OrderLink]) -> OrderedDict[str, list[OrderLink]]:
    grouped: OrderedDict[str, list[OrderLink]] = OrderedDict()
    for link in links:
        grouped.setdefault(link.order_no, []).append(link)
    return grouped


def is_google_drive_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host == "drive.google.com" or host.endswith(".drive.google.com")


def safe_filename(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()
    return cleaned or "downloaded_image"


def next_available_path(directory: Path, filename: str) -> Path:
    filename = safe_filename(filename)
    candidate = directory / filename
    if not candidate.exists():
        return candidate

    stem = candidate.stem
    suffix = candidate.suffix
    counter = 2
    while True:
        candidate = directory / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def iter_image_files(directory: Path) -> Iterable[Path]:
    for path in directory.rglob("*"):
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS:
            yield path


def import_gdown():
    try:
        import gdown  # type: ignore
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "Missing dependency: gdown\n"
            "Install it with:\n"
            "  python3 -m pip install -r requirements.txt"
        ) from exc
    return gdown


def download_drive_folder(url: str, temp_dir: Path) -> None:
    gdown = import_gdown()
    result = gdown.download_folder(
        url=url,
        output=str(temp_dir),
        quiet=False,
        use_cookies=False,
        remaining_ok=True,
    )
    if result is None:
        raise RuntimeError("gdown returned no downloaded files")


def copy_images(download_dir: Path, order_dir: Path) -> int:
    copied = 0
    for image_path in iter_image_files(download_dir):
        target = next_available_path(order_dir, image_path.name)
        shutil.copy2(image_path, target)
        copied += 1
    return copied


def print_dry_run(grouped: OrderedDict[str, list[OrderLink]]) -> None:
    total_links = sum(len(links) for links in grouped.values())
    print(f"Orders: {len(grouped)}")
    print(f"Unique order/link pairs: {total_links}")
    for order_no, links in grouped.items():
        print(f"{order_no}/")
        for link in links:
            print(f"  row {link.row_number}: {link.design_link}")


def limited_grouped(
    grouped: OrderedDict[str, list[OrderLink]], limit: Optional[int]
) -> OrderedDict[str, list[OrderLink]]:
    if limit is None:
        return grouped
    limited: OrderedDict[str, list[OrderLink]] = OrderedDict()
    for index, (order_no, links) in enumerate(grouped.items()):
        if index >= limit:
            break
        limited[order_no] = links
    return limited


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
                with tempfile.TemporaryDirectory(prefix="order-drive-") as tmp:
                    temp_dir = Path(tmp)
                    download_drive_folder(link.design_link, temp_dir)
                    copied = copy_images(temp_dir, order_dir)
                    copied_total += copied
                    print(f"copied {copied} image(s) into {order_dir}")
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

    links = read_order_links(excel_path, args.sheet)
    unique_links = unique_order_links(links)
    grouped = limited_grouped(grouped_by_order(unique_links), args.limit)

    print(
        f"Read {len(links)} row(s), {len(unique_links)} unique order/link pair(s), "
        f"{len(grouped)} order folder(s)."
    )

    if args.dry_run:
        print_dry_run(grouped)
        return 0

    return download_orders(grouped, output_dir)


if __name__ == "__main__":
    raise SystemExit(main())
