from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from starlette.requests import Request

from app import main as app_main
from app.core import database as db
from app.core.security import hash_password, new_session_token, session_expiry_string


@pytest.fixture()
def isolated_app(tmp_path: Path, monkeypatch):
    original_path = db.DB_PATH
    db.DB_PATH = tmp_path / "app.db"
    monkeypatch.setattr(app_main, "RESOURCES_DIR", tmp_path / "resources")
    db.init_db(db.DB_PATH)
    try:
        yield tmp_path
    finally:
        db.DB_PATH = original_path


class FakeUploadFile:
    def __init__(self, filename: str, content: bytes):
        self.filename = filename
        self.content = content

    async def read(self) -> bytes:
        return self.content


def request_for_token(token: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"x-app-session", token.encode())],
        }
    )


def create_logged_in_request(role: str, username: str) -> tuple[Request, int]:
    user_id = db.create_user(username, hash_password("pw"), role=role)
    token = new_session_token()
    db.create_session(token, user_id, session_expiry_string())
    return request_for_token(token), user_id


def create_stored_resource(
    tmp_path: Path,
    uploaded_by_user_id: int,
    status: str = "active",
    filename: str = "template.xlsx",
    category: str = "header_template",
) -> int:
    resource_id = db.create_resource_file(
        "订单模板",
        category,
        filename,
        uploaded_by_user_id,
        "测试版本",
    )
    path = tmp_path / "resources" / str(resource_id) / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"resource-content")
    db.update_resource_file_storage(resource_id, path, path.stat().st_size)
    db.update_resource_file_status(resource_id, status)
    return resource_id


def test_developer_can_upload_resource_and_page_groups_it(
    isolated_app: Path,
) -> None:
    request, _developer_id = create_logged_in_request("developer", "dev")
    response = asyncio.run(
        app_main.upload_resource(
            request,
            title="订单导入表头模板",
            category="header_template",
            version_note="2026-06 版本",
            file=FakeUploadFile("headers.xlsx", b"xlsx-bytes"),
        )
    )
    page = app_main.resources_page(request)

    assert response.status_code == 303
    assert response.headers["location"] == "/resources"
    rows = db.list_resource_files(include_disabled=True)
    assert len(rows) == 1
    assert rows[0]["title"] == "订单导入表头模板"
    assert rows[0]["category"] == "header_template"
    assert rows[0]["file_size"] == len(b"xlsx-bytes")
    assert Path(rows[0]["storage_path"]).read_bytes() == b"xlsx-bytes"
    assert page.status_code == 200
    assert page.context["resource_groups"]["header_template"][0]["title"] == "订单导入表头模板"


def test_non_developer_cannot_upload_or_change_resource_status(
    isolated_app: Path,
) -> None:
    developer_id = db.create_user("dev", hash_password("pw"), role="developer")
    resource_id = create_stored_resource(isolated_app, developer_id)
    request, _operator_id = create_logged_in_request("operator", "op")

    with pytest.raises(Exception) as upload_response:
        asyncio.run(
            app_main.upload_resource(
                request,
                title="插件",
                category="browser_extension",
                version_note="",
                file=FakeUploadFile("extension.zip", b"zip-bytes"),
            )
        )
    with pytest.raises(Exception) as status_response:
        app_main.update_resource_status(
            request,
            resource_id,
            status="disabled",
        )

    assert getattr(upload_response.value, "status_code") == 403
    assert getattr(status_response.value, "status_code") == 403
    assert db.get_resource_file(resource_id, include_disabled=True)["status"] == "active"


def test_active_resource_downloads_for_logged_in_users(isolated_app: Path) -> None:
    developer_id = db.create_user("dev", hash_password("pw"), role="developer")
    resource_id = create_stored_resource(isolated_app, developer_id)
    request, _operator_id = create_logged_in_request("operator", "op")

    response = app_main.download_resource(request, resource_id)

    assert Path(response.path).read_bytes() == b"resource-content"
    assert 'filename="template.xlsx"' in response.headers["content-disposition"]


def test_disabled_resource_hidden_from_non_developer_but_available_to_developer(
    isolated_app: Path,
) -> None:
    developer_request, developer_id = create_logged_in_request("developer", "dev")
    resource_id = create_stored_resource(isolated_app, developer_id, status="disabled")
    operator_request, _operator_id = create_logged_in_request("operator", "op")

    with pytest.raises(Exception) as operator_download:
        app_main.download_resource(operator_request, resource_id)
    operator_page = app_main.resources_page(operator_request)
    developer_download = app_main.download_resource(developer_request, resource_id)
    developer_page = app_main.resources_page(developer_request)

    assert getattr(operator_download.value, "status_code") == 404
    assert operator_page.context["resource_groups"]["header_template"] == []
    assert Path(developer_download.path).read_bytes() == b"resource-content"
    assert developer_page.context["resource_groups"]["header_template"][0]["status"] == "disabled"


@pytest.mark.parametrize(
    ("category", "filename", "status_code"),
    [
        ("header_template", "headers.xlsx", 303),
        ("header_template", "headers.zip", 400),
        ("browser_extension", "extension.zip", 303),
        ("browser_extension", "extension.xlsx", 400),
        ("guide", "guide.pdf", 303),
        ("guide", "guide.exe", 400),
    ],
)
def test_resource_upload_extension_validation(
    isolated_app: Path, category: str, filename: str, status_code: int
) -> None:
    request, _developer_id = create_logged_in_request("developer", f"dev-{category}-{filename}")

    if status_code == 303:
        response = asyncio.run(
            app_main.upload_resource(
                request,
                title=filename,
                category=category,
                version_note="",
                file=FakeUploadFile(filename, b"content"),
            )
        )
        assert response.status_code == status_code
    else:
        with pytest.raises(Exception) as response:
            asyncio.run(
                app_main.upload_resource(
                    request,
                    title=filename,
                    category=category,
                    version_note="",
                    file=FakeUploadFile(filename, b"content"),
                )
            )
        assert getattr(response.value, "status_code") == status_code


def test_missing_resource_file_returns_404(isolated_app: Path) -> None:
    request, developer_id = create_logged_in_request("developer", "dev")
    resource_id = db.create_resource_file(
        "缺失文件",
        "guide",
        "guide.pdf",
        developer_id,
    )
    db.update_resource_file_storage(resource_id, isolated_app / "missing.pdf", 12)

    with pytest.raises(Exception) as response:
        app_main.download_resource(request, resource_id)

    assert getattr(response.value, "status_code") == 404
