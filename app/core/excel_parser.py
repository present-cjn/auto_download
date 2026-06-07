from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional
from zipfile import ZipFile
import xml.etree.ElementTree as ET


EXCEL_NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
DEFAULT_SHEET = "12"


@dataclass(frozen=True)
class OrderItemRow:
    order_no: str
    row_number: int
    order_date_raw: str
    order_date: Optional[str]
    design_link: str
    sku: str
    size: str
    color: str
    quantity: int
    custom_name: str
    tracking_no: str
    shipping_fullname: str
    address: str
    city: str
    province: str
    zip_code: str
    country: str
    phone: str
    mail: str
    customer_note: str
    product_id: str
    week: Optional[int]
    parent_item_name_local: str
    parent_item_name: str


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


def excel_serial_date(raw_value: str) -> Optional[str]:
    if not raw_value:
        return None
    try:
        number = float(raw_value)
    except ValueError:
        return raw_value
    if number <= 0:
        return None
    parsed = datetime(1899, 12, 30) + timedelta(days=number)
    return date(parsed.year, parsed.month, parsed.day).isoformat()


def parse_int(raw_value: str, default: int = 0) -> int:
    if not raw_value:
        return default
    try:
        return int(float(raw_value))
    except ValueError:
        return default


def parse_optional_int(raw_value: str) -> Optional[int]:
    if not raw_value:
        return None
    try:
        return int(float(raw_value))
    except ValueError:
        return None


def require_headers(header: dict[int, str], sheet_name: str) -> None:
    expected = {
        1: "订单号",
        2: "日期",
        3: "Design Link",
        4: "sku",
        5: "尺码",
        6: "Color",
        7: "数量",
        9: "单号",
    }
    mismatches = [
        f"column {index}: expected {name!r}, got {header.get(index)!r}"
        for index, name in expected.items()
        if header.get(index) != name
    ]
    if mismatches:
        raise ValueError(
            f"Unexpected header in worksheet {sheet_name!r}: " + "; ".join(mismatches)
        )


def parse_order_items(excel_path: Path, sheet_name: str = DEFAULT_SHEET) -> list[OrderItemRow]:
    with ZipFile(excel_path) as zip_file:
        shared_strings = load_shared_strings(zip_file)
        sheet_path = worksheet_path(zip_file, sheet_name)
        root = ET.fromstring(zip_file.read(sheet_path))

        rows = root.findall("m:sheetData/m:row", EXCEL_NS)
        if not rows:
            return []

        header = row_values(rows[0], shared_strings)
        require_headers(header, sheet_name)

        items: list[OrderItemRow] = []
        for row in rows[1:]:
            values = row_values(row, shared_strings)
            order_no = values.get(1, "")
            design_link = values.get(3, "")
            sku = values.get(4, "")
            if not order_no and not design_link and not sku:
                continue
            if not order_no:
                continue

            items.append(
                OrderItemRow(
                    order_no=order_no,
                    row_number=int(row.attrib.get("r", "0") or 0),
                    order_date_raw=values.get(2, ""),
                    order_date=excel_serial_date(values.get(2, "")),
                    design_link=design_link,
                    sku=sku,
                    size=values.get(5, ""),
                    color=values.get(6, ""),
                    quantity=parse_int(values.get(7, ""), 0),
                    custom_name=values.get(8, ""),
                    tracking_no=values.get(9, ""),
                    shipping_fullname=values.get(10, ""),
                    address=values.get(11, ""),
                    city=values.get(12, ""),
                    province=values.get(13, ""),
                    zip_code=values.get(14, ""),
                    country=values.get(15, ""),
                    phone=values.get(16, ""),
                    mail=values.get(17, ""),
                    customer_note=values.get(18, ""),
                    product_id=values.get(19, ""),
                    week=parse_optional_int(values.get(20, "")),
                    parent_item_name_local=values.get(21, ""),
                    parent_item_name=values.get(22, ""),
                )
            )
        return items


def order_links_from_items(items: Iterable[OrderItemRow]) -> list[OrderLink]:
    return [
        OrderLink(item.order_no, item.design_link, item.row_number)
        for item in items
        if item.design_link
    ]


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
