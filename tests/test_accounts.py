from __future__ import annotations

from pathlib import Path

import pytest

from app.core import database as db
from app.core.excel_parser import OrderItemRow
from app.core.security import (
    hash_password,
    new_session_token,
    session_expiry_string,
    utc_now_string,
    verify_password,
)
from app.main import (
    delete_batch as delete_batch_route,
    download_archive,
    download_source_file,
    ensure_initial_accounts,
    enrich_batch_work_queue,
    require_batch_access,
    require_batch_operation,
    update_batch_download_name as update_batch_download_name_route,
)


class FakeRequest:
    def __init__(self, token: str | None = None):
        self.cookies = {}
        self.headers = {"x-app-session": token} if token else {}


def with_temp_db(database_path: Path):
    original_path = db.DB_PATH
    db.DB_PATH = database_path
    db.init_db(database_path)
    return original_path


def order_item() -> OrderItemRow:
    return OrderItemRow(
        order_no="ORD-1",
        row_number=2,
        order_date_raw="46174",
        order_date="2026-06-01",
        design_link="https://drive.google.com/file/d/file123/view",
        mockup_link="",
        sku="SKU-A",
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


def session_for_user(user_id: int) -> str:
    token = new_session_token()
    db.create_session(token, user_id, session_expiry_string())
    return token


def test_password_hash_and_verify() -> None:
    password_hash = hash_password("secret")

    assert verify_password("secret", password_hash)
    assert not verify_password("wrong", password_hash)
    assert not verify_password("secret", "bad-hash")


def test_sessions_and_disabled_user(tmp_path: Path) -> None:
    original_path = with_temp_db(tmp_path / "app.db")
    try:
        user_id = db.create_user("alice", hash_password("pw"), role="operator")
        token = new_session_token()
        db.create_session(token, user_id, session_expiry_string())

        user = db.get_user_by_session(token, utc_now_string())
        assert user is not None
        assert user["username"] == "alice"

        db.update_user_status(user_id, "disabled")
        assert db.get_user_by_session(token, utc_now_string()) is None
    finally:
        db.DB_PATH = original_path


def test_initial_accounts_seed_developer_and_two_admins(tmp_path: Path, monkeypatch) -> None:
    original_path = with_temp_db(tmp_path / "app.db")
    monkeypatch.setenv("DEVELOPER_USERNAME", "owner")
    monkeypatch.setenv("DEVELOPER_PASSWORD", "pw")
    monkeypatch.setenv("ADMIN1_USERNAME", "manager-a")
    monkeypatch.setenv("ADMIN1_PASSWORD", "pw")
    monkeypatch.setenv("ADMIN2_USERNAME", "manager-b")
    monkeypatch.setenv("ADMIN2_PASSWORD", "pw")
    try:
        ensure_initial_accounts()

        assert db.get_user_by_username("owner")["role"] == "developer"
        assert db.get_user_by_username("manager-a")["role"] == "admin"
        assert db.get_user_by_username("manager-b")["role"] == "admin"
    finally:
        db.DB_PATH = original_path


def test_batch_visibility_by_role(tmp_path: Path) -> None:
    original_path = with_temp_db(tmp_path / "app.db")
    try:
        developer_id = db.create_user("dev", hash_password("pw"), role="developer")
        admin_id = db.create_user("admin", hash_password("pw"), role="admin")
        operator_id = db.create_user("op", hash_password("pw"), role="operator")
        other_id = db.create_user("other", hash_password("pw"), role="operator")
        developer = db.get_user(developer_id)
        admin = db.get_user(admin_id)
        operator = db.get_user(operator_id)
        other = db.get_user(other_id)
        assert developer is not None and admin is not None and operator is not None and other is not None

        own_batch = db.create_batch("own.xlsx", Path("source.xlsx"), operator_id)
        other_batch = db.create_batch("other.xlsx", Path("source.xlsx"), other_id)
        legacy_batch = db.create_batch("legacy.xlsx", Path("source.xlsx"))

        assert {batch["id"] for batch in db.list_batches_for_user(developer)} == {
            own_batch,
            other_batch,
            legacy_batch,
        }
        assert {batch["id"] for batch in db.list_batches_for_user(admin)} == {
            own_batch,
            other_batch,
            legacy_batch,
        }
        assert [batch["id"] for batch in db.list_batches_for_user(operator)] == [
            own_batch
        ]

        assert require_batch_access(own_batch, operator)["id"] == own_batch
        assert require_batch_operation(own_batch, operator)["id"] == own_batch
        assert require_batch_access(other_batch, admin)["id"] == other_batch
        with pytest.raises(Exception) as admin_operation:
            require_batch_operation(other_batch, admin)
        assert getattr(admin_operation.value, "status_code") == 403
        assert require_batch_operation(other_batch, developer)["id"] == other_batch
        with pytest.raises(Exception) as exc_info:
            require_batch_access(other_batch, operator)
        assert getattr(exc_info.value, "status_code") == 403
        with pytest.raises(Exception) as legacy_exc:
            require_batch_access(legacy_batch, operator)
        assert getattr(legacy_exc.value, "status_code") == 403
    finally:
        db.DB_PATH = original_path


def test_download_usage_counts_only_formal_batches(tmp_path: Path) -> None:
    original_path = with_temp_db(tmp_path / "app.db")
    try:
        user_id = db.create_user("op", hash_password("pw"), role="operator")
        draft_batch = db.create_batch("draft.xlsx", Path("source.xlsx"), user_id)
        formal_batch = db.create_batch("formal.xlsx", Path("source.xlsx"), user_id)
        db.insert_import_items(draft_batch, [])
        db.insert_import_items(formal_batch, [])
        db.update_batch_status(draft_batch, "precheck_ready")
        db.update_batch_status(formal_batch, "confirmed")
        with db.connect() as conn:
            conn.execute(
                """
                UPDATE import_batches
                SET order_count = 2, item_count = 3, link_count = 4,
                    success_count = 1, failed_count = 1
                WHERE id = ?
                """,
                (formal_batch,),
            )

        rows = db.list_download_usage_by_user()
        totals = db.download_usage_totals(rows)

        assert len(rows) == 1
        assert rows[0]["user_id"] == user_id
        assert rows[0]["formal_batch_count"] == 1
        assert rows[0]["order_count"] == 2
        assert rows[0]["download_item_count"] == 4
        assert rows[0]["downloaded_count"] == 1
        assert rows[0]["failed_count"] == 1
        assert totals["formal_batch_count"] == 1
    finally:
        db.DB_PATH = original_path


def test_batch_business_display_name_sequences_by_user_and_day(tmp_path: Path) -> None:
    original_path = with_temp_db(tmp_path / "app.db")
    try:
        user_id = db.create_user("op", hash_password("pw"), role="operator")
        other_id = db.create_user("other", hash_password("pw"), role="operator")
        user = db.get_user(user_id)
        assert user is not None

        first = db.create_batch("first.xlsx", Path("source.xlsx"), user_id)
        second = db.create_batch("second.xlsx", Path("source.xlsx"), user_id)
        other = db.create_batch("other.xlsx", Path("source.xlsx"), other_id)

        rows = {
            row["id"]: row for row in enrich_batch_work_queue(db.list_batches_for_user(user))
        }

        assert rows[first]["display_name"].endswith("-001")
        assert rows[second]["display_name"].endswith("-002")
        assert db.get_batch(other)["user_daily_sequence"] == 1
    finally:
        db.DB_PATH = original_path


def test_batch_download_name_defaults_from_uploaded_file_and_deduplicates_by_user(
    tmp_path: Path,
) -> None:
    original_path = with_temp_db(tmp_path / "app.db")
    try:
        user_id = db.create_user("op", hash_password("pw"), role="operator")
        other_id = db.create_user("other", hash_password("pw"), role="operator")

        first = db.create_batch("June Orders.xlsx", Path("source.xlsx"), user_id)
        second = db.create_batch("June Orders.xlsx", Path("source.xlsx"), user_id)
        other = db.create_batch("June Orders.xlsx", Path("source.xlsx"), other_id)
        unsafe = db.create_batch('bad:/name?.xlsx', Path("source.xlsx"), user_id)

        assert db.get_batch(first)["download_name"] == "June Orders"
        assert db.get_batch(second)["download_name"] == "June Orders-2"
        assert db.get_batch(other)["download_name"] == "June Orders"
        assert db.get_batch(unsafe)["download_name"] == "bad__name_"
    finally:
        db.DB_PATH = original_path


def test_init_db_backfills_legacy_batch_download_name(tmp_path: Path) -> None:
    original_path = with_temp_db(tmp_path / "app.db")
    try:
        user_id = db.create_user("op", hash_password("pw"), role="operator")
        first = db.create_batch("legacy.xlsx", Path("source.xlsx"), user_id)
        second = db.create_batch("legacy.xlsx", Path("source.xlsx"), user_id)
        with db.connect() as conn:
            conn.execute("UPDATE import_batches SET download_name = NULL")

        db.init_db(tmp_path / "app.db")

        assert db.get_batch(first)["download_name"] == "legacy"
        assert db.get_batch(second)["download_name"] == "legacy-2"
    finally:
        db.DB_PATH = original_path


def test_init_db_backfills_legacy_batch_business_display_name(tmp_path: Path) -> None:
    original_path = with_temp_db(tmp_path / "app.db")
    try:
        user_id = db.create_user("op", hash_password("pw"), role="operator")
        first = db.create_batch("first.xlsx", Path("source.xlsx"), user_id)
        second = db.create_batch("second.xlsx", Path("source.xlsx"), user_id)
        with db.connect() as conn:
            conn.execute(
                """
                UPDATE import_batches
                SET created_at = '2026-06-25 08:00:00',
                    business_date = NULL,
                    user_daily_sequence = NULL
                WHERE id = ?
                """,
                (first,),
            )
            conn.execute(
                """
                UPDATE import_batches
                SET created_at = '2026-06-25 09:00:00',
                    business_date = NULL,
                    user_daily_sequence = NULL
                WHERE id = ?
                """,
                (second,),
            )

        db.init_db(db.DB_PATH)

        first_batch = db.get_batch(first)
        second_batch = db.get_batch(second)
        assert first_batch["business_date"] == "20260625"
        assert first_batch["user_daily_sequence"] == 1
        assert second_batch["business_date"] == "20260625"
        assert second_batch["user_daily_sequence"] == 2
    finally:
        db.DB_PATH = original_path


def test_developer_can_download_source_file(tmp_path: Path) -> None:
    original_path = with_temp_db(tmp_path / "app.db")
    try:
        developer_id = db.create_user("dev", hash_password("pw"), role="developer")
        admin_id = db.create_user("admin", hash_password("pw"), role="admin")
        developer_token = session_for_user(developer_id)
        admin_token = session_for_user(admin_id)
        source_path = tmp_path / "source.xlsx"
        source_path.write_bytes(b"xlsx")
        batch_id = db.create_batch("orders.xlsx", source_path, admin_id)

        with pytest.raises(Exception) as forbidden:
            download_source_file(FakeRequest(admin_token), batch_id)
        assert getattr(forbidden.value, "status_code") == 403

        response = download_source_file(FakeRequest(developer_token), batch_id)

        assert Path(response.path) == source_path
        assert response.headers["content-disposition"].endswith('filename="orders.xlsx"')
    finally:
        db.DB_PATH = original_path


def test_batch_download_name_can_be_updated_by_owner_or_developer(tmp_path: Path) -> None:
    original_path = with_temp_db(tmp_path / "app.db")
    try:
        developer_id = db.create_user("dev", hash_password("pw"), role="developer")
        owner_id = db.create_user("owner", hash_password("pw"), role="operator")
        other_id = db.create_user("other", hash_password("pw"), role="operator")
        developer_token = session_for_user(developer_id)
        owner_token = session_for_user(owner_id)
        other_token = session_for_user(other_id)
        batch_id = db.create_batch("orders.xlsx", Path("source.xlsx"), owner_id)

        with pytest.raises(Exception) as forbidden:
            update_batch_download_name_route(
                FakeRequest(other_token),
                batch_id,
                download_name="other-name",
            )
        assert getattr(forbidden.value, "status_code") == 403

        response = update_batch_download_name_route(
            FakeRequest(owner_token),
            batch_id,
            download_name="June Orders",
        )
        assert response.status_code == 303
        assert db.get_batch(batch_id)["download_name"] == "June Orders"

        response = update_batch_download_name_route(
            FakeRequest(developer_token),
            batch_id,
            download_name="Final:Orders?",
        )
        assert response.status_code == 303
        assert db.get_batch(batch_id)["download_name"] == "Final_Orders_"
    finally:
        db.DB_PATH = original_path


def test_batch_download_name_cannot_be_updated_while_downloading(tmp_path: Path) -> None:
    original_path = with_temp_db(tmp_path / "app.db")
    try:
        user_id = db.create_user("op", hash_password("pw"), role="operator")
        token = session_for_user(user_id)
        batch_id = db.create_batch("orders.xlsx", Path("source.xlsx"), user_id)
        db.update_batch_status(batch_id, "processing")

        with pytest.raises(Exception) as blocked:
            update_batch_download_name_route(
                FakeRequest(token),
                batch_id,
                download_name="new-name",
            )
        assert getattr(blocked.value, "status_code") == 400
        assert db.get_batch(batch_id)["download_name"] == "orders"
    finally:
        db.DB_PATH = original_path


def test_archive_download_uses_batch_download_name(tmp_path: Path) -> None:
    original_path = with_temp_db(tmp_path / "app.db")
    try:
        user_id = db.create_user("op", hash_password("pw"), role="operator")
        token = session_for_user(user_id)
        archive_path = tmp_path / "batch-1.zip"
        archive_path.write_bytes(b"zip")
        batch_id = db.create_batch("orders.xlsx", Path("source.xlsx"), user_id)
        db.update_batch_download_name(batch_id, "Customer Orders")
        db.set_batch_archive(batch_id, archive_path)

        response = download_archive(FakeRequest(token), batch_id)

        assert Path(response.path) == archive_path
        assert (
            "filename*=utf-8''Customer%20Orders.zip"
            in response.headers["content-disposition"]
        )
    finally:
        db.DB_PATH = original_path


def test_developer_delete_batch_requires_confirmation_and_safe_state(
    tmp_path: Path, monkeypatch
) -> None:
    original_path = with_temp_db(tmp_path / "app.db")
    removed_batches = []
    monkeypatch.setattr(
        "app.main.remove_batch_files",
        lambda batch_id: removed_batches.append(batch_id),
    )
    try:
        developer_id = db.create_user("dev", hash_password("pw"), role="developer")
        admin_id = db.create_user("admin", hash_password("pw"), role="admin")
        developer_token = session_for_user(developer_id)
        admin_token = session_for_user(admin_id)
        batch_id = db.create_batch("orders.xlsx", Path("source.xlsx"), admin_id)
        db.insert_import_items(batch_id, [order_item()])
        db.update_batch_status(batch_id, "confirmed")
        item = db.get_pending_download_items(batch_id)[0]
        db.add_downloaded_file(
            download_item_id=int(item["id"]),
            batch_id=batch_id,
            order_id=int(item["order_id"]),
            order_no=item["order_no"],
            file_name="image.jpg",
            local_path=Path("auto-download/batch-1/SKU-A/image.jpg"),
            file_size=12,
        )
        db.record_extension_event(
            batch_id=batch_id,
            download_item_id=int(item["id"]),
            event="diagnostic",
        )

        with pytest.raises(Exception) as admin_delete:
            delete_batch_route(
                FakeRequest(admin_token),
                batch_id,
                confirmation=f"DELETE-BATCH-{batch_id}",
                reason="admin should not delete",
            )
        assert getattr(admin_delete.value, "status_code") == 403

        with pytest.raises(Exception) as bad_confirmation:
            delete_batch_route(
                FakeRequest(developer_token),
                batch_id,
                confirmation="delete",
                reason="bad confirmation",
            )
        assert getattr(bad_confirmation.value, "status_code") == 400

        db.update_batch_status(batch_id, "processing")
        with pytest.raises(Exception) as processing_delete:
            delete_batch_route(
                FakeRequest(developer_token),
                batch_id,
                confirmation=f"DELETE-BATCH-{batch_id}",
                reason="processing",
            )
        assert getattr(processing_delete.value, "status_code") == 400
        db.update_batch_status(batch_id, "confirmed")

        response = delete_batch_route(
            FakeRequest(developer_token),
            batch_id,
            confirmation=f"DELETE-BATCH-{batch_id}",
            reason="清理测试批次",
        )

        assert response.status_code == 303
        assert removed_batches == [batch_id]
        assert db.get_batch(batch_id) is None
        assert db.get_batch_orders(batch_id) == []
        with db.connect() as conn:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM download_items WHERE batch_id = ?",
                    (batch_id,),
                ).fetchone()[0]
                == 0
            )
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM downloaded_files WHERE batch_id = ?",
                    (batch_id,),
                ).fetchone()[0]
                == 0
            )
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM extension_events WHERE batch_id = ?",
                    (batch_id,),
                ).fetchone()[0]
                == 0
            )
        audit_rows = db.list_batch_deletion_audit()
        assert len(audit_rows) == 1
        assert audit_rows[0]["batch_id"] == batch_id
        assert audit_rows[0]["file_name"] == "orders.xlsx"
        assert audit_rows[0]["deleted_by_user_id"] == developer_id
        assert audit_rows[0]["delete_reason"] == "清理测试批次"
    finally:
        db.DB_PATH = original_path


def test_developer_delete_batch_blocks_downloading_items(tmp_path: Path) -> None:
    original_path = with_temp_db(tmp_path / "app.db")
    try:
        developer_id = db.create_user("dev", hash_password("pw"), role="developer")
        developer_token = session_for_user(developer_id)
        batch_id = db.create_batch("orders.xlsx", Path("source.xlsx"), developer_id)
        db.insert_import_items(batch_id, [order_item()])
        db.update_batch_status(batch_id, "confirmed")
        item = db.get_pending_download_items(batch_id)[0]
        db.mark_download_started(int(item["id"]))

        with pytest.raises(Exception) as exc:
            delete_batch_route(
                FakeRequest(developer_token),
                batch_id,
                confirmation=f"DELETE-BATCH-{batch_id}",
                reason="downloading",
            )

        assert getattr(exc.value, "status_code") == 400
        assert db.get_batch(batch_id) is not None
        assert db.list_batch_deletion_audit() == []
    finally:
        db.DB_PATH = original_path
