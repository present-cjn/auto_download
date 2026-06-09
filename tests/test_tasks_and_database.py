from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile

from app.core import database as db
from app.core.excel_parser import OrderItemRow
from app.core.downloader import DriveDownloadTimeout
from app.core.tasks import create_order_archive, parse_batch, process_download_items


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


def test_parse_batch_creates_empty_batch_order_dir(tmp_path: Path, monkeypatch) -> None:
    database_path = tmp_path / "app.db"
    orders_dir = tmp_path / "orders"
    original_path = db.DB_PATH
    db.DB_PATH = database_path
    monkeypatch.setattr("app.core.tasks.ORDERS_DIR", orders_dir)
    monkeypatch.setattr("app.core.tasks.list_worksheet_names", lambda source_path, visible_only=True: ["sheet1"])
    monkeypatch.setattr("app.core.tasks.single_visible_worksheet_name", lambda source_path: "sheet1")
    monkeypatch.setattr("app.core.tasks.parse_order_items", lambda source_path, sheet_name: [order_item()])
    try:
        db.init_db(database_path)
        batch_id = db.create_batch("orders.xlsx", Path("source.xlsx"))

        parse_batch(batch_id, Path("source.xlsx"), sheet_name=None)

        assert (orders_dir / str(batch_id)).is_dir()
        assert db.get_batch(batch_id)["status"] == "review_ready"
    finally:
        db.DB_PATH = original_path


def test_init_db_recovers_stale_downloading_items(tmp_path: Path) -> None:
    database_path = tmp_path / "app.db"
    original_path = db.DB_PATH
    db.DB_PATH = database_path
    try:
        db.init_db(database_path)
        batch_id = db.create_batch("orders.xlsx", Path("source.xlsx"))
        db.insert_import_items(batch_id, [order_item()])
        item = db.get_pending_download_items(batch_id)[0]
        db.update_batch_status(batch_id, "downloading")
        db.mark_download_started(int(item["id"]))

        db.init_db(database_path)

        recovered = db.get_download_item(int(item["id"]))
        batch = db.get_batch(batch_id)
        assert recovered is not None
        assert recovered["status"] == "failed"
        assert recovered["error_code"] == "interrupted"
        assert "服务中断" in recovered["error_message"]
        assert batch is not None
        assert batch["status"] == "completed_with_errors"
        assert int(batch["failed_count"]) == 1
        assert [row["id"] for row in db.get_failed_download_items(batch_id)] == [item["id"]]
    finally:
        db.DB_PATH = original_path


def test_download_timeout_does_not_stop_following_items(tmp_path: Path, monkeypatch) -> None:
    database_path = tmp_path / "app.db"
    orders_dir = tmp_path / "orders"
    cache_dir = tmp_path / "cache"
    original_path = db.DB_PATH
    db.DB_PATH = database_path
    monkeypatch.setattr("app.core.tasks.ORDERS_DIR", orders_dir)
    monkeypatch.setattr("app.core.tasks.CACHE_DIR", cache_dir)

    calls = []

    def fake_cached_drive_folder(url: str, cache_root: Path) -> Path:
        calls.append(url)
        if "slow" in url:
            raise DriveDownloadTimeout("too slow")
        source = cache_root / "ok"
        source.mkdir(parents=True, exist_ok=True)
        (source / "image.jpg").write_bytes(b"jpg")
        return source

    monkeypatch.setattr("app.core.tasks.cached_drive_folder", fake_cached_drive_folder)
    try:
        db.init_db(database_path)
        batch_id = db.create_batch("orders.xlsx", Path("source.xlsx"))
        db.insert_import_items(
            batch_id,
            [
                order_item(
                    row_number=2,
                    sku="SKU-SLOW",
                    design_link="https://drive.google.com/drive/folders/slow",
                ),
                order_item(
                    order_no="ORD-2",
                    row_number=3,
                    sku="SKU-OK",
                    design_link="https://drive.google.com/drive/folders/ok",
                ),
            ],
        )

        process_download_items(batch_id)

        batch = db.get_batch(batch_id)
        status_counts = db.get_batch_status_counts(batch_id)
        assert calls == [
            "https://drive.google.com/drive/folders/slow",
            "https://drive.google.com/drive/folders/ok",
        ]
        assert batch is not None
        assert batch["status"] == "completed_with_errors"
        assert status_counts["failed"] == 1
        assert status_counts["downloaded"] == 1
        assert (orders_dir / str(batch_id) / "SKU-OK" / "image.jpg").exists()
    finally:
        db.DB_PATH = original_path
