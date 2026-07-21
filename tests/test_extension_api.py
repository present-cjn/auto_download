from __future__ import annotations

from pathlib import Path

import pytest

from app.core import database as db
from app.core.downloader import ERROR_LABELS
from app.core.excel_parser import OrderItemRow
from app.core.security import hash_password, new_session_token, session_expiry_string
from app.main import (
    batch_status,
    extension_events,
    extension_download_item_heartbeat,
    extension_download_item_failure,
    extension_download_item_success,
    extension_download_items,
    extension_next_download_item,
    extension_start_download_item,
)


class FakeRequest:
    def __init__(self, token: str | None = None):
        self.cookies = {}
        self.headers = {"x-app-session": token} if token else {}



def order_item(
    sku: str = "SKU-A",
    design_link: str = "https://drive.google.com/file/d/file123/view",
    mockup_link: str = "https://drive.google.com/drive/folders/folder123",
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


def setup_extension_batch(database_path: Path):
    original_path = db.DB_PATH
    db.DB_PATH = database_path
    db.init_db(database_path)
    user_id = db.create_user("op", hash_password("pw"), role="operator")
    other_id = db.create_user("other", hash_password("pw"), role="operator")
    token = new_session_token()
    db.create_session(token, user_id, session_expiry_string())
    batch_id = db.create_batch("orders.xlsx", Path("source.xlsx"), user_id)
    other_batch_id = db.create_batch("other.xlsx", Path("source.xlsx"), other_id)
    db.insert_import_items(batch_id, [order_item()])
    db.insert_import_items(other_batch_id, [order_item(sku="SKU-B")])
    db.update_batch_status(batch_id, "confirmed")
    db.update_batch_status(other_batch_id, "confirmed")
    return original_path, token, batch_id, other_batch_id


def test_extension_download_items_use_header_session_and_batch_access(tmp_path: Path) -> None:
    original_path, token, batch_id, other_batch_id = setup_extension_batch(tmp_path / "app.db")
    try:
        with pytest.raises(Exception) as unauth:
            extension_download_items(FakeRequest(), batch_id)
        assert getattr(unauth.value, "status_code") == 303

        with pytest.raises(Exception) as forbidden:
            extension_download_items(FakeRequest(token), other_batch_id)
        assert getattr(forbidden.value, "status_code") == 403

        payload = extension_download_items(FakeRequest(token), batch_id)
        assert payload["batch"]["id"] == batch_id
        assert [item["source_type"] for item in payload["items"]] == [
            "design",
            "mockup",
        ]
        assert payload["items"][0]["resource_kind"] == "file"
        assert payload["items"][0]["resource_id"] == "file123"
        assert payload["items"][0]["sku_folder"] == "auto-download/orders/SKU-A"
        assert payload["items"][1]["resource_kind"] == "folder"
        assert payload["items"][1]["resource_id"] == "folder123"
    finally:
        db.DB_PATH = original_path


def test_extension_success_and_failure_update_download_state(tmp_path: Path) -> None:
    original_path, token, batch_id, _ = setup_extension_batch(tmp_path / "app.db")
    try:
        request = FakeRequest(token)
        items = extension_download_items(request, batch_id)["items"]
        design_id = items[0]["download_item_id"]
        mockup_id = items[1]["download_item_id"]

        started = extension_start_download_item(request, design_id)
        assert started["ok"] is True
        assert db.get_download_item(design_id)["status"] == "downloading"

        success = extension_download_item_success(
            request,
            design_id,
            {
                "files": [
                    {
                        "file_name": "design-1-image.jpg",
                        "local_path": "auto-download/orders/SKU-A/design-1-image.jpg",
                        "file_size": 12,
                    }
                ]
            },
        )
        assert success["ok"] is True
        assert db.get_download_item(design_id)["status"] == "downloaded"

        failed = extension_download_item_failure(
            request,
            mockup_id,
            {
                "raw_error_code": "extension_google_apps_file",
                "raw_error_message": "链接指向 Google 在线文件。",
                "raw_error_detail": "application/vnd.google-apps.document",
                "files": [
                    {
                        "file_name": "mockup-partial.jpg",
                        "local_path": "auto-download/orders/SKU-A/mockup-partial.jpg",
                        "file_size": 34,
                    }
                ],
                "partial_image_count": 1,
            },
        )
        assert failed["ok"] is True
        assert failed["partial_image_count"] == 1
        mockup = db.get_download_item(mockup_id)
        assert mockup["status"] == "failed"
        assert mockup["error_code"] == "extension_google_apps_file"
        assert mockup["image_count"] == 1
        with db.connect() as conn:
            files = conn.execute(
                """
                SELECT file_name, local_path, file_size
                FROM downloaded_files
                WHERE download_item_id = ?
                """,
                (mockup_id,),
            ).fetchall()
        assert len(files) == 1
        assert files[0]["file_name"] == "mockup-partial.jpg"
        assert db.get_batch(batch_id)["status"] == "completed_with_errors"
        counts = db.get_batch_status_counts(batch_id)
        assert counts["downloaded"] == 1
        assert counts["failed"] == 1
    finally:
        db.DB_PATH = original_path


def test_extension_next_download_item_dispatches_one_item(tmp_path: Path) -> None:
    original_path, token, batch_id, _ = setup_extension_batch(tmp_path / "app.db")
    try:
        request = FakeRequest(token)

        first = extension_next_download_item(request, batch_id)
        assert first["ok"] is True
        assert first["item"]["source_type"] == "design"
        first_id = first["item"]["download_item_id"]
        first_db = db.get_download_item(first_id)
        assert first_db["status"] == "downloading"
        assert first_db["attempt_count"] == 1
        extension_download_item_success(request, first_id, {"image_count": 1})

        second = extension_next_download_item(request, batch_id)
        assert second["item"]["source_type"] == "mockup"
        assert second["item"]["download_item_id"] != first_id
    finally:
        db.DB_PATH = original_path


def test_extension_next_download_item_rejects_unconfirmed_batch(tmp_path: Path) -> None:
    original_path, token, batch_id, _ = setup_extension_batch(tmp_path / "app.db")
    try:
        db.update_batch_status(batch_id, "precheck_ready")

        with pytest.raises(Exception) as exc:
            extension_next_download_item(FakeRequest(token), batch_id)

        assert getattr(exc.value, "status_code") == 400
        assert "尚未确认导入" in exc.value.detail
    finally:
        db.DB_PATH = original_path


def test_extension_failure_retries_retryable_errors_server_side(tmp_path: Path) -> None:
    original_path, token, batch_id, _ = setup_extension_batch(tmp_path / "app.db")
    try:
        request = FakeRequest(token)
        payload = extension_next_download_item(request, batch_id)
        download_item_id = payload["item"]["download_item_id"]

        failed = extension_download_item_failure(
            request,
            download_item_id,
            {
                "raw_error_code": "extension_download_stalled",
                "raw_error_message": "Chrome 下载长时间没有进展。",
                "raw_error_detail": "Chrome download stalled",
            },
        )

        assert failed["status"] == "pending"
        assert failed["retryable"] is True
        item = db.get_download_item(download_item_id)
        assert item["status"] == "pending"
        assert item["error_code"] == "download_timeout"

        redispatched = extension_next_download_item(request, batch_id)
        assert redispatched["item"]["download_item_id"] == download_item_id
        assert db.get_download_item(download_item_id)["attempt_count"] == 2
    finally:
        db.DB_PATH = original_path


def test_extension_failure_stops_retrying_at_max_attempts(tmp_path: Path) -> None:
    original_path, token, batch_id, _ = setup_extension_batch(tmp_path / "app.db")
    try:
        request = FakeRequest(token)
        payload = extension_next_download_item(request, batch_id)
        download_item_id = payload["item"]["download_item_id"]
        with db.connect() as conn:
            conn.execute(
                """
                UPDATE download_items
                SET attempt_count = 4
                WHERE id = ?
                """,
                (download_item_id,),
            )

        failed = extension_download_item_failure(
            request,
            download_item_id,
            {
                "raw_error_code": "extension_download_stalled",
                "raw_error_message": "Chrome 下载长时间没有进展。",
            },
        )

        assert failed["status"] == "failed"
        assert failed["retryable"] is False
        item = db.get_download_item(download_item_id)
        assert item["status"] == "failed"
        assert item["error_code"] == "download_timeout"
    finally:
        db.DB_PATH = original_path


def test_extension_failure_keeps_non_retryable_errors_failed(tmp_path: Path) -> None:
    original_path, token, batch_id, _ = setup_extension_batch(tmp_path / "app.db")
    try:
        request = FakeRequest(token)
        payload = extension_next_download_item(request, batch_id)
        download_item_id = payload["item"]["download_item_id"]

        failed = extension_download_item_failure(
            request,
            download_item_id,
            {
                "raw_error_code": "extension_google_apps_file",
                "raw_error_message": "链接指向 Google 在线文件。",
            },
        )

        assert failed["status"] == "failed"
        assert failed["error_code"] == "extension_google_apps_file"
        assert db.get_download_item(download_item_id)["status"] == "failed"
    finally:
        db.DB_PATH = original_path


def test_extension_events_records_remote_diagnostics(tmp_path: Path) -> None:
    original_path, token, batch_id, _ = setup_extension_batch(tmp_path / "app.db")
    try:
        request = FakeRequest(token)
        response = extension_events(
            request,
            {
                "batch_id": batch_id,
                "event": "download_created",
                "level": "info",
                "message": "SKU-A",
                "detail": {"stage": "download_created"},
            },
        )

        assert response["ok"] is True
        events = db.list_extension_events(batch_id)
        assert events[0]["event"] == "download_created"
        assert events[0]["message"] == "SKU-A"
        assert '"stage": "download_created"' in events[0]["detail_json"]
    finally:
        db.DB_PATH = original_path


def test_extension_heartbeat_updates_downloading_item(tmp_path: Path) -> None:
    original_path, token, batch_id, _ = setup_extension_batch(tmp_path / "app.db")
    try:
        request = FakeRequest(token)
        design_id = extension_download_items(request, batch_id)["items"][0]["download_item_id"]
        extension_start_download_item(request, design_id)
        with db.connect() as conn:
            conn.execute(
                """
                UPDATE download_items
                SET heartbeat_at = datetime('now', '-10 minutes')
                WHERE id = ?
                """,
                (design_id,),
            )

        response = extension_download_item_heartbeat(request, design_id)

        assert response["ok"] is True
        assert response["updated"] is True
        item = db.get_download_item(design_id)
        assert item["heartbeat_at"] is not None
        assert item["status"] == "downloading"
    finally:
        db.DB_PATH = original_path


def test_batch_status_returns_actions_and_current_task(tmp_path: Path) -> None:
    original_path, token, batch_id, _ = setup_extension_batch(tmp_path / "app.db")
    try:
        request = FakeRequest(token)
        design_id = extension_download_items(request, batch_id)["items"][0]["download_item_id"]
        extension_start_download_item(request, design_id)

        payload = batch_status(request, batch_id)

        assert payload["stale_recovered_count"] == 0
        assert payload["status_counts"]["downloading"] == 1
        assert payload["current_task"]["download_item_id"] == design_id
        assert payload["current_task"]["sku"] == "SKU-A"
        assert payload["actions"] == {
            "can_start_local_download": False,
            "can_start_extension": False,
            "can_retry_failed": False,
            "can_refresh": True,
        }
    finally:
        db.DB_PATH = original_path


def test_extension_download_items_recovers_stale_downloading_item(tmp_path: Path) -> None:
    original_path, token, batch_id, _ = setup_extension_batch(tmp_path / "app.db")
    try:
        request = FakeRequest(token)
        design_id = extension_download_items(request, batch_id)["items"][0]["download_item_id"]
        extension_start_download_item(request, design_id)
        with db.connect() as conn:
            conn.execute(
                """
                UPDATE download_items
                SET started_at = datetime('now', '-31 minutes'),
                    heartbeat_at = datetime('now', '-31 minutes')
                WHERE id = ?
                """,
                (design_id,),
            )

        payload = extension_download_items(request, batch_id)

        recovered = db.get_download_item(design_id)
        assert recovered["status"] == "failed"
        assert recovered["error_code"] == "interrupted"
        assert "下载结果未回写" in recovered["error_message"]
        assert any(item["download_item_id"] == design_id for item in payload["items"])
    finally:
        db.DB_PATH = original_path


def test_batch_status_reports_stale_recovery(tmp_path: Path) -> None:
    original_path, token, batch_id, _ = setup_extension_batch(tmp_path / "app.db")
    try:
        request = FakeRequest(token)
        design_id = extension_download_items(request, batch_id)["items"][0]["download_item_id"]
        extension_start_download_item(request, design_id)
        with db.connect() as conn:
            conn.execute(
                """
                UPDATE download_items
                SET started_at = datetime('now', '-31 minutes'),
                    heartbeat_at = datetime('now', '-31 minutes')
                WHERE id = ?
                """,
                (design_id,),
            )

        payload = batch_status(request, batch_id)

        recovered = db.get_download_item(design_id)
        assert payload["stale_recovered_count"] == 1
        assert payload["current_task"] is None
        assert recovered["status"] == "failed"
        assert recovered["error_code"] == "interrupted"
        assert payload["actions"]["can_start_extension"] is True
        assert payload["actions"]["can_retry_failed"] is True
    finally:
        db.DB_PATH = original_path


def test_extension_download_items_keeps_fresh_downloading_item(tmp_path: Path) -> None:
    original_path, token, batch_id, _ = setup_extension_batch(tmp_path / "app.db")
    try:
        request = FakeRequest(token)
        design_id = extension_download_items(request, batch_id)["items"][0]["download_item_id"]
        extension_start_download_item(request, design_id)
        with db.connect() as conn:
            conn.execute(
                """
                UPDATE download_items
                SET started_at = datetime('now', '-31 minutes'),
                    heartbeat_at = datetime('now', '-1 minutes')
                WHERE id = ?
                """,
                (design_id,),
            )

        payload = extension_download_items(request, batch_id)

        current = db.get_download_item(design_id)
        assert current["status"] == "downloading"
        assert all(item["download_item_id"] != design_id for item in payload["items"])
    finally:
        db.DB_PATH = original_path


def test_extension_download_items_prioritize_pending_before_failed(tmp_path: Path) -> None:
    original_path, token, batch_id, _ = setup_extension_batch(tmp_path / "app.db")
    try:
        request = FakeRequest(token)
        items = extension_download_items(request, batch_id)["items"]
        first_id = items[0]["download_item_id"]

        db.mark_download_failed(
            first_id,
            "插件失败",
            error_code="extension_download_failed",
            error_detail="Drive API 403",
        )

        reordered = extension_download_items(request, batch_id)["items"]
        assert [item["status"] for item in reordered] == ["pending", "failed"]
        assert reordered[-1]["download_item_id"] == first_id
    finally:
        db.DB_PATH = original_path


def test_extension_download_items_parse_profile_folder_urls(tmp_path: Path) -> None:
    original_path, token, batch_id, _ = setup_extension_batch(tmp_path / "app.db")
    try:
        request = FakeRequest(token)
        item = db.get_pending_download_items(batch_id)[0]
        with db.connect() as conn:
            conn.execute(
                """
                UPDATE download_items
                SET design_link = ?
                WHERE id = ?
                """,
                (
                    "https://drive.google.com/drive/u/0/folders/13h6YaJa8JlgmYZXlEG0HyGtwGF71wYkh",
                    item["id"],
                ),
            )

        payload = extension_download_items(request, batch_id)["items"][0]
        assert payload["resource_kind"] == "folder"
        assert payload["resource_id"] == "13h6YaJa8JlgmYZXlEG0HyGtwGF71wYkh"
    finally:
        db.DB_PATH = original_path


def test_extension_download_items_parse_open_id_urls(tmp_path: Path) -> None:
    original_path, token, batch_id, _ = setup_extension_batch(tmp_path / "app.db")
    try:
        request = FakeRequest(token)
        item = db.get_pending_download_items(batch_id)[0]
        with db.connect() as conn:
            conn.execute(
                """
                UPDATE download_items
                SET design_link = ?
                WHERE id = ?
                """,
                (
                    "https://drive.google.com/open?id=1sDIPhFZPuUPHO_uWKPEXYZp-XN0rQ52j&usp=drive_copy",
                    item["id"],
                ),
            )

        payload = extension_download_items(request, batch_id)["items"][0]
        assert payload["resource_kind"] == "file"
        assert payload["resource_id"] == "1sDIPhFZPuUPHO_uWKPEXYZp-XN0rQ52j"
    finally:
        db.DB_PATH = original_path


def test_extension_download_items_keep_plain_image_urls(tmp_path: Path) -> None:
    original_path, token, batch_id, _ = setup_extension_batch(tmp_path / "app.db")
    try:
        request = FakeRequest(token)
        item = db.get_pending_download_items(batch_id)[0]
        with db.connect() as conn:
            conn.execute(
                """
                UPDATE download_items
                SET design_link = ?
                WHERE id = ?
                """,
                (
                    "https://cdn.example.com/products/front-view.jpg?token=abc",
                    item["id"],
                ),
            )

        payload = extension_download_items(request, batch_id)["items"][0]
        assert payload["resource_kind"] == "url"
        assert payload["resource_id"] == ""
        assert payload["url"] == "https://cdn.example.com/products/front-view.jpg?token=abc"
    finally:
        db.DB_PATH = original_path


def test_extension_error_labels_include_stop_and_non_image_codes() -> None:
    assert ERROR_LABELS["extension_stopped_by_user"] == "用户停止插件下载"
    assert ERROR_LABELS["extension_non_image_download"] == "下载到非图片"
    assert ERROR_LABELS["extension_google_apps_file"] == "链接不是原始图片"
