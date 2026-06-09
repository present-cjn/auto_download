from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile

import pytest

from app.core.excel_parser import (
    build_import_summary,
    list_worksheet_names,
    parse_order_items,
    single_visible_worksheet_name,
)


HEADERS = [
    "订单号",
    "日期",
    "Design Link",
    "sku",
    "尺码",
    "Color",
    "数量",
    "定制名",
    "单号",
    "Shipping Fullname",
    "Address",
    "City",
    "Province",
    "Zip",
    "Country",
    "Phone",
    "Mail",
    "Customer Note",
    "Product ID",
    "Week",
    "Các mục mẹ",
    "Parent items",
]

STANDARD_HEADERS = [
    "日期",
    "订单号",
    "SKU",
    "Design Link",
    "Mockup Link",
    "尺码",
    "颜色",
    "数量",
    "定制名",
    "物流公司",
    "Shipping Fullname",
    "Address",
    "City",
    "Province",
    "Zip",
    "Country",
    "Phone",
    "Mail",
    "Customer Note",
    "Product ID",
]


def column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def cell_xml(row_number: int, col_index: int, value: object) -> str:
    escaped = str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    ref = f"{column_name(col_index)}{row_number}"
    return f'<c r="{ref}" t="inlineStr"><is><t>{escaped}</t></is></c>'


def write_xlsx(path: Path, rows: list[list[object]]) -> None:
    write_multi_sheet_xlsx(path, [("12", rows, "visible")])


def write_multi_sheet_xlsx(
    path: Path, sheets: list[tuple[str, list[list[object]], str]]
) -> None:
    sheet_entries = []
    relationship_entries = []
    worksheet_entries = []
    for sheet_index, (sheet_name, rows, state) in enumerate(sheets, start=1):
        state_attr = "" if state == "visible" else f' state="{state}"'
        sheet_entries.append(
            f'<sheet name="{sheet_name}" sheetId="{sheet_index}" r:id="rId{sheet_index}"{state_attr}/>'
        )
        relationship_entries.append(
            f'<Relationship Id="rId{sheet_index}" Type="worksheet" Target="worksheets/sheet{sheet_index}.xml"/>'
        )
        worksheet_entries.append((f"xl/worksheets/sheet{sheet_index}.xml", rows))

    with ZipFile(path, "w") as workbook:
        workbook.writestr(
            "xl/workbook.xml",
            f"""
            <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
              xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
              <sheets>{''.join(sheet_entries)}</sheets>
            </workbook>
            """,
        )
        workbook.writestr(
            "xl/_rels/workbook.xml.rels",
            f"""
            <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
              {''.join(relationship_entries)}
            </Relationships>
            """,
        )
        for worksheet_path, rows in worksheet_entries:
            workbook.writestr(worksheet_path, worksheet_xml(rows))


def worksheet_xml(rows: list[list[object]]) -> str:
    row_xml = []
    for row_index, row in enumerate(rows, start=1):
        cells = "".join(
            cell_xml(row_index, col_index, value)
            for col_index, value in enumerate(row, start=1)
            if value != ""
        )
        row_xml.append(f'<row r="{row_index}">{cells}</row>')
    return f"""
    <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
      <sheetData>{''.join(row_xml)}</sheetData>
    </worksheet>
    """


def test_parse_order_items_and_summary(tmp_path: Path) -> None:
    source = tmp_path / "orders.xlsx"
    drive_link = "https://drive.google.com/drive/folders/folder123?usp=sharing"
    write_xlsx(
        source,
        [
            HEADERS,
            ["ORD-1", "46174", drive_link, "SKU-A", "L", "Black", "2", "", "TRK1"],
            ["ORD-1", "46174", drive_link, "SKU-B", "M", "Blue", "1"],
            ["ORD-2", "bad-date", "", "SKU-C", "S", "Red", ""],
            ["ORD-3", "46175", "https://example.com/file", "SKU-D", "S", "Red", "1"],
        ],
    )

    items = parse_order_items(source)
    summary = build_import_summary(items)

    assert len(items) == 4
    assert items[0].order_date == "2026-06-01"
    assert items[2].quantity == 0
    assert summary["item_count"] == 4
    assert summary["order_count"] == 3
    assert summary["design_link_count"] == 3
    assert summary["mockup_link_count"] == 0
    assert summary["empty_design_link_count"] == 1
    assert summary["non_google_drive_link_count"] == 1
    assert summary["duplicate_design_link_count"] == 1
    assert summary["multi_sku_order_count"] == 1
    assert summary["tracking_no_count"] == 1


def test_parse_order_items_auto_uses_single_visible_sheet(tmp_path: Path) -> None:
    source = tmp_path / "single.xlsx"
    write_multi_sheet_xlsx(
        source,
        [
            ("6月订单", [HEADERS, ["ORD-1", "46174", "", "SKU-A", "L", "Black", "1"]], "visible"),
        ],
    )

    assert list_worksheet_names(source) == ["6月订单"]
    assert single_visible_worksheet_name(source) == "6月订单"
    assert parse_order_items(source, sheet_name=None)[0].order_no == "ORD-1"


def test_parse_order_items_auto_rejects_multiple_visible_sheets(tmp_path: Path) -> None:
    source = tmp_path / "multi.xlsx"
    write_multi_sheet_xlsx(
        source,
        [
            ("订单", [HEADERS], "visible"),
            ("Sheet2", [["other"]], "visible"),
        ],
    )

    with pytest.raises(ValueError, match="多个可见工作表"):
        parse_order_items(source, sheet_name=None)


def test_hidden_sheets_are_ignored_for_auto_sheet(tmp_path: Path) -> None:
    source = tmp_path / "hidden.xlsx"
    write_multi_sheet_xlsx(
        source,
        [
            ("订单", [HEADERS, ["ORD-1", "46174", "", "SKU-A", "L", "Black", "1"]], "visible"),
            ("隐藏", [["other"]], "hidden"),
        ],
    )

    assert list_worksheet_names(source) == ["订单"]
    assert parse_order_items(source, sheet_name=None)[0].sku == "SKU-A"


def test_parse_rejects_missing_required_header(tmp_path: Path) -> None:
    source = tmp_path / "bad.xlsx"
    headers = HEADERS.copy()
    headers[2] = "Wrong Link"
    write_xlsx(source, [headers])

    with pytest.raises(ValueError, match="缺少必填表头"):
        parse_order_items(source)


def test_parse_standard_headers_and_mockup_link(tmp_path: Path) -> None:
    source = tmp_path / "standard.xlsx"
    design_link = "https://drive.google.com/drive/folders/design123"
    mockup_link = "https://drive.google.com/drive/folders/mockup123"
    write_multi_sheet_xlsx(
        source,
        [
            (
                "sheet1",
                [
                    STANDARD_HEADERS,
                    [
                        "46174",
                        "ORD-1",
                        "SKU-A",
                        design_link,
                        mockup_link,
                        "L",
                        "Black",
                        "",
                        "Name",
                        "UPS",
                        "Buyer",
                        "Address",
                        "City",
                        "CA",
                        "90001",
                        "US",
                    ],
                ],
                "visible",
            )
        ],
    )

    item = parse_order_items(source, sheet_name=None)[0]
    summary = build_import_summary([item])

    assert item.order_no == "ORD-1"
    assert item.sku == "SKU-A"
    assert item.design_link == design_link
    assert item.mockup_link == mockup_link
    assert item.color == "Black"
    assert item.carrier == "UPS"
    assert item.zip_code == "90001"
    assert item.quantity == 0
    assert summary["can_start_download"] is True
    assert summary["mockup_link_count"] == 1


def test_parse_standard_aliases_and_blocking_summary(tmp_path: Path) -> None:
    source = tmp_path / "aliases.xlsx"
    headers = STANDARD_HEADERS.copy()
    headers[2] = "sku"
    headers[6] = "Color"
    headers[14] = "Postcode"
    write_multi_sheet_xlsx(
        source,
        [
            (
                "sheet1",
                [
                    headers,
                    ["46174", "ORD-1", "SKU-A", "", "", "L", "Black", "1", "", "", "Buyer", "Address", "City", "CA", "90001", "US"],
                    ["46174", "ORD-2", "SKU-A", "https://drive.google.com/drive/folders/design123", "", "M", "Blue", "1", "", "", "Buyer", "Address", "City", "CA", "90002", "US"],
                ],
                "visible",
            )
        ],
    )

    items = parse_order_items(source, sheet_name=None)
    summary = build_import_summary(items)

    assert items[0].zip_code == "90001"
    assert items[0].color == "Black"
    assert summary["duplicate_sku_count"] == 1
    assert summary["empty_design_link_count"] == 1
    assert summary["missing_required_fields"]["Design Link"] == [2]
    assert summary["can_start_download"] is False


def test_build_import_summary_accepts_database_rows() -> None:
    summary = build_import_summary(
        [
            {
                "order_no": "ORD-1",
                "design_link": "https://drive.google.com/drive/folders/folder123",
                "tracking_no": "TRK1",
            },
            {"order_no": "ORD-1", "design_link": "", "tracking_no": ""},
        ]
    )

    assert summary["item_count"] == 2
    assert summary["order_count"] == 1
    assert summary["empty_design_link_count"] == 1
    assert summary["multi_sku_order_count"] == 1
