from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlparse
from zipfile import ZipFile
import xml.etree.ElementTree as ET


EXCEL_NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
DEFAULT_SHEET = "12"
VISIBLE_SHEET_STATES = {"visible", ""}
REQUIRED_IMPORT_FIELDS = [
    "order_date_raw",
    "order_no",
    "sku",
    "design_link",
    "shipping_fullname",
    "address",
    "city",
    "province",
    "zip_code",
    "country",
]
FIELD_LABELS = {
    "order_date_raw": "日期",
    "order_no": "订单号",
    "sku": "SKU",
    "design_link": "Design Link",
    "mockup_link": "Mockup Link",
    "shipping_fullname": "Shipping Fullname",
    "address": "Address",
    "city": "City",
    "province": "Province",
    "zip_code": "Zip",
    "country": "Country",
}
HEADER_ALIASES = {
    "order_no": ["订单号"],
    "order_date_raw": ["日期"],
    "design_link": ["Design Link"],
    "mockup_link": ["Mockup Link"],
    "sku": ["SKU", "sku"],
    "size": ["尺码"],
    "color": ["颜色", "Color"],
    "quantity": ["数量"],
    "custom_name": ["定制名"],
    "tracking_no": ["物流单号", "单号"],
    "carrier": ["物流公司"],
    "shipping_fullname": ["Shipping Fullname"],
    "address": ["Address"],
    "city": ["City"],
    "province": ["Province"],
    "zip_code": ["Zip", "Postcode"],
    "country": ["Country"],
    "phone": ["Phone"],
    "mail": ["Mail"],
    "customer_note": ["Customer Note"],
    "product_id": ["Product ID"],
    "week": ["Week"],
    "parent_item_name_local": ["Các mục mẹ"],
    "parent_item_name": ["Parent items"],
}


@dataclass(frozen=True)
class OrderItemRow:
    order_no: str
    row_number: int
    order_date_raw: str
    order_date: Optional[str]
    design_link: str
    mockup_link: str
    sku: str
    size: str
    color: str
    quantity: int
    custom_name: str
    tracking_no: str
    carrier: str
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


def workbook_sheets(zip_file: ZipFile) -> list[dict[str, str]]:
    workbook = ET.fromstring(zip_file.read("xl/workbook.xml"))
    sheets = []
    for sheet in workbook.findall("m:sheets/m:sheet", EXCEL_NS):
        sheets.append(
            {
                "name": sheet.attrib.get("name", ""),
                "state": sheet.attrib.get("state", "visible"),
            }
        )
    return sheets


def list_worksheet_names(excel_path: Path, visible_only: bool = True) -> list[str]:
    with ZipFile(excel_path) as zip_file:
        names = []
        for sheet in workbook_sheets(zip_file):
            if visible_only and sheet["state"] not in VISIBLE_SHEET_STATES:
                continue
            names.append(sheet["name"])
        return names


def single_visible_worksheet_name(excel_path: Path) -> str:
    names = list_worksheet_names(excel_path, visible_only=True)
    if len(names) == 1:
        return names[0]
    if not names:
        raise ValueError("Excel 文件中没有可见工作表，请检查文件格式。")
    raise ValueError(
        "Excel 文件包含多个可见工作表，请只保留订单明细工作表后重新上传。"
        f" 当前可见工作表: {', '.join(names)}"
    )


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


def is_google_drive_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host == "drive.google.com" or host.endswith(".drive.google.com")


def item_field(item: Any, field_name: str) -> Any:
    if isinstance(item, dict):
        return item.get(field_name, "")
    return getattr(item, field_name, "")


def normalized_header_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip()).casefold()


def header_lookup(header: dict[int, str]) -> dict[str, int]:
    lookup = {}
    for index, name in header.items():
        normalized = normalized_header_name(name)
        if normalized:
            lookup[normalized] = index
    return lookup


def header_mapping(header: dict[int, str], sheet_name: str) -> dict[str, int]:
    lookup = header_lookup(header)
    mapping: dict[str, int] = {}
    missing = []
    for field_name, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            index = lookup.get(normalized_header_name(alias))
            if index is not None:
                mapping[field_name] = index
                break
        if field_name in REQUIRED_IMPORT_FIELDS and field_name not in mapping:
            missing.append(FIELD_LABELS[field_name])

    if missing:
        raise ValueError(
            f"工作表 {sheet_name!r} 缺少必填表头: " + ", ".join(missing)
        )
    return mapping


def mapped_value(values: dict[int, str], mapping: dict[str, int], field_name: str) -> str:
    index = mapping.get(field_name)
    if index is None:
        return ""
    return values.get(index, "")


def parse_order_items(excel_path: Path, sheet_name: str = DEFAULT_SHEET) -> list[OrderItemRow]:
    if sheet_name is None:
        sheet_name = single_visible_worksheet_name(excel_path)
    with ZipFile(excel_path) as zip_file:
        shared_strings = load_shared_strings(zip_file)
        sheet_path = worksheet_path(zip_file, sheet_name)
        root = ET.fromstring(zip_file.read(sheet_path))

        rows = root.findall("m:sheetData/m:row", EXCEL_NS)
        if not rows:
            return []

        header = row_values(rows[0], shared_strings)
        mapping = header_mapping(header, sheet_name)

        items: list[OrderItemRow] = []
        for row in rows[1:]:
            values = row_values(row, shared_strings)
            order_no = mapped_value(values, mapping, "order_no")
            design_link = mapped_value(values, mapping, "design_link")
            mockup_link = mapped_value(values, mapping, "mockup_link")
            sku = mapped_value(values, mapping, "sku")
            if not order_no and not design_link and not sku:
                continue
            if not order_no:
                continue

            order_date_raw = mapped_value(values, mapping, "order_date_raw")
            items.append(
                OrderItemRow(
                    order_no=order_no,
                    row_number=int(row.attrib.get("r", "0") or 0),
                    order_date_raw=order_date_raw,
                    order_date=excel_serial_date(order_date_raw),
                    design_link=design_link,
                    mockup_link=mockup_link,
                    sku=sku,
                    size=mapped_value(values, mapping, "size"),
                    color=mapped_value(values, mapping, "color"),
                    quantity=parse_int(mapped_value(values, mapping, "quantity"), 0),
                    custom_name=mapped_value(values, mapping, "custom_name"),
                    tracking_no=mapped_value(values, mapping, "tracking_no"),
                    carrier=mapped_value(values, mapping, "carrier"),
                    shipping_fullname=mapped_value(values, mapping, "shipping_fullname"),
                    address=mapped_value(values, mapping, "address"),
                    city=mapped_value(values, mapping, "city"),
                    province=mapped_value(values, mapping, "province"),
                    zip_code=mapped_value(values, mapping, "zip_code"),
                    country=mapped_value(values, mapping, "country"),
                    phone=mapped_value(values, mapping, "phone"),
                    mail=mapped_value(values, mapping, "mail"),
                    customer_note=mapped_value(values, mapping, "customer_note"),
                    product_id=mapped_value(values, mapping, "product_id"),
                    week=parse_optional_int(mapped_value(values, mapping, "week")),
                    parent_item_name_local=mapped_value(
                        values, mapping, "parent_item_name_local"
                    ),
                    parent_item_name=mapped_value(values, mapping, "parent_item_name"),
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


def build_import_summary(items: Iterable[Any]) -> dict[str, object]:
    item_list = list(items)
    order_items: OrderedDict[str, int] = OrderedDict()
    design_links: list[str] = []
    mockup_links: list[str] = []
    all_links: list[str] = []
    sku_rows: OrderedDict[str, list[int]] = OrderedDict()
    missing_required_fields: OrderedDict[str, list[int]] = OrderedDict()
    empty_design_link_count = 0
    non_google_drive_link_count = 0
    tracking_no_count = 0

    for item in item_list:
        order_no = str(item_field(item, "order_no") or "")
        design_link = str(item_field(item, "design_link") or "")
        mockup_link = str(item_field(item, "mockup_link") or "")
        sku = str(item_field(item, "sku") or "")
        row_number = int(item_field(item, "row_number") or 0)
        tracking_no = str(item_field(item, "tracking_no") or "")
        order_items[order_no] = order_items.get(order_no, 0) + 1
        if sku:
            sku_rows.setdefault(sku, []).append(row_number)
        for field_name in REQUIRED_IMPORT_FIELDS:
            if not str(item_field(item, field_name) or ""):
                missing_required_fields.setdefault(FIELD_LABELS[field_name], []).append(
                    row_number
                )
        if tracking_no:
            tracking_no_count += 1
        if not design_link:
            empty_design_link_count += 1
        else:
            design_links.append(design_link)
            all_links.append(design_link)
            if not is_google_drive_url(design_link):
                non_google_drive_link_count += 1
        if mockup_link:
            mockup_links.append(mockup_link)
            all_links.append(mockup_link)
            if not is_google_drive_url(mockup_link):
                non_google_drive_link_count += 1

    duplicate_design_link_count = len(design_links) - len(set(design_links))
    duplicate_all_link_count = len(all_links) - len(set(all_links))
    multi_sku_order_count = sum(1 for count in order_items.values() if count > 1)
    duplicate_skus = {
        sku: rows for sku, rows in sku_rows.items() if len(rows) > 1
    }
    blocking_issue_count = len(missing_required_fields) + len(duplicate_skus)
    warnings = []
    if not item_list:
        warnings.append("没有解析到订单明细。")
    if empty_design_link_count:
        warnings.append(f"有 {empty_design_link_count} 行没有 Design Link。")
    if non_google_drive_link_count:
        warnings.append(f"有 {non_google_drive_link_count} 个链接不是 Google Drive 链接。")
    if duplicate_design_link_count:
        warnings.append(f"有 {duplicate_design_link_count} 个重复 Design Link。")
    if duplicate_all_link_count:
        warnings.append(f"Design/Mockup 中共有 {duplicate_all_link_count} 个重复链接。")
    if multi_sku_order_count:
        warnings.append(f"有 {multi_sku_order_count} 个订单包含多条 SKU。")
    if duplicate_skus:
        warnings.append(f"有 {len(duplicate_skus)} 个重复 SKU，需要修正后再下载。")
    if missing_required_fields:
        warnings.append("有必填字段为空，需要补齐后再下载。")

    return {
        "item_count": len(item_list),
        "order_count": len(order_items),
        "design_link_count": len(design_links),
        "mockup_link_count": len(mockup_links),
        "total_download_link_count": len(all_links),
        "empty_design_link_count": empty_design_link_count,
        "non_google_drive_link_count": non_google_drive_link_count,
        "duplicate_design_link_count": duplicate_design_link_count,
        "duplicate_all_link_count": duplicate_all_link_count,
        "multi_sku_order_count": multi_sku_order_count,
        "duplicate_skus": duplicate_skus,
        "duplicate_sku_count": len(duplicate_skus),
        "missing_required_fields": dict(missing_required_fields),
        "blocking_issue_count": blocking_issue_count,
        "can_start_download": blocking_issue_count == 0,
        "tracking_no_count": tracking_no_count,
        "warnings": warnings,
    }


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
