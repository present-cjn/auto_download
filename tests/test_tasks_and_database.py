from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile

from app.core import database as db
from app.core.excel_parser import OrderItemRow
from app.core.tasks import create_order_archive


def order_item(
    order_no: str = "ORD-1",
    row_number: int = 2,
    sku: str = "SKU-A",
    design_link: str = "https://drive.google.com/drive/folders/folder123",
    mockup_link: str = "",
) -> OrderItemRow:
    return OrderItemRow(
        order_no=order_no,
        row_number=row_number,
        order_date_raw="46174",
        order_date="2026-06-01",
        design_link=design_link,
        mockup_link=mockup_link,
        sku=sku,
        size="L",
        color="Black",
        quantity=1,
        custom_name="",
        tracking_no="",
        carrier="",
        shipping_fullname="Name",
        address="Address",
        city="City",
        province="State",
        zip_code="12345",
        country="US",
        phone="",
        mail="",
        customer_note="",
        product_id="",
        week=23,
        parent_item_name_local="",
        parent_item_name="",
    )


def test_download_status_and_error_fields(tmp_path: Path) -> None:
    database_path = tmp_path / "app.db"
    original_path = db.DB_PATH
    db.DB_PATH = database_path
    try:
        db.init_db(database_path)
        batch_id = db.create_batch("orders.xlsx", Path("source.xlsx"))
        db.insert_import_items(
            batch_id,
            [
                order_item(
                    mockup_link="https://drive.google.com/drive/folders/folder123"
                )
            ],
        )
        items = db.get_pending_download_items(batch_id)
        assert [item["source_type"] for item in items] == ["design", "mockup"]
        item = items[0]

        db.mark_download_started(int(item["id"]))
        started = db.get_download_item(int(item["id"]))
        assert started is not None
        assert started["status"] == "downloading"
        assert started["error_code"] is None

        db.mark_download_failed(
            int(item["id"]),
            "短错误",
            error_code="network_error",
            error_detail="ssl detail",
        )
        failed = db.get_download_item(int(item["id"]))
        assert failed is not None
        assert failed["status"] == "failed"
        assert failed["error_message"] == "短错误"
        assert failed["error_code"] == "network_error"
        assert failed["error_detail"] == "ssl detail"

        db.mark_download_manual_done(int(item["id"]))
        manual_done = db.get_download_item(int(item["id"]))
        assert manual_done is not None
        assert manual_done["status"] == "manual_done"
        assert manual_done["error_code"] is None
    finally:
        db.DB_PATH = original_path


def test_create_order_archive_keeps_order_root(tmp_path: Path, monkeypatch) -> None:
    database_path = tmp_path / "app.db"
    orders_dir = tmp_path / "orders"
    archives_dir = tmp_path / "archives"
    original_path = db.DB_PATH
    db.DB_PATH = database_path
    monkeypatch.setattr("app.core.tasks.ORDERS_DIR", orders_dir)
    monkeypatch.setattr("app.core.tasks.ARCHIVES_DIR", archives_dir)
    try:
        db.init_db(database_path)
        batch_id = db.create_batch("orders.xlsx", Path("source.xlsx"))
        db.insert_import_items(batch_id, [order_item()])
        order = db.get_batch_orders(batch_id)[0]
        image_path = orders_dir / str(batch_id) / "SKU-A" / "image.jpg"
        image_path.parent.mkdir(parents=True)
        image_path.write_bytes(b"image")

        archive_path = create_order_archive(batch_id, int(order["id"]))

        with ZipFile(archive_path) as archive:
            assert archive.namelist() == ["SKU-A/image.jpg"]
            assert archive.read("SKU-A/image.jpg") == b"image"
    finally:
        db.DB_PATH = original_path
