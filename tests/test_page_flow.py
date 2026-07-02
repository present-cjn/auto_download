from __future__ import annotations

from pathlib import Path

import pytest

from app import main as app_main
from app.core import database as db
from app.core.excel_parser import OrderItemRow
from app.core.security import hash_password, new_session_token, session_expiry_string


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


def create_user_session(username: str, role: str) -> tuple[int, str]:
    user_id = db.create_user(username, hash_password("pw"), role=role)
    return user_id, session_for_user(user_id)


def order_item(
    order_no: str = "ORD-1",
    row_number: int = 2,
    sku: str = "SKU-A",
    design_link: str = "https://drive.google.com/file/d/file123/view",
) -> OrderItemRow:
    return OrderItemRow(
        order_no=order_no,
        row_number=row_number,
        order_date_raw="46174",
        order_date="2026-06-01",
        design_link=design_link,
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


def test_root_redirects_logged_in_user_to_upload_start(tmp_path: Path) -> None:
    original_path = with_temp_db(tmp_path / "app.db")
    try:
        _user_id, token = create_user_session("op", "operator")

        response = app_main.index(FakeRequest(token))

        assert response.status_code == 303
        assert response.headers["location"] == "/uploads/new"
    finally:
        db.DB_PATH = original_path


def test_batch_and_stats_pages_require_login(tmp_path: Path) -> None:
    original_path = with_temp_db(tmp_path / "app.db")
    try:
        with pytest.raises(Exception) as batches_exc:
            app_main.batches_page(FakeRequest())
        with pytest.raises(Exception) as stats_exc:
            app_main.stats_page(FakeRequest())

        assert getattr(batches_exc.value, "status_code") == 303
        assert getattr(batches_exc.value, "headers")["Location"] == "/login"
        assert getattr(stats_exc.value, "status_code") == 303
        assert getattr(stats_exc.value, "headers")["Location"] == "/login"
    finally:
        db.DB_PATH = original_path


def test_upload_page_shows_recent_three_own_precheck_batches(tmp_path: Path) -> None:
    original_path = with_temp_db(tmp_path / "app.db")
    try:
        user_id, token = create_user_session("op", "operator")
        other_id, _other_token = create_user_session("other", "operator")
        own_ids = []
        for index in range(4):
            batch_id = db.create_batch(f"own-{index}.xlsx", Path("source.xlsx"), user_id)
            db.update_batch_status(batch_id, "precheck_ready")
            own_ids.append(batch_id)
        other_batch_id = db.create_batch("other.xlsx", Path("source.xlsx"), other_id)
        db.update_batch_status(other_batch_id, "precheck_ready")

        page = app_main.upload_page(FakeRequest(token))
        recent_ids = [batch["id"] for batch in page.context["recent_precheck_batches"]]

        assert recent_ids == list(reversed(own_ids[-3:]))
        assert other_batch_id not in recent_ids
    finally:
        db.DB_PATH = original_path


def test_batches_page_groups_and_team_scope(tmp_path: Path) -> None:
    original_path = with_temp_db(tmp_path / "app.db")
    try:
        admin_id, admin_token = create_user_session("admin", "admin")
        other_id, _other_token = create_user_session("op", "operator")
        precheck_id = db.create_batch("precheck.xlsx", Path("source.xlsx"), admin_id)
        formal_id = db.create_batch("formal.xlsx", Path("source.xlsx"), admin_id)
        discarded_id = db.create_batch("discarded.xlsx", Path("source.xlsx"), admin_id)
        other_batch_id = db.create_batch("other.xlsx", Path("source.xlsx"), other_id)
        db.update_batch_status(precheck_id, "precheck_ready")
        db.update_batch_status(formal_id, "confirmed")
        db.update_batch_status(discarded_id, "discarded")
        db.update_batch_status(other_batch_id, "confirmed")

        my_page = app_main.batches_page(FakeRequest(admin_token))
        team_page = app_main.batches_page(FakeRequest(admin_token), scope="team")

        assert my_page.context["active_scope"] == "my"
        assert {batch["id"] for batch in my_page.context["batches"]} == {
            precheck_id,
            formal_id,
            discarded_id,
        }
        assert [batch["id"] for batch in my_page.context["batch_groups"]["precheck"]] == [precheck_id]
        assert [batch["id"] for batch in my_page.context["batch_groups"]["formal"]] == [formal_id]
        assert [batch["id"] for batch in my_page.context["batch_groups"]["discarded"]] == [discarded_id]
        assert team_page.context["active_scope"] == "team"
        assert other_batch_id in {batch["id"] for batch in team_page.context["batches"]}
    finally:
        db.DB_PATH = original_path


def test_stats_page_uses_role_visibility(tmp_path: Path) -> None:
    original_path = with_temp_db(tmp_path / "app.db")
    try:
        admin_id, admin_token = create_user_session("admin", "admin")
        operator_id, operator_token = create_user_session("op", "operator")
        admin_batch_id = db.create_batch("admin.xlsx", Path("source.xlsx"), admin_id)
        operator_batch_id = db.create_batch("operator.xlsx", Path("source.xlsx"), operator_id)
        db.update_batch_status(admin_batch_id, "confirmed")
        db.update_batch_status(operator_batch_id, "confirmed")
        with db.connect() as conn:
            conn.execute(
                """
                UPDATE import_batches
                SET order_count = 2, item_count = 2, link_count = 2, success_count = 1
                WHERE id = ?
                """,
                (admin_batch_id,),
            )
            conn.execute(
                """
                UPDATE import_batches
                SET order_count = 3, item_count = 3, link_count = 3, failed_count = 1
                WHERE id = ?
                """,
                (operator_batch_id,),
            )

        admin_page = app_main.stats_page(FakeRequest(admin_token))
        operator_page = app_main.stats_page(FakeRequest(operator_token))

        assert {row["user_id"] for row in admin_page.context["usage_rows"]} == {
            admin_id,
            operator_id,
        }
        assert [row["user_id"] for row in operator_page.context["usage_rows"]] == [
            operator_id
        ]
        assert admin_page.context["usage_totals"]["formal_batch_count"] == 2
        assert operator_page.context["usage_totals"]["formal_batch_count"] == 1
    finally:
        db.DB_PATH = original_path


def test_stats_page_includes_batch_download_durations_by_role(tmp_path: Path) -> None:
    original_path = with_temp_db(tmp_path / "app.db")
    try:
        admin_id, admin_token = create_user_session("admin", "admin")
        operator_id, operator_token = create_user_session("op", "operator")
        admin_batch_id = db.create_batch("admin.xlsx", Path("source.xlsx"), admin_id)
        operator_batch_id = db.create_batch("operator.xlsx", Path("source.xlsx"), operator_id)
        db.insert_import_items(admin_batch_id, [order_item(order_no="ADMIN-1", sku="ADMIN-SKU")])
        db.insert_import_items(operator_batch_id, [order_item(order_no="OP-1", sku="OP-SKU")])
        db.update_batch_status(admin_batch_id, "completed")
        db.update_batch_status(operator_batch_id, "completed")
        with db.connect() as conn:
            conn.execute(
                """
                UPDATE download_items
                SET status = 'downloaded',
                    started_at = '2026-06-01 10:00:00',
                    completed_at = '2026-06-01 10:02:30'
                WHERE batch_id = ?
                """,
                (admin_batch_id,),
            )
            conn.execute(
                """
                UPDATE download_items
                SET status = 'downloaded',
                    started_at = '2026-06-01 11:00:00',
                    completed_at = '2026-06-01 11:01:05'
                WHERE batch_id = ?
                """,
                (operator_batch_id,),
            )

        admin_page = app_main.stats_page(FakeRequest(admin_token))
        operator_page = app_main.stats_page(FakeRequest(operator_token))

        assert {row["id"] for row in admin_page.context["batch_duration_rows"]} == {
            admin_batch_id,
            operator_batch_id,
        }
        assert [row["id"] for row in operator_page.context["batch_duration_rows"]] == [
            operator_batch_id
        ]
        assert operator_page.context["batch_duration_rows"][0]["duration_label"] == "1m 5s"
    finally:
        db.DB_PATH = original_path


def test_upload_page_no_longer_shows_workflow_steps() -> None:
    template = Path("templates/upload.html").read_text(encoding="utf-8")

    assert "1. 上传表格" not in template
    assert "2. 预检核对" not in template
    assert "3. 启动下载" not in template
    assert "upload-workflow" not in template


def test_download_name_edit_lives_only_on_precheck_tab() -> None:
    template = Path("templates/batch_detail.html").read_text(encoding="utf-8")
    download_tab = template.split('{% if active_tab == "download" %}', 1)[1]

    assert template.count('action="/batches/{{ batch.id }}/download-name"') == 1
    assert 'action="/batches/{{ batch.id }}/download-name"' not in download_tab


def test_server_fallback_controls_are_start_pause_continue_style() -> None:
    template = Path("templates/batch_detail.html").read_text(encoding="utf-8")

    assert "服务器下载 10 项" not in template
    assert "服务器下载 20 项" not in template
    assert "服务器继续全部" not in template
    assert "服务器重试 10 个失败项" not in template
    assert "服务器重试全部失败项" not in template
    assert 'action="/batches/{{ batch.id }}/server-download/pause"' in template
    assert "暂停会在当前项结束后生效" in template
    assert "打开链接" in template


def test_pause_server_download_sets_stop_request(tmp_path: Path) -> None:
    original_path = with_temp_db(tmp_path / "app.db")
    try:
        developer_id, token = create_user_session("dev", "developer")
        batch_id = db.create_batch("orders.xlsx", Path("source.xlsx"), developer_id)
        db.insert_import_items(batch_id, [order_item()])
        db.update_batch_status(batch_id, "processing")

        response = app_main.pause_server_download(FakeRequest(token), batch_id)

        assert response.status_code == 303
        batch = db.get_batch(batch_id)
        assert batch is not None
        assert int(batch["server_stop_requested"]) == 1
    finally:
        db.DB_PATH = original_path


def test_quota_navigation_is_developer_only_and_stats_omit_quota_hint() -> None:
    base_template = Path("templates/base.html").read_text(encoding="utf-8")
    stats_template = Path("templates/stats.html").read_text(encoding="utf-8")

    assert 'current_user and current_user.role == "developer"' in base_template
    assert 'href="/quota">套餐' in base_template
    assert "套餐额度" not in stats_template
