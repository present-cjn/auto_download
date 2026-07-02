from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile

from app.core import database as db
from app.core.excel_parser import OrderItemRow
from app.core.downloader import DriveDownloadError, DriveDownloadTimeout
from app.core.tasks import (
    RATE_LIMIT_PAUSE_MESSAGE,
    SERVER_DOWNLOAD_PAUSE_MESSAGE,
    configured_download_delay_seconds,
    configured_retry_backoff_seconds,
    create_order_archive,
    parse_batch,
    process_download_items,
)


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


def test_database_connection_uses_busy_timeout_and_wal(tmp_path: Path) -> None:
    database_path = tmp_path / "app.db"
    with db.connect(database_path) as conn:
        busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]

    assert busy_timeout == db.SQLITE_BUSY_TIMEOUT_MS
    assert journal_mode.lower() == "wal"


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
                    mockup_link="https://drive.google.com/drive/folders/mockup123"
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


def test_duplicate_mockup_link_is_not_inserted_as_download_item(tmp_path: Path) -> None:
    database_path = tmp_path / "app.db"
    original_path = db.DB_PATH
    db.DB_PATH = database_path
    try:
        db.init_db(database_path)
        batch_id = db.create_batch("orders.xlsx", Path("source.xlsx"))
        link = "https://drive.google.com/drive/folders/folder123"
        db.insert_import_items(
            batch_id,
            [
                order_item(
                    design_link=link,
                    mockup_link=f" {link} ",
                )
            ],
        )

        items = db.get_pending_download_items(batch_id)

        assert len(items) == 1
        assert items[0]["source_type"] == "design"
        assert items[0]["design_link"] == link
    finally:
        db.DB_PATH = original_path


def test_claim_batch_processing_is_atomic(tmp_path: Path) -> None:
    database_path = tmp_path / "app.db"
    original_path = db.DB_PATH
    db.DB_PATH = database_path
    try:
        db.init_db(database_path)
        batch_id = db.create_batch("orders.xlsx", Path("source.xlsx"))
        db.update_batch_status(batch_id, "confirmed")

        assert db.claim_batch_processing(batch_id) is True
        assert db.claim_batch_processing(batch_id) is False
        assert db.get_batch(batch_id)["status"] == "processing"
    finally:
        db.DB_PATH = original_path


def test_dispatch_download_item_claims_once(tmp_path: Path) -> None:
    database_path = tmp_path / "app.db"
    original_path = db.DB_PATH
    db.DB_PATH = database_path
    try:
        db.init_db(database_path)
        batch_id = db.create_batch("orders.xlsx", Path("source.xlsx"))
        db.insert_import_items(batch_id, [order_item()])
        item = db.get_pending_download_items(batch_id)[0]

        claimed = db.dispatch_download_item(int(item["id"]))
        duplicate = db.dispatch_download_item(int(item["id"]))

        assert claimed is not None
        assert claimed["status"] == "downloading"
        assert duplicate is None
    finally:
        db.DB_PATH = original_path


def test_enqueue_production_outbox_for_ready_items_is_idempotent(tmp_path: Path) -> None:
    database_path = tmp_path / "app.db"
    original_path = db.DB_PATH
    db.DB_PATH = database_path
    try:
        db.init_db(database_path)
        batch_id = db.create_batch("orders.xlsx", Path("source.xlsx"))
        db.insert_import_items(batch_id, [order_item()])
        item = db.get_pending_download_items(batch_id)[0]
        db.mark_download_success(int(item["id"]), 1)

        assert db.enqueue_production_outbox_for_batch(batch_id) == 1
        assert db.enqueue_production_outbox_for_batch(batch_id) == 0

        with db.connect(database_path) as conn:
            rows = conn.execute("SELECT * FROM production_outbox").fetchall()
        assert len(rows) == 1
        assert rows[0]["status"] == "pending_push"
        assert rows[0]["idempotency_key"] == f"batch:{batch_id}:order:{item['order_id']}:sku:SKU-A"
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
        assert db.get_batch(batch_id)["status"] == "precheck_ready"
    finally:
        db.DB_PATH = original_path


def test_confirm_and_discard_batch_status_flow(tmp_path: Path) -> None:
    database_path = tmp_path / "app.db"
    original_path = db.DB_PATH
    db.DB_PATH = database_path
    try:
        db.init_db(database_path)
        batch_id = db.create_batch("orders.xlsx", Path("source.xlsx"))

        assert db.get_batch(batch_id)["status"] == "uploaded"
        assert db.confirm_batch(batch_id) is False

        db.update_batch_status(batch_id, "precheck_ready")
        assert db.confirm_batch(batch_id) is True
        assert db.get_batch(batch_id)["status"] == "confirmed"

        assert db.discard_batch(batch_id) is True
        assert db.get_batch(batch_id)["status"] == "discarded"
        assert db.discard_batch(batch_id) is False
    finally:
        db.DB_PATH = original_path


def test_init_db_normalizes_legacy_batch_statuses(tmp_path: Path) -> None:
    database_path = tmp_path / "app.db"
    original_path = db.DB_PATH
    db.DB_PATH = database_path
    try:
        db.init_db(database_path)
        batch_id = db.create_batch("orders.xlsx", Path("source.xlsx"))
        db.update_batch_status(batch_id, "review_ready")

        db.init_db(database_path)

        assert db.get_batch(batch_id)["status"] == "confirmed"
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
    monkeypatch.setattr("app.core.tasks.sleep_between_download_items", lambda: None)
    monkeypatch.setattr("app.core.tasks.configured_retry_backoff_seconds", lambda: [])

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
        db.update_batch_status(batch_id, "confirmed")

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


def test_plain_image_url_downloads_through_server_task(tmp_path: Path, monkeypatch) -> None:
    database_path = tmp_path / "app.db"
    orders_dir = tmp_path / "orders"
    cache_dir = tmp_path / "cache"
    original_path = db.DB_PATH
    db.DB_PATH = database_path
    monkeypatch.setattr("app.core.tasks.ORDERS_DIR", orders_dir)
    monkeypatch.setattr("app.core.tasks.CACHE_DIR", cache_dir)
    monkeypatch.setattr("app.core.tasks.sleep_between_download_items", lambda: None)
    monkeypatch.setattr("app.core.tasks.configured_retry_backoff_seconds", lambda: [])
    monkeypatch.setattr(
        "app.core.downloader.run_download_with_timeout",
        lambda download_func, resource_id, output_dir: download_func(resource_id, output_dir),
    )

    class FakeResponse:
        status_code = 200
        headers = {"Content-Type": "image/jpeg"}

        def iter_content(self, chunk_size: int):
            yield b"jpg"

        def close(self) -> None:
            pass

    calls = []

    def fake_get(url: str, stream: bool, timeout: int, headers: dict[str, str]):
        calls.append((url, stream, timeout, headers))
        return FakeResponse()

    monkeypatch.setattr("app.core.downloader.requests.get", fake_get)
    try:
        db.init_db(database_path)
        batch_id = db.create_batch("orders.xlsx", Path("source.xlsx"))
        url = "https://cdn.example.com/products/front-view.jpg?token=abc"
        db.insert_import_items(
            batch_id,
            [
                order_item(
                    row_number=2,
                    sku="SKU-DIRECT",
                    design_link=url,
                )
            ],
        )
        db.update_batch_status(batch_id, "confirmed")

        process_download_items(batch_id)

        item = db.get_batch_orders(batch_id)[0]["items"][0]["download_items"][0]
        status_counts = db.get_batch_status_counts(batch_id)
        assert calls[0][0:3] == (url, True, 180)
        assert "Mozilla/5.0" in calls[0][3]["User-Agent"]
        assert "Referer" not in calls[0][3]
        assert status_counts["downloaded"] == 1
        assert item["download_status"] == "downloaded"
        assert item["download_image_count"] == 1
        assert (orders_dir / str(batch_id) / "SKU-DIRECT" / "front-view.jpg").read_bytes() == b"jpg"
    finally:
        db.DB_PATH = original_path


def test_server_download_pause_stops_after_current_item_and_can_continue(tmp_path: Path, monkeypatch) -> None:
    database_path = tmp_path / "app.db"
    orders_dir = tmp_path / "orders"
    cache_dir = tmp_path / "cache"
    original_path = db.DB_PATH
    db.DB_PATH = database_path
    monkeypatch.setattr("app.core.tasks.ORDERS_DIR", orders_dir)
    monkeypatch.setattr("app.core.tasks.CACHE_DIR", cache_dir)
    monkeypatch.setattr("app.core.tasks.sleep_between_download_items", lambda: None)
    monkeypatch.setattr("app.core.tasks.configured_retry_backoff_seconds", lambda: [])

    calls = []

    def fake_cached_drive_folder(url: str, cache_root: Path) -> Path:
        calls.append(url)
        source = cache_root / str(len(calls))
        source.mkdir(parents=True, exist_ok=True)
        (source / f"image-{len(calls)}.jpg").write_bytes(b"jpg")
        if len(calls) == 1:
            db.request_server_download_stop(batch_id)
        return source

    monkeypatch.setattr("app.core.tasks.cached_drive_folder", fake_cached_drive_folder)
    try:
        db.init_db(database_path)
        batch_id = db.create_batch("orders.xlsx", Path("source.xlsx"))
        db.insert_import_items(
            batch_id,
            [
                order_item(row_number=2, sku="SKU-1", design_link="https://drive.google.com/drive/folders/1"),
                order_item(order_no="ORD-2", row_number=3, sku="SKU-2", design_link="https://drive.google.com/drive/folders/2"),
            ],
        )
        db.update_batch_status(batch_id, "confirmed")

        process_download_items(batch_id)

        batch = db.get_batch(batch_id)
        status_counts = db.get_batch_status_counts(batch_id)
        assert calls == ["https://drive.google.com/drive/folders/1"]
        assert batch is not None
        assert batch["status"] == "confirmed"
        assert batch["error_message"] == SERVER_DOWNLOAD_PAUSE_MESSAGE
        assert int(batch["server_stop_requested"]) == 0
        assert status_counts["downloaded"] == 1
        assert status_counts["pending"] == 1

        process_download_items(batch_id)

        batch = db.get_batch(batch_id)
        status_counts = db.get_batch_status_counts(batch_id)
        assert calls == [
            "https://drive.google.com/drive/folders/1",
            "https://drive.google.com/drive/folders/2",
        ]
        assert batch is not None
        assert batch["status"] == "completed"
        assert status_counts["downloaded"] == 2
        assert status_counts["pending"] == 0
    finally:
        db.DB_PATH = original_path


def test_download_config_parsing(monkeypatch) -> None:
    monkeypatch.setenv("DRIVE_DOWNLOAD_DELAY_SECONDS", "12")
    monkeypatch.setenv("DRIVE_ITEM_RETRY_BACKOFF_SECONDS", "1, 2, bad, 0")

    assert configured_download_delay_seconds() == 12
    assert configured_retry_backoff_seconds() == [1, 2, 0]

    monkeypatch.setenv("DRIVE_DOWNLOAD_DELAY_SECONDS", "bad")
    monkeypatch.setenv("DRIVE_ITEM_RETRY_BACKOFF_SECONDS", "bad")

    assert configured_download_delay_seconds() == 8
    assert configured_retry_backoff_seconds() == [30, 90]


def test_rate_limited_download_retries_and_succeeds(tmp_path: Path, monkeypatch) -> None:
    database_path = tmp_path / "app.db"
    orders_dir = tmp_path / "orders"
    cache_dir = tmp_path / "cache"
    original_path = db.DB_PATH
    db.DB_PATH = database_path
    monkeypatch.setattr("app.core.tasks.ORDERS_DIR", orders_dir)
    monkeypatch.setattr("app.core.tasks.CACHE_DIR", cache_dir)
    monkeypatch.setattr("app.core.tasks.configured_retry_backoff_seconds", lambda: [0, 0])

    calls = []

    def fake_cached_drive_folder(url: str, cache_root: Path) -> Path:
        calls.append(url)
        if len(calls) == 1:
            raise DriveDownloadError("FileURLRetrievalError: Cannot retrieve the public link")
        source = cache_root / "ok"
        source.mkdir(parents=True, exist_ok=True)
        (source / "image.jpg").write_bytes(b"jpg")
        return source

    monkeypatch.setattr("app.core.tasks.cached_drive_folder", fake_cached_drive_folder)
    try:
        db.init_db(database_path)
        batch_id = db.create_batch("orders.xlsx", Path("source.xlsx"))
        db.insert_import_items(batch_id, [order_item()])
        db.update_batch_status(batch_id, "confirmed")

        process_download_items(batch_id)

        batch = db.get_batch(batch_id)
        status_counts = db.get_batch_status_counts(batch_id)
        item = db.get_pending_download_items(batch_id)
        assert calls == [
            "https://drive.google.com/drive/folders/folder123",
            "https://drive.google.com/drive/folders/folder123",
        ]
        assert batch is not None
        assert batch["status"] == "completed"
        assert status_counts["downloaded"] == 1
        assert status_counts["failed"] == 0
        assert item == []
        assert (orders_dir / str(batch_id) / "SKU-A" / "image.jpg").exists()
    finally:
        db.DB_PATH = original_path


def test_download_items_limit_processes_only_selected_count(tmp_path: Path, monkeypatch) -> None:
    database_path = tmp_path / "app.db"
    orders_dir = tmp_path / "orders"
    cache_dir = tmp_path / "cache"
    original_path = db.DB_PATH
    db.DB_PATH = database_path
    monkeypatch.setattr("app.core.tasks.ORDERS_DIR", orders_dir)
    monkeypatch.setattr("app.core.tasks.CACHE_DIR", cache_dir)
    monkeypatch.setattr("app.core.tasks.sleep_between_download_items", lambda: None)

    calls = []

    def fake_cached_drive_folder(url: str, cache_root: Path) -> Path:
        calls.append(url)
        source = cache_root / str(len(calls))
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
                order_item(row_number=2, sku="SKU-1", design_link="https://drive.google.com/drive/folders/1"),
                order_item(order_no="ORD-2", row_number=3, sku="SKU-2", design_link="https://drive.google.com/drive/folders/2"),
                order_item(order_no="ORD-3", row_number=4, sku="SKU-3", design_link="https://drive.google.com/drive/folders/3"),
            ],
        )
        db.update_batch_status(batch_id, "confirmed")

        process_download_items(batch_id, limit=2)

        batch = db.get_batch(batch_id)
        status_counts = db.get_batch_status_counts(batch_id)
        assert calls == [
            "https://drive.google.com/drive/folders/1",
            "https://drive.google.com/drive/folders/2",
        ]
        assert batch is not None
        assert status_counts["downloaded"] == 2
        assert status_counts["pending"] == 1
        assert batch["status"] == "confirmed"
    finally:
        db.DB_PATH = original_path


def test_consecutive_rate_limits_pause_batch(tmp_path: Path, monkeypatch) -> None:
    database_path = tmp_path / "app.db"
    orders_dir = tmp_path / "orders"
    cache_dir = tmp_path / "cache"
    original_path = db.DB_PATH
    db.DB_PATH = database_path
    monkeypatch.setattr("app.core.tasks.ORDERS_DIR", orders_dir)
    monkeypatch.setattr("app.core.tasks.CACHE_DIR", cache_dir)
    monkeypatch.setattr("app.core.tasks.sleep_between_download_items", lambda: None)
    monkeypatch.setattr("app.core.tasks.configured_retry_backoff_seconds", lambda: [])

    calls = []

    def fake_cached_drive_folder(url: str, cache_root: Path) -> Path:
        calls.append(url)
        raise DriveDownloadError("FileURLRetrievalError: Cannot retrieve the public link")

    monkeypatch.setattr("app.core.tasks.cached_drive_folder", fake_cached_drive_folder)
    try:
        db.init_db(database_path)
        batch_id = db.create_batch("orders.xlsx", Path("source.xlsx"))
        db.insert_import_items(
            batch_id,
            [
                order_item(row_number=2, sku="SKU-1", design_link="https://drive.google.com/drive/folders/1"),
                order_item(order_no="ORD-2", row_number=3, sku="SKU-2", design_link="https://drive.google.com/drive/folders/2"),
                order_item(order_no="ORD-3", row_number=4, sku="SKU-3", design_link="https://drive.google.com/drive/folders/3"),
            ],
        )
        db.update_batch_status(batch_id, "confirmed")

        process_download_items(batch_id)

        batch = db.get_batch(batch_id)
        status_counts = db.get_batch_status_counts(batch_id)
        assert calls == [
            "https://drive.google.com/drive/folders/1",
            "https://drive.google.com/drive/folders/2",
        ]
        assert batch is not None
        assert batch["status"] == "completed_with_errors"
        assert batch["error_message"] == RATE_LIMIT_PAUSE_MESSAGE
        assert status_counts["failed"] == 2
        assert status_counts["pending"] == 1
    finally:
        db.DB_PATH = original_path
