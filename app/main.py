from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.core import database as db
from app.core.downloader import ERROR_LABELS, parse_drive_resource, safe_filename
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
from scripts.download_speed_report import build_speed_report, format_seconds


app = FastAPI(title="Order Design Image Downloader")
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

SESSION_COOKIE = "app_session"


STATUS_LABELS = {
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


WORK_STATE_LABELS = {
    "action_required": "需要处理",
    "running": "下载中",
    "ready": "可开始",
    "blocked": "需修正",
    "complete": "已完成",
    "waiting": "处理中",
}


def configured_extension_max_attempts() -> int:
    try:
        return int(os.getenv("EXTENSION_DOWNLOAD_MAX_ATTEMPTS", "4"))
    except ValueError:
        return 4


EXTENSION_MAX_ATTEMPTS = configured_extension_max_attempts()
EXTENSION_RETRYABLE_ERROR_CODES = {
    "network_error",
    "download_timeout",
    "extension_download_failed",
    "extension_download_interrupted",
    "extension_download_timeout",
    "extension_download_stalled",
    "extension_fetch_timeout",
}
EXTENSION_NON_RETRYABLE_ERROR_CODES = {
    "extension_stopped_by_user",
    "extension_non_image_download",
    "extension_google_apps_file",
    "invalid_drive_url",
    "drive_not_found_or_permission",
    "drive_permission_denied",
}


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
    token = request.cookies.get(SESSION_COOKIE) or request.headers.get("x-app-session")
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


def batch_work_state(batch: dict, counts: dict[str, int]) -> dict[str, str]:
    pending_or_failed = int(counts["pending"]) + int(counts["failed"])
    if batch["status"] == "needs_fix":
        return {
            "code": "blocked",
            "label": WORK_STATE_LABELS["blocked"],
            "next_action": "修正表格后重新上传",
        }
    if batch["status"] in {"parsing", "pending"}:
        return {
            "code": "waiting",
            "label": WORK_STATE_LABELS["waiting"],
            "next_action": "等待解析完成",
        }
    if int(counts["downloading"]) > 0 or batch["status"] == "downloading":
        return {
            "code": "running",
            "label": WORK_STATE_LABELS["running"],
            "next_action": "查看进度或停止本批次",
        }
    if int(counts["failed"]) > 0:
        return {
            "code": "action_required",
            "label": WORK_STATE_LABELS["action_required"],
            "next_action": "重试失败项或标记已处理",
        }
    if pending_or_failed > 0 and batch["status"] == "review_ready":
        return {
            "code": "ready",
            "label": WORK_STATE_LABELS["ready"],
            "next_action": "开始下载待处理项",
        }
    return {
        "code": "complete",
        "label": WORK_STATE_LABELS["complete"],
        "next_action": "下载订单 ZIP 或归档",
    }


def enrich_batch_work_queue(batches: list[dict]) -> list[dict]:
    priority = {
        "action_required": 1,
        "running": 2,
        "ready": 3,
        "blocked": 4,
        "waiting": 5,
        "complete": 6,
    }
    enriched = []
    for batch in batches:
        counts = db.get_batch_status_counts(int(batch["id"]))
        work_state = batch_work_state(batch, counts)
        row = {
            **batch,
            "status_label": STATUS_LABELS.get(batch["status"], batch["status"]),
            "status_counts": counts,
            "work_state": work_state,
            "pending_or_failed_count": int(counts["pending"]) + int(counts["failed"]),
            "handled_count": int(counts["downloaded"]) + int(counts["manual_done"]),
        }
        enriched.append(row)
    return sorted(
        enriched,
        key=lambda row: (
            priority.get(row["work_state"]["code"], 99),
            -int(row["id"]),
        ),
    )


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
                    "download_item_id": task.get("download_item_id"),
                    "sku": row["item"].get("sku") or "-",
                    "source_type": task.get("source_type") or "design",
                    "duration_label": task.get("download_duration_label"),
                }
    return None


def current_download_task_from_orders(orders: list[dict]) -> Optional[dict]:
    for order in orders:
        for item in order.get("items", []):
            for task in item.get("download_items", []):
                if task.get("download_status") == "downloading":
                    return {
                        "download_item_id": task.get("download_item_id"),
                        "sku": item.get("sku") or "-",
                        "source_type": task.get("source_type") or "design",
                        "duration_label": task.get("download_duration_label"),
                    }
    return None


def batch_download_actions(batch: dict, counts: dict[str, int]) -> dict[str, bool]:
    has_pending_work = int(counts["pending"]) + int(counts["failed"]) > 0
    has_downloading = int(counts["downloading"]) > 0
    can_start = (
        batch["status"] != "needs_fix"
        and has_pending_work
        and not has_downloading
    )
    return {
        "can_start_extension": can_start,
        "can_retry_failed": int(counts["failed"]) > 0 and not has_downloading,
        "can_refresh": True,
    }


def batch_primary_action(batch: dict, counts: dict[str, int]) -> dict[str, str]:
    work_state = batch_work_state(batch, counts)
    actions = batch_download_actions(batch, counts)
    if work_state["code"] == "blocked":
        title = "导入预检未通过"
        body = "补齐 SKU 和 Design Link 后重新上传。"
        cta = "查看预检问题"
    elif work_state["code"] == "running":
        title = "正在下载素材"
        body = "插件正在处理当前批次。下载中可停止本批次，其他处理动作会暂时禁用。"
        cta = "查看当前下载"
    elif actions["can_retry_failed"]:
        title = "有失败项需要处理"
        body = "失败项已集中列在下方。可以重试失败项，或确认已人工处理后标记完成。"
        cta = "处理失败项"
    elif actions["can_start_extension"]:
        title = "批次已准备好"
        body = "确认 SKU 和下载链接后，开始下载待处理项。"
        cta = "开始下载待处理项"
    elif work_state["code"] == "complete":
        title = "批次已完成"
        body = "当前没有待处理下载项。可以下载 ZIP 或查看明细。"
        cta = "下载订单 ZIP"
    else:
        title = "批次处理中"
        body = "系统正在处理导入或状态恢复，请稍后刷新。"
        cta = "刷新状态"
    return {
        **work_state,
        "title": title,
        "body": body,
        "cta": cta,
    }


def refresh_batch_status_after_extension_update(batch_id: int) -> None:
    db.refresh_batch_counts(batch_id)
    counts = db.get_batch_status_counts(batch_id)
    if counts["downloading"] > 0:
        db.update_batch_status(batch_id, "downloading")
    elif counts["failed"] > 0:
        db.update_batch_status(batch_id, "completed_with_errors")
    elif counts["pending"] > 0:
        db.update_batch_status(batch_id, "review_ready")
    else:
        db.update_batch_status(batch_id, "completed")


def recover_stale_extension_downloads(batch_id: int) -> int:
    recovered = db.recover_stale_downloading_items(batch_id)
    if recovered:
        refresh_batch_status_after_extension_update(batch_id)
    return recovered


def drive_resource_payload(url: str) -> dict[str, str]:
    try:
        resource = parse_drive_resource(url)
    except ValueError:
        return {"resource_kind": "url", "resource_id": ""}
    return {"resource_kind": resource.kind, "resource_id": resource.resource_id}


def extension_download_item_payload(item: dict[str, Any]) -> dict[str, Any]:
    source_type = item.get("source_type") or "design"
    sku = item.get("item_sku") or item.get("sku") or f"row-{item['row_number']}"
    folder = f"auto-download/batch-{item['batch_id']}/{safe_filename(str(sku))}"
    payload = {
        "download_item_id": int(item["id"]),
        "batch_id": int(item["batch_id"]),
        "order_id": int(item["order_id"]),
        "order_no": item["order_no"],
        "row_number": int(item["row_number"]),
        "sku": sku,
        "sku_folder": folder,
        "source_type": source_type,
        "url": item["design_link"],
        "filename_prefix": f"{source_type}-{item['id']}-",
        "status": item["status"],
        "attempt_count": int(item.get("attempt_count") or 0),
        "max_attempts": extension_max_attempts(),
    }
    payload.update(drive_resource_payload(str(item["design_link"])))
    return payload


def request_json_dict(body: Any) -> dict[str, Any]:
    return body if isinstance(body, dict) else {}


def extension_max_attempts() -> int:
    return max(1, EXTENSION_MAX_ATTEMPTS)


def normalize_extension_raw_error(data: dict[str, Any]) -> tuple[str, str, str]:
    raw_code = str(
        data.get("raw_error_code")
        or data.get("error_code")
        or "extension_download_failed"
    )
    raw_message = str(
        data.get("raw_error_message")
        or data.get("error_message")
        or "浏览器插件下载失败。"
    )
    raw_detail = str(
        data.get("raw_error_detail")
        or data.get("error_detail")
        or raw_message
    )
    return raw_code, raw_message, raw_detail


def classify_extension_failure(data: dict[str, Any]) -> dict[str, str | bool]:
    raw_code, raw_message, raw_detail = normalize_extension_raw_error(data)
    combined = f"{raw_code} {raw_message} {raw_detail}".lower()
    if raw_code == "extension_stopped_by_user" or "用户停止" in combined:
        code = "extension_stopped_by_user"
        message = "用户停止了插件下载。"
    elif raw_code in {"extension_non_image_download", "extension_google_apps_file"}:
        code = raw_code
        message = (
            "链接指向 Google 在线文件，不是原始图片。"
            if raw_code == "extension_google_apps_file"
            else "插件下载到了非图片文件。"
        )
    elif "google apps" in combined or "application/vnd.google-apps" in combined:
        code = "extension_google_apps_file"
        message = "链接指向 Google 在线文件，不是原始图片。"
    elif "non_image" in combined or "不是图片" in combined or "html" in combined:
        code = "extension_non_image_download"
        message = "插件下载到了非图片文件。"
    elif "404" in combined or "403" in combined or "permission" in combined or "权限" in combined:
        code = "drive_not_found_or_permission"
        message = "Drive 文件不存在或当前 Google 账号没有访问权限。"
    elif "timeout" in combined or "stalled" in combined or "超时" in combined:
        code = "download_timeout"
        message = "浏览器下载超时或长时间没有进展。"
    elif "network" in combined or "server_unreachable" in combined:
        code = "network_error"
        message = "浏览器网络连接失败，可稍后重试。"
    else:
        code = raw_code if raw_code else "extension_download_failed"
        message = raw_message or "浏览器插件下载失败。"
    retryable = code in EXTENSION_RETRYABLE_ERROR_CODES and code not in EXTENSION_NON_RETRYABLE_ERROR_CODES
    return {
        "code": code,
        "message": message,
        "detail": raw_detail,
        "retryable": retryable,
    }


def should_retry_extension_failure(item: dict, classified: dict[str, str | bool]) -> bool:
    if not bool(classified["retryable"]):
        return False
    return int(item.get("attempt_count") or 0) < extension_max_attempts()


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
    batches = enrich_batch_work_queue(db.list_batches_for_user(user))
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context=template_context(user, batches=batches),
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
    stale_recovered_count = recover_stale_extension_downloads(batch_id)
    if stale_recovered_count:
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
    primary_action = batch_primary_action(batch, status_counts)
    filter_counts = {
        "all": len(display_rows),
        "failed": sum(1 for row in display_rows if row["item"].get("download_status") == "failed"),
        "downloading": sum(1 for row in display_rows if row["item"].get("download_status") == "downloading"),
        "pending": sum(1 for row in display_rows if row["item"].get("download_status") == "pending"),
        "completed": sum(1 for row in display_rows if row["item"].get("download_status") == "downloaded"),
    }
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
            status_labels=STATUS_LABELS,
            handled_count=handled_count,
            progress_percent=progress_percent,
            batch_archive_ready=batch_archive_ready,
            error_labels=ERROR_LABELS,
            import_summary=import_summary,
            stale_recovered_count=stale_recovered_count,
            primary_action=primary_action,
            filter_counts=filter_counts,
        ),
    )


@app.get("/batches/{batch_id}/speed-report")
def batch_speed_report(request: Request, batch_id: int):
    user = require_user(request)
    batch = require_batch_access(batch_id, user)
    report = build_speed_report(batch_id, db.DB_PATH, slow_limit=20)
    return templates.TemplateResponse(
        request=request,
        name="speed_report.html",
        context=template_context(
            user,
            batch=batch,
            report=report,
            format_seconds=format_seconds,
        ),
    )


@app.get("/batches/{batch_id}/status")
def batch_status(request: Request, batch_id: int):
    user = require_user(request)
    batch = require_batch_access(batch_id, user)
    stale_recovered_count = recover_stale_extension_downloads(batch_id)
    if stale_recovered_count:
        batch = require_batch_access(batch_id, user)
    orders = db.get_batch_orders(batch_id)
    enrich_order_download_durations(orders)
    counts = db.get_batch_status_counts(batch_id)
    return {
        "batch": batch,
        "status_counts": counts,
        "current_task": current_download_task_from_orders(orders),
        "stale_recovered_count": stale_recovered_count,
        "actions": batch_download_actions(batch, counts),
    }


@app.get("/api/extension/batches/{batch_id}/download-items")
def extension_download_items(request: Request, batch_id: int, limit: int = 50):
    user = require_user(request)
    batch = require_batch_access(batch_id, user)
    if recover_stale_extension_downloads(batch_id):
        batch = require_batch_access(batch_id, user)
    items = db.get_extension_download_items(batch_id, limit)
    counts = db.get_batch_status_counts(batch_id)
    return {
        "batch": {
            "id": int(batch["id"]),
            "file_name": batch["file_name"],
            "status": batch["status"],
        },
        "status_counts": counts,
        "items": [extension_download_item_payload(item) for item in items],
    }


@app.post("/api/extension/batches/{batch_id}/next-download-item")
def extension_next_download_item(request: Request, batch_id: int):
    user = require_user(request)
    batch = require_batch_access(batch_id, user)
    if recover_stale_extension_downloads(batch_id):
        batch = require_batch_access(batch_id, user)
    if batch["status"] == "needs_fix":
        raise HTTPException(status_code=400, detail="导入预检未通过，请先修正表格后重新上传。")

    item = db.get_next_extension_download_item(
        batch_id,
        EXTENSION_RETRYABLE_ERROR_CODES,
        extension_max_attempts(),
    )
    if not item:
        counts = db.get_batch_status_counts(batch_id)
        refresh_batch_status_after_extension_update(batch_id)
        return {
            "ok": True,
            "item": None,
            "status_counts": counts,
            "max_attempts": extension_max_attempts(),
        }

    dispatched = db.dispatch_download_item(int(item["id"]))
    if not dispatched:
        raise HTTPException(status_code=409, detail="Download item could not be dispatched")
    refresh_batch_status_after_extension_update(batch_id)
    db.record_extension_event(
        batch_id=batch_id,
        download_item_id=int(dispatched["id"]),
        event="item_dispatched",
        message=f"attempt {int(dispatched.get('attempt_count') or 0)}/{extension_max_attempts()}",
        detail={"download_item_id": int(dispatched["id"])},
    )
    return {
        "ok": True,
        "item": extension_download_item_payload(dispatched),
        "status_counts": db.get_batch_status_counts(batch_id),
        "max_attempts": extension_max_attempts(),
    }


@app.post("/api/extension/download-items/{download_item_id}/start")
def extension_start_download_item(request: Request, download_item_id: int):
    user = require_user(request)
    item = db.get_download_item(download_item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Download item not found")
    require_batch_access(int(item["batch_id"]), user)
    db.clear_downloaded_files(download_item_id)
    db.mark_download_started(download_item_id)
    refresh_batch_status_after_extension_update(int(item["batch_id"]))
    return {"ok": True, "download_item_id": download_item_id}


@app.post("/api/extension/download-items/{download_item_id}/heartbeat")
def extension_download_item_heartbeat(request: Request, download_item_id: int):
    user = require_user(request)
    item = db.get_download_item(download_item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Download item not found")
    require_batch_access(int(item["batch_id"]), user)
    updated = db.mark_download_heartbeat(download_item_id)
    return {
        "ok": True,
        "download_item_id": download_item_id,
        "updated": updated,
        "status": item["status"],
    }


@app.post("/api/extension/download-items/{download_item_id}/success")
def extension_download_item_success(
    request: Request,
    download_item_id: int,
    body: Any = Body(default=None),
):
    user = require_user(request)
    item = db.get_download_item(download_item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Download item not found")
    require_batch_access(int(item["batch_id"]), user)
    data = request_json_dict(body)
    files = data.get("files") if isinstance(data.get("files"), list) else []
    image_count = data.get("image_count")
    try:
        image_count = int(image_count)
    except (TypeError, ValueError):
        image_count = len(files)
    if image_count <= 0:
        image_count = 1
    db.replace_downloaded_files_for_item(download_item_id, files)
    db.mark_download_success(download_item_id, image_count)
    refresh_batch_status_after_extension_update(int(item["batch_id"]))
    return {"ok": True, "download_item_id": download_item_id, "image_count": image_count}


@app.post("/api/extension/download-items/{download_item_id}/failure")
def extension_download_item_failure(
    request: Request,
    download_item_id: int,
    body: Any = Body(default=None),
):
    user = require_user(request)
    item = db.get_download_item(download_item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Download item not found")
    require_batch_access(int(item["batch_id"]), user)
    data = request_json_dict(body)
    files = data.get("files") if isinstance(data.get("files"), list) else []
    partial_image_count = data.get("partial_image_count")
    try:
        partial_image_count = int(partial_image_count)
    except (TypeError, ValueError):
        partial_image_count = len(files)
    partial_image_count = max(0, partial_image_count)
    classified = classify_extension_failure(data)
    error_code = str(classified["code"])
    error_message = str(classified["message"])
    error_detail = str(classified["detail"] or "")
    if files:
        db.replace_downloaded_files_for_item(download_item_id, files)
    if should_retry_extension_failure(item, classified):
        db.mark_download_retry_pending(
            download_item_id,
            error_message,
            error_code=error_code,
            error_detail=error_detail,
            image_count=partial_image_count,
        )
        final_status = "pending"
    else:
        db.mark_download_failed(
            download_item_id,
            error_message,
            error_code=error_code,
            error_detail=error_detail,
            image_count=partial_image_count,
        )
        final_status = "failed"
    db.record_extension_event(
        batch_id=int(item["batch_id"]),
        download_item_id=download_item_id,
        event="item_failure",
        level="warning",
        message=error_message,
        detail={
            "raw": data,
            "classified_code": error_code,
            "final_status": final_status,
            "attempt_count": int(item.get("attempt_count") or 0),
            "max_attempts": extension_max_attempts(),
        },
    )
    refresh_batch_status_after_extension_update(int(item["batch_id"]))
    return {
        "ok": True,
        "download_item_id": download_item_id,
        "partial_image_count": partial_image_count,
        "status": final_status,
        "error_code": error_code,
        "retryable": final_status == "pending",
    }


@app.post("/api/extension/events")
def extension_events(request: Request, body: Any = Body(default=None)):
    user = require_user(request)
    data = request_json_dict(body)
    batch_id = int(data.get("batch_id") or 0)
    if batch_id <= 0:
        raise HTTPException(status_code=400, detail="batch_id is required")
    require_batch_access(batch_id, user)
    download_item_id = data.get("download_item_id")
    try:
        download_item_id = int(download_item_id) if download_item_id else None
    except (TypeError, ValueError):
        download_item_id = None
    event_id = db.record_extension_event(
        batch_id=batch_id,
        download_item_id=download_item_id,
        event=str(data.get("event") or "event"),
        level=str(data.get("level") or "info"),
        message=str(data.get("message") or ""),
        detail=data.get("detail") if isinstance(data.get("detail"), dict) else None,
    )
    return {"ok": True, "event_id": event_id}


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
