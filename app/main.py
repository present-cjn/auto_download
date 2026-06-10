from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.core import database as db
from app.core.downloader import ERROR_LABELS
from app.core.excel_parser import build_import_summary
from app.core.security import (
    hash_password,
    new_session_token,
    session_expiry_string,
    utc_now_string,
    verify_password,
)
from app.core.tasks import (
    ARCHIVES_DIR,
    ORDERS_DIR,
    create_order_archive,
    ensure_data_dirs,
    mark_download_item_manual_done,
    process_batch,
    retry_download_item,
    retry_failed,
    retry_failed_limited,
    save_upload,
    start_download,
    start_download_limited,
    start_background,
)


app = FastAPI(title="Order Design Image Downloader")
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

SESSION_COOKIE = "app_session"


def initial_admin_credentials() -> Tuple[str, str]:
    return (
        os.getenv("ADMIN_USERNAME", "admin"),
        os.getenv("ADMIN_PASSWORD", os.getenv("APP_PASSWORD", "change-me")),
    )


def ensure_initial_admin() -> None:
    if db.user_count() > 0:
        return
    username, password = initial_admin_credentials()
    db.create_user(username, hash_password(password), role="admin")


def current_user(request: Request) -> Optional[dict]:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return db.get_user_by_session(token, utc_now_string())


def require_user(request: Request) -> dict:
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


def require_admin(user: dict) -> None:
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")


def can_access_batch(user: dict, batch: dict) -> bool:
    if user["role"] == "admin":
        return True
    return batch.get("created_by_user_id") == user["id"]


def require_batch_access(batch_id: int, user: dict) -> dict:
    batch = db.get_batch(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    if not can_access_batch(user, batch):
        raise HTTPException(status_code=403, detail="Batch access denied")
    return batch


def template_context(user: dict, **extra):
    context = {"current_user": user}
    context.update(extra)
    return context


def sort_rows_by_excel_row(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda row: int(row["item"].get("row_number") or 0))


def normalized_limit(limit: int) -> Optional[int]:
    return limit if limit > 0 else None


def parse_db_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def format_duration_seconds(seconds: int) -> str:
    seconds = max(0, seconds)
    minutes, remaining_seconds = divmod(seconds, 60)
    hours, remaining_minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {remaining_minutes}m {remaining_seconds}s"
    if minutes:
        return f"{minutes}m {remaining_seconds}s"
    return f"{remaining_seconds}s"


def enrich_download_task_duration(task: dict, now: Optional[datetime] = None) -> None:
    started_at = parse_db_timestamp(task.get("download_started_at"))
    completed_at = parse_db_timestamp(task.get("download_completed_at"))
    if not started_at:
        task["download_duration_label"] = None
        return
    end_at = completed_at or now or datetime.utcnow()
    prefix = "已用时" if task.get("download_status") == "downloading" else "耗时"
    task["download_duration_label"] = (
        f"{prefix} {format_duration_seconds(int((end_at - started_at).total_seconds()))}"
    )


def enrich_order_download_durations(orders: list[dict]) -> None:
    now = datetime.utcnow()
    for order in orders:
        for item in order["items"]:
            for task in item.get("download_items", []):
                enrich_download_task_duration(task, now)


def current_download_task(display_rows: list[dict]) -> Optional[dict]:
    for row in display_rows:
        for task in row["item"].get("download_items", []):
            if task.get("download_status") == "downloading":
                return {
                    "sku": row["item"].get("sku") or "-",
                    "source_type": task.get("source_type") or "design",
                    "duration_label": task.get("download_duration_label"),
                }
    return None


@app.on_event("startup")
def startup() -> None:
    ensure_data_dirs()
    db.init_db()
    ensure_initial_admin()
    db.delete_expired_sessions(utc_now_string())


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/login")
def login_page(request: Request):
    if current_user(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": None, "username": ""},
    )


@app.post("/login")
def login(request: Request, username: str = Form(""), password: str = Form("")):
    user = db.get_user_by_username(username)
    if (
        not user
        or user["status"] != "active"
        or not verify_password(password, user["password_hash"])
    ):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "用户名或密码不正确", "username": username},
            status_code=401,
        )
    token = new_session_token()
    db.create_session(token, int(user["id"]), session_expiry_string())
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax")
    return response


@app.post("/logout")
def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        db.delete_session(token)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/")
def index(request: Request):
    user = require_user(request)
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context=template_context(user, batches=db.list_batches_for_user(user)),
    )


@app.get("/uploads/new")
def upload_page(request: Request):
    user = require_user(request)
    return templates.TemplateResponse(
        request=request,
        name="upload.html",
        context=template_context(user),
    )


@app.post("/uploads")
async def upload_excel(request: Request, file: UploadFile = File(...)):
    user = require_user(request)
    filename = file.filename or "orders.xlsx"
    if Path(filename).suffix.lower() != ".xlsx":
        raise HTTPException(status_code=400, detail="Only .xlsx files are supported.")

    batch_id = db.create_batch(filename, Path(""), created_by_user_id=int(user["id"]))
    content = await file.read()
    source_path = save_upload(batch_id, filename, content)
    db.update_batch_source(batch_id, source_path)
    start_background(process_batch, batch_id, source_path)
    return RedirectResponse(f"/batches/{batch_id}", status_code=303)


@app.get("/users")
def users_page(request: Request):
    user = require_user(request)
    require_admin(user)
    return templates.TemplateResponse(
        request=request,
        name="users.html",
        context=template_context(user, users=db.list_users(), error=None),
    )


@app.post("/users")
def create_user(request: Request, username: str = Form(""), password: str = Form(""), role: str = Form("operator")):
    user = require_user(request)
    require_admin(user)
    username = username.strip()
    if not username or not password:
        return templates.TemplateResponse(
            request=request,
            name="users.html",
            context=template_context(
                user,
                users=db.list_users(),
                error="用户名和密码不能为空",
            ),
            status_code=400,
        )
    if role not in {"admin", "operator"}:
        raise HTTPException(status_code=400, detail="Invalid role")
    try:
        db.create_user(username, hash_password(password), role=role)
    except sqlite3.IntegrityError:
        return templates.TemplateResponse(
            request=request,
            name="users.html",
            context=template_context(
                user,
                users=db.list_users(),
                error="用户名已存在",
            ),
            status_code=400,
        )
    return RedirectResponse("/users", status_code=303)


@app.post("/users/{user_id}/status")
def update_user_status(request: Request, user_id: int, status: str = Form("")):
    user = require_user(request)
    require_admin(user)
    if status not in {"active", "disabled"}:
        raise HTTPException(status_code=400, detail="Invalid status")
    if int(user["id"]) == user_id and status == "disabled":
        raise HTTPException(status_code=400, detail="Cannot disable current user")
    if not db.get_user(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    db.update_user_status(user_id, status)
    return RedirectResponse("/users", status_code=303)


@app.post("/users/{user_id}/password")
def reset_user_password(request: Request, user_id: int, password: str = Form("")):
    user = require_user(request)
    require_admin(user)
    if not password:
        raise HTTPException(status_code=400, detail="Password is required")
    if not db.get_user(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    db.update_user_password(user_id, hash_password(password))
    return RedirectResponse("/users", status_code=303)


@app.get("/batches/{batch_id}")
def batch_detail(request: Request, batch_id: int):
    user = require_user(request)
    batch = require_batch_access(batch_id, user)
    orders = db.get_batch_orders(batch_id)
    enrich_order_download_durations(orders)
    display_rows = []
    failed_rows = []
    for group_index, order in enumerate(orders):
        item_count = len(order["items"])
        order_archive_ready = False
        for item in order["items"]:
            sku_dir = ORDERS_DIR / str(batch_id) / str(item.get("sku") or "")
            if sku_dir.exists() and any(path.is_file() for path in sku_dir.rglob("*")):
                order_archive_ready = True
                break
        for item_index, item in enumerate(order["items"]):
            row = {
                "order": order,
                "item": item,
                "is_multi_order": item_count > 1,
                "is_first_in_order": item_index == 0,
                "order_item_count": item_count,
                "order_group_index": group_index % 2,
                "order_archive_ready": order_archive_ready,
            }
            display_rows.append(row)
            if item.get("download_status") == "failed":
                for download_item in item.get("download_items", []):
                    if download_item.get("download_status") == "failed":
                        failed_rows.append({**row, "download_item": download_item})
    display_rows = sort_rows_by_excel_row(display_rows)
    failed_rows = sort_rows_by_excel_row(failed_rows)
    current_task = current_download_task(display_rows)
    status_counts = db.get_batch_status_counts(batch_id)
    handled_count = int(batch["success_count"]) + int(status_counts["manual_done"])
    progress_percent = 0
    if int(batch["link_count"]) > 0:
        progress_percent = round(
            handled_count * 100 / int(batch["link_count"])
        )
    archive_path = batch["archive_path"]
    batch_archive_ready = bool(archive_path and Path(archive_path).exists())
    import_summary = None
    if batch.get("import_summary_json"):
        try:
            import_summary = json.loads(batch["import_summary_json"])
        except json.JSONDecodeError:
            import_summary = None
    if import_summary is None:
        import_summary = build_import_summary(
            item for order in orders for item in order["items"]
        )
    status_labels = {
        "pending": "等待",
        "parsing": "解析中",
        "review_ready": "已解析",
        "needs_fix": "需修正",
        "downloading": "下载中",
        "downloaded": "成功",
        "failed": "失败",
        "completed": "已完成",
        "completed_with_errors": "有失败项",
        "skipped": "已跳过",
        "manual_done": "手动完成",
    }
    return templates.TemplateResponse(
        request=request,
        name="batch_detail.html",
        context=template_context(
            user,
            batch=batch,
            display_rows=display_rows,
            failed_rows=failed_rows,
            current_task=current_task,
            status_counts=status_counts,
            status_labels=status_labels,
            handled_count=handled_count,
            progress_percent=progress_percent,
            batch_archive_ready=batch_archive_ready,
            error_labels=ERROR_LABELS,
            import_summary=import_summary,
        ),
    )


@app.get("/batches/{batch_id}/status")
def batch_status(request: Request, batch_id: int):
    user = require_user(request)
    batch = require_batch_access(batch_id, user)
    return {"batch": batch, "status_counts": db.get_batch_status_counts(batch_id)}


@app.post("/batches/{batch_id}/start-download")
def start_batch_download(request: Request, batch_id: int, limit: int = Form(0)):
    user = require_user(request)
    batch = require_batch_access(batch_id, user)
    if batch["status"] == "needs_fix":
        raise HTTPException(status_code=400, detail="导入预检未通过，请先修正表格后重新上传。")
    selected_limit = normalized_limit(limit)
    if selected_limit:
        start_background(start_download_limited, batch_id, selected_limit)
    else:
        start_background(start_download, batch_id)
    return RedirectResponse(f"/batches/{batch_id}", status_code=303)


@app.post("/batches/{batch_id}/retry-failed")
def retry_failed_items(request: Request, batch_id: int, limit: int = Form(0)):
    user = require_user(request)
    require_batch_access(batch_id, user)
    selected_limit = normalized_limit(limit)
    if selected_limit:
        start_background(retry_failed_limited, batch_id, selected_limit)
    else:
        start_background(retry_failed, batch_id)
    return RedirectResponse(f"/batches/{batch_id}", status_code=303)


@app.post("/download-items/{download_item_id}/retry")
def retry_one_download_item(request: Request, download_item_id: int):
    user = require_user(request)
    item = db.get_download_item(download_item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Download item not found")
    require_batch_access(int(item["batch_id"]), user)
    start_background(retry_download_item, download_item_id)
    return RedirectResponse(f"/batches/{item['batch_id']}", status_code=303)


@app.post("/download-items/{download_item_id}/manual-done")
def mark_one_download_item_manual_done(request: Request, download_item_id: int):
    user = require_user(request)
    item = db.get_download_item(download_item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Download item not found")
    require_batch_access(int(item["batch_id"]), user)
    mark_download_item_manual_done(download_item_id)
    return RedirectResponse(f"/batches/{item['batch_id']}", status_code=303)


@app.get("/batches/{batch_id}/download.zip")
def download_archive(request: Request, batch_id: int):
    user = require_user(request)
    batch = require_batch_access(batch_id, user)
    archive_path = Path(batch["archive_path"] or ARCHIVES_DIR / f"batch-{batch_id}.zip")
    if not archive_path.exists():
        raise HTTPException(status_code=404, detail="Archive is not ready")
    return FileResponse(
        archive_path,
        media_type="application/zip",
        filename=f"batch-{batch_id}-orders.zip",
    )


@app.get("/batches/{batch_id}/orders/{order_id}/download.zip")
def download_order_archive(request: Request, batch_id: int, order_id: int):
    user = require_user(request)
    require_batch_access(batch_id, user)
    order = db.get_order(order_id)
    if not order or int(order["batch_id"]) != batch_id:
        raise HTTPException(status_code=404, detail="Order not found")
    try:
        archive_path = create_order_archive(batch_id, order_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(
        archive_path,
        media_type="application/zip",
        filename=f"batch-{batch_id}-{order['order_no']}.zip",
    )
