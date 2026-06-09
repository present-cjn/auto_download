from __future__ import annotations

from pathlib import Path

import pytest

from app.core import database as db
from app.core.security import (
    hash_password,
    new_session_token,
    session_expiry_string,
    utc_now_string,
    verify_password,
)
from app.main import require_batch_access


def with_temp_db(database_path: Path):
    original_path = db.DB_PATH
    db.DB_PATH = database_path
    db.init_db(database_path)
    return original_path


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


def test_batch_visibility_by_role(tmp_path: Path) -> None:
    original_path = with_temp_db(tmp_path / "app.db")
    try:
        admin_id = db.create_user("admin", hash_password("pw"), role="admin")
        operator_id = db.create_user("op", hash_password("pw"), role="operator")
        other_id = db.create_user("other", hash_password("pw"), role="operator")
        admin = db.get_user(admin_id)
        operator = db.get_user(operator_id)
        other = db.get_user(other_id)
        assert admin is not None and operator is not None and other is not None

        own_batch = db.create_batch("own.xlsx", Path("source.xlsx"), operator_id)
        other_batch = db.create_batch("other.xlsx", Path("source.xlsx"), other_id)
        legacy_batch = db.create_batch("legacy.xlsx", Path("source.xlsx"))

        assert {batch["id"] for batch in db.list_batches_for_user(admin)} == {
            own_batch,
            other_batch,
            legacy_batch,
        }
        assert [batch["id"] for batch in db.list_batches_for_user(operator)] == [
            own_batch
        ]

        assert require_batch_access(own_batch, operator)["id"] == own_batch
        with pytest.raises(Exception) as exc_info:
            require_batch_access(other_batch, operator)
        assert getattr(exc_info.value, "status_code") == 403
        with pytest.raises(Exception) as legacy_exc:
            require_batch_access(legacy_batch, operator)
        assert getattr(legacy_exc.value, "status_code") == 403
    finally:
        db.DB_PATH = original_path
