from __future__ import annotations

from pathlib import Path

from app.core import database as db
from app.core.excel_parser import OrderItemRow
from scripts.download_speed_report import build_speed_report, render_text_report


def order_item(
    sku: str = "SKU-A",
    design_link: str = "https://drive.google.com/file/d/file123/view",
    mockup_link: str = "",
) -> OrderItemRow:
    return OrderItemRow(
        order_no="ORD-1",
        row_number=2,
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


def add_event(
    batch_id: int,
    download_item_id: int | None,
    event: str,
    created_at: str,
    detail: dict | None = None,
    message: str = "",
) -> None:
    event_id = db.record_extension_event(
        batch_id=batch_id,
        download_item_id=download_item_id,
        event=event,
        message=message,
        detail=detail,
    )
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE extension_events
            SET created_at = ?
            WHERE id = ?
            """,
            (created_at, event_id),
        )


def setup_report_batch(database_path: Path) -> tuple[Path, int, int]:
    original_path = db.DB_PATH
    db.DB_PATH = database_path
    db.init_db(database_path)
    batch_id = db.create_batch("orders.xlsx", Path("source.xlsx"))
    db.insert_import_items(batch_id, [order_item()])
    item = db.get_pending_download_items(batch_id)[0]
    return original_path, batch_id, int(item["id"])


def test_speed_report_summarizes_completed_download(tmp_path: Path) -> None:
    original_path, batch_id, item_id = setup_report_batch(tmp_path / "app.db")
    try:
        db.mark_download_started(item_id)
        db.add_downloaded_file(
            download_item_id=item_id,
            batch_id=batch_id,
            order_id=int(db.get_download_item(item_id)["order_id"]),
            order_no="ORD-1",
            file_name="image.jpg",
            local_path=Path("auto-download/batch-1/SKU-A/image.jpg"),
            file_size=1024,
        )
        db.mark_download_success(item_id, 1)
        add_event(batch_id, None, "queue_start", "2026-06-24 10:00:00")
        add_event(batch_id, item_id, "task_start", "2026-06-24 10:00:02")
        add_event(batch_id, item_id, "folder_list_start", "2026-06-24 10:00:03")
        add_event(batch_id, item_id, "folder_list_done", "2026-06-24 10:00:08", {"total": 1})
        add_event(
            batch_id,
            item_id,
            "download_complete",
            "2026-06-24 10:00:20",
            {"elapsedMs": 12000},
        )
        add_event(batch_id, item_id, "success_post_start", "2026-06-24 10:00:21")
        add_event(batch_id, item_id, "success_post_done", "2026-06-24 10:00:22")
        add_event(batch_id, item_id, "task_success", "2026-06-24 10:00:23")
        add_event(batch_id, None, "queue_stop", "2026-06-24 10:00:25")

        report = build_speed_report(batch_id, tmp_path / "app.db")
        text = render_text_report(report)

        assert report["queue_seconds"] == 25.0
        assert report["download_item_count"] == 1
        assert report["status_counts"] == {"downloaded": 1}
        assert report["item_seconds"]["median"] == 21.0
        assert report["folder_list_seconds"]["median"] == 5.0
        assert report["download_seconds"]["median"] == 12.0
        assert report["slowest_items"][0]["file_count"] == 1
        assert "Download speed report for batch" in text
        assert "Chrome download duration" in text
    finally:
        db.DB_PATH = original_path


def test_speed_report_tracks_retry_and_incomplete_items(tmp_path: Path) -> None:
    original_path, batch_id, item_id = setup_report_batch(tmp_path / "app.db")
    try:
        db.mark_download_started(item_id)
        add_event(batch_id, None, "queue_start", "2026-06-24 11:00:00")
        add_event(batch_id, item_id, "task_start", "2026-06-24 11:00:01")
        add_event(
            batch_id,
            item_id,
            "retry_wait",
            "2026-06-24 11:00:05",
            {"waitMs": 3000, "attempt": 1},
        )

        report = build_speed_report(batch_id, tmp_path / "app.db")

        assert report["queue_seconds"] is None
        assert report["retry_count"] == 1
        assert report["retry_wait_seconds"] == 3.0
        assert report["incomplete_item_count"] == 1
        assert report["slowest_items"][0]["complete"] is False
    finally:
        db.DB_PATH = original_path
