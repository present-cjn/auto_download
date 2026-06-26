from __future__ import annotations

from pathlib import Path

import pytest

from app.core import database as db
from app.core.excel_parser import OrderItemRow
from app.core.security import hash_password, new_session_token, session_expiry_string
from app.main import quota_page, update_quota


class FakeRequest:
    def __init__(self, token: str | None = None):
        self.cookies = {}
        self.headers = {"x-app-session": token} if token else {}


def with_temp_db(database_path: Path):
    original_path = db.DB_PATH
    db.DB_PATH = database_path
    db.init_db(database_path)
    return original_path


def session_for_user(user_id: int) -> str:
    token = new_session_token()
    db.create_session(token, user_id, session_expiry_string())
    return token


def order_item(row_number: int = 2, sku: str = "SKU-A") -> OrderItemRow:
    return OrderItemRow(
        order_no=f"ORD-{row_number}",
        row_number=row_number,
        order_date_raw="46174",
        order_date="2026-06-01",
        design_link=f"https://drive.google.com/file/d/file{row_number}/view",
        mockup_link="",
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


def create_download_item(user_id: int, row_number: int = 2) -> dict:
    batch_id = db.create_batch(f"orders-{row_number}.xlsx", Path("source.xlsx"), user_id)
    db.insert_import_items(batch_id, [order_item(row_number=row_number)])
    db.update_batch_status(batch_id, "confirmed")
    return db.get_pending_download_items(batch_id)[0]


def set_completed_at(download_item_id: int, completed_at: str | None) -> None:
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE download_items
            SET completed_at = ?
            WHERE id = ?
            """,
            (completed_at, download_item_id),
        )


def usage_by_username(rows: list[dict]) -> dict[str, dict]:
    return {row["username"]: row for row in rows}


def test_monthly_quota_counts_successful_links_only_and_excludes_developer(
    tmp_path: Path,
) -> None:
    original_path = with_temp_db(tmp_path / "app.db")
    try:
        developer_id = db.create_user("dev", hash_password("pw"), role="developer")
        admin_id = db.create_user("admin1", hash_password("pw"), role="admin")
        operator_id = db.create_user("op", hash_password("pw"), role="operator")

        admin_item = create_download_item(admin_id, 2)
        db.mark_download_success(int(admin_item["id"]), image_count=5)
        set_completed_at(int(admin_item["id"]), "2026-06-15 10:00:00")

        developer_item = create_download_item(developer_id, 3)
        db.mark_download_success(int(developer_item["id"]), image_count=1)
        set_completed_at(int(developer_item["id"]), "2026-06-15 10:00:00")

        next_month_item = create_download_item(admin_id, 4)
        db.mark_download_success(int(next_month_item["id"]), image_count=1)
        set_completed_at(int(next_month_item["id"]), "2026-07-01 00:00:00")

        failed_item = create_download_item(operator_id, 5)
        db.mark_download_failed(int(failed_item["id"]), "failed")
        manual_item = create_download_item(operator_id, 6)
        db.mark_download_manual_done(int(manual_item["id"]))
        create_download_item(operator_id, 7)

        db.set_monthly_team_quota("2026-06", 10, developer_id, "June quota")
        quota = db.get_monthly_team_quota("2026-06")
        rows = db.list_monthly_quota_usage_by_user("2026-06")
        totals = db.monthly_quota_totals(quota, rows)
        by_username = usage_by_username(rows)

        assert by_username["admin1"]["downloaded_count"] == 1
        assert by_username["op"]["downloaded_count"] == 0
        assert "dev" not in by_username
        assert totals["total_quota"] == 10
        assert totals["used_count"] == 1
        assert totals["remaining_count"] == 9
        assert totals["overage_count"] == 0
    finally:
        db.DB_PATH = original_path


def test_monthly_quota_overage_and_unassigned_success_count(tmp_path: Path) -> None:
    original_path = with_temp_db(tmp_path / "app.db")
    try:
        developer_id = db.create_user("dev", hash_password("pw"), role="developer")
        admin_id = db.create_user("admin1", hash_password("pw"), role="admin")
        item = create_download_item(admin_id, 2)
        db.mark_download_success(int(item["id"]), image_count=1)
        set_completed_at(int(item["id"]), "2026-06-15 10:00:00")
        unassigned_item = create_download_item(admin_id, 3)
        db.mark_download_success(int(unassigned_item["id"]), image_count=1)
        set_completed_at(int(unassigned_item["id"]), None)

        db.set_monthly_team_quota("2026-06", 0, developer_id)
        quota = db.get_monthly_team_quota("2026-06")
        rows = db.list_monthly_quota_usage_by_user("2026-06")
        totals = db.monthly_quota_totals(quota, rows)

        assert totals["used_count"] == 1
        assert totals["remaining_count"] == 0
        assert totals["overage_count"] == 1
        assert db.count_unassigned_successful_downloads() == 1
    finally:
        db.DB_PATH = original_path


def test_quota_page_visibility_and_developer_update(tmp_path: Path) -> None:
    original_path = with_temp_db(tmp_path / "app.db")
    try:
        developer_id = db.create_user("dev", hash_password("pw"), role="developer")
        admin_id = db.create_user("admin1", hash_password("pw"), role="admin")
        operator_id = db.create_user("op", hash_password("pw"), role="operator")
        developer_token = session_for_user(developer_id)
        admin_token = session_for_user(admin_id)
        operator_token = session_for_user(operator_id)
        item = create_download_item(operator_id, 2)
        db.mark_download_success(int(item["id"]), image_count=1)
        set_completed_at(int(item["id"]), "2026-06-15 10:00:00")

        response = update_quota(
            FakeRequest(developer_token),
            quota_month="2026-06",
            total_quota=3,
            note="test quota",
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/quota?month=2026-06"

        with pytest.raises(Exception) as admin_update:
            update_quota(
                FakeRequest(admin_token),
                quota_month="2026-06",
                total_quota=4,
            )
        assert getattr(admin_update.value, "status_code") == 403

        admin_page = quota_page(FakeRequest(admin_token), month="2026-06")
        operator_page = quota_page(FakeRequest(operator_token), month="2026-06")

        assert admin_page.context["quota_totals"]["used_count"] == 1
        assert len(admin_page.context["quota_usage_rows"]) == 2
        assert len(operator_page.context["quota_usage_rows"]) == 1
        assert operator_page.context["quota_usage_rows"][0]["username"] == "op"
        assert operator_page.context["quota_usage_rows"][0]["downloaded_count"] == 1
    finally:
        db.DB_PATH = original_path
