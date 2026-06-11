from __future__ import annotations

from pathlib import Path

import pytest

from app.core import database as db
from app.core.excel_parser import OrderItemRow
from app.core.security import hash_password, new_session_token, session_expiry_string
from app.main import (
    extension_download_item_failure,
    extension_download_item_success,
    extension_download_items,
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
        assert payload["items"][0]["sku_folder"] == f"auto-download/batch-{batch_id}/SKU-A"
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
                        "local_path": "auto-download/batch-1/SKU-A/design-1-image.jpg",
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
                "error_code": "extension_download_failed",
                "error_message": "插件失败",
                "error_detail": "Drive API 403",
            },
        )
        assert failed["ok"] is True
        mockup = db.get_download_item(mockup_id)
        assert mockup["status"] == "failed"
        assert mockup["error_message"] == "插件失败"
        assert db.get_batch(batch_id)["status"] == "completed_with_errors"
        counts = db.get_batch_status_counts(batch_id)
        assert counts["downloaded"] == 1
        assert counts["failed"] == 1
    finally:
        db.DB_PATH = original_path
